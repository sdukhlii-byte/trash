"""
callback_context.py — PHASE 1+2: scoped, state-safe callback system.

Every inline button MUST be created through build_callback().
Every callback MUST be resolved through resolve_callback()
and validated through validate_callback() before any action runs.

Format (inline, no DB):
    {prefix}:{result_id}:{kbv}      e.g.  rs:top5:182:3
Format (DB token, used when metadata is needed):
    cb:{token8}                      e.g.  cb:9f3a21b4

Both formats stay well under Telegram's 64-byte limit.
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_DEFAULT_TTL_HOURS = 48

# ── ACTION_REGISTRY ────────────────────────────────────────────────────────────
#
# repeatable     — True: кнопка остаётся после выполнения (softer, bolder, ...)
# marks_completed — True: действие записывается в completed_actions
# marks_completed + repeatable=False → кнопка исчезает после первого клика
# resets_completed — True: сбрасывает completed_actions у result (regen)
# allowed_tools  — None = любой инструмент; list = только указанные

ACTION_REGISTRY: dict[str, dict] = {
    # ── Reels ──────────────────────────────────────────────────────────────
    "rs:softer": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["reels_short"],
    },
    "rs:bolder": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["reels_short"],
    },
    "rs:top5": {
        "repeatable": False,
        "requires_result": True,
        "marks_completed": True,   # исчезает после выполнения
        "allowed_tools": ["reels_short"],
    },
    "rs:style": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["reels_short"],
    },
    "rs:regen": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "resets_completed": True,
        "allowed_tools": ["reels_short"],
    },
    "rs:pick_desc": {
        "repeatable": False,
        "requires_result": True,
        "marks_completed": True,
        "allowed_tools": ["reels_short"],
    },
    # ── Carousel ───────────────────────────────────────────────────────────
    "car:headline": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["carousel"],
    },
    "car:softer": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["carousel"],
    },
    "car:bolder": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["carousel"],
    },
    "car:shorten": {
        "repeatable": False,
        "requires_result": True,
        "marks_completed": True,
        "allowed_tools": ["carousel"],
    },
    "car:add_slide": {
        "repeatable": False,
        "requires_result": True,
        "marks_completed": True,
        "allowed_tools": ["carousel"],
    },
    "car:trigger": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["carousel"],
    },
    "car:format": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["carousel"],
    },
    # ── Generic agents ─────────────────────────────────────────────────────
    "ag:softer": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": None,
    },
    "ag:bolder": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": None,
    },
    "ag:shorter": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": None,
    },
    "ag:detail": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": None,
    },
    "ag:cta": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": None,
    },
    "ag:resonance": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["warmup"],
    },
    "ag:add_proof": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["warmup"],
    },
    "ag:mid_hook": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["stories"],
    },
    "ag:hook": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["talking_head", "cartoon", "post"],
    },
    "ag:deepen": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["profile"],
    },
    "ag:tactics": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["competitor"],
    },
    "ag:positioning": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["competitor"],
    },
    "ag:save_moment": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": ["carousel"],
    },
    # ── Universal ──────────────────────────────────────────────────────────
    "regen": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": None,
    },
    "refine": {
        "repeatable": True,
        "requires_result": True,
        "marks_completed": False,
        "allowed_tools": None,
    },
    "save": {
        "repeatable": False,
        "requires_result": True,
        "marks_completed": True,
        "allowed_tools": None,
    },
}

# ── Compact prefix map ─────────────────────────────────────────────────────────
# prefix → action  (used for inline format without DB)
_SHORT_PREFIX: dict[str, str] = {
    "rs:softer":  "rs:sftr",
    "rs:bolder":  "rs:bldr",
    "rs:top5":    "rs:top5",
    "rs:style":   "rs:styl",
    "rs:regen":   "rs:rgen",
    "rs:pick_desc": "rs:dsc",
    "car:headline": "car:hdl",
    "car:softer":  "car:sftr",
    "car:bolder":  "car:bldr",
    "car:shorten": "car:shrt",
    "car:add_slide":"car:add",
    "car:trigger": "car:trig",
    "car:format":  "car:fmt",
    "ag:softer":   "ag:sftr",
    "ag:bolder":   "ag:bldr",
    "ag:shorter":  "ag:shrt",
    "ag:detail":   "ag:dtl",
    "ag:cta":      "ag:cta",
    "ag:resonance":"ag:res",
    "ag:add_proof":"ag:prf",
    "ag:mid_hook": "ag:mhk",
    "ag:hook":     "ag:hk",
    "ag:deepen":   "ag:dpn",
    "ag:tactics":  "ag:tct",
    "ag:positioning": "ag:pos",
    "ag:save_moment": "ag:svm",
    "regen":       "regen",
    "refine":      "refine",
}

# Reverse: compact prefix → canonical action name
_PREFIX_TO_ACTION: dict[str, str] = {v: k for k, v in _SHORT_PREFIX.items()}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class ResolvedCallback:
    action: str
    tool_id: str | None        # заполняется из result в validate_callback
    result_id: int
    keyboard_version: int
    user_id: int
    metadata: dict | None
    allow_repeat: bool
    token: str | None          # None для inline-формата


@dataclass
class ValidationResult:
    is_valid: bool
    error_code: str | None
    user_message: str | None
    result: dict | None = None  # заполняется при is_valid=True


# ── Public API ─────────────────────────────────────────────────────────────────

async def build_callback(
    user_id: int,
    tool_id: str,
    result_id: int,
    action: str,
    keyboard_version: int,
    *,
    metadata: dict | None = None,
    allow_repeat: bool = False,
    ttl_hours: int = _DEFAULT_TTL_HOURS,
) -> str:
    """
    Создаёт callback_data для InlineKeyboardButton.
    Всегда ≤ 64 байт.

    Inline-формат (без БД):   {prefix}:{result_id}:{kbv}
    Токен (с metadata / unknown action):  cb:{token8}
    """
    needs_db = bool(metadata) or (action not in _SHORT_PREFIX)

    if not needs_db:
        prefix = _SHORT_PREFIX[action]
        data = f"{prefix}:{result_id}:{keyboard_version}"
        assert len(data.encode("utf-8")) <= 64, f"callback_data too long: {data!r}"
        return data

    # DB token path
    token = secrets.token_hex(4)   # 8 hex chars
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    try:
        from db import _get_pool
        pool = _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO callback_contexts
                   (token, user_id, tool_id, result_id, action,
                    keyboard_version, metadata_json, allow_repeat, expires_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                   ON CONFLICT (token) DO NOTHING""",
                token, user_id, tool_id, result_id, action,
                keyboard_version,
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
                allow_repeat,
                expires_at,
            )
    except Exception as e:
        logger.error(f"[cb] build_callback DB error: {e}")
    data = f"cb:{token}"
    assert len(data.encode("utf-8")) <= 64
    return data


async def resolve_callback(
    callback_data: str,
    user_id: int,
) -> ResolvedCallback | None:
    """
    Парсит callback_data в ResolvedCallback.
    Возвращает None если формат не распознан (legacy callback — роутится дальше).
    """
    if not callback_data:
        return None

    # DB-токен
    if callback_data.startswith("cb:"):
        token = callback_data[3:]
        return await _resolve_token(token, user_id)

    # Inline-формат: prefix:result_id:kbv
    parts = callback_data.split(":")
    if len(parts) == 3:
        raw_prefix = ":".join(parts[:2]) if parts[0] in ("rs", "car", "ag") else parts[0]
        # Для префиксов вида "rs:top5", "car:hdl", "ag:sftr" нужно взять первые два сегмента
        # Формат: namespace:shortcode:result_id:kbv  — нет, формат rs:top5:182:3
        # parts = ["rs", "top5", "182", "3"] — 4 части!
        pass

    # Inline-формат: prefix:result_id:kbv  где prefix может содержать ":"
    # Итого строка: "rs:top5:182:3" → split(":") = ["rs","top5","182","3"] — 4 элемента
    if len(parts) == 4:
        prefix = f"{parts[0]}:{parts[1]}"
        try:
            result_id = int(parts[2])
            kbv = int(parts[3])
        except (ValueError, TypeError):
            return None
        action = _PREFIX_TO_ACTION.get(prefix)
        if action:
            spec = ACTION_REGISTRY.get(action, {})
            return ResolvedCallback(
                action=action,
                tool_id=None,
                result_id=result_id,
                keyboard_version=kbv,
                user_id=user_id,
                metadata=None,
                allow_repeat=spec.get("repeatable", True),
                token=None,
            )

    # Inline-формат для "regen" и "refine" (без namespace): "regen:182:3"
    if len(parts) == 3:
        prefix = parts[0]
        try:
            result_id = int(parts[1])
            kbv = int(parts[2])
        except (ValueError, TypeError):
            return None
        action = _PREFIX_TO_ACTION.get(prefix)
        if action:
            spec = ACTION_REGISTRY.get(action, {})
            return ResolvedCallback(
                action=action,
                tool_id=None,
                result_id=result_id,
                keyboard_version=kbv,
                user_id=user_id,
                metadata=None,
                allow_repeat=spec.get("repeatable", True),
                token=None,
            )

    return None   # не распознан — legacy


async def validate_callback(
    resolved: ResolvedCallback,
    claiming_user_id: int,
) -> ValidationResult:
    """
    Проверяет все охранные условия.
    При is_valid=True — result заполнен и tool_id проставлен в resolved.
    """
    from db import get_result_kb_state

    # 1. user_id совпадает
    if resolved.user_id != claiming_user_id:
        return ValidationResult(
            False, "wrong_user",
            None,   # silent reject
        )

    # 2. Результат существует
    result = await get_result_kb_state(resolved.result_id)
    if not result:
        return ValidationResult(
            False, "result_not_found",
            "Материал не найден — возможно, был удалён. Создай новый.",
        )

    # 3. Принадлежит пользователю
    if result["user_id"] != claiming_user_id:
        return ValidationResult(False, "result_wrong_user", None)

    # 4. tool_id совпадает (если задан в resolved)
    if resolved.tool_id and result["agent_key"] != resolved.tool_id:
        return ValidationResult(
            False, "tool_mismatch",
            "Действие не применимо к этому инструменту.",
        )

    # 5. Keyboard version актуальна
    current_kbv = result["keyboard_version"]
    if resolved.keyboard_version < current_kbv:
        return ValidationResult(
            False, "stale_keyboard",
            "⚠️ Эта кнопка устарела. Используй кнопки под последним результатом.",
        )

    # 6. Действие разрешено для инструмента
    spec = ACTION_REGISTRY.get(resolved.action, {})
    allowed_tools = spec.get("allowed_tools")
    if allowed_tools is not None and result["agent_key"] not in allowed_tools:
        return ValidationResult(
            False, "action_not_allowed_for_tool",
            "Это действие недоступно для данного инструмента.",
        )

    # 7. Однократное действие уже выполнено?
    completed = set(result.get("completed_actions") or [])
    marks_completed = spec.get("marks_completed", False)
    is_repeatable = spec.get("repeatable", True)
    if marks_completed and not is_repeatable and resolved.action in completed:
        return ValidationResult(
            False, "already_completed",
            "✅ Это действие уже выполнено. Используй доступные кнопки.",
        )

    # 8. DB-токен: использован / истёк
    if resolved.token:
        try:
            from db import _get_pool
            pool = _get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT used_at, allow_repeat, expires_at FROM callback_contexts WHERE token=$1",
                    resolved.token,
                )
            if row:
                if row["used_at"] and not row["allow_repeat"]:
                    return ValidationResult(
                        False, "token_used",
                        "✅ Это действие уже было выполнено.",
                    )
                if row["expires_at"] < datetime.now(timezone.utc):
                    return ValidationResult(
                        False, "token_expired",
                        "⚠️ Эта кнопка устарела. Используй кнопки под последним результатом.",
                    )
        except Exception as e:
            logger.warning(f"[cb] token check error: {e}")

    # Всё ок — обогащаем tool_id из result
    resolved.tool_id = result["agent_key"]
    return ValidationResult(True, None, None, result=result)


async def mark_callback_used(token: str) -> None:
    """Помечает DB-токен использованным (только для однократных действий)."""
    if not token:
        return
    try:
        from db import _get_pool
        pool = _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE callback_contexts SET used_at=NOW() WHERE token=$1",
                token,
            )
    except Exception as e:
        logger.warning(f"[cb] mark_callback_used error: {e}")


async def expire_result_callbacks(result_id: int) -> None:
    """
    Инвалидирует все DB-токены для result_id.
    Для inline-callbacks инвалидация происходит через keyboard_version в results.
    """
    try:
        from db import _get_pool
        pool = _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE callback_contexts SET expires_at=NOW() WHERE result_id=$1",
                result_id,
            )
    except Exception as e:
        logger.warning(f"[cb] expire_result_callbacks error: {e}")


# ── Internal ───────────────────────────────────────────────────────────────────

async def _resolve_token(token: str, user_id: int) -> ResolvedCallback | None:
    try:
        from db import _get_pool
        pool = _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT user_id, tool_id, result_id, action,
                          keyboard_version, metadata_json, allow_repeat
                   FROM callback_contexts WHERE token=$1""",
                token,
            )
        if not row:
            return None
        meta = json.loads(row["metadata_json"]) if row["metadata_json"] else None
        return ResolvedCallback(
            action=row["action"],
            tool_id=row["tool_id"],
            result_id=row["result_id"],
            keyboard_version=row["keyboard_version"],
            user_id=row["user_id"],
            metadata=meta,
            allow_repeat=row["allow_repeat"],
            token=token,
        )
    except Exception as e:
        logger.error(f"[cb] _resolve_token error: {e}")
        return None
