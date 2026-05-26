"""
db.py — хранилище данных.

KV (профиль, сессии, настройки)  → Redis   (переживает рестарты)
History + Results                 → Postgres (asyncpg, connection pool)

Изменения v2:
- Redis singleton защищён asyncio.Lock → нет race condition при параллельном старте
- get_redis() инициализирует клиент безопасно
"""
import asyncio
import logging
import json
import os

import asyncpg
import redis.asyncio as aioredis

from config import MAX_HISTORY, DEFAULT_MODEL

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Postgres pool
# ══════════════════════════════════════════════════════════════════════════════

_pool: asyncpg.Pool | None = None
DATABASE_URL = os.environ.get("DATABASE_URL", "")


async def init_db() -> None:
    global _pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан в переменных окружения")

    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    _pool = await asyncpg.create_pool(
        url,
        min_size=2,
        max_size=10,
        command_timeout=30,
        statement_cache_size=0,
        ssl="require",
    )

    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id        BIGSERIAL PRIMARY KEY,
                user_id   BIGINT  NOT NULL,
                role      TEXT    NOT NULL,
                content   TEXT    NOT NULL,
                model     TEXT    NOT NULL,
                mode      TEXT    NOT NULL DEFAULT 'chat',
                ts        TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_msg
                ON messages (user_id, model, mode, id)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                agent_key  TEXT   NOT NULL,
                agent_name TEXT   NOT NULL,
                content    TEXT   NOT NULL,
                ts         TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_results
                ON results (user_id, ts DESC)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS voice_signals (
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT      NOT NULL,
                kind       TEXT        NOT NULL,
                agent_key  TEXT        NOT NULL DEFAULT '',
                content    TEXT        NOT NULL,
                ts         TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_voice_signals_user_kind
                ON voice_signals (user_id, kind, ts DESC)
        """)

    logger.info("Postgres pool ready")

    from lava_payments import init_payments_db
    await init_payments_db()

    from retention import init_push_db
    await init_push_db()


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Postgres pool closed")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_db() first")
    return _pool


# ══════════════════════════════════════════════════════════════════════════════
# Redis setup — защищённая инициализация (fix: race condition)
# ══════════════════════════════════════════════════════════════════════════════

_REDIS_URL = os.environ.get("REDIS_URL", "")
_redis: aioredis.Redis | None = None
_redis_lock = asyncio.Lock()


async def get_redis() -> aioredis.Redis:
    """
    Thread-safe singleton.
    Предыдущая версия имела race condition: при двух параллельных первых вызовах
    оба видели _redis is None и создавали два клиента. Lock это исключает.
    """
    global _redis
    if _redis is not None:
        return _redis
    async with _redis_lock:
        # Двойная проверка после захвата лока
        if _redis is not None:
            return _redis
        if not _REDIS_URL:
            raise RuntimeError("REDIS_URL не задан в переменных окружения")
        _redis = aioredis.from_url(
            _REDIS_URL,
            decode_responses=True,
            max_connections=50,
        )
        logger.info("Redis client initialised")
        return _redis


def _rkey(user_id: int, key: str) -> str:
    return f"bot:kv:{user_id}:{key}"


# ══════════════════════════════════════════════════════════════════════════════
# KV helpers — Redis
# ══════════════════════════════════════════════════════════════════════════════

async def kv_get(user_id: int, key: str) -> str | None:
    r = await get_redis()
    return await r.get(_rkey(user_id, key))


async def kv_set(user_id: int, key: str, value: str, ttl: int | None = None) -> None:
    r = await get_redis()
    if ttl:
        await r.set(_rkey(user_id, key), value, ex=ttl)
    else:
        await r.set(_rkey(user_id, key), value)


async def kv_del(user_id: int, key: str) -> None:
    r = await get_redis()
    await r.delete(_rkey(user_id, key))


async def kv_keys_matching(user_id: int, pattern: str) -> list[str]:
    """SCAN — O(keyspace). Используй только для служебных операций, не на горячем пути."""
    r = await get_redis()
    prefix = f"bot:kv:{user_id}:"
    full_pattern = f"{prefix}{pattern}"
    keys = []
    async for k in r.scan_iter(full_pattern):
        keys.append(k[len(prefix):])
    return keys


# ══════════════════════════════════════════════════════════════════════════════
# Profile
# ══════════════════════════════════════════════════════════════════════════════

async def get_profile(user_id: int) -> dict:
    raw = await kv_get(user_id, "__profile__")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


async def save_profile(user_id: int, profile: dict) -> None:
    await kv_set(user_id, "__profile__", json.dumps(profile, ensure_ascii=False))


async def get_user_name(user_id: int) -> str:
    """
    Возвращает имя пользователя из профиля.
    Пустая строка если не сохранено.
    Используй везде где Мира обращается к пользователю лично.
    """
    try:
        p = await get_profile(user_id)
        return p.get("first_name", "")
    except Exception:
        return ""


async def is_onboarded(user_id: int) -> bool:
    p = await get_profile(user_id)
    return bool(p.get("onboarded"))


# ══════════════════════════════════════════════════════════════════════════════
# Onboarding state
# ══════════════════════════════════════════════════════════════════════════════

async def get_onboarding_state(user_id: int) -> dict | None:
    raw = await kv_get(user_id, "__onboarding__")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


async def save_onboarding_state(user_id: int, state: dict) -> None:
    await kv_set(user_id, "__onboarding__", json.dumps(state, ensure_ascii=False))


async def clear_onboarding_state(user_id: int) -> None:
    await kv_del(user_id, "__onboarding__")


# ══════════════════════════════════════════════════════════════════════════════
# Model preference
# ══════════════════════════════════════════════════════════════════════════════

async def get_model(user_id: int) -> str:
    raw = await kv_get(user_id, "__model__")
    return raw if raw else DEFAULT_MODEL


async def set_model(user_id: int, model_key: str) -> None:
    await kv_set(user_id, "__model__", model_key)


# ══════════════════════════════════════════════════════════════════════════════
# Chat history — Postgres
# ══════════════════════════════════════════════════════════════════════════════

async def get_history(user_id: int, model: str, mode: str = "chat") -> list:
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content FROM messages
            WHERE user_id=$1 AND model=$2 AND mode=$3
            ORDER BY id DESC LIMIT $4
            """,
            user_id, model, mode, MAX_HISTORY,
        )
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    return history


async def save_message(user_id: int, role: str, content: str,
                       model: str, mode: str = "chat") -> None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages (user_id, role, content, model, mode) VALUES ($1,$2,$3,$4,$5)",
            user_id, role, content, model, mode,
        )


async def clear_history(user_id: int) -> None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE user_id=$1", user_id)


# ══════════════════════════════════════════════════════════════════════════════
# Agent sessions — Redis
# ══════════════════════════════════════════════════════════════════════════════

def _session_key(user_id: int, agent_key: str) -> str:
    return f"__agent__{agent_key}__"


async def get_agent_session(user_id: int, key: str) -> dict | None:
    raw = await kv_get(user_id, _session_key(user_id, key))
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


async def save_agent_session(user_id: int, key: str, data: dict) -> None:
    await kv_set(user_id, _session_key(user_id, key),
                 json.dumps(data, ensure_ascii=False))


async def clear_agent_session(user_id: int, key: str) -> None:
    await kv_del(user_id, _session_key(user_id, key))


async def clear_all_agent_sessions(user_id: int) -> None:
    """Очищает все агент-сессии. Вызывать только при старте НОВОГО агента."""
    await kv_del(user_id, "__active_agent__")
    keys = await kv_keys_matching(user_id, "__agent__*__")
    r = await get_redis()
    for k in keys:
        await r.delete(_rkey(user_id, k))


# ══════════════════════════════════════════════════════════════════════════════
# Results library — Postgres
# ══════════════════════════════════════════════════════════════════════════════

async def save_result(user_id: int, agent_key: str,
                      agent_name: str, content: str) -> int:
    """Сохраняет результат, возвращает ID для followup."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        result_id = await conn.fetchval(
            """INSERT INTO results (user_id, agent_key, agent_name, content)
               VALUES ($1, $2, $3, $4) RETURNING id""",
            user_id, agent_key, agent_name, content,
        )
    return result_id or 0
    # Примечание: update_streak_on_result вызывается из agents._send_result()
    # чтобы избежать circular import db → ui.home → db


async def get_results(user_id: int, limit: int = 50) -> list[dict]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, agent_key, agent_name, content, ts::text FROM results "
            "WHERE user_id=$1 ORDER BY ts DESC LIMIT $2",
            user_id, limit,
        )
    return [dict(r) for r in rows]


async def get_result_by_id(user_id: int, result_id: int) -> dict | None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, agent_key, agent_name, content, ts::text FROM results "
            "WHERE user_id=$1 AND id=$2",
            user_id, result_id,
        )
    return dict(row) if row else None


async def delete_result(user_id: int, result_id: int) -> None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM results WHERE user_id=$1 AND id=$2",
            user_id, result_id,
        )


async def get_stats(user_id: int) -> dict:
    pool = _get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM results WHERE user_id=$1", user_id
        )
        by_agent = await conn.fetch(
            "SELECT agent_name, COUNT(*) as cnt FROM results "
            "WHERE user_id=$1 GROUP BY agent_name ORDER BY cnt DESC",
            user_id,
        )
    return {
        "total":    total or 0,
        "by_agent": [(r["agent_name"], r["cnt"]) for r in by_agent],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Style examples — Redis (до 10 постов)
# ══════════════════════════════════════════════════════════════════════════════

async def get_style_examples(user_id: int) -> list[str]:
    raw = await kv_get(user_id, "__style_examples__")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return []


async def add_style_example(user_id: int, text: str) -> int:
    examples = await get_style_examples(user_id)
    if len(examples) >= 10:
        examples.pop(0)
    examples.append(text[:3000])
    await kv_set(user_id, "__style_examples__", json.dumps(examples, ensure_ascii=False))
    return len(examples)


async def clear_style_examples(user_id: int) -> None:
    await kv_del(user_id, "__style_examples__")


# ══════════════════════════════════════════════════════════════════════════════
# Schedule / Planner — Redis
# ══════════════════════════════════════════════════════════════════════════════

async def get_schedule(user_id: int) -> list:
    raw = await kv_get(user_id, "__schedule__")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return []


async def save_schedule(user_id: int, schedule: list) -> None:
    await kv_set(user_id, "__schedule__", json.dumps(schedule, ensure_ascii=False))


async def add_to_schedule(user_id: int, date: str, platform: str, idea: str) -> None:
    schedule = await get_schedule(user_id)
    schedule.append({"date": date, "platform": platform, "idea": idea, "done": False})
    await save_schedule(user_id, schedule)


async def mark_done(user_id: int, idx: int) -> None:
    schedule = await get_schedule(user_id)
    if 0 <= idx < len(schedule):
        schedule[idx]["done"] = True
    await save_schedule(user_id, schedule)


# ══════════════════════════════════════════════════════════════════════════════
# Daily briefing settings — Redis
# ══════════════════════════════════════════════════════════════════════════════

async def get_daily_settings(user_id: int) -> dict:
    raw = await kv_get(user_id, "__daily__")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"enabled": False, "hour": 9, "minute": 0}


async def save_daily_settings(user_id: int, settings: dict) -> None:
    await kv_set(user_id, "__daily__", json.dumps(settings, ensure_ascii=False))


async def get_all_daily_users() -> list:
    r = await get_redis()
    result = []
    async for key in r.scan_iter("bot:kv:*:__daily__"):
        try:
            raw = await r.get(key)
            if not raw:
                continue
            s = json.loads(raw)
            if s.get("enabled"):
                parts = key.split(":")
                user_id = int(parts[2])
                result.append((user_id, s))
        except Exception as e:
            logger.warning(f"get_all_daily_users key={key} error: {e}")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Misc helpers
# ══════════════════════════════════════════════════════════════════════════════

async def clear_all(user_id: int) -> None:
    """Очищает историю чата и все сессии. НЕ трогает профиль и подписку."""
    await clear_history(user_id)
    await clear_all_agent_sessions(user_id)


# ══════════════════════════════════════════════════════════════════════════════
# Creator Intelligence Profile (CIP) — живая стратегическая память
# ══════════════════════════════════════════════════════════════════════════════

async def get_cip(user_id: int) -> dict:
    """
    Возвращает Creator Intelligence Profile — живую стратегическую память бота.
    Содержит: recent_topics, hooks_that_worked, current_funnel_phase,
    audience_trust_level, content_backlog и другие стратегические поля.
    """
    raw = await kv_get(user_id, "__cip__")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {
        "recent_topics": [],
        "hooks_that_worked": [],
        "hooks_that_failed": [],
        "current_funnel_phase": "awareness",   # awareness / consideration / conversion / retention
        "audience_trust_level": 5,             # 1-10
        "audience_primary_objection": "",
        "audience_sophistication": "medium",   # low / medium / high
        "active_launch": False,
        "content_backlog": [],
        "positioning_statement": "",
        "content_format_affinity": {},         # {"carousel": 0.8, "reels": 0.6}
        "emotional_rhythm": [],                # последние 10 эмоций контента
        "warmup_neutralized_objections": [],   # возражения нейтрализованные в последнем прогреве
    }


async def save_cip(user_id: int, cip: dict) -> None:
    await kv_set(user_id, "__cip__", json.dumps(cip, ensure_ascii=False))


async def update_cip(user_id: int, **fields) -> dict:
    """Частичное обновление CIP. Возвращает обновлённый объект."""
    cip = await get_cip(user_id)
    cip.update(fields)
    await save_cip(user_id, cip)
    return cip


async def add_recent_topic(user_id: int, topic: str) -> None:
    """Добавляет тему в recent_topics (хранит последние 20)."""
    cip = await get_cip(user_id)
    topics = cip.get("recent_topics", [])
    if topic and topic not in topics:
        topics.append(topic)
    cip["recent_topics"] = topics[-20:]
    await save_cip(user_id, cip)


async def add_hook_feedback(user_id: int, hook: str, worked: bool) -> None:
    """Записывает обратную связь по хуку в CIP."""
    cip = await get_cip(user_id)
    key = "hooks_that_worked" if worked else "hooks_that_failed"
    lst = cip.get(key, [])
    lst.append(hook[:200])
    cip[key] = lst[-30:]
    await save_cip(user_id, cip)


# ══════════════════════════════════════════════════════════════════════════════
# Content Backlog — банк идей из Brainstorm для Planner
# ══════════════════════════════════════════════════════════════════════════════

async def get_content_backlog(user_id: int) -> list:
    """Возвращает банк контент-идей."""
    cip = await get_cip(user_id)
    return cip.get("content_backlog", [])


async def add_to_backlog(user_id: int, idea: str, format_type: str = "") -> int:
    """
    Добавляет идею в контент-бэклог.
    Возвращает новый размер бэклога.
    """
    cip = await get_cip(user_id)
    backlog = cip.get("content_backlog", [])
    entry = {"idea": idea[:300], "format": format_type, "used": False}
    # Не дублируем идентичные идеи
    if not any(b["idea"] == entry["idea"] for b in backlog):
        backlog.append(entry)
    cip["content_backlog"] = backlog[-50:]  # максимум 50 идей
    await save_cip(user_id, cip)
    return len(backlog)


async def mark_backlog_used(user_id: int, idea: str) -> None:
    """Помечает идею из бэклога как использованную."""
    cip = await get_cip(user_id)
    for entry in cip.get("content_backlog", []):
        if entry.get("idea") == idea:
            entry["used"] = True
    await save_cip(user_id, cip)


async def clear_content_backlog(user_id: int) -> None:
    cip = await get_cip(user_id)
    cip["content_backlog"] = []
    await save_cip(user_id, cip)


def build_profile_ctx(profile: dict) -> str:
    parts = []
    if profile.get("niche"):    parts.append(f"\nНиша автора: {profile['niche']}")
    if profile.get("audience"): parts.append(f"Аудитория: {profile['audience']}")
    if profile.get("tone"):     parts.append(f"Тон голоса: {profile['tone']}")
    return "\n".join(parts)
