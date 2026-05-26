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
from utils import send, kb, safe_delete, typing_loop
from voice_learner import build_voice_context, voice_feedback_kb
from niche_intel import build_niche_context

# Keys used for routing in apply_edit — imported from their respective flows
# to avoid NameError when checking the active agent type.
from flows.reels import _RS_KEY
from flows.carousel import _CAR_KEY

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

# ── Панель доработки (как в carousel.py) ─────────────────────────────────────

# Инструкции правок — под разные типы контента
_AGENT_REFINE_PROMPTS: dict[str, dict[str, str]] = {
    # Универсальные (для всех агентов)
    "_default": {
        "softer":     "Перепиши в более мягком и поддерживающем тоне. Убери давление. Сохрани структуру.",
        "bolder":     "Сделай текст смелее и провокационнее: прямые утверждения, без воды. Сохрани структуру.",
        "shorter":    "Сократи на 30%: убери воду, оставь только главное. Сохрани ключевые идеи.",
        "add_detail": "Добавь одну конкретную деталь, историю или цифру — в самом сильном месте текста.",
        "stronger_cta": "Усиль призыв к действию в конце: сделай его конкретным и неотразимым.",
    },
    # Специфика для разных агентов
    "post": {
        "softer":       "Перепиши пост в более тёплом тоне — поддерживающий, без давления. Сохрани хук.",
        "bolder":       "Сделай пост провокационнее: острый хук, неудобная правда, прямые утверждения.",
        "shorter":      "Сократи пост до 150 слов: только хук + суть + CTA. Никакой воды.",
        "add_detail":   "Добавь личную историю или конкретный кейс — одним абзацем в середине поста.",
        "stronger_cta": "Перепиши последний абзац: сделай CTA конкретным, с выгодой для читателя.",
        "hook":         "Перепиши только первые 1-2 строки поста. Новый хук должен бить в другую эмоцию или ситуацию. Остальное не трогай.",
    },
    "warmup": {
        "softer":       "Смягчи тон серии: убери ощущение продажи, добавь заботу и поддержку. ДЕНЬ 1 — сохрани мягкость полностью.",
        "bolder":       "__WARMUP_BOLDER_GUARD__",
        "shorter":      "Сократи каждый пост серии до 100-150 слов. Оставь только суть и психологическую логику каждого дня.",
        "add_detail":   "Добавь социальное доказательство — историю клиента или кейс — в самый слабый пост серии.",
        "stronger_cta": "Усиль финальный призыв в последнем посте серии (День 3). ДЕНЬ 1 — CTA остаётся мягким.",
        # Warmup-specific
        "resonance":    "Усиль эмоциональный резонанс серии — без усиления давления. Добавь больше узнавания, близости, 'это про меня'. ЗАЩИЩЕНО: не меняй интенсивность давления в Дне 1.",
        "add_proof":    "Добавь конкретное социальное доказательство в День 2 (трансформация). Кейс клиента, цифра, до/после — что-то конкретное что делает результат реальным.",
        "cta":          "Усиль оффер только в Дне 3. Сделай его конкретнее: что именно получит человек, когда, за что. ДЕНЬ 1 и ДЕНЬ 2 — не трогай.",
    },
    "stories": {
        "softer":       "Перепиши сторис в более живом и неформальном стиле — как разговор с подругой.",
        "bolder":       "Сделай сторис интереснее: добавь интригу, недосказанность, провокационный вопрос.",
        "shorter":      "Сократи каждый слайд до 1-2 предложений. Читается за 2 секунды.",
        "add_detail":   "Добавь один слайд с конкретным фактом или цифрой — в середину цепочки.",
        "stronger_cta": "Усиль последний слайд: конкретный призыв с выгодой.",
        # Stories-specific
        "mid_hook":     "Перепиши слайды 5-7 (опасная зона — здесь уходит большинство). Добавь интригующий вопрос, неожиданный поворот или самый острый инсайт в этой точке. Остальные слайды не трогай.",
    },
    "tg_plan": {
        "softer":       "Смягчи формулировки тем: более живые, разговорные заголовки.",
        "bolder":       "Усиль темы: более провокационные, цепляющие заголовки для каждого поста.",
        "shorter":      "Оставь 5 лучших тем недели. Убери слабые.",
        "add_detail":   "К каждой теме добавь первую фразу поста — чтобы сразу можно было писать.",
        "stronger_cta": "К каждой теме добавь конкретный призыв к действию.",
    },
    "talking_head": {
        "softer":       "Перепиши сценарий в более личном, разговорном тоне. Как будто говоришь другу.",
        "bolder":       "Сделай сценарий провокационнее: острый вход, прямые утверждения, сильный финал.",
        "shorter":      "Сократи сценарий до 60 секунд: убери лирику, оставь суть. Обнови хронометраж в производственном брифе.",
        "add_detail":   "Добавь личную историю или конкретный пример в середину сценария.",
        "stronger_cta": "Усиль финальный призыв: конкретный следующий шаг для зрителя.",
        # Talking head-specific
        "hook":         "Перепиши только блок ХУК (0:00-0:03). Новый хук должен бить в другую боль или заблуждение аудитории. ЗАЩИЩЕНО: остальные блоки сценария не трогай.",
    },
    "cartoon": {
        "softer":       "Смягчи юмор: менее абсурдно, более узнаваемо и тепло.",
        "bolder":       "Сделай мультик острее: более неожиданный поворот, более дерзкий финал.",
        "shorter":      "Сократи до 30 секунд: убери промежуточные сцены, оставь завязку → поворот → финал.",
        "add_detail":   "Сделай персонажей живее: добавь деталь-характеристику для главного персонажа.",
        "stronger_cta": "Усиль финальный CTA: конкретнее, под боль аудитории.",
        "hook":         "Перепиши только хук (0:00-0:03): другая открывающая сцена, другое первое слово. Остальное не трогай.",
    },
    "profile": {
        # Profile refinements are additive, not stylistic
        "deepen":       "Углуби один раздел — выбери тот который сейчас звучит наиболее обобщённо. Добавь конкретику из интервью. Остальные разделы не трогай.",
        "shorter":      "Сделай компактную версию: оставь диагностическое предложение, топ-3 проблемы с метками 🔴🟡🟢, и раздел 'Что делать прямо сейчас'. Убери подробные разделы.",
        "add_detail":   "Добавь к разделу 'Что делать прямо сейчас' один конкретный пример — покажи как именно должно выглядеть исправление.",
        "stronger_cta": "Перепиши раздел 'Что делать прямо сейчас': каждое действие должно быть настолько конкретным, что его можно выполнить сегодня без вопросов.",
    },
    "competitor": {
        "softer":       "Смягчи формулировки — менее агрессивный тон, сохрани суть анализа.",
        "bolder":       "Сделай анализ честнее: скажи прямо что конкурент делает лучше и где реальная угроза. Без дипломатии.",
        "shorter":      "Оставь только: главный конкурентный разрыв, стратегию переманивания аудитории, 3 шага на этой неделе.",
        "add_detail":   "Добавь конкретные примеры контента конкурента который работает — и почему именно он работает на эту аудиторию.",
        "stronger_cta": "Перепиши '3 конкретных шага' — каждый шаг должен быть действием которое можно выполнить сегодня.",
        # Competitor-specific
        "tactics":      "Перепиши раздел 'Стратегия переманивания аудитории': укажи конкретные темы постов, форматы и дни публикации — а не общие рекомендации.",
        "positioning":  "Добавь или перепиши раздел 'Дифференцирующая фраза': одно предложение которое автор может использовать в шапке профиля или в контенте чтобы чётко отличаться от этого конкурента. Формат: 'В отличие от [конкурент], я [конкретное отличие] — потому что [причина важная для аудитории].'",
    },
    "carousel": {
        "softer":       "Смягчи тон карусели: более тёплые заголовки слайдов, меньше давления.",
        "bolder":       "Сделай карусель острее: провокационнее заголовки слайдов, сильнее первый слайд.",
        "shorter":      "Сократи каждый слайд до 2-3 строк. Текст читается за 2 секунды — проверь каждый.",
        "add_detail":   "Добавь один слайд с конкретной цифрой, кейсом или примером — в середину карусели.",
        "stronger_cta": "Перепиши последний слайд: дай чёткую причину сохранить эту карусель + конкретное следующее действие.",
        # Carousel-specific
        "save_moment":  (
            "Найди в этой карусели главный 'save-момент' — то из-за чего человек захочет сохранить её. "
            "Если такого момента нет — добавь его: это может быть чеклист, мини-инструкция, неочевидный факт или shareable цитата на отдельном слайде. "
            "Если он есть но слабый — усиль его. "
            "Объясни в одной строке ЧТО именно ты изменил и ПОЧЕМУ это повысит save-rate. "
            "Остальные слайды не трогай."
        ),
    },
}


def _get_agent_refine_prompts(agent_key: str) -> dict[str, str]:
    """Возвращает промпты правок для конкретного агента (с fallback на дефолт)."""
    return _AGENT_REFINE_PROMPTS.get(agent_key, _AGENT_REFINE_PROMPTS["_default"])


def _voice_followup_rows(agent_key: str) -> list:
    """
    Возвращает 1-2 умных предложения после результата — вместо generic «← Меню».
    Основано на _AGENT_SIBLINGS из intent_router (фаза 1.4).
    """
    from intent_router import get_agent_suggestions, AGENT_EMOJI, AGENT_NAMES
    siblings = get_agent_suggestions(agent_key)
    rows = []
    for sibling in siblings[:2]:
        cb = (
            "flow_reels_short" if sibling == "reels_short" else
            "flow_carousel"    if sibling == "carousel"    else
            f"agent_start_{sibling}"
        )
        emoji = AGENT_EMOJI.get(sibling, "🤖")
        name  = AGENT_NAMES.get(sibling, sibling)
        rows.append([f"{emoji} {name}|{cb}"])
    return rows


def _agent_edit_panel_kb(spec_key: str, completed: set | None = None,
                          refinement_count: int = 0) -> object:
    """
    Панель доработки после генерации — инструментно-специфичная.
    completed: множество уже выполненных действий — они исключаются из клавиатуры.
    refinement_count: сколько правок применено — при 3+ показываем drift warning.
    """
    from utils import kb as _kb
    done = completed or set()

    # Drift warning after 3+ refinements
    drift_warning = ""
    if refinement_count >= 3:
        drift_warning = f"\n\n⚠️ _Применено правок: {refinement_count}. Контент может отдалиться от исходного замысла._"

    rows = []

    # Tool-specific panels
    if spec_key == "warmup":
        # Warmup: no "bolder" top-level, Day 1 is protected
        if "resonance" not in done:
            rows.append(["✨ Усилить резонанс|ag_edit_resonance", "🎨 Мягче|ag_edit_softer"])
        if "add_proof" not in done:
            rows.append(["📣 Добавить доказательство|ag_edit_add_proof"])
        if "cta" not in done:
            rows.append(["💪 Усилить оффер Дня 3|ag_edit_cta"])

    elif spec_key == "profile":
        # Profile: additive refinements only
        if "deepen" not in done:
            rows.append(["🔬 Углубить раздел|ag_edit_deepen"])
        if "shorter" not in done:
            rows.append(["✂️ Компактная версия|ag_edit_shorter"])
        # No "bolder/softer" — profile is diagnostic, not stylistic

    elif spec_key == "competitor":
        if "tactics" not in done:
            rows.append(["⚡ Конкретнее тактики|ag_edit_tactics"])
        if "positioning" not in done:
            rows.append(["🎯 Фраза позиционирования|ag_edit_positioning"])

    elif spec_key == "stories":
        if "softer" not in done:
            rows.append(["🎨 Разговорнее|ag_edit_softer", "🔥 Острее|ag_edit_bolder"])
        if "shorter" not in done:
            rows.append(["✂️ Сократить слайды|ag_edit_shorter"])
        if "mid_hook" not in done:
            rows.append(["🎣 Усилить слайды 5-7|ag_edit_mid_hook"])

    elif spec_key in ("talking_head", "cartoon"):
        if "softer" not in done:
            rows.append(["🎨 Разговорнее|ag_edit_softer", "🔥 Острее|ag_edit_bolder"])
        if "shorter" not in done:
            rows.append(["✂️ До 60 сек|ag_edit_shorter"])
        if "hook" not in done:
            rows.append(["🎬 Переписать хук (0-3 сек)|ag_edit_hook"])

    elif spec_key == "post":
        if "softer" not in done:
            rows.append(["🎨 Мягче|ag_edit_softer", "🔥 Жёстче|ag_edit_bolder"])
        if "shorter" not in done:
            rows.append(["✂️ Сократить|ag_edit_shorter", "➕ Деталь|ag_edit_detail"])
        if "cta" not in done:
            rows.append(["💪 Усилить CTA|ag_edit_cta"])

    elif spec_key == "carousel":
        if "softer" not in done:
            rows.append(["🎨 Мягче|ag_edit_softer", "🔥 Жёстче|ag_edit_bolder"])
        if "shorter" not in done:
            rows.append(["✂️ Сократить|ag_edit_shorter", "➕ Деталь|ag_edit_detail"])
        if "save_moment" not in done:
            rows.append(["🎯 Save-момент|ag_edit_save_moment"])

    else:
        # Default panel
        row1 = []
        if "softer" not in done:
            row1.append("🎨 Мягче|ag_edit_softer")
        if "bolder" not in done:
            row1.append("🔥 Жёстче|ag_edit_bolder")
        if row1:
            rows.append(row1)
        row2 = []
        if "shorter" not in done:
            row2.append("✂️ Сократить|ag_edit_shorter")
        if "detail" not in done:
            row2.append("➕ Деталь|ag_edit_detail")
        if row2:
            rows.append(row2)
        if "cta" not in done:
            rows.append(["💪 Усилить CTA|ag_edit_cta"])

    rows.append(["✏️ Доработать|refine_last", "🔄 Другой вариант|regen_last"])
    if refinement_count >= 3:
        rows.append(["🔙 Вернуться к первому результату|ag_restore_original"])
    rows.append([f"🔁 Заново|agent_restart_{spec_key}", "← Меню|menu_main"])
    return _kb(*rows), drift_warning

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

async def start(update: Update, user_id: int, spec: AgentSpec,
                urgency: bool = False) -> None:
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
        "urgency": urgency,
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

    # Urgency-триггер по счётчику: если накопилось достаточно ответов — форсируем генерацию
    if step == "interview" and not session.get("urgency"):
        q_count = session.get("q_count", 0)
        if q_count >= spec.max_q:
            session["urgency"] = True
            await save_agent_session(user_id, spec.key, session)

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
    _tt_first = asyncio.create_task(typing_loop(update.effective_chat))
    try:
        first_q = await llm.complete(
            interviewer,
            f"{ctx}\n\nПервичная информация:\n{text}\n\nЗадай первый уточняющий вопрос.",
            temperature=0.4,
        )
    except Exception as e:
        logger.error(f"[{spec.key}] first_message LLM error: {e}")
        _tt_first.cancel()
        await safe_delete(status)
        await send(update, "Связь прервалась — ответь ещё раз, данные сохранены 🔁",
                   reply_markup=kb([f"🔁 Попробовать снова|agent_restart_{spec.key}",
                                    "← Меню|menu_main"]))
        return
    finally:
        _tt_first.cancel()
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


async def _extract_cip_from_interview(user_id: int, agent_key: str, history: list) -> None:
    """
    После завершения интервью извлекает стратегические сигналы в CIP:
    - warmup: audience_trust_level, audience_primary_objection, active_launch
    - competitor: positioning cue
    - all: current_funnel_phase hint, audience_language_patterns
    Использует быстрый LLM-вызов с JSON-ответом.
    """
    if not history:
        return
    try:
        from db import get_cip, save_cip
        interview_text = "\n".join(
            f"{'Агент' if m['role'] == 'assistant' else 'Пользователь'}: {m['content'][:300]}"
            for m in history[-10:]  # последние 10 сообщений достаточно
        )
        extract_system = (
            "Ты извлекаешь стратегические данные из интервью. "
            "Отвечай ТОЛЬКО JSON без пояснений и без markdown-обёртки.\n\n"
            "Поля (все опциональные, включай только если есть явные данные):\n"
            '{"audience_trust_level": 1-10, '
            '"audience_primary_objection": "текст главного возражения", '
            '"active_launch": true/false, '
            '"audience_sophistication": "low|medium|high", '
            '"current_funnel_phase": "awareness|consideration|conversion|retention", '
            '"audience_language_patterns": "слова и фразы которыми аудитория сама описывает свои боли — извлеки из ответов пользователя", '
            '"creator_voice_signature": "2-3 характерных речевых паттерна автора из интервью — короткие предложения / личные истории / конкретные цифры"}'
        )
        raw = await llm.complete(extract_system, interview_text, temperature=0.0)
        import json as _json
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start < 0 or end <= start:
            return
        data = _json.loads(raw[start:end])
        if not data:
            return
        cip = await get_cip(user_id)
        if "audience_sophistication" in data and data["audience_sophistication"] in ("low", "medium", "high"):
            cip["audience_sophistication"] = data["audience_sophistication"]
        if "audience_trust_level" in data:
            try:
                cip["audience_trust_level"] = int(data["audience_trust_level"])
            except (ValueError, TypeError):
                pass
        if "audience_primary_objection" in data and data["audience_primary_objection"]:
            cip["audience_primary_objection"] = str(data["audience_primary_objection"])[:200]
        if "active_launch" in data:
            cip["active_launch"] = bool(data["active_launch"])
        if "current_funnel_phase" in data and data["current_funnel_phase"] in (
            "awareness", "consideration", "conversion", "retention"
        ):
            cip["current_funnel_phase"] = data["current_funnel_phase"]
        if "audience_language_patterns" in data and data["audience_language_patterns"]:
            cip["audience_language_patterns"] = str(data["audience_language_patterns"])[:300]
        if "creator_voice_signature" in data and data["creator_voice_signature"]:
            # Накапливаем сигнатуру голоса, не перезаписываем
            existing = cip.get("creator_voice_signature", "")
            new_sig = str(data["creator_voice_signature"])[:200]
            if new_sig not in existing:
                cip["creator_voice_signature"] = (existing + " | " + new_sig).strip(" |")[:400]
        await save_cip(user_id, cip)
        logger.info(f"[cip_extract] user={user_id} agent={agent_key} → {data}")
    except Exception as e:
        logger.debug(f"[cip_extract] user={user_id} skipped: {e}")


async def _interview_step(update: Update, user_id: int,
                           spec: AgentSpec, text: str, session: dict) -> None:
    # Urgency bypass: пользователь хотел результат немедленно — пропускаем интервью
    if session.get("urgency"):
        session["initial"] = session.get("initial") or text
        session["history"].append({"role": "user", "content": text})
        await save_agent_session(user_id, spec.key, session)
        if spec.accept_photos:
            await _offer_photos(update, user_id, spec, session)
        elif spec.has_pick_step:
            await _gen_variants(update, user_id, spec, session)
        else:
            await generate(update, user_id, spec, session)
        return

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

    # Показываем typing пока идёт LLM-вызов
    import asyncio as _asyncio
    _typing_task = _asyncio.create_task(
        typing_loop(update.effective_chat)
    )
    try:
        next_msg = await llm.chat(ih, system=interviewer, temperature=0.4)
    except Exception as e:
        logger.error(f"[{spec.key}] interview LLM error: {e}")
        ih.pop()
        _typing_task.cancel()
        await send(update, "Связь прервалась — ответь ещё раз и продолжим 🔁",
                   reply_markup=kb(["⏭ Пропустить вопросы|agent_skip", "← Меню|menu_main"]))
        return
    finally:
        _typing_task.cancel()

    ih.append({"role": "assistant", "content": next_msg})
    session["history"] = ih
    session["q_count"] = session.get("q_count", 0) + 1
    await save_agent_session(user_id, spec.key, session)

    clean_msg = next_msg.replace("[READY]", "").replace("[ready]", "").strip()
    if _DONE_SIGNAL in next_msg.lower() or session["q_count"] >= spec.max_q:
        await send(update, clean_msg, parse_mode="Markdown")
        # Extract strategic signals from interview into CIP before generating
        await _extract_cip_from_interview(user_id, spec.key, session["history"])

        # Pre-generation strategic brief (#7) for heavy tools
        _BRIEF_TOOLS = ("warmup", "profile", "competitor")
        if spec.key in _BRIEF_TOOLS:
            try:
                _brief_sys = (
                    "На основе интервью сформулируй стратегический бриф — 3 строки, не больше. "
                    "Формат:\n"
                    "🎯 Главная задача: [одно конкретное предложение]\n"
                    "⚡ Ключевой инсайт: [что важнее всего учесть]\n"
                    "🛡 Главный риск: [что может пойти не так если не учесть]\n"
                    "Только бриф. Без вступлений."
                )
                _brief_history = session["history"]
                _brief = await llm.chat(_brief_history, system=_brief_sys, temperature=0.3)
                if _brief and _brief.strip():
                    session["pre_brief"] = _brief.strip()
                    await save_agent_session(user_id, spec.key, session)
                    await send(update,
                        f"*Вот как я вижу задачу:*\n\n{_brief.strip()}\n\n"
                        "Генерирую 🚀",
                        parse_mode="Markdown"
                    )
            except Exception:
                pass  # Brief is optional — don't block generation on error

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

    # Строим полный контекст: голос + ниша + CIP = уникальный output
    voice_ctx = await build_voice_context(user_id)
    niche_ctx = build_niche_context(profile.get("niche", ""))
    base_sys  = await _resolve_generator_custom(user_id, spec, session, profile)

    # Inject CIP strategic context
    cip_ctx = ""
    try:
        from db import get_cip, get_content_backlog
        cip = await get_cip(user_id)
        cip_parts = []

        # ── СТРАТЕГИЧЕСКИЕ РЕШЕНИЯ (меняют весь подход к инструменту) ──
        trust = cip.get("audience_trust_level", 0)
        soph  = cip.get("audience_sophistication", "medium")
        phase = cip.get("current_funnel_phase", "")

        if trust and trust < 4:
            cip_parts.append(
                "СТРАТЕГИЧЕСКОЕ РЕШЕНИЕ (ХОЛОДНАЯ АУДИТОРИЯ, доверие < 4): "
                "все хуки и заходы строить через УЗНАВАНИЕ и боль — не через авторитет автора. "
                "Никаких 'я знаю как'. Только 'ты сталкивался с этим?'. "
                "День 1 прогрева — ТОЛЬКО близость, ноль намёков на продукт."
            )
        elif trust and trust >= 8:
            cip_parts.append(
                "СТРАТЕГИЧЕСКОЕ РЕШЕНИЕ (ГОРЯЧАЯ АУДИТОРИЯ, доверие ≥ 8): "
                "можно давать внутренние инсайты и unpopular opinions. "
                "Identity-хуки работают лучше pain-хуков на этом уровне доверия."
            )

        if soph == "low":
            cip_parts.append(
                "СТРАТЕГИЧЕСКОЕ РЕШЕНИЕ (НОВИЧКИ): давай permission ('можно') и clarity ('вот как'). "
                "Избегай жаргона ниши — используй язык которым аудитория сама описывает проблему."
            )
        elif soph == "high":
            cip_parts.append(
                "СТРАТЕГИЧЕСКОЕ РЕШЕНИЕ (ПРОДВИНУТАЯ АУДИТОРИЯ): давай insider perspective ('вот что видят только те кто внутри'). "
                "Не разжёвывай базу — цени их время и уровень."
            )

        if phase == "awareness":
            cip_parts.append(
                "СТРАТЕГИЧЕСКОЕ РЕШЕНИЕ (ЭТАП AWARENESS): контент работает на виральность и узнавание. "
                "CTA → любопытство (вопрос / DM / 'хочешь узнать больше?'). Никаких продажных элементов."
            )
        elif phase == "conversion":
            cip_parts.append(
                "СТРАТЕГИЧЕСКОЕ РЕШЕНИЕ (ЭТАП CONVERSION): контент работает на решение. "
                "CTA → конкретное действие (ссылка / DM со словом / запись). "
                "Усиливай конкретность обещания, убирай расплывчатые 'поможет', 'улучшит'."
            )

        # ── ФАКТИЧЕСКИЙ КОНТЕКСТ (информация для инструмента) ──
        if cip.get("recent_topics"):
            cip_parts.append(f"Недавние темы (не повторяй): {', '.join(cip['recent_topics'][-5:])}")
        if cip.get("hooks_that_worked"):
            cip_parts.append(f"Хуки которые работали (мутируй, не повторяй): {'; '.join(cip['hooks_that_worked'][-3:])}")
        if trust and 4 <= trust < 8:
            cip_parts.append(f"Уровень доверия аудитории: {trust}/10")
        if cip.get("audience_primary_objection"):
            cip_parts.append(
                f"Главное возражение аудитории: {cip['audience_primary_objection']} "
                f"— учитывай при написании ВСЕХ инструментов, не только прогрева."
            )
        if cip.get("positioning_statement"):
            cip_parts.append(f"Позиционирование автора: {cip['positioning_statement']} — используй при генерации хуков и заголовков.")
        if cip.get("audience_language_patterns"):
            cip_parts.append(f"Язык аудитории (используй в хуках): {cip['audience_language_patterns']}")

        # #15: Competitor insight → hook differentiation (reels, carousel, qi_start)
        if cip.get("positioning_statement") and spec.key in ("reels_short", "reels_adapt", "carousel", "qi_start"):
            cip_parts.append(
                f"КОНКУРЕНТНЫЙ КОНТЕКСТ ДЛЯ ХУКОВ: если конкурент в нише использует pain-хуки (боль/страх) — "
                f"попробуй identity-хуки как контрпозиционирование (узнавание + принадлежность). "
                f"Если конкурент использует авторитет — используй близость и честность. "
                f"Позиционирование автора уже определено: {cip['positioning_statement']}"
            )

        # Format affinity: surfaces which formats the audience has responded to
        _affinity = cip.get("content_format_affinity", {})
        if _affinity:
            _top = sorted(_affinity.items(), key=lambda x: x[1], reverse=True)[:2]
            _low = [k for k, v in _affinity.items() if v < 0.4]
            if _top and _top[0][1] > 0.6:
                cip_parts.append(f"Форматы которые аудитория одобряет: {', '.join(k for k, _ in _top)}")
            if _low:
                cip_parts.append(f"Форматы которые аудитория отвергает: {', '.join(_low)}")
            # For brainstorm and planner: explicitly weight toward approved formats
            if spec.key in ("qi_start", "planner") and _top and _top[0][1] > 0.6:
                _preferred = _top[0][0]
                _fmt_map = {
                    "reels_short": "Рилс", "carousel": "Карусель",
                    "post": "Пост", "stories": "Сторис",
                    "warmup": "Прогрев", "talking_head": "Talking Head",
                }
                _pref_name = _fmt_map.get(_preferred, _preferred)
                cip_parts.append(
                    f"СТРАТЕГИЧЕСКОЕ РЕШЕНИЕ: аудитория автора лучше реагирует на {_pref_name} "
                    f"(score {_top[0][1]:.1f}/1.0) — отдавай предпочтение этому формату "
                    "при генерации идей и плана, если нет веской причины использовать другой."
                )
        if cip.get("active_launch"):
            cip_parts.append("АКТИВНЫЙ ЗАПУСК: контент должен поддерживать прогрев")
        if cip.get("narrative_continuity"):
            cip_parts.append(f"Незакрытые истории (можно продолжить): {cip['narrative_continuity']}")
        # For planner — inject backlog
        if spec.key in ("planner", "qi_start"):
            backlog = await get_content_backlog(user_id)
            unused = [b["idea"] for b in backlog if not b.get("used")][:5]
            if unused:
                cip_parts.append(f"Идеи из банка идей (используй если подходят): {'; '.join(unused)}")
        if cip_parts:
            cip_ctx = "\n\nСТРАТЕГИЧЕСКИЙ КОНТЕКСТ:\n" + "\n".join(cip_parts)
    except Exception:
        pass

    # Platform-native language filter for video/carousel tools
    platform_filter = ""
    try:
        from config import PLATFORM_NATIVE_REELS, PLATFORM_NATIVE_CAROUSEL, MIRA_KNOWLEDGE_LAYER
        if spec.key in ("reels_short", "talking_head", "cartoon", "reels_adapt"):
            platform_filter = PLATFORM_NATIVE_REELS
        elif spec.key in ("carousel",):
            platform_filter = PLATFORM_NATIVE_CAROUSEL
        # Knowledge layer for all tools
        knowledge_layer = MIRA_KNOWLEDGE_LAYER
    except Exception:
        platform_filter = ""
        knowledge_layer = ""

    sys_prompt = base_sys + voice_ctx + niche_ctx + cip_ctx + platform_filter + knowledge_layer

    # ── Image context: если есть OCR-текст со скриншота — инжектим в промпт ──
    image_ctx = ""
    try:
        from db import kv_get as _kv_get
        raw_img_ctx = await _kv_get(user_id, "__image_context__")
        if raw_img_ctx:
            img_data = __import__("json").loads(raw_img_ctx)
            extracted = img_data.get("extracted_text", "")
            intent    = img_data.get("intent", "reference")
            if extracted:
                _intent_labels = {
                    "post_example":    "примеры постов пользователя",
                    "profile_audit":   "скриншот профиля для разбора",
                    "competitor_post": "пост конкурента для анализа",
                    "viral_reel":      "вирусный рилс для адаптации",
                    "reference":       "дополнительный контекст",
                }
                _label = _intent_labels.get(intent, "дополнительный контекст")
                image_ctx = (
                    f"\n\nКОНТЕКСТ ИЗ СКРИНШОТА ({_label}):\n"
                    f"{extracted[:2000]}\n"
                    f"Учитывай этот текст при генерации."
                )
    except Exception as _img_err:
        logger.debug(f"image_context inject skipped: {_img_err}")

    sys_prompt = sys_prompt + image_ctx

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

    # Track generated topic in CIP for novelty filtering in future brainstorms
    try:
        from db import add_recent_topic as _art
        _topic_hint = session.get("initial", "")[:80] or spec.name
        await _art(user_id, _topic_hint)
    except Exception:
        pass

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

    # WHY LAYER — обучающий слой (одно предложение о механизме)
    try:
        from config import WHY_LAYER_PROMPT
        why_sys = WHY_LAYER_PROMPT
        why_result = await llm.complete(
            why_sys,
            f"Инструмент: {spec.key}\nКонтент:\n{result[:800]}",
            temperature=0.5,
        )
        if why_result and why_result.strip():
            await send(update, why_result.strip(), parse_mode="Markdown")
    except Exception:
        pass

    # CONTENT RHYTHM ANALYSIS — track emotional register, warn on fatigue
    try:
        from config import (
            CONTENT_EMOTION_EXTRACT_SYSTEM, CONTENT_RHYTHM_WARNING,
            _EMOTION_ALTERNATIVES
        )
        from db import get_cip, save_cip as _sc
        _emotion_raw = await llm.complete(
            CONTENT_EMOTION_EXTRACT_SYSTEM, result[:600], temperature=0.0
        )
        _emotion = _emotion_raw.strip().lower().split()[0] if _emotion_raw else ""
        if _emotion:
            _cip = await get_cip(user_id)
            _rhythm = _cip.get("emotional_rhythm", [])
            _rhythm.append(_emotion)
            _rhythm = _rhythm[-10:]  # keep last 10
            _cip["emotional_rhythm"] = _rhythm
            await _sc(user_id, _cip)
            # Warn if last 5 are all same emotion
            if len(_rhythm) >= 5 and len(set(_rhythm[-5:])) == 1:
                _alt = _EMOTION_ALTERNATIVES.get(_emotion, "другой угол")
                await send(
                    update,
                    CONTENT_RHYTHM_WARNING.format(emotion=_emotion, suggestion=_alt),
                    parse_mode="Markdown",
                )
    except Exception:
        pass

    # MICRO-LAUNCH DETECTION (#28) — triggered when warmup completes AND
    # active_launch was True before this generation (user was in a launch).
    # Detect by CIP state, not by text content (text always contains Day 3 offer).
    try:
        if spec.key == "warmup":
            from config import MICRO_LAUNCH_FOLLOWUP
            from db import get_cip, save_cip as _sc2
            _cip2 = await get_cip(user_id)
            # Only fire if user was explicitly in an active launch
            if _cip2.get("active_launch"):
                # Warmup generation = launch sequence written = mark completed
                _cip2["active_launch"] = False
                _cip2["launch_completed_at"] = str(__import__("datetime").date.today())
                await _sc2(user_id, _cip2)
                await asyncio.sleep(1.5)
                await send(update, MICRO_LAUNCH_FOLLOWUP, parse_mode="Markdown")
    except Exception:
        pass

    # WARMUP OBJECTION TRACKER (#22) — extract which objections are neutralized,
    # flag any uncovered ones from the primary objection stored in CIP
    try:
        if spec.key == "warmup":
            from db import get_cip, save_cip as _sc3
            _cip3 = await get_cip(user_id)
            _primary_obj = _cip3.get("audience_primary_objection", "")
            # Extract objections mentioned in the result
            _obj_extract_sys = (
                "Из текста прогрева извлеки список возражений которые нейтрализуются в контенте. "
                "Возражение нейтрализуется когда текст прямо или косвенно снимает сомнение. "
                "Ответь ТОЛЬКО JSON: {\"neutralized\": [\"возражение1\", \"возражение2\"]}"
            )
            _obj_raw = await llm.complete(_obj_extract_sys, result[:1000], temperature=0.0)
            import json as _json
            _obj_start = _obj_raw.find("{")
            _obj_end = _obj_raw.rfind("}") + 1
            if _obj_start >= 0 and _obj_end > _obj_start:
                _obj_data = _json.loads(_obj_raw[_obj_start:_obj_end])
                _neutralized = _obj_data.get("neutralized", [])
                _cip3["warmup_neutralized_objections"] = _neutralized
                await _sc3(user_id, _cip3)
                # Warn if primary objection seems uncovered
                if _primary_obj and _neutralized:
                    _covered = any(
                        _primary_obj.lower()[:20] in obj.lower()
                        for obj in _neutralized
                    )
                    if not _covered:
                        await send(
                            update,
                            f"⚠️ *Обрати внимание:* главное возражение «{_primary_obj[:80]}» "
                            "явно не нейтрализовано в прогреве. "
                            "Хочешь добавить это в один из дней?",
                            parse_mode="Markdown",
                            reply_markup=kb(
                                ["➕ Добавить в прогрев|ag_edit_add_proof", "← Пропустить|menu_main"]
                            ),
                        )
    except Exception:
        pass

    # NARRATIVE CONTINUITY TRACKER (#12) — после warmup и stories
    # Извлекает незакрытые истории/темы с высоким потенциалом продолжения
    try:
        if spec.key in ("warmup", "stories"):
            from db import get_cip as _gcip_nc, save_cip as _sc_nc
            _cip_nc = await _gcip_nc(user_id)
            _nc_sys = (
                "Из этого контента извлеки незакрытые истории или темы которые явно ждут продолжения "
                "(часть 1, 'об этом подробнее расскажу', незавершённый нарратив, тема которая вызовет вопросы). "
                "Ответь ТОЛЬКО JSON: {\"open_threads\": [\"краткое описание незакрытой истории\"]}. "
                "Если таких нет — {\"open_threads\": []}. Максимум 3."
            )
            _nc_raw = await llm.complete(_nc_sys, result[:1500], temperature=0.0)
            import json as _json_nc
            _nc_s = _nc_raw.find("{")
            _nc_e = _nc_raw.rfind("}") + 1
            if _nc_s >= 0 and _nc_e > _nc_s:
                _nc_data = _json_nc.loads(_nc_raw[_nc_s:_nc_e])
                _threads = _nc_data.get("open_threads", [])
                if _threads:
                    _existing_nc = _cip_nc.get("narrative_continuity", "")
                    _new_nc = " | ".join(_threads)
                    # Merge with existing, keep last 5 threads max
                    _all_nc = [t.strip() for t in _existing_nc.split("|") if t.strip()] + _threads
                    _cip_nc["narrative_continuity"] = " | ".join(_all_nc[-5:])[:400]
                    await _sc_nc(user_id, _cip_nc)
    except Exception:
        pass

    # AUDIENCE FATIGUE DETECTOR (#24) — если последние 5 постов одной темы
    try:
        from db import get_results as _gr_fat, get_cip as _gcip_fat, save_cip as _sc_fat
        _recent_fat = await _gr_fat(user_id, limit=6)
        if len(_recent_fat) >= 5:
            _fat_topics = [r.get("agent_key", "") for r in _recent_fat[:5]]
            # Check topic repetition via quick LLM call on last 5 contents
            _fat_snippets = "\n---\n".join(
                r.get("content", "")[:200] for r in _recent_fat[:5]
            )
            _fat_sys = (
                "Проверь последние 5 единиц контента. Есть ли доминирующая тема которая повторяется "
                "в 4 или 5 из них? Ответь JSON: "
                "{\"fatigue\": true/false, \"dominant_theme\": \"тема или null\", "
                "\"switch_suggestion\": \"конкретный альтернативный угол или null\"}. "
                "Только JSON, без пояснений."
            )
            _fat_raw = await llm.complete(_fat_sys, _fat_snippets, temperature=0.0)
            _fat_s = _fat_raw.find("{")
            _fat_e = _fat_raw.rfind("}") + 1
            if _fat_s >= 0 and _fat_e > _fat_s:
                import json as _json_fat
                _fat_data = _json_fat.loads(_fat_raw[_fat_s:_fat_e])
                if _fat_data.get("fatigue") and _fat_data.get("dominant_theme"):
                    _theme = _fat_data["dominant_theme"]
                    _switch = _fat_data.get("switch_suggestion") or "другой формат или эмоцию"
                    await send(
                        update,
                        f"👀 *Замечаю паттерн:* последние 5 материалов — все вокруг темы «{_theme[:60]}».\n"
                        f"Аудитория может устать. Попробуй следующий через другой угол: _{_switch}_",
                        parse_mode="Markdown",
                    )
    except Exception:
        pass

    # Стикер/GIF при первой генерации
    try:
        from db import get_stats
        from ui.media import send_gif, send_sticker
        _stats = await get_stats(user_id)
        if _stats.get("total", 0) == 1:
            if not await send_sticker(update, "done"):
                await send_gif(update, "first_generation")
    except Exception:
        pass

    await _after_result(update, spec, user_id)


async def _after_result(update: Update, spec: AgentSpec, user_id: int) -> None:
    """
    После результата — одно единое сообщение с действиями.

    АРХИТЕКТУРНЫЙ ПРИНЦИП:
    Было: 4 отдельных сообщения (правки + voice feedback + upsell + proactive)
    Стало: 1 сообщение с панелью правок + отложенный voice feedback

    Upsell и proactive убраны из этого потока:
    - Upsell показывается через конверсионный триггер по счётчику (intent_oneshot)
    - Proactive suggestion показывается через 10 мин (job) или при следующем /menu
    """
    from db import get_results, kv_set

    _result_id = 0
    try:
        _recent = await get_results(user_id, limit=1)
        if _recent:
            _result_id = _recent[0]["id"]
            _content   = _recent[0].get("content", "")
            if _content:
                try:
                    _s = await get_agent_session(user_id, spec.key) or {}
                    _s["last_result"] = _content
                    _s["spec_key"]    = spec.key
                    # Save original_result for drift recovery (🔙 button)
                    if not _s.get("original_result"):
                        _s["original_result"] = _content
                    # Persist strategic context so apply_edit can pass it to REFINE_SYSTEM
                    try:
                        from db import get_cip as _gcip
                        _cip = await _gcip(user_id)
                        _s["funnel_stage"]    = _cip.get("current_funnel_phase", "awareness")
                        _s["audience_trust"]  = _cip.get("audience_trust_level", 5)
                    except Exception:
                        pass
                    await save_agent_session(user_id, f"__ag_edit_{spec.key}__", _s)
                except Exception:
                    pass
    except Exception:
        pass

    # Стрик
    try:
        from ui.home import update_streak_on_result as _usr
        await _usr(user_id)
    except Exception:
        pass

    # Строим подсказку voice-прогресса для первой строки кнопки
    _voice_hint = ""
    if _result_id:
        try:
            from voice_learner import get_voice_stats
            from ui.progress_bar import voice_progress_short
            _vs    = await get_voice_stats(user_id)
            _total = _vs.get("total_signals", 0)
            _voice_hint = voice_progress_short(_total)
        except Exception:
            pass

    # Единственное сообщение после результата — панель правок + voice feedback
    # Структура: voice question наверху (важнее), затем правки
    if _result_id:
        from utils import kb as _kb
        from voice_learner import voice_feedback_kb as _vfkb

        # Tool-specific edit panel (returns kb + drift_warning)
        _panel_kb, _drift = _agent_edit_panel_kb(spec.key)
        # Combine with voice feedback
        combined_kb = _vfkb(_result_id, extra_rows=[
            r for r in _panel_kb.inline_keyboard
        ])
        await send(
            update,
            f"Звучит как твой голос?{_voice_hint}",
            parse_mode="Markdown",
            reply_markup=combined_kb,
        )
    else:
        # Нет result_id — только панель правок
        _panel_kb, _drift = _agent_edit_panel_kb(spec.key)
        await send(update, "Докрути под себя 👇", reply_markup=_panel_kb)

    # Сохраняем отложенную proactive-подсказку — покажется при следующем меню
    try:
        from flows.proactive import schedule_proactive_hint
        await schedule_proactive_hint(user_id, spec.key)
    except Exception:
        pass

    # УМНЫЕ FOLLOW-UPS (фаза 1.4) — 2 контекстных предложения вместо generic меню.
    # Если запрос был голосовым — используем theme из voice_ctx.
    # Если нет голосового контекста — fallback на INTER_TOOL_MAP.
    try:
        import json as _json
        _voice_ctx_raw = await __import__("db").kv_get(user_id, "__voice_ctx__")
        _voice_ctx = _json.loads(_voice_ctx_raw) if _voice_ctx_raw else {}
        _key_theme = _voice_ctx.get("key_theme") or ""

        _INTER_TOOL_MAP = {
            "reels_short":  ("carousel",     "Хочешь карусель на эту же тему? Сохраню контекст →", "💎 Карусель на эту тему|inter_tool_carousel"),
            "carousel":     ("reels_short",  "Хочешь Рилс-хук для этой карусели?",                "🎬 Рилс-хук|inter_tool_reels_short"),
            "warmup":       ("post",         "Хочешь экспертный пост чтобы поддержать прогрев?",  "📝 Пост поддержки|inter_tool_post"),
            "stories":      ("reels_short",  "Хочешь Рилс по этой же теме?",                      "🎬 Рилс на эту тему|inter_tool_reels_short"),
            "post":         ("carousel",     "Хочешь развернуть это в карусель?",                  "💎 Карусель|inter_tool_carousel"),
            "profile":      ("competitor",   "Хочешь теперь разобрать конкурента рядом с тобой?", "🔍 Анализ конкурента|inter_tool_competitor"),
        }
        if spec.key in _INTER_TOOL_MAP:
            _it_target, _it_text, _it_btn = _INTER_TOOL_MAP[spec.key]
            # Если есть голосовая тема — персонализируем текст
            if _key_theme:
                _it_text = f"Хочешь {_it_text.split('Хочешь')[-1].strip()} по теме «{_key_theme}»?"
            await asyncio.sleep(0.8)
            await send(
                update,
                f"💡 {_it_text}",
                reply_markup=kb([_it_btn, "← Не сейчас|menu_main"]),
            )
    except Exception:
        pass

    # POST-PERFORMANCE FEEDBACK (#22) — кнопка «Результат» появляется через 24ч (3-е использование+)
    # Планируем напоминание через KV — обработчик покажет кнопку при следующем входе
    try:
        import datetime as _dt
        from db import kv_set as _kv_ppf, kv_get as _kv_get_ppf
        _ppf_key = f"__ppf_pending_{spec.key}__"
        _ppf_existing = await _kv_get_ppf(user_id, _ppf_key)
        if not _ppf_existing:
            # Schedule: store result_id + timestamp, show reminder on next /menu
            _ppf_payload = {
                "result_id": _result_id,
                "spec_key": spec.key,
                "scheduled_at": _dt.datetime.utcnow().isoformat(),
            }
            import json as _json_ppf
            await _kv_ppf(user_id, _ppf_key, _json_ppf.dumps(_ppf_payload))
    except Exception:
        pass


async def apply_agent_edit(update: Update, user_id: int, edit_key: str) -> None:
    """
    Универсальный обработчик правок из панели агента.
    Аналог _apply_edit() в carousel.py.
    Вызывается из callbacks.py по ag_edit_* callback_data.
    """
    # BUG #3 FIX: read the persisted edit session first (it carries completed_actions).
    # Only fall back to get_results() for the content itself.
    from db import get_results, kv_get as _kvg

    _spec_key = None
    _edit_session = None

    # Step 1: find active agent key
    try:
        _active = await _kvg(user_id, "__active_agent__")
        if _active and _active not in (_RS_KEY, _CAR_KEY):
            _spec_key = _active
    except Exception:
        pass

    # Step 2: read persisted edit session (has completed_actions + last_result)
    if _spec_key:
        try:
            _edit_session = await get_agent_session(user_id, f"__ag_edit_{_spec_key}__")
        except Exception:
            pass

    # Step 3: if no edit session with content, fall back to DB result
    _s = None
    if _edit_session and _edit_session.get("last_result"):
        _s = _edit_session
    else:
        try:
            _recent = await get_results(user_id, limit=1)
            if _recent:
                _spec_key = _spec_key or _recent[0]["agent_key"]
                # Merge: DB content + any completed_actions already persisted in edit session
                _s = {
                    "last_result":       _recent[0]["content"],
                    "spec_key":          _spec_key,
                    "completed_actions": (_edit_session or {}).get("completed_actions", []),
                }
        except Exception:
            pass

    if not _s or not _s.get("last_result"):
        await send(update, "Нет материала для правки — создай что-нибудь сначала.",
                   reply_markup=kb(["← Меню|menu_main"]))
        return

    current   = _s["last_result"]
    _spec_key = _spec_key or "post"
    prompts   = _get_agent_refine_prompts(_spec_key)
    instruction = prompts.get(edit_key, f"Улучши текст: {edit_key}.")

    profile = await get_profile(user_id)
    spec    = get_spec(_spec_key)

    # Warmup Day 1 bolder guard
    if instruction == "__WARMUP_BOLDER_GUARD__":
        session_data = _s
        warmup_day = session_data.get("warmup_day", 0)
        if warmup_day == 1 or "ДЕНЬ 1" in current[:500]:
            await send(update,
                "⚠️ *День 1 прогрева строит доверие — давление здесь разрушает всю стратегию.*\n\n"
                "Могу усилить эмоциональный резонанс сохраняя мягкость. Хочешь так?",
                parse_mode="Markdown",
                reply_markup=kb(
                    ["✅ Да, усилить резонанс|ag_edit_add_detail", "← Назад|menu_main"]
                ))
            return
        else:
            instruction = "Усиль прогрев: конкретнее про боль аудитории, убери расплывчатые формулировки. ДЕНЬ 3: можно усилить ощущение срочности."

    # Build strategic context for refine
    from db import get_cip
    cip = await get_cip(user_id)
    funnel_stage = cip.get("current_funnel_phase", "awareness")
    audience_trust = cip.get("audience_trust_level", 5)

    # Funnel-aware instruction override for context-sensitive edits
    if edit_key == "bolder" and _spec_key not in ("warmup",):
        if funnel_stage == "awareness" and audience_trust < 5:
            instruction = (
                "ЖЁСТЧЕ — но в рамках ДОВЕРИТЕЛЬНОГО фрейма (awareness + холодная аудитория): "
                "усиль конфликт ТОЛЬКО в описании проблемы. "
                "Не усиливай авторитетность — усиливай узнаваемость боли. "
                "ЗАЩИЩЕНО: тон автора должен оставаться живым и человеческим."
            )
        elif funnel_stage == "conversion":
            instruction = (
                "ЖЁСТЧЕ — через срочность и конкретность (этап conversion): "
                "усиль конкретность обещания, убери расплывчатые 'поможет', 'улучшит'. "
                "Можно добавить конкретный срок если есть логическое основание. "
                "ЗАЩИЩЕНО: не создавай искусственного дефицита — аудитория на этой стадии чувствует манипуляцию."
            )

    # softer_confirmed: user acknowledged the conversion-stage warning, proceed without another warning
    if edit_key == "softer_confirmed":
        instruction = (
            "Смягчи текст: убери давление и срочность, сделай тон мягче и человечнее. "
            "ВАЖНО: сохрани суть оффера и его конкретность — меняй только интенсивность, не предмет."
        )

    if edit_key == "softer" and funnel_stage == "conversion":
        # Warn before softening a conversion text
        await send(update,
            "⚠️ *Смягчаю текст этапа конверсии — это может снизить эффективность CTA.*\n\n"
            "Уверена что хочешь продолжить?",
            parse_mode="Markdown",
            reply_markup=kb([
                "✅ Да, смягчить|ag_edit_softer_confirmed",
                "← Назад|menu_main"
            ]))
        return

    system = (
        f"Ты — стратегический редактор контента. "
        f"Ниша автора: {profile.get('niche', '')}. "
        f"Аудитория: {profile.get('audience', '')}. "
        f"Тон: {profile.get('tone', 'живой')}.\n"
        f"Инструмент: {_spec_key}. Этап воронки: {funnel_stage}. Доверие аудитории: {audience_trust}/10.\n\n"
        f"ТЕКУЩИЙ МАТЕРИАЛ:\n{current}"
    )

    status = await update.effective_chat.send_message("Переписываю...")
    import asyncio as _aio
    _tt = _aio.create_task(typing_loop(update.effective_chat))
    try:
        from llm import complete_long
        new_result = await complete_long(system, instruction, model_key="claude", temperature=0.8)
    except Exception as e:
        logger.error(f"[agents] apply_edit {edit_key} failed: {e}")
        _tt.cancel()
        await safe_delete(status)
        _panel_kb, _ = _agent_edit_panel_kb(_spec_key)
        await send(update, "Не получилось — попробуй ещё раз 🔁", reply_markup=_panel_kb)
        return
    finally:
        _tt.cancel()

    await safe_delete(status)

    if not new_result or not new_result.strip():
        _panel_kb, _ = _agent_edit_panel_kb(_spec_key)
        await send(update, "Пустой ответ — попробуй ещё раз.",
                   reply_markup=_panel_kb)
        return

    # Трекинг завершённых действий — эта кнопка исчезнет из следующей клавиатуры
    _completed = set(_s.get("completed_actions", []))
    _completed.add(edit_key)
    _refinement_count = _s.get("refinement_count", 0) + 1

    # Обновляем сохранённый last_result
    _s["last_result"] = new_result
    _s["refinement_count"] = _refinement_count
    try:
        await save_agent_session(user_id, f"__ag_edit_{_spec_key}__", _s)
    except Exception:
        pass

    # Side-effect: save positioning_statement to CIP after competitor edit
    if edit_key == "positioning" and _spec_key == "competitor":
        try:
            from db import get_cip, save_cip as _sc
            _cip = await get_cip(user_id)
            # Extract the positioning sentence from the result
            for _line in new_result.split("\n"):
                if "В отличие от" in _line or "отличие" in _line.lower():
                    _cip["positioning_statement"] = _line.strip()[:300]
                    break
            await _sc(user_id, _cip)
        except Exception:
            pass

    try:
        _name = spec.name if spec else _spec_key
        await save_result(user_id, _spec_key, f"{_name} (правка)", new_result)
    except Exception:
        pass

    # Отправляем
    full  = f"✅ *Готово!*\n\n{new_result}"
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

    _panel_kb, _drift = _agent_edit_panel_kb(_spec_key, _completed, _refinement_count)
    await send(update, f"Ещё докрутить?{_drift}", parse_mode="Markdown", reply_markup=_panel_kb)
