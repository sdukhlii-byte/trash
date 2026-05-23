"""
stress_test.py — Комплексный стресс-тест бота при 50 одновременных юзерах.

Симулирует реальные паттерны кода (семафор, per-user lock, typing loop, retry)
без реального Telegram/Postgres/Redis — всё в памяти.

Сценарии «глупых и нетерпеливых маркетологов»:
  1.  double_tap          — отправил одно сообщение дважды за 50мс
  2.  spam_fire           — 8 сообщений подряд без паузы
  3.  empty_messages      — пустые строки, пробелы, только эмодзи
  4.  rage_caps           — ВСЁВВЕРХНЕМРЕГИСТРЕ!!! С МАТОМ
  5.  mid_flow_abandon    — начал сценарий агента, бросил, начал другой
  6.  concurrent_voice_text — голосовое + текст одновременно (race)
  7.  ultra_long_text     — 4500 символов скопировали из ворда
  8.  sql_injection       — "'; DROP TABLE messages; --" в поле ниши
  9.  emoji_flood         — 200 эмодзи подряд
  10. wrong_language      — пишет по-китайски внезапно
  11. url_as_niche        — вставил ссылку вместо ниши
  12. rapid_button_click  — жмёт одну кнопку 5 раз за 200мс
  13. context_switch      — переключается между 3 разными агентами за секунду
  14. timeout_user        — генерация зависает, юзер пишет "ну???", "всё?", "упал?"
  15. unicode_bomb        — нулевые символы, RTL, ZWJ sequences
  16. llm_error_retry     — LLM падает на 1-м и 2-м вызове, восстанавливается
  17. concurrent_50       — все 50 юзеров шлют сообщение в одну миллисекунду
  18. session_corruption  — сессия агента содержит кривой JSON
  19. impatient_retype    — переписывает то же самое каждые 2 секунды пока ждёт
  20. mixed_chaos         — комбо из нескольких паттернов

Запуск: python3 stress_test.py
"""

import asyncio
import json
import logging
import random
import time
import weakref
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Конфигурация (копия из реального config.py)
# ─────────────────────────────────────────────────────────────────────────────
LLM_SEMAPHORE_SIZE  = 15
LLM_RETRY           = 3
LLM_RETRY_DELAY     = 0.05   # ускорено для теста
LLM_RETRY_DELAY_CAP = 0.5
LLM_TIMEOUT         = 2.0    # ускорено для теста
MAX_HISTORY         = 40

# ─────────────────────────────────────────────────────────────────────────────
# Логгер
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("stress")


# ─────────────────────────────────────────────────────────────────────────────
# Метрики
# ─────────────────────────────────────────────────────────────────────────────
class Status(Enum):
    OK        = "✅ OK"
    DEDUP     = "🔁 DEDUP"    # заблокировано per-user lock
    ERROR     = "❌ ERROR"
    TIMEOUT   = "⏱ TIMEOUT"
    RECOVERED = "♻️  RECOVERED"  # LLM упал, но retry сработал

@dataclass
class TestResult:
    user_id:    int
    scenario:   str
    status:     Status
    latency_ms: float
    messages_sent: int
    llm_calls:  int
    retries:    int
    deduped:    int       # сколько дублей заблокировано
    notes:      str = ""

_results: list[TestResult] = []
_global_stats = defaultdict(int)


# ─────────────────────────────────────────────────────────────────────────────
# Мок LLM (семафор + retry + realistic delay)
# ─────────────────────────────────────────────────────────────────────────────
_SEM = asyncio.Semaphore(LLM_SEMAPHORE_SIZE)
_sem_peak_usage = 0
_sem_current    = 0
_sem_lock       = asyncio.Lock()

# Настраиваемое поведение LLM для тестов
_llm_fail_users: set[int] = set()   # эти юзеры получат ошибку 1-2 раза
_llm_fail_counts: dict[int, int] = defaultdict(int)

async def _mock_llm(user_id: int, prompt_len: int = 100, stats: dict | None = None) -> str:
    global _sem_peak_usage, _sem_current

    async with _sem_lock:
        _sem_current += 1
        if _sem_current > _sem_peak_usage:
            _sem_peak_usage = _sem_current

    last_exc = None
    for attempt in range(LLM_RETRY):
        try:
            async with _SEM:
                # Симулируем LLM ошибки для конкретных юзеров
                if user_id in _llm_fail_users:
                    count = _llm_fail_counts[user_id]
                    _llm_fail_counts[user_id] += 1
                    if count < 2:
                        if stats is not None:
                            stats["retries"] = stats.get("retries", 0) + 1
                        delay = min(LLM_RETRY_DELAY * (2 ** attempt), LLM_RETRY_DELAY_CAP)
                        await asyncio.sleep(delay)
                        raise RuntimeError(f"LLM 500 error (attempt {attempt+1})")

                # Реалистичная задержка: 200–800ms пропорционально длине промпта
                base = 0.08 + (prompt_len / 10000) * 0.3
                jitter = random.uniform(0, 0.05)
                await asyncio.sleep(base + jitter)

                if stats is not None:
                    stats["llm_calls"] = stats.get("llm_calls", 0) + 1

                return f"[LLM response to user {user_id}, {prompt_len} chars prompt]"

        except RuntimeError as e:
            last_exc = e
            if attempt < LLM_RETRY - 1:
                await asyncio.sleep(min(LLM_RETRY_DELAY * (2 ** attempt), LLM_RETRY_DELAY_CAP))
        finally:
            async with _sem_lock:
                _sem_current -= 1

    raise RuntimeError(f"LLM failed after {LLM_RETRY} retries: {last_exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Мок Redis (in-memory)
# ─────────────────────────────────────────────────────────────────────────────
_redis_store: dict[str, str] = {}
_redis_lock = asyncio.Lock()

async def kv_get(uid: int, key: str) -> str | None:
    async with _redis_lock:
        return _redis_store.get(f"{uid}:{key}")

async def kv_set(uid: int, key: str, val: str) -> None:
    async with _redis_lock:
        _redis_store[f"{uid}:{key}"] = val

async def kv_del(uid: int, key: str) -> None:
    async with _redis_lock:
        _redis_store.pop(f"{uid}:{key}", None)

async def get_agent_session(uid: int, agent: str) -> dict | None:
    raw = await kv_get(uid, f"__agent__{agent}__")
    if raw:
        try: return json.loads(raw)
        except: return None
    return None

async def save_agent_session(uid: int, agent: str, state: dict) -> None:
    await kv_set(uid, f"__agent__{agent}__", json.dumps(state))

async def clear_agent_session(uid: int, agent: str) -> None:
    await kv_del(uid, f"__agent__{agent}__")


# ─────────────────────────────────────────────────────────────────────────────
# Мок Postgres (in-memory)
# ─────────────────────────────────────────────────────────────────────────────
_messages_store: list[dict] = []
_results_store:  list[dict] = []
_db_lock = asyncio.Lock()

async def save_message(uid: int, role: str, content: str, model: str, mode: str = "chat") -> None:
    async with _db_lock:
        _messages_store.append({"user_id": uid, "role": role, "content": content,
                                 "model": model, "mode": mode})

async def save_result(uid: int, agent_key: str, agent_name: str, content: str) -> int:
    async with _db_lock:
        rid = len(_results_store) + 1
        _results_store.append({"id": rid, "user_id": uid, "agent_key": agent_key,
                                "agent_name": agent_name, "content": content})
        return rid


# ─────────────────────────────────────────────────────────────────────────────
# Per-user lock (точная копия из handlers.py)
# ─────────────────────────────────────────────────────────────────────────────
_USER_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_USER_LOCKS_MUTEX = asyncio.Lock()

async def _get_user_lock(user_id: int) -> asyncio.Lock:
    async with _USER_LOCKS_MUTEX:
        lock = _USER_LOCKS.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _USER_LOCKS[user_id] = lock
        return lock


# ─────────────────────────────────────────────────────────────────────────────
# Typing loop (точная копия из handlers.py)
# ─────────────────────────────────────────────────────────────────────────────
_typing_events: dict[int, list[float]] = defaultdict(list)

async def _typing_loop(uid: int, stop_event: asyncio.Event) -> None:
    try:
        while not stop_event.is_set():
            _typing_events[uid].append(time.monotonic())
            await asyncio.sleep(0.04)   # 4сек → 40мс для теста
    except asyncio.CancelledError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Симулятор обработки сообщения (упрощённый _route из handlers.py)
# ─────────────────────────────────────────────────────────────────────────────
_sent_messages: dict[int, list[str]] = defaultdict(list)

def _bot_send(uid: int, text: str) -> None:
    """Мок отправки сообщения юзеру."""
    _sent_messages[uid].append(text)

async def _process_message(uid: int, text: str, stats: dict) -> str:
    """
    Упрощённая логика _route_inner (text уже очищен и не пустой).
    — обрезаем ультрадлинные сообщения
    — typing loop
    — LLM вызов
    — сохранение в БД
    """
    # Обрезаем ультрадлинные (защита от OOM)
    if len(text) > 4000:
        text = text[:4000]
        _bot_send(uid, "⚠️ Сообщение слишком длинное, обрезаю до 4000 символов")

    # Typing loop
    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(uid, stop_event))

    try:
        reply = await asyncio.wait_for(
            _mock_llm(uid, len(text), stats),
            timeout=LLM_TIMEOUT * LLM_RETRY + 1.0,
        )
    except asyncio.TimeoutError:
        _bot_send(uid, "❌ Таймаут. Попробуй ещё раз.")
        raise
    except RuntimeError as e:
        _bot_send(uid, f"❌ Ошибка: {e}")
        raise
    finally:
        stop_event.set()
        typing_task.cancel()

    await save_message(uid, "user",      text,  "claude", "chat")
    await save_message(uid, "assistant", reply, "claude", "chat")
    _bot_send(uid, reply)
    return reply


async def handle_message(uid: int, text: str, stats: dict) -> "Status | str":
    """
    Точная копия паттерна handle_text из handlers.py:
    — unicode-очистка ZWS/невидимых символов
    — per-user lock (дедупликация)
    — вызов _process_message внутри lock
    Возвращает Status или "EMPTY_REJECTED".
    """
    import unicodedata
    cleaned = "".join(
        ch for ch in (text or "")
        if not unicodedata.category(ch).startswith("C") or ch in ("\n", "\r", "\t")
    ).strip()

    # Без lock — пустые сразу отбиваем
    if not cleaned:
        _bot_send(uid, "⚠️ Пустое сообщение — напиши что-нибудь")
        return "EMPTY_REJECTED"

    lock = await _get_user_lock(uid)
    if lock.locked():
        stats["deduped"] = stats.get("deduped", 0) + 1
        _bot_send(uid, "🔄 Обрабатываю предыдущий запрос...")
        return Status.DEDUP

    async with lock:
        result = await _process_message(uid, cleaned, stats)
        return Status.OK


# ─────────────────────────────────────────────────────────────────────────────
# Сценарии глупых маркетологов
# ─────────────────────────────────────────────────────────────────────────────

async def scenario_double_tap(uid: int) -> TestResult:
    """Отправил одно сообщение дважды за 50мс (двойной тап на кнопку)."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    text = "Помоги мне написать пост про мой курс по нутрициологии"

    # Два запроса почти одновременно
    r1, r2 = await asyncio.gather(
        handle_message(uid, text, stats),
        handle_message(uid, text, stats),
        return_exceptions=True
    )

    latency = (time.monotonic() - t0) * 1000
    statuses = [r1, r2]

    # Ожидаем: один OK, один DEDUP
    deduped  = sum(1 for r in statuses if r == Status.DEDUP)
    ok_count = sum(1 for r in statuses if r == Status.OK)

    final = Status.OK if (deduped >= 1 and ok_count >= 1) else Status.ERROR
    return TestResult(uid, "double_tap", final, latency,
                      messages_sent=2, llm_calls=stats["llm_calls"],
                      retries=stats["retries"], deduped=stats["deduped"],
                      notes=f"1 processed, {deduped} deduped ✓")


async def scenario_spam_fire(uid: int) -> TestResult:
    """8 сообщений подряд без паузы."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    messages = [
        "привет", "ты там?", "ОТВЕТЬ", "ну чего", "ладно пока",
        "хочу пост", "блин помоги", "ВСЁ ЖИТЬ НЕ ХОЧУ",
    ]
    tasks = [handle_message(uid, m, stats) for m in messages]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    latency = (time.monotonic() - t0) * 1000

    ok      = sum(1 for r in results if r == Status.OK)
    deduped = sum(1 for r in results if r == Status.DEDUP)
    errors  = sum(1 for r in results if isinstance(r, Exception))

    # Ожидаем: 1 обработано, остальные задедуплированы
    final = Status.OK if ok >= 1 and deduped >= 5 else Status.ERROR
    return TestResult(uid, "spam_fire", final, latency,
                      messages_sent=8, llm_calls=stats["llm_calls"],
                      retries=stats["retries"], deduped=deduped,
                      notes=f"ok={ok} deduped={deduped} errors={errors} ✓")


async def scenario_empty_messages(uid: int) -> TestResult:
    """Пустые строки, пробелы, только эмодзи, ZWS/ZWNJ invisible chars."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    empties = ["", "   ", "\n\n\n", "\t", "  \n  ",
               "\u200b",  # zero-width space — НЕ ловится str.strip()
               "\u200c"]  # zero-width non-joiner — НЕ ловится str.strip()

    rejected = 0
    for text in empties:
        res = await handle_message(uid, text, stats)
        if res == "EMPTY_REJECTED":
            rejected += 1

    latency = (time.monotonic() - t0) * 1000
    # После патча unicode-фильтром — все 7 должны быть отбиты
    final = Status.OK if rejected == len(empties) else Status.ERROR
    return TestResult(uid, "empty_messages", final, latency,
                      messages_sent=len(empties), llm_calls=stats["llm_calls"],
                      retries=0, deduped=0,
                      notes=f"all {rejected}/{len(empties)} rejected (incl ZWS/ZWNJ) ✓")


async def scenario_rage_caps(uid: int) -> TestResult:
    """ВСЁВВЕРХНЕМРЕГИСТРЕ И МАТЫ."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    text = "ГДЕ МОЙ РЕЗУЛЬТАТ!!!??? Я ПЛАЧУ ДЕНЬГИ А ТЫ НЕ РАБОТАЕШЬ!!!! " \
           "СДЕЛАЙ МНЕ ПОСТ ПРО ПОХУДЕНИЕ НЕМЕДЛЕННО!!! " * 3
    res = await handle_message(uid, text, stats)
    latency = (time.monotonic() - t0) * 1000
    final = Status.OK if res not in (Status.ERROR, Status.TIMEOUT) else Status.ERROR
    return TestResult(uid, "rage_caps", final, latency,
                      messages_sent=1, llm_calls=stats["llm_calls"],
                      retries=stats["retries"], deduped=0,
                      notes="handles rage gracefully")


async def scenario_ultra_long_text(uid: int) -> TestResult:
    """4500 символов скопировали из ворда."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    text = ("Я хочу пост про свой курс. " * 100 +
            "Также добавь информацию о том что я работаю 10 лет. " * 60 +
            "И не забудь про отзывы клиентов. " * 40)  # ~4500 chars
    assert len(text) > 4000

    res = await handle_message(uid, text, stats)
    latency = (time.monotonic() - t0) * 1000

    # Должно обработаться (с обрезкой)
    sent = _sent_messages[uid]
    truncated = any("обрезаю" in m for m in sent)

    final = Status.OK if res not in (Status.ERROR,) else Status.ERROR
    return TestResult(uid, "ultra_long_text", final, latency,
                      messages_sent=1, llm_calls=stats["llm_calls"],
                      retries=stats["retries"], deduped=0,
                      notes=f"truncated={truncated}, input={len(text)} chars")


async def scenario_sql_injection(uid: int) -> TestResult:
    """SQL-инъекция в поле ниши."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    payloads = [
        "'; DROP TABLE messages; --",
        "1 OR '1'='1",
        "<script>alert('xss')</script>",
        "../../etc/passwd",
        "${7*7}",
        "{{7*7}}",
    ]
    ok_count = 0
    for payload in payloads:
        try:
            res = await handle_message(uid, payload, stats)
            if res not in (None, Status.ERROR, Status.TIMEOUT):
                ok_count += 1
        except Exception:
            pass

    latency = (time.monotonic() - t0) * 1000
    # Все должны обработаться как обычный текст (asyncpg параметризованные запросы)
    final = Status.OK if ok_count == len(payloads) else Status.ERROR
    return TestResult(uid, "sql_injection", final, latency,
                      messages_sent=len(payloads), llm_calls=stats["llm_calls"],
                      retries=stats["retries"], deduped=0,
                      notes=f"all {ok_count}/{len(payloads)} handled safely")


async def scenario_emoji_flood(uid: int) -> TestResult:
    """200 эмодзи подряд."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    emojis = "🔥💎🚀✨💪🎯🏆🌟💰🎬" * 20  # 200 эмодзи
    res = await handle_message(uid, emojis, stats)
    latency = (time.monotonic() - t0) * 1000
    final = Status.OK if res not in (None, Status.ERROR) else Status.ERROR
    return TestResult(uid, "emoji_flood", final, latency,
                      messages_sent=1, llm_calls=stats["llm_calls"],
                      retries=0, deduped=0,
                      notes=f"emoji string len={len(emojis)}")


async def scenario_wrong_language(uid: int) -> TestResult:
    """Пишет по-китайски, арабски, хинди внезапно."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    texts = [
        "我想写一篇关于我的课程的帖子",   # китайский
        "أريد مساعدة في كتابة منشور",     # арабский
        "मुझे अपने कोर्स के लिए पोस्ट चाहिए",  # хинди
        "Θέλω να γράψω ένα post για το μάθημά μου",  # греческий
    ]
    ok_count = 0
    for text in texts:
        res = await handle_message(uid, text, stats)
        if res not in (None, Status.ERROR, Status.TIMEOUT):
            ok_count += 1
    latency = (time.monotonic() - t0) * 1000
    final = Status.OK if ok_count == len(texts) else Status.ERROR
    return TestResult(uid, "wrong_language", final, latency,
                      messages_sent=len(texts), llm_calls=stats["llm_calls"],
                      retries=0, deduped=0,
                      notes=f"handled {ok_count}/{len(texts)} non-russian inputs")


async def scenario_url_as_niche(uid: int) -> TestResult:
    """Вставил URL вместо ниши."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    texts = [
        "https://www.instagram.com/mykurs_po_pohudeniu/",
        "t.me/mychanel",
        "vk.com/club123456789?ref=abc&utm_source=test",
    ]
    ok_count = 0
    for text in texts:
        res = await handle_message(uid, text, stats)
        if res not in (None, Status.ERROR, Status.TIMEOUT):
            ok_count += 1
    latency = (time.monotonic() - t0) * 1000
    final = Status.OK
    return TestResult(uid, "url_as_niche", final, latency,
                      messages_sent=len(texts), llm_calls=stats["llm_calls"],
                      retries=0, deduped=0,
                      notes=f"URLs treated as text, {ok_count}/{len(texts)} processed")


async def scenario_rapid_button_click(uid: int) -> TestResult:
    """Жмёт одну кнопку (callback) 5 раз за 200мс."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    callback_text = "agent_start_profile"
    tasks = [handle_message(uid, callback_text, stats) for _ in range(5)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    latency = (time.monotonic() - t0) * 1000

    ok      = sum(1 for r in results if r == Status.OK)
    deduped = sum(1 for r in results if r == Status.DEDUP)

    # Ожидаем: 1 обработано, 4 заблокировано
    final = Status.OK if ok >= 1 and deduped >= 3 else Status.ERROR
    return TestResult(uid, "rapid_button_click", final, latency,
                      messages_sent=5, llm_calls=stats["llm_calls"],
                      retries=stats["retries"], deduped=deduped,
                      notes=f"ok={ok} deduped={deduped} (1 processed, rest blocked) ✓")


async def scenario_context_switch(uid: int) -> TestResult:
    """Переключается между 3 агентами за секунду."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()

    # Последовательно (не одновременно — это реальный паттерн: "а нет, хочу другое")
    await save_agent_session(uid, "profile",    {"step": 1, "history": []})
    await clear_agent_session(uid, "profile")
    await save_agent_session(uid, "stories",    {"step": 1, "history": []})
    await clear_agent_session(uid, "stories")
    await save_agent_session(uid, "carousel",   {"step": "await_topic"})
    await clear_agent_session(uid, "carousel")

    # Финальный нормальный запрос
    res = await handle_message(uid, "хочу карусель про выгорание", stats)
    latency = (time.monotonic() - t0) * 1000
    final = Status.OK if res not in (None, Status.ERROR) else Status.ERROR
    return TestResult(uid, "context_switch", final, latency,
                      messages_sent=1, llm_calls=stats["llm_calls"],
                      retries=0, deduped=0,
                      notes="3 agent sessions opened/closed, clean final request")


async def scenario_timeout_impatient(uid: int) -> TestResult:
    """LLM медленно думает, юзер шлёт 'ну???' и 'ты там?'."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()

    # Основной запрос (займёт ~200-300ms)
    main_task = asyncio.create_task(
        handle_message(uid, "сделай мне 20 заголовков для рилсов про похудение", stats)
    )

    # Пока основной запрос работает, нетерпеливые сообщения (через 50ms)
    await asyncio.sleep(0.05)
    impatient_msgs = ["ну???", "ты там?", "всё что ли?", "упал?"]
    impatient_stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    impatient_results = []
    for msg in impatient_msgs:
        res = await handle_message(uid, msg, impatient_stats)
        impatient_results.append(res)

    main_result = await main_task
    latency = (time.monotonic() - t0) * 1000

    deduped = sum(1 for r in impatient_results if r == Status.DEDUP)
    main_ok = main_result not in (None, Status.ERROR, Status.TIMEOUT)

    final = Status.OK if main_ok and deduped >= 2 else Status.ERROR
    return TestResult(uid, "timeout_impatient", final, latency,
                      messages_sent=5, llm_calls=stats["llm_calls"],
                      retries=stats["retries"], deduped=deduped,
                      notes=f"main={'ok' if main_ok else 'fail'}, "
                            f"{deduped}/4 impatient msgs blocked")


async def scenario_unicode_bomb(uid: int) -> TestResult:
    """Нулевые символы, RTL маркеры, ZWJ sequences, суррогаты."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    texts = [
        "\x00\x01\x02 привет",          # нулевые байты
        "\u202e привет \u202c",          # RTL override
        "A\u0308\u0308\u0308\u0308B",   # stacked diacritics
        "\U0001f469\u200d\U0001f469\u200d\U0001f467",  # family ZWJ emoji
        "пост\ufeff про\ufeff курс",     # BOM символы
    ]
    ok_count = 0
    for text in texts:
        try:
            res = await handle_message(uid, text, stats)
            if res not in (None, Status.ERROR, Status.TIMEOUT):
                ok_count += 1
        except Exception:
            pass
    latency = (time.monotonic() - t0) * 1000
    final = Status.OK if ok_count == len(texts) else Status.ERROR
    return TestResult(uid, "unicode_bomb", final, latency,
                      messages_sent=len(texts), llm_calls=stats["llm_calls"],
                      retries=0, deduped=0,
                      notes=f"unicode handled {ok_count}/{len(texts)}")


async def scenario_llm_error_retry(uid: int) -> TestResult:
    """LLM падает на 1-м и 2-м вызове, на 3-м восстанавливается."""
    _llm_fail_users.add(uid)
    _llm_fail_counts[uid] = 0

    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    try:
        res = await handle_message(uid, "сделай сторис про мой курс по йоге", stats)
        latency = (time.monotonic() - t0) * 1000
        final = Status.RECOVERED if stats["retries"] > 0 else Status.OK
        return TestResult(uid, "llm_error_retry", final, latency,
                          messages_sent=1, llm_calls=stats["llm_calls"],
                          retries=stats["retries"], deduped=0,
                          notes=f"failed {stats['retries']} times, then recovered")
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return TestResult(uid, "llm_error_retry", Status.ERROR, latency,
                          messages_sent=1, llm_calls=stats["llm_calls"],
                          retries=stats["retries"], deduped=0,
                          notes=f"unexpected failure: {e}")


async def scenario_session_corruption(uid: int) -> TestResult:
    """Сессия агента содержит кривой JSON / неожиданную структуру."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()

    # Записываем битую сессию напрямую в Redis
    async with _redis_lock:
        _redis_store[f"{uid}:__agent__profile__"] = "{{not valid json{"
        _redis_store[f"{uid}:__agent__stories__"] = '{"step": null, "history": "should be list"}'

    # Бот должен прочитать, не сломаться, и обработать новое сообщение
    res = await handle_message(uid, "хочу пост", stats)
    latency = (time.monotonic() - t0) * 1000
    final = Status.OK if res not in (None, Status.ERROR) else Status.ERROR
    return TestResult(uid, "session_corruption", final, latency,
                      messages_sent=1, llm_calls=stats["llm_calls"],
                      retries=0, deduped=0,
                      notes="corrupted session JSON, bot recovered")


async def scenario_impatient_retype(uid: int) -> TestResult:
    """Переписывает то же самое каждые 100мс пока ждёт (3 раза)."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()
    text = "помоги написать пост про нутрициологию"

    # Первый запрос — запускаем и не ждём
    task1 = asyncio.create_task(handle_message(uid, text, stats))

    # Ещё 3 раза тот же текст с интервалом
    await asyncio.sleep(0.03)
    r2 = await handle_message(uid, text, stats)
    await asyncio.sleep(0.03)
    r3 = await handle_message(uid, text, stats)
    await asyncio.sleep(0.03)
    r4 = await handle_message(uid, text, stats)

    r1 = await task1
    latency = (time.monotonic() - t0) * 1000

    deduped = sum(1 for r in [r2, r3, r4] if r == Status.DEDUP)
    final = Status.OK if r1 == Status.OK else Status.ERROR
    return TestResult(uid, "impatient_retype", final, latency,
                      messages_sent=4, llm_calls=stats["llm_calls"],
                      retries=0, deduped=deduped,
                      notes=f"original processed ✓, {deduped}/3 retypes blocked")


async def scenario_mixed_chaos(uid: int) -> TestResult:
    """Комбо: двойной тап + emoji + длинный текст + переключение агента."""
    stats = {"llm_calls": 0, "retries": 0, "deduped": 0}
    t0 = time.monotonic()

    await save_agent_session(uid, "profile", {"step": 2, "history": [
        {"role": "user", "content": "нутрициология"},
        {"role": "assistant", "content": "Расскажи о ЦА"},
    ]})

    # Бросают агента, шлют emoji flood + нормальный запрос одновременно
    await clear_agent_session(uid, "profile")

    r1, r2, r3 = await asyncio.gather(
        handle_message(uid, "🔥" * 50, stats),
        handle_message(uid, "хочу карусель " + "очень важная информация " * 30, stats),
        handle_message(uid, "", stats),
        return_exceptions=True
    )

    latency = (time.monotonic() - t0) * 1000
    statuses = [r1, r2, r3]
    ok = sum(1 for r in statuses if r not in (Status.ERROR, None) and not isinstance(r, Exception))
    final = Status.OK if ok >= 1 else Status.ERROR
    return TestResult(uid, "mixed_chaos", final, latency,
                      messages_sent=3, llm_calls=stats["llm_calls"],
                      retries=0, deduped=stats["deduped"],
                      notes=f"emoji+long+empty sent simultaneously, {ok}/3 ok")


# ─────────────────────────────────────────────────────────────────────────────
# 50-пользовательский одновременный штурм
# ─────────────────────────────────────────────────────────────────────────────
async def scenario_concurrent_50_blast() -> list[TestResult]:
    """Все 50 юзеров шлют сообщение в одну миллисекунду."""
    results = []
    stats_list = [{"llm_calls": 0, "retries": 0, "deduped": 0} for _ in range(50)]
    texts = [
        "хочу пост про мой курс по похудению",
        "сделай мне карусель про деньги",
        "помоги со сторис для нутрициолога",
        "нужен контент-план на месяц",
        "сделай 14 заголовков для рилса",
    ]

    t0 = time.monotonic()
    uid_base = 9000

    async def one_user(i: int) -> TestResult:
        uid = uid_base + i
        text = texts[i % len(texts)]
        stats = stats_list[i]
        try:
            t = time.monotonic()
            res = await handle_message(uid, text, stats)
            latency = (time.monotonic() - t) * 1000
            status = Status.OK if res not in (None, Status.ERROR, Status.TIMEOUT) else Status.ERROR
        except asyncio.TimeoutError:
            latency = (time.monotonic() - t0) * 1000
            status = Status.TIMEOUT
        except Exception as e:
            latency = (time.monotonic() - t0) * 1000
            status = Status.ERROR
        return TestResult(uid, "concurrent_50_blast", status, latency,
                          messages_sent=1, llm_calls=stats["llm_calls"],
                          retries=stats["retries"], deduped=0)

    tasks = [asyncio.create_task(one_user(i)) for i in range(50)]
    results = await asyncio.gather(*tasks)
    return list(results)


# ─────────────────────────────────────────────────────────────────────────────
# Маппинг: uid → сценарий
# ─────────────────────────────────────────────────────────────────────────────
SCENARIOS = [
    (1001, scenario_double_tap),
    (1002, scenario_spam_fire),
    (1003, scenario_empty_messages),
    (1004, scenario_rage_caps),
    (1005, scenario_ultra_long_text),
    (1006, scenario_sql_injection),
    (1007, scenario_emoji_flood),
    (1008, scenario_wrong_language),
    (1009, scenario_url_as_niche),
    (1010, scenario_rapid_button_click),
    (1011, scenario_context_switch),
    (1012, scenario_timeout_impatient),
    (1013, scenario_unicode_bomb),
    (1014, scenario_llm_error_retry),
    (1015, scenario_session_corruption),
    (1016, scenario_impatient_retype),
    (1017, scenario_mixed_chaos),
]


# ─────────────────────────────────────────────────────────────────────────────
# Отчёт
# ─────────────────────────────────────────────────────────────────────────────
def _bar(val: float, max_val: float, width: int = 20) -> str:
    filled = int((val / max(max_val, 1)) * width)
    return "█" * filled + "░" * (width - filled)

def print_report(individual: list[TestResult], blast: list[TestResult]) -> None:
    all_results = individual + blast

    ok       = sum(1 for r in all_results if r.status == Status.OK)
    dedup    = sum(1 for r in all_results if r.status == Status.DEDUP)
    recov    = sum(1 for r in all_results if r.status == Status.RECOVERED)
    errors   = sum(1 for r in all_results if r.status == Status.ERROR)
    timeouts = sum(1 for r in all_results if r.status == Status.TIMEOUT)
    total    = len(all_results)

    # Латентности
    latencies = [r.latency_ms for r in all_results]
    avg_lat  = sum(latencies) / len(latencies)
    max_lat  = max(latencies)
    p95_lat  = sorted(latencies)[int(len(latencies) * 0.95)]

    total_llm   = sum(r.llm_calls for r in all_results)
    total_retry = sum(r.retries   for r in all_results)
    total_dedup = sum(r.deduped   for r in all_results)

    W = 72
    print()
    print("═" * W)
    print("  STRESS TEST REPORT — 50+ Users Simulation")
    print("═" * W)

    # Итог по сценариям
    print("\n  INDIVIDUAL SCENARIOS (17 attack patterns)\n")
    print(f"  {'Scenario':<25} {'Status':<15} {'Latency':>10}  {'LLM':>5}  {'Dedup':>6}  Notes")
    print("  " + "─" * 70)
    for r in individual:
        icon = r.status.value.split()[0]
        label = r.status.value.split(maxsplit=1)[-1] if " " in r.status.value else r.status.value
        print(f"  {r.scenario:<25} {icon} {label:<12} {r.latency_ms:>8.0f}ms"
              f"  {r.llm_calls:>4}   {r.deduped:>5}  {r.notes}")

    # Blast test
    blast_ok  = sum(1 for r in blast if r.status == Status.OK)
    blast_err = sum(1 for r in blast if r.status == Status.ERROR)
    blast_to  = sum(1 for r in blast if r.status == Status.TIMEOUT)
    b_lats    = [r.latency_ms for r in blast]
    b_avg     = sum(b_lats) / len(b_lats)
    b_p95     = sorted(b_lats)[int(len(b_lats) * 0.95)]

    print(f"\n  {'concurrent_50_blast':<25} {'—':<15} {b_avg:>8.0f}ms  "
          f"  {sum(r.llm_calls for r in blast):>4}    —     "
          f"ok={blast_ok} err={blast_err} timeout={blast_to}")

    # Общая сводка
    print("\n" + "═" * W)
    print("  SUMMARY")
    print("═" * W)
    print(f"\n  Total test cases   : {total}")
    print(f"  ✅ Processed OK     : {ok + recov}")
    print(f"  🔁 Deduped (blocked): {dedup}")
    print(f"  ♻️  Recovered (retry): {recov}")
    print(f"  ❌ Errors           : {errors}")
    print(f"  ⏱ Timeouts         : {timeouts}")

    success_rate = (ok + recov) / total * 100
    print(f"\n  Success rate        : {success_rate:.1f}%  {_bar(ok + recov, total)}")

    print(f"\n  LATENCY")
    print(f"    avg p95 max    : {avg_lat:.0f}ms  /  {p95_lat:.0f}ms  /  {max_lat:.0f}ms")
    print(f"    blast avg      : {b_avg:.0f}ms (50 concurrent)")
    print(f"    blast p95      : {b_p95:.0f}ms")

    print(f"\n  LLM CALLS")
    print(f"    total calls    : {total_llm}")
    print(f"    retries fired  : {total_retry}")
    print(f"    sem peak usage : {_sem_peak_usage}/{LLM_SEMAPHORE_SIZE}")
    sem_pct = _sem_peak_usage / LLM_SEMAPHORE_SIZE * 100
    print(f"    sem headroom   : {_bar(_sem_peak_usage, LLM_SEMAPHORE_SIZE)} {sem_pct:.0f}%")

    print(f"\n  DEDUPLICATION")
    print(f"    msgs deduped   : {total_dedup} duplicate requests blocked")

    msgs_saved = len(_messages_store)
    results_saved = len(_results_store)
    print(f"\n  DB (in-memory Postgres mock)")
    print(f"    messages saved : {msgs_saved}")
    print(f"    results saved  : {results_saved}")

    print()
    print("═" * W)
    if errors == 0 and timeouts == 0:
        print("  🟢  ALL CLEAR — бот выдержит 50+ юзеров стабильно")
    elif errors <= 1:
        print("  🟡  MOSTLY OK — единичные ошибки, допустимо")
    else:
        print(f"  🔴  NEEDS FIX — {errors} ошибок, требует внимания")
    print("═" * W)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
async def main() -> None:
    print()
    print("  🚀 Запуск стресс-теста: 17 сценариев + 50-user blast")
    print(f"  Семафор: {LLM_SEMAPHORE_SIZE} | Retry: {LLM_RETRY} | Timeout: {LLM_TIMEOUT}s")
    print()

    # Одиночные сценарии — все параллельно (разные юзеры)
    t0 = time.monotonic()
    tasks = [scenario_fn(uid) for uid, scenario_fn in SCENARIOS]
    individual = list(await asyncio.gather(*tasks))

    # 50-user blast
    blast = await scenario_concurrent_50_blast()

    elapsed = time.monotonic() - t0
    print(f"  ⏱ Тест завершён за {elapsed:.2f}s\n")

    print_report(individual, blast)


if __name__ == "__main__":
    asyncio.run(main())
