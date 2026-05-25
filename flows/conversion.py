"""
flows/conversion.py — конверсионная воронка.

Закрывает четыре главные утечки:

УТЕЧКА 1: Онбординг → не нажали "активировать триал"
  Решение: через 15 минут после онбординга — если триал не активирован,
  отправляем конкретный пример результата под их нишу.

УТЕЧКА 2: Триал день 1 → не вернулись на день 2
  Решение: через 20 часов после активации — "вот что ты ещё не попробовала"
  с конкретным агентом под их нишу.

УТЕЧКА 3: Триал истекает — последний день
  Решение: за 6 часов до истечения — персональное сообщение с их материалами.

УТЕЧКА 4: Триал истёк → не купили
  Решение: уже в retention.py (EXPIRED_CHAIN), но добавляем
  тактику "ограниченного предложения" для шага 1 (48 часов).
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Ключи для отслеживания что уже отправлено
_KEY_ONBOARDING_NUDGE   = "__cv_onb_nudge__"
_KEY_DAY2_SENT          = "__cv_day2__"
_KEY_LASTDAY_SENT       = "__cv_lastday__"


# ═══════════════════════════════════════════════════════════════════════
# УТЕЧКА 1: Онбординг без активации триала
# ═══════════════════════════════════════════════════════════════════════

async def schedule_onboarding_nudge(app, user_id: int) -> None:
    """
    Планирует напоминание через 15 минут если триал не активирован.
    Вызывать в конце _finish_onboarding() для пользователей без доступа.
    """
    from db import kv_get
    try:
        existing = await kv_get(user_id, _KEY_ONBOARDING_NUDGE)
        if existing:
            return  # уже запланировано

        app.job_queue.run_once(
            callback=_onboarding_nudge_job,
            when=timedelta(minutes=15),
            name=f"onb_nudge_{user_id}",
            data=user_id,
        )
        from db import kv_set
        await kv_set(user_id, _KEY_ONBOARDING_NUDGE, "1", ttl=86400)
        logger.info(f"Onboarding nudge scheduled: user={user_id}")
    except Exception as e:
        logger.warning(f"schedule_onboarding_nudge: {e}")


async def _onboarding_nudge_job(ctx) -> None:
    user_id = ctx.job.data
    from user_state import get_user_state, has_access
    from db import get_profile

    try:
        state = await get_user_state(user_id)
        if has_access(state):
            return  # уже активировал — не беспокоим

        profile = await get_profile(user_id)
        niche   = profile.get("niche", "твоей теме")

        # Показываем конкретный пример под нишу — это и есть WOW
        from niche_intel import get_niche_intel
        intel    = get_niche_intel(niche)
        works    = intel.get("works", "")
        example  = works.split("\n")[1].strip("• ").split("\n")[0] if works else "создания контента"

        text = (
            f"Кстати.\n\n"
            f"Для твоей ниши «{niche[:30]}» лучше всего работает: {example.lower()}\n\n"
            f"Хочешь посмотреть как Мира напишет это конкретно под твою аудиторию?\n\n"
            f"*{3} дня бесплатно — без карты.*"
        )

        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        from lava_payments import get_payment_link
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎁 Да, показывай", callback_data="sub_trial"),
        ]])

        await ctx.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        from flows.utm import track_event
        await track_event(user_id, "onboarding_nudge_sent")
        logger.info(f"Onboarding nudge sent: user={user_id}")
    except Exception as e:
        logger.warning(f"_onboarding_nudge_job failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# УТЕЧКА 2: Триал день 2 — не вернулись
# ═══════════════════════════════════════════════════════════════════════

async def schedule_day2_nudge(app, user_id: int) -> None:
    """
    Планирует Day-2 нудж через 20 часов после активации триала.
    Вызывать при grant_trial().
    """
    from db import kv_get, kv_set
    try:
        existing = await kv_get(user_id, _KEY_DAY2_SENT)
        if existing:
            return

        app.job_queue.run_once(
            callback=_day2_nudge_job,
            when=timedelta(hours=20),
            name=f"day2_{user_id}",
            data=user_id,
        )
        await kv_set(user_id, _KEY_DAY2_SENT, "scheduled", ttl=86400 * 4)
        logger.info(f"Day-2 nudge scheduled: user={user_id}")
    except Exception as e:
        logger.warning(f"schedule_day2_nudge: {e}")


async def _day2_nudge_job(ctx) -> None:
    user_id = ctx.job.data
    from user_state import get_user_state, UserState
    from db import get_profile, get_results, kv_set

    try:
        state = await get_user_state(user_id)
        if state == UserState.SUBSCRIBED:
            return  # уже купил

        profile     = await get_profile(user_id)
        results     = await get_results(user_id, limit=3)
        niche       = profile.get("niche", "")

        # Показываем что они ещё не попробовали
        used_agents = {r["agent_key"] for r in results}
        untried     = _get_untried_recommendation(niche, used_agents)

        if results:
            created_count = len(results)
            text = (
                f"Вчера создала {created_count} {'материал' if created_count == 1 else 'материала'}.\n\n"
                f"Есть ещё кое-что под твою нишу что стоит попробовать: {untried['name']}.\n\n"
                f"_{untried['pitch']}_"
            )
        else:
            text = (
                f"Триал активирован, но ты ещё ничего не создала.\n\n"
                f"Самое быстрое с чего начать для «{niche[:30]}» — {untried['name']}.\n\n"
                f"_{untried['pitch']}_"
            )

        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"→ Попробовать {untried['name']}", callback_data=untried["cb"]),
        ]])

        await ctx.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        await kv_set(user_id, _KEY_DAY2_SENT, "sent", ttl=86400 * 4)
        from flows.utm import track_event
        await track_event(user_id, "day2_nudge_sent")
    except Exception as e:
        logger.warning(f"_day2_nudge_job failed: {e}")


def _get_untried_recommendation(niche: str, used_agents: set) -> dict:
    """Возвращает агента которого ещё не пробовали — под нишу."""
    niche_l = niche.lower()

    candidates = []
    if "warmup" not in used_agents:
        candidates.append({
            "name": "Прогрев",
            "pitch": "Серия которая ведёт к покупке — без ощущения «впаривания».",
            "cb":    "agent_start_warmup",
        })
    if "carousel" not in used_agents:
        candidates.append({
            "name": "Карусель",
            "pitch": "20 вариантов заголовков + структура под твою аудиторию.",
            "cb":    "flow_carousel",
        })
    if "reels_short" not in used_agents:
        candidates.append({
            "name": "Хуки для рилса",
            "pitch": "14 заголовков которые останавливают скролл.",
            "cb":    "flow_reels_short",
        })
    if "stories" not in used_agents:
        candidates.append({
            "name": "Сторис",
            "pitch": "Цепочка которую досматривают до конца.",
            "cb":    "agent_start_stories",
        })
    if "tg_plan" not in used_agents:
        candidates.append({
            "name": "Контент-план TG",
            "pitch": "7 дней с темами, форматами и первыми фразами.",
            "cb":    "agent_start_tg_plan",
        })

    # Если всё попробовали
    if not candidates:
        return {
            "name": "Разбор профиля",
            "pitch": "Честный аудит аккаунта — где теряются клиенты.",
            "cb":    "agent_start_profile",
        }

    # Приоритеты по нише
    if any(kw in niche_l for kw in ["запуск", "курс", "продукт", "коуч", "тренер"]):
        warmup = next((c for c in candidates if c["cb"] == "agent_start_warmup"), None)
        if warmup:
            return warmup

    return candidates[0]


# ═══════════════════════════════════════════════════════════════════════
# УТЕЧКА 3: Последний день триала
# ═══════════════════════════════════════════════════════════════════════

async def schedule_lastday_nudge(app, user_id: int, expires_at: str) -> None:
    """
    Планирует сообщение за 6 часов до истечения триала.
    Вызывать при grant_trial() вместе с day2.
    """
    from db import kv_get
    try:
        existing = await kv_get(user_id, _KEY_LASTDAY_SENT)
        if existing:
            return

        exp_dt  = datetime.fromisoformat(expires_at)
        trigger = exp_dt - timedelta(hours=6)
        now     = datetime.now(timezone.utc)

        if trigger <= now:
            return  # уже прошло

        app.job_queue.run_once(
            callback=_lastday_nudge_job,
            when=trigger,
            name=f"lastday_{user_id}",
            data=user_id,
        )
        from db import kv_set
        await kv_set(user_id, _KEY_LASTDAY_SENT, "scheduled", ttl=86400 * 5)
        logger.info(f"Last-day nudge scheduled: user={user_id} trigger={trigger.strftime('%H:%M')}")
    except Exception as e:
        logger.warning(f"schedule_lastday_nudge: {e}")


async def _lastday_nudge_job(ctx) -> None:
    user_id = ctx.job.data
    from user_state import get_user_state, UserState
    from db import get_results, kv_set
    from lava_payments import get_payment_link

    try:
        state = await get_user_state(user_id)
        if state == UserState.SUBSCRIBED:
            return

        results = await get_results(user_id, limit=3)

        if results:
            names = ", ".join(r["agent_name"] for r in results[:2])
            created_text = f"За эти дни создала: {names}{'и другие' if len(results) > 2 else ''}."
        else:
            created_text = "Ты только начала — и это нормально."

        text = (
            f"⏳ *Сегодня последний день триала.*\n\n"
            f"{created_text}\n\n"
            f"Если продолжишь — все материалы останутся, голос Миры уже настроен на тебя.\n\n"
            f"Если нет — доступ закроется, но сохранённое никуда не денется."
        )

        link = get_payment_link(user_id)
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        buttons = []
        if link:
            buttons.append([InlineKeyboardButton("💳 Оформить подписку", url=link)])
        buttons.append([InlineKeyboardButton("← В меню", callback_data="menu_main")])
        kb = InlineKeyboardMarkup(buttons)

        await ctx.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        await kv_set(user_id, _KEY_LASTDAY_SENT, "sent", ttl=86400 * 5)
        from flows.utm import track_event
        await track_event(user_id, "lastday_nudge_sent")
    except Exception as e:
        logger.warning(f"_lastday_nudge_job failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# ХЕЛПЕР: вызывается из grant_trial()
# ═══════════════════════════════════════════════════════════════════════

async def on_trial_activated(app, user_id: int, expires_at: str) -> None:
    """
    Единая точка входа при активации триала.
    Планирует все конверсионные нуджи.
    """
    await schedule_day2_nudge(app, user_id)
    await schedule_lastday_nudge(app, user_id, expires_at)
    from flows.utm import track_event
    await track_event(user_id, "trial_activated")
