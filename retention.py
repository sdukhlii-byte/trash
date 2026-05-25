"""
retention.py — система удержания пользователей.

Логика:
  Каждый пользователь без активного доступа получает серию пушей.
  Как только оплатил — серия останавливается навсегда.

Состояния и цепочки:
  ONBOARDED  → не использовал триал → пушим триал (4 касания)
  TRIAL      → активный триал → предупреждение об истечении
  EXPIRED    → был доступ, истёк → пушим возврат (4 касания)

Антиспам:
  • Один пуш в сутки максимум на пользователя (кроме шага 0 в цепочке ONBOARDED)
  • После N касаний без реакции — прекращаем
  • Разные тексты на каждом шаге — не копипаст

Запуск:
  Два задания в APScheduler:
  • run_daily_pushes()     — каждый день в 10:00 UTC
  • run_trial_warnings()   — каждый час (за 24ч до истечения триала)
"""

import logging
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

LAVA_LINK = os.environ.get(
    "LAVA_LINK",
    "https://app.lava.top/products/8c78afe1-941d-4db7-b53b-eb097dba9215/c4d4749d-54f4-4230-8975-b6d9d649dea6"
)
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")


def _pay_url(user_id: int) -> str:
    """Прямая ссылка на Lava с buyer_id — юзер платит без возврата в бот."""
    sep = "&" if "?" in LAVA_LINK else "?"
    return f"{LAVA_LINK}{sep}buyer_id={user_id}"


def _bot_url() -> str:
    """Диплинк на открытие бота."""
    return f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else ""


def pay_kb(user_id: int, trial: bool = False) -> list:
    """
    Универсальный набор кнопок для пуша.
    Всегда включает прямую ссылку на оплату.
    trial=True — добавляет кнопку бесплатного триала через бота.
    """
    rows = []
    if trial:
        rows.append([InlineKeyboardButton(
            "🎁 Активировать 7 дней бесплатно",
            callback_data="sub_trial"
        )])
    rows.append([InlineKeyboardButton(
        "💳 Оформить подписку →",
        url=_pay_url(user_id)
    )])
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# Цепочки сообщений (тексты без кнопок — кнопки генерируются динамически)
# ══════════════════════════════════════════════════════════════════════════════

ONBOARDED_CHAIN = [
    {
        # Шаг 0 — +2ч после онбординга
        # Момент: человек настроил бот но не запустил. Не знает с чего начать.
        # Ключевое ощущение: ты не один — здесь есть тот кто уже всё знает.
        "step": 0,
        "delay_hours": 2,
        "text": (
            "Ты настроила профиль — отлично.\n\n"
            "Хочу показать одну вещь пока не забыла.\n\n"
            "Большинство людей приходят с ощущением что контент — это отдельная профессия "
            "которой надо учиться годами. Рилсы, алгоритмы, хуки, прогревы...\n\n"
            "На самом деле всё что нужно — это знать что работает именно в твоей нише "
            "прямо сейчас. Я это знаю.\n\n"
            "*7 дней бесплатно* — просто напиши тему и посмотри что будет 👇"
        ),
        "trial": True,
    },
    {
        # Шаг 1 — следующий день
        # Момент: прошёл день, человек не решился. Вероятно думает "потом разберусь".
        # Ключевое ощущение: пока ты разбираешься — другие уже делают.
        "step": 1,
        "delay_hours": 26,
        "text": (
            "Знаешь что самое обидное в контенте?\n\n"
            "Не алгоритмы и не конкуренция.\n\n"
            "А то что сидишь перед экраном, знаешь о чём хочешь написать — "
            "и всё равно не знаешь как начать. Что за хук, какой формат, "
            "почему этот пост а не тот...\n\n"
            "Именно для этого я здесь. Не чтобы писать вместо тебя — "
            "чтобы ты никогда больше не зависала над пустым экраном.\n\n"
            "*Попробуй прямо сейчас — 7 дней бесплатно.*"
        ),
        "trial": True,
    },
    {
        # Шаг 2 — через 3 дня
        # Момент: не конвертировался после двух касаний. Возможно вопрос ценности.
        # Ключевое ощущение: это не ещё один инструмент — это другой уровень спокойствия.
        "step": 2,
        "delay_hours": 74,
        "text": (
            "Смотри, я скажу честно.\n\n"
            "Большинство инструментов для контента дают тебе шаблоны. "
            "Ты всё равно думаешь что писать, всё равно не знаешь что сейчас работает, "
            "всё равно чувствуешь что разбираешься в этом хуже чем хочется.\n\n"
            "Я работаю иначе. Я знаю твою нишу, твою аудиторию — "
            "и говорю конкретно: вот что сейчас работает, вот как это сделать.\n\n"
            "€31/мес — меньше одной консультации по контенту. "
            "И я здесь каждый день, не раз в месяц.\n\n"
            "*7 дней бесплатно — без карты.*"
        ),
        "trial": True,
    },
    {
        # Шаг 3 — через 7 дней, последний
        # Момент: финальная попытка. Апеллируем к главной боли — одиночеству с задачей.
        "step": 3,
        "delay_hours": 170,
        "text": (
            "Последний раз пишу — и это не давление, просто хочу сказать кое-что важное.\n\n"
            "Контент — это не та задача где «разберусь сама когда будет время». "
            "Время есть сейчас. А опыт и понимание что работает — вот за чем сюда приходят.\n\n"
            "Если решишь попробовать — я здесь.\n"
            "Если нет — удачи, серьёзно 🤝"
        ),
        "trial": True,
    },
]

EXPIRED_CHAIN = [
    {
        # Шаг 0 — +1ч после истечения
        # Момент: только что потеряла доступ. Горячий момент — человек ещё в контексте.
        # Ключевое ощущение: ты не потеряла то что нашла.
        "step": 0,
        "delay_hours": 1,
        "text": (
            "Пробный период закончился.\n\n"
            "Всё что ты создавала — сохранено и ждёт тебя.\n\n"
            "Я не исчезла — просто пора перейти к нормальной работе вместе. "
            "Один клик и продолжаем."
        ),
        "trial": False,
    },
    {
        # Шаг 1 — через 2 дня
        # Момент: 2 дня без инструмента. Вернулась задача которую решала с ботом.
        # Ключевое ощущение: ты снова один на один — а я рядом если нужно.
        "step": 1,
        "delay_hours": 48,
        "text": (
            "Как контент?\n\n"
            "Не спрашиваю чтобы надавить — просто знаю как это бывает. "
            "Садишься писать, и снова то самое ощущение: непонятно с чего начать, "
            "что сейчас работает, правильно ли это...\n\n"
            "Именно это я и решаю. Возвращайся когда будет нужно — "
            "буду здесь."
        ),
        "trial": False,
    },
    {
        # Шаг 2 — через неделю
        # Момент: прошла неделя. Либо нашла альтернативу, либо просто не дошли руки.
        # Ключевое ощущение: это не про деньги — это про то сколько стоит твоё время.
        "step": 2,
        "delay_hours": 168,
        "text": (
            "Прошла неделя.\n\n"
            "Если честно — €31/мес это не про инструмент. "
            "Это про то сколько стоит час твоего времени "
            "который ты тратишь на то чтобы разобраться как написать один пост.\n\n"
            "Если твой час стоит больше €1 — математика простая.\n\n"
            "Возвращайся 👇"
        ),
        "trial": False,
    },
    {
        # Шаг 3 — через 2 недели, последний
        # Момент: финал. Без давления — с уважением.
        "step": 3,
        "delay_hours": 336,
        "text": (
            "Последнее сообщение — обещаю.\n\n"
            "Если нашла что-то что работает лучше — отлично, я рада.\n\n"
            "Если просто не дошли руки — кнопка внизу, "
            "и продолжим с того места где остановились 💙"
        ),
        "trial": False,
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# DB: таблица push_log
# ══════════════════════════════════════════════════════════════════════════════

async def init_push_db() -> None:
    """Создаёт таблицу push_log. Вызывать из init_db()."""
    from db import _get_pool
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS push_log (
                id          BIGSERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                push_type   TEXT NOT NULL,
                step        INT  NOT NULL DEFAULT 0,
                sent_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_push_user
                ON push_log (user_id, push_type)
        """)
    logger.info("Push log table ready")


async def get_push_step(user_id: int, push_type: str) -> int:
    """Возвращает следующий шаг цепочки (0 если ещё не получал)."""
    from db import _get_pool
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT MAX(step) as last_step FROM push_log
            WHERE user_id=$1 AND push_type=$2
        """, user_id, push_type)
    if row and row["last_step"] is not None:
        return row["last_step"] + 1
    return 0


async def log_push(user_id: int, push_type: str, step: int) -> None:
    """Записывает отправленный пуш."""
    from db import _get_pool
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO push_log (user_id, push_type, step)
            VALUES ($1, $2, $3)
        """, user_id, push_type, step)


async def was_pushed_today(user_id: int) -> bool:
    """Проверяет: получал ли юзер пуш за последние 20 часов (антиспам)."""
    from db import _get_pool
    pool = _get_pool()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=20)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 1 FROM push_log
            WHERE user_id=$1 AND sent_at > $2
            LIMIT 1
        """, user_id, cutoff)
    return row is not None


async def was_trial_warned(user_id: int) -> bool:
    """Проверяет: получал ли юзер предупреждение об истечении триала."""
    from db import _get_pool
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 1 FROM push_log
            WHERE user_id=$1 AND push_type='trial_warning'
            LIMIT 1
        """, user_id)
    return row is not None


# ══════════════════════════════════════════════════════════════════════════════
# Получение списков пользователей
# ══════════════════════════════════════════════════════════════════════════════

async def get_onboarded_users_without_access() -> list[int]:
    """
    Пользователи с профилем, без подписки и без активного триала.

    Сначала пробуем Redis (быстро), при ошибке — фоллбэк на PostgreSQL
    (таблица profiles / subscriptions / trials).
    """
    from db import get_redis, _get_pool

    # ── Попытка через Redis ──────────────────────────────────────────────────
    try:
        r = await get_redis()
        user_ids = []
        async for key in r.scan_iter("bot:kv:*:__profile__"):
            try:
                parts = key.split(":")
                uid = int(parts[2])
                raw = await r.get(key)
                if not raw:
                    continue
                profile = json.loads(raw)
                if not profile.get("onboarded"):
                    continue
                # BUG FIX: exclude EXPIRED users — those who EVER had a trial or subscription.
                # Previously only active trial/subscription was checked, so expired users
                # appeared in both ONBOARDED and EXPIRED chains, receiving double pushes.
                from lava_payments import get_subscription, has_used_trial
                sub = await get_subscription(uid)
                if sub:
                    continue
                ever_had_trial = await has_used_trial(uid)
                if ever_had_trial:
                    continue
                # Also exclude users who ever had a subscription (even expired/cancelled)
                from db import _get_pool as _pg_pool
                _pg = _pg_pool()
                async with _pg.acquire() as conn:
                    had_sub = await conn.fetchrow(
                        "SELECT 1 FROM subscriptions WHERE user_id=$1", uid
                    )
                if had_sub:
                    continue
                user_ids.append(uid)
            except Exception as e:
                logger.warning(f"get_onboarded_users redis key={key}: {e}")
        if user_ids:
            logger.info(f"Retention: found {len(user_ids)} onboarded users via Redis")
            return user_ids
        # Redis вернул 0 — возможно ключей нет совсем, пробуем PG
    except Exception as e:
        logger.warning(f"Redis unavailable in get_onboarded_users, falling back to PG: {e}")

    # ── Фоллбэк: PostgreSQL ──────────────────────────────────────────────────
    try:
        pool = _get_pool()
        async with pool.acquire() as conn:
            # BUG FIX: exclude users who EVER had a trial or subscription (not just active ones).
            # The original query only excluded active trials/subscriptions, causing EXPIRED users
            # to appear in both ONBOARDED and EXPIRED push chains simultaneously.
            rows = await conn.fetch("""
                SELECT DISTINCT p.user_id
                FROM profiles p
                WHERE p.onboarded = TRUE
                  AND NOT EXISTS (
                      SELECT 1 FROM subscriptions s
                      WHERE s.user_id = p.user_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM trials t
                      WHERE t.user_id = p.user_id
                  )
            """)
        result = [r["user_id"] for r in rows]
        logger.info(f"Retention: found {len(result)} onboarded users via PG fallback")
        return result
    except Exception as e:
        logger.error(f"get_onboarded_users PG fallback failed: {e}", exc_info=True)
        return []


async def get_expired_users() -> list[int]:
    """Пользователи у которых была подписка/триал — и истекла."""
    from db import _get_pool
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        # Истекшие подписки
        sub_rows = await conn.fetch("""
            SELECT DISTINCT user_id FROM subscriptions
            WHERE expires_at < $1
        """, now)
        # Истекшие триалы
        trial_rows = await conn.fetch("""
            SELECT DISTINCT user_id FROM trials
            WHERE expires_at < $1
        """, now)

    sub_uids = {r["user_id"] for r in sub_rows}
    trial_uids = {r["user_id"] for r in trial_rows}
    all_expired = sub_uids | trial_uids

    # Исключаем тех у кого сейчас активна подписка
    from lava_payments import get_subscription
    result = []
    for uid in all_expired:
        sub = await get_subscription(uid)
        if not sub:
            result.append(uid)
    return result


async def get_trial_expiring_soon() -> list[int]:
    """Пользователи у которых триал истекает через 20-28 часов."""
    from db import _get_pool
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=20)
    window_end   = now + timedelta(hours=28)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id FROM trials
            WHERE expires_at BETWEEN $1 AND $2
        """, window_start, window_end)
    return [r["user_id"] for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# Отправка пушей
# ══════════════════════════════════════════════════════════════════════════════

async def send_push(bot: Bot, user_id: int, text: str,
                    buttons: list | None = None) -> bool:
    """Отправляет пуш юзеру. Возвращает False если юзер заблокировал бота."""
    try:
        reply_markup = None
        if buttons:
            reply_markup = InlineKeyboardMarkup(buttons)
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        err = str(e).lower()
        if "blocked" in err or "deactivated" in err or "not found" in err:
            logger.info(f"Push skipped: user {user_id} blocked bot")
            return False
        logger.warning(f"Push failed for {user_id}: {e}")
        return False


async def push_onboarded_user(bot: Bot, user_id: int) -> None:
    """Отправляет следующий пуш в цепочке ONBOARDED."""
    step = await get_push_step(user_id, "onboarded")
    if step >= len(ONBOARDED_CHAIN):
        return

    msg = ONBOARDED_CHAIN[step]

    # Шаг 0: антиспам НЕ применяем — это первый пуш после онбординга,
    # он должен уйти даже если сегодня был другой пуш.
    # Шаги 1+: проверяем и антиспам, и задержку между шагами.
    if step > 0:
        if await was_pushed_today(user_id):
            return

        from db import _get_pool
        now = datetime.now(timezone.utc)
        pool = _get_pool()
        async with pool.acquire() as conn:
            last = await conn.fetchrow("""
                SELECT sent_at FROM push_log
                WHERE user_id=$1 AND push_type='onboarded'
                ORDER BY sent_at DESC LIMIT 1
            """, user_id)
            if last:
                elapsed = (now - last["sent_at"]).total_seconds() / 3600
                if elapsed < msg["delay_hours"] * 0.9:
                    return

    buttons = pay_kb(user_id, trial=msg.get("trial", False))
    sent = await send_push(bot, user_id, msg["text"], buttons)
    if sent:
        await log_push(user_id, "onboarded", step)
        logger.info(f"Onboarded push sent: user={user_id} step={step}")


async def push_expired_user(bot: Bot, user_id: int) -> None:
    """Отправляет следующий пуш в цепочке EXPIRED."""
    step = await get_push_step(user_id, "expired")
    if step >= len(EXPIRED_CHAIN):
        return

    msg = EXPIRED_CHAIN[step]

    # BUG FIX: Step 0 skips antispam (same pattern as push_onboarded_user).
    # Previously all steps checked was_pushed_today() first, which could block
    # the urgent step-0 push if any other push had gone out within 20 hours
    # (e.g. a trial-warning push sent right before expiry).
    if step > 0:
        if await was_pushed_today(user_id):
            return

        from db import _get_pool
        now = datetime.now(timezone.utc)
        pool = _get_pool()
        async with pool.acquire() as conn:
            last = await conn.fetchrow("""
                SELECT sent_at FROM push_log
                WHERE user_id=$1 AND push_type='expired'
                ORDER BY sent_at DESC LIMIT 1
            """, user_id)
            if last:
                elapsed = (now - last["sent_at"]).total_seconds() / 3600
                if elapsed < msg["delay_hours"] * 0.9:
                    return

    # Персонализация шага 0: называем реальные материалы пользователя
    text = msg["text"]
    if step == 0:
        try:
            from db import get_results
            results = await get_results(user_id, limit=2)
            if results:
                names = ", ".join(r["agent_name"] for r in results[:2])
                text = (
                    f"Пробный период закончился.\n\n"
                    f"Твои материалы — {names} — сохранены и ждут тебя.\n\n"
                    f"Я не исчезла — просто пора перейти к нормальной работе вместе. "
                    f"Один клик и продолжаем."
                )
        except Exception:
            pass  # fallback на стандартный текст

    buttons = pay_kb(user_id, trial=False)
    sent = await send_push(bot, user_id, text, buttons)
    if sent:
        await log_push(user_id, "expired", step)
        logger.info(f"Expired push sent: user={user_id} step={step}")


async def push_trial_warning(bot: Bot, user_id: int) -> None:
    """Предупреждение об истечении триала (один раз)."""
    if await was_trial_warned(user_id):
        return

    from lava_payments import get_trial
    trial = await get_trial(user_id)
    if not trial:
        return

    exp = datetime.fromisoformat(trial["expires_at"])
    hours_left = int((exp - datetime.now(timezone.utc)).total_seconds() / 3600)

    text = (
        f"⚠️ *Осталось ~{hours_left} часов бесплатного доступа.*\n\n"
        "Надеюсь, ты уже попробовала рилсы, карусели или контент-план.\n\n"
        "Чтобы не потерять доступ и все свои материалы — "
        "оформи подписку и продолжим работу вместе 💙"
    )
    buttons = pay_kb(user_id, trial=False)

    sent = await send_push(bot, user_id, text, buttons)
    if sent:
        await log_push(user_id, "trial_warning", 0)
        logger.info(f"Trial warning sent: user={user_id} hours_left={hours_left}")


# ══════════════════════════════════════════════════════════════════════════════
# Главные задания (вызываются из APScheduler)
# ══════════════════════════════════════════════════════════════════════════════

async def run_daily_pushes(bot: Bot) -> None:
    """
    Ежедневный запуск в 10:00 UTC.
    Шлёт пуши всем кто не подписан.
    """
    logger.info("Retention: starting daily push run")
    sent_count = 0
    error_count = 0

    try:
        # 1. ONBOARDED — не использовали триал
        onboarded = await get_onboarded_users_without_access()
        logger.info(f"Retention: {len(onboarded)} onboarded users without access")
        for uid in onboarded:
            try:
                await push_onboarded_user(bot, uid)
                sent_count += 1
            except Exception as e:
                error_count += 1
                logger.error(f"Retention push error uid={uid}: {e}")

        # 2. EXPIRED — был доступ, истёк
        expired = await get_expired_users()
        logger.info(f"Retention: {len(expired)} expired users")
        for uid in expired:
            try:
                await push_expired_user(bot, uid)
                sent_count += 1
            except Exception as e:
                error_count += 1
                logger.error(f"Retention push error uid={uid}: {e}")

    except Exception as e:
        logger.error(f"Retention daily run failed: {e}", exc_info=True)

    logger.info(f"Retention: daily run done. sent={sent_count} errors={error_count}")


async def run_trial_warnings(bot: Bot) -> None:
    """
    Почасовой запуск.
    Находит триалы истекающие через ~24ч и шлёт предупреждение.
    """
    try:
        expiring = await get_trial_expiring_soon()
        if expiring:
            logger.info(f"Retention: {len(expiring)} trials expiring soon")
        for uid in expiring:
            try:
                await push_trial_warning(bot, uid)
            except Exception as e:
                logger.error(f"Trial warning error uid={uid}: {e}")
    except Exception as e:
        logger.error(f"Retention trial warning run failed: {e}", exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
# APScheduler jobs (PTB job_queue)
# ══════════════════════════════════════════════════════════════════════════════

def setup_retention_jobs(app) -> None:
    """
    Регистрирует задания в PTB ApplicationBuilder job_queue.
    Вызывать из _do_init в main.py — ПОСЛЕ app.start() и job_queue.start(),
    чтобы scheduler уже был жив.
    """
    import datetime as dt

    jq = app.job_queue
    if jq is None:
        logger.error(
            "RETENTION: job_queue is None — пуши не будут работать! "
            "Убедись что python-telegram-bot установлен с extras [job-queue]: "
            "pip install 'python-telegram-bot[job-queue]'"
        )
        return

    if not jq.scheduler.running:
        logger.error(
            "RETENTION: job_queue.scheduler не запущен — задания встанут в очередь "
            "но не выполнятся. В webhook-режиме вызывай setup_retention_jobs "
            "только после await app.job_queue.start()."
        )
        # Не возвращаемся — регистрируем всё равно, вдруг scheduler стартует позже

    async def daily_job(context):
        await run_daily_pushes(context.bot)

    async def hourly_job(context):
        await run_trial_warnings(context.bot)

    # Ежедневно в 10:00 UTC
    app.job_queue.run_daily(
        callback=daily_job,
        time=dt.time(hour=10, minute=0, tzinfo=dt.timezone.utc),
        name="retention_daily",
    )

    # Каждый час; первый запуск через 60 сек после старта
    app.job_queue.run_repeating(
        callback=hourly_job,
        interval=3600,
        first=60,
        name="retention_hourly",
    )

    logger.info(
        f"Retention jobs scheduled: daily@10UTC + hourly trial warnings "
        f"(scheduler_running={jq.scheduler.running})"
    )
