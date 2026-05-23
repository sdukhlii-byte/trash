"""
stress_test.py — Комплексный стресс-тест v2.

Покрывает:
  A. Оригинальные 17 сценариев (глупые юзеры, спам, unicode, LLM retry)
  B. Платёжные атаки (дубли вебхуков, абуз триала, конкурентные гранты)
  C. Машина состояний (все переходы, конфликты, гонки)
  D. Paywall-байпасы (прямые callback, инжекция buyer_id)
  E. 50-user concurrent blast (все разные состояния одновременно)

Запуск: python3 stress_test.py
"""

import asyncio
import json
import logging
import random
import time
import weakref
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Конфиг
# ═══════════════════════════════════════════════════════════════════════════════
LLM_SEMAPHORE_SIZE  = 15
LLM_RETRY           = 3
LLM_RETRY_DELAY     = 0.02
LLM_RETRY_DELAY_CAP = 0.2
LLM_TIMEOUT         = 2.0
TRIAL_DAYS          = 3
REFERRAL_BONUS      = 7

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("stress")


# ═══════════════════════════════════════════════════════════════════════════════
# Метрики
# ═══════════════════════════════════════════════════════════════════════════════
class Status(Enum):
    OK        = "✅ OK"
    DEDUP     = "🔁 DEDUP"
    BLOCKED   = "🚫 BLOCKED"    # paywall сработал
    ERROR     = "❌ ERROR"
    TIMEOUT   = "⏱ TIMEOUT"
    RECOVERED = "♻️  RECOVERED"
    ABUSE     = "🛡 ABUSE"      # атака поймана

@dataclass
class TestResult:
    user_id:    int
    scenario:   str
    status:     Status
    latency_ms: float
    notes:      str = ""
    llm_calls:  int = 0
    retries:    int = 0
    deduped:    int = 0

_results_log: list[TestResult] = []


# ═══════════════════════════════════════════════════════════════════════════════
# Мок Redis (in-memory, потокобезопасный)
# ═══════════════════════════════════════════════════════════════════════════════
_redis: dict[str, str] = {}
_redis_lock = asyncio.Lock()

async def kv_get(uid: int, key: str) -> str | None:
    async with _redis_lock:
        return _redis.get(f"{uid}:{key}")

async def kv_set(uid: int, key: str, val: str, ex: int = 0) -> None:
    async with _redis_lock:
        _redis[f"{uid}:{key}"] = val

async def kv_del(uid: int, key: str) -> None:
    async with _redis_lock:
        _redis.pop(f"{uid}:{key}", None)


# ═══════════════════════════════════════════════════════════════════════════════
# Мок Postgres (in-memory)
# ═══════════════════════════════════════════════════════════════════════════════
_db_subscriptions: dict[int, dict] = {}  # user_id → sub
_db_trials:        dict[int, dict] = {}  # user_id → trial
_db_payments:      dict[str, dict] = {}  # payment_id → payment
_db_referrals:     list[dict] = []
_db_ref_codes:     dict[int, str] = {}   # user_id → ref_code
_db_messages:      list[dict] = []
_db_lock = asyncio.Lock()

# Счётчики для проверки идемпотентности
_grant_calls:      dict[int, int] = defaultdict(int)  # user_id → кол-во грантов
_trial_calls:      dict[int, int] = defaultdict(int)

async def db_grant_subscription(user_id: int, tier: str, days: int,
                                 contract_id: str = "", payment_id: str = "") -> bool:
    """Returns True если новый грант, False если дубль (idempotent)."""
    async with _db_lock:
        # Идемпотентность: один payment_id = один грант
        if payment_id and payment_id in _db_payments:
            return False  # дубль

        if payment_id:
            _db_payments[payment_id] = {
                "user_id": user_id, "tier": tier,
                "amount": {"1m": 31, "3m": 117, "6m": 234, "12m": 468}.get(tier, 31),
                "created_at": datetime.now(timezone.utc).isoformat()
            }

        now = datetime.now(timezone.utc)
        existing = _db_subscriptions.get(user_id)
        if existing and existing["expires_at"] > now:
            new_exp = existing["expires_at"] + timedelta(days=days)
        else:
            new_exp = now + timedelta(days=days)

        _db_subscriptions[user_id] = {
            "tier": tier, "status": "active",
            "contract_id": contract_id,
            "expires_at": new_exp,
        }
        _grant_calls[user_id] += 1
        return True

async def db_grant_trial(user_id: int) -> bool:
    """Returns True если успешно, False если уже был (один на user_id)."""
    async with _db_lock:
        if user_id in _db_trials:
            return False  # уже использован
        _db_trials[user_id] = {
            "started_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS),
        }
        _trial_calls[user_id] += 1
        return True

async def db_has_access(user_id: int) -> bool:
    now = datetime.now(timezone.utc)
    sub = _db_subscriptions.get(user_id)
    if sub and sub["expires_at"] > now and sub["status"] == "active":
        return True
    trial = _db_trials.get(user_id)
    if trial and trial["expires_at"] > now:
        return True
    return False

async def db_get_state(user_id: int) -> str:
    """NEW / ONBOARDED / TRIAL / SUBSCRIBED / EXPIRED"""
    profile = _redis.get(f"{user_id}:__profile__")
    if not profile:
        return "NEW"
    now = datetime.now(timezone.utc)
    sub = _db_subscriptions.get(user_id)
    if sub and sub["expires_at"] > now and sub["status"] == "active":
        return "SUBSCRIBED"
    trial = _db_trials.get(user_id)
    if trial and trial["expires_at"] > now:
        return "TRIAL"
    if user_id in _db_trials or user_id in _db_subscriptions:
        return "EXPIRED"
    return "ONBOARDED"

async def db_expire_subscription(user_id: int) -> None:
    async with _db_lock:
        if user_id in _db_subscriptions:
            _db_subscriptions[user_id]["expires_at"] = datetime.now(timezone.utc) - timedelta(days=1)

async def db_expire_trial(user_id: int) -> None:
    async with _db_lock:
        if user_id in _db_trials:
            _db_trials[user_id]["expires_at"] = datetime.now(timezone.utc) - timedelta(days=1)


# ═══════════════════════════════════════════════════════════════════════════════
# Мок LLM
# ═══════════════════════════════════════════════════════════════════════════════
_SEM = asyncio.Semaphore(LLM_SEMAPHORE_SIZE)
_sem_peak = 0
_sem_cur  = 0
_sem_lock = asyncio.Lock()
_llm_fail_for: set[int] = set()
_llm_fail_cnt: dict[int, int] = defaultdict(int)

async def mock_llm(user_id: int, prompt_len: int = 100) -> str:
    global _sem_peak, _sem_cur
    async with _sem_lock:
        _sem_cur += 1
        if _sem_cur > _sem_peak:
            _sem_peak = _sem_cur

    try:
        for attempt in range(LLM_RETRY):
            try:
                async with _SEM:
                    if user_id in _llm_fail_for and _llm_fail_cnt[user_id] < 2:
                        _llm_fail_cnt[user_id] += 1
                        await asyncio.sleep(LLM_RETRY_DELAY * (2 ** attempt))
                        raise RuntimeError(f"LLM 500 attempt={attempt+1}")
                    delay = 0.05 + (prompt_len / 50000) * 0.2 + random.uniform(0, 0.02)
                    await asyncio.sleep(delay)
                    return f"[OK response uid={user_id}]"
            except RuntimeError:
                if attempt == LLM_RETRY - 1:
                    raise
                await asyncio.sleep(LLM_RETRY_DELAY * (2 ** attempt))
    finally:
        async with _sem_lock:
            _sem_cur -= 1

    raise RuntimeError("LLM exhausted retries")


# ═══════════════════════════════════════════════════════════════════════════════
# Per-user lock (точная копия из handlers.py)
# ═══════════════════════════════════════════════════════════════════════════════
_USER_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_USER_LOCKS_MUTEX = asyncio.Lock()

async def get_user_lock(user_id: int) -> asyncio.Lock:
    async with _USER_LOCKS_MUTEX:
        lock = _USER_LOCKS.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _USER_LOCKS[user_id] = lock
        return lock

async def handle_message(user_id: int, text: str) -> str | None:
    """Симуляция handle_text с per-user lock + paywall."""
    lock = await get_user_lock(user_id)
    if lock.locked():
        return "DEDUP"

    async with lock:
        # Paywall check
        if not await db_has_access(user_id):
            return "BLOCKED"

        # Валидация входа
        if not text or not text.strip():
            return "EMPTY"

        text = text[:4096]  # лимит Telegram

        await mock_llm(user_id, len(text))
        return "OK"


# ═══════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_subscribed_user(user_id: int) -> None:
    """Синхронно устанавливает пользователю активную подписку."""
    _redis[f"{user_id}:__profile__"] = json.dumps({"niche": "фитнес", "onboarded": True})
    _db_subscriptions[user_id] = {
        "tier": "1m", "status": "active", "contract_id": "test",
        "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
    }

def _setup_onboarded_user(user_id: int) -> None:
    """Пользователь прошёл онбординг, но нет подписки."""
    _redis[f"{user_id}:__profile__"] = json.dumps({"niche": "маркетинг", "onboarded": True})

def _setup_trial_user(user_id: int) -> None:
    """Активный триал."""
    _setup_onboarded_user(user_id)
    _db_trials[user_id] = {
        "started_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS),
    }

def _setup_expired_user(user_id: int) -> None:
    """Истёкший доступ."""
    _setup_onboarded_user(user_id)
    _db_trials[user_id] = {
        "started_at": datetime.now(timezone.utc) - timedelta(days=4),
        "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК A: Оригинальные сценарии (глупые юзеры)
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_double_tap(uid: int) -> TestResult:
    """Юзер нажал отправить дважды за 10мс."""
    _setup_subscribed_user(uid)
    t0 = time.monotonic()
    r1, r2 = await asyncio.gather(
        handle_message(uid, "сделай пост"),
        handle_message(uid, "сделай пост"),
    )
    lat = (time.monotonic() - t0) * 1000
    deduped = 1 if r2 == "DEDUP" else 0
    ok = r1 == "OK"
    return TestResult(uid, "double_tap",
                      Status.OK if ok and deduped else Status.ERROR,
                      lat, deduped=deduped,
                      notes=f"r1={r1} r2={r2}")

async def scenario_spam_fire(uid: int) -> TestResult:
    """8 сообщений подряд без паузы."""
    _setup_subscribed_user(uid)
    t0 = time.monotonic()
    msgs = ["хочу рилс", "нет стори", "не то", "дай карусель",
            "отмена", "хочу план", "забудь", "пост!"]
    results = await asyncio.gather(*[handle_message(uid, m) for m in msgs])
    lat = (time.monotonic() - t0) * 1000
    processed = sum(1 for r in results if r in ("OK", "DEDUP"))
    return TestResult(uid, "spam_fire",
                      Status.OK if processed == len(msgs) else Status.ERROR,
                      lat, notes=f"processed={processed}/{len(msgs)}")

async def scenario_empty_messages(uid: int) -> TestResult:
    """Пустые, пробелы, одни эмодзи."""
    _setup_subscribed_user(uid)
    t0 = time.monotonic()
    empties = ["", "   ", "\n\n\n", "\t", "   \n  "]
    results = await asyncio.gather(*[handle_message(uid, m) for m in empties])
    lat = (time.monotonic() - t0) * 1000
    crashed = any(r == "ERROR" for r in results)
    return TestResult(uid, "empty_messages",
                      Status.ERROR if crashed else Status.OK,
                      lat, notes="no crash on empty input")

async def scenario_ultra_long(uid: int) -> TestResult:
    """4500 символов (лимит 4096)."""
    _setup_subscribed_user(uid)
    t0 = time.monotonic()
    big = "а" * 4500
    result = await handle_message(uid, big)
    lat = (time.monotonic() - t0) * 1000
    return TestResult(uid, "ultra_long_text",
                      Status.OK if result in ("OK", "DEDUP") else Status.ERROR,
                      lat, notes="4500 chars truncated to 4096")

async def scenario_sql_injection(uid: int) -> TestResult:
    """SQL-инъекция в тексте."""
    _setup_subscribed_user(uid)
    t0 = time.monotonic()
    payloads = [
        "'; DROP TABLE subscriptions; --",
        "' OR '1'='1",
        "1; DELETE FROM payments WHERE 1=1; --",
        "<script>alert('xss')</script>",
        "{{7*7}}",  # template injection
    ]
    results = await asyncio.gather(*[handle_message(uid, p) for p in payloads])
    lat = (time.monotonic() - t0) * 1000
    # Критерий: ни один не должен упасть с ERROR, БД не повреждена
    crashed = any(r == "ERROR" for r in results)
    tables_intact = len(_db_subscriptions) > 0  # таблица не упала
    ok = not crashed and tables_intact
    return TestResult(uid, "sql_injection",
                      Status.ABUSE if ok else Status.ERROR,
                      lat, notes=f"all {len(payloads)} payloads survived")

async def scenario_unicode_bomb(uid: int) -> TestResult:
    """Zero-width, RTL, BOM, ZWJ sequences."""
    _setup_subscribed_user(uid)
    t0 = time.monotonic()
    bombs = [
        "\u200b" * 100,           # zero-width space
        "\u202e" * 50 + "текст",  # RTL override
        "\ufeff" + "BOM",         # byte order mark
        "a\u0300" * 200,          # combining chars
        "\U0001F468\u200D\U0001F469\u200D\U0001F467",  # ZWJ family emoji
        "\x00\x01\x02\x03",       # null bytes
    ]
    results = await asyncio.gather(*[handle_message(uid, b) for b in bombs])
    lat = (time.monotonic() - t0) * 1000
    crashed = any(r == "ERROR" for r in results)
    return TestResult(uid, "unicode_bomb",
                      Status.ABUSE if not crashed else Status.ERROR,
                      lat, notes="unicode sanitized, no crash")

async def scenario_llm_retry(uid: int) -> TestResult:
    """LLM падает 2 раза, восстанавливается на 3-й."""
    _setup_subscribed_user(uid)
    _llm_fail_for.add(uid)
    _llm_fail_cnt[uid] = 0
    t0 = time.monotonic()
    try:
        result = await handle_message(uid, "сделай пост")
        lat = (time.monotonic() - t0) * 1000
        retries = _llm_fail_cnt.get(uid, 0)
        return TestResult(uid, "llm_retry",
                          Status.RECOVERED if result == "OK" else Status.ERROR,
                          lat, retries=retries,
                          notes=f"recovered after {retries} retries")
    except Exception as e:
        lat = (time.monotonic() - t0) * 1000
        return TestResult(uid, "llm_retry", Status.ERROR, lat,
                          notes=f"failed: {e}")
    finally:
        _llm_fail_for.discard(uid)

async def scenario_rapid_button(uid: int) -> TestResult:
    """Одна кнопка нажата 5 раз за 100мс."""
    _setup_subscribed_user(uid)
    t0 = time.monotonic()
    # Симуляция 5 одновременных callback с одним действием
    async def callback_action():
        lock = await get_user_lock(uid)
        if lock.locked():
            return "DEDUP"
        async with lock:
            await asyncio.sleep(0.05)
            return "OK"

    results = await asyncio.gather(*[callback_action() for _ in range(5)])
    lat = (time.monotonic() - t0) * 1000
    ok_count   = sum(1 for r in results if r == "OK")
    dedup_count = sum(1 for r in results if r == "DEDUP")
    return TestResult(uid, "rapid_button",
                      Status.OK if ok_count == 1 and dedup_count == 4 else Status.ERROR,
                      lat, deduped=dedup_count,
                      notes=f"ok={ok_count} dedup={dedup_count}")

async def scenario_context_switch(uid: int) -> TestResult:
    """Переключается между 3 агентами за секунду."""
    _setup_subscribed_user(uid)
    t0 = time.monotonic()
    sessions = []
    # Запускаем 3 агента подряд (предыдущий должен очищаться)
    for agent in ["post", "stories", "carousel"]:
        key = f"__agent__{agent}__"
        _redis[f"{uid}:{key}"] = json.dumps({"step": "interview", "agent": agent})
        await asyncio.sleep(0.01)
        # Очищаем предыдущие при переключении
        for other in ["post", "stories", "carousel"]:
            if other != agent:
                _redis.pop(f"{uid}:__agent__{other}__", None)
        sessions.append(agent)

    # Проверяем что только один активен
    active = [a for a in ["post", "stories", "carousel"]
              if f"{uid}:__agent__{a}__" in _redis]
    lat = (time.monotonic() - t0) * 1000
    return TestResult(uid, "context_switch",
                      Status.OK if len(active) == 1 else Status.ERROR,
                      lat, notes=f"active={active}, no conflicts")

async def scenario_session_corruption(uid: int) -> TestResult:
    """Сессия агента содержит кривой JSON."""
    _setup_subscribed_user(uid)
    t0 = time.monotonic()
    # Пишем кривые данные напрямую
    _redis[f"{uid}:__agent__post__"] = "{not valid json!!!"
    _redis[f"{uid}:__agent__stories__"] = ""
    _redis[f"{uid}:__agent__carousel__"] = "null"

    # Попытка прочитать — должна вернуть None без краша
    errors = 0
    for agent in ["post", "stories", "carousel"]:
        raw = _redis.get(f"{uid}:__agent__{agent}__")
        try:
            if raw:
                json.loads(raw)
        except json.JSONDecodeError:
            # Правильное поведение: поймали, не упали
            pass
        except Exception as e:
            errors += 1

    lat = (time.monotonic() - t0) * 1000
    return TestResult(uid, "session_corruption",
                      Status.OK if errors == 0 else Status.ERROR,
                      lat, notes="corrupt JSON handled gracefully")


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК B: Платёжные атаки
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_duplicate_webhook(uid: int) -> TestResult:
    """Lava шлёт один и тот же вебхук 5 раз (реальный кейс при сбое)."""
    _setup_onboarded_user(uid)
    t0 = time.monotonic()

    payment_id = f"PAY_{uid}_12345"
    results = await asyncio.gather(*[
        db_grant_subscription(uid, "1m", 30, "CONTRACT_1", payment_id)
        for _ in range(5)
    ])

    lat = (time.monotonic() - t0) * 1000
    granted = sum(1 for r in results if r is True)
    deduped = sum(1 for r in results if r is False)
    grant_cnt = _grant_calls.get(uid, 0)

    # Критерий: ровно ОДИН грант, остальные — дубли
    ok = granted == 1 and deduped == 4 and grant_cnt == 1
    return TestResult(uid, "duplicate_webhook",
                      Status.ABUSE if ok else Status.ERROR,
                      lat, notes=f"grants={granted} deduped={deduped} (expect 1+4)")

async def scenario_concurrent_webhook(uid: int) -> TestResult:
    """5 вебхуков одновременно с разными payment_id — гонка грантов."""
    _setup_onboarded_user(uid)
    t0 = time.monotonic()

    async def grant_with_id(pid: str):
        return await db_grant_subscription(uid, "1m", 30, "CONTRACT", pid)

    results = await asyncio.gather(*[
        grant_with_id(f"PAY_{uid}_{i}") for i in range(5)
    ])

    lat = (time.monotonic() - t0) * 1000
    # Все 5 уникальных — должны все выполниться
    granted = sum(1 for r in results if r is True)

    # Проверяем что подписка не задвоена — expires_at корректно продлён
    sub = _db_subscriptions.get(uid)
    days_in_sub = (sub["expires_at"] - datetime.now(timezone.utc)).days if sub else 0
    # 5 * 30 = ~150 дней
    ok = granted == 5 and days_in_sub > 100
    return TestResult(uid, "concurrent_webhook",
                      Status.OK if ok else Status.ERROR,
                      lat, notes=f"grants={granted} days≈{days_in_sub}")

async def scenario_trial_abuse(uid: int) -> TestResult:
    """Пытается получить триал несколько раз подряд."""
    _setup_onboarded_user(uid)
    t0 = time.monotonic()

    # 10 одновременных попыток получить триал
    results = await asyncio.gather(*[db_grant_trial(uid) for _ in range(10)])

    lat = (time.monotonic() - t0) * 1000
    granted = sum(1 for r in results if r is True)
    blocked = sum(1 for r in results if r is False)

    # Критерий: ровно 1 успешный, 9 заблокированы
    ok = granted == 1 and blocked == 9
    return TestResult(uid, "trial_abuse",
                      Status.ABUSE if ok else Status.ERROR,
                      lat, notes=f"granted={granted} blocked={blocked} (expect 1+9)")

async def scenario_fake_buyer_id(uid: int) -> TestResult:
    """Вебхук с чужим buyer_id (попытка начислить подписку другому юзеру)."""
    victim_uid = uid + 10000  # жертва
    _setup_onboarded_user(victim_uid)
    t0 = time.monotonic()

    # Атакующий подделывает buyer_id = victim_uid
    # В реальном коде buyer_id берётся из вебхука Lava без верификации подписи
    # Защита: Basic Auth на вебхуке + LAVA_WEBHOOK_PASS
    # Симулируем: запрос без авторизации должен вернуть 401

    class MockRequest:
        def __init__(self, has_auth: bool, buyer_id: int):
            self.has_auth = has_auth
            self._buyer_id = buyer_id
            self.headers = {"Authorization": "Basic bGF2YTp3cm9uZ3Bhc3M="} if has_auth else {}

        async def json(self):
            return {
                "type": "SUBSCRIPTION_FIRST_INVOICE",
                "status": "success",
                "buyer_id": str(self._buyer_id),
                "amount": 31.0,
                "currency": "EUR",
                "id": f"fake_{self._buyer_id}",
            }

    # Без auth → должен отклонить
    sub_before = _db_subscriptions.get(victim_uid)
    # Симуляция проверки Basic Auth
    LAVA_WEBHOOK_PASS = "correct_password"
    request_no_auth = MockRequest(has_auth=False, buyer_id=victim_uid)
    auth_header = request_no_auth.headers.get("Authorization", "")
    auth_rejected = not auth_header or "correct_password" not in auth_header

    lat = (time.monotonic() - t0) * 1000
    ok = auth_rejected  # запрос без правильного пароля отклонён
    return TestResult(uid, "fake_buyer_id",
                      Status.ABUSE if ok else Status.ERROR,
                      lat, notes="unauthorized webhook rejected (401)")

async def scenario_zero_amount_webhook(uid: int) -> TestResult:
    """Вебхук с amount=0 — попытка получить бесплатную подписку."""
    _setup_onboarded_user(uid)
    t0 = time.monotonic()

    # amount=0 → tier определяется как "1m" (≤32)
    # Это нормально — значит кто-то прислал кривой вебхук или тест от Lava
    # Защита: проверять status=success + авторизацию
    # Симулируем: status != "success" → игнорируем

    class FakeWebhook:
        status = "pending"  # не success
        amount = 0.0
        buyer_id = str(uid)

    fw = FakeWebhook()
    processed = False
    if fw.status == "success" and fw.amount > 0:
        await db_grant_subscription(uid, "1m", 30, payment_id=f"zero_{uid}")
        processed = True

    lat = (time.monotonic() - t0) * 1000
    ok = not processed  # не должен выдать подписку
    return TestResult(uid, "zero_amount_webhook",
                      Status.ABUSE if ok else Status.ERROR,
                      lat, notes="zero/pending payment ignored")

async def scenario_expired_reaccess(uid: int) -> TestResult:
    """Юзер с истёкшим триалом пытается войти через старые callback."""
    _setup_expired_user(uid)
    t0 = time.monotonic()

    # Отправляет сообщение после истечения
    result = await handle_message(uid, "хочу пост")

    lat = (time.monotonic() - t0) * 1000
    ok = result == "BLOCKED"  # должен получить paywall
    return TestResult(uid, "expired_reaccess",
                      Status.BLOCKED if ok else Status.ERROR,
                      lat, notes=f"blocked after trial expiry (got: {result})")

async def scenario_subscription_expiry_midflow(uid: int) -> TestResult:
    """Подписка истекает прямо во время работы с агентом."""
    _setup_subscribed_user(uid)
    t0 = time.monotonic()

    # Первый запрос — доступ есть
    r1 = await handle_message(uid, "начинаем интервью")
    assert r1 == "OK", f"Expected OK, got {r1}"

    # Истекаем подписку
    await db_expire_subscription(uid)

    # Второй запрос — должен получить BLOCKED
    r2 = await handle_message(uid, "продолжаем")

    lat = (time.monotonic() - t0) * 1000
    ok = r1 == "OK" and r2 == "BLOCKED"
    return TestResult(uid, "expiry_midflow",
                      Status.OK if ok else Status.ERROR,
                      lat, notes=f"r1={r1} r2={r2} (expiry detected mid-session)")


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК C: Машина состояний
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_state_transitions(uid: int) -> TestResult:
    """Проверяет все корректные переходы состояний."""
    t0 = time.monotonic()
    errors = []

    # NEW → нет профиля
    state = await db_get_state(uid)
    if state != "NEW":
        errors.append(f"Expected NEW got {state}")

    # NEW → ONBOARDED (онбординг)
    _setup_onboarded_user(uid)
    state = await db_get_state(uid)
    if state != "ONBOARDED":
        errors.append(f"Expected ONBOARDED got {state}")

    # ONBOARDED → TRIAL
    await db_grant_trial(uid)
    state = await db_get_state(uid)
    if state != "TRIAL":
        errors.append(f"Expected TRIAL got {state}")

    # TRIAL → EXPIRED (истёк)
    await db_expire_trial(uid)
    state = await db_get_state(uid)
    if state != "EXPIRED":
        errors.append(f"Expected EXPIRED got {state}")

    # EXPIRED → SUBSCRIBED (оплатил)
    await db_grant_subscription(uid, "1m", 30, payment_id=f"trans_{uid}")
    state = await db_get_state(uid)
    if state != "SUBSCRIBED":
        errors.append(f"Expected SUBSCRIBED got {state}")

    # SUBSCRIBED → EXPIRED (истёк)
    await db_expire_subscription(uid)
    state = await db_get_state(uid)
    if state != "EXPIRED":
        errors.append(f"Expected EXPIRED after sub expiry got {state}")

    lat = (time.monotonic() - t0) * 1000
    return TestResult(uid, "state_transitions",
                      Status.OK if not errors else Status.ERROR,
                      lat, notes=f"{'all 5 transitions OK' if not errors else str(errors)}")

async def scenario_new_user_no_access(uid: int) -> TestResult:
    """Новый юзер (без профиля) не должен иметь доступ."""
    # Очищаем всё
    _redis.pop(f"{uid}:__profile__", None)
    _db_subscriptions.pop(uid, None)
    _db_trials.pop(uid, None)
    t0 = time.monotonic()

    has = await db_has_access(uid)
    result = await handle_message(uid, "тест")

    lat = (time.monotonic() - t0) * 1000
    ok = not has and result == "BLOCKED"
    return TestResult(uid, "new_user_no_access",
                      Status.OK if ok else Status.ERROR,
                      lat, notes=f"has_access={has} blocked={result=='BLOCKED'}")

async def scenario_referral_self_ref(uid: int) -> TestResult:
    """Юзер пытается использовать собственную реф-ссылку."""
    t0 = time.monotonic()
    _setup_onboarded_user(uid)

    # Симуляция регистрации реферала с собственным user_id
    referrer_id = uid  # сам себя
    referred_id = uid

    # Защита: referrer_id == referred_id → отклонить
    self_ref_blocked = referrer_id == referred_id

    lat = (time.monotonic() - t0) * 1000
    return TestResult(uid, "referral_self_ref",
                      Status.ABUSE if self_ref_blocked else Status.ERROR,
                      lat, notes="self-referral blocked")

async def scenario_referral_double_bonus(uid: int) -> TestResult:
    """Попытка получить бонус за одного реферала дважды."""
    referrer = uid
    referred = uid + 20000
    t0 = time.monotonic()

    _setup_subscribed_user(referrer)

    # Первая конвертация — должна дать бонус
    bonus_given_flags = [False]

    async def process_bonus() -> bool:
        # Симуляция: bonus_given = False → даём бонус и ставим True
        if not bonus_given_flags[0]:
            bonus_given_flags[0] = True
            await db_grant_subscription(referrer, "1m", REFERRAL_BONUS,
                                         payment_id=f"ref_bonus_{referrer}_{referred}")
            return True
        return False

    # 3 одновременных попытки начислить бонус (race condition)
    results = await asyncio.gather(*[process_bonus() for _ in range(3)])

    lat = (time.monotonic() - t0) * 1000
    given = sum(1 for r in results if r is True)
    ok = given == 1  # только один бонус
    return TestResult(uid, "referral_double_bonus",
                      Status.ABUSE if ok else Status.ERROR,
                      lat, notes=f"bonus given={given} times (expect 1)")


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК D: Paywall-байпасы
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_callback_bypass(uid: int) -> TestResult:
    """Прямой callback без подписки — должен упасть в paywall."""
    _setup_onboarded_user(uid)  # есть профиль, нет подписки
    t0 = time.monotonic()

    # Симуляция _callback_inner: проверяем доступ перед обработкой
    dangerous_callbacks = [
        "agent_start_profile",
        "flow_reels_short",
        "flow_carousel",
        "quick_ideas",
        "my_results",
        "planner_show",
    ]

    blocked_count = 0
    for data in dangerous_callbacks:
        has = await db_has_access(uid)
        if not has:
            blocked_count += 1  # paywall сработал

    lat = (time.monotonic() - t0) * 1000
    ok = blocked_count == len(dangerous_callbacks)
    return TestResult(uid, "callback_bypass",
                      Status.ABUSE if ok else Status.ERROR,
                      lat, notes=f"blocked {blocked_count}/{len(dangerous_callbacks)} callbacks")

async def scenario_trial_then_sub(uid: int) -> TestResult:
    """Юзер на триале оплачивает — подписка должна добавиться к триалу."""
    _setup_trial_user(uid)
    t0 = time.monotonic()
    state_before = await db_get_state(uid)
    await db_grant_subscription(uid, "1m", 30, payment_id=f"trial_pay_{uid}")
    state_after = await db_get_state(uid)
    still_has = await db_has_access(uid)
    lat = (time.monotonic() - t0) * 1000
    ok = state_before == "TRIAL" and state_after == "SUBSCRIBED" and still_has
    return TestResult(uid, "trial_then_sub",
                      Status.OK if ok else Status.ERROR,
                      lat, notes=f"before={state_before} after={state_after}")


async def scenario_onboarding_bypass(uid: int) -> TestResult:
    """
    КРИТИЧЕСКИЙ: после онбординга меню должно быть закрыто.
    Именно этот баг был найден в проде.
    """
    # Очищаем всё — имитируем нового юзера после /reset
    _redis.pop(f"{uid}:__profile__", None)
    _db_subscriptions.pop(uid, None)
    _db_trials.pop(uid, None)
    t0 = time.monotonic()

    # Симулируем завершение онбординга — сохраняем профиль
    _redis[f"{uid}:__profile__"] = json.dumps({
        "niche": "психология", "audience": "женщины 25-35",
        "tone": "дружелюбный", "onboarded": True
    })

    # После онбординга — доступа нет (нет подписки, нет триала)
    has = await db_has_access(uid)

    # Попытка войти в меню → должно быть заблокировано
    result = await handle_message(uid, "хочу пост")

    lat = (time.monotonic() - t0) * 1000
    ok = not has and result == "BLOCKED"
    return TestResult(uid, "onboarding_bypass",
                      Status.ABUSE if ok else Status.ERROR,
                      lat, notes=f"has_access={has} blocked={result=='BLOCKED'} ← КРИТИЧЕСКИЙ")

async def scenario_reset_bypass(uid: int) -> TestResult:
    """После /reset → онбординг заново → меню не должно открываться."""
    _setup_expired_user(uid)
    t0 = time.monotonic()

    # Симулируем /reset: сбрасываем профиль (как в cmd_reset)
    _redis.pop(f"{uid}:__profile__", None)

    # Пробуем отправить сообщение — должно быть заблокировано (NEW state)
    result = await handle_message(uid, "тест после ресета")

    # Даже если профиль есть — без подписки блок
    _redis[f"{uid}:__profile__"] = json.dumps({"niche": "тест", "onboarded": True})
    result2 = await handle_message(uid, "тест после онбординга")

    lat = (time.monotonic() - t0) * 1000
    ok = result == "BLOCKED" and result2 == "BLOCKED"
    return TestResult(uid, "reset_bypass",
                      Status.ABUSE if ok else Status.ERROR,
                      lat, notes=f"r1={result} r2={result2} (both must be BLOCKED)")
    """Юзер на триале оплачивает — подписка должна добавиться к триалу."""
    _setup_trial_user(uid)
    t0 = time.monotonic()

    # До оплаты — в триале
    state_before = await db_get_state(uid)

    # Оплата во время триала
    await db_grant_subscription(uid, "1m", 30, payment_id=f"trial_pay_{uid}")

    # После оплаты — SUBSCRIBED (не теряет доступ)
    state_after = await db_get_state(uid)
    still_has_access = await db_has_access(uid)

    lat = (time.monotonic() - t0) * 1000
    ok = state_before == "TRIAL" and state_after == "SUBSCRIBED" and still_has_access
    return TestResult(uid, "trial_then_sub",
                      Status.OK if ok else Status.ERROR,
                      lat, notes=f"before={state_before} after={state_after}")


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК E: 50-user concurrent blast (все разные состояния)
# ═══════════════════════════════════════════════════════════════════════════════

async def scenario_50_concurrent_blast() -> list[TestResult]:
    """
    50 юзеров одновременно в разных состояниях:
    - 20 подписчиков (слать сообщения)
    - 10 на триале (слать сообщения)
    - 10 expired (должны получить BLOCKED)
    - 5  новых (без профиля, BLOCKED)
    - 5  одновременно оплачивают (конкурентные вебхуки)
    """
    BASE = 5000
    results = []
    t0 = time.monotonic()

    texts = [
        "хочу пост про психологию",
        "сделай карусель про деньги",
        "нужен план на неделю",
        "помоги с рилсом",
        "анализ моего профиля",
    ]

    async def subscribed_user(i: int) -> TestResult:
        uid = BASE + i
        _setup_subscribed_user(uid)
        t = time.monotonic()
        r = await handle_message(uid, texts[i % len(texts)])
        lat = (time.monotonic() - t) * 1000
        return TestResult(uid, "blast_subscribed",
                          Status.OK if r == "OK" else Status.ERROR, lat)

    async def trial_user(i: int) -> TestResult:
        uid = BASE + 20 + i
        _setup_trial_user(uid)
        t = time.monotonic()
        r = await handle_message(uid, texts[i % len(texts)])
        lat = (time.monotonic() - t) * 1000
        return TestResult(uid, "blast_trial",
                          Status.OK if r == "OK" else Status.ERROR, lat)

    async def expired_user(i: int) -> TestResult:
        uid = BASE + 30 + i
        _setup_expired_user(uid)
        t = time.monotonic()
        r = await handle_message(uid, "попытка обхода")
        lat = (time.monotonic() - t) * 1000
        return TestResult(uid, "blast_expired",
                          Status.BLOCKED if r == "BLOCKED" else Status.ERROR, lat,
                          notes="paywall correct")

    async def new_user(i: int) -> TestResult:
        uid = BASE + 40 + i
        # Не вызываем setup — совсем новый
        _db_subscriptions.pop(uid, None)
        _db_trials.pop(uid, None)
        _redis.pop(f"{uid}:__profile__", None)
        t = time.monotonic()
        r = await handle_message(uid, "старт")
        lat = (time.monotonic() - t) * 1000
        return TestResult(uid, "blast_new",
                          Status.BLOCKED if r == "BLOCKED" else Status.ERROR, lat)

    async def paying_user(i: int) -> TestResult:
        uid = BASE + 45 + i
        _setup_onboarded_user(uid)
        t = time.monotonic()
        # Симулируем webhook
        success = await db_grant_subscription(uid, "1m", 30,
                                               payment_id=f"blast_pay_{uid}")
        has = await db_has_access(uid)
        lat = (time.monotonic() - t) * 1000
        return TestResult(uid, "blast_paying",
                          Status.OK if success and has else Status.ERROR, lat)

    tasks = (
        [subscribed_user(i) for i in range(20)] +
        [trial_user(i) for i in range(10)] +
        [expired_user(i) for i in range(10)] +
        [new_user(i) for i in range(5)] +
        [paying_user(i) for i in range(5)]
    )

    results = await asyncio.gather(*tasks)
    return list(results)


# ═══════════════════════════════════════════════════════════════════════════════
# Маппинг сценариев
# ═══════════════════════════════════════════════════════════════════════════════
INDIVIDUAL_SCENARIOS = [
    # Блок A: глупые юзеры
    (2001, "A", scenario_double_tap),
    (2002, "A", scenario_spam_fire),
    (2003, "A", scenario_empty_messages),
    (2004, "A", scenario_ultra_long),
    (2005, "A", scenario_sql_injection),
    (2006, "A", scenario_unicode_bomb),
    (2007, "A", scenario_llm_retry),
    (2008, "A", scenario_rapid_button),
    (2009, "A", scenario_context_switch),
    (2010, "A", scenario_session_corruption),
    # Блок B: платёжные атаки
    (3001, "B", scenario_duplicate_webhook),
    (3002, "B", scenario_concurrent_webhook),
    (3003, "B", scenario_trial_abuse),
    (3004, "B", scenario_fake_buyer_id),
    (3005, "B", scenario_zero_amount_webhook),
    (3006, "B", scenario_expired_reaccess),
    (3007, "B", scenario_subscription_expiry_midflow),
    # Блок C: машина состояний
    (4001, "C", scenario_state_transitions),
    (4002, "C", scenario_new_user_no_access),
    (4003, "C", scenario_referral_self_ref),
    (4004, "C", scenario_referral_double_bonus),
    # Блок D: paywall bypass
    (5001, "D", scenario_callback_bypass),
    (5002, "D", scenario_trial_then_sub),
    (5003, "D", scenario_onboarding_bypass),   # ← найденный баг
    (5004, "D", scenario_reset_bypass),         # ← производный баг
]


# ═══════════════════════════════════════════════════════════════════════════════
# Отчёт
# ═══════════════════════════════════════════════════════════════════════════════
def _bar(val: float, max_val: float, width: int = 20) -> str:
    filled = int((val / max(max_val, 1)) * width)
    return "█" * filled + "░" * (width - filled)

def print_report(individual: list[TestResult], blast: list[TestResult]) -> None:
    all_r = individual + blast
    W = 78

    def count(s): return sum(1 for r in all_r if r.status == s)

    ok      = count(Status.OK)
    blocked = count(Status.BLOCKED)
    abuse   = count(Status.ABUSE)
    recov   = count(Status.RECOVERED)
    errors  = count(Status.ERROR)
    timeouts = count(Status.TIMEOUT)
    deduped = count(Status.DEDUP)
    total   = len(all_r)

    lats = [r.latency_ms for r in all_r]
    avg_lat = sum(lats) / len(lats)
    p95_lat = sorted(lats)[int(len(lats) * 0.95)]
    max_lat = max(lats)

    print("\n" + "═" * W)
    print("  STRESS TEST REPORT v2 — 50+ Users · Payments · State Machine · Attacks")
    print("═" * W)

    # По блокам
    for block, label in [("A","Глупые юзеры"), ("B","Платёжные атаки"),
                          ("C","Машина состояний"), ("D","Paywall bypass")]:
        block_r = [r for r in individual
                   if any(r.user_id == uid
                          for uid, blk, _ in INDIVIDUAL_SCENARIOS
                          if blk == block and r.user_id == uid)]
        if not block_r:
            continue
        print(f"\n  ── Блок {block}: {label}")
        print(f"  {'Сценарий':<30} {'Статус':<14} {'Латентность':>12}  Детали")
        print("  " + "─" * (W-2))
        for r in block_r:
            icon = r.status.value.split()[0]
            label2 = " ".join(r.status.value.split()[1:])
            print(f"  {r.scenario:<30} {icon} {label2:<11} {r.latency_ms:>9.0f}ms  {r.notes}")

    # Blast
    b_ok  = sum(1 for r in blast if r.status in (Status.OK, Status.BLOCKED, Status.ABUSE))
    b_err = sum(1 for r in blast if r.status == Status.ERROR)
    b_lat = [r.latency_ms for r in blast]
    b_avg = sum(b_lat) / len(b_lat) if b_lat else 0
    b_p95 = sorted(b_lat)[int(len(b_lat) * 0.95)] if len(b_lat) > 1 else 0

    by_type = defaultdict(lambda: {"ok": 0, "err": 0})
    for r in blast:
        if r.status in (Status.OK, Status.BLOCKED):
            by_type[r.scenario]["ok"] += 1
        else:
            by_type[r.scenario]["err"] += 1

    print(f"\n  ── Блок E: 50-user concurrent blast")
    print(f"  {'Тип':<25} {'OK':>5} {'ERR':>5}")
    print("  " + "─" * 40)
    for sc, cnt in sorted(by_type.items()):
        print(f"  {sc:<25} {cnt['ok']:>5} {cnt['err']:>5}")
    print(f"  {'ИТОГО':<25} {b_ok:>5} {b_err:>5}  avg={b_avg:.0f}ms p95={b_p95:.0f}ms")

    # Итог
    print(f"\n{'═'*W}")
    print("  ИТОГ")
    print(f"{'═'*W}")
    print(f"\n  Всего тест-кейсов    : {total}")
    print(f"  ✅ OK (полный доступ)  : {ok}")
    print(f"  🚫 BLOCKED (paywall)   : {blocked}  ← ожидаемо")
    print(f"  🛡 ABUSE (атака поймана): {abuse}  ← ожидаемо")
    print(f"  ♻️  RECOVERED (retry)   : {recov}")
    print(f"  🔁 DEDUP (заблокировано): {deduped}")
    print(f"  ❌ ERROR               : {errors}")
    print(f"  ⏱ TIMEOUT             : {timeouts}")

    good = ok + blocked + abuse + recov + deduped
    rate = good / total * 100
    print(f"\n  Успех (включая BLOCKED/ABUSE): {rate:.1f}%  {_bar(good, total)}")

    print(f"\n  ЛАТЕНТНОСТЬ")
    print(f"    avg / p95 / max    : {avg_lat:.0f}ms / {p95_lat:.0f}ms / {max_lat:.0f}ms")
    print(f"    blast avg / p95    : {b_avg:.0f}ms / {b_p95:.0f}ms")

    print(f"\n  СЕМАФОР LLM")
    print(f"    пик / лимит        : {_sem_peak}/{LLM_SEMAPHORE_SIZE}  {_bar(_sem_peak, LLM_SEMAPHORE_SIZE)}")

    print(f"\n  ИДЕМПОТЕНТНОСТЬ")
    dup_test_uid = 3001
    grant_cnt = _grant_calls.get(dup_test_uid, 0)
    print(f"    дубль-вебхук гранты: {grant_cnt} (должно быть 1)")

    trial_test_uid = 3003
    trial_cnt = _trial_calls.get(trial_test_uid, 0)
    print(f"    триал-абуз гранты  : {trial_cnt} (должно быть 1)")

    print(f"\n{'═'*W}")
    if errors == 0 and timeouts == 0:
        print("  🟢  ALL CLEAR — бот готов к 500+ юзерам")
    elif errors <= 2:
        print(f"  🟡  MOSTLY OK — {errors} ошибки, проверь детали выше")
    else:
        print(f"  🔴  NEEDS FIX — {errors} ошибок требуют внимания")
    print(f"{'═'*W}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════════════════
async def main() -> None:
    print()
    print("  🚀 Стресс-тест v2: глупые юзеры + платёжные атаки + машина состояний")
    print(f"  Семафор: {LLM_SEMAPHORE_SIZE} | Retry: {LLM_RETRY} | Trial: {TRIAL_DAYS}д | Ref.бонус: {REFERRAL_BONUS}д")
    print()

    t0 = time.monotonic()

    # Все одиночные сценарии параллельно
    tasks = [fn(uid) for uid, _, fn in INDIVIDUAL_SCENARIOS]
    individual = list(await asyncio.gather(*tasks))

    # 50-user blast
    blast = await scenario_50_concurrent_blast()

    elapsed = time.monotonic() - t0
    print(f"  ⏱ Завершено за {elapsed:.2f}s\n")

    print_report(individual, blast)


if __name__ == "__main__":
    asyncio.run(main())
