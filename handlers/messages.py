"""
handlers/messages.py — текст, голос, фото.
"""
import asyncio
import base64
import json
import logging
import unicodedata

from telegram import Update
from telegram.ext import ContextTypes

from db import (
    get_onboarding_state, get_agent_session, save_agent_session,
    get_model, get_history, save_message, get_profile,
    kv_get, kv_set, kv_del, build_profile_ctx,
)
from llm import chat, transcribe, vision_describe, vision_chat
from security import protect
from user_state import get_user_state, has_access
from flows.onboarding import handle_onboarding
from flows import reels, carousel
from flows.misc import (
    _QI_KEY, qi_generate,
    _REFINE_KEY, refine_do,
    _PLANNER_KEY, route_planner_text,
    _STYLE_KEY, style_save_example,
)
from flows.reels import _RS_KEY, route_text as rs_route_text
from flows.carousel import _CAR_KEY, route_text as car_route_text
from ui.menu import main_menu_kb, show_menu
from ui.paywall import show_paywall
from utils import send, kb, safe_delete
from prompt_editor import get_prompt, _PE_KEY, pe_save_text
from config import CHAT_SYSTEM, MODELS
import agents as ag
from security import ADMIN_ID
from voice_learner import handle_voice_note_text

logger = logging.getLogger(__name__)

MAX_INPUT = 4000


# ── Per-user lock ─────────────────────────────────────────────────────────────
import weakref

_USER_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_USER_LOCKS_MUTEX = asyncio.Lock()


async def _get_user_lock(user_id: int) -> asyncio.Lock:
    async with _USER_LOCKS_MUTEX:
        lock = _USER_LOCKS.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _USER_LOCKS[user_id] = lock
        return lock


# ── Typing loop ────────────────────────────────────────────────────────────────

async def _typing_loop(chat_obj, stop_event: asyncio.Event) -> None:
    try:
        while not stop_event.is_set():
            try:
                await chat_obj.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# ── Text handler ───────────────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    raw = update.message.text or ""

    if raw.strip() in ("☰ Меню", "/menu"):
        state = await get_user_state(user_id)
        if not has_access(state):
            await show_paywall(update, user_id, state)
        else:
            await show_menu(update, user_id)
        return

    logger.info(f"[handle_text] user={user_id} raw={raw[:50]!r}")

    # Очистка unicode мусора
    text = "".join(
        ch for ch in raw
        if not unicodedata.category(ch).startswith("C") or ch in ("\n", "\r", "\t")
    ).strip()
    if not text:
        return

    # Защита от слишком длинных сообщений
    if len(text) > MAX_INPUT:
        text = text[:MAX_INPUT]
        await update.effective_chat.send_message(
            "⚠️ _Сообщение обрезано до 4000 символов._", parse_mode="Markdown"
        )

    lock = await _get_user_lock(user_id)
    if lock.locked():
        logger.info(f"[dedup] user={user_id} — запрос уже в обработке")
        return

    # Мгновенная реакция на сообщение — как живой персонаж
    # Ставится до обработки, пока бот "думает"
    asyncio.create_task(_react_to_message(update, text))

    async with lock:
        await _route(update, ctx, user_id, text)


async def _react_to_message(update: Update, text: str) -> None:
    """
    Умная реакция на входящее сообщение.
    Подбирает эмодзи по контексту сообщения — как живой человек.
    Ставится мгновенно, не блокирует обработку.
    """
    try:
        msg = update.message
        if not msg:
            return

        text_l = text.lower()

        # Подбираем реакцию по смыслу
        if any(w in text_l for w in ("привет", "здравствуй", "хай", "hello", "hi", "добрый")):
            emoji = "🔥"
        elif any(w in text_l for w in ("спасибо", "благодарю", "thanks", "класс", "отлично", "супер", "круто", "огонь", "💪")):
            emoji = "❤"
        elif any(w in text_l for w in ("хорошо", "окей", "ок", "понял", "поняла", "ясно", "да", "конечно")):
            emoji = "👍"
        elif any(w in text_l for w in ("нет", "не надо", "стоп", "отмена", "не хочу")):
            emoji = "🤔"
        elif any(w in text_l for w in ("помог", "получилось", "вышло", "работает", "готово", "сделала")):
            emoji = "🏆"
        elif any(w in text_l for w in ("почему", "зачем", "как", "что", "когда", "объясни", "расскажи", "?")):
            emoji = "🤔"
        elif any(w in text_l for w in ("пост", "рилс", "контент", "текст", "идея", "пишу", "напиши")):
            emoji = "✍"
        elif any(w in text_l for w in ("не работает", "ошибка", "сломал", "плохо", "ужасно", "не нравится")):
            emoji = "🤝"
        elif len(text) > 200:
            # Длинное сообщение — прочитала внимательно
            emoji = "👀"
        else:
            # По умолчанию — огонь (энергия, интерес)
            import random
            emoji = random.choice(["🔥", "❤", "👍", "⚡"])

        from ui.media import set_reaction
        await set_reaction(msg.get_bot(), msg.chat_id, msg.message_id, emoji)

    except Exception as e:
        logger.debug(f"[react] failed: {e}")


# ── Voice handler ──────────────────────────────────────────────────────────────

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = await get_user_state(user_id)
    if not has_access(state):
        await show_paywall(update, user_id, state)
        return

    # Мгновенная реакция на голосовое — показывает что услышала
    try:
        from ui.media import set_reaction
        msg = update.message
        asyncio.create_task(
            set_reaction(msg.get_bot(), msg.chat_id, msg.message_id, "🎙")
        )
    except Exception:
        pass

    status = await update.message.reply_text("Слушаю... 🎙")
    try:
        file     = await update.message.voice.get_file()
        ogg_data = await file.download_as_bytearray()
        text     = await transcribe(bytes(ogg_data))
    except Exception as e:
        logger.error(f"Whisper failed: {e}")
        await safe_delete(status)
        await send(update, "Голосовые сообщения пока недоступны. Напиши текстом 🙏")
        return

    await safe_delete(status)

    if not text or not text.strip():
        await send(update, "Не расслышала — попробуй ещё раз.")
        return

    clean = text.strip()
    await kv_set(user_id, "__voice_pending__", clean, ttl=3600)
    await send(
        update,
        f"🎙 _Расшифровала:_\n\n{clean}\n\nОтправить как запрос?",
        parse_mode="Markdown",
        reply_markup=kb(["✅ Отправить|voice_send"], ["❌ Отмена|voice_cancel"]),
    )


# ── Photo handler ──────────────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = await get_user_state(user_id)
    if not has_access(state):
        await show_paywall(update, user_id, state)
        return

    caption = (update.message.caption or "").strip()

    # Активный generic агент
    agent_key = await _detect_active_agent(user_id)
    spec = ag.get_spec(agent_key) if agent_key else None
    if spec:
        s = await ag.get_agent_session_safe(user_id, spec.key)
        if s and s.get("step") in ("await_photos", "interview", "initial"):
            await ag.handle_photo(update, user_id, spec)
            return

    # Карусель во время интервью
    car = await get_agent_session(user_id, _CAR_KEY)
    if car and car.get("step") == "interview":
        await _car_handle_photo(update, user_id, car, caption)
        return

    # Рилс-коротышка
    rs = await get_agent_session(user_id, _RS_KEY)
    if rs and rs.get("step") in ("await_topic", "await_desc_details"):
        await _rs_handle_photo(update, user_id, rs, caption)
        return

    # Универсальный vision-анализ
    await _handle_photo_universal(update, user_id, caption)


async def _handle_photo_universal(update: Update, user_id: int, caption: str) -> None:
    photo   = update.message.photo[-1]
    file    = await photo.get_file()
    content = await file.download_as_bytearray()
    b64     = base64.b64encode(content).decode()

    status = await update.effective_chat.send_message("Смотрю на фото...")
    try:
        result = await vision_describe([(b64, "image/jpeg")], question=caption)
    except Exception as e:
        logger.error(f"vision_describe error: {e}")
        await safe_delete(status)
        await send(update, "Фото не открылось — попробуй ещё раз 🔁", reply_markup=main_menu_kb())
        return
    await safe_delete(status)
    await send(update, f"🖼 {result}", reply_markup=kb(["← Меню|menu_main"]))


async def _rs_handle_photo(update, user_id, s, caption):
    photo   = update.message.photo[-1]
    file    = await photo.get_file()
    content = await file.download_as_bytearray()
    b64     = base64.b64encode(content).decode()
    step    = s.get("step", "")
    q = caption if caption else (
        "Это скриншот рилса. Опиши тему, главную мысль и крючок (hook) этого рилса."
        if step == "await_topic" else
        "Опиши детали, факты или историю, которую видишь на скриншоте."
    )
    status = await update.effective_chat.send_message("Смотрю что тут...")
    try:
        desc = await vision_describe([(b64, "image/jpeg")], question=q)
    except Exception as e:
        logger.error(f"_rs_handle_photo error: {e}")
        await safe_delete(status)
        await send(update, "Скрин не открылся — опиши тему текстом 🙏")
        return
    await safe_delete(status)
    await send(update, f"📸 _Вижу на скрине:_ {desc[:300]}...", parse_mode="Markdown")
    await rs_route_text(update, user_id, desc, s)


async def _car_handle_photo(update, user_id, s, caption):
    from config import CAROUSEL_INTERVIEWER
    photo   = update.message.photo[-1]
    file    = await photo.get_file()
    content = await file.download_as_bytearray()
    b64     = base64.b64encode(content).decode()
    ih      = s.get("interview_history", [])
    caption_text = caption if caption else "Посмотри на этот скриншот и задай следующий вопрос."
    status = await update.effective_chat.send_message("Изучаю скриншот...")
    try:
        reply = await vision_chat(
            history=ih,
            images_b64=[(b64, "image/jpeg")],
            caption=caption_text,
            system=CAROUSEL_INTERVIEWER,
            model_key="gpt4",
        )
    except Exception as e:
        logger.error(f"_car_handle_photo error: {e}")
        await safe_delete(status)
        await send(update, "Скрин не открылся — опиши словами 🙏",
                   reply_markup=kb(["⏭ Пропустить, генерируй|car_generate"]))
        return
    await safe_delete(status)
    ih.append({"role": "user", "content": caption_text + " [скриншот прикреплён]"})
    ih.append({"role": "assistant", "content": reply})
    s["interview_history"] = ih
    s["q_count"] = s.get("q_count", 0) + 1
    await save_agent_session(user_id, _CAR_KEY, s)
    clean_reply = reply.replace("[READY]", "").replace("[ready]", "").strip()
    if "[ready]" in reply.lower() or s["q_count"] >= 5:
        await send(update, clean_reply, parse_mode="Markdown")
        await carousel.car_generate(update, user_id, s)
    else:
        await send(update, clean_reply, parse_mode="Markdown",
                   reply_markup=kb(["⏭ Достаточно, генерируй|car_generate"]))


# ── Routing ────────────────────────────────────────────────────────────────────

async def _route(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                 user_id: int, text: str) -> None:
    try:
        await _route_inner(update, ctx, user_id, text)
    except Exception as e:
        logger.error(f"_route unhandled: {e}", exc_info=True)
        await send(update, "Что-то пошло не так — попробуй ещё раз или напиши /menu",
                   reply_markup=kb(["🔁 В меню|menu_main"]))


async def _route_inner(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                       user_id: int, text: str) -> None:

    # 1. Онбординг — проверяем Redis-флаг ПЕРВЫМ, до любых Postgres-запросов
    onb = await get_onboarding_state(user_id)
    if onb:
        is_intentional = onb.get("source") in ("profile_edit",)
        if not is_intentional:
            # Проверяем есть ли активный агент — только KV, без Postgres
            active_agent = await _detect_active_agent(user_id)
            if active_agent:
                # Пользователь в середине агента — онбординг прерываем
                from db import clear_onboarding_state
                await clear_onboarding_state(user_id)
            else:
                # Онбординг активен — обрабатываем ввод
                try:
                    if await handle_onboarding(update, user_id, text, onb):
                        return
                except Exception as _onb_err:
                    logger.error(f"handle_onboarding error uid={user_id}: {_onb_err}", exc_info=True)
                    await send(update, "Связь прервалась — ответь ещё раз 🔁")
                    return
        else:
            try:
                if await handle_onboarding(update, user_id, text, onb):
                    return
            except Exception as _onb_err:
                logger.error(f"handle_onboarding (intentional) error uid={user_id}: {_onb_err}", exc_info=True)
                await send(update, "Что-то пошло не так — попробуй снова 🔁")
                return

    # Проверяем онбординг (edge-case: Redis-ключ потерян после рестарта)
    from db import is_onboarded, save_profile
    if not await is_onboarded(user_id):
        p = await get_profile(user_id)
        if p.get("niche"):
            p["onboarded"] = True
            await save_profile(user_id, p)
        else:
            state = {"step": 0, "data": {}}
            from db import save_onboarding_state
            await save_onboarding_state(user_id, state)
            from flows.onboarding import onb_next
            await onb_next(update, user_id, state)
            return

    # Paywall
    _state = await get_user_state(user_id)
    if not has_access(_state):
        await show_paywall(update, user_id, _state)
        return

    # 2. Голосовой режим (редактирование)
    if await kv_get(user_id, "__voice_edit_mode__"):
        await kv_del(user_id, "__voice_edit_mode__")
        await kv_del(user_id, "__voice_pending__")
        await send(update, f"🎙 _{text}_", parse_mode="Markdown")

    # 2a. Дейли — установка времени
    daily_time_s = await get_agent_session(user_id, "daily_time_flow")
    if daily_time_s and daily_time_s.get("step") == "await_time":
        import re as _re
        m = _re.match(r"(\d{1,2}):(\d{2})", text.strip())
        if m:
            hour_local, minute = int(m.group(1)), int(m.group(2))
            tz_offset = daily_time_s.get("tz_offset", 2)
            hour_utc  = (hour_local - tz_offset) % 24
            from db import save_daily_settings, get_daily_settings
            ds = await get_daily_settings(user_id)
            ds.update({"hour": hour_utc, "minute": minute, "hour_local": hour_local})
            await save_daily_settings(user_id, ds)
            from db import clear_agent_session
            await clear_agent_session(user_id, "daily_time_flow")
            if ds.get("enabled"):
                try:
                    from flows.misc import schedule_daily
                    await schedule_daily(ctx.application, user_id, hour_utc, minute)
                except Exception as e:
                    logger.warning(f"Could not reschedule: {e}")
            await send(
                update,
                f"✅ Время установлено: *{hour_local:02d}:{minute:02d}*\n_UTC: {hour_utc:02d}:{minute:02d}_",
                parse_mode="Markdown",
                reply_markup=kb(["☀️ Дейли-режим|daily_menu", "← Меню|menu_main"]),
            )
        else:
            await send(update, "Не понял формат. Напиши например: `9:00` или `08:30`",
                       parse_mode="Markdown")
        return

    # 2b. Планировщик
    planner_s = await get_agent_session(user_id, _PLANNER_KEY)
    if planner_s and await route_planner_text(update, user_id, text, planner_s):
        return

    # 2c. Стиль
    style_s = await get_agent_session(user_id, _STYLE_KEY)
    if style_s and style_s.get("step") == "await_example":
        await style_save_example(update, user_id, text)
        return

    # 2d. Быстрые идеи
    qi = await get_agent_session(user_id, _QI_KEY)
    if qi and qi.get("step") == "await_niche":
        await qi_generate(update, user_id, text)
        return

    # 2e. Доработка
    refine_s = await get_agent_session(user_id, _REFINE_KEY)
    if refine_s and refine_s.get("step") == "await_instruction":
        await refine_do(update, user_id, text, refine_s)
        return

    # 2f. Prompt editor (только для админа)
    pe_s = await get_agent_session(user_id, _PE_KEY)
    if pe_s and pe_s.get("step") == "await_text":
        if user_id == ADMIN_ID:
            await pe_save_text(update, user_id, text, pe_s)
        return

    # 2g. Voice note feedback — ПОСЛЕ всех активных сессий агентов.
    # Иначе перехватывает текст доработки/рефайна пока активен _FEEDBACK_STEP_KEY.
    if await handle_voice_note_text(update, user_id, text):
        return

    # 3. Рилс-коротышка
    rs = await get_agent_session(user_id, _RS_KEY)
    if rs:
        if rs.get("step") == "generating":
            from db import clear_agent_session
            await clear_agent_session(user_id, _RS_KEY)
            await send(update, "⚠️ Предыдущая генерация прервалась. Начни тему заново.",
                       reply_markup=kb(["🎬 Новый рилс|flow_reels_short", "← Меню|menu_main"]))
            return
        await rs_route_text(update, user_id, text, rs)
        return

    # 4. Карусель
    car = await get_agent_session(user_id, _CAR_KEY)
    if car:
        if car.get("step") == "generating":
            from db import clear_agent_session
            await clear_agent_session(user_id, _CAR_KEY)
            await send(update, "⚠️ Предыдущая генерация прервалась. Начни тему заново.",
                       reply_markup=kb(["🎠 Новая карусель|flow_carousel", "← Меню|menu_main"]))
            return
        await car_route_text(update, user_id, text, car)
        return

    # 5. Активный generic агент
    agent_key = await _detect_active_agent(user_id)
    if agent_key:
        spec = ag.get_spec(agent_key)
        if spec:
            s = await get_agent_session(user_id, agent_key)
            if s and s.get("step") == "generating":
                from db import clear_agent_session
                await clear_agent_session(user_id, agent_key)
                await send(update, "⚠️ Предыдущая генерация прервалась. Запусти агента снова.",
                           reply_markup=kb([f"🔁 Снова|agent_restart_{agent_key}", "← Меню|menu_main"]))
                return
            await ag.handle_text(update, user_id, spec, text)
            return

    # 6. Intent router
    from intent_router import classify_intent, oneshot_generate, get_agent_suggestions, \
        AGENT_EMOJI, AGENT_NAMES, CONFIDENCE_THRESHOLD, _ONESHOT_PROMPTS

    profile = await get_profile(user_id)
    intent  = await classify_intent(text, profile)
    logger.info(f"[intent] user={user_id} agent={intent.agent} conf={intent.confidence:.2f}")

    if intent.confidence >= CONFIDENCE_THRESHOLD and intent.agent != "chat":
        await kv_set(user_id, "__intent_ctx__", json.dumps({
            "agent": intent.agent, "text": text, "topic": intent.topic,
        }, ensure_ascii=False))

        main_cb = (
            "flow_reels_short" if intent.agent == "reels_short" else
            "flow_carousel"    if intent.agent == "carousel"    else
            f"agent_start_{intent.agent}"
        )

        # Высокая уверенность — запускаем агента сразу
        if intent.confidence >= 0.85:
            if intent.agent == "reels_short":
                from db import clear_all_agent_sessions
                await clear_all_agent_sessions(user_id)
                await reels.rs_start(update, user_id)
                return
            elif intent.agent == "carousel":
                from db import clear_all_agent_sessions
                await clear_all_agent_sessions(user_id)
                await carousel.car_start(update, user_id)
                return
            else:
                spec = ag.get_spec(intent.agent)
                if spec:
                    from db import clear_all_agent_sessions
                    await clear_all_agent_sessions(user_id)
                    await ag.start(update, user_id, spec)
                    return

        emoji_main = AGENT_EMOJI.get(intent.agent, "🤖")
        name_main  = AGENT_NAMES.get(intent.agent, intent.agent)
        rows = [[f"{emoji_main} {name_main}|{main_cb}"]]

        suggestions = get_agent_suggestions(intent.agent)
        alt_row = []
        for alt_key in suggestions[:2]:
            alt_cb = (
                "flow_reels_short" if alt_key == "reels_short" else
                "flow_carousel"    if alt_key == "carousel"    else
                f"agent_start_{alt_key}"
            )
            alt_row.append(f"{AGENT_EMOJI.get(alt_key, '🤖')} {AGENT_NAMES.get(alt_key, alt_key)}|{alt_cb}")
        if alt_row:
            rows.append(alt_row)

        if intent.agent in _ONESHOT_PROMPTS:
            rows.append(["⚡ Быстрый результат|intent_oneshot"])
        rows.append(["☰ Меню|menu_main"])

        topic_display = intent.topic or text[:60]
        await send(
            update,
            f"Понял — *«{topic_display}»*\n\nВыбери инструмент:",
            parse_mode="Markdown",
            reply_markup=kb(*rows),
        )
        return

    # 7. Chat fallback
    model_key = await get_model(user_id)
    history   = await get_history(user_id, model_key, "chat")
    _chat_base = CHAT_SYSTEM + build_profile_ctx(profile)
    _chat_base_with_ctx = await __import__("chat_context").build_chat_system(_chat_base, user_id)

    # Если юзер открыл "Спроси Миру" — добавляем тёплый persona-суффикс
    _chat_mode = await kv_get(user_id, "__chat_mode__")
    if _chat_mode == "ask_mira":
        _chat_base_with_ctx += (
            "\n\nСейчас пользователь задаёт вопрос напрямую через «Спроси Миру». "
            "Отвечай тепло и развёрнуто — как близкая подруга которая разбирается в теме. "
            "Не обрывай мысль. Если тема требует уточнения — задай один вопрос в конце."
        )

    system    = protect(user_id, await get_prompt(user_id, "chat", _chat_base_with_ctx))
    history.append({"role": "user", "content": text})

    _stop = asyncio.Event()
    _typing_task = asyncio.create_task(_typing_loop(update.effective_chat, _stop))
    try:
        reply = await chat(history, system=system, model_key=model_key, temperature=0.4)
    except Exception as e:
        logger.error(f"chat error: {e}")
        await send(update, "Что-то пошло не так — попробуй ещё раз 🔁",
                   reply_markup=kb(["🔁 Повторить|mode_chat", "← Меню|menu_main"]))
        return
    finally:
        _stop.set()
        _typing_task.cancel()

    await save_message(user_id, "user",      text,  model_key, "chat")
    await save_message(user_id, "assistant", reply, model_key, "chat")

    _is_refusal = any(m in reply for m in ["# 🛑 Стоп", "не про маркетинг"])
    await send(
        update,
        reply,
        reply_markup=kb(
            ["💡 Идеи для постов|quick_ideas", "← Меню|menu_main"]
            if _is_refusal else
            ["💬 Ещё вопрос|mode_chat", "← Меню|menu_main"]
        ),
    )


async def _detect_active_agent(user_id: int) -> str | None:
    active = await kv_get(user_id, "__active_agent__")
    if not active or active in (_RS_KEY, _CAR_KEY):
        return None
    session = await get_agent_session(user_id, active)
    if session:
        return active
    await kv_del(user_id, "__active_agent__")
    return None
