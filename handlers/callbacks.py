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
)
from user_state import get_user_state, has_access, UserState, invalidate_state_cache
from lava_payments import (
    grant_trial, get_payment_link, render_status, render_history, render_referral,
    get_subscription, has_used_trial, TRIAL_DAYS,
)
from flows import reels, carousel
from flows.reels import _RS_KEY
from flows.carousel import _CAR_KEY, carousel_format_kb, carousel_trigger_kb
from flows.misc import (
    qi_start, refine_start, regen_last, regen_by_id,
    planner_show, planner_gen_week, planner_add_start,
    daily_menu, style_menu, style_add_start,
    schedule_daily,
)
from ui.menu import show_menu, main_menu_kb, more_menu_kb, model_kb
from ui.paywall import show_paywall
from ui.cabinet import show_cabinet
from handlers.messages import _detect_active_agent, _typing_loop
from prompt_editor import (
    pe_menu, pe_show_category, pe_view_prompt,
    pe_start_edit, pe_save_text, pe_reset, get_category_for_slug,
    _PE_KEY,
)
from utils import send, edit, kb, safe_delete
from security import ADMIN_ID
from config import CAROUSEL_FORMATS_4, CAROUSEL_TRIGGERS_20, MODELS
import agents as ag

logger = logging.getLogger(__name__)
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "Stanley_Berks")


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


async def _dispatch(update, ctx, query, user_id: int, data: str) -> None:

    # ── Подписка / Кабинет ────────────────────────────────────────────────────

    if data in ("sub_cabinet", "sub_menu"):
        await show_cabinet(update, user_id)
        return

    if data == "sub_trial":
        try:
            await grant_trial(user_id)
            await invalidate_state_cache(user_id)
            caption = (
                f"🎁 *{TRIAL_DAYS} дня доступа активированы.*\n\n"
                "Полный доступ ко всем инструментам — пиши, стратегируй, генерируй.\n"
                "Напиши тему поста или нажми меню 👇"
            )
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
            "*Вот что умею:*\n\n"
            "✍️ *Написать за меня* — пост в твоём голосе\n"
            "🎬 *Хуки для рилса* — заголовки которые останавливают скролл\n"
            "🎠 *Карусель* — структура + 20 вариантов заголовков\n"
            "📸 *Сторис* — цепочки которые досматривают до конца\n"
            "🎙 *Talking Head* — сценарий монолога в кадре\n"
            "🔥 *Прогрев* — серия которая ведёт к покупке\n"
            "📅 *Контент-план TG* — на 7-14 дней\n"
            "🧠 *Мозговой штурм* — 10 идей за секунды\n\n"
            f"*{TRIAL_DAYS} дня бесплатно* — без карты."
        )
        await send(update, _about, parse_mode="Markdown",
                   reply_markup=kb(["🎁 Активировать|sub_trial"], ["💳 Оформить|sub_pay"]))
        return

    if data == "sub_pay":
        link = get_payment_link(user_id)
        if not link:
            await edit(query, "❌ Ссылка на оплату не настроена. Обратись к администратору.",
                       reply_markup=kb(["← Назад|sub_cabinet"]))
            return
        await edit(query,
                   "💳 *Оформление подписки*\n\n"
                   "• 1 месяц — €31\n• 3 месяца — €117 _(скидка 6%)_\n"
                   "• 6 месяцев — €234 _(скидка 10%)_\n• 12 месяцев — €468 _(скидка 13%)_\n\n"
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
        await edit(query, ref_text, parse_mode="Markdown",
                   reply_markup=InlineKeyboardMarkup([
                       [InlineKeyboardButton("📤 Поделиться ссылкой", switch_inline_query=ref_link)],
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

    if data == "menu_main":
        # НЕ очищаем сессии — пользователь может вернуться к незавершённой работе
        from ui.home import show_home
        try:
            await query.message.delete()
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
        await edit(query, "💬 *Спроси Миру*\n\nПиши — отвечу.", parse_mode="Markdown",
                   reply_markup=kb(["← Меню|menu_main"]))
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
        s = await get_agent_session(user_id, _RS_KEY)
        if s:
            await reels.rs_gen_headlines(update, user_id, s.get("topic", ""))
        else:
            await reels.rs_start(update, user_id)
        return

    if data == "rs_pick":
        await reels.rs_pick(update, user_id)
        return

    if data == "rs_add_details":
        s = await get_agent_session(user_id, _RS_KEY)
        if s:
            s["step"] = "await_details_text"
            await save_agent_session(user_id, _RS_KEY, s)
            await edit(query, "✍️ *Напиши детали:*\n\n_Личная история, кейс, цифры_",
                       parse_mode="Markdown")
        return

    if data == "rs_skip_details":
        s = await get_agent_session(user_id, _RS_KEY)
        if s:
            s["details"] = ""
            await save_agent_session(user_id, _RS_KEY, s)
            await reels.rs_await_destination(update, user_id, s)
        return

    if data == "rs_default_cta":
        s = await get_agent_session(user_id, _RS_KEY)
        if s:
            s["destination"] = "подписаться на канал"
            await save_agent_session(user_id, _RS_KEY, s)
            await reels.rs_generate(update, user_id, s)
        return

    if data == "rs_retry_gen":
        s = await get_agent_session(user_id, _RS_KEY)
        if s:
            await reels.rs_generate(update, user_id, s)
        else:
            await reels.rs_start(update, user_id)
        return

    if data == "rs_back_to_pick":
        s = await get_agent_session(user_id, _RS_KEY)
        if not s:
            last = await get_agent_session(user_id, "__rs_last__")
            if last and last.get("headlines"):
                s = {"step": "enter_headline", "topic": last["topic"], "headlines": last["headlines"]}
                await save_agent_session(user_id, _RS_KEY, s)
                await send(update,
                           f"🎯 *Заголовки — тема:* _{last['topic']}_\n\n{last['headlines']}\n\n"
                           "Напиши *номер* (1–14) или скопируй нужный заголовок:",
                           parse_mode="Markdown",
                           reply_markup=kb(["← Назад|rs_regen", "← Меню|menu_main"]))
            else:
                await reels.rs_start(update, user_id)
        else:
            s["step"] = "enter_headline"
            await save_agent_session(user_id, _RS_KEY, s)
            await send(update, "Напиши *номер* (1–14) или скопируй нужный заголовок:",
                       parse_mode="Markdown",
                       reply_markup=kb(["← Назад|rs_regen", "← Меню|menu_main"]))
        return

    # ── Карусель ──────────────────────────────────────────────────────────────

    if data == "flow_carousel":
        await clear_all_agent_sessions(user_id)
        await clear_onboarding_state(user_id)
        await carousel.car_start(update, user_id)
        return

    if data == "car_headlines":
        s = await get_agent_session(user_id, _CAR_KEY)
        if s:
            await carousel.car_gen_headlines(update, user_id, s)
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

    if data == "car_generate":
        s = await get_agent_session(user_id, _CAR_KEY)
        if s:
            await carousel.car_generate(update, user_id, s)
        else:
            await carousel.car_start(update, user_id)
        return

    if data.startswith("cfmt_"):
        fmt = data[5:]
        if fmt in CAROUSEL_FORMATS_4:
            s = await get_agent_session(user_id, _CAR_KEY)
            if s:
                await carousel.car_format_chosen(update, user_id, fmt, s)
        return

    if data == "carousel_fmt_back":
        s = await get_agent_session(user_id, _CAR_KEY)
        if s:
            s["step"] = "pick_format"
            await save_agent_session(user_id, _CAR_KEY, s)
            await edit(query, "Выбери формат 👇", reply_markup=carousel_format_kb())
        return

    if data.startswith("ctrig_"):
        trigger = data[6:]
        if trigger in CAROUSEL_TRIGGERS_20:
            s = await get_agent_session(user_id, _CAR_KEY)
            if s:
                await carousel.car_trigger_chosen(update, user_id, trigger, s)
        return

    if data == "car_fmt_back_to_trigger":
        s = await get_agent_session(user_id, _CAR_KEY)
        if s:
            s["step"] = "pick_trigger"
            await save_agent_session(user_id, _CAR_KEY, s)
            await edit(query, "Выбери триггер 👇", reply_markup=carousel_trigger_kb())
        else:
            await carousel.car_start(update, user_id)
        return

    # ── Быстрые идеи ──────────────────────────────────────────────────────────
    if data == "quick_ideas":
        await clear_all_agent_sessions(user_id)
        await qi_start(update, user_id)
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

    # ── Голосовое подтверждение ───────────────────────────────────────────────
    if data == "voice_send":
        pending = await kv_get(user_id, "__voice_pending__")
        await kv_del(user_id, "__voice_pending__")
        if not pending:
            await edit(query, "Нет ожидающего запроса.", reply_markup=kb(["← Меню|menu_main"]))
            return
        await edit(query, f"🎙 _{pending}_", parse_mode="Markdown")
        from handlers.messages import _route
        await _route(update, ctx, user_id, pending)
        return

    if data == "voice_edit":
        pending = await kv_get(user_id, "__voice_pending__")
        if not pending:
            await edit(query, "Нет ожидающего запроса.", reply_markup=kb(["← Меню|menu_main"]))
            return
        await kv_set(user_id, "__voice_edit_mode__", "1", ttl=3600)
        await edit(query, f"✏️ Отредактируй и отправь:\n\n`{pending}`",
                   parse_mode="Markdown", reply_markup=kb(["❌ Отмена|voice_edit_cancel"]))
        return

    if data == "voice_edit_cancel":
        await kv_del(user_id, "__voice_pending__")
        await kv_del(user_id, "__voice_edit_mode__")
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
        _stop = asyncio.Event()
        _tt   = asyncio.create_task(_typing_loop(update.effective_chat, _stop))
        try:
            profile = await get_profile(user_id)
            result  = await oneshot_generate(agent_key, orig_text, profile)
        except Exception as e:
            logger.error(f"intent_oneshot error: {e}")
            result = None
        finally:
            _stop.set()
            _tt.cancel()
            await safe_delete(status)
        if result:
            deep_cb = (
                "flow_reels_short" if agent_key == "reels_short" else
                "flow_carousel"    if agent_key == "carousel"    else
                f"agent_start_{agent_key}"
            )
            # Сохраняем oneshot в БД как полноценный результат
            _result_id = 0
            try:
                from db import save_result as _sr
                _result_id = await _sr(user_id, agent_key, f"{name} (быстро)", result)
            except Exception as _e:
                logger.warning(f"oneshot save_result failed: {_e}")

            await send(update, result, parse_mode="Markdown",
                       reply_markup=kb(
                           [f"⚡ Детальнее с вопросами|{deep_cb}",
                            f"💾 Сохранить|oneshot_save_{agent_key}"],
                           ["✏️ Доработать|refine_last", "🔄 Другой вариант|regen_last"],
                       ))
            await kv_set(user_id, "__oneshot_draft__",
                         json.dumps({"agent": agent_key, "name": name, "content": result},
                                    ensure_ascii=False))

            # Voice feedback на oneshot тоже
            if _result_id:
                import asyncio as _aio
                await _aio.sleep(0.8)
                from voice_learner import voice_feedback_kb as _vfkb, get_voice_stats as _gvs
                try:
                    _vs    = await _gvs(user_id)
                    _total = _vs.get("total_signals", 0)
                    if _total == 0:
                        _hint = "\n\n_Оцени — и Мира запомнит твой стиль._"
                    elif _total < 5:
                        _filled = "▓" * _total
                        _empty  = "░" * (5 - _total)
                        _hint   = f"\n\n_Голос Миры: [{_filled}{_empty}] {_total}/5_"
                    else:
                        _hint = f"\n\n_Голос Миры: прокачан ({_total} сигналов) 🎯_"
                except Exception:
                    _hint = ""
                await send(update, f"Звучит как твой голос?{_hint}",
                           parse_mode="Markdown", reply_markup=_vfkb(_result_id))

            # Usage-based конверсионный триггер (после 3-й генерации в триале)
            try:
                from user_state import get_user_state, UserState
                from db import get_stats as _gs
                _state = await get_user_state(user_id)
                if _state == UserState.TRIAL:
                    _stats = await _gs(user_id)
                    if _stats.get("total", 0) == 3:
                        import asyncio as _aio2
                        await _aio2.sleep(1.5)
                        from lava_payments import get_payment_link
                        _link = get_payment_link(user_id)
                        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
                        _pay_kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton("💳 Оформить подписку", url=_link)
                        ]] if _link else [])
                        await update.effective_chat.send_message(
                            "Ты уже создала 3 материала — это хороший результат.\n\n"
                            "Триал заканчивается. Хочешь продолжить без ограничений?",
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
        except Exception as e:
            from utils import send
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

    # ── Follow-up ответы ─────────────────────────────────────────────────────
    if data.startswith("fu_"):
        from flows.followup import handle_followup_callback
        await handle_followup_callback(ctx.bot, user_id, data)
        return

    logger.warning(f"[callback] unhandled data={data!r} user={user_id}")
