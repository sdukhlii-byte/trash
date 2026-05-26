"""
handlers/callbacks.py — тонкий роутер callback-кнопок.

Вся бизнес-логика делегируется в flows/* и ui/*.
"""
import asyncio
import json
import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from db import (
    get_agent_session, save_agent_session, clear_agent_session,
    clear_all_agent_sessions, clear_onboarding_state, get_profile,
    kv_get, kv_set, kv_del, get_model, set_model,
    save_result, get_results, get_result_by_id, delete_result,
    get_result_kb_state, mark_result_action_completed, reset_result_completed,
)
from user_state import get_user_state, has_access, UserState, invalidate_state_cache
from lava_payments import (
    grant_trial, get_payment_link, render_status, render_history, render_referral,
    get_subscription, has_used_trial, TRIAL_DAYS,
)
from flows import reels, carousel
from flows.reels import _RS_KEY
from flows.reels import (
    rs_edit_softer, rs_edit_bolder, rs_edit_top5,
    rs_edit_style, rs_apply_style, rs_edit_back,
    rs_pick_for_desc, rs_regen,
)
from flows.carousel import _CAR_KEY, carousel_format_kb, carousel_trigger_kb
from flows.misc import (
    qi_start, qi_save_backlog, refine_start, regen_last, regen_by_id,
    planner_show, planner_gen_week, planner_add_start,
    daily_menu, style_menu, style_add_start,
    schedule_daily, diagnostic_start,
)
from ui.menu import show_menu, main_menu_kb, more_menu_kb, model_kb
from ui.paywall import show_paywall
from ui.cabinet import show_cabinet
from handlers.messages import _detect_active_agent
from utils import typing_loop
from prompt_editor import (
    pe_menu, pe_show_category, pe_view_prompt,
    pe_start_edit, pe_save_text, pe_reset, get_category_for_slug,
    _PE_KEY,
)
from utils import send, edit, kb, safe_delete
from security import ADMIN_ID
from config import CAROUSEL_FORMATS_4, CAROUSEL_TRIGGERS_20, MODELS
import agents as ag
from callback_context import (
    resolve_callback, validate_callback, mark_callback_used,
    expire_result_callbacks,
)

logger = logging.getLogger(__name__)
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "Stanley_Berks")


async def _rebuild_and_edit_keyboard(query, user_id: int, result_id: int, tool_id: str) -> None:
    """Перестраивает клавиатуру из актуального состояния result и редактирует сообщение."""
    try:
        updated = await get_result_kb_state(result_id)
        if updated:
            from ui.keyboards import build_result_keyboard
            new_kb = await build_result_keyboard(user_id, updated, tool_id)
            await query.message.edit_reply_markup(reply_markup=new_kb)
    except Exception as e:
        logger.debug(f"[scoped] rebuild_keyboard failed (non-critical): {e}")


async def _route_scoped_action(
    update, ctx, query, user_id: int, resolved, result_row: dict
) -> None:
    """
    PHASE 4+5: Единая точка входа для всех scoped callbacks.
    resolved: ResolvedCallback (tool_id уже заполнен из validate_callback)
    result_row: dict из get_result_kb_state
    """
    from callback_context import ACTION_REGISTRY

    action    = resolved.action
    result_id = resolved.result_id
    tool_id   = resolved.tool_id  # проставлен в validate_callback

    # ── Reels ─────────────────────────────────────────────────────────────────
    if action == "rs:softer":
        await rs_edit_softer(update, user_id, result_id=result_id)

    elif action == "rs:bolder":
        await rs_edit_bolder(update, user_id, result_id=result_id)

    elif action == "rs:top5":
        await rs_edit_top5(update, user_id, result_id=result_id)
        await mark_result_action_completed(result_id, action)
        await expire_result_callbacks(result_id)
        await _rebuild_and_edit_keyboard(query, user_id, result_id, tool_id)

    elif action == "rs:style":
        await rs_edit_style(update, user_id, result_id=result_id)

    elif action == "rs:regen":
        await reset_result_completed(result_id)
        await rs_regen(update, user_id, result_id=result_id)

    elif action == "rs:pick_desc":
        await rs_pick_for_desc(update, user_id, result_id=result_id)
        await mark_result_action_completed(result_id, action)
        await _rebuild_and_edit_keyboard(query, user_id, result_id, tool_id)

    # ── Carousel ──────────────────────────────────────────────────────────────
    elif action == "car:headline":
        await carousel.car_edit_headline(update, user_id, result_id=result_id)

    elif action == "car:softer":
        await carousel.car_edit_softer(update, user_id, result_id=result_id)

    elif action == "car:bolder":
        await carousel.car_edit_bolder(update, user_id, result_id=result_id)

    elif action == "car:shorten":
        await carousel.car_edit_shorten(update, user_id, result_id=result_id)
        await mark_result_action_completed(result_id, action)
        await _rebuild_and_edit_keyboard(query, user_id, result_id, tool_id)

    elif action == "car:add_slide":
        await carousel.car_edit_add_slide(update, user_id, result_id=result_id)
        await mark_result_action_completed(result_id, action)
        await _rebuild_and_edit_keyboard(query, user_id, result_id, tool_id)

    elif action == "car:trigger":
        await carousel.car_edit_trigger(update, user_id, result_id=result_id)

    elif action == "car:format":
        await carousel.car_edit_format(update, user_id, result_id=result_id)

    # ── Generic agents ────────────────────────────────────────────────────────
    elif action.startswith("ag:"):
        _ag_key_map = {
            "ag:softer":      "softer",
            "ag:bolder":      "bolder",
            "ag:shorter":     "shorter",
            "ag:detail":      "add_detail",
            "ag:cta":         "stronger_cta",
            "ag:resonance":   "resonance",
            "ag:add_proof":   "add_proof",
            "ag:mid_hook":    "mid_hook",
            "ag:hook":        "hook",
            "ag:deepen":      "deepen",
            "ag:tactics":     "tactics",
            "ag:positioning": "positioning",
            "ag:save_moment": "save_moment",
        }
        edit_key = _ag_key_map.get(action)
        if edit_key:
            await ag.apply_agent_edit(update, user_id, edit_key, result_id=result_id)

    # ── Universal ─────────────────────────────────────────────────────────────
    elif action == "regen":
        await regen_by_id(update, user_id, result_id)

    elif action == "refine":
        await clear_agent_session(user_id, "refine_flow")
        await refine_start(update, user_id, result_id=result_id)

    else:
        logger.warning(f"[scoped] unhandled action={action!r} user={user_id}")


# ── Entry point ────────────────────────────────────────────────────────────────

async def callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    user_id = update.effective_user.id
    data    = query.data
    await query.answer()

    # Защита от двойного тапа
    from handlers.messages import _get_user_lock
    lock = await _get_user_lock(user_id)
    if lock.locked():
        logger.info(f"[callback dedup] user={user_id} data={data!r}")
        return
    async with lock:
        await _dispatch(update, ctx, query, user_id, data)


# ── BUG #2 / #4 FIX: centralized stale-callback guards ───────────────────────

async def _rs_stale_guard(user_id: int, query) -> bool:
    """Returns True if the reels callback is truly stale (no session AND no snapshot).
    If session is missing but snapshot exists — restores it silently and returns False.
    """
    from db import get_agent_session as _g, save_agent_session as _sa
    s = await _g(user_id, _RS_KEY)
    if s and s.get("last_result"):
        return False

    # Session cleared — try snapshot
    from flows.reels import _load_rs_snapshot, _RS_KEY as _RK
    snapshot = await _load_rs_snapshot(user_id)
    if snapshot and snapshot.get("last_result"):
        restored = {
            "step":              "done",
            "last_result":       snapshot["last_result"],
            "headlines":         snapshot["last_result"],
            "topic":             snapshot.get("topic", ""),
            "style":             snapshot.get("style", ""),
            "completed_actions": snapshot.get("completed_actions", []),
        }
        await _sa(user_id, _RK, restored)
        return False  # guard passes — session restored

    await query.answer("⚠️ Нет сохранённых хуков для правки. Начни новую тему.", show_alert=True)
    return True


async def _car_stale_guard(user_id: int, query) -> bool:
    """Returns True if the carousel callback is truly stale (no session AND no snapshot).
    If session is missing but snapshot exists — restores it silently and returns False.
    """
    from db import get_agent_session as _g, save_agent_session as _sa
    s = await _g(user_id, _CAR_KEY)
    if s and s.get("last_result"):
        return False

    # Session cleared (e.g. user started a new carousel) — try snapshot
    from flows.carousel import _load_car_snapshot, _CAR_KEY as _CK
    snapshot = await _load_car_snapshot(user_id)
    if snapshot and snapshot.get("last_result"):
        # Restore session from snapshot so the edit can proceed
        restored = {
            "step":             "done",
            "last_result":      snapshot["last_result"],
            "headline":         snapshot.get("headline", ""),
            "fmt_label":        snapshot.get("fmt_label", ""),
            "trigger_label":    snapshot.get("trigger_label", ""),
            "fmt":              snapshot.get("fmt", ""),
            "trigger":          snapshot.get("trigger", ""),
            "completed_actions": snapshot.get("completed_actions", []),
        }
        await _sa(user_id, _CK, restored)
        return False  # guard passes — session restored

    await query.answer("⚠️ Нет сохранённой карусели для правки. Создай новую.", show_alert=True)
    return True


async def _dispatch(update, ctx, query, user_id: int, data: str) -> None:
    """
    Главный роутер callback-кнопок.
    PHASE 4+5: Сначала пробуем scoped resolver. Если не распознан — legacy-ветки.
    """

    # ══════════════════════════════════════════════════════════════════════════
    # SCOPED CALLBACK SYSTEM — новые кнопки идут сюда
    # ══════════════════════════════════════════════════════════════════════════
    resolved = await resolve_callback(data, user_id)
    if resolved is not None:
        validation = await validate_callback(resolved, user_id)
        if not validation.is_valid:
            _error_msgs = {
                "stale_keyboard":           "⚠️ Эта кнопка устарела. Используй кнопки под последним результатом.",
                "already_completed":        "✅ Это действие уже выполнено.",
                "wrong_user":               None,   # silent
                "result_wrong_user":        None,   # silent
                "result_not_found":         "Материал не найден — создай новый.",
                "token_expired":            "⚠️ Эта кнопка устарела. Используй кнопки под последним результатом.",
                "token_used":               "✅ Это действие уже было выполнено.",
                "tool_mismatch":            "Действие не применимо к этому инструменту.",
                "action_not_allowed_for_tool": "Это действие недоступно для данного инструмента.",
            }
            msg = _error_msgs.get(validation.error_code, validation.user_message)
            if msg:
                await query.answer(msg, show_alert=True)
            return

        # Маркируем DB-токен использованным для однократных действий
        if resolved.token:
            from callback_context import ACTION_REGISTRY
            spec = ACTION_REGISTRY.get(resolved.action, {})
            if spec.get("marks_completed") and not spec.get("repeatable", True):
                await mark_callback_used(resolved.token)

        await _route_scoped_action(update, ctx, query, user_id, resolved, validation.result)
        return

    # ══════════════════════════════════════════════════════════════════════════
    # LEGACY CALLBACKS — временно, до завершения миграции
    # Логируем для мониторинга: когда legacy исчезнут — удалить весь блок ниже
    # ══════════════════════════════════════════════════════════════════════════
    logger.debug(f"[legacy_callback] user={user_id} data={data!r}")

    # ── Подписка / Кабинет ────────────────────────────────────────────────────

    if data in ("sub_cabinet", "sub_menu"):
        await show_cabinet(update, user_id)
        return

    if data == "sub_trial":
        try:
            await grant_trial(user_id)
            await invalidate_state_cache(user_id)
            caption = (
                f"🎁 *{TRIAL_DAYS} дней доступа активированы.*\n\n"
                "Полный доступ ко всем инструментам — пиши, стратегируй, генерируй.\n"
                "Напиши тему поста или нажми меню 👇"
            )
            from ui.media import send_gif, send_sticker
            # Стикер → GIF → текст (первое что сработает)
            sent = await send_sticker(update, "trial_welcome")
            if not sent:
                sent = await send_gif(update, "trial_activated", caption)
            if not sent:
                await send(update, caption, parse_mode="Markdown",
                           reply_markup=kb(["☰ Главное меню|menu_main"]))
        except ValueError:
            await edit(query,
                       "Пробный период уже использовала.\n\nОформи подписку — и продолжим работу 👇",
                       parse_mode="Markdown",
                       reply_markup=kb(["💳 Оформить подписку|sub_pay", "← Назад|sub_cabinet"]))
        return

    if data == "sub_about":
        _about = (
            "*Как это работает — коротко*\n\n"
            "1️⃣ *Говоришь голосом или текстом* — что нужно сделать.\n"
            "   Я понимаю живую речь: «сделай пост про то что клиенты боятся начинать» — готово.\n\n"
            "2️⃣ *Я пишу в твоём стиле* — не шаблон, а твой голос.\n"
            "   После каждого текста нажимаешь «Звучит как я ✅» — я запоминаю и становлюсь точнее.\n\n"
            "3️⃣ *Скидываешь свои старые посты* — и я начинаю писать твоими словами.\n"
            "   Структура, лексика, ритм — всё твоё.\n\n"
            "4️⃣ *Утром получаешь идею дня* — конкретную, под твою нишу и аудиторию.\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "*Что создаём вместе:*\n"
            "Посты · Рилсы · Карусели · Прогревы · Сторис · Talking Head\n"
            "Разбор профиля · Разбор конкурента · Контент-план · Мозговой штурм\n\n"
            f"*{TRIAL_DAYS} дней бесплатно* — без карты. Отменить в один клик."
        )
        await send(update, _about, parse_mode="Markdown",
                   reply_markup=kb(["🎁 Попробовать бесплатно|sub_trial"], ["💳 Оформить подписку|sub_pay"]))
        return

    if data == "sub_pay":
        link = get_payment_link(user_id)
        if not link:
            logger.error(f"sub_pay: payment link empty for user={user_id}, LAVA_LINK={os.environ.get('LAVA_LINK')!r}")
            await edit(query, "❌ Ссылка на оплату не настроена. Обратись к администратору.",
                       reply_markup=kb(["← Назад|sub_cabinet"]))
            return
        from lava_payments import TIER_PRICES_EUR
        p = TIER_PRICES_EUR
        await edit(query,
                   f"💳 *Оформление подписки*\n\n"
                   f"• 1 месяц — €{p['1m']}\n"
                   f"• 3 месяца — €{p['3m']} _(скидка 7%)_\n"
                   f"• 6 месяцев — €{p['6m']} _(скидка 10%)_\n"
                   f"• 12 месяцев — €{p['12m']} _(скидка 13%)_\n\n"
                   "_После оплаты доступ активируется автоматически_ ✅",
                   parse_mode="Markdown",
                   reply_markup=InlineKeyboardMarkup([
                       [InlineKeyboardButton("💳 Перейти к оплате", url=link)],
                       [InlineKeyboardButton("← Назад", callback_data="sub_cabinet")],
                   ]))
        return

    if data == "cab_history":
        history_text = await render_history(user_id)
        await edit(query, history_text, parse_mode="Markdown",
                   reply_markup=kb(["← Кабинет|sub_cabinet"]))
        return

    if data == "cab_referral":
        ref_text, ref_link = await render_referral(user_id)
        import urllib.parse as _up
        _share_text = _up.quote("Попробуй Миру — 7 дней бесплатно! Создаёт посты, рилсы и прогревы в твоём голосе 🔥")
        _share_url  = f"https://t.me/share/url?url={_up.quote(ref_link)}&text={_share_text}"
        await edit(query, ref_text, parse_mode="Markdown",
                   reply_markup=InlineKeyboardMarkup([
                       [InlineKeyboardButton("📤 Поделиться ссылкой", url=_share_url)],
                       [InlineKeyboardButton("← Кабинет", callback_data="sub_cabinet")],
                   ]))
        return

    # ── Paywall для всех остальных (до проверки доступа) ─────────────────────

    _cb_state = await get_user_state(user_id)
    if not has_access(_cb_state):
        # Исключение: resume_agent и menu_main_clear — навигационные, не требуют доступа
        if not data.startswith("resume_agent_") and data != "menu_main_clear":
            await show_paywall(update, user_id, _cb_state)
            return

    # ── Навигация ─────────────────────────────────────────────────────────────

    if data == "support":
        await update.effective_chat.send_message(
            f"🆘 Поддержка\n\nМенеджер: @{SUPPORT_USERNAME}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💬 Написать @{SUPPORT_USERNAME}",
                                      url=f"https://t.me/{SUPPORT_USERNAME}")],
                [InlineKeyboardButton("Меню", callback_data="menu_main")],
            ]))
        return

    if data == "voice_hint":
        await edit(
            query,
            "🎙 *Просто запиши голосовое сообщение*\n\n"
            "Расскажи что хочешь создать — пост, рилс, прогрев, карусель.\n"
            "Говори как хочешь, хаотично или кратко — Мира разберётся и сделает.\n\n"
            "_Голосовые работают так же хорошо, как текст — часто лучше._",
            parse_mode="Markdown",
            reply_markup=kb(["← Меню|menu_main"]),
        )
        return

    if data == "menu_main":
        from ui.home import show_home
        # Пробуем отредактировать текущее сообщение — убираем кнопки
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await show_home(update, user_id)
        return

    if data == "menu_main_clear":
        # Явная кнопка "начать новую задачу" — тут очищаем сессии
        await clear_all_agent_sessions(user_id)
        await edit(query, "Что делаем? 👇", reply_markup=main_menu_kb())
        await kv_set(user_id, "__menu_msg_id__", str(query.message.message_id))
        return

    if data == "menu_more":
        await edit(query, "Все инструменты 👇", reply_markup=more_menu_kb())
        return

    if data == "mode_chat":
        await clear_all_agent_sessions(user_id)
        await clear_onboarding_state(user_id)
        await kv_set(user_id, "__chat_mode__", "ask_mira", ttl=3600)
        await edit(
            query,
            "💬 *Свободный чат с Мирой*\n\n"
            "Здесь можно спросить про стратегию, разобрать идею, "
            "обсудить что не заходит в контенте — всё что угодно по теме.\n\n"
            "Говори голосом или пиши — отвечу как продюсер, не как справочник.",
            parse_mode="Markdown",
            reply_markup=kb(
                ["🎙 Говори голосом|voice_hint"],
                ["← Меню|menu_main"],
            )
        )
        return

    # ── Resume агента ─────────────────────────────────────────────────────────

    if data.startswith("resume_agent_"):
        agent_key = data[len("resume_agent_"):]
        spec = ag.get_spec(agent_key)
        if spec:
            session = await get_agent_session(user_id, agent_key)
            if session:
                step = session.get("step", "")
                q_count = session.get("q_count", 0)
                await edit(query,
                           f"🔙 *Продолжаем — {spec.name}*\n\n"
                           f"_Вопрос {q_count} из {spec.max_q} — ответь и продолжим._",
                           parse_mode="Markdown",
                           reply_markup=kb(["⏭ Пропустить вопросы|agent_skip",
                                            "← Меню|menu_main"]))
                return
        await show_menu(update, user_id)
        return

    # ── Profile ───────────────────────────────────────────────────────────────

    if data == "menu_profile":
        from utils import profile_val
        from db import get_style_examples
        p   = await get_profile(user_id)
        mdl = await get_model(user_id)
        _mdl_names = {"claude": "Claude Sonnet", "gpt4": "GPT-4o", "grok": "Grok 3 Mini"}
        examples  = await get_style_examples(user_id)
        ex_count  = len(examples)
        await edit(query,
                   f"⚙️ Профиль\n\n"
                   f"Ниша: {profile_val(p, 'niche')}\n"
                   f"Аудитория: {profile_val(p, 'audience')}\n"
                   f"Тон: {profile_val(p, 'tone')}\n"
                   f"Примеры стиля: {ex_count}/10\n\n"
                   f"Модель: {_mdl_names.get(mdl, mdl)}",
                   reply_markup=kb(
                       ["✏️ Изменить профиль|profile_edit"],
                       ["📝 Примеры стиля|style_menu"],
                       ["🤖 Сменить модель|profile_model"],
                       ["← Меню|menu_main"],
                   ))
        return

    if data == "onb_skip_push":
        # Пользователь пропустил настройку времени пуша в онбординге
        from db import get_onboarding_state
        from flows.onboarding import _finish_onboarding
        state = await get_onboarding_state(user_id)
        if state:
            # Помечаем push_enabled=False и завершаем онбординг
            state.setdefault("data", {})["push_enabled"] = False
            state["step"] = len(["niche", "audience", "tone", "push_time"])
            await _finish_onboarding(update, user_id, state)
        return

    if data == "profile_edit":
        state = {"step": 0, "data": {}, "source": "profile_edit"}
        from db import save_onboarding_state
        await save_onboarding_state(user_id, state)
        await edit(query, "Обновим профиль 👇")
        from flows.onboarding import onb_next
        await onb_next(update, user_id, state)
        return

    if data == "profile_model":
        mdl = await get_model(user_id)
        await edit(query, "Выбери модель 👇", reply_markup=model_kb(mdl))
        return

    if data.startswith("model_"):
        key = data[6:]
        if key in MODELS:
            await set_model(user_id, key)
            name = {"claude": "Claude Sonnet", "gpt4": "GPT-4o", "grok": "Grok 3 Mini"}.get(key, key)
            await edit(query, f"✅ Модель: *{name}*", parse_mode="Markdown",
                       reply_markup=kb(["← Меню|menu_main"]))
        return

    # ── Generic agents ────────────────────────────────────────────────────────

    if data.startswith("agent_start_"):
        agent_key = data[len("agent_start_"):]
        spec = ag.get_spec(agent_key)
        if not spec:
            await edit(query, "Агент не найден.")
            return
        await clear_all_agent_sessions(user_id)
        await clear_onboarding_state(user_id)
        await ag.start(update, user_id, spec)
        return

    if data == "agent_skip":
        agent_key = await _detect_active_agent(user_id)
        spec = ag.get_spec(agent_key) if agent_key else None
        if not spec:
            await send(update, "Выбери инструмент 👇", reply_markup=main_menu_kb())
            return
        s = await get_agent_session(user_id, agent_key)
        if not s:
            await send(update, "Сессия истекла — начни заново 👇", reply_markup=main_menu_kb())
            return
        if spec.accept_photos:
            await ag._offer_photos(update, user_id, spec, s)
        elif spec.has_pick_step:
            await ag._gen_variants(update, user_id, spec, s)
        else:
            await ag.generate(update, user_id, spec, s)
        return

    if data == "agent_generate":
        agent_key = await _detect_active_agent(user_id)
        spec = ag.get_spec(agent_key) if agent_key else None
        if spec:
            await ag.force_generate(update, user_id, spec)
        else:
            await send(update, "Выбери инструмент 👇", reply_markup=main_menu_kb())
        return

    if data == "agent_pick_prompt":
        agent_key = await _detect_active_agent(user_id)
        spec = ag.get_spec(agent_key) if agent_key else None
        if spec and spec.has_pick_step:
            s = await get_agent_session(user_id, agent_key)
            if s:
                s["step"] = "pick"
                await save_agent_session(user_id, agent_key, s)
                await send(update, "Напиши *номер* понравившегося варианта:", parse_mode="Markdown")
        return

    if data == "agent_regen":
        agent_key = await _detect_active_agent(user_id)
        spec = ag.get_spec(agent_key) if agent_key else None
        if spec:
            s = await get_agent_session(user_id, agent_key)
            if s:
                await ag._gen_variants(update, user_id, spec, s)
        return

    if data == "agent_retry":
        agent_key = await _detect_active_agent(user_id)
        spec = ag.get_spec(agent_key) if agent_key else None
        if spec:
            s = await get_agent_session(user_id, agent_key)
            if s:
                await ag.generate(update, user_id, spec, s)
        return

    if data.startswith("agent_restart_"):
        agent_key = data[len("agent_restart_"):]
        spec = ag.get_spec(agent_key)
        if spec:
            await clear_all_agent_sessions(user_id)
            await ag.start(update, user_id, spec)
        return

    # ── Рилс-коротышка ────────────────────────────────────────────────────────

    if data == "flow_reels_short":
        await clear_all_agent_sessions(user_id)
        await clear_onboarding_state(user_id)
        await reels.rs_start(update, user_id)
        return

    if data == "rs_regen":
        # Сбрасываем completed_actions при перегенерации
        from db import get_agent_session as _gas, save_agent_session as _sas
        _s = await _gas(user_id, _RS_KEY)
        if _s:
            _s["completed_actions"] = []
            await _sas(user_id, _RS_KEY, _s)
        await rs_regen(update, user_id)
        return

    # Панель доработки хуков
    if data == "rs_edit_softer":
        if await _rs_stale_guard(user_id, query): return
        await rs_edit_softer(update, user_id)
        return

    if data == "rs_edit_bolder":
        if await _rs_stale_guard(user_id, query): return
        await rs_edit_bolder(update, user_id)
        return

    if data == "rs_edit_top5":
        if await _rs_stale_guard(user_id, query): return
        await rs_edit_top5(update, user_id)
        return

    if data == "rs_edit_style":
        if await _rs_stale_guard(user_id, query): return
        await rs_edit_style(update, user_id)
        return

    if data == "rs_edit_back":
        await rs_edit_back(update, user_id)
        return

    if data.startswith("rs_style_"):
        if await _rs_stale_guard(user_id, query): return
        style_key = data[9:]
        await rs_apply_style(update, user_id, style_key)
        return

    # Описание к выбранному хуку
    if data == "rs_pick_for_desc":
        if await _rs_stale_guard(user_id, query): return
        await rs_pick_for_desc(update, user_id)
        return

    if data == "rs_desc_skip":
        s = await get_agent_session(user_id, _RS_KEY)
        if s:
            s["desc_details"] = ""
            await save_agent_session(user_id, _RS_KEY, s)
            await reels.rs_generate_desc(update, user_id, s)
        else:
            await reels.rs_start(update, user_id)
        return

    if data == "rs_retry_desc":
        s = await get_agent_session(user_id, _RS_KEY)
        if s:
            await reels.rs_generate_desc(update, user_id, s)
        else:
            await reels.rs_start(update, user_id)
        return

    # ── Карусель ──────────────────────────────────────────────────────────────

    if data == "flow_carousel":
        await clear_all_agent_sessions(user_id)
        await clear_onboarding_state(user_id)
        await carousel.car_start(update, user_id)
        return

    if data == "car_generate":
        s = await get_agent_session(user_id, _CAR_KEY)
        if s:
            await carousel.car_generate(update, user_id, s)
        else:
            await carousel.car_start(update, user_id)
        return

    if data == "car_change_topic":
        await clear_agent_session(user_id, _CAR_KEY)
        await carousel.car_start(update, user_id)
        return

    if data == "car_pick_headline":
        await carousel.car_pick(update, user_id)
        return

    # ── Панель доработки карусели ─────────────────────────────────────────────

    if data == "car_edit_headline":
        if await _car_stale_guard(user_id, query): return
        await carousel.car_edit_headline(update, user_id)
        return

    if data == "car_edit_add_slide":
        if await _car_stale_guard(user_id, query): return
        await carousel.car_edit_add_slide(update, user_id)
        return

    if data == "car_edit_shorten":
        if await _car_stale_guard(user_id, query): return
        await carousel.car_edit_shorten(update, user_id)
        return

    if data == "car_edit_softer":
        if await _car_stale_guard(user_id, query): return
        await carousel.car_edit_softer(update, user_id)
        return

    if data == "car_edit_bolder":
        if await _car_stale_guard(user_id, query): return
        await carousel.car_edit_bolder(update, user_id)
        return

    if data == "car_edit_trigger":
        if await _car_stale_guard(user_id, query): return
        await carousel.car_edit_trigger(update, user_id)
        return

    if data == "car_edit_format":
        if await _car_stale_guard(user_id, query): return
        await carousel.car_edit_format(update, user_id)
        return

    if data == "car_edit_back":
        await carousel.car_edit_back(update, user_id)
        return

    if data.startswith("car_fmt_"):
        fmt_key = data[8:]
        await carousel.car_apply_format(update, user_id, fmt_key)
        return

    if data.startswith("car_trig_"):
        trig_key = data[9:]
        await carousel.car_apply_trigger(update, user_id, trig_key)
        return

    if data.startswith("cfmt_"):
        await carousel.car_apply_format(update, user_id, data[5:])
        return

    if data.startswith("ctrig_"):
        await carousel.car_apply_trigger(update, user_id, data[6:])
        return

    # ── Быстрые идеи ──────────────────────────────────────────────────────────
    if data == "quick_ideas":
        await clear_all_agent_sessions(user_id)
        await qi_start(update, user_id)
        return

    if data == "qi_save_backlog":
        await qi_save_backlog(update, user_id)
        return

    if data == "diagnostic_start":
        await diagnostic_start(update, user_id)
        return

    # ── Библиотека ────────────────────────────────────────────────────────────
    if data == "my_results":
        from flows.misc import show_results
        await show_results(update, user_id, page=0)
        return

    if data.startswith("results_page_"):
        try:
            page = int(data.split("_")[-1])
        except ValueError:
            page = 0
        from flows.misc import show_results
        await show_results(update, user_id, page=page)
        return

    if data.startswith("result_open_"):
        try:
            result_id = int(data.split("_")[-1])
        except ValueError:
            result_id = 0
        from flows.misc import show_result_full
        await show_result_full(update, user_id, result_id)
        return

    if data.startswith("result_del_"):
        try:
            result_id = int(data.split("_")[-1])
        except ValueError:
            result_id = 0
        await delete_result(user_id, result_id)
        await send(update, "🗑 Материал удалён.",
                   reply_markup=kb(["← Мои материалы|my_results", "← Меню|menu_main"]))
        return

    # ── Аналитика ─────────────────────────────────────────────────────────────
    if data == "my_stats":
        from flows.misc import show_stats
        await show_stats(update, user_id)
        return

    # ── Доработка / другой вариант ────────────────────────────────────────────
    if data == "refine_last":
        from db import clear_agent_session as _cas
        await _cas(user_id, "refine_flow")
        await refine_start(update, user_id)
        return

    if data.startswith("refine_id_"):
        try:
            result_id = int(data.split("_")[-1])
        except ValueError:
            result_id = 0
        from db import clear_agent_session as _cas
        await _cas(user_id, "refine_flow")
        await refine_start(update, user_id, result_id=result_id)
        return

    if data == "regen_last":
        await regen_last(update, user_id)
        return

    if data.startswith("regen_id_"):
        try:
            result_id = int(data.split("_")[-1])
        except ValueError:
            result_id = 0
        await regen_by_id(update, user_id, result_id)
        return

    # ── Планировщик ───────────────────────────────────────────────────────────
    if data == "planner_show":
        await planner_show(update, user_id)
        return
    if data == "planner_add":
        await clear_all_agent_sessions(user_id)
        await planner_add_start(update, user_id)
        return
    if data == "planner_week":
        from db import clear_agent_session as _cas
        await _cas(user_id, "planner_flow")
        await planner_gen_week(update, user_id)
        return
    if data == "planner_week_skip":
        # User skipped week goal — generate without narrative context
        from db import save_agent_session as _sas
        await _sas(user_id, "planner_flow", {"week_goal": "без конкретной цели — сбалансированный план"})
        await planner_gen_week(update, user_id)
        return
    if data == "planner_done":
        from db import get_schedule as _gs
        schedule = await _gs(user_id)
        pending = [(i, s) for i, s in enumerate(schedule) if not s.get("done")]
        if not pending:
            await send(update, "Нет невыполненных постов.", reply_markup=kb(["← Планировщик|planner_show"]))
        else:
            rows = [[f"✅ {s['date']} {s['idea'][:30]}|planner_mark_{i}"] for i, s in pending[:8]]
            rows.append(["← Назад|planner_show"])
            await send(update, "Отметь выполненное:", reply_markup=kb(*rows))
        return
    if data.startswith("planner_mark_"):
        try:
            idx = int(data.split("_")[-1])
            from db import mark_done as _md
            await _md(user_id, idx)
        except Exception:
            pass
        await planner_show(update, user_id)
        return
    if data == "planner_clear":
        from db import save_schedule as _ss
        await _ss(user_id, [])
        await send(update, "🗑 Расписание очищено.", reply_markup=kb(["← Планировщик|planner_show"]))
        return

    # ── Дейли-режим ───────────────────────────────────────────────────────────
    if data == "daily_menu":
        await daily_menu(update, user_id)
        return

    if data == "daily_on":
        from db import save_daily_settings, get_daily_settings
        ds = await get_daily_settings(user_id)
        ds["enabled"] = True
        await save_daily_settings(user_id, ds)
        try:
            await schedule_daily(ctx.application, user_id, ds.get("hour", 9), ds.get("minute", 0))
        except Exception as e:
            logger.warning(f"Could not schedule daily job: {e}")
        await daily_menu(update, user_id)
        return

    if data == "daily_off":
        from db import save_daily_settings, get_daily_settings
        ds = await get_daily_settings(user_id)
        ds["enabled"] = False
        await save_daily_settings(user_id, ds)
        try:
            for job in ctx.application.job_queue.get_jobs_by_name(f"daily_{user_id}"):
                job.schedule_removal()
        except Exception:
            pass
        await daily_menu(update, user_id)
        return

    if data == "daily_set_time":
        from db import save_agent_session as _sas
        await _sas(user_id, "daily_time_flow", {"step": "await_time"})
        await send(update, "⏰ Напиши время:\n_Например: 9:00 или 8:30_",
                   parse_mode="Markdown", reply_markup=kb(["← Назад|daily_menu"]))
        return

    if data == "daily_test":
        from flows.misc import daily_send_now
        await daily_send_now(ctx, user_id, ctx.bot)
        return

    # ── Утренние пуши (Persona-style) ─────────────────────────────────────────
    if data == "daily_push_menu":
        from flows.daily_push import push_menu
        await push_menu(update, user_id)
        return

    if data == "daily_push_on":
        from flows.daily_push import (
            get_push_settings, save_push_settings, schedule_daily_push, push_menu
        )
        s = await get_push_settings(user_id)
        s["enabled"] = True
        await save_push_settings(user_id, s)
        try:
            await schedule_daily_push(
                ctx.application, user_id,
                s.get("hour", 9), s.get("minute", 0)
            )
        except Exception as e:
            logger.warning(f"schedule_daily_push failed: {e}")
        await push_menu(update, user_id)
        return

    if data == "daily_push_off":
        from flows.daily_push import get_push_settings, save_push_settings, push_menu
        s = await get_push_settings(user_id)
        s["enabled"] = False
        await save_push_settings(user_id, s)
        try:
            for job in ctx.application.job_queue.get_jobs_by_name(f"daily_push_{user_id}"):
                job.schedule_removal()
        except Exception:
            pass
        await push_menu(update, user_id)
        return

    if data == "daily_push_time":
        from db import save_agent_session as _sas
        import utils as _utils
        await _sas(user_id, "daily_push_time_flow", {"step": "await_time"})
        await _utils.send(update,
                          "⏰ Напиши время для утреннего пуша:\n_Например: 9:00 или 8:30_",
                          parse_mode="Markdown",
                          reply_markup=_utils.kb(["← Назад|daily_push_menu"]))
        return

    if data == "daily_push_test":
        from flows.daily_push import daily_push_send_now
        await daily_push_send_now(ctx, user_id, ctx.bot)
        return

    # ── Стиль ─────────────────────────────────────────────────────────────────
    if data == "style_menu":
        await clear_agent_session(user_id, "style_flow")
        await style_menu(update, user_id)
        return
    if data == "style_add":
        await style_add_start(update, user_id)
        return
    if data == "style_view":
        from db import get_style_examples
        examples = await get_style_examples(user_id)
        if not examples:
            await send(update, "Примеры не добавлены.", reply_markup=kb(["← Кабинет|sub_cabinet"]))
        else:
            for i, ex in enumerate(examples, 1):
                await send(update, f"*Пример {i}:*\n\n{ex[:500]}", parse_mode="Markdown")
            await send(update, "Это все твои примеры 👆", reply_markup=kb(["← Кабинет|sub_cabinet"]))
        return
    if data == "style_clear":
        from db import clear_style_examples
        await clear_style_examples(user_id)
        await send(update, "🗑 Примеры стиля удалены.", reply_markup=kb(["← Кабинет|sub_cabinet"]))
        return

    # Пункт 4: принудительное завершение batch-сбора постов (кнопка «Завершить сбор»)
    if data == "style_collect_done":
        import json as _json
        from db import kv_del as _kv_del, kv_get as _kv_get
        raw = await _kv_get(user_id, "__collecting_posts__")
        if raw:
            session = _json.loads(raw)
            collected = session.get("collected", [])
            await _kv_del(user_id, "__collecting_posts__")
            if collected:
                await send(
                    update,
                    f"✅ Собрала {len(collected)} постов — анализирую твой голос... "
                    f"Это займёт минуту 🔍",
                )
                import asyncio as _asyncio
                from handlers.messages import finalize_voice_learning
                _asyncio.create_task(finalize_voice_learning(collected, user_id, update))
            else:
                await send(
                    update,
                    "Пока не получила ни одного поста. "
                    "Скинь хотя бы один — текстом или скриншотом 📸",
                    reply_markup=kb(["← Назад|style_menu"]),
                )
        else:
            await send(update, "Сбор постов не активен.", reply_markup=kb(["← Меню|menu_main"]))
        return

    # ── Голосовое подтверждение (устаревшее — голос теперь роутится напрямую) ──
    if data == "voice_send":
        # Этот callback больше не используется — голос идёт сразу в routing
        # Оставлен для совместимости со старыми сообщениями в чате
        pending = await kv_get(user_id, "__voice_pending__")
        await kv_del(user_id, "__voice_pending__")
        if not pending:
            await edit(query, "Запрос уже отправлен автоматически.", reply_markup=kb(["← Меню|menu_main"]))
            return
        await edit(query, f"🎙 _{pending}_", parse_mode="Markdown")
        from handlers.messages import _route
        await _route(update, ctx, user_id, pending)
        return

    if data == "voice_cancel":
        await kv_del(user_id, "__voice_pending__")
        await edit(query, "Отменено.", reply_markup=kb(["← Меню|menu_main"]))
        return

    # ── One-shot сохранение ───────────────────────────────────────────────────
    if data.startswith("oneshot_save_"):
        raw = await kv_get(user_id, "__oneshot_draft__")
        if raw:
            try:
                draft = json.loads(raw)
                await save_result(user_id, draft["agent"], draft["name"], draft["content"])
                await query.answer("✅ Сохранено в «Мои материалы»")
            except Exception as e:
                logger.error(f"oneshot_save error: {e}")
                await query.answer("❌ Не удалось сохранить")
        else:
            await query.answer("Нет черновика для сохранения")
        return

    # ── Intent one-shot ───────────────────────────────────────────────────────
    if data == "intent_oneshot":
        from intent_router import oneshot_generate, AGENT_EMOJI, AGENT_NAMES, _ONESHOT_PROMPTS
        ctx_raw = await kv_get(user_id, "__intent_ctx__")
        if not ctx_raw:
            await query.answer("Контекст устарел — напиши запрос снова")
            return
        ctx_data  = json.loads(ctx_raw)
        agent_key = ctx_data.get("agent", "post")
        orig_text = ctx_data.get("text", "")
        emoji     = AGENT_EMOJI.get(agent_key, "🤖")
        name      = AGENT_NAMES.get(agent_key, agent_key)
        status = await update.effective_chat.send_message(
            f"{emoji} Генерирую быстрый результат — *{name}*...", parse_mode="Markdown"
        )
        _tt   = asyncio.create_task(typing_loop(update.effective_chat))
        try:
            profile = await get_profile(user_id)
            result  = await oneshot_generate(agent_key, orig_text, profile)
        except Exception as e:
            logger.error(f"intent_oneshot error: {e}")
            result = None
        finally:
            _tt.cancel()
        await safe_delete(status)
        if result:
            deep_cb = (
                "flow_reels_short" if agent_key == "reels_short" else
                "flow_carousel"    if agent_key == "carousel"    else
                f"agent_start_{agent_key}"
            )
            _result_id = 0
            try:
                from db import save_result as _sr
                _result_id = await _sr(user_id, agent_key, f"{name} (быстро)", result)
            except Exception as _e:
                logger.warning(f"oneshot save_result failed: {_e}")

            await kv_set(user_id, "__oneshot_draft__",
                         json.dumps({"agent": agent_key, "name": name, "content": result},
                                    ensure_ascii=False),
                         ttl=1800)  # BUG #7 FIX: explicit 30-min TTL

            # Голосовой прогресс
            _hint = ""
            if _result_id:
                try:
                    from voice_learner import get_voice_stats as _gvs
                    from ui.progress_bar import voice_progress_short as _vps
                    _vs   = await _gvs(user_id)
                    _hint = _vps(_vs.get("total_signals", 0))
                except Exception:
                    pass

            # Всё в одном сообщении с combined_kb
            # PHASE 9 FIX: scoped кнопки привязаны к конкретному _result_id
            _regen_cb_btn  = f"regen:{_result_id}:1" if _result_id else "regen_last"
            _refine_cb_btn = f"refine:{_result_id}:1" if _result_id else "refine_last"

            if _result_id and _hint is not None:
                from voice_learner import voice_feedback_kb as _vfkb
                _combined_kb = _vfkb(_result_id, extra_rows=[
                    [f"⚡ Детальнее|{deep_cb}", f"💾 Сохранить|oneshot_save_{agent_key}"],
                    [f"✏️ Доработать|{_refine_cb_btn}", f"🔄 Другой вариант|{_regen_cb_btn}"],
                    ["← Меню|menu_main"],
                ])
                await send(update, f"{result}\n\n_Звучит как твой голос?{_hint}_",
                           parse_mode="Markdown", reply_markup=_combined_kb)
            else:
                await send(update, result, parse_mode="Markdown",
                           reply_markup=kb(
                               [f"⚡ Детальнее|{deep_cb}", f"💾 Сохранить|oneshot_save_{agent_key}"],
                               [f"✏️ Доработать|{_refine_cb_btn}", f"🔄 Другой вариант|{_regen_cb_btn}"],
                               ["← Меню|menu_main"],
                           ))

            # Конверсионный триггер: только 3-я генерация в триале
            try:
                from db import get_stats as _gs
                _state = await get_user_state(user_id)
                if _state == UserState.TRIAL:
                    _stats = await _gs(user_id)
                    if _stats.get("total", 0) == 3:
                        from lava_payments import get_payment_link as _gpl
                        from telegram import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
                        _link = _gpl(user_id)
                        _pay_kb = _IKM([[_IKB("💳 Оформить подписку", url=_link)]] if _link else [])
                        await update.effective_chat.send_message(
                            "Уже 3 материала — продолжаем?\n\n"
                            "Триал скоро закончится. Оформи подписку и не потеряй всё что создала.",
                            reply_markup=_pay_kb,
                        )
            except Exception:
                pass
        return

    # ── Редактор промптов (только для админа) ─────────────────────────────────
    if data in ("pe_menu", "pe_back_cats") or data.startswith(
        ("pe_cat_", "pe_view_", "pe_edit_", "pe_reset_", "pe_back_list_")
    ):
        if user_id != ADMIN_ID:
            await edit(query, "⛔ Доступ закрыт.")
            return
        if data in ("pe_menu", "pe_back_cats"):
            await pe_menu(update, user_id)
        elif data.startswith("pe_cat_"):
            await pe_show_category(update, user_id, data[7:], query)
        elif data.startswith("pe_view_"):
            await pe_view_prompt(update, user_id, data[8:], query)
        elif data.startswith("pe_edit_"):
            await pe_start_edit(update, user_id, data[8:], query)
        elif data.startswith("pe_reset_"):
            await pe_reset(update, user_id, data[9:], query)
        elif data.startswith("pe_back_list_"):
            cat = get_category_for_slug(data[13:])
            await pe_show_category(update, user_id, cat, query)
        return

    # ── Voice feedback (звучит как я?) ───────────────────────────────────────
    if data.startswith("vf_yes_"):
        try:
            result_id = int(data.split("_")[-1])
            from voice_learner import handle_voice_feedback_yes
            await handle_voice_feedback_yes(update, user_id, result_id)
            # Реакция ❤️
            from ui.media import react_to_voice_feedback, send_sticker, send_gif
            await react_to_voice_feedback(update, ctx.bot)
            # Стикер на каждое одобрение — живой отклик
            await send_sticker(update, "voice_yes")
            # Level-up стикер/GIF на milestone
            try:
                from voice_learner import get_voice_stats
                _vs = await get_voice_stats(user_id)
                _total = _vs.get("total_signals", 0)
                if _total in (5, 10, 20):
                    if not await send_sticker(update, "level_up"):
                        await send_gif(update, "voice_level_up")
            except Exception:
                pass
        except Exception as e:
            await send(update, "✅ Записала!", reply_markup=kb(["← Меню|menu_main"]))
        return

    if data.startswith("vf_no_"):
        try:
            result_id = int(data.split("_")[-1])
            from voice_learner import handle_voice_feedback_no
            await handle_voice_feedback_no(update, user_id, result_id)
        except Exception as e:
            await edit(query, "Что именно не так? Напиши одним предложением.")
        return

    # ── Панель доработки агентов (ag_edit_*) ─────────────────────────────────
    # Аналог car_edit_* в карусели — применяет правку к последнему результату

    if data == "ag_restore_original":
        # Восстанавливает первый результат (до всех правок)
        from agents import get_agent_session, save_agent_session
        from db import kv_get as _kvg
        try:
            _active = await _kvg(user_id, "__active_agent__")
            if _active:
                _es = await get_agent_session(user_id, f"__ag_edit_{_active}__")
                if _es and _es.get("original_result"):
                    _es["last_result"] = _es["original_result"]
                    _es["refinement_count"] = 0
                    _es["completed_actions"] = []
                    await save_agent_session(user_id, f"__ag_edit_{_active}__", _es)
                    from agents import _agent_edit_panel_kb
                    import utils as _u
                    _panel_kb, _ = _agent_edit_panel_kb(_active)
                    await _u.send(update, f"{_es['original_result']}\n\n_🔙 Восстановлен первый результат._",
                               parse_mode="Markdown", reply_markup=_panel_kb)
                    return
        except Exception:
            pass
        import utils as _u
        await _u.send(update, "Не удалось восстановить — оригинал не найден.", reply_markup=_u.kb(["← Меню|menu_main"]))
        return

    if data.startswith("ag_edit_"):
        edit_key_map = {
            "ag_edit_softer":           "softer",
            "ag_edit_softer_confirmed": "softer_confirmed",  # confirmed soften on conversion stage
            "ag_edit_bolder":           "bolder",
            "ag_edit_shorter":          "shorter",
            "ag_edit_detail":           "add_detail",
            "ag_edit_cta":              "stronger_cta",
            # Warmup-specific
            "ag_edit_resonance":    "resonance",
            "ag_edit_add_proof":    "add_proof",
            # Stories-specific
            "ag_edit_mid_hook":     "mid_hook",
            # Talking head / cartoon
            "ag_edit_hook":         "hook",
            # Profile-specific
            "ag_edit_deepen":       "deepen",
            # Competitor-specific
            "ag_edit_tactics":      "tactics",
            "ag_edit_positioning":  "positioning",
        }
        edit_key = edit_key_map.get(data)
        if edit_key:
            await ag.apply_agent_edit(update, user_id, edit_key)
        return

    # ── Follow-up ответы ─────────────────────────────────────────────────────
    if data.startswith("fu_"):
        from flows.followup import handle_followup_callback
        await handle_followup_callback(ctx.bot, user_id, data)
        return

    logger.warning(f"[callback] unhandled data={data!r} user={user_id}")
