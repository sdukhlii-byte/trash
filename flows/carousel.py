"""
flows/carousel.py — v3: тема → 2 вопроса → карусель → панель доработки.

Принцип: движок (формат, триггер, заголовок) работает автоматически под профиль.
Пользователь видит результат сразу — и может докрутить его через панель правок.

Флоу:
  1. car_start         — запрашиваем тему
  2. car_quick_interview — 2 вопроса для внутрянки (или сразу генерация)
  3. car_generate       — готовая карусель
  4. Панель доработки:
       ✏️ Изменить заголовок
       🔄 Другой формат
       💪 Усилить триггер / Сменить триггер
       ➕ Добавить слайд
       🎨 Другой тон
       ✂️ Сократить
"""
import asyncio
import logging
import random

from telegram import Update

from db import (
    get_agent_session, save_agent_session, clear_agent_session,
    get_profile, save_result,
)
from llm import complete, chat, generate_from_history, complete_long
from security import protect
from prompt_editor import get_prompt
from utils import send, edit, kb, safe_delete
from voice_learner import build_voice_context, voice_feedback_kb
from config import (
    CAROUSEL_FORMATS_4, CAROUSEL_TRIGGERS_20,
    CAROUSEL_INTERVIEWER, build_carousel_system,
)

logger = logging.getLogger(__name__)



_CAR_KEY = "carousel_flow"

# Системный промпт для автовыбора формата+триггера под профиль
_AUTO_PICK_SYSTEM = """
Ты — эксперт по Instagram-каруселям. По теме и профилю автора выбери:
1. Лучший формат из: instruction (инструкция/шаги), list (список/ошибки/идеи), test (тест), prediction (предсказание)
2. Лучший эмоциональный триггер из: fear, curiosity, social_proof, transformation, authority, urgency, belonging, shame, aspiration, nostalgia

Ответь строго в формате JSON без пояснений:
{"format": "list", "trigger": "fear", "headline": "Заголовок карусели до 10 слов"}
"""

# Промпт для быстрого интервью — 1 вопрос за раз
_QUICK_INTERVIEW_SYSTEM = """
Ты — Мира, помощник по контенту. Собираешь "внутрянку" для карусели.
Задай ОДИН конкретный вопрос чтобы получить личный опыт, цифры или историю автора.
Вопрос короткий (1-2 предложения). Без вступлений типа "Отлично!" или "Хорошо!".
После 2-го ответа пользователя верни только текст [READY] — без других слов.
"""

# Промпты для панели доработки
_REFINE_PROMPTS = {
    "headline": (
        "Перепиши только заголовок (первый слайд) карусели. "
        "Сделай его сильнее: добавь конкретику, цифру или интригу. "
        "Верни полную карусель с новым заголовком."
    ),
    "format_instruction": (
        "Переформатируй карусель в формат ИНСТРУКЦИЯ/ШАГИ: "
        "каждый слайд = один чёткий шаг с действием. "
        "Сохрани тему и внутрянку."
    ),
    "format_list": (
        "Переформатируй карусель в формат СПИСОК: "
        "каждый слайд = один пункт из списка (ошибка/идея/факт). "
        "Сохрани тему и внутрянку."
    ),
    "trigger_stronger": (
        "Усиль эмоциональный триггер карусели: "
        "сделай заголовки острее, добавь конфликт или боль аудитории. "
        "Не меняй структуру — только формулировки."
    ),
    "trigger_curiosity": (
        "Перепиши заголовки слайдов через триггер ЛЮБОПЫТСТВО: "
        "недосказанность, интрига, 'а что если'. Верни полную карусель."
    ),
    "trigger_social": (
        "Перепиши заголовки слайдов через триггер СОЦИАЛЬНОЕ ДОКАЗАТЕЛЬСТВО: "
        "истории других людей, 'она сделала', 'они не знали'. Верни полную карусель."
    ),
    "add_slide": (
        "Добавь один новый слайд в карусель — он должен быть самым сильным. "
        "Верни полную карусель с добавленным слайдом."
    ),
    "tone_softer": (
        "Смягчи тон карусели: убери давление и остроту, сделай текст теплее и поддерживающим. "
        "Сохрани структуру."
    ),
    "tone_bolder": (
        "Сделай тон карусели смелее и провокационнее: прямые утверждения, без воды. "
        "Сохрани структуру."
    ),
    "shorten": (
        "Сократи карусель: убери слабые слайды, оставь только самые сильные (5-7 слайдов). "
        "Каждый слайд — ударный."
    ),
}

# Статусные сообщения
_THINKING_MSGS = [
    "Изучаю тему...",
    "Подбираю структуру под твою нишу...",
    "Думаю что зацепит аудиторию...",
]
_GEN_MSGS = [
    "Пишу карусель — занимает около минуты...",
    "Строю слайды — почти готово...",
    "Собираю карусель...",
]


# ── Утилиты ───────────────────────────────────────────────────────────────────

def _edit_panel_kb(completed: set | None = None):
    """
    Панель доработки карусели.
    completed: завершённые действия — исчезают из клавиатуры.
    """
    done = completed or set()
    rows = []

    row1 = []
    if "headline" not in done:
        row1.append("✏️ Заголовок|car_edit_headline")
    row1.append("🔄 Формат|car_edit_format")
    if row1:
        rows.append(row1)

    row2 = []
    if "trigger_stronger" not in done:
        row2.append("💪 Триггер|car_edit_trigger")
    if "add_slide" not in done:
        row2.append("➕ Слайд|car_edit_add_slide")
    if row2:
        rows.append(row2)

    row3 = []
    if "tone_softer" not in done:
        row3.append("🎨 Мягче|car_edit_softer")
    if "tone_bolder" not in done:
        row3.append("🔥 Жёстче|car_edit_bolder")
    if row3:
        rows.append(row3)

    if "shorten" not in done:
        rows.append(["✂️ Сократить|car_edit_shorten"])

    rows.append(["🔁 Новая карусель|flow_carousel", "← Меню|menu_main"])
    return kb(*rows)


def _trigger_choice_kb():
    """Выбор триггера для смены через панель доработки."""
    return kb(
        ["😨 Страх|car_trig_fear",              "🤔 Любопытство|car_trig_curiosity"],
        ["👥 Соц. доказательство|car_trig_social", "✨ Трансформация|car_trig_transform"],
        ["← Назад|car_edit_back",               "← Меню|menu_main"],
    )


def _format_choice_kb():
    """Выбор формата для смены через панель доработки."""
    return kb(
        ["🪜 Инструкция / шаги|car_fmt_instruction",
         "📋 Список / ошибки|car_fmt_list"],
        ["← Назад|car_edit_back", "← Меню|menu_main"],
    )


async def _auto_pick(topic: str, profile: dict) -> dict:
    """
    Автоматически выбирает формат, триггер и заголовок под профиль.
    Возвращает dict с ключами: format, trigger, headline.
    При ошибке — безопасные дефолты.
    """
    prompt = (
        f"Ниша: {profile.get('niche', 'не указана')}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\n"
        f"Тема: {topic}"
    )
    try:
        import json as _json
        raw = await complete(_AUTO_PICK_SYSTEM, prompt, temperature=0.4)
        # Вырезаем JSON из ответа даже если есть лишний текст
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = _json.loads(raw[start:end])
            return {
                "format":   data.get("format", "list"),
                "trigger":  data.get("trigger", "curiosity"),
                "headline": data.get("headline", topic),
            }
    except Exception as e:
        logger.warning(f"[carousel] auto_pick failed: {e}")
    return {"format": "list", "trigger": "curiosity", "headline": topic}


# ── Основной флоу ─────────────────────────────────────────────────────────────

async def car_start(update: Update, user_id: int) -> None:
    """Шаг 1: запрашиваем тему."""
    await clear_agent_session(user_id, _CAR_KEY)
    await save_agent_session(user_id, _CAR_KEY, {"step": "await_topic"})
    await send(
        update,
        "🎠 *Карусель*\n\nНапиши тему одним предложением:\n\n"
        "_Например: «Почему мои приседания не работали 3 года»_",
        parse_mode="Markdown",
        reply_markup=kb(["← Меню|menu_main"]),
    )


async def car_topic_received(update: Update, user_id: int, topic: str, s: dict) -> None:
    """
    Шаг 2: получили тему.
    Мира сама выбирает формат+триггер, задаёт первый вопрос для внутрянки.
    """
    profile = await get_profile(user_id)
    status  = await update.effective_chat.send_message(random.choice(_THINKING_MSGS))

    picked = await _auto_pick(topic, profile)

    # Первый вопрос интервью
    ctx = (
        f"Тема карусели: {topic}\n"
        f"Формат: {picked['format']}\n"
        f"Триггер: {picked['trigger']}\n"
        f"Ниша: {profile.get('niche', '')}\n"
        f"Аудитория: {profile.get('audience', '')}"
    )
    _tt1 = asyncio.create_task(typing_loop(update.effective_chat))
    try:
        car_int  = protect(user_id, await get_prompt(user_id, "carousel_interviewer", _QUICK_INTERVIEW_SYSTEM))
        first_q  = await complete(car_int, f"{ctx}\n\nЗадай первый вопрос.", temperature=0.4)
    except Exception as e:
        logger.error(f"[carousel] first_q failed: {e}")
        first_q = "Расскажи одну реальную историю или пример из своего опыта по этой теме."
    finally:
        _tt1.cancel()

    await safe_delete(status)

    s.update({
        "step": "interview",
        "topic": topic,
        "fmt": picked["format"],
        "trigger": picked["trigger"],
        "headline": picked["headline"],
        "interview_history": [
            {"role": "user",      "content": ctx},
            {"role": "assistant", "content": first_q},
        ],
        "q_count": 1,
    })
    await save_agent_session(user_id, _CAR_KEY, s)

    clean_q = first_q.replace("[READY]", "").strip()
    await send(
        update,
        f"{clean_q}\n\n_Вопрос 1 из 2_",
        parse_mode="Markdown",
        reply_markup=kb(
            ["⚡ Пропустить вопросы — генерируй|car_generate"],
            ["← Меню|menu_main"],
        ),
    )


async def car_interview_step(update: Update, user_id: int, text: str, s: dict) -> None:
    """Шаг 3: обрабатываем ответ на вопрос интервью."""
    ih = s.get("interview_history", [])
    ih.append({"role": "user", "content": text})
    q_count = s.get("q_count", 1)

    # После 2 вопросов — сразу генерируем
    if q_count >= 2:
        ih.append({"role": "assistant", "content": "[READY]"})
        s["interview_history"] = ih
        await save_agent_session(user_id, _CAR_KEY, s)
        await car_generate(update, user_id, s)
        return

    # Задаём второй вопрос
    _tt2 = asyncio.create_task(typing_loop(update.effective_chat))
    try:
        car_int  = protect(user_id, await get_prompt(user_id, "carousel_interviewer", _QUICK_INTERVIEW_SYSTEM))
        next_msg = await chat(ih, system=car_int, temperature=0.4)
    except Exception as e:
        logger.error(f"[carousel] interview step failed: {e}")
        _tt2.cancel()
        await send(
            update,
            "Связь прервалась — ответь ещё раз 🔁",
            reply_markup=kb(
                ["⚡ Пропустить — генерируй|car_generate"],
                ["← Меню|menu_main"],
            ),
        )
        return
    finally:
        _tt2.cancel()

    # Если модель сама решила что готова
    if "[ready]" in next_msg.lower():
        ih.append({"role": "assistant", "content": next_msg})
        s["interview_history"] = ih
        await save_agent_session(user_id, _CAR_KEY, s)
        await car_generate(update, user_id, s)
        return

    ih.append({"role": "assistant", "content": next_msg})
    s["interview_history"] = ih
    s["q_count"] = q_count + 1
    await save_agent_session(user_id, _CAR_KEY, s)

    clean_msg = next_msg.replace("[READY]", "").strip()
    await send(
        update,
        f"{clean_msg}\n\n_Вопрос 2 из 2_",
        parse_mode="Markdown",
        reply_markup=kb(
            ["⚡ Пропустить — генерируй|car_generate"],
            ["← Меню|menu_main"],
        ),
    )


async def car_generate(update: Update, user_id: int, s: dict) -> None:
    """Финальная генерация карусели."""
    profile = await get_profile(user_id)
    voice   = await build_voice_context(user_id)

    fmt     = s.get("fmt", "list")
    trigger = s.get("trigger", "curiosity")
    system  = build_carousel_system(
        fmt=fmt, trigger=trigger,
        niche=profile.get("niche", "не указана"),
        audience=profile.get("audience", "не указана"),
        tone=profile.get("tone", "нейтральный"),
    ) + voice

    ih = s.get("interview_history", [])
    if not ih:
        ih = [{"role": "user", "content": f"Тема: {s.get('topic', '')}"}]

    headline = s.get("headline", s.get("topic", ""))

    status = await update.effective_chat.send_message(random.choice(_GEN_MSGS))
    s["step"] = "generating"
    await save_agent_session(user_id, _CAR_KEY, s)

    try:
        result = await generate_from_history(
            system, ih,
            f"Заголовок карусели: «{headline}». Генерируй готовую карусель. Без вступлений.",
            model_key="claude", temperature=0.85, presence_penalty=0.3,
        )
    except Exception as e:
        logger.error(f"[carousel] generate error: {e}")
        result = None

    await safe_delete(status)

    if not result:
        s["step"] = "interview"
        await save_agent_session(user_id, _CAR_KEY, s)
        await send(
            update,
            "Генерация не прошла — твои ответы сохранены, нажми 🔁",
            reply_markup=kb(
                ["🔁 Повторить|car_generate"],
                ["← Меню|menu_main"],
            ),
        )
        return

    # Сохраняем результат и сессию для панели доработки
    fmt_names    = {k: n for k, (e, n) in CAROUSEL_FORMATS_4.items()}
    trigger_names = {k: n for k, (e, n, _) in CAROUSEL_TRIGGERS_20.items()}
    fmt_label    = fmt_names.get(fmt, fmt)
    trigger_label = trigger_names.get(trigger, trigger)

    s.update({
        "step":           "done",
        "last_result":    result,
        "fmt_label":      fmt_label,
        "trigger_label":  trigger_label,
    })
    await save_agent_session(user_id, _CAR_KEY, s)

    try:
        await save_result(
            user_id, "carousel", "Карусель",
            f"Заголовок: {headline}\nФормат: {fmt_label} · Триггер: {trigger_label}\n\n{result}",
        )
    except Exception as e:
        logger.warning(f"save_result carousel failed: {e}")

    try:
        from ui.home import update_streak_on_result
        await update_streak_on_result(user_id)
    except Exception:
        pass

    # Отправляем результат
    await _send_result(update, user_id, result, headline, fmt_label, trigger_label, s)


async def _send_result(
    update, user_id: int, result: str,
    headline: str, fmt_label: str, trigger_label: str, s: dict
) -> None:
    """Отправляет карусель + панель доработки."""
    header = f"🎠 *{headline}*\n_Формат: {fmt_label} · Триггер: {trigger_label}_\n\n"
    full   = header + result
    CHUNK  = 3800

    if len(full) <= CHUNK:
        await send(update, full, parse_mode="Markdown")
    else:
        preview = full[:CHUNK].rsplit("\n", 1)[0]
        await send(update, preview + "\n\n_...продолжение ниже_", parse_mode="Markdown")
        rest = full[len(preview):]
        for chunk in [rest[i:i + 4000] for i in range(0, len(rest), 4000)]:
            await asyncio.sleep(0.3)
            await send(update, chunk, parse_mode="Markdown")

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
                    ["🎨 Мягче|car_edit_softer",    "🔥 Жёстче|car_edit_bolder"],
                    ["✂️ Сократить|car_edit_shorten", "➕ Слайд|car_edit_add_slide"],
                    ["💪 Триггер|car_edit_trigger",  "🎨 Формат|car_edit_format"],
                    ["✏️ Доработать свободно|refine_last", "🔄 Другой вариант|regen_last"],
                    ["← Меню|menu_main"],
                ])
                await send(update, f"Звучит как твой голос?{_hint}",
                           parse_mode="Markdown", reply_markup=combined_kb)
            else:
                await send(update, "Докрути под себя 👇", reply_markup=_edit_panel_kb())
        else:
            await send(update, "Докрути под себя 👇", reply_markup=_edit_panel_kb())
    except Exception:
        await send(update, "Докрути под себя 👇", reply_markup=_edit_panel_kb())

    # Отложенная proactive-подсказка — покажется при следующем меню
    try:
        from flows.proactive import schedule_proactive_hint
        await schedule_proactive_hint(user_id, "carousel")
    except Exception:
        pass


# ── Панель доработки ──────────────────────────────────────────────────────────

async def _apply_edit(update: Update, user_id: int, edit_key: str) -> None:
    """
    Универсальный обработчик правок из панели.
    Берёт last_result из сессии, применяет инструкцию, возвращает новый результат.
    """
    s = await get_agent_session(user_id, _CAR_KEY)
    if not s or not s.get("last_result"):
        await send(
            update,
            "Сессия устарела — начни карусель заново.",
            reply_markup=kb(["🎠 Новая карусель|flow_carousel", "← Меню|menu_main"]),
        )
        return

    instruction = _REFINE_PROMPTS.get(edit_key, "Улучши карусель.")
    current     = s["last_result"]
    headline    = s.get("headline", "")
    profile     = await get_profile(user_id)

    system = (
        f"Ты — редактор Instagram-каруселей. "
        f"Ниша автора: {profile.get('niche', '')}. "
        f"Аудитория: {profile.get('audience', '')}. "
        f"Тон: {profile.get('tone', 'нейтральный')}.\n\n"
        f"Текущая карусель:\n{current}"
    )

    status = await update.effective_chat.send_message("Переписываю...")
    _tt3 = asyncio.create_task(typing_loop(update.effective_chat))
    try:
        new_result = await complete_long(system, instruction, model_key="claude", temperature=0.8)
    except Exception as e:
        logger.error(f"[carousel] edit {edit_key} failed: {e}")
        _tt3.cancel()
        await safe_delete(status)
        await send(
            update,
            "Не получилось — попробуй ещё раз 🔁",
            reply_markup=_edit_panel_kb(),
        )
        return
    finally:
        _tt3.cancel()

    await safe_delete(status)

    if not new_result or not new_result.strip():
        await send(update, "Пустой ответ — попробуй ещё раз.", reply_markup=_edit_panel_kb())
        return

    # Сохраняем новую версию + трекаем завершённые
    _car_completed = set(s.get("completed_actions", []))
    _car_completed.add(edit_key)
    s["last_result"] = new_result
    s["completed_actions"] = list(_car_completed)
    await save_agent_session(user_id, _CAR_KEY, s)

    try:
        fmt_label    = s.get("fmt_label", "")
        trigger_label = s.get("trigger_label", "")
        await save_result(
            user_id, "carousel", "Карусель (правка)",
            f"Заголовок: {headline}\nФормат: {fmt_label} · Триггер: {trigger_label}\n\n{new_result}",
        )
    except Exception:
        pass

    # Отправляем
    full = f"🎠 *{headline}*\n\n{new_result}"
    CHUNK = 3800
    if len(full) <= CHUNK:
        await send(update, full, parse_mode="Markdown")
    else:
        preview = full[:CHUNK].rsplit("\n", 1)[0]
        await send(update, preview + "\n\n_...продолжение ниже_", parse_mode="Markdown")
        rest = full[len(preview):]
        for chunk in [rest[i:i + 4000] for i in range(0, len(rest), 4000)]:
            await asyncio.sleep(0.3)
            await send(update, chunk, parse_mode="Markdown")

    await send(update, "Ещё докрутить?", reply_markup=_edit_panel_kb(completed=_car_completed))


async def car_edit_headline(update: Update, user_id: int) -> None:
    await _apply_edit(update, user_id, "headline")


async def car_edit_add_slide(update: Update, user_id: int) -> None:
    await _apply_edit(update, user_id, "add_slide")


async def car_edit_shorten(update: Update, user_id: int) -> None:
    await _apply_edit(update, user_id, "shorten")


async def car_edit_softer(update: Update, user_id: int) -> None:
    await _apply_edit(update, user_id, "tone_softer")


async def car_edit_bolder(update: Update, user_id: int) -> None:
    await _apply_edit(update, user_id, "tone_bolder")


async def car_edit_trigger_stronger(update: Update, user_id: int) -> None:
    await _apply_edit(update, user_id, "trigger_stronger")


async def car_edit_format(update: Update, user_id: int) -> None:
    """Показываем выбор формата."""
    await send(update, "Выбери формат 👇", reply_markup=_format_choice_kb())


async def car_edit_trigger(update: Update, user_id: int) -> None:
    """Показываем выбор триггера."""
    await send(update, "Выбери триггер 👇", reply_markup=_trigger_choice_kb())


async def car_apply_format(update: Update, user_id: int, fmt_key: str) -> None:
    """Применяем выбранный формат."""
    key_map = {"instruction": "format_instruction", "list": "format_list"}
    await _apply_edit(update, user_id, key_map.get(fmt_key, "format_list"))


async def car_apply_trigger(update: Update, user_id: int, trig_key: str) -> None:
    """Применяем выбранный триггер."""
    key_map = {
        "fear":      "trigger_stronger",
        "curiosity": "trigger_curiosity",
        "social":    "trigger_social",
        "transform": "trigger_stronger",
    }
    await _apply_edit(update, user_id, key_map.get(trig_key, "trigger_stronger"))


async def car_edit_back(update: Update, user_id: int) -> None:
    """Возврат к панели доработки."""
    await send(update, "Докрути под себя 👇", reply_markup=_edit_panel_kb())


# ── Роутер текстовых сообщений ────────────────────────────────────────────────

async def route_text(update: Update, user_id: int, text: str, s: dict) -> None:
    step = s.get("step", "")
    if step == "await_topic":
        await car_topic_received(update, user_id, text, s)
    elif step == "interview":
        await car_interview_step(update, user_id, text, s)
    elif step == "done":
        # Пользователь пишет текст после готовой карусели — трактуем как правку
        s_new = s.copy()
        s_new["step"] = "interview"
        # Добавляем текст как уточнение и перегенерируем
        ih = s.get("interview_history", [])
        ih.append({"role": "user", "content": f"Правка: {text}"})
        s_new["interview_history"] = ih
        await save_agent_session(user_id, _CAR_KEY, s_new)
        await car_generate(update, user_id, s_new)
    elif step == "generating":
        await send(
            update,
            "Генерирую — подожди немного ⏳",
            reply_markup=kb(["← Меню|menu_main"]),
        )
    else:
        await send(
            update,
            "Начнём заново?",
            reply_markup=kb(["🎠 Новая карусель|flow_carousel", "← Меню|menu_main"]),
        )


# ── Обратная совместимость с callbacks.py ────────────────────────────────────
# Старые callback_data которые могут прийти из сохранённых сессий

async def car_pick(update: Update, user_id: int) -> None:
    """Устаревший шаг pick_headline — редиректим на панель доработки."""
    s = await get_agent_session(user_id, _CAR_KEY)
    if s and s.get("last_result"):
        await send(update, "Докрути под себя 👇", reply_markup=_edit_panel_kb())
    else:
        await car_start(update, user_id)


def carousel_format_kb():
    """Оставляем для обратной совместимости с callbacks.py."""
    return _format_choice_kb()


def carousel_trigger_kb():
    """Оставляем для обратной совместимости с callbacks.py."""
    return _trigger_choice_kb()
