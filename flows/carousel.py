"""
flows/carousel.py — flow «Каруселькин»: тема → тренд → 20 заголовков → формат → триггер → интервью → карусель.

Изменения v2:
- Статусные сообщения с голосом Миры
- Ошибки с контекстом ("твои ответы сохранены")
- safe_delete вместо bare except: pass
"""
import asyncio
import logging

from telegram import Update

from db import (
    get_agent_session, save_agent_session, clear_agent_session,
    get_profile, save_result,
)
from llm import complete, chat, generate_from_history
from security import protect
from prompt_editor import get_prompt
from utils import send, edit, kb, safe_delete
from voice_learner import build_voice_context, voice_feedback_kb
from config import (
    CAROUSEL_FORMATS_4, CAROUSEL_TRIGGERS_20,
    CAROUSEL_TREND_SYSTEM, CAROUSEL_HEADLINE_SYSTEM,
    CAROUSEL_INTERVIEWER, build_carousel_system,
)

logger = logging.getLogger(__name__)

_CAR_KEY = "carousel_flow"

_STATUS_TREND    = "Смотрю на потенциал темы..."
_STATUS_HEADLINE = "Собираю 20 заголовков — ищу что останавливает скролл..."
_STATUS_FIRST_Q  = "Думаю с чего начать интервью..."
_STATUS_GEN      = "Пишу карусель — занимает около минуты..."


def carousel_format_kb():
    rows = [[f"{e} {n}|cfmt_{k}"] for k, (e, n) in CAROUSEL_FORMATS_4.items()]
    rows.append(["← Меню|menu_main"])
    return kb(*rows)


def carousel_trigger_kb():
    items = list(CAROUSEL_TRIGGERS_20.items())
    rows = []
    for i in range(0, len(items), 2):
        pair = items[i:i + 2]
        rows.append([f"{e} {n}|ctrig_{k}" for k, (e, n, _) in pair])
    rows.append(["← Формат|carousel_fmt_back", "← Меню|menu_main"])
    return kb(*rows)


async def car_start(update: Update, user_id: int) -> None:
    await clear_agent_session(user_id, _CAR_KEY)
    await save_agent_session(user_id, _CAR_KEY, {"step": "await_topic"})
    caption = (
        "🎠 *Карусель*\n\nНапиши тему одним предложением:\n\n"
        "_Например: «5 ошибок при запуске рекламы»_"
    )
    await send(update, caption, parse_mode="Markdown", reply_markup=kb(["← Меню|menu_main"]))


async def car_check_trend(update: Update, user_id: int, topic: str, s: dict) -> None:
    profile = await get_profile(user_id)
    prompt = (
        f"Ниша: {profile.get('niche', 'не указана')}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\nТема: {topic}"
    )
    status = await update.effective_chat.send_message(_STATUS_TREND)
    trend_sys = protect(user_id, await get_prompt(user_id, "carousel_trend", CAROUSEL_TREND_SYSTEM))
    try:
        trend = await complete(trend_sys, prompt, temperature=0.4)
    except Exception:
        trend = "Не удалось проверить. Продолжаем."
    await safe_delete(status)

    s.update({"step": "trend_shown", "topic": topic, "trend": trend})
    await save_agent_session(user_id, _CAR_KEY, s)
    await send(
        update,
        f"📊 *Оценка темы*\n\n{trend}\n\n_Можешь сменить тему или продолжить с этой._",
        parse_mode="Markdown",
        reply_markup=kb(
            ["✅ Всё равно сделай|car_headlines", "✏️ Изменить тему|car_change_topic"],
            ["← Меню|menu_main"],
        ),
    )


async def car_gen_headlines(update: Update, user_id: int, s: dict) -> None:
    profile = await get_profile(user_id)
    prompt = (
        f"Ниша: {profile.get('niche', '')}\n"
        f"Аудитория: {profile.get('audience', '')}\nТема: {s['topic']}"
    )
    status = await update.effective_chat.send_message(_STATUS_HEADLINE)
    try:
        hl_sys = protect(user_id, await get_prompt(user_id, "carousel_headlines", CAROUSEL_HEADLINE_SYSTEM))
        headlines = await complete(hl_sys, prompt, temperature=0.85)
    except Exception as e:
        logger.error(f"[carousel] gen_headlines error: {e}")
        await safe_delete(status)
        await send(
            update,
            "Связь прервалась — нажми ещё раз 🔁",
            reply_markup=kb(["🔁 Повторить|car_headlines", "← Меню|menu_main"]),
        )
        return
    await safe_delete(status)

    if not headlines or not headlines.strip():
        headlines = "⚠️ Не удалось сгенерировать заголовки. Нажми «Повторить»."

    s.update({"step": "pick_headline", "headlines": headlines})
    await save_agent_session(user_id, _CAR_KEY, s)
    await send(
        update,
        f"🎯 *20 заголовков*\n\n{headlines}",
        parse_mode="Markdown",
        reply_markup=kb(
            ["✅ Выбрать заголовок|car_pick_headline", "🔄 Перегенерировать|car_headlines"],
            ["← Меню|menu_main"],
        ),
    )


async def car_pick(update: Update, user_id: int) -> None:
    s = await get_agent_session(user_id, _CAR_KEY)
    if not s:
        return await car_start(update, user_id)
    s["step"] = "enter_headline"
    await save_agent_session(user_id, _CAR_KEY, s)
    await send(
        update,
        "Напиши *номер* (1–20) или скопируй заголовок:",
        parse_mode="Markdown",
        reply_markup=kb(["← Меню|menu_main"]),
    )


async def car_headline_chosen(update: Update, user_id: int, text: str, s: dict) -> None:
    import re
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
    s["chosen_headline"] = chosen
    s["step"] = "pick_format"
    await save_agent_session(user_id, _CAR_KEY, s)
    await send(
        update,
        f"✅ *Заголовок:*\n_{chosen}_\n\nВыбери формат 👇",
        parse_mode="Markdown",
        reply_markup=carousel_format_kb(),
    )


async def car_format_chosen(update: Update, user_id: int, fmt: str, s: dict) -> None:
    e, n = CAROUSEL_FORMATS_4[fmt]
    s.update({"fmt": fmt, "step": "pick_trigger"})
    await save_agent_session(user_id, _CAR_KEY, s)
    await send(update, f"*{e} {n}*\n\nВыбери триггер 👇",
               parse_mode="Markdown", reply_markup=carousel_trigger_kb())


async def car_trigger_chosen(update: Update, user_id: int, trigger: str, s: dict) -> None:
    te, tn, _ = CAROUSEL_TRIGGERS_20[trigger]
    fe, fn = CAROUSEL_FORMATS_4.get(s.get("fmt", "list"), ("📋", "Список"))
    profile = await get_profile(user_id)
    ctx = (
        f"Тема: {s.get('chosen_headline', s.get('topic', ''))}\n"
        f"Формат: {fn}\nТриггер: {tn}\n"
        f"Ниша: {profile.get('niche', '')}\nАудитория: {profile.get('audience', '')}"
    )

    status = await update.effective_chat.send_message(_STATUS_FIRST_Q)
    try:
        car_int = protect(user_id, await get_prompt(user_id, "carousel_interviewer", CAROUSEL_INTERVIEWER))
        first_q = await complete(car_int, f"{ctx}\n\nЗадай первый вопрос для сбора внутрянки.",
                                 temperature=0.4)
    except Exception as e:
        logger.error(f"[carousel] first question failed: {e}")
        await safe_delete(status)
        await send(
            update,
            "Что-то сломалось — нажми ещё раз 🔁",
            reply_markup=kb(["🔁 Выбрать триггер снова|car_fmt_back_to_trigger", "← Меню|menu_main"]),
        )
        return
    await safe_delete(status)

    ih = [{"role": "user", "content": ctx}, {"role": "assistant", "content": first_q}]
    s.update({"trigger": trigger, "step": "interview", "interview_history": ih, "q_count": 1})
    await save_agent_session(user_id, _CAR_KEY, s)

    clean_first_q = first_q.replace("[READY]", "").replace("[ready]", "").strip()
    await send(
        update,
        f"🎙 *Сбор внутрянки*\n_{tn}_ · _{fn}_\n\n{clean_first_q}\n\n_Вопрос 1 из 5_",
        parse_mode="Markdown",
        reply_markup=kb(["⏭ Пропустить, генерируй|car_generate", "← Меню|menu_main"]),
    )


async def car_interview_step(update: Update, user_id: int, text: str, s: dict) -> None:
    ih = s["interview_history"]
    ih.append({"role": "user", "content": text})
    try:
        car_int = protect(user_id, await get_prompt(user_id, "carousel_interviewer", CAROUSEL_INTERVIEWER))
        next_msg = await chat(ih, system=car_int, temperature=0.4)
    except Exception as e:
        logger.error(f"[carousel] interview LLM error: {e}")
        await send(
            update,
            "Связь прервалась — ответь ещё раз 🔁",
            reply_markup=kb(["⏭ Пропустить, генерируй|car_generate", "← Меню|menu_main"]),
        )
        return

    ih.append({"role": "assistant", "content": next_msg})
    s["interview_history"] = ih
    s["q_count"] = s.get("q_count", 0) + 1
    await save_agent_session(user_id, _CAR_KEY, s)

    clean_msg = next_msg.replace("[READY]", "").replace("[ready]", "").strip()
    if "[ready]" in next_msg.lower() or s["q_count"] >= 5:
        await send(update, clean_msg, parse_mode="Markdown")
        await car_generate(update, user_id, s)
    else:
        await send(
            update,
            clean_msg,
            parse_mode="Markdown",
            reply_markup=kb(["⏭ Достаточно, генерируй|car_generate", "← Меню|menu_main"]),
        )


async def car_generate(update: Update, user_id: int, s: dict) -> None:
    profile = await get_profile(user_id)
    system = build_carousel_system(
        fmt=s.get("fmt", "list"), trigger=s.get("trigger", "fear"),
        niche=profile.get("niche", "не указана"),
        audience=profile.get("audience", "не указана"),
        tone=profile.get("tone", "нейтральный"),
    )
    fe, fn = CAROUSEL_FORMATS_4.get(s.get("fmt", "list"), ("", ""))
    te, tn, _ = CAROUSEL_TRIGGERS_20.get(s.get("trigger", "fear"), ("", "", ""))
    ih = s.get("interview_history", [])
    if not ih:
        ih = [{"role": "user", "content": f"Тема карусели: {s.get('chosen_headline', '')}"}]

    status = await update.effective_chat.send_message(
        f"🎠 {_STATUS_GEN}", parse_mode="Markdown"
    )
    try:
        result = await generate_from_history(
            system, ih, "Генерируй готовую карусель. Без вступлений.",
            model_key="claude", temperature=0.85, presence_penalty=0.3,
        )
    except Exception as e:
        logger.error(f"[carousel] generate error: {e}")
        result = None
    await safe_delete(status)

    if not result:
        await send(
            update,
            "Генерация не прошла — твои ответы сохранены, нажми 🔁",
            reply_markup=kb(["🔁 Повторить|car_generate", "← Меню|menu_main"]),
        )
        return

    await clear_agent_session(user_id, _CAR_KEY)

    try:
        headline = s.get("chosen_headline", s.get("topic", ""))
        await save_result(
            user_id, "carousel", "Каруселькин",
            f"Заголовок: {headline}\nФормат: {fn} · Триггер: {tn}\n\n{result}",
        )
    except Exception as e:
        logger.warning(f"save_result carousel failed: {e}")
    try:
        from ui.home import update_streak_on_result
        await update_streak_on_result(user_id)
    except Exception:
        pass

    header = f"🎠 *Карусель готова*\n{te} _{tn}_ · {fe} _{fn}_\n\n"
    CHUNK = 3800
    full = header + result
    if len(full) <= CHUNK:
        await send(update, full, parse_mode="Markdown")
    else:
        preview = full[:CHUNK].rsplit("\n", 1)[0]
        await send(update, preview + "\n\n_...продолжение ниже_", parse_mode="Markdown")
        rest = full[len(preview):]
        for chunk in [rest[i:i + 4000] for i in range(0, len(rest), 4000)]:
            await asyncio.sleep(0.3)
            await send(update, chunk, parse_mode="Markdown")

    # Voice feedback кнопка
    try:
        from db import get_results as _gr
        _recent = await _gr(user_id, limit=1)
        _rid = _recent[0]["id"] if _recent else 0
    except Exception:
        _rid = 0


    await send(
        update,
        "Что дальше?",
        reply_markup=kb(
            ["✏️ Доработать|refine_last",    "🔄 Другой вариант|regen_last"],
            ["🔁 Новая карусель|flow_carousel", "← Меню|menu_main"],
        ),
    )

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
    await maybe_suggest_next(update, user_id, "carousel", delay=1.2)


async def route_text(update: Update, user_id: int, text: str, s: dict) -> None:
    step = s.get("step", "")
    if step == "await_topic":
        await car_check_trend(update, user_id, text, s)
    elif step == "enter_headline":
        await car_headline_chosen(update, user_id, text, s)
    elif step == "interview":
        await car_interview_step(update, user_id, text, s)
    elif step == "trend_shown":
        await send(update, "Используй кнопки 👆",
                   reply_markup=kb(["✅ Генерируй заголовки|car_headlines",
                                    "✏️ Сменить тему|car_change_topic",
                                    "← Меню|menu_main"]))
    elif step in ("pick_headline", "pick_format", "pick_trigger"):
        prompt_map = {
            "pick_headline": ("Нажми «Выбрать заголовок» 👆",
                              kb(["✅ Выбрать заголовок|car_pick_headline",
                                  "🔄 Перегенерировать|car_headlines", "← Меню|menu_main"])),
            "pick_format":   ("Выбери формат кнопкой 👆", carousel_format_kb()),
            "pick_trigger":  ("Выбери триггер кнопкой 👆", carousel_trigger_kb()),
        }
        msg, _kb = prompt_map[step]
        await send(update, msg, reply_markup=_kb)
    else:
        await send(update, "Что-то пошло не так. Начни заново.",
                   reply_markup=kb(["🎠 Новая карусель|flow_carousel", "← Меню|menu_main"]))
