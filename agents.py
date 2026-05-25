"""
agents.py — движок для всех generic-агентов.

Изменения v2:
- Статусные сообщения с голосом Миры (не "Пишу...", а конкретные)
- _after_result: upsell для триал-пользователей после каждой генерации
- generate(): complete_long() для тяжёлых вызовов (было: complete())
- safe_delete() вместо bare except: pass
- _first_message: статус "Записала — думаю над первым вопросом..."
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
from llm import vision_chat, complete_long, generate_from_history
from db import (
    get_agent_session, save_agent_session, clear_agent_session,
    get_profile, save_result, get_style_examples,
)
from utils import send, kb, safe_delete
from voice_learner import build_voice_context, voice_feedback_kb
from niche_intel import build_niche_context

logger = logging.getLogger(__name__)

_AGENT_PROMPT_SLUGS: dict[str, tuple[str, str]] = {
    "profile":      ("profile_interviewer",     "profile_generator"),
    "stories":      ("stories_interviewer",      "stories_generator"),
    "reels_adapt":  ("reels_adapt_interviewer",  "reels_adapt_generator"),
    "tg_plan":      ("tg_plan_interviewer",       "tg_plan_generator"),
    "warmup":       ("warmup_interviewer",        "warmup_generator"),
    "talking_head": ("talking_head_interviewer", "talking_head_generator"),
    "cartoon":      ("cartoon_interviewer",       "cartoon_generator"),
    "competitor":   ("competitor_interviewer",    "competitor_generator"),
}

_DONE_SIGNAL = "[ready]"

# Статусные сообщения с голосом Миры
_STATUS_THINKING = [
    "Записала — думаю над первым вопросом...",
    "Изучаю контекст...",
    "Смотрю что здесь можно сделать...",
]
_STATUS_GENERATING = [
    "Пишу — занимает минуту...",
    "Собираю всё что ты рассказала...",
    "Уже вижу структуру, дописываю...",
]
_STATUS_VARIANTS = [
    "Составляю варианты — ищу лучшие углы...",
    "Генерирую несколько подходов...",
]


@dataclass
class AgentSpec:
    key:               str
    name:              str
    emoji:             str
    welcome:           str
    interviewer:       str
    generator:         str | Callable
    final_prompt:      str
    final_prompt_pick: str   = ""
    as_file:           bool  = False
    file_prefix:       str   = "Результат"
    max_q:             int   = 6
    accept_photos:     bool  = False
    model:             str   = "claude"
    has_pick_step:     bool  = False
    pick_prompt:       str   = ""
    photo:             str   = ""


_REGISTRY: dict[str, AgentSpec] = {}


def register(spec: AgentSpec) -> AgentSpec:
    _REGISTRY[spec.key] = spec
    return spec


def get_spec(key: str) -> AgentSpec | None:
    return _REGISTRY.get(key)


def all_specs() -> list[AgentSpec]:
    return list(_REGISTRY.values())


async def get_agent_session_safe(user_id: int, key: str) -> dict | None:
    try:
        return await get_agent_session(user_id, key)
    except Exception:
        return None


# ── Engine ─────────────────────────────────────────────────────────────────────

async def start(update: Update, user_id: int, spec: AgentSpec) -> None:
    # Проверяем доступ ДО создания сессии — нет смысла создавать если нет доступа
    from user_state import get_user_state, has_access
    from ui.paywall import show_paywall
    _state = await get_user_state(user_id)
    if not has_access(_state):
        await show_paywall(update, user_id, _state)
        return

    await clear_agent_session(user_id, spec.key)
    await save_agent_session(user_id, spec.key, {
        "agent_key": spec.key, "step": "initial",
        "initial": "", "history": [], "q_count": 0,
        "picked": "", "extra": {}, "photos": [],
    })
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
                    with open(_p, "rb") as _f:
                        await update.get_bot().send_photo(
                            chat_id=update.effective_chat.id,
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
    # Проверяем доступ на каждый шаг интервью — подписка могла истечь в процессе
    from user_state import get_user_state, has_access
    from ui.paywall import show_paywall
    _state = await get_user_state(user_id)
    if not has_access(_state):
        await show_paywall(update, user_id, _state)
        return

    session = await get_agent_session(user_id, spec.key)
    if not session:
        await start(update, user_id, spec)
        return
    step = session.get("step", "initial")
    if   step == "initial":     await _first_message(update, user_id, spec, text, session)
    elif step == "interview":   await _interview_step(update, user_id, spec, text, session)
    elif step == "pick":        await _pick_step(update, user_id, spec, text, session)
    elif step == "await_photos":
        await send(update, "Скинь скриншоты или нажми «Генерировать» 👇",
                   reply_markup=kb(["🚀 Генерировать|agent_generate"]))
    elif step == "generating":
        await send(update, "⏳ Уже пишу — подожди буквально минуту...")


async def handle_photo(update: Update, user_id: int, spec: AgentSpec) -> None:
    session = await get_agent_session(user_id, spec.key)
    if not session:
        return

    photo   = update.message.photo[-1]
    file    = await photo.get_file()
    content = await file.download_as_bytearray()
    b64     = base64.b64encode(content).decode()
    caption = (update.message.caption or "").strip()
    step    = session.get("step", "")

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

    if step in ("interview", "initial"):
        ih = session.get("history", [])
        caption_text = caption if caption else "Посмотри на скриншот — учти его в следующем вопросе."
        interviewer  = await _resolve_interviewer_custom(user_id, spec)
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
            await safe_delete(status)
            await send(update, "❌ Не удалось обработать скриншот. Напиши текстом.",
                       reply_markup=kb(["⏭ Пропустить вопросы|agent_skip", "← Меню|menu_main"]))
            return
        await safe_delete(status)

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

    await send(update, "Сейчас не жду фото. Продолжай отвечать на вопрос 👆")


async def force_generate(update: Update, user_id: int, spec: AgentSpec) -> None:
    from user_state import get_user_state, has_access
    from ui.paywall import show_paywall
    _state = await get_user_state(user_id)
    if not has_access(_state):
        await show_paywall(update, user_id, _state)
        return

    session = await get_agent_session(user_id, spec.key)
    if not session:
        await start(update, user_id, spec)
        return
    await generate(update, user_id, spec, session)


# ── Internal steps ─────────────────────────────────────────────────────────────

async def _first_message(update: Update, user_id: int,
                          spec: AgentSpec, text: str, session: dict) -> None:
    session["initial"] = text
    profile = await get_profile(user_id)
    style_examples = await get_style_examples(user_id)
    ctx = _build_context(spec, profile, style_examples)
    interviewer = await _resolve_interviewer_custom(user_id, spec)

    status = await update.effective_chat.send_message(
        _STATUS_THINKING[hash(spec.key) % len(_STATUS_THINKING)]
    )
    try:
        first_q = await llm.complete(
            interviewer,
            f"{ctx}\n\nПервичная информация:\n{text}\n\nЗадай первый уточняющий вопрос.",
            temperature=0.4,
        )
    except Exception as e:
        logger.error(f"[{spec.key}] first_message LLM error: {e}")
        await safe_delete(status)
        await send(update, "Связь прервалась — ответь ещё раз, данные сохранены 🔁",
                   reply_markup=kb([f"🔁 Попробовать снова|agent_restart_{spec.key}",
                                    "← Меню|menu_main"]))
        return
    await safe_delete(status)

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
    q_info  = f"\n\n_Вопрос 1 из {spec.max_q}_"
    await send(update, clean_q + q_info, parse_mode="Markdown",
               reply_markup=kb(["⏭ Пропустить вопросы|agent_skip", "← Меню|menu_main"]))


async def _is_offtopic(question: str, answer: str, agent_name: str) -> bool:
    """
    Быстрый классификатор: релевантен ли ответ вопросу.
    Температура 0, max_tokens минимальный — занимает ~0.5с.
    Возвращает True если ответ явно не по теме.
    """
    try:
        verdict = await llm.complete(
            f"Ты классифицируешь ответы пользователя в чат-боте для контент-мейкеров ({agent_name}).\n"
            "Ответь ТОЛЬКО словом YES если ответ явно не относится к заданному вопросу "
            "(случайный текст, вопрос вместо ответа, совсем другая тема).\n"
            "Ответь ТОЛЬКО словом NO если ответ хоть как-то отвечает на вопрос, даже кратко или косвенно.\n"
            "Сомнение = NO. Короткий ответ типа «не знаю», «любой», «что-то своё» = NO.",
            f"Вопрос агента: {question}\n\nОтвет пользователя: {answer}",
            temperature=0.0,
        )
        return verdict.strip().upper().startswith("YES")
    except Exception:
        return False  # при ошибке классификатора — не блокируем


async def _interview_step(update: Update, user_id: int,
                           spec: AgentSpec, text: str, session: dict) -> None:
    ih = session["history"]

    # Off-topic guard: проверяем только если есть предыдущий вопрос от агента
    last_agent_q = next(
        (m["content"] for m in reversed(ih) if m["role"] == "assistant"),
        None
    )
    if last_agent_q and len(text.strip()) > 3:
        offtopic = await _is_offtopic(last_agent_q[:300], text[:300], spec.name)
        if offtopic:
            # Вырезаем сам вопрос из истории для повтора
            clean_q = last_agent_q.replace("[READY]", "").replace("[ready]", "").strip()
            # Берём только первое предложение вопроса
            short_q = clean_q.split("\n")[0][:200]
            await send(
                update,
                f"Кажется, это не совсем про то о чём я спросила.\n\n"
                f"_Мой вопрос был:_ {short_q}\n\n"
                "Ответь на него — и двинемся дальше 👇",
                parse_mode="Markdown",
                reply_markup=kb(["⏭ Пропустить этот вопрос|agent_skip", "← Меню|menu_main"]),
            )
            return

    ih.append({"role": "user", "content": text[:1500]})
    interviewer = await _resolve_interviewer_custom(user_id, spec)
    try:
        next_msg = await llm.chat(ih, system=interviewer, temperature=0.4)
    except Exception as e:
        logger.error(f"[{spec.key}] interview LLM error: {e}")
        ih.pop()
        await send(update, "Связь прервалась — ответь ещё раз и продолжим 🔁",
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
    session["step"] = "generating"
    await save_agent_session(user_id, spec.key, session)

    status = await update.effective_chat.send_message(
        _STATUS_VARIANTS[hash(spec.key) % len(_STATUS_VARIANTS)]
    )
    try:
        profile    = await get_profile(user_id)
        sys_prompt = await _resolve_generator_custom(user_id, spec, session, profile)
        result     = await llm.generate_with_progress(
            sys_prompt, session["history"],
            final_prompt=spec.final_prompt,
            status_msg=status,
            model_key=spec.model,
            temperature=0.85,
            presence_penalty=0.3,
            agent_key=spec.key,
        )
    except Exception as e:
        logger.error(f"[{spec.key}] gen_variants error: {e}")
        await safe_delete(status)
        session["step"] = "interview"
        await save_agent_session(user_id, spec.key, session)
        await send(update, "Не вышло с первого раза — жми ещё раз 🔁",
                   reply_markup=kb(["🔁 Повторить|agent_regen", "← Меню|menu_main"]))
        return
    await safe_delete(status)

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

    stage2_prompt = (spec.final_prompt_pick or spec.final_prompt)
    final_p       = f"Выбранный вариант пользователя: {chosen}\n\n{stage2_prompt}"
    profile       = await get_profile(user_id)
    sys_prompt    = await _resolve_generator_custom(user_id, spec, session, profile)

    try:
        result = await generate_from_history(
            sys_prompt, session["history"],
            final_prompt=final_p,
            model_key=spec.model,
            temperature=0.85,
            presence_penalty=0.3,
        )
    except Exception as e:
        logger.error(f"[{spec.key}] pick_step Stage2 error: {e}")
        session["step"] = "pick"
        await save_agent_session(user_id, spec.key, session)
        await send(update, "❌ Ошибка. Попробуй выбрать снова.",
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
    session["step"] = "generating"
    await save_agent_session(user_id, spec.key, session)

    status = await update.effective_chat.send_message(
        _STATUS_GENERATING[hash(spec.key) % len(_STATUS_GENERATING)]
    )

    profile        = await get_profile(user_id)
    style_examples = await get_style_examples(user_id)

    # Строим полный контекст: голос + ниша = уникальный output
    voice_ctx = await build_voice_context(user_id)
    niche_ctx = build_niche_context(profile.get("niche", ""))
    base_sys  = await _resolve_generator_custom(user_id, spec, session, profile)
    sys_prompt = base_sys + voice_ctx + niche_ctx

    try:
        photos = [[b64, mime] for b64, mime in session.get("photos", [])]
        if photos:
            ctx    = (f"Информация:\n{session.get('initial', '')}\n\n"
                      + _history_to_text(session["history"]))
            result = await llm.vision_complete(sys_prompt, ctx, photos, model_key="gpt4")
        else:
            # generate_with_progress: апдейт статуса через 20с и 50с при долгих генерациях
            result = await llm.generate_with_progress(
                sys_prompt, session["history"],
                final_prompt=spec.final_prompt,
                status_msg=status,
                model_key=spec.model,
                temperature=0.85,
                presence_penalty=0.3,
                agent_key=spec.key,
            )
    except Exception as e:
        logger.error(f"[{spec.key}] generate error: {e}")
        await safe_delete(status)
        await send(update, "Не вышло с первого раза — жми ещё раз 🔁",
                   reply_markup=kb(["🔁 Повторить|agent_retry", "← Меню|menu_main"]))
        return

    await safe_delete(status)

    if not result or not result.strip():
        result = "⚠️ Пустой ответ от модели. Запусти агента заново."

    await clear_agent_session(user_id, spec.key)
    await _send_result(update, result, spec, user_id)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _resolve_generator(spec: AgentSpec, session: dict, profile: dict) -> str:
    if callable(spec.generator):
        return spec.generator(profile, session)
    return spec.generator


async def _resolve_interviewer_custom(user_id: int, spec: AgentSpec) -> str:
    slugs = _AGENT_PROMPT_SLUGS.get(spec.key)
    if not slugs:
        return spec.interviewer
    from prompt_editor import get_prompt
    return await get_prompt(user_id, slugs[0], spec.interviewer)


async def _resolve_generator_custom(user_id: int, spec: AgentSpec,
                                    session: dict, profile: dict) -> str:
    base = _resolve_generator(spec, session, profile)
    slugs = _AGENT_PROMPT_SLUGS.get(spec.key)
    if not slugs:
        return base
    from prompt_editor import get_prompt
    return await get_prompt(user_id, slugs[1], base)


def _build_context(spec: AgentSpec, profile: dict,
                   style_examples: list = None) -> str:
    parts = [f"Агент: {spec.name}"]
    if profile.get("niche"):    parts.append(f"Ниша: {profile['niche']}")
    if profile.get("audience"): parts.append(f"Аудитория: {profile['audience']}")
    if profile.get("tone"):     parts.append(f"Тон голоса: {profile['tone']}")
    if style_examples:
        examples_text = "\n---\n".join(style_examples[-3:])
        parts.append(f"\nПРИМЕРЫ СТИЛЯ АВТОРА:\n{examples_text}")
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

    result_id = 0
    if not _already_saved:
        try:
            result_id = await save_result(user_id, spec.key, spec.name, result)
        except Exception as e:
            logger.warning(f"Could not save result: {e}")
        # Обновляем стрик здесь (не в db.py — там circular import)
        try:
            from ui.home import update_streak_on_result
            await update_streak_on_result(user_id)
        except Exception:
            pass

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
            await _after_result(update, spec, user_id)
            return
        except Exception as e:
            logger.error(f"File send failed for {spec.key}: {e}")
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    header = f"✅ *{spec.name} готово!*\n\n"

    # Форматируем результат — хуки в моно, spoiler-концовка, разделители
    from ui.media import format_result
    formatted = format_result(result, spec.key)

    full  = header + formatted
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

    # GIF при первой генерации
    try:
        from db import get_stats
        from ui.media import send_gif
        _stats = await get_stats(user_id)
        if _stats.get("total", 0) == 1:
            await send_gif(update, "first_generation")
    except Exception:
        pass

    await _after_result(update, spec, user_id)


async def _after_result(update: Update, spec: AgentSpec, user_id: int) -> None:
    """После результата — кнопки действий + voice feedback + upsell + проактив."""
    # Получаем ID последнего результата для voice feedback
    from db import get_results
    _result_id = 0
    try:
        _recent = await get_results(user_id, limit=1)
        if _recent:
            _result_id = _recent[0]["id"]
    except Exception:
        pass

    await send(
        update,
        "Что дальше?",
        reply_markup=kb(
            ["✏️ Доработать|refine_last",          "🔄 Другой вариант|regen_last"],
            [f"🔁 Ещё раз|agent_restart_{spec.key}", "📚 Сохранённые|my_results"],
        ),
    )

    # Стрик
    try:
        from ui.home import update_streak_on_result as _usr
        await _usr(user_id)
    except Exception:
        pass

    # Кнопка голосового фидбека с прогресс-баром — ключевой differentiator
    if _result_id:
        import asyncio
        await asyncio.sleep(0.5)
        # Прогресс-бар голоса через единую библиотеку
        try:
            from voice_learner import get_voice_stats
            from ui.progress_bar import voice_progress_short
            _vs = await get_voice_stats(user_id)
            _total = _vs.get("total_signals", 0)
            _voice_hint = voice_progress_short(_total)
        except Exception:
            _voice_hint = ""

        await send(
            update,
            f"Звучит как твой голос?{_voice_hint}",
            parse_mode="Markdown",
            reply_markup=voice_feedback_kb(_result_id),
        )

    # Мягкий upsell для триал-пользователей
    from ui.cabinet import maybe_show_upsell
    await maybe_show_upsell(update, user_id)

    # Проактивный следующий шаг
    from flows.proactive import maybe_suggest_next
    await maybe_suggest_next(update, user_id, spec.key, delay=1.5)
