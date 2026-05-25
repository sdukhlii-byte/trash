"""
ui/cabinet.py — личный кабинет и post-generation upsell.

Изменения v2:
- После генерации для TRIAL-пользователей — soft upsell (не навязчивый).
- Кабинет объединяет статус подписки + профиль в одном экране.
"""
import logging

from telegram import Update

from db import get_profile, get_style_examples
from lava_payments import (
    render_status, render_history, render_referral,
    get_subscription, get_trial, has_used_trial,
    get_payment_link, subscription_menu_kb, cabinet_kb,
    TRIAL_DAYS,
)
from user_state import UserState
from utils import send, edit, kb, profile_val

logger = logging.getLogger(__name__)


async def show_cabinet(update: Update, user_id: int) -> None:
    state_obj = None
    from user_state import get_user_state
    state_obj = await get_user_state(user_id)

    status_text = await render_status(user_id)
    p = await get_profile(user_id)
    examples = await get_style_examples(user_id)
    ex_count = len(examples)

    # Голос Миры — полный прогресс-бар
    from voice_learner import get_voice_stats
    from ui.progress_bar import voice_progress, trial_urgency
    try:
        vs = await get_voice_stats(user_id)
        voice_line = f"\n\n{voice_progress(vs.get('total_signals', 0))}"
    except Exception:
        voice_line = ""

    profile_line = (
        f"\n\n*Профиль:*\n"
        f"Ниша: {profile_val(p, 'niche')}\n"
        f"Аудитория: {profile_val(p, 'audience')}\n"
        f"Тон: {profile_val(p, 'tone')}\n"
        f"Примеры стиля: {ex_count}/10"
    )

    rows = []
    if state_obj == UserState.ONBOARDED:
        rows.append(["🎁 Активировать 7 дней бесплатно|sub_trial"])
        rows.append(["💳 Оформить подписку|sub_pay"])
    elif state_obj == UserState.TRIAL:
        rows.append(["💳 Оформить подписку|sub_pay"])
    elif state_obj == UserState.SUBSCRIBED:
        rows.append(["💳 Продлить подписку|sub_pay"])
        rows.append(["👥 Пригласи коллегу — получи 7 дней|cab_referral"])
    elif state_obj == UserState.EXPIRED:
        rows.append(["🔄 Возобновить подписку|sub_pay"])

    rows += [
        ["✏️ Изменить профиль|profile_edit",  "📝 Стиль|style_menu"],
        ["🧾 История платежей|cab_history",
         "👥 Реферал|cab_referral" if state_obj != UserState.SUBSCRIBED else "📊 Прогресс|show_stats"],
        ["🤖 Сменить модель|profile_model"],
        ["← Меню|menu_main"],
    ]

    await send(
        update,
        f"⚙️ *Кабинет*\n\n{status_text}{profile_line}{voice_line}",
        parse_mode="Markdown",
        reply_markup=kb(*rows),
    )


async def maybe_show_upsell(update: Update, user_id: int) -> None:
    """
    Показывает мягкий upsell ПОСЛЕ генерации для триал-пользователей.
    Не блокирует — отправляется отдельным сообщением.
    """
    from user_state import get_user_state, UserState as US
    state = await get_user_state(user_id)
    if state != US.TRIAL:
        return

    trial = await get_trial(user_id)
    if not trial:
        return

    from datetime import datetime, timezone
    expires = datetime.fromisoformat(trial["expires_at"])
    days_left = max(0, (expires - datetime.now(timezone.utc)).days)

    if days_left <= 1:
        msg = (
            f"⏳ _Триал заканчивается {'сегодня' if days_left == 0 else 'завтра'}._\n\n"
            f"Продолжи работу — оформи подписку и не теряй накопленный контент."
        )
    else:
        msg = (
            f"💡 _Понравилось? Сохраняй, дорабатывай, генерируй без ограничений._"
        )

    await send(
        update,
        msg,
        parse_mode="Markdown",
        reply_markup=kb(
            ["💳 Оформить подписку|sub_pay"],
            ["← Пропустить|menu_main"],
        ),
    )
