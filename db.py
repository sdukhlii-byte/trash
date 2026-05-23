"""
db.py — хранилище данных.

KV (профиль, сессии, настройки)  → Redis   (переживает рестарты)
History + Results                 → Postgres (asyncpg, connection pool)
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
    """Создаёт пул соединений и инициализирует таблицы."""
    global _pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан в переменных окружения")

    # Railway отдаёт URL с postgres://, asyncpg требует postgresql://
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    _pool = await asyncpg.create_pool(
        url,
        min_size=2,
        max_size=10,
        command_timeout=30,
        statement_cache_size=0,   # нужно для PgBouncer / Railway proxy
        ssl="require",            # Railway Postgres требует SSL
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

    logger.info("Postgres pool ready")

    # Инициализируем таблицы платёжной системы
    from lava_payments import init_payments_db
    await init_payments_db()

    # Инициализируем таблицу пушей
    from retention import init_push_db
    await init_push_db()


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Postgres pool closed")


def _get_pool() -> asyncpg.Pool:
    assert _pool is not None, "DB pool not initialised — call init_db() first"
    return _pool


# ══════════════════════════════════════════════════════════════════════════════
# Redis setup
# ══════════════════════════════════════════════════════════════════════════════

_REDIS_URL = os.environ.get("REDIS_URL", "")
_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        if not _REDIS_URL:
            raise RuntimeError("REDIS_URL не задан в переменных окружения")
        _redis = aioredis.from_url(_REDIS_URL, decode_responses=True)
    return _redis


def _rkey(user_id: int, key: str) -> str:
    return f"bot:kv:{user_id}:{key}"


# ══════════════════════════════════════════════════════════════════════════════
# KV helpers — Redis
# ══════════════════════════════════════════════════════════════════════════════

async def kv_get(user_id: int, key: str) -> str | None:
    r = await get_redis()
    return await r.get(_rkey(user_id, key))


async def kv_set(user_id: int, key: str, value: str) -> None:
    r = await get_redis()
    await r.set(_rkey(user_id, key), value)


async def kv_del(user_id: int, key: str) -> None:
    r = await get_redis()
    await r.delete(_rkey(user_id, key))


async def kv_keys_matching(user_id: int, pattern: str) -> list[str]:
    r = await get_redis()
    prefix = f"bot:kv:{user_id}:"
    full_pattern = f"{prefix}{pattern}"
    keys = []
    async for k in r.scan_iter(full_pattern):
        keys.append(k[len(prefix):])
    return keys


# ══════════════════════════════════════════════════════════════════════════════
# Profile (Redis)
# ══════════════════════════════════════════════════════════════════════════════

async def get_profile(user_id: int) -> dict:
    raw = await kv_get(user_id, "__profile__")
    if raw:
        try: return json.loads(raw)
        except: pass
    return {"niche": "", "audience": "", "tone": "", "onboarded": False}


async def save_profile(user_id: int, data: dict) -> None:
    await kv_set(user_id, "__profile__", json.dumps(data, ensure_ascii=False))


async def is_onboarded(user_id: int) -> bool:
    p = await get_profile(user_id)
    return bool(p.get("onboarded"))


def build_profile_ctx(profile: dict) -> str:
    parts = []
    if profile.get("niche"):    parts.append(f"Ниша: {profile['niche']}")
    if profile.get("audience"): parts.append(f"Аудитория: {profile['audience']}")
    if profile.get("tone"):     parts.append(f"Тон: {profile['tone']}")
    return ("\n[Профиль]\n" + "\n".join(parts)) if parts else ""


# ══════════════════════════════════════════════════════════════════════════════
# Model (Redis)
# ══════════════════════════════════════════════════════════════════════════════

async def get_model(user_id: int) -> str:
    v = await kv_get(user_id, "__model__")
    return v or DEFAULT_MODEL


async def set_model(user_id: int, v: str) -> None:
    await kv_set(user_id, "__model__", v)


# ══════════════════════════════════════════════════════════════════════════════
# History (Postgres)
# ══════════════════════════════════════════════════════════════════════════════

async def save_message(user_id: int, role: str, content: str,
                       model: str, mode: str = "chat") -> None:
    async with _get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(user_id, role, content, model, mode) "
            "VALUES($1, $2, $3, $4, $5)",
            user_id, role, content, model, mode,
        )


async def get_history(user_id: int, model: str, mode: str = "chat") -> list:
    async with _get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, content FROM messages "
            "WHERE user_id=$1 AND model=$2 AND mode=$3 "
            "ORDER BY id DESC LIMIT $4",
            user_id, model, mode, MAX_HISTORY,
        )
    msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    while msgs and msgs[0]["role"] == "assistant":
        msgs = msgs[1:]
    return msgs


async def clear_history(user_id: int) -> None:
    async with _get_pool().acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE user_id=$1", user_id)


# ══════════════════════════════════════════════════════════════════════════════
# Agent sessions (Redis)
# ══════════════════════════════════════════════════════════════════════════════

def _agent_key(agent: str) -> str:
    return f"__agent__{agent}__"


async def get_agent_session(user_id: int, agent: str) -> dict | None:
    raw = await kv_get(user_id, _agent_key(agent))
    if raw:
        try: return json.loads(raw)
        except: pass
    return None


async def save_agent_session(user_id: int, agent: str, state: dict) -> None:
    await kv_set(user_id, _agent_key(agent), json.dumps(state, ensure_ascii=False))


async def clear_agent_session(user_id: int, agent: str) -> None:
    await kv_del(user_id, _agent_key(agent))


async def get_active_agent(user_id: int) -> str | None:
    keys = await kv_keys_matching(user_id, "__agent__*__")
    if keys:
        k = keys[0]
        return k[len("__agent__"):-len("__")]
    return None


async def clear_all_agent_sessions(user_id: int) -> None:
    keys = await kv_keys_matching(user_id, "__agent__*__")
    if keys:
        r = await get_redis()
        prefix = f"bot:kv:{user_id}:"
        await r.delete(*[prefix + k for k in keys])


async def clear_all(user_id: int) -> None:
    await clear_history(user_id)
    await clear_all_agent_sessions(user_id)


# ══════════════════════════════════════════════════════════════════════════════
# Onboarding (Redis)
# ══════════════════════════════════════════════════════════════════════════════

async def get_onboarding_state(user_id: int) -> dict | None:
    raw = await kv_get(user_id, "__onboarding__")
    if raw:
        try: return json.loads(raw)
        except: pass
    return None


async def save_onboarding_state(user_id: int, state: dict) -> None:
    await kv_set(user_id, "__onboarding__", json.dumps(state, ensure_ascii=False))


async def clear_onboarding_state(user_id: int) -> None:
    await kv_del(user_id, "__onboarding__")


# ══════════════════════════════════════════════════════════════════════════════
# Results / library (Postgres)
# ══════════════════════════════════════════════════════════════════════════════

async def save_result(user_id: int, agent_key: str, agent_name: str, content: str) -> int:
    async with _get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO results(user_id, agent_key, agent_name, content) "
            "VALUES($1, $2, $3, $4) RETURNING id",
            user_id, agent_key, agent_name, content,
        )
    return row["id"]


async def get_results(user_id: int, limit: int = 20) -> list:
    async with _get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, agent_key, agent_name, content, ts FROM results "
            "WHERE user_id=$1 ORDER BY ts DESC LIMIT $2",
            user_id, limit,
        )
    return [{"id": r["id"], "agent_key": r["agent_key"], "agent_name": r["agent_name"],
             "content": r["content"], "ts": str(r["ts"])} for r in rows]


async def get_result_by_id(user_id: int, result_id: int) -> dict | None:
    async with _get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, agent_key, agent_name, content, ts FROM results "
            "WHERE user_id=$1 AND id=$2",
            user_id, result_id,
        )
    if row:
        return {"id": row["id"], "agent_key": row["agent_key"],
                "agent_name": row["agent_name"], "content": row["content"],
                "ts": str(row["ts"])}
    return None


async def get_stats(user_id: int) -> dict:
    async with _get_pool().acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM results WHERE user_id=$1", user_id
        )
        rows = await conn.fetch(
            "SELECT agent_name, COUNT(*) as cnt FROM results "
            "WHERE user_id=$1 GROUP BY agent_key, agent_name ORDER BY cnt DESC",
            user_id,
        )
    by_agent = [(r["agent_name"], r["cnt"]) for r in rows]
    return {"total": total or 0, "by_agent": by_agent}


async def delete_result(user_id: int, result_id: int) -> None:
    async with _get_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM results WHERE user_id=$1 AND id=$2",
            user_id, result_id,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Style examples (Redis)
# ══════════════════════════════════════════════════════════════════════════════

async def get_style_examples(user_id: int) -> list:
    raw = await kv_get(user_id, "__style_examples__")
    if raw:
        try: return json.loads(raw)
        except: pass
    return []


async def save_style_examples(user_id: int, examples: list) -> None:
    await kv_set(user_id, "__style_examples__", json.dumps(examples, ensure_ascii=False))


async def add_style_example(user_id: int, text: str) -> int:
    examples = await get_style_examples(user_id)
    examples.append(text)
    if len(examples) > 10:
        examples = examples[-10:]
    await save_style_examples(user_id, examples)
    return len(examples)


async def clear_style_examples(user_id: int) -> None:
    await kv_del(user_id, "__style_examples__")


# ══════════════════════════════════════════════════════════════════════════════
# Schedule (Redis)
# ══════════════════════════════════════════════════════════════════════════════

async def get_schedule(user_id: int) -> list:
    raw = await kv_get(user_id, "__schedule__")
    if raw:
        try: return json.loads(raw)
        except: pass
    return []


async def save_schedule(user_id: int, schedule: list) -> None:
    await kv_set(user_id, "__schedule__", json.dumps(schedule, ensure_ascii=False))


async def add_to_schedule(user_id: int, date: str, platform: str, idea: str) -> None:
    schedule = await get_schedule(user_id)
    schedule.append({"date": date, "platform": platform, "idea": idea, "done": False})
    schedule.sort(key=lambda x: x["date"])
    await save_schedule(user_id, schedule)


async def mark_done(user_id: int, idx: int) -> None:
    schedule = await get_schedule(user_id)
    if 0 <= idx < len(schedule):
        schedule[idx]["done"] = True
        await save_schedule(user_id, schedule)


async def remove_from_schedule(user_id: int, idx: int) -> None:
    schedule = await get_schedule(user_id)
    if 0 <= idx < len(schedule):
        schedule.pop(idx)
        await save_schedule(user_id, schedule)


# ══════════════════════════════════════════════════════════════════════════════
# Daily mode (Redis)
# ══════════════════════════════════════════════════════════════════════════════

async def get_daily_settings(user_id: int) -> dict:
    raw = await kv_get(user_id, "__daily__")
    if raw:
        try: return json.loads(raw)
        except: pass
    return {"enabled": False, "hour": 9, "minute": 0}


async def save_daily_settings(user_id: int, settings: dict) -> None:
    await kv_set(user_id, "__daily__", json.dumps(settings, ensure_ascii=False))


async def get_all_daily_users() -> list:
    r = await get_redis()
    result = []
    async for key in r.scan_iter("bot:kv:*:__daily__"):
        try:
            raw = await r.get(key)
            if not raw: continue
            s = json.loads(raw)
            if s.get("enabled"):
                parts = key.split(":")
                user_id = int(parts[2])
                result.append((user_id, s))
        except Exception as e:
            logger.warning(f"get_all_daily_users key={key} error: {e}")
    return result
