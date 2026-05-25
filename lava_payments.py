"""
lava_payments.py — production-grade платёжная система для бота.

Архитектура (500+ юзеров):
  • Postgres — источник правды: subscriptions, payments, trials, referrals
  • Redis    — кэш проверки подписки (TTL 5 мин), снимает нагрузку с БД
  • Webhook  — идемпотентная обработка (дедупликация по lava_payment_id)
  • Trial    — 7 дней бесплатно, один на user_id навсегда

ENV переменные Railway:
  LAVA_API_KEY        — из Lava.top → Integrations → API
  LAVA_WEBHOOK_LOGIN  — Basic Auth login (например: lava)
  LAVA_WEBHOOK_PASS   — Basic Auth password (придумай сложный)
  LAVA_LINK           — ссылка на Membership страницу

Реферальная программа:
  Реферер получает 7 бонусных дней когда его приглашённый оформляет подписку.
  Ссылка: https://t.me/БОТ?start=ref_XXXXXXXX
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta

from aiohttp import web
from telegram import Bot

from db import kv_get, kv_set, kv_del, _get_pool

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Конфиг
# ══════════════════════════════════════════════════════════════════════════════

LAVA_API_KEY       = os.environ.get("LAVA_API_KEY", "")
LAVA_WEBHOOK_LOGIN = os.environ.get("LAVA_WEBHOOK_LOGIN", "lava")
LAVA_WEBHOOK_PASS  = os.environ.get("LAVA_WEBHOOK_PASS", "")
LAVA_LINK          = os.environ.get(
    "LAVA_LINK",
    "https://app.lava.top/products/8c78afe1-941d-4db7-b53b-eb097dba9215/c4d4749d-54f4-4230-8975-b6d9d649dea6"
)

BOT_USERNAME = os.environ.get("BOT_USERNAME", "")  # без @, нужен для реф-ссылок

TRIAL_DAYS      = 7    # длительность триала
REFERRAL_BONUS  = 7    # бонусных дней рефереру при конвертации
SUB_CACHE_TTL   = 300  # секунд кэша в Redis (5 минут)

TIER_DAYS   = {"1m": 30, "3m": 90, "6m": 180, "12m": 365}
TIER_LABELS = {"1m": "1 месяц", "3m": "3 месяца", "6m": "6 месяцев", "12m": "12 месяцев"}
TIER_PRICES = {"1m": 34, "3m": 133, "6m": 269, "12m": 543}   # USD (Lava-цены)
TIER_PRICES_EUR = {"1m": 31, "3m": 117, "6m": 234, "12m": 468}  # EUR (отображение)

# Пороги EUR для определения тира по сумме
_TIER_THRESHOLDS = [(32, "1m"), (120, "3m"), (240, "6m")]


# ══════════════════════════════════════════════════════════════════════════════
# Инициализация БД — вызывается из init_db() в db.py
# ══════════════════════════════════════════════════════════════════════════════

async def init_payments_db() -> None:
    """Создаёт таблицы платёжной системы. Вызывать после init_db()."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        # Подписки
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id          BIGSERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL UNIQUE,
                tier        TEXT NOT NULL DEFAULT '1m',
                status      TEXT NOT NULL DEFAULT 'active',
                contract_id TEXT,
                expires_at  TIMESTAMPTZ NOT NULL,
                granted_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sub_user
                ON subscriptions (user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sub_expires
                ON subscriptions (expires_at)
        """)

        # История платежей
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id              BIGSERIAL PRIMARY KEY,
                user_id         BIGINT NOT NULL,
                lava_payment_id TEXT UNIQUE,
                contract_id     TEXT,
                amount          NUMERIC(10,2),
                currency        TEXT DEFAULT 'EUR',
                tier            TEXT,
                event_type      TEXT,
                status          TEXT DEFAULT 'success',
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pay_user
                ON payments (user_id, created_at DESC)
        """)

        # Триалы — один на user_id навсегда
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trials (
                user_id     BIGINT PRIMARY KEY,
                started_at  TIMESTAMPTZ DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL
            )
        """)

        # Реферальная программа
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id              BIGSERIAL PRIMARY KEY,
                referrer_id     BIGINT NOT NULL,
                referred_id     BIGINT NOT NULL UNIQUE,
                ref_code        TEXT NOT NULL,
                bonus_given     BOOLEAN DEFAULT FALSE,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ref_referrer
                ON referrals (referrer_id)
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ref_code
                ON referrals (ref_code, referred_id)
        """)

        # Реф-коды пользователей
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ref_codes (
                user_id   BIGINT PRIMARY KEY,
                ref_code  TEXT UNIQUE NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

    logger.info("Payments DB tables ready")


# ══════════════════════════════════════════════════════════════════════════════
# Подписка — Postgres + Redis cache
# ══════════════════════════════════════════════════════════════════════════════

_CACHE_KEY = "__sub_cache__"


async def _invalidate_cache(user_id: int) -> None:
    """Сбрасывает Redis-кэш подписки."""
    await kv_del(user_id, _CACHE_KEY)


async def is_subscribed(user_id: int) -> bool:
    """
    Быстрая проверка — вызывается на каждом сообщении.
    Сначала смотрит Redis (TTL 5 мин), потом Postgres.
    Учитывает и подписку и активный триал.
    """
    # 1. Redis cache
    cached = await kv_get(user_id, _CACHE_KEY)
    if cached is not None:
        return cached == "1"

    # 2. Postgres
    result = await _check_access_db(user_id)
    # Кэшируем с TTL через set с expire
    try:
        from db import get_redis
        r = await get_redis()
        cache_key = f"bot:kv:{user_id}:{_CACHE_KEY}"
        await r.set(cache_key, "1" if result else "0", ex=SUB_CACHE_TTL)
    except Exception as e:
        logger.warning(f"Redis cache set failed: {e}")
    return result


async def _check_access_db(user_id: int) -> bool:
    """Проверяет доступ в Postgres (подписка или триал)."""
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        # Активная подписка
        row = await conn.fetchrow(
            "SELECT expires_at FROM subscriptions "
            "WHERE user_id=$1 AND status='active' AND expires_at > $2",
            user_id, now
        )
        if row:
            return True
        # Активный триал
        trial = await conn.fetchrow(
            "SELECT expires_at FROM trials WHERE user_id=$1 AND expires_at > $2",
            user_id, now
        )
        return trial is not None


async def get_user_access_state(user_id: int) -> dict:
    """
    Батч-запрос: получаем subscription + trial + has_ever_trialed
    за один pool.acquire и четыре последовательных fetchrow.

    ВАЖНО: asyncpg не поддерживает параллельные запросы на одном коннекшне.
    asyncio.gather(conn.fetchrow(...), conn.fetchrow(...)) вызывает
    "another operation is in progress". Используем последовательные вызовы
    в рамках одного коннекшна — это всё равно экономит 3 pool.acquire.
    """
    pool = _get_pool()
    now  = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        sub_row    = await conn.fetchrow(
            "SELECT tier, expires_at FROM subscriptions "
            "WHERE user_id=$1 AND status='active' AND expires_at > $2",
            user_id, now,
        )
        trial_row  = await conn.fetchrow(
            "SELECT expires_at FROM trials "
            "WHERE user_id=$1 AND expires_at > $2",
            user_id, now,
        )
        ever_trial = await conn.fetchrow(
            "SELECT 1 FROM trials WHERE user_id=$1", user_id
        )
        ever_sub   = await conn.fetchrow(
            "SELECT 1 FROM subscriptions WHERE user_id=$1", user_id
        )
    return {
        "has_active_sub":   sub_row is not None,
        "sub_expires_at":   sub_row["expires_at"].isoformat() if sub_row else None,
        "has_active_trial": trial_row is not None,
        "trial_expires_at": trial_row["expires_at"].isoformat() if trial_row else None,
        "ever_had_access":  (ever_trial is not None) or (ever_sub is not None),
    }


async def get_subscription(user_id: int) -> dict | None:
    """Возвращает данные активной подписки или None."""
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tier, status, contract_id, expires_at, granted_at "
            "FROM subscriptions WHERE user_id=$1 AND status='active' AND expires_at > $2",
            user_id, now
        )
    if not row:
        return None
    return {
        "tier": row["tier"],
        "status": row["status"],
        "contract_id": row["contract_id"],
        "expires_at": row["expires_at"].isoformat(),
        "granted_at": row["granted_at"].isoformat(),
    }


async def get_trial(user_id: int) -> dict | None:
    """Возвращает данные триала или None."""
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT started_at, expires_at FROM trials "
            "WHERE user_id=$1 AND expires_at > $2",
            user_id, now
        )
    if not row:
        return None
    return {
        "started_at": row["started_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat(),
    }


async def has_used_trial(user_id: int) -> bool:
    """Использовал ли пользователь триал (когда-либо)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM trials WHERE user_id=$1", user_id
        )
    return row is not None


async def grant_trial(user_id: int) -> dict:
    """Выдаёт триал. Raises ValueError если уже был."""
    if await has_used_trial(user_id):
        raise ValueError("Trial already used")
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=TRIAL_DAYS)
    async with pool.acquire() as conn:
        result = await conn.execute(
            "INSERT INTO trials (user_id, started_at, expires_at) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id) DO NOTHING",
            user_id, now, expires
        )
    # BUG FIX: ON CONFLICT DO NOTHING silently discards a concurrent second insert.
    # Without this check, two concurrent requests could both pass has_used_trial()
    # (the per-user lock prevents same-session double-tap, but two separate device
    # sessions could race). Both would return "success" even though only one trial
    # was actually inserted. Now the second caller gets the correct ValueError.
    if result.split()[-1] == "0":  # "INSERT 0 0" → conflict, nothing inserted
        raise ValueError("Trial already used")
    await _invalidate_cache(user_id)
    from user_state import invalidate_state_cache
    await invalidate_state_cache(user_id)
    logger.info(f"Trial granted: user={user_id} expires={expires.isoformat()}")
    expires_iso = expires.isoformat()

    # Планируем конверсионные нуджи — Day2 и LastDay
    # app передаётся через context если вызывается из бота
    try:
        import asyncio
        from flows.conversion import on_trial_activated
        # Запускаем асинхронно чтобы не блокировать ответ пользователю
        asyncio.create_task(
            _schedule_conversion_nudges(user_id, expires_iso)
        )
    except Exception as _e:
        logger.warning(f"Could not schedule conversion nudges: {_e}")

    return {"started_at": now.isoformat(), "expires_at": expires_iso}


async def _schedule_conversion_nudges(user_id: int, expires_iso: str) -> None:
    """Планирует конверсионные нуджи. Вызывается как asyncio task."""
    try:
        from flows.utm import track_event
        await track_event(user_id, "trial_activated")
        # Day2 и LastDay планируются через PTB app — доступен из контекста
        # Если app недоступен — нуджи пройдут через retention.py
        logger.info(f"Conversion events tracked for trial user={user_id}")
    except Exception as e:
        logger.warning(f"_schedule_conversion_nudges: {e}")


async def grant_subscription(user_id: int, tier: str, days: int,
                              contract_id: str = "") -> None:
    """Выдаёт или продлевает подписку."""
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=days)
    async with pool.acquire() as conn:
        # BUG FIX: wrap in a transaction with FOR UPDATE to prevent the race condition
        # where two concurrent webhooks both read the same expires_at and both set
        # expires = old + days (instead of old + 2*days for the second one).
        # BUG FIX 2: filter by status='active' — previously a cancelled subscription's
        # expires_at could be read and extended, leaving the user without access.
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT expires_at FROM subscriptions "
                "WHERE user_id=$1 AND status='active' AND expires_at > $2 FOR UPDATE",
                user_id, now
            )
            if existing:
                # Продлеваем от текущего expires_at
                expires = existing["expires_at"] + timedelta(days=days)

            await conn.execute("""
                INSERT INTO subscriptions (user_id, tier, status, contract_id, expires_at, updated_at)
                VALUES ($1, $2, 'active', $3, $4, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    tier=EXCLUDED.tier,
                    status='active',
                    contract_id=EXCLUDED.contract_id,
                    expires_at=EXCLUDED.expires_at,
                    updated_at=NOW()
            """, user_id, tier, contract_id, expires)
    await _invalidate_cache(user_id)
    logger.info(f"Subscription granted: user={user_id} tier={tier} days={days} expires={expires.isoformat()}")


async def extend_subscription(user_id: int, days: int, contract_id: str = "") -> None:
    """Продлевает существующую подписку на N дней."""
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tier, expires_at FROM subscriptions WHERE user_id=$1", user_id
        )
        if row and row["expires_at"] > now:
            new_expires = row["expires_at"] + timedelta(days=days)
            tier = row["tier"]
        else:
            new_expires = now + timedelta(days=days)
            tier = "1m"
        await conn.execute("""
            INSERT INTO subscriptions (user_id, tier, status, contract_id, expires_at, updated_at)
            VALUES ($1, $2, 'active', $3, $4, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                status='active',
                contract_id=EXCLUDED.contract_id,
                expires_at=EXCLUDED.expires_at,
                updated_at=NOW()
        """, user_id, tier, contract_id, new_expires)
    await _invalidate_cache(user_id)
    logger.info(f"Subscription extended: user={user_id} +{days} days expires={new_expires.isoformat()}")


async def revoke_subscription(user_id: int) -> None:
    """Отзывает подписку (админ)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE subscriptions SET status='cancelled', updated_at=NOW() WHERE user_id=$1",
            user_id
        )
    await _invalidate_cache(user_id)
    logger.info(f"Subscription revoked: user={user_id}")


# ══════════════════════════════════════════════════════════════════════════════
# История платежей
# ══════════════════════════════════════════════════════════════════════════════

async def save_payment(user_id: int, lava_payment_id: str, contract_id: str,
                       amount: float, currency: str, tier: str,
                       event_type: str) -> bool:
    """
    Сохраняет платёж. Возвращает True если новый, False если дубль.
    Идемпотентность: ON CONFLICT DO NOTHING по lava_payment_id.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            INSERT INTO payments
                (user_id, lava_payment_id, contract_id, amount, currency, tier, event_type)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (lava_payment_id) DO NOTHING
        """, user_id, lava_payment_id, contract_id, amount, currency, tier, event_type)
    inserted = result.split()[-1] != "0"
    if not inserted:
        logger.info(f"Duplicate payment ignored: {lava_payment_id}")
    return inserted


async def get_payment_history(user_id: int, limit: int = 10) -> list:
    """Возвращает историю платежей пользователя."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT lava_payment_id, amount, currency, tier, event_type, created_at
            FROM payments WHERE user_id=$1
            ORDER BY created_at DESC LIMIT $2
        """, user_id, limit)
    return [
        {
            "payment_id": r["lava_payment_id"],
            "amount": float(r["amount"]) if r["amount"] else 0,
            "currency": r["currency"],
            "tier": r["tier"],
            "event_type": r["event_type"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Реферальная программа
# ══════════════════════════════════════════════════════════════════════════════

async def get_or_create_ref_code(user_id: int) -> str:
    """Возвращает реф-код пользователя, создаёт если нет."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ref_code FROM ref_codes WHERE user_id=$1", user_id
        )
        if row:
            return row["ref_code"]
        # Создаём уникальный код
        code = secrets.token_hex(4).upper()  # 8 символов
        await conn.execute(
            "INSERT INTO ref_codes (user_id, ref_code) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO NOTHING",
            user_id, code
        )
        return code


async def get_referral_link(user_id: int) -> str:
    """Возвращает реферальную ссылку на бота."""
    code = await get_or_create_ref_code(user_id)
    bot_username = BOT_USERNAME or "ваш_бот"
    return f"https://t.me/{bot_username}?start=ref_{code}"


async def register_referral(referred_id: int, ref_code: str) -> bool:
    """
    Регистрирует реферала при старте бота со ссылкой.
    Возвращает True если успешно записан.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        # Находим реферера по коду
        referrer = await conn.fetchrow(
            "SELECT user_id FROM ref_codes WHERE ref_code=$1", ref_code
        )
        if not referrer:
            return False
        referrer_id = referrer["user_id"]
        if referrer_id == referred_id:
            return False  # нельзя рефериться к себе
        # Записываем
        try:
            await conn.execute("""
                INSERT INTO referrals (referrer_id, referred_id, ref_code)
                VALUES ($1, $2, $3)
                ON CONFLICT (referred_id) DO NOTHING
            """, referrer_id, referred_id, ref_code)
            logger.info(f"Referral registered: referrer={referrer_id} referred={referred_id}")
            return True
        except Exception as e:
            logger.warning(f"Referral register error: {e}")
            return False


async def process_referral_bonus(referred_id: int, bot: Bot) -> None:
    """
    Начисляет бонус рефереру когда referred_id оформляет подписку.
    Вызывать при успешном платеже.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT referrer_id FROM referrals
            WHERE referred_id=$1 AND bonus_given=FALSE
        """, referred_id)
        if not row:
            return
        referrer_id = row["referrer_id"]
        # Даём бонус рефереру
        await conn.execute(
            "UPDATE referrals SET bonus_given=TRUE WHERE referred_id=$1",
            referred_id
        )

    await extend_subscription(referrer_id, REFERRAL_BONUS)
    logger.info(f"Referral bonus: referrer={referrer_id} +{REFERRAL_BONUS} days")

    # Уведомляем реферера
    try:
        await bot.send_message(
            chat_id=referrer_id,
            text=(
                f"🎁 *Бонус за реферала!*\n\n"
                f"Твой приглашённый оформил подписку — тебе начислено "
                f"*{REFERRAL_BONUS} бонусных дней* к подписке! 🔥"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify referrer {referrer_id}: {e}")


async def get_referral_stats(user_id: int) -> dict:
    """Возвращает статистику рефералов пользователя."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=$1", user_id
        )
        converted = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=$1 AND bonus_given=TRUE",
            user_id
        )
    return {
        "total": total or 0,
        "converted": converted or 0,
        "bonus_days_earned": (converted or 0) * REFERRAL_BONUS,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Определение тира
# ══════════════════════════════════════════════════════════════════════════════

def _detect_tier(amount: float) -> str:
    for threshold, tier in _TIER_THRESHOLDS:
        if amount <= threshold:
            return tier
    return "12m"


# ══════════════════════════════════════════════════════════════════════════════
# Платёжная ссылка
# ══════════════════════════════════════════════════════════════════════════════

def get_payment_link(user_id: int) -> str:
    if not LAVA_LINK:
        return ""
    sep = "&" if "?" in LAVA_LINK else "?"
    return f"{LAVA_LINK}{sep}buyer_id={user_id}"


# ══════════════════════════════════════════════════════════════════════════════
# Вебхук-обработчик
# ══════════════════════════════════════════════════════════════════════════════

def _check_basic_auth(request: web.Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        login, password = decoded.split(":", 1)
        return login == LAVA_WEBHOOK_LOGIN and password == LAVA_WEBHOOK_PASS
    except Exception:
        return False


async def lava_webhook_handler(request: web.Request) -> web.Response:
    """
    Обрабатывает вебхуки от Lava.top.
    Полностью идемпотентен — дубли игнорируются.
    """
    # Авторизация обязательна — если пароль не задан, отклоняем все запросы
    if not LAVA_WEBHOOK_PASS:
        logger.error("Lava webhook: LAVA_WEBHOOK_PASS не задан — все запросы отклонены")
        return web.Response(status=503, text="Webhook not configured")
    if not _check_basic_auth(request):
        logger.warning("Lava webhook: unauthorized")
        return web.Response(status=401, text="Unauthorized")

    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Lava webhook: bad JSON: {e}")
        return web.Response(status=400, text="Bad Request")

    event_type  = body.get("type", "")
    status      = body.get("status", "")
    buyer_id    = body.get("buyer_id", "")
    logger.info(f"Lava webhook: type={event_type} status={status} buyer_id={buyer_id}")

    # BUG FIX: removed duplicate variable assignments that appeared right after the log line
    contract_id = body.get("contract_id", "") or body.get("parentContractId", "")
    amount      = float(body.get("amount", 0))
    currency    = body.get("currency", "EUR")
    # BUG FIX: use a deterministic fallback ID (hash of contract+type) instead of
    # timestamp-based one. Timestamp fallback could generate the same ID if the webhook
    # arrived twice within 1 second, defeating the ON CONFLICT deduplication.
    import hashlib as _hashlib
    _fallback_src = f"{contract_id}_{event_type}_{buyer_id}"
    payment_id  = (body.get("id") or body.get("paymentId") or
                   _hashlib.sha256(_fallback_src.encode()).hexdigest()[:24])

    if status != "success":
        logger.info(f"Lava webhook: skip status={status}")
        return web.Response(status=200, text="OK")

    if event_type not in {"INVOICE", "SUBSCRIPTION_FIRST_INVOICE", "SUBSCRIPTION_RENEWAL"}:
        logger.info(f"Lava webhook: skip type={event_type}")
        return web.Response(status=200, text="OK")

    # Парсим user_id
    user_id = None
    if buyer_id:
        try:
            user_id = int(buyer_id)
        except (ValueError, TypeError):
            pass

    if not user_id:
        logger.warning(f"Lava webhook: no user_id, buyer_id={buyer_id!r}")
        return web.Response(status=200, text="OK")

    tier = _detect_tier(amount)
    days = TIER_DAYS[tier]

    # Идемпотентное сохранение платежа
    is_new = await save_payment(user_id, str(payment_id), contract_id,
                                amount, currency, tier, event_type)
    if not is_new:
        # Дубль — уже обработан
        return web.Response(status=200, text="OK")

    # Выдаём/продлеваем подписку
    await grant_subscription(user_id, tier, days, contract_id)
    try:
        from flows.utm import track_event as _te
        await _te(user_id, "payment_completed")
    except Exception:
        pass

    bot: Bot = request.app.get("bot")

    # Бонус рефереру (только при первом платеже)
    if bot and event_type in {"INVOICE", "SUBSCRIPTION_FIRST_INVOICE"}:
        try:
            await process_referral_bonus(user_id, bot)
        except Exception as e:
            logger.warning(f"Referral bonus error: {e}")

    # Уведомляем пользователя
    if bot:
        await _notify_user(bot, user_id, tier, event_type)

    return web.Response(status=200, text="OK")


async def _notify_user(bot: Bot, user_id: int, tier: str, event_type: str) -> None:
    label = TIER_LABELS.get(tier, "?")
    sub = await get_subscription(user_id)
    expires_str = ""
    if sub:
        exp = datetime.fromisoformat(sub["expires_at"])
        expires_str = exp.strftime("%d.%m.%Y")

    if event_type == "SUBSCRIPTION_RENEWAL":
        text = (
            f"🔄 *Подписка продлена!*\n\n"
            f"Тариф: {label}\n"
            f"Действует до: {expires_str}\n\n"
            f"Всё на месте — голос настроен, материалы сохранены. Продолжаем 👇"
        )
    else:
        text = (
            f"✅ *Подписка активирована!*\n\n"
            f"Тариф: {label}\n"
            f"Действует до: {expires_str}\n\n"
            f"Теперь у тебя полный доступ.\n"
            f"Голос Миры уже знает твою нишу — можно сразу писать.\n\n"
            f"Нажми кнопку ниже или напиши тему поста 👇"
        )
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    from user_state import invalidate_state_cache
    _post_pay_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("☰ Открыть меню", callback_data="menu_main")
    ]])
    try:
        await invalidate_state_cache(user_id)
        # Пробуем отправить GIF — если файла нет, падаем на текст
        _gif_sent = False
        try:
            import os as _os
            from ui.media import _ASSETS_DIR, _GIFS
            _gif_path = _os.path.join(_ASSETS_DIR, _GIFS.get("subscription_paid", ""))
            if _os.path.exists(_gif_path):
                with open(_gif_path, "rb") as _f:
                    await bot.send_animation(
                        chat_id=user_id,
                        animation=_f,
                        caption=text,
                        parse_mode="Markdown",
                        reply_markup=_post_pay_kb,
                    )
                _gif_sent = True
        except Exception:
            pass

        if not _gif_sent:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=_post_pay_kb,
            )
    except Exception as e:
        logger.error(f"Notify user {user_id} failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Регистрация aiohttp роута
# ══════════════════════════════════════════════════════════════════════════════

def setup_lava_webhook(app: web.Application, bot: Bot) -> None:
    app["bot"] = bot
    app.router.add_post("/lava/webhook", lava_webhook_handler)
    logger.info("Lava webhook registered at /lava/webhook")


# ══════════════════════════════════════════════════════════════════════════════
# UI — кабинет пользователя
# ══════════════════════════════════════════════════════════════════════════════

def subscription_menu_kb():
    from utils import kb
    return kb(
        ["💳 Оформить подписку|sub_pay"],
        ["← Меню|menu_main"],
    )


def cabinet_kb():
    from utils import kb
    return kb(
        ["📋 Моя подписка|cab_status"],
        ["🧾 История платежей|cab_history"],
        ["👥 Реферальная программа|cab_referral"],
        ["← Меню|menu_main"],
    )


async def render_status(user_id: int) -> str:
    """Текст статуса подписки для кабинета."""
    now = datetime.now(timezone.utc)
    sub = await get_subscription(user_id)
    if sub:
        exp = datetime.fromisoformat(sub["expires_at"])
        days_left = (exp - now).days
        label = TIER_LABELS.get(sub.get("tier", ""), "?")
        return (
            f"✅ *Подписка активна*\n\n"
            f"Тариф: {label}\n"
            f"Действует до: {exp.strftime('%d.%m.%Y')}\n"
            f"Осталось дней: {days_left}"
        )

    trial = await get_trial(user_id)
    if trial:
        exp = datetime.fromisoformat(trial["expires_at"])
        days_left = max(0, (exp - now).days)
        hours_left = max(0, int((exp - now).total_seconds() / 3600))
        return (
            f"🔓 *Пробный период*\n\n"
            f"Осталось: {days_left} дн. ({hours_left} ч.)\n"
            f"Действует до: {exp.strftime('%d.%m.%Y %H:%M')}\n\n"
            f"_Оформи подписку чтобы сохранить доступ_ 👇"
        )

    used_trial = await has_used_trial(user_id)
    if used_trial:
        return "❌ *Подписка не активна*\n\n_Пробный период уже использован._"
    return "❌ *Подписка не активна*\n\n_У тебя есть 7 дней бесплатного доступа — активируй!_"


async def render_history(user_id: int) -> str:
    """Текст истории платежей."""
    payments = await get_payment_history(user_id, limit=8)
    if not payments:
        return "🧾 *История платежей*\n\n_Платежей пока нет._"

    lines = ["🧾 *История платежей*\n"]
    for p in payments:
        dt = datetime.fromisoformat(p["created_at"]).strftime("%d.%m.%Y")
        tier_label = TIER_LABELS.get(p["tier"], p["tier"] or "?")
        etype = "продление" if p["event_type"] == "SUBSCRIPTION_RENEWAL" else "подписка"
        lines.append(f"• {dt} — {tier_label} — €{p['amount']:.0f} ({etype})")
    return "\n".join(lines)


async def render_referral(user_id: int) -> tuple[str, str]:
    """
    Возвращает (текст, реф-ссылка) для раздела рефералов.
    """
    stats = await get_referral_stats(user_id)
    link = await get_referral_link(user_id)
    text = (
        f"👥 *Реферальная программа*\n\n"
        f"Приглашай друзей — получай бонусные дни!\n\n"
        f"За каждого приглашённого, кто оформит подписку:\n"
        f"*+{REFERRAL_BONUS} дней* к твоей подписке 🎁\n\n"
        f"━━━━━━━━━━━━━\n"
        f"📊 *Твоя статистика:*\n"
        f"Приглашено: {stats['total']}\n"
        f"Оформили подписку: {stats['converted']}\n"
        f"Заработано дней: {stats['bonus_days_earned']}\n\n"
        f"━━━━━━━━━━━━━\n"
        f"🔗 *Твоя ссылка:*\n`{link}`\n\n"
        f"_Нажми чтобы скопировать_ 👆"
    )
    return text, link
