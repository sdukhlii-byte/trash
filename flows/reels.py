"""
flows/reels.py — flow «Рилс-коротышка»: тема → 14 заголовков → описание.

Изменения v2:
- Статусные сообщения с характером Миры (не «Пишу заголовки...»)
- Ошибки — с контекстом, не одинаковое «Что-то сломалось»
- safe_delete вместо bare except: pass
"""
import logging
import re

from telegram import Update

from db import (
    get_agent_session, save_agent_session, clear_agent_session,
    get_profile, save_result,
)
from llm import complete
from security import protect
from prompt_editor import get_prompt
from utils import send, edit, kb, safe_delete
from voice_learner import build_voice_context, voice_feedback_kb
from config import build_reels_short_headline_system, build_reels_short_desc_system

logger = logging.getLogger(__name__)

_RS_KEY = "reels_short_flow"

# Статусные сообщения с голосом Миры
_STATUS_HEADLINES = [
    "Смотрю что останавливает скролл в твоей нише...",
    "Уже вижу несколько сильных углов...",
    "Почти готово — выбираю лучшие из 20...",
]
_STATUS_DESC = [
    "Пишу описание — хочу чтобы оно усиливало рилс...",
    "Собираю CTA под твою аудиторию...",
]


async def rs_start(update: Update, user_id: int) -> None:
    await clear_agent_session(user_id, _RS_KEY)
    await save_agent_session(user_id, _RS_KEY, {"step": "await_topic"})
    caption = (
        "🎬 *Хуки для рилса*\n\nНапиши тему рилса:\n\n"
        "_Например: делегирование / выгорание / как поднять цену_"
    )
    await send(update, caption, parse_mode="Markdown", reply_markup=kb(["← Меню|menu_main"]))


async def rs_gen_headlines(update: Update, user_id: int, topic: str) -> None:
    profile = await get_profile(user_id)
    base = build_reels_short_headline_system(
        profile.get("niche", "не указана"),
        profile.get("audience", "не указана"),
        profile.get("tone", "живой"),
    )
    system = protect(user_id, await get_prompt(user_id, "reels_short_headlines", base))

    status = await update.effective_chat.send_message(_STATUS_HEADLINES[0])
    try:
        headlines = await complete(system, f"Тема: {topic}", temperature=0.85)
    except Exception as e:
        logger.error(f"[reels] gen_headlines error: {e}")
        await safe_delete(status)
        await send(
            update,
            "Связь прервалась — все данные сохранены, нажми 🔁",
            reply_markup=kb(["🔁 Попробовать снова|rs_regen", "← Меню|menu_main"]),
        )
        return
    await safe_delete(status)

    await save_agent_session(user_id, _RS_KEY, {
        "step": "pick_headline", "topic": topic, "headlines": headlines,
    })
    await send(
        update,
        f"🎯 *14 заголовков — тема:* _{topic}_\n\n{headlines}",
        parse_mode="Markdown",
        reply_markup=kb(
            ["✅ Выбрать заголовок|rs_pick", "🔄 Перегенерировать|rs_regen"],
            ["← Меню|menu_main"],
        ),
    )


async def rs_pick(update: Update, user_id: int) -> None:
    s = await get_agent_session(user_id, _RS_KEY)
    if not s:
        return await rs_start(update, user_id)
    s["step"] = "enter_headline"
    await save_agent_session(user_id, _RS_KEY, s)
    await send(
        update,
        "Напиши *номер* (1–14) или скопируй нужный заголовок:",
        parse_mode="Markdown",
        reply_markup=kb(["← Меню|menu_main"]),
    )


async def rs_headline_chosen(update: Update, user_id: int, text: str, s: dict) -> None:
    chosen = text.strip()
    if chosen.isdigit():
        lines = []
        for line in s.get("headlines", "").split("\n"):
            clean = re.sub(r"[\*_`~]+", "", line).strip()
            if re.match(r"^\d+[\.]\s+\S", clean):
                lines.append(re.sub(r"^\d+[\.]\s+", "", clean).strip())
        n = int(chosen)
        if 1 <= n <= len(lines):
            chosen = lines[n - 1]

    s["chosen"] = chosen
    s["step"]   = "await_details"
    await save_agent_session(user_id, _RS_KEY, s)
    await send(
        update,
        f"✅ *Заголовок:*\n_{chosen}_\n\nХочешь добавить детали для описания?\n"
        "_Личная история, кейс, цифры — что вложить в смысл_",
        parse_mode="Markdown",
        reply_markup=kb(
            ["✍️ Да, добавлю|rs_add_details", "⏭ Нет, пропустить|rs_skip_details"],
            ["← Меню|menu_main"],
        ),
    )


async def rs_await_destination(update: Update, user_id: int, s: dict) -> None:
    s["step"] = "await_destination"
    await save_agent_session(user_id, _RS_KEY, s)
    await send(
        update,
        "📍 *Куда ведёт рилс?*\n\n_Подписаться / написать слово в комменты / перейти по ссылке_",
        parse_mode="Markdown",
        reply_markup=kb(
            ["⏭ Стандартный CTA|rs_default_cta"],
            ["← Меню|menu_main"],
        ),
    )


async def rs_generate(update: Update, user_id: int, s: dict) -> None:
    profile = await get_profile(user_id)
    base = build_reels_short_desc_system(
        profile.get("niche", "не указана"),
        profile.get("audience", "не указана"),
        profile.get("tone", "живой"),
    )
    system = protect(user_id, await get_prompt(user_id, "reels_short_desc", base))
    prompt = (
        f"Тема: {s.get('topic', '')}\n"
        f"Заголовок: {s.get('chosen', '')}\n"
        f"Детали: {s.get('details', 'нет')}\n"
        f"CTA / куда ведёт: {s.get('destination', 'подписаться')}"
    )
    status = await update.effective_chat.send_message(_STATUS_DESC[0])
    try:
        result = await complete(system, prompt, max_tokens=2000, temperature=0.85)
    except Exception as e:
        logger.error(f"[reels] rs_generate error: {e}")
        await safe_delete(status)
        await send(
            update,
            "Не вышло с первого раза — все данные сохранены, нажми 🔁",
            reply_markup=kb(["🔁 Повторить|rs_retry_gen", "← Меню|menu_main"]),
        )
        return
    await safe_delete(status)

    if not result or not result.strip():
        result = "⚠️ Пустой ответ. Попробуй снова."

    # Сохраняем slim-состояние для «Другой заголовок»
    try:
        await save_agent_session(user_id, "__rs_last__", {
            "topic":     s.get("topic", ""),
            "headlines": s.get("headlines", ""),
        })
    except Exception:
        pass

    await clear_agent_session(user_id, _RS_KEY)
    try:
        await save_result(
            user_id, "reels_short", "Рилс-коротышка",
            f"Заголовок: {s.get('chosen', '')}\n\n{result}",
        )
    except Exception as e:
        logger.warning(f"save_result rs failed: {e}")
    try:
        from ui.home import update_streak_on_result
        await update_streak_on_result(user_id)
    except Exception:
        pass

    await send(
        update,
        f"🎬 *Готово!*\n\n*Заголовок:*\n{s.get('chosen', '')}\n\n*Описание:*\n{result}",
        parse_mode="Markdown",
        reply_markup=kb(
            ["✏️ Доработать|refine_last",      "🔄 Другой вариант|regen_last"],
            ["🔁 Новая тема|flow_reels_short",  "🔄 Другой заголовок|rs_back_to_pick"],
            ["← Меню|menu_main"],
        ),
    )

    # Мягкий upsell для триал-пользователей
    # Voice feedback
    try:
        import asyncio as _asyncio
        from db import get_results as _gr
        _recent = await _gr(user_id, limit=1)
        if _recent:
            await _asyncio.sleep(0.5)
            await send(update, "Звучит как твой голос?",
                       reply_markup=voice_feedback_kb(_recent[0]["id"]))
    except Exception:
        pass

    # Стрик
    try:
        from ui.home import update_streak_on_result as _usr
        await _usr(user_id)
    except Exception:
        pass

    from ui.cabinet import maybe_show_upsell
    await maybe_show_upsell(update, user_id)

    from flows.proactive import maybe_suggest_next
    await maybe_suggest_next(update, user_id, "reels_short", delay=1.2)


async def route_text(update: Update, user_id: int, text: str, s: dict) -> None:
    step = s.get("step", "")
    if step == "await_topic":
        await rs_gen_headlines(update, user_id, text)
    elif step == "enter_headline":
        await rs_headline_chosen(update, user_id, text, s)
    elif step == "await_details":
        s["details"] = text
        await save_agent_session(user_id, _RS_KEY, s)
        await rs_await_destination(update, user_id, s)
    elif step == "await_details_text":
        s["details"] = text
        await save_agent_session(user_id, _RS_KEY, s)
        await rs_await_destination(update, user_id, s)
    elif step == "await_destination":
        s["destination"] = text
        await save_agent_session(user_id, _RS_KEY, s)
        await rs_generate(update, user_id, s)
    else:
        await send(update, "Используй кнопки 👆", reply_markup=kb(["← Меню|menu_main"]))
