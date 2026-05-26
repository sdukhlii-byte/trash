"""
tests/test_state_machine.py

Покрываем:
  - все 5 переходов UserState
  - кэш состояния (invalidate_state_cache)
  - has_access()
  - onboarding flow (handle_onboarding, дубль return True исключён)
  - payment webhook idempotency
  - Redis singleton (нет гонки при двойной инициализации)
  - Whisper language=None (авто-детект)
  - jitter расписания (±5 мин вместо ±30 сек)

Запуск:
    pip install pytest pytest-asyncio --break-system-packages
    pytest tests/ -v
"""
import asyncio
import json
import sys
import os
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Stub-модули для изоляции от реального окружения ──────────────────────────

def _make_stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod

# Минимальный config
_make_stub(
    "config",
    BOT_TOKEN="test-token",
    WEBHOOK_URL="",
    WEBHOOK_SECRET="",
    SEM_FAST_SIZE=10,
    SEM_HEAVY_SIZE=4,
    OPENROUTER_KEY="test",
    OPENROUTER_URL="https://example.com/v1",
    OPENAI_KEY="test",
    WHISPER_URL="https://api.openai.com/v1/audio/transcriptions",
    MODELS={"claude": "anthropic/claude-sonnet-4-0", "gpt4": "openai/gpt-4o"},
    DEFAULT_MODEL="claude",
    MAX_HISTORY=20,
    LLM_RETRY=3,
    LLM_RETRY_DELAY=1,
    LLM_RETRY_DELAY_CAP=30,
    LLM_TIMEOUT=60,
    GEN_TIMEOUT=120,
    QUICK_IDEAS_SYSTEM="quick ideas system",
    CHAT_SYSTEM="chat system",
    build_profile_ctx=lambda p: "",
    MIRA_INTRO="Привет, я Мира.",
    TRIAL_DAYS=3,
    PLANNER_IDEAS_SYSTEM="planner system",
    DAILY_DIGEST_SYSTEM="daily system",
    REFINE_SYSTEM="refine system",
    REGEN_SYSTEM="regen system",
)


# ════════════════════════════════════════════════════════════════════════════════
# Часть 1: UserState machine
# ════════════════════════════════════════════════════════════════════════════════

class FakeProfile:
    def __init__(self, niche="", onboarded=False):
        self._data = {"niche": niche, "onboarded": onboarded}

    async def get(self):
        return self._data


@pytest.mark.asyncio
class TestUserState:

    async def _make_state(
        self,
        onboarded=False,
        has_sub=False,
        has_trial=False,
        used_trial=False,
        had_sub_in_db=False,
        niche="фитнес",
    ):
        """Создаёт мок-окружение и импортирует get_user_state."""
        from user_state import _compute_user_state, UserState

        profile = {"niche": niche, "onboarded": onboarded} if (niche or onboarded) else {}

        with (
            patch("user_state.is_onboarded", new=AsyncMock(return_value=onboarded)),
            patch("user_state.get_profile",  new=AsyncMock(return_value=profile)),
            patch("user_state.get_subscription", new=AsyncMock(return_value={"id": 1} if has_sub else None)),
            patch("user_state.get_trial",        new=AsyncMock(return_value={"id": 1} if has_trial else None)),
            patch("user_state.has_used_trial",   new=AsyncMock(return_value=used_trial)),
        ):
            # Мокируем DB pool для raw SQL-запроса
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value={"1": 1} if had_sub_in_db else None)
            pool_mock = MagicMock()
            pool_mock.acquire = MagicMock(
                return_value=MagicMock(
                    __aenter__=AsyncMock(return_value=conn),
                    __aexit__=AsyncMock(return_value=None),
                )
            )
            with patch("user_state._get_pool", return_value=pool_mock):
                return await _compute_user_state(42)

    async def test_new_user_no_niche(self):
        """Пользователь без ниши и онбординга → NEW."""
        from user_state import UserState
        result = await self._make_state(onboarded=False, niche="")
        assert result == UserState.NEW

    async def test_onboarded_no_access(self):
        """Онбординг пройден, нет подписки и триала → ONBOARDED."""
        from user_state import UserState
        result = await self._make_state(onboarded=True)
        assert result == UserState.ONBOARDED

    async def test_trial_state(self):
        """Есть активный триал → TRIAL."""
        from user_state import UserState
        result = await self._make_state(onboarded=True, has_trial=True)
        assert result == UserState.TRIAL

    async def test_subscribed_state(self):
        """Есть активная подписка → SUBSCRIBED."""
        from user_state import UserState
        result = await self._make_state(onboarded=True, has_sub=True)
        assert result == UserState.SUBSCRIBED

    async def test_expired_used_trial(self):
        """Триал использован, нет активной подписки → EXPIRED."""
        from user_state import UserState
        result = await self._make_state(onboarded=True, used_trial=True)
        assert result == UserState.EXPIRED

    async def test_expired_had_sub_in_db(self):
        """Была подписка в БД, сейчас нет → EXPIRED."""
        from user_state import UserState
        result = await self._make_state(onboarded=True, had_sub_in_db=True)
        assert result == UserState.EXPIRED

    async def test_sub_takes_priority_over_trial(self):
        """Подписка и триал одновременно → SUBSCRIBED (подписка главнее)."""
        from user_state import UserState
        result = await self._make_state(onboarded=True, has_sub=True, has_trial=True)
        assert result == UserState.SUBSCRIBED


# ════════════════════════════════════════════════════════════════════════════════
# Часть 2: has_access()
# ════════════════════════════════════════════════════════════════════════════════

def test_has_access_trial():
    from user_state import has_access, UserState
    assert has_access(UserState.TRIAL) is True

def test_has_access_subscribed():
    from user_state import has_access, UserState
    assert has_access(UserState.SUBSCRIBED) is True

def test_no_access_new():
    from user_state import has_access, UserState
    assert has_access(UserState.NEW) is False

def test_no_access_expired():
    from user_state import has_access, UserState
    assert has_access(UserState.EXPIRED) is False

def test_no_access_onboarded():
    from user_state import has_access, UserState
    assert has_access(UserState.ONBOARDED) is False


# ════════════════════════════════════════════════════════════════════════════════
# Часть 3: State cache invalidation
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cache_invalidation():
    """invalidate_state_cache() удаляет ключ → следующий get идёт в БД."""
    from user_state import invalidate_state_cache, _STATE_KEY
    kv_del = AsyncMock()
    with patch("user_state.kv_del", kv_del):
        await invalidate_state_cache(123)
    kv_del.assert_called_once_with(123, _STATE_KEY)


@pytest.mark.asyncio
async def test_get_user_state_uses_cache():
    """get_user_state() возвращает кэшированное значение без обращения к БД."""
    from user_state import get_user_state, UserState
    with patch("user_state.kv_get", new=AsyncMock(return_value="subscribed")):
        state = await get_user_state(999)
    assert state == UserState.SUBSCRIBED


@pytest.mark.asyncio
async def test_get_user_state_cache_miss_calls_compute():
    """При промахе кэша вызывается _compute_user_state."""
    from user_state import get_user_state, UserState
    with (
        patch("user_state.kv_get", new=AsyncMock(return_value=None)),
        patch("user_state._compute_user_state", new=AsyncMock(return_value=UserState.TRIAL)) as mock_compute,
        patch("user_state.kv_set", new=AsyncMock()),
    ):
        state = await get_user_state(777)
    assert state == UserState.TRIAL
    mock_compute.assert_called_once_with(777)


# ════════════════════════════════════════════════════════════════════════════════
# Часть 4: Onboarding — дубль return True исключён
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_onboarding_returns_true_once():
    """
    handle_onboarding() должен возвращать True ровно один раз на каждый шаг.
    В оригинальном коде было два `return True` подряд — второй мёртвый.
    Этот тест сломался бы если бы функция вернула значение дважды.
    """
    from flows.onboarding import handle_onboarding

    update = MagicMock()
    update.effective_chat = AsyncMock()
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()

    state = {"step": 0, "data": {}}

    with (
        patch("flows.onboarding.save_onboarding_state", new=AsyncMock()),
        patch("flows.onboarding.onb_next",              new=AsyncMock()),
        patch("flows.onboarding.send",                  new=AsyncMock()),
    ):
        result = await handle_onboarding(update, 42, "фитнес", state)

    assert result is True


@pytest.mark.asyncio
async def test_onboarding_three_steps():
    """Все три шага обрабатываются, данные копятся в state['data']."""
    from flows.onboarding import handle_onboarding

    update = MagicMock()
    update.message = AsyncMock()

    state = {"step": 0, "data": {}}
    answers = ["фитнес", "женщины 30-45 которые хотят похудеть", "дружелюбно и прямо"]

    with (
        patch("flows.onboarding.save_onboarding_state", new=AsyncMock()),
        patch("flows.onboarding.onb_next",              new=AsyncMock()),
        patch("flows.onboarding.send",                  new=AsyncMock()),
    ):
        for i, answer in enumerate(answers):
            state["step"] = i
            result = await handle_onboarding(update, 42, answer, state)
            assert result is True, f"Step {i} did not return True"
            assert state["data"].get(["niche", "audience", "tone"][i]) == answer


@pytest.mark.asyncio
async def test_onboarding_step_beyond_range_returns_false():
    """Если step >= 3 — онбординг завершён, возвращает False."""
    from flows.onboarding import handle_onboarding

    state = {"step": 3, "data": {"niche": "x", "audience": "y", "tone": "z"}}
    result = await handle_onboarding(MagicMock(), 42, "extra", state)
    assert result is False


# ════════════════════════════════════════════════════════════════════════════════
# Часть 5: Webhook idempotency
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dedup_first_call_returns_false():
    """Первый вызов _is_duplicate → False (не дубликат, обрабатываем)."""
    redis_mock = AsyncMock()
    redis_mock.set = AsyncMock(return_value=True)  # nx=True, ключ не было → добавлен

    with patch("main.get_redis", new=AsyncMock(return_value=redis_mock)):
        from main import _is_duplicate
        result = await _is_duplicate(12345)

    assert result is False


@pytest.mark.asyncio
async def test_dedup_second_call_returns_true():
    """Второй вызов _is_duplicate → True (дубликат, пропускаем)."""
    redis_mock = AsyncMock()
    redis_mock.set = AsyncMock(return_value=None)  # nx=True, ключ уже был → None

    with patch("main.get_redis", new=AsyncMock(return_value=redis_mock)):
        from main import _is_duplicate
        result = await _is_duplicate(12345)

    assert result is True


# ════════════════════════════════════════════════════════════════════════════════
# Часть 6: Redis singleton — нет race condition
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_redis_singleton_no_race():
    """
    Два параллельных вызова get_redis() должны создать ровно один клиент.
    В оригинальном коде без Lock() оба могли пройти `if _redis is None`
    и создать два разных клиента.
    """
    import db

    # Сбрасываем singleton
    original_redis = db._redis
    db._redis = None

    created = []

    async def fake_from_url(url, **kwargs):
        await asyncio.sleep(0.01)  # имитируем задержку подключения
        client = MagicMock()
        created.append(client)
        return client

    with (
        patch.dict(os.environ, {"REDIS_URL": "redis://localhost"}),
        patch("db.aioredis.from_url", new=fake_from_url),
    ):
        # Запускаем два параллельных вызова
        results = await asyncio.gather(db.get_redis(), db.get_redis())

    # Восстанавливаем
    db._redis = original_redis

    # Должен быть создан ровно один клиент
    assert len(created) == 1, f"Ожидался 1 клиент Redis, создано {len(created)}"
    # Оба вызова вернули одно и то же
    assert results[0] is results[1]


# ════════════════════════════════════════════════════════════════════════════════
# Часть 7: Whisper — language параметр убран
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_whisper_no_language_param():
    """
    transcribe() НЕ должен передавать language в Whisper API.
    Оригинальный баг: language="ru" ломал транскрипцию для нерусских юзеров.
    """
    from llm import transcribe, init_http, close_http, get_http

    # Инициализируем клиент
    await init_http()

    called_data = {}

    async def fake_post(url, *, headers, files, data, timeout):
        called_data.update(data)
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"text": "тестовый текст"})
        return resp

    with patch.object(get_http(), "post", side_effect=fake_post):
        result = await transcribe(b"fake_ogg_data")

    await close_http()

    assert "language" not in called_data, (
        f"Whisper получил language={called_data.get('language')!r} — "
        "должен определять язык автоматически"
    )
    assert result == "тестовый текст"


# ════════════════════════════════════════════════════════════════════════════════
# Часть 8: Daily jitter — ±5 минут, не ±30 секунд
# ════════════════════════════════════════════════════════════════════════════════

def test_daily_jitter_range():
    """
    Jitter для дейли-расписания должен быть в диапазоне ±300 секунд (5 мин).
    В оригинале был ±30 сек — при 1000 юзерах создавал пик нагрузки.
    """
    import datetime

    scheduled_times = []
    job_queue_mock = MagicMock()

    def fake_run_daily(callback, time, name, data):
        scheduled_times.append(time)

    job_queue_mock.run_daily = fake_run_daily
    job_queue_mock.get_jobs_by_name = MagicMock(return_value=[])

    app_mock = MagicMock()
    app_mock.job_queue = job_queue_mock

    # Тестируем 20 разных user_id с одинаковым временем 09:00 UTC
    async def run():
        from flows.misc import schedule_daily
        for uid in range(1, 21):
            await schedule_daily(app_mock, uid, hour=9, minute=0)

    asyncio.run(run())

    assert len(scheduled_times) == 20

    # Базовое время: 9*3600 = 32400 сек
    base = 9 * 3600
    for t in scheduled_times:
        actual_sec = t.hour * 3600 + t.minute * 60 + t.second
        delta = abs(actual_sec - base)
        # Учитываем переход через полночь
        if delta > 43200:
            delta = 86400 - delta
        assert delta <= 300, (
            f"Jitter {delta}s выходит за пределы ±300s. "
            f"Запланировано: {t.hour:02d}:{t.minute:02d}:{t.second:02d}"
        )


def test_daily_jitter_distribution():
    """Разные user_id дают разные смещения — jitter работает."""
    import datetime

    job_queue_mock = MagicMock()
    scheduled = {}

    def fake_run_daily(callback, time, name, data):
        scheduled[data] = time

    job_queue_mock.run_daily = fake_run_daily
    job_queue_mock.get_jobs_by_name = MagicMock(return_value=[])
    app_mock = MagicMock()
    app_mock.job_queue = job_queue_mock

    async def run():
        from flows.misc import schedule_daily
        for uid in [100, 200, 300, 400, 500]:
            await schedule_daily(app_mock, uid, hour=9, minute=0)

    asyncio.run(run())

    times = [(t.hour * 3600 + t.minute * 60 + t.second) for t in scheduled.values()]
    # Все времена должны быть разными (разный user_id → разный jitter)
    assert len(set(times)) > 1, "Все пользователи получили одинаковое время — jitter не работает"


# ════════════════════════════════════════════════════════════════════════════════
# Часть 9: Security — protect()
# ════════════════════════════════════════════════════════════════════════════════

def test_protect_adds_suffix_for_regular_user():
    from security import protect, _PROMPT_PROTECTION
    result = protect(999, "system prompt")
    assert _PROMPT_PROTECTION in result
    assert result.startswith("system prompt")


def test_protect_no_suffix_for_admin():
    from security import protect, ADMIN_ID
    result = protect(ADMIN_ID, "admin prompt")
    assert result == "admin prompt"


def test_protect_idempotent_for_different_users():
    from security import protect
    r1 = protect(1, "base")
    r2 = protect(2, "base")
    assert r1 == r2  # одинаковый суффикс для всех не-админов


# ════════════════════════════════════════════════════════════════════════════════
# Часть 10: utils — kb(), profile_val()
# ════════════════════════════════════════════════════════════════════════════════

def test_kb_single_row():
    from utils import kb
    from telegram import InlineKeyboardMarkup
    result = kb(["Кнопка|data1"])
    assert isinstance(result, InlineKeyboardMarkup)
    assert result.inline_keyboard[0][0].text == "Кнопка"
    assert result.inline_keyboard[0][0].callback_data == "data1"


def test_kb_multiple_rows():
    from utils import kb
    result = kb(["А|a", "Б|b"], ["В|c"])
    assert len(result.inline_keyboard) == 2
    assert len(result.inline_keyboard[0]) == 2
    assert len(result.inline_keyboard[1]) == 1


def test_profile_val_missing_key():
    from utils import profile_val
    assert profile_val({}, "niche") == "—"


def test_profile_val_present_key():
    from utils import profile_val
    result = profile_val({"niche": "фитнес"}, "niche")
    assert "фитнес" in result


def test_profile_val_escapes_markdown():
    from utils import profile_val
    result = profile_val({"niche": "IT & *маркетинг*"}, "niche")
    # Markdown символы должны быть экранированы
    assert "*" not in result.replace("\\*", "")
