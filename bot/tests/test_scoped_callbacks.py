"""
tests/test_scoped_callbacks.py — PHASE 10: regression tests.

Покрывают:
- парсинг inline-формата
- validate_callback: все охранные условия
- build_result_keyboard: скрытие completed кнопок, keyboard_version
- regen_by_id: использует source_input, не content
- быстрый двойной клик (double tap)
- смена инструмента (tool switch)
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_result(
    result_id: int = 1,
    user_id: int = 100,
    agent_key: str = "reels_short",
    kbv: int = 1,
    completed: list | None = None,
    allowed: list | None = None,
    source_input: str | None = None,
    content: str = "тестовый контент",
) -> dict:
    return {
        "id":                result_id,
        "user_id":           user_id,
        "agent_key":         agent_key,
        "keyboard_version":  kbv,
        "completed_actions": completed or [],
        "allowed_actions":   allowed,
        "source_input":      source_input,
        "content":           content,
    }


def make_resolved_inline(
    action: str = "rs:top5",
    result_id: int = 1,
    kbv: int = 1,
    user_id: int = 100,
    tool_id: str | None = None,
):
    from callback_context import ResolvedCallback, ACTION_REGISTRY
    spec = ACTION_REGISTRY.get(action, {})
    return ResolvedCallback(
        action=action,
        tool_id=tool_id,
        result_id=result_id,
        keyboard_version=kbv,
        user_id=user_id,
        metadata=None,
        allow_repeat=spec.get("repeatable", True),
        token=None,
    )


# ── Format / parse tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_callback_inline_format():
    """build_callback без metadata → inline формат без БД."""
    from callback_context import build_callback
    cb = await build_callback(100, "reels_short", 182, "rs:top5", 3)
    assert cb == "rs:top5:182:3"
    assert len(cb.encode("utf-8")) <= 64


@pytest.mark.asyncio
async def test_build_callback_under_64_bytes_max_ids():
    """Даже с большими ID держится в 64 байтах."""
    from callback_context import build_callback
    cb = await build_callback(9999999, "carousel", 9999999, "car:softer", 99)
    assert len(cb.encode("utf-8")) <= 64


@pytest.mark.asyncio
async def test_resolve_callback_rs_top5():
    from callback_context import resolve_callback
    resolved = await resolve_callback("rs:top5:182:3", user_id=100)
    assert resolved is not None
    assert resolved.action == "rs:top5"
    assert resolved.result_id == 182
    assert resolved.keyboard_version == 3
    assert resolved.token is None


@pytest.mark.asyncio
async def test_resolve_callback_regen():
    from callback_context import resolve_callback
    resolved = await resolve_callback("regen:55:2", user_id=100)
    assert resolved is not None
    assert resolved.action == "regen"
    assert resolved.result_id == 55
    assert resolved.keyboard_version == 2


@pytest.mark.asyncio
async def test_resolve_callback_all_ag_actions():
    """Все ag: действия корректно парсятся."""
    from callback_context import resolve_callback, ACTION_REGISTRY
    ag_actions = [a for a in ACTION_REGISTRY if a.startswith("ag:")]
    for action in ag_actions:
        resolved = await resolve_callback(f"{action}:99:1".replace("ag:", "ag:").replace("ag:ag:", "ag:"), user_id=100)
        # Формат: ag:sftr:99:1 — 4 части после split(":")
        # Проверяем через build_callback
        from callback_context import build_callback
        cb = await build_callback(100, "post", 99, action, 1)
        r2 = await resolve_callback(cb, user_id=100)
        assert r2 is not None, f"Failed to resolve action={action!r}, cb={cb!r}"
        assert r2.action == action


@pytest.mark.asyncio
async def test_resolve_legacy_callback_returns_none():
    """Старые callback_data не распознаются → None (для legacy fallback)."""
    from callback_context import resolve_callback
    assert await resolve_callback("rs_edit_softer",  user_id=100) is None
    assert await resolve_callback("regen_last",      user_id=100) is None
    assert await resolve_callback("ag_edit_bolder",  user_id=100) is None
    assert await resolve_callback("menu_main",       user_id=100) is None
    assert await resolve_callback("",                user_id=100) is None


# ── validate_callback tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_wrong_user():
    from callback_context import validate_callback
    resolved = make_resolved_inline(user_id=100)
    with patch("callback_context.get_result_kb_state",
               AsyncMock(return_value=make_result(user_id=100))):
        vr = await validate_callback(resolved, claiming_user_id=999)
    assert not vr.is_valid
    assert vr.error_code == "wrong_user"
    assert vr.user_message is None   # silent reject


@pytest.mark.asyncio
async def test_validate_result_not_found():
    from callback_context import validate_callback
    resolved = make_resolved_inline()
    with patch("callback_context.get_result_kb_state", AsyncMock(return_value=None)):
        vr = await validate_callback(resolved, claiming_user_id=100)
    assert not vr.is_valid
    assert vr.error_code == "result_not_found"


@pytest.mark.asyncio
async def test_validate_result_wrong_user():
    """result.user_id != claiming_user_id."""
    from callback_context import validate_callback
    resolved = make_resolved_inline(user_id=100)
    with patch("callback_context.get_result_kb_state",
               AsyncMock(return_value=make_result(user_id=200))):
        vr = await validate_callback(resolved, claiming_user_id=100)
    assert not vr.is_valid
    assert vr.error_code == "result_wrong_user"


@pytest.mark.asyncio
async def test_validate_stale_keyboard():
    """kbv кнопки (1) < текущий kbv результата (3) → stale."""
    from callback_context import validate_callback
    resolved = make_resolved_inline(kbv=1)
    result   = make_result(kbv=3)
    with patch("callback_context.get_result_kb_state", AsyncMock(return_value=result)):
        vr = await validate_callback(resolved, claiming_user_id=100)
    assert not vr.is_valid
    assert vr.error_code == "stale_keyboard"


@pytest.mark.asyncio
async def test_validate_already_completed_top5():
    """rs:top5 — marks_completed=True, repeatable=False → rejected if in completed."""
    from callback_context import validate_callback
    resolved = make_resolved_inline(action="rs:top5")
    result   = make_result(completed=["rs:top5"])
    with patch("callback_context.get_result_kb_state", AsyncMock(return_value=result)):
        vr = await validate_callback(resolved, claiming_user_id=100)
    assert not vr.is_valid
    assert vr.error_code == "already_completed"


@pytest.mark.asyncio
async def test_validate_repeatable_after_completion():
    """rs:softer — repeatable=True → проходит даже если в completed."""
    from callback_context import validate_callback
    resolved = make_resolved_inline(action="rs:softer")
    result   = make_result(completed=["rs:softer"])
    with patch("callback_context.get_result_kb_state", AsyncMock(return_value=result)):
        vr = await validate_callback(resolved, claiming_user_id=100)
    assert vr.is_valid


@pytest.mark.asyncio
async def test_validate_tool_mismatch_ag_resonance():
    """ag:resonance разрешён только для warmup, не для reels_short."""
    from callback_context import validate_callback
    resolved = make_resolved_inline(action="ag:resonance", tool_id="reels_short")
    result   = make_result(agent_key="reels_short")
    with patch("callback_context.get_result_kb_state", AsyncMock(return_value=result)):
        vr = await validate_callback(resolved, claiming_user_id=100)
    assert not vr.is_valid
    assert vr.error_code == "action_not_allowed_for_tool"


@pytest.mark.asyncio
async def test_validate_ok_sets_tool_id():
    """При успехе — tool_id проставляется в resolved из result."""
    from callback_context import validate_callback
    resolved = make_resolved_inline(action="rs:softer", tool_id=None)
    result   = make_result(agent_key="reels_short", kbv=1)
    with patch("callback_context.get_result_kb_state", AsyncMock(return_value=result)):
        vr = await validate_callback(resolved, claiming_user_id=100)
    assert vr.is_valid
    assert resolved.tool_id == "reels_short"
    assert vr.result is not None


# ── Double tap test ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_double_tap_one_time_action():
    """Второй клик на rs:top5 отклоняется после mark_result_action_completed."""
    from callback_context import validate_callback

    resolved = make_resolved_inline(action="rs:top5", kbv=1)

    # Первый клик: result без completed, kbv=1
    result_before = make_result(completed=[], kbv=1)
    with patch("callback_context.get_result_kb_state", AsyncMock(return_value=result_before)):
        vr1 = await validate_callback(resolved, claiming_user_id=100)
    assert vr1.is_valid

    # После первого клика: action добавлен в completed, kbv инкрементировался
    result_after = make_result(completed=["rs:top5"], kbv=2)
    with patch("callback_context.get_result_kb_state", AsyncMock(return_value=result_after)):
        vr2 = await validate_callback(resolved, claiming_user_id=100)
    # kbv кнопки (1) < current (2) → stale_keyboard (раньше чем already_completed)
    assert not vr2.is_valid
    assert vr2.error_code in ("stale_keyboard", "already_completed")


# ── Keyboard builder tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_keyboard_hides_completed_top5():
    """rs:top5 не должна появляться в клавиатуре после выполнения."""
    from ui.keyboards import build_result_keyboard
    result = make_result(completed=["rs:top5"], kbv=2, agent_key="reels_short")

    kb = await build_result_keyboard(100, result, "reels_short")
    all_cb = [btn.callback_data for row in kb.inline_keyboard for btn in row]

    # Ни одна кнопка не должна содержать rs:top5
    assert not any("rs:top5" in cb for cb in all_cb), f"rs:top5 found in {all_cb}"
    # softer и bolder — должны быть
    assert any("rs:sftr" in cb for cb in all_cb), "rs:softer missing"
    assert any("rs:bldr" in cb for cb in all_cb), "rs:bolder missing"


@pytest.mark.asyncio
async def test_keyboard_version_in_all_scoped_buttons():
    """Все scoped кнопки должны содержать актуальный keyboard_version."""
    from ui.keyboards import build_result_keyboard
    result = make_result(kbv=5, agent_key="reels_short")
    kb = await build_result_keyboard(100, result, "reels_short")

    scoped_cbs = [
        btn.callback_data
        for row in kb.inline_keyboard
        for btn in row
        if btn.callback_data and ":" in btn.callback_data
        and not btn.callback_data.startswith("menu_")
        and not btn.callback_data.startswith("flow_")
        and not btn.callback_data.startswith("cb:")
    ]
    for cb in scoped_cbs:
        parts = cb.split(":")
        if len(parts) >= 3:
            kbv_str = parts[-1]
            assert kbv_str == "5", f"Expected kbv=5 in {cb!r}"


@pytest.mark.asyncio
async def test_keyboard_carousel_all_actions_present():
    """Карусель без completed — все действия присутствуют."""
    from ui.keyboards import build_result_keyboard
    result = make_result(completed=[], kbv=1, agent_key="carousel")
    kb = await build_result_keyboard(100, result, "carousel")
    all_cb = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    # Должны быть softer, bolder (они всегда repeatable)
    assert any("car:sftr" in cb for cb in all_cb)
    assert any("car:bldr" in cb for cb in all_cb)


@pytest.mark.asyncio
async def test_keyboard_carousel_hides_shorten_after_complete():
    """car:shorten (marks_completed=True, repeatable=False) исчезает после выполнения."""
    from ui.keyboards import build_result_keyboard
    result = make_result(completed=["car:shorten"], kbv=2, agent_key="carousel")
    kb = await build_result_keyboard(100, result, "carousel")
    all_cb = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert not any("car:shrt" in cb for cb in all_cb), "car:shorten должна исчезнуть"


# ── Tool switch test ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_old_button_after_tool_switch_stale():
    """Старая кнопка reels (kbv=1) при kbv=3 у result → stale_keyboard."""
    from callback_context import validate_callback
    resolved = make_resolved_inline(action="rs:softer", result_id=55, kbv=1)
    result   = make_result(result_id=55, kbv=3, agent_key="reels_short")
    with patch("callback_context.get_result_kb_state", AsyncMock(return_value=result)):
        vr = await validate_callback(resolved, claiming_user_id=100)
    assert not vr.is_valid
    assert vr.error_code == "stale_keyboard"


# ── Regen safety test ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_regen_by_id_uses_source_input():
    """regen_by_id использует source_input, а не content."""
    from flows.misc import regen_by_id

    mock_result = {
        "id": 42,
        "user_id": 100,
        "agent_key": "post",
        "agent_name": "Пост",
        "content": "правленный контент после правок",
        "source_input": "исходный топик или первый вариант",
        "allowed_actions": None,
        "completed_actions": [],
    }

    captured_prompt = []

    async def mock_complete(system, prompt, **kwargs):
        captured_prompt.append(prompt)
        return "новый вариант"

    update_mock = MagicMock()
    update_mock.effective_chat.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    update_mock.effective_chat.id = 100

    with patch("flows.misc.get_result_by_id", AsyncMock(return_value=mock_result)), \
         patch("flows.misc.complete", mock_complete), \
         patch("flows.misc.save_result", AsyncMock(return_value=99)), \
         patch("flows.misc.safe_delete", AsyncMock()), \
         patch("flows.misc.send", AsyncMock()), \
         patch("flows.misc.build_voice_context", AsyncMock(return_value="")), \
         patch("flows.misc.get_prompt", AsyncMock(return_value="sys")), \
         patch("flows.misc.protect", lambda uid, s: s), \
         patch("flows.misc.typing_loop", return_value=MagicMock(cancel=MagicMock())):
        await regen_by_id(update_mock, 100, 42)

    # Проверяем что в промпт пошёл source_input, не content
    assert captured_prompt, "complete не был вызван"
    assert "исходный топик" in captured_prompt[0], (
        f"Ожидали source_input в промпте, получили: {captured_prompt[0]!r}"
    )
    assert "правленный контент" not in captured_prompt[0], (
        "В промпт попал правленный content вместо source_input"
    )


# ── Action registry completeness ──────────────────────────────────────────────

def test_action_registry_has_required_fields():
    """Каждое действие в реестре имеет обязательные поля."""
    from callback_context import ACTION_REGISTRY
    required = {"repeatable", "requires_result", "allowed_tools"}
    for action, spec in ACTION_REGISTRY.items():
        for field in required:
            assert field in spec, f"ACTION_REGISTRY[{action!r}] missing field {field!r}"


def test_all_short_prefixes_reversible():
    """Каждый компактный prefix однозначно маппится обратно в action."""
    from callback_context import _SHORT_PREFIX, _PREFIX_TO_ACTION
    for action, prefix in _SHORT_PREFIX.items():
        assert _PREFIX_TO_ACTION.get(prefix) == action, (
            f"prefix {prefix!r} → {_PREFIX_TO_ACTION.get(prefix)!r}, expected {action!r}"
        )


@pytest.mark.asyncio
async def test_round_trip_all_inline_actions():
    """build_callback + resolve_callback = исходный action для всех inline actions."""
    from callback_context import build_callback, resolve_callback, _SHORT_PREFIX
    for action in _SHORT_PREFIX:
        cb = await build_callback(100, "reels_short", 42, action, 1)
        resolved = await resolve_callback(cb, user_id=100)
        assert resolved is not None, f"resolve failed for action={action!r}, cb={cb!r}"
        assert resolved.action == action, (
            f"Round-trip failed: action={action!r}, cb={cb!r}, got={resolved.action!r}"
        )
        assert resolved.result_id == 42
        assert resolved.keyboard_version == 1
