"""
api_client.py — HTTP-клиент для Python Telegram-бота.

Бот больше не содержит бизнес-логику — он только:
1. Аутентифицирует пользователя через /auth/bot
2. Вызывает /generate, /refine, /materials/* через этот клиент
3. Рендерит ответы в Telegram

Все кэши JWT хранятся в Redis.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE      = os.environ.get("MIRA_API_URL", "http://localhost:4000/api/v1")
BOT_SECRET    = os.environ.get("BOT_API_SECRET", "")
_JWT_TTL      = 28 * 24 * 3600   # 28 дней — чуть меньше срока JWT
_CLIENT: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _CLIENT


async def close_client() -> None:
    global _CLIENT
    if _CLIENT:
        await _CLIENT.aclose()
        _CLIENT = None


# ── Auth ───────────────────────────────────────────────────────────────────────

async def get_jwt(user_id: int, telegram_id: int) -> str:
    """Возвращает JWT из кэша Redis или запрашивает новый."""
    from db import kv_get, kv_set
    cached = await kv_get(user_id, "__api_jwt__")
    if cached:
        return cached

    resp = await get_client().post(
        "/auth/bot",
        json={"telegramId": telegram_id},
        headers={"x-bot-secret": BOT_SECRET},
    )
    resp.raise_for_status()
    token = resp.json()["data"]["token"]
    await kv_set(user_id, "__api_jwt__", token, ttl=_JWT_TTL)
    return token


def _headers(jwt: str) -> dict:
    return {"Authorization": f"Bearer {jwt}"}


# ── Generation ─────────────────────────────────────────────────────────────────

async def generate(
    user_id: int,
    telegram_id: int,
    tool_key: str,
    topic: str | None = None,
    description: str | None = None,
) -> dict:
    """Создаёт новую генерацию."""
    jwt = await get_jwt(user_id, telegram_id)
    resp = await get_client().post(
        "/generate",
        json={
            "toolKey": tool_key,
            "input": {
                "topic":       topic,
                "description": description,
            },
        },
        headers=_headers(jwt),
    )
    if resp.status_code == 409:
        raise RuntimeError("already_generating")
    if resp.status_code == 402:
        raise PermissionError("subscription_required")
    resp.raise_for_status()
    return resp.json()["data"]


async def regenerate(user_id: int, telegram_id: int, generation_id: int) -> dict:
    """PHASE 9: регенерация по конкретному generation_id."""
    jwt = await get_jwt(user_id, telegram_id)
    resp = await get_client().post(
        f"/generate/{generation_id}/regen",
        headers=_headers(jwt),
    )
    resp.raise_for_status()
    return resp.json()["data"]


# ── Refinement ─────────────────────────────────────────────────────────────────

async def refine(
    user_id: int,
    telegram_id: int,
    generation_id: int,
    action: str,
    metadata: dict | None = None,
) -> dict:
    """Применяет правку к generation."""
    jwt = await get_jwt(user_id, telegram_id)
    resp = await get_client().post(
        "/refine",
        json={
            "generationId": generation_id,
            "action":       action,
            "metadata":     metadata or {},
        },
        headers=_headers(jwt),
    )
    if resp.status_code == 409:
        code = resp.json().get("code", "")
        if code == "ALREADY_COMPLETED":
            raise ValueError("already_completed")
        raise RuntimeError("already_generating")
    resp.raise_for_status()
    return resp.json()["data"]


# ── Materials ──────────────────────────────────────────────────────────────────

async def save_material(
    user_id: int,
    telegram_id: int,
    generation_id: int,
    title: str | None = None,
) -> dict:
    jwt = await get_jwt(user_id, telegram_id)
    resp = await get_client().post(
        "/materials/save",
        json={"generationId": generation_id, "title": title},
        headers=_headers(jwt),
    )
    resp.raise_for_status()
    return resp.json()["data"]


async def list_materials(
    user_id: int,
    telegram_id: int,
    page: int = 0,
    limit: int = 20,
    tool_key: str | None = None,
) -> dict:
    jwt = await get_jwt(user_id, telegram_id)
    params: dict[str, Any] = {"page": page, "limit": limit}
    if tool_key:
        params["toolKey"] = tool_key
    resp = await get_client().get(
        "/materials",
        params=params,
        headers=_headers(jwt),
    )
    resp.raise_for_status()
    return resp.json()["data"]


# ── Voice feedback ─────────────────────────────────────────────────────────────

async def voice_feedback(
    user_id: int,
    telegram_id: int,
    generation_id: int,
    signal: str,            # "approved" | "rejected"
    note: str | None = None,
) -> None:
    jwt = await get_jwt(user_id, telegram_id)
    resp = await get_client().post(
        "/voice/feedback",
        json={"generationId": generation_id, "signal": signal, "note": note},
        headers=_headers(jwt),
    )
    resp.raise_for_status()


# ── User ───────────────────────────────────────────────────────────────────────

async def get_user_state(user_id: int, telegram_id: int) -> str:
    """Возвращает state пользователя: new / onboarded / trial / subscribed / expired."""
    jwt = await get_jwt(user_id, telegram_id)
    resp = await get_client().get("/auth/me", headers=_headers(jwt))
    resp.raise_for_status()
    return resp.json()["data"]["state"]


async def activate_trial(user_id: int, telegram_id: int) -> None:
    jwt = await get_jwt(user_id, telegram_id)
    resp = await get_client().post("/subscription/trial", headers=_headers(jwt))
    if resp.status_code == 409:
        raise ValueError("trial_already_used")
    resp.raise_for_status()
