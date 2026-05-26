"""
ui/keyboards.py — PHASE 6-8: centralized keyboard builder.

Все панели доработки (reels, carousel, agents) строятся через build_result_keyboard().
Каждая кнопка создаётся через build_callback() — никакого ручного callback_data.
"""
from __future__ import annotations

import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from callback_context import build_callback, ACTION_REGISTRY

logger = logging.getLogger(__name__)


# ── Button label map ───────────────────────────────────────────────────────────
# action → (label, force_new_row)
_BTN: dict[str, tuple[str, bool]] = {
    "rs:top5":       ("✂️ Топ-5",                False),
    "rs:softer":     ("🎨 Мягче",                False),
    "rs:bolder":     ("🔥 Провокационнее",        True),
    "rs:style":      ("💡 Стиль",                 False),
    "rs:pick_desc":  ("📝 Описание к хуку",       True),
    "car:headline":  ("✏️ Заголовок",             False),
    "car:format":    ("🔄 Формат",                True),
    "car:trigger":   ("💪 Триггер",               False),
    "car:add_slide": ("➕ Слайд",                 True),
    "car:softer":    ("🎨 Мягче",                 False),
    "car:bolder":    ("🔥 Жёстче",                True),
    "car:shorten":   ("✂️ Сократить",             True),
    "ag:softer":     ("🎨 Мягче",                 False),
    "ag:bolder":     ("🔥 Жёстче",                True),
    "ag:shorter":    ("✂️ Сократить",             False),
    "ag:detail":     ("➕ Деталь",                True),
    "ag:cta":        ("💪 Усилить CTA",           True),
    "ag:resonance":  ("✨ Усилить резонанс",       False),
    "ag:add_proof":  ("📣 Добавить доказательство", True),
    "ag:mid_hook":   ("🎣 Усилить слайды 5-7",    True),
    "ag:hook":       ("🎬 Переписать хук",         True),
    "ag:deepen":     ("🔬 Углубить",               True),
    "ag:tactics":    ("⚡ Тактики",                False),
    "ag:positioning":("🎯 Позиционирование",       True),
    "ag:save_moment":("🎯 Save-момент",            True),
    "regen":         ("🔄 Ещё вариант",            False),
    "refine":        ("✏️ Доработать",             False),
}

# Дефолтные наборы действий по инструменту
_TOOL_ACTIONS: dict[str, list[str]] = {
    "reels_short": [
        "rs:top5", "rs:softer", "rs:bolder", "rs:style", "rs:pick_desc",
    ],
    "carousel": [
        "car:headline", "car:softer", "car:bolder", "car:shorten",
        "car:add_slide", "car:trigger", "car:format",
    ],
    "warmup": [
        "ag:resonance", "ag:add_proof", "ag:softer", "ag:cta",
    ],
    "stories": [
        "ag:softer", "ag:bolder", "ag:shorter", "ag:mid_hook",
    ],
    "talking_head": [
        "ag:softer", "ag:bolder", "ag:shorter", "ag:hook",
    ],
    "cartoon": [
        "ag:softer", "ag:bolder", "ag:shorter", "ag:hook",
    ],
    "competitor": [
        "ag:tactics", "ag:positioning", "ag:softer", "ag:shorter",
    ],
    "profile": [
        "ag:deepen", "ag:shorter",
    ],
    "post": [
        "ag:softer", "ag:bolder", "ag:shorter", "ag:detail", "ag:cta",
    ],
    "carousel_agent": [   # ag-версия карусели (не flow)
        "ag:softer", "ag:bolder", "ag:shorter", "ag:detail", "ag:save_moment",
    ],
}
_DEFAULT_ACTIONS = ["ag:softer", "ag:bolder", "ag:shorter", "ag:detail", "ag:cta"]


async def build_result_keyboard(
    user_id: int,
    result: dict,      # строка из results (или get_result_kb_state)
    tool_id: str,
    *,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
    include_regen_refine: bool = True,
    drift_warning_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """
    Строит клавиатуру из allowed_actions минус completed_actions.
    Каждая кнопка — через build_callback() с актуальным keyboard_version.

    result — dict с полями: id, keyboard_version, completed_actions, allowed_actions, user_id
    """
    result_id = result["id"]
    kbv       = result.get("keyboard_version", 1)
    completed = set(result.get("completed_actions") or [])
    allowed   = result.get("allowed_actions")  # None = дефолт по tool

    # Список видимых действий
    if allowed:
        action_list = [a for a in allowed if a in ACTION_REGISTRY]
    else:
        action_list = _TOOL_ACTIONS.get(tool_id, _DEFAULT_ACTIONS)

    def is_visible(action: str) -> bool:
        spec = ACTION_REGISTRY.get(action, {})
        if spec.get("marks_completed") and not spec.get("repeatable", True):
            return action not in completed
        return True

    visible = [a for a in action_list if is_visible(a)]

    # Строим кнопки
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []

    for action in visible:
        label, force_new_row = _BTN.get(action, (action, False))
        try:
            cb_data = await build_callback(
                user_id=user_id,
                tool_id=tool_id,
                result_id=result_id,
                action=action,
                keyboard_version=kbv,
            )
        except Exception as e:
            logger.error(f"[keyboards] build_callback failed action={action}: {e}")
            continue

        btn = InlineKeyboardButton(label, callback_data=cb_data)

        if force_new_row and current_row:
            rows.append(current_row)
            current_row = [btn]
        elif len(current_row) >= 2:
            rows.append(current_row)
            current_row = [btn]
        else:
            current_row.append(btn)

    if current_row:
        rows.append(current_row)

    # Drift warning rows (при 3+ правках)
    if drift_warning_rows:
        rows.extend(drift_warning_rows)

    # Regen + Refine
    if include_regen_refine:
        try:
            regen_cb  = await build_callback(user_id, tool_id, result_id, "regen",  kbv)
            refine_cb = await build_callback(user_id, tool_id, result_id, "refine", kbv)
            rows.append([
                InlineKeyboardButton("🔄 Ещё вариант", callback_data=regen_cb),
                InlineKeyboardButton("✏️ Доработать",  callback_data=refine_cb),
            ])
        except Exception as e:
            logger.error(f"[keyboards] regen/refine build failed: {e}")

    # Extra rows (например, voice feedback)
    if extra_rows:
        rows.extend(extra_rows)

    # Меню — всегда последний
    rows.append([InlineKeyboardButton("← Меню", callback_data="menu_main")])

    return InlineKeyboardMarkup(rows)


async def build_result_keyboard_with_voice(
    user_id: int,
    result: dict,
    tool_id: str,
    *,
    voice_result_id: int | None = None,
    voice_hint: str = "",
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
    drift_warning_rows: list[list[InlineKeyboardButton]] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Возвращает (prompt_text, keyboard) с voice feedback кнопками поверх панели правок.
    Используется в flows где нужна комбинированная клавиатура.
    """
    from voice_learner import voice_feedback_kb as _vfkb

    edit_kb = await build_result_keyboard(
        user_id, result, tool_id,
        extra_rows=extra_rows,
        drift_warning_rows=drift_warning_rows,
        include_regen_refine=True,
    )
    # Достаём все строки кроме последней (← Меню) — добавим через voice_feedback_kb
    inner_rows = edit_kb.inline_keyboard[:-1]

    rid = voice_result_id or result["id"]
    combined_kb = _vfkb(rid, extra_rows=inner_rows)
    prompt_text = f"Звучит как твой голос?{voice_hint}"
    return prompt_text, combined_kb
