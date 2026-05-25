"""
flows/reels.py — v3: тема → хуки готовы → панель доработки.

Принцип (как в carousel.py):
  Движок сам выбирает стиль хуков под профиль пользователя.
  Пользователь видит 14 хуков сразу — никаких вопросов до результата.
  После — панель правок для докрутки.

Флоу:
  1. rs_start        — запрашиваем тему
  2. rs_gen          — авто-выбор стиля + генерация 14 хуков → результат
  3. Панель доработки:
       ✏️ Другой хук (выбрать из списка)
       🔄 Перегенерировать все
       📝 Написать описание (к выбранному хуку)
       🎨 Мягче / 🔥 Провокационнее
       ✂️ Топ-5 лучших
"""
import asyncio
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
from utils import send, kb, safe_delete
from voice_learner import build_voice_context, voice_feedback_kb
from config import build_reels_short_headline_system, build_reels_short_desc_system

logger = logging.getLogger(__name__)

_RS_KEY = "reels_short_flow"

# ── Авто-выбор стиля хуков под профиль ───────────────────────────────────────

_AUTO_STYLE_SYSTEM = """\
Ты — эксперт по Reels. По теме и профилю автора выбери лучший стиль хуков.
Ответь строго в формате JSON без пояснений:
{"style": "pain", "angle": "личная история провала"}

Стили: pain (боль/страх), curiosity (недосказанность), social_proof (истории других),
transformation (до/после), authority (экспертность), provocation (провокация/вызов).
"""

# Промпты для панели доработки
_REFINE_PROMPTS = {
    "softer": (
        "Перепиши все хуки в более мягком и поддерживающем тоне. "
        "Убери давление и агрессию, сохрани суть. Верни нумерованный список."
    ),
    "bolder": (
        "Сделай хуки провокационнее и острее: прямые утверждения, вызов, "
        "неудобная правда. Без воды. Верни нумерованный список."
    ),
    "top5": (
        "Из этих хуков выбери 5 самых сильных. Объясни в одном предложении "
        "почему каждый работает. Верни нумерованный список с пояснением."
    ),
    "restyle_curiosity": (
        "Перепиши все хуки через триггер ЛЮБОПЫТСТВО: недосказанность, интрига, "
        "'а что если', 'мало кто знает'. Верни нумерованный список."
    ),
    "restyle_pain": (
        "Перепиши все хуки через триггер БОЛЬ: назови конкретную проблему, "
        "страх, ошибку которую совершает аудитория. Верни нумерованный список."
    ),
    "restyle_social": (
        "Перепиши хуки через СОЦИАЛЬНОЕ ДОКАЗАТЕЛЬСТВО: 'они сделали', "
        "'у неё получилось', конкретные истории. Верни нумерованный список."
    ),
}

_THINKING_MSGS = [
    "Смотрю что останавливает скролл в твоей нише...",
    "Подбираю углы под твою аудиторию...",
    "Нахожу формулировки которые цепляют...",
]


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _edit_panel_kb():
    """Панель доработки после генерации хуков."""
    return kb(
        ["📝 Написать описание к хуку|rs_pick_for_desc",  "🔄 Перегенерировать|rs_regen"],
        ["🎨 Мягче|rs_edit_softer",                        "🔥 Провокационнее|rs_edit_bolder"],
        ["✂️ Топ-5 лучших|rs_edit_top5",                  "💡 Другой стиль|rs_edit_style"],
        ["🔁 Новая тема|flow_reels_short",                  "← Меню|menu_main"],
    )


def _style_choice_kb():
    """Выбор стиля для смены через панель доработки."""
    return kb(
        ["🤔 Любопытство|rs_style_curiosity",   "😨 Боль / страх|rs_style_pain"],
        ["👥 Соц. доказательство|rs_style_social"],
        ["← Назад|rs_edit_back",                "← Меню|menu_main"],
    )


async def _auto_style(topic: str, profile: dict) -> dict:
    """
    Автоматически выбирает стиль хуков под профиль.
    Скрыто от пользователя — он видит только готовые хуки.
    """
    prompt = (
        f"Ниша: {profile.get('niche', 'не указана')}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\n"
        f"Тема: {topic}"
    )
    try:
        import json as _json
        raw = await complete(_AUTO_STYLE_SYSTEM, prompt, temperature=0.3)
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = _json.loads(raw[start:end])
            return {
                "style": data.get("style", "curiosity"),
                "angle": data.get("angle", ""),
            }
    except Exception as e:
        logger.warning(f"[reels] auto_style failed: {e}")
    return {"style": "curiosity", "angle": ""}


# ── Основной флоу ─────────────────────────────────────────────────────────────

async def rs_start(update: Update, user_id: int) -> None:
    """Шаг 1: запрашиваем тему."""
    await clear_agent_session(user_id, _RS_KEY)
    await save_agent_session(user_id, _RS_KEY, {"step": "await_topic"})
    await send(
        update,
        "🎬 *Хуки для рилса*\n\nНапиши тему:\n\n"
        "_Например: делегирование / выгорание / как поднять цену_",
        parse_mode="Markdown",
        reply_markup=kb(["← Меню|menu_main"]),
    )


async def rs_gen(update: Update, user_id: int, topic: str) -> None:
    """
    Шаг 2: авто-выбор стиля + генерация всех хуков.
    Никаких вопросов — пользователь видит результат сразу.
    """
    profile = await get_profile(user_id)
    status  = await update.effective_chat.send_message(
        _THINKING_MSGS[hash(topic) % len(_THINKING_MSGS)]
    )

    # Авто-выбор стиля (скрыто от пользователя)
    picked = await _auto_style(topic, profile)

    voice = await build_voice_context(user_id)
    base  = build_reels_short_headline_system(
        profile.get("niche", "не указана"),
        profile.get("audience", "не указана"),
        profile.get("tone", "живой"),
    )
    # Добавляем стиль в системный промпт
    style_hint = (
        f"\n\nСтиль хуков для этой темы: {picked['style']}. "
        f"Угол: {picked['angle']}." if picked["angle"] else
        f"\n\nСтиль хуков: {picked['style']}."
    )
    system = protect(user_id, await get_prompt(user_id, "reels_short_headlines", base + style_hint + voice))

    s = {"step": "generating", "topic": topic, "style": picked["style"]}
    await save_agent_session(user_id, _RS_KEY, s)

    try:
        headlines = await complete(system, f"Тема: {topic}", temperature=0.85)
    except Exception as e:
        logger.error(f"[reels] rs_gen error: {e}")
        await safe_delete(status)
        s["step"] = "await_topic"
        await save_agent_session(user_id, _RS_KEY, s)
        await send(
            update,
            "Связь прервалась — все данные сохранены, нажми 🔁",
            reply_markup=kb(["🔁 Попробовать снова|rs_regen", "← Меню|menu_main"]),
        )
        return

    await safe_delete(status)

    if not headlines or not headlines.strip():
        headlines = "⚠️ Пустой ответ. Попробуй снова."

    # Сохраняем результат для панели правок
    s.update({
        "step":           "done",
        "headlines":      headlines,
        "last_result":    headlines,
    })
    await save_agent_session(user_id, _RS_KEY, s)

    try:
        await save_result(user_id, "reels_short", "Хуки для рилса",
                          f"Тема: {topic}\n\n{headlines}")
    except Exception as e:
        logger.warning(f"save_result rs failed: {e}")

    try:
        from ui.home import update_streak_on_result
        await update_streak_on_result(user_id)
    except Exception:
        pass

    await _send_result(update, user_id, headlines, topic, s)


async def _send_result(update, user_id: int, headlines: str, topic: str, s: dict) -> None:
    """Отправляет хуки + панель доработки."""
    await send(
        update,
        f"🎬 *Хуки — тема:* _{topic}_\n\n{headlines}",
        parse_mode="Markdown",
    )
    await send(update, "Докрути под себя 👇", reply_markup=_edit_panel_kb())

    # Voice feedback (fatigue guard)
    try:
        from db import get_results as _gr, get_stats as _gs
        from voice_learner import get_voice_stats, should_show_voice_feedback
        _recent = await _gr(user_id, limit=1)
        if _recent:
            await asyncio.sleep(0.5)
            _vs        = await get_voice_stats(user_id)
            _total     = _vs.get("total_signals", 0)
            _gen_count = (await _gs(user_id)).get("total", 1)
            if should_show_voice_feedback(_total, _gen_count):
                await send(update, "Звучит как твой голос?",
                           reply_markup=voice_feedback_kb(_recent[0]["id"]))
    except Exception:
        pass

    from ui.cabinet import maybe_show_upsell
    await maybe_show_upsell(update, user_id)

    from flows.proactive import maybe_suggest_next
    await maybe_suggest_next(update, user_id, "reels_short", delay=1.2)


# ── Панель доработки ──────────────────────────────────────────────────────────

async def _apply_edit(update: Update, user_id: int, edit_key: str) -> None:
    """
    Универсальный обработчик правок из панели.
    Берёт last_result из сессии, применяет инструкцию, возвращает новые хуки.
    """
    s = await get_agent_session(user_id, _RS_KEY)
    if not s or not s.get("last_result"):
        await send(
            update,
            "Сессия устарела — начни заново.",
            reply_markup=kb(["🎬 Новые хуки|flow_reels_short", "← Меню|menu_main"]),
        )
        return

    instruction = _REFINE_PROMPTS.get(edit_key, "Улучши хуки.")
    current     = s["last_result"]
    topic       = s.get("topic", "")
    profile     = await get_profile(user_id)

    system = (
        f"Ты — редактор хуков для Instagram Reels. "
        f"Ниша автора: {profile.get('niche', '')}. "
        f"Аудитория: {profile.get('audience', '')}. "
        f"Тон: {profile.get('tone', 'живой')}.\n\n"
        f"Текущие хуки:\n{current}"
    )

    status = await update.effective_chat.send_message("Переписываю...")
    try:
        new_result = await complete(system, instruction, temperature=0.8, max_tokens=1500)
    except Exception as e:
        logger.error(f"[reels] edit {edit_key} failed: {e}")
        await safe_delete(status)
        await send(update, "Не получилось — попробуй ещё раз 🔁", reply_markup=_edit_panel_kb())
        return

    await safe_delete(status)

    if not new_result or not new_result.strip():
        await send(update, "Пустой ответ — попробуй ещё раз.", reply_markup=_edit_panel_kb())
        return

    # Сохраняем новую версию
    s["last_result"] = new_result
    await save_agent_session(user_id, _RS_KEY, s)

    try:
        await save_result(user_id, "reels_short", "Хуки для рилса (правка)",
                          f"Тема: {topic}\n\n{new_result}")
    except Exception:
        pass

    await send(
        update,
        f"🎬 *Хуки — тема:* _{topic}_\n\n{new_result}",
        parse_mode="Markdown",
    )
    await send(update, "Ещё докрутить?", reply_markup=_edit_panel_kb())


async def rs_edit_softer(update: Update, user_id: int) -> None:
    await _apply_edit(update, user_id, "softer")


async def rs_edit_bolder(update: Update, user_id: int) -> None:
    await _apply_edit(update, user_id, "bolder")


async def rs_edit_top5(update: Update, user_id: int) -> None:
    await _apply_edit(update, user_id, "top5")


async def rs_edit_style(update: Update, user_id: int) -> None:
    """Показываем выбор стиля."""
    await send(update, "Выбери стиль хуков 👇", reply_markup=_style_choice_kb())


async def rs_apply_style(update: Update, user_id: int, style_key: str) -> None:
    """Применяем выбранный стиль."""
    key_map = {
        "curiosity": "restyle_curiosity",
        "pain":      "restyle_pain",
        "social":    "restyle_social",
    }
    await _apply_edit(update, user_id, key_map.get(style_key, "restyle_curiosity"))


async def rs_edit_back(update: Update, user_id: int) -> None:
    """Возврат к панели доработки."""
    await send(update, "Докрути под себя 👇", reply_markup=_edit_panel_kb())


# ── Написание описания к выбранному хуку ─────────────────────────────────────

async def rs_pick_for_desc(update: Update, user_id: int) -> None:
    """Пользователь хочет написать описание к конкретному хуку."""
    s = await get_agent_session(user_id, _RS_KEY)
    if not s or not s.get("last_result"):
        await rs_start(update, user_id)
        return
    s["step"] = "pick_for_desc"
    await save_agent_session(user_id, _RS_KEY, s)
    await send(
        update,
        "Напиши *номер* хука (1–14) или скопируй текст — напишу к нему описание:",
        parse_mode="Markdown",
        reply_markup=kb(["← Назад|rs_edit_back", "← Меню|menu_main"]),
    )


async def _parse_hook_choice(text: str, headlines: str) -> str:
    """Извлекает текст хука по номеру или возвращает текст как есть."""
    chosen = text.strip()
    if chosen.isdigit():
        lines = []
        for line in headlines.split("\n"):
            clean = re.sub(r"[\*_`~]+", "", line).strip()
            if re.match(r"^\d+[\.)\s]\s*\S", clean):
                lines.append(re.sub(r"^\d+[\.)\s]\s*", "", clean).strip())
        n = int(chosen)
        if 1 <= n <= len(lines):
            return lines[n - 1]
    return chosen


async def rs_hook_chosen_for_desc(update: Update, user_id: int, text: str, s: dict) -> None:
    """Пользователь выбрал хук — переходим к деталям для описания."""
    chosen = await _parse_hook_choice(text, s.get("last_result", ""))
    s["chosen"] = chosen
    s["step"]   = "await_desc_details"
    await save_agent_session(user_id, _RS_KEY, s)
    await send(
        update,
        f"✅ *Хук выбран:*\n_{chosen}_\n\n"
        "Добавь детали для описания — или пропусти:\n"
        "_Личная история, кейс, цифры, куда ведёт рилс_",
        parse_mode="Markdown",
        reply_markup=kb(
            ["⏭ Пропустить — стандартное описание|rs_desc_skip"],
            ["← Назад|rs_edit_back", "← Меню|menu_main"],
        ),
    )


async def rs_generate_desc(update: Update, user_id: int, s: dict) -> None:
    """Генерирует описание к выбранному хуку."""
    profile = await get_profile(user_id)
    base    = build_reels_short_desc_system(
        profile.get("niche", "не указана"),
        profile.get("audience", "не указана"),
        profile.get("tone", "живой"),
    )
    system  = protect(user_id, await get_prompt(user_id, "reels_short_desc", base))
    prompt  = (
        f"Тема: {s.get('topic', '')}\n"
        f"Хук: {s.get('chosen', '')}\n"
        f"Детали: {s.get('desc_details', 'нет')}\n"
        f"CTA: {s.get('destination', 'подписаться')}"
    )
    status = await update.effective_chat.send_message("Пишу описание — хочу чтобы оно усиливало рилс...")
    try:
        result = await complete(system, prompt, max_tokens=2000, temperature=0.85)
    except Exception as e:
        logger.error(f"[reels] rs_generate_desc error: {e}")
        await safe_delete(status)
        await send(
            update,
            "Не вышло — данные сохранены, нажми 🔁",
            reply_markup=kb(["🔁 Повторить|rs_retry_desc", "← Меню|menu_main"]),
        )
        return

    await safe_delete(status)

    if not result or not result.strip():
        result = "⚠️ Пустой ответ. Попробуй снова."

    try:
        await save_result(
            user_id, "reels_short", "Рилс — описание",
            f"Хук: {s.get('chosen', '')}\n\n{result}",
        )
    except Exception as e:
        logger.warning(f"save_result rs_desc failed: {e}")

    # Сбрасываем step обратно на done — хуки по-прежнему доступны
    s["step"] = "done"
    await save_agent_session(user_id, _RS_KEY, s)

    await send(
        update,
        f"🎬 *Готово!*\n\n*Хук:*\n_{s.get('chosen', '')}_\n\n*Описание:*\n{result}",
        parse_mode="Markdown",
        reply_markup=kb(
            ["✏️ Доработать|refine_last",          "🔄 Другой вариант|regen_last"],
            ["📝 Другой хук — описание|rs_pick_for_desc"],
            ["🔁 Новая тема|flow_reels_short",      "← Меню|menu_main"],
        ),
    )

    try:
        from db import get_results as _gr, get_stats as _gs
        from voice_learner import get_voice_stats, should_show_voice_feedback
        _recent = await _gr(user_id, limit=1)
        if _recent:
            await asyncio.sleep(0.5)
            _vs        = await get_voice_stats(user_id)
            _total     = _vs.get("total_signals", 0)
            _gen_count = (await _gs(user_id)).get("total", 1)
            if should_show_voice_feedback(_total, _gen_count):
                await send(update, "Звучит как твой голос?",
                           reply_markup=voice_feedback_kb(_recent[0]["id"]))
    except Exception:
        pass

    from ui.cabinet import maybe_show_upsell
    await maybe_show_upsell(update, user_id)


# ── Роутер текстовых сообщений ────────────────────────────────────────────────

async def route_text(update: Update, user_id: int, text: str, s: dict) -> None:
    step = s.get("step", "")
    if step == "await_topic":
        await rs_gen(update, user_id, text)
    elif step == "pick_for_desc":
        await rs_hook_chosen_for_desc(update, user_id, text, s)
    elif step == "await_desc_details":
        s["desc_details"] = text
        await save_agent_session(user_id, _RS_KEY, s)
        await rs_generate_desc(update, user_id, s)
    elif step == "done":
        # Текст после готовых хуков — трактуем как новую тему
        await rs_gen(update, user_id, text)
    elif step == "generating":
        await send(update, "Генерирую — подожди немного ⏳",
                   reply_markup=kb(["← Меню|menu_main"]))
    else:
        await send(update, "Начнём заново?",
                   reply_markup=kb(["🎬 Новые хуки|flow_reels_short", "← Меню|menu_main"]))


# ── Обратная совместимость (старые callback_data из сохранённых сессий) ───────

async def rs_regen(update: Update, user_id: int) -> None:
    """Перегенерировать хуки с той же темой."""
    s = await get_agent_session(user_id, _RS_KEY)
    if s and s.get("topic"):
        await rs_gen(update, user_id, s["topic"])
    else:
        await rs_start(update, user_id)
