"""
handlers/commands.py — обработчики команд Telegram.
"""
import logging
import os

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from db import (
    get_onboarding_state, save_onboarding_state, clear_onboarding_state,
    clear_all, kv_del, kv_get, kv_set, get_profile,
)
from user_state import get_user_state, has_access, UserState, invalidate_state_cache
from lava_payments import get_trial, register_referral
from flows.onboarding import onb_next
from ui.menu import show_menu, main_menu_kb
from ui.home import show_home
from ui.paywall import show_paywall
from ui.cabinet import show_cabinet
from utils import send, kb

logger = logging.getLogger(__name__)

SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "Stanley_Berks")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    logger.info(f"[cmd_start] user={user_id}")

    # Реф-ссылка + UTM-трекинг
    if ctx.args:
        arg = ctx.args[0]
        if arg.startswith("ref_"):
            try:
                await register_referral(user_id, arg[4:])
            except Exception as e:
                logger.warning(f"register_referral error: {e}")
        elif arg.startswith("utm_"):
            try:
                from flows.utm import track_start
                await track_start(user_id, arg)
            except Exception as e:
                logger.warning(f"utm track_start error: {e}")

    state = await get_user_state(user_id)

    if state == UserState.NEW:
        onb = await get_onboarding_state(user_id)
        if onb:
            await clear_onboarding_state(user_id)
        new_state = {"step": 0, "data": {}}
        await save_onboarding_state(user_id, new_state)
        from config import MIRA_INTRO
        await send(update, MIRA_INTRO)
        await onb_next(update, user_id, new_state)
        return

    if state == UserState.TRIAL:
        await show_home(update, user_id)
        return

    if state == UserState.SUBSCRIBED:
        await show_home(update, user_id)
        return

    await show_paywall(update, user_id, state)


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = await get_user_state(user_id)
    if state == UserState.NEW:
        await cmd_start(update, ctx)
        return
    if not has_access(state):
        await show_paywall(update, user_id, state)
        return
    await show_menu(update, user_id)


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await clear_all(user_id)
    state = await get_user_state(user_id)
    if has_access(state):
        await send(update, "🗑 История очищена.", reply_markup=main_menu_kb())
    else:
        await send(update, "🗑 История очищена.")
        await show_paywall(update, user_id, state)


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Полный сброс → онбординг заново."""
    user_id = update.effective_user.id
    await clear_all(user_id)
    await kv_del(user_id, "__profile__")
    await kv_del(user_id, "__model__")
    await kv_del(user_id, "__onboarding__")
    await invalidate_state_cache(user_id)

    # Сбрасываем push_log чтобы retention пуши пришли повторно
    try:
        from db import _get_pool
        pool = _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM push_log WHERE user_id=$1 AND push_type IN ('onboarded', 'expired')",
                user_id,
            )
    except Exception as e:
        logger.warning(f"cmd_reset: could not clear push_log for {user_id}: {e}")

    state = {"step": 0, "data": {}}
    await save_onboarding_state(user_id, state)
    await onb_next(update, user_id, state)


async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await show_cabinet(update, user_id)


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /admin для администратора — статистика и управление."""
    from security import ADMIN_ID
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    sub = ctx.args[0] if ctx.args else "help"

    if sub == "utm":
        from flows.utm import get_utm_report
        report = await get_utm_report()
        await send(update, report, parse_mode="Markdown")

    elif sub == "stats":
        from db import _get_pool
        pool = _get_pool()
        async with pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(DISTINCT user_id) FROM messages") or 0
            total_subs  = await conn.fetchval("SELECT COUNT(*) FROM subscriptions WHERE status='active'") or 0
            total_trial = await conn.fetchval("SELECT COUNT(*) FROM trials") or 0
            total_results = await conn.fetchval("SELECT COUNT(*) FROM results") or 0
        await send(
            update,
            f"📊 *Статистика*\n\n"
            f"Пользователей: *{total_users}*\n"
            f"Активных подписок: *{total_subs}*\n"
            f"Триалов: *{total_trial}*\n"
            f"Создано материалов: *{total_results}*\n\n"
            f"CR триал→оплата: *{round(total_subs/total_trial*100 if total_trial else 0)}%*",
            parse_mode="Markdown",
        )

    else:
        await send(
            update,
            "*Admin команды:*\n\n"
            "/admin utm — статистика по источникам трафика\n"
            "/admin stats — общая статистика",
            parse_mode="Markdown",
        )


async def cmd_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await send(
        update,
        f"🆘 *Поддержка*\n\nЕсли возникли вопросы — пиши напрямую.\n\nМенеджер: @{SUPPORT_USERNAME}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"💬 Написать @{SUPPORT_USERNAME}",
                                 url=f"https://t.me/{SUPPORT_USERNAME}"),
        ]]),
    )
