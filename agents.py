"""
agents.py — единый движок для всех 9 агентов.
"""
from __future__ import annotations
import asyncio
import base64
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from telegram import Update

import llm
from llm import vision_chat
from db import get_agent_session, save_agent_session, clear_agent_session, get_profile, save_result, get_style_examples
from utils import send, kb

# Slug-маппинг для агентов: agent_key → (interviewer_slug, generator_slug)
_AGENT_PROMPT_SLUGS: dict[str, tuple[str, str]] = {
    "profile":       ("profile_interviewer",      "profile_generator"),
    "stories":       ("stories_interviewer",       "stories_generator"),
    "reels_adapt":   ("reels_adapt_interviewer",   "reels_adapt_generator"),
    "tg_plan":       ("tg_plan_interviewer",        "tg_plan_generator"),
    "warmup":        ("warmup_interviewer",         "warmup_generator"),
    "talking_head":  ("talking_head_interviewer",  "talking_head_generator"),
    "cartoon":       ("cartoon_interviewer",        "cartoon_generator"),
    "competitor":    ("competitor_interviewer",     "competitor_generator"),
}

logger = logging.getLogger(__name__)

# FIX: "генерирую" is too common in Russian speech and false-triggered the interview end.
# Now interviewers append the hidden marker [READY] to their final message.
# The code detects it, strips it before showing to user, and triggers generation.
_DONE_SIGNAL = "[ready]"


@dataclass
class AgentSpec:
    key:              str
    name:             str
    emoji:            str
    welcome:          str
    interviewer:      str
    generator:        str | Callable
    final_prompt:     str
    # Stage-2 prompt для агентов с has_pick_step (иначе = final_prompt)
    final_prompt_pick: str          = ""
    as_file:          bool          = False
    file_prefix:      str           = "Результат"
    max_q:            int           = 6
    accept_photos:    bool          = False
    model:            str           = "claude"
    has_pick_step:    bool          = False
    pick_prompt:      str           = ""
    photo:            str           = ""   # имя файла картинки для welcome-экрана


# ── registry ──────────────────────────────────────────────────────────────────
_REGISTRY: dict[str, AgentSpec] = {}

def register(spec: AgentSpec) -> AgentSpec:
    _REGISTRY[spec.key] = spec
    return spec

def get_spec(key: str) -> AgentSpec | None:
    return _REGISTRY.get(key)

def all_specs() -> list[AgentSpec]:
    return list(_REGISTRY.values())


async def get_agent_session_safe(user_id: int, key: str) -> dict | None:
    """Безопасный геттер сессии агента — возвращает None вместо исключения."""
    try:
        return await get_agent_session(user_id, key)
    except Exception:
        return None


# ── engine ────────────────────────────────────────────────────────────────────

async def start(update: Update, user_id: int, spec: AgentSpec) -> None:
    await clear_agent_session(user_id, spec.key)
    await save_agent_session(user_id, spec.key, {
        "agent_key": spec.key, "step": "initial",
        "initial": "", "history": [], "q_count": 0,
        "picked": "", "extra": {}, "photos": [],
    })
    # FIX: store explicit active agent key to avoid expensive Redis SCAN
    from db import kv_set as _kv_set
    await _kv_set(user_id, "__active_agent__", spec.key)
    _kb = kb(["← Меню|menu_main"])
    if spec.photo:
        _dirs = [os.path.dirname(os.path.abspath(__file__)), os.getcwd()]
        _sent = False
        for _d in _dirs:
            _p = os.path.join(_d, spec.photo)
            if os.path.exists(_p):
                try:
                    chat_id = update.effective_chat.id
                    bot = update.get_bot()
                    with open(_p, "rb") as _f:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=_f,
                            caption=spec.welcome,
                            parse_mode="Markdown",
                            reply_markup=_kb,
                        )
                    _sent = True
                    break
                except Exception as e:
                    logger.error(f"[agent photo] {spec.photo}: {e}")
        if not _sent:
            await send(update, spec.welcome, parse_mode="Markdown", reply_markup=_kb)
    else:
        await send(update, spec.welcome, parse_mode="Markdown", reply_markup=_kb)


async def handle_text(update: Update, user_id: int,
                      spec: AgentSpec, text: str) -> None:
    session = await get_agent_session(user_id, spec.key)
    if not session:
        await start(update, user_id, spec)
        return
    step = session.get("step", "initial")
    if   step == "initial":    await _first_message(update, user_id, spec, text, session)
    elif step == "interview":  await _interview_step(update, user_id, spec, text, session)
    elif step == "pick":       await _pick_step(update, user_id, spec, text, session)
    elif step == "await_photos":
        await send(update, "Скинь скриншоты или нажми «Генерировать» 👇",
                   reply_markup=kb(["🚀 Генерировать|agent_generate"]))
    elif step == "generating":
        await send(update, "⏳ Генерирую, подожди немного...")


async def handle_photo(update: Update, user_id: int, spec: AgentSpec) -> None:
    """Принимает фото на шаге await_photos (накопление) или interview (прямо в разговор)."""
    session = await get_agent_session(user_id, spec.key)
    if not session:
        return

    # Скачиваем фото
    photo   = update.message.photo[-1]
    file    = await photo.get_file()
    content = await file.download_as_bytearray()
    b64     = base64.b64encode(content).decode()
    caption = (update.message.caption or "").strip()

    step = session.get("step", "")

    # ── Режим накопления фото (профиль-агент) ──
    if step == "await_photos":
        photos = session.get("photos", [])
        photos.append([b64, "image/jpeg"])
        session["photos"] = photos
        await save_agent_session(user_id, spec.key, session)
        count = len(photos)
        if count >= 5:
            await send(update, f"✅ {count} скринов — максимум. Генерирую...")
            await generate(update, user_id, spec, session)
        else:
            await send(update,
                       f"✅ Скрин {count}/5 принят. Добавь ещё или жми «Генерировать».",
                       reply_markup=kb(["🚀 Генерировать|agent_generate"]))
        return

    # ── Фото во время интервью — vision_chat продолжает диалог ──
    if step in ("interview", "initial"):
        ih = session.get("history", [])
        caption_text = caption if caption else "Смотри на скриншот — учти его в следующем вопросе."
        interviewer = await _resolve_interviewer_custom(user_id, spec)
        status = await update.effective_chat.send_message("Смотрю что тут...")
        try:
            reply = await llm.vision_chat(
                history=ih,
                images_b64=[(b64, "image/jpeg")],
                caption=caption_text,
                system=interviewer,
                model_key="gpt4",
            )
        except Exception as e:
            logger.error(f"[{spec.key}] vision_chat error: {e}")
            try: await status.delete()
            except: pass
            await send(update, "❌ Не удалось обработать скриншот. Напиши текстом.",
                       reply_markup=kb(["⏭ Пропустить вопросы|agent_skip", "← Меню|menu_main"]))
            return
        try: await status.delete()
        except: pass

        # Добавляем фото-сообщение в историю как текст (для совместимости)
        photo_user_msg = caption_text + " [скриншот прикреплён]"
        ih.append({"role": "user",      "content": photo_user_msg})
        ih.append({"role": "assistant", "content": reply})
        session["history"]  = ih
        session["q_count"]  = session.get("q_count", 0) + 1
        session["step"]     = "interview"
        await save_agent_session(user_id, spec.key, session)

        clean_reply = reply.replace("[READY]", "").replace("[ready]", "").strip()
        if _DONE_SIGNAL in reply.lower() or session["q_count"] >= spec.max_q:
            await send(update, clean_reply, parse_mode="Markdown")
            if spec.accept_photos:
                await _offer_photos(update, user_id, spec, session)
            elif spec.has_pick_step:
                await _gen_variants(update, user_id, spec, session)
            else:
                await generate(update, user_id, spec, session)
        else:
            q_info = f"\n\n_Вопрос {session['q_count']} из {spec.max_q}_"
            await send(update, clean_reply + q_info, parse_mode="Markdown",
                       reply_markup=kb(["⏭ Достаточно|agent_skip", "← Меню|menu_main"]))
        return

    # Иных шагов — игнорируем молча
    await send(update, "Сейчас не жду фото. Продолжай отвечать на вопрос 👆")


async def force_generate(update: Update, user_id: int, spec: AgentSpec) -> None:
    session = await get_agent_session(user_id, spec.key)
    if not session:
        await start(update, user_id, spec)
        return
    await generate(update, user_id, spec, session)


# ── internal steps ─────────────────────────────────────────────────────────────

async def _first_message(update: Update, user_id: int,
                          spec: AgentSpec, text: str, session: dict) -> None:
    session["initial"] = text
    profile = await get_profile(user_id)
    style_examples = await get_style_examples(user_id)
    ctx     = _build_context(spec, profile, style_examples)
    interviewer = await _resolve_interviewer_custom(user_id, spec)

    status = await update.effective_chat.send_message("Записала — думаю...")
    try:
        first_q = await llm.complete(
            interviewer,
            f"{ctx}\n\nПервичная информация:\n{text}\n\nЗадай первый уточняющий вопрос.",
        )
    except Exception as e:
        logger.error(f"[{spec.key}] first_message LLM error: {e}")
        try: await status.delete()
        except: pass
        await send(update, "Что-то сломалось — нажми ещё раз 🔁",
                   reply_markup=kb(["🔁 Попробовать снова|agent_restart_" + spec.key,
                                    "← Меню|menu_main"]))
        return
    try: await status.delete()
    except: pass

    session.update({
        "history": [
            {"role": "user",      "content": f"{ctx}\n\n{text}"},
            {"role": "assistant", "content": first_q},
        ],
        "q_count": 1,
        "step":    "interview",
    })
    await save_agent_session(user_id, spec.key, session)
    clean_q = first_q.replace("[READY]", "").replace("[ready]", "").strip()
    q_info = f"\n\n_Вопрос 1 из {spec.max_q}_"
    await send(update, clean_q + q_info, parse_mode="Markdown",
               reply_markup=kb(["⏭ Пропустить вопросы|agent_skip", "← Меню|menu_main"]))


async def _interview_step(update: Update, user_id: int,
                           spec: AgentSpec, text: str, session: dict) -> None:
    ih = session["history"]
    ih.append({"role": "user", "content": text[:1500]})  # cap to prevent context overflow
    interviewer = await _resolve_interviewer_custom(user_id, spec)
    try:
        next_msg = await llm.chat(ih, system=interviewer)
    except Exception as e:
        logger.error(f"[{spec.key}] interview LLM error: {e}")
        ih.pop()  # откатываем незасохранённое сообщение
        await send(update, "Связь прервалась — ответь ещё раз 🔁",
                   reply_markup=kb(["⏭ Пропустить вопросы|agent_skip", "← Меню|menu_main"]))
        return

    ih.append({"role": "assistant", "content": next_msg})
    session["history"] = ih
    session["q_count"] = session.get("q_count", 0) + 1
    await save_agent_session(user_id, spec.key, session)

    clean_msg = next_msg.replace("[READY]", "").replace("[ready]", "").strip()
    if _DONE_SIGNAL in next_msg.lower() or session["q_count"] >= spec.max_q:
        await send(update, clean_msg, parse_mode="Markdown")
        if spec.accept_photos:
            await _offer_photos(update, user_id, spec, session)
        elif spec.has_pick_step:
            await _gen_variants(update, user_id, spec, session)
        else:
            await generate(update, user_id, spec, session)
    else:
        q_info = f"\n\n_Вопрос {session['q_count']} из {spec.max_q}_"
        await send(update, clean_msg + q_info, parse_mode="Markdown",
                   reply_markup=kb(["⏭ Достаточно|agent_skip", "← Меню|menu_main"]))


async def _offer_photos(update: Update, user_id: int,
                         spec: AgentSpec, session: dict) -> None:
    session["step"] = "await_photos"
    await save_agent_session(user_id, spec.key, session)
    await send(
        update,
        "📸 *Хочешь добавить скриншоты?*\n\n"
        "Шапка профиля, хайлайты, примеры постов — до 5 фото.\n"
        "Или жми «Генерировать» — разберу по тексту.",
        parse_mode="Markdown",
        reply_markup=kb(["🚀 Генерировать|agent_generate"]),
    )


async def _gen_variants(update: Update, user_id: int,
                         spec: AgentSpec, session: dict) -> None:
    """Stage 1 для pick-step агентов: генерируем варианты для выбора."""
    session["step"] = "generating"
    await save_agent_session(user_id, spec.key, session)

    status = await update.effective_chat.send_message("Составляю варианты для тебя...")
    try:
        profile    = await get_profile(user_id)
        sys_prompt = await _resolve_generator_custom(user_id, spec, session, profile)
        result     = await llm.generate_from_history(
            sys_prompt, session["history"],
            final_prompt=spec.final_prompt,
            model_key=spec.model,
        )
    except Exception as e:
        logger.error(f"[{spec.key}] gen_variants error: {e}")
        try: await status.delete()
        except: pass
        session["step"] = "interview"
        await save_agent_session(user_id, spec.key, session)
        await send(update, "Не вышло с первого раза — жми ещё раз 🔁",
                   reply_markup=kb(["🔁 Повторить|agent_regen", "← Меню|menu_main"]))
        return
    try: await status.delete()
    except: pass

    if not result or not result.strip():
        result = "⚠️ Пустой ответ от модели. Попробуй ещё раз."

    session["extra"]["variants"] = result
    session["step"] = "pick"
    await save_agent_session(user_id, spec.key, session)

    await send(update, result, parse_mode="Markdown",
               reply_markup=kb(
                   ["✅ Выбрать вариант|agent_pick_prompt"],
                   ["🔄 Перегенерировать|agent_regen"],
                   ["← Меню|menu_main"],
               ))


async def _pick_step(update: Update, user_id: int,
                      spec: AgentSpec, text: str, session: dict) -> None:
    """Пользователь выбрал вариант → Stage 2: финальная генерация."""
    variants = session.get("extra", {}).get("variants", "")
    chosen   = text.strip()
    if chosen.isdigit():
        n     = int(chosen)
        lines = [l.strip() for l in variants.split("\n")
                 if l.strip() and l.strip()[0].isdigit()]
        if 1 <= n <= len(lines):
            chosen = lines[n - 1]

    session["picked"] = chosen
    session["step"]   = "generating"
    await save_agent_session(user_id, spec.key, session)
    await send(update, f"✅ Выбрано: _{chosen}_\n\nГенерирую полный сценарий...",
               parse_mode="Markdown")

    # Stage 2: используем final_prompt_pick если задан, иначе final_prompt
    stage2_prompt = (spec.final_prompt_pick or spec.final_prompt)
    final_p       = f"Выбранный вариант пользователя: {chosen}\n\n{stage2_prompt}"
    profile       = await get_profile(user_id)
    sys_prompt    = await _resolve_generator_custom(user_id, spec, session, profile)

    try:
        result = await llm.generate_from_history(
            sys_prompt, session["history"],
            final_prompt=final_p,
            model_key=spec.model,
        )
    except Exception as e:
        logger.error(f"[{spec.key}] pick_step Stage2 error: {e}")
        session["step"] = "pick"
        await save_agent_session(user_id, spec.key, session)
        await send(update, "❌ Ошибка. Попробуй выбрать снова или нажми «Перегенерировать».",
                   reply_markup=kb(["🔄 Выбрать снова|agent_pick_prompt",
                                    "🔁 Перегенерировать|agent_regen",
                                    "← Меню|menu_main"]))
        return

    if not result or not result.strip():
        result = "⚠️ Пустой ответ. Попробуй снова."

    await clear_agent_session(user_id, spec.key)
    await _send_result(update, result, spec, user_id)


async def generate(update: Update, user_id: int,
                    spec: AgentSpec, session: dict) -> None:
    """Финальная генерация (одношаговые агенты + profile с фото)."""
    session["step"] = "generating"
    await save_agent_session(user_id, spec.key, session)
    status = await update.effective_chat.send_message("Пишу...")

    profile    = await get_profile(user_id)
    style_examples = await get_style_examples(user_id)
    # FIX: use latest 3 examples, not oldest 3
    if style_examples and not callable(spec.generator):
        examples_text = "\n---\n".join(style_examples[-3:])
        sys_prompt = (await _resolve_generator_custom(user_id, spec, session, profile)) + f"\n\nПРИМЕРЫ СТИЛЯ АВТОРА (пиши в этом стиле):\n{examples_text}"
    else:
        sys_prompt = await _resolve_generator_custom(user_id, spec, session, profile)

    try:
        photos = [[b64, mime] for b64, mime in session.get("photos", [])]
        if photos:
            ctx    = (f"Информация:\n{session.get('initial','')}\n\n"
                      + _history_to_text(session["history"]))
            result = await llm.vision_complete(sys_prompt, ctx, photos, model_key="gpt4")
        else:
            result = await llm.generate_from_history(
                sys_prompt, session["history"],
                final_prompt=spec.final_prompt,
                model_key=spec.model,
            )
    except Exception as e:
        logger.error(f"[{spec.key}] generate error: {e}")
        try: await status.delete()
        except: pass
        await send(update, "Не вышло с первого раза — жми ещё раз 🔁",
                   reply_markup=kb(["🔁 Повторить|agent_retry", "← Меню|menu_main"]))
        return

    try: await status.delete()
    except: pass

    if not result or not result.strip():
        result = "⚠️ Пустой ответ от модели. Запусти агента заново."

    await clear_agent_session(user_id, spec.key)
    await _send_result(update, result, spec, user_id)


# ── helpers ────────────────────────────────────────────────────────────────────

def _resolve_generator(spec: AgentSpec, session: dict, profile: dict) -> str:
    if callable(spec.generator):
        return spec.generator(profile, session)
    return spec.generator


async def _resolve_interviewer_custom(user_id: int, spec: AgentSpec) -> str:
    """Возвращает interviewer с учётом кастомного промпта пользователя."""
    slugs = _AGENT_PROMPT_SLUGS.get(spec.key)
    if not slugs:
        return spec.interviewer
    from prompt_editor import get_prompt
    return await get_prompt(user_id, slugs[0], spec.interviewer)


async def _resolve_generator_custom(user_id: int, spec: AgentSpec,
                                    session: dict, profile: dict) -> str:
    """Возвращает generator с учётом кастомного промпта пользователя."""
    base = _resolve_generator(spec, session, profile)
    slugs = _AGENT_PROMPT_SLUGS.get(spec.key)
    if not slugs:
        return base
    from prompt_editor import get_prompt
    return await get_prompt(user_id, slugs[1], base)


def _build_context(spec: AgentSpec, profile: dict, style_examples: list = None) -> str:
    parts = [f"Агент: {spec.name}"]
    if profile.get("niche"):    parts.append(f"Ниша: {profile['niche']}")
    if profile.get("audience"): parts.append(f"Аудитория: {profile['audience']}")
    if profile.get("tone"):     parts.append(f"Тон голоса: {profile['tone']}")
    if style_examples:
        examples_text = "\n---\n".join(style_examples[-3:])  # FIX: use latest 3, not oldest
        parts.append(f"\nПРИМЕРЫ СТИЛЯ АВТОРА (пиши в этом стиле):\n{examples_text}")
    return "\n".join(parts)


def _history_to_text(history: list) -> str:
    return "\n".join(
        f"{'Агент' if m['role'] == 'assistant' else 'Пользователь'}: {m['content']}"
        for m in history
    )


async def _send_result(update: Update, result: str,
                        spec: AgentSpec, user_id: int,
                        _already_saved: bool = False) -> None:
    if not result or not result.strip():
        result = "⚠️ Получен пустой ответ. Попробуй запустить агента заново."

    # Сохраняем в библиотеку контента (только если ещё не сохранено)
    if not _already_saved:
        try:
            await save_result(user_id, spec.key, spec.name, result)
        except Exception as e:
            logger.warning(f"Could not save result to library: {e}")

    if spec.as_file:
        fname    = f"{spec.file_prefix}_{user_id}.txt"
        tmp_path = os.path.join(tempfile.gettempdir(), fname)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(result)
            await send(update, f"✅ *{spec.name} готово!*", parse_mode="Markdown")
            with open(tmp_path, "rb") as f:
                await update.effective_chat.send_document(
                    document=f,
                    filename=f"{spec.file_prefix}.txt",
                    caption=f"📄 {spec.name}",
                )
            await _after_result(update, spec, result)
            return
        except Exception as e:
            logger.error(f"File send failed for {spec.key}: {e}")
        finally:
            try: os.unlink(tmp_path)
            except: pass

    # Отправка текстом с разбивкой
    header = f"✅ *{spec.name} готово!*\n\n"
    full   = header + result
    CHUNK  = 3800
    if len(full) <= CHUNK:
        await send(update, full, parse_mode="Markdown")
    else:
        preview = full[:CHUNK].rsplit("\n", 1)[0]
        await send(update, preview + "\n\n_...продолжение ниже_", parse_mode="Markdown")
        rest   = full[len(preview):]
        for chunk in [rest[i:i+4000] for i in range(0, len(rest), 4000)]:
            await asyncio.sleep(0.3)
            await send(update, chunk, parse_mode="Markdown")

    await _after_result(update, spec, result)


async def _after_result(update: Update, spec: AgentSpec, result: str = "") -> None:
    """После результата — кнопки действий. Меню доступно через кнопку снизу."""
    await send(update, "Что дальше?",
               reply_markup=kb(
                   ["✏️ Доработать|refine_last",    "🔄 Другой вариант|regen_last"],
                   [f"🔁 Ещё раз|agent_restart_{spec.key}",  "📚 Сохранённые|my_results"],
               ))
