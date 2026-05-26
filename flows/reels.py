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
from utils import send, kb, safe_delete, typing_loop
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
        "Перепиши хуки в более мягком и поддерживающем тоне. "
        "ЗАЩИЩЕНО: психологический механизм каждого хука (метка в скобках) — меняй слова, не триггер. "
        "Убери давление и агрессию, сохрани суть. Верни нумерованный список с метками механизмов."
    ),
    "bolder": (
        "Сделай хуки провокационнее и острее: прямые утверждения, вызов, неудобная правда. "
        "ЗАЩИЩЕНО: разнообразие механизмов — не превращай все хуки в провокацию, каждый остаётся своим типом, просто острее. "
        "Без воды. Верни нумерованный список с метками механизмов."
    ),
    "top5": (
        "Из этих хуков выбери 5 самых сильных для ТЕСТИРОВАНИЯ. "
        "Объясни в одном предложении почему каждый работает и в какой последовательности тестировать: "
        "сначала тот который даёт данные быстрее, потом остальные. "
        "Верни нумерованный список с пояснением и рекомендованным порядком тестирования."
    ),
    "restyle_curiosity": (
        "Перепиши все хуки через триггер ЛЮБОПЫТСТВО: недосказанность, интрига, "
        "'а что если', 'мало кто знает'. Каждый — через свою версию любопытства, не все одинаково. "
        "Верни нумерованный список."
    ),
    "restyle_pain": (
        "Перепиши все хуки через триггер БОЛЬ: назови конкретную проблему, "
        "страх, ошибку которую совершает аудитория. Конкретика из ниши — не абстракция. "
        "Верни нумерованный список."
    ),
    "restyle_social": (
        "Перепиши хуки через СОЦИАЛЬНОЕ ДОКАЗАТЕЛЬСТВО: 'они сделали', "
        "'у неё получилось', конкретные истории. Разные форматы доказательства. "
        "Верни нумерованный список."
    ),
}

_THINKING_MSGS = [
    "Смотрю что останавливает скролл в твоей нише...",
    "Подбираю углы под твою аудиторию...",
    "Нахожу формулировки которые цепляют...",
]


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _edit_panel_kb(result_id: int = 0, completed: set | None = None):
    """
    Панель доработки хуков.
    completed: завершённые действия — исчезают из клавиатуры.
    Например, после 'Топ-5' эта кнопка больше не показывается.
    """
    done = completed or set()
    rid  = f"_{result_id}" if result_id else ""
    rows = []

    row1 = []
    # BUG #8 FIX: do NOT append rid to these two buttons — their handlers use exact
    # string matching ("rs_pick_for_desc", "rs_regen") and ignore result_id anyway.
    row1.append("📝 Описание к хуку|rs_pick_for_desc")
    row1.append("🔄 Перегенерировать|rs_regen")
    rows.append(row1)

    row2 = []
    if "softer" not in done:
        row2.append(f"🎨 Мягче|rs_edit_softer{rid}")
    if "bolder" not in done:
        row2.append(f"🔥 Провокационнее|rs_edit_bolder{rid}")
    if row2:
        rows.append(row2)

    row3 = []
    if "top5" not in done:
        row3.append(f"✂️ Топ-5|rs_edit_top5{rid}")
    row3.append(f"💡 Стиль|rs_edit_style{rid}")
    if row3:
        rows.append(row3)

    rows.append([f"🔁 Новая тема|flow_reels_short", f"← Меню|menu_main"])
    return kb(*rows)


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

    # Update CIP: add topic to recent_topics, store hooks for learning
    try:
        from db import add_recent_topic, get_cip, save_cip
        await add_recent_topic(user_id, topic[:100])
        cip = await get_cip(user_id)
        # Extract first few hooks to remember
        hook_lines = [l.strip() for l in headlines.split("\n")
                      if l.strip() and (l.strip()[0].isdigit() or l.strip().startswith("«"))]
        cip["hooks_that_worked"] = (cip.get("hooks_that_worked", []) + hook_lines[:3])[-30:]
        await save_cip(user_id, cip)
    except Exception:
        pass

    try:
        from ui.home import update_streak_on_result
        await update_streak_on_result(user_id)
    except Exception:
        pass

    await _send_result(update, user_id, headlines, topic, s, result_id=result_id)


async def _send_result(update, user_id: int, headlines: str, topic: str, s: dict, result_id: int = 0) -> None:
    """Отправляет хуки + панель доработки."""
    await send(
        update,
        f"🎬 *Хуки — тема:* _{topic}_\n\n{headlines}",
        parse_mode="Markdown",
    )
    # Единое сообщение: панель правок + voice feedback
    try:
        from db import get_results as _gr, get_stats as _gs
        from voice_learner import get_voice_stats, should_show_voice_feedback, voice_feedback_kb as _vfkb
        from ui.progress_bar import voice_progress_short as _vps
        _recent = await _gr(user_id, limit=1)
        if _recent:
            _rid       = _recent[0]["id"]
            _vs        = await get_voice_stats(user_id)
            _total     = _vs.get("total_signals", 0)
            _gen_count = (await _gs(user_id)).get("total", 1)
            if should_show_voice_feedback(_total, _gen_count):
                _hint = _vps(_total)
                combined_kb = _vfkb(_rid, extra_rows=[
                    ["🎨 Мягче|rs_edit_softer",   "🔥 Жёстче|rs_edit_bolder"],
                    ["🏆 Топ-5|rs_edit_top5",     "🎨 Стиль|rs_edit_style"],
                    ["✅ Выбрать хук — описание|rs_pick_for_desc"],
                    ["🔄 Другая тема|rs_regen",   "← Меню|menu_main"],
                ])
                await send(update, f"Звучит как твой голос?{_hint}",
                           parse_mode="Markdown", reply_markup=combined_kb)
            else:
                await send(update, "Докрути под себя 👇", reply_markup=_edit_panel_kb())
        else:
            await send(update, "Докрути под себя 👇", reply_markup=_edit_panel_kb())
    except Exception:
        await send(update, "Докрути под себя 👇", reply_markup=_edit_panel_kb())

    try:
        from flows.proactive import schedule_proactive_hint
        await schedule_proactive_hint(user_id, "reels_short")
    except Exception:
        pass


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
    import asyncio as _aio
    _tt = _aio.create_task(typing_loop(update.effective_chat))
    try:
        new_result = await complete(system, instruction, temperature=0.8, max_tokens=1500)
    except Exception as e:
        logger.error(f"[reels] edit {edit_key} failed: {e}")
        _tt.cancel()
        await safe_delete(status)
        await send(update, "Не получилось — попробуй ещё раз 🔁", reply_markup=_edit_panel_kb())
        return
    finally:
        _tt.cancel()

    await safe_delete(status)

    if not new_result or not new_result.strip():
        await send(update, "Пустой ответ — попробуй ещё раз.", reply_markup=_edit_panel_kb())
        return

    # Сохраняем новую версию + трекаем завершённые действия
    _rs_completed = set(s.get("completed_actions", []))
    _rs_completed.add(edit_key)
    s["last_result"] = new_result
    s["completed_actions"] = list(_rs_completed)
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
    await send(update, "Ещё докрутить?", reply_markup=_edit_panel_kb(completed=_rs_completed))


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
    """
    Извлекает чистый текст хука по номеру или из скопированного текста.
    Обрабатывает форматы: обычный список, top5 с пояснениями, хуки в кавычках.
    """
    chosen = text.strip()
    if chosen.isdigit():
        lines = []
        for line in headlines.split("\n"):
            clean = re.sub(r"[\*_`~]+", "", line).strip()
            if re.match(r"^\d+[\.)\s]\s*\S", clean):
                hook_raw = re.sub(r"^\d+[\.)\s]\s*", "", clean).strip()
                # Убираем пояснение после " — " (top5 режим: "хук — потому что...")
                hook_raw = re.split(r"\s+—\s+", hook_raw)[0].strip()
                # Убираем кавычки-ёлочки и обычные
                hook_raw = hook_raw.strip("«»\"'")
                if hook_raw:
                    lines.append(hook_raw)
        n = int(chosen)
        if 1 <= n <= len(lines):
            return lines[n - 1]
        return chosen  # число вне диапазона
    # Пользователь скопировал текст — чистим
    chosen = re.sub(r"^\d+[\.)\s]\s*", "", chosen).strip()
    chosen = re.split(r"\s+—\s+", chosen)[0].strip()
    chosen = chosen.strip("«»\"'")
    return chosen


async def rs_hook_chosen_for_desc(update: Update, user_id: int, text: str, s: dict) -> None:
    """Пользователь выбрал хук — переходим к деталям для описания."""
    chosen = await _parse_hook_choice(text, s.get("last_result", ""))

    # Защита: если chosen — просто цифра (парсинг не нашёл текст), просим повторить
    if chosen.isdigit():
        total_hooks = len([l for l in s.get("last_result", "").split("\n")
                          if re.match(r"^\d+[\.\)\s]", l.strip())])
        await send(
            update,
            f"Не нашла хук с номером {chosen}. "
            f"Хуков в списке: {total_hooks}. Напиши число от 1 до {total_hooks} или скопируй текст хука.",
            reply_markup=kb(["← Назад|rs_edit_back", "← Меню|menu_main"]),
        )
        return

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
    """Генерирует описание к выбранному хуку с соблюдением хук-контракта."""
    profile = await get_profile(user_id)
    base    = build_reels_short_desc_system(
        profile.get("niche", "не указана"),
        profile.get("audience", "не указана"),
        profile.get("tone", "живой"),
    )
    system  = protect(user_id, await get_prompt(user_id, "reels_short_desc", base))

    # Hook contract: extract the mechanism label from the chosen hook line
    # Hooks are formatted as: «текст хука» [МЕХАНИЗМ]
    chosen_hook = s.get("chosen", "")
    hook_mechanism = ""
    _mech_match = re.search(r"\[([А-ЯЁA-Z][А-ЯЁA-Za-z\-]+(?:[- ][А-ЯЁA-Za-z]+)*)\]", chosen_hook)
    if _mech_match:
        hook_mechanism = _mech_match.group(1)

    mechanism_hint = (
        f"\nПСИХОЛОГИЧЕСКИЙ МЕХАНИЗМ ХУКА: {hook_mechanism}. "
        "Описание должно либо углубить этот механизм (если он требует продолжения), "
        "либо зайти с контрастной стороны (если механизм уже сделал работу). "
        "ЗАПРЕЩЕНО: повторять механизм хука теми же словами."
    ) if hook_mechanism else ""

    prompt  = (
        f"Тема: {s.get('topic', '')}\n"
        f"Хук: {chosen_hook}\n"
        f"Детали: {s.get('desc_details', 'нет')}\n"
        f"CTA: {s.get('destination', 'подписаться')}"
        f"{mechanism_hint}"
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
        from voice_learner import get_voice_stats, should_show_voice_feedback, voice_feedback_kb as _vfkb2
        from ui.progress_bar import voice_progress_short as _vps2
        _recent2 = await _gr(user_id, limit=1)
        if _recent2:
            _vs2       = await get_voice_stats(user_id)
            _total2    = _vs2.get("total_signals", 0)
            _gc2       = (await _gs(user_id)).get("total", 1)
            if should_show_voice_feedback(_total2, _gc2):
                await send(update, f"Звучит как твой голос?{_vps2(_total2)}",
                           parse_mode="Markdown", reply_markup=_vfkb2(_recent2[0]["id"]))
    except Exception:
        pass


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
