"""
flows/followup.py — отложенный follow-up через 3 дня после генерации.

Через 3 дня после того как пользователь создал контент — Мира пишет:
«Как зашёл тот пост/рилс?»

Зачем:
1. Создаёт петлю обратной связи — пользователь думает о результате
2. Собирает социальное доказательство (если спросить правильно)
3. Re-engagement без давления — самый органичный тач
4. Возможность предложить следующий шаг на основе результата

Реализация:
- При сохранении результата в save_result() планируется followup job
- Через 72 часа отправляется вопрос
- Пользователь может ответить → Мира даёт совет
- Если бот заблокирован → job удаляется тихо
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Сообщения follow-up по типу агента
_FOLLOWUP_MESSAGES: dict[str, str] = {
    "carousel": (
        "📊 Привет!\n\n"
        "Три дня назад мы делали карусель. Как зашла?\n\n"
        "_Цифры, реакции, заявки — любой результат интересен._"
    ),
    "warmup": (
        "🔥 Привет!\n\n"
        "Три дня назад делали прогрев. Запуск прошёл?\n\n"
        "_Что зашло, что нет — расскажи, дам обратную связь._"
    ),
    "post": (
        "✍️ Привет!\n\n"
        "Три дня назад писали пост. Как он сработал?\n\n"
        "_Сохранения, охват, комменты — что заметила?_"
    ),
    "reels_short": (
        "🎬 Привет!\n\n"
        "Три дня назад делали хуки для рилса. Сняла?\n\n"
        "_Если да — как зашёл? Если нет — что остановило?_"
    ),
    "talking_head": (
        "🎙 Привет!\n\n"
        "Три дня назад писали сценарий. Сняла?\n\n"
        "_Расскажи — дам обратную связь или поправим сценарий._"
    ),
    "tg_plan": (
        "📅 Привет!\n\n"
        "Три дня назад составляли план. Идёт?\n\n"
        "_Что уже вышло, что буксует — расскажи._"
    ),
    "profile": (
        "🔍 Привет!\n\n"
        "Три дня назад делали разбор профиля. Что-то изменила?\n\n"
        "_Даже маленький шаг важен — поделись._"
    ),
    "stories": (
        "📸 Привет!\n\n"
        "Три дня назад писали сторис. Как досматриваемость?\n\n"
        "_Результаты интересны — расскажи._"
    ),
}

_DEFAULT_FOLLOWUP = (
    "💬 Привет!\n\n"
    "Три дня назад создавали контент. Как сработало?\n\n"
    "_Расскажи — дам обратную связь или поможем с следующим шагом._"
)

_FOLLOWUP_KB_DATA = "followup_response"


async def schedule_followup(app, user_id: int, agent_key: str, result_id: int) -> None:
    """
    Планирует follow-up через 72 часа.
    Вызывается из save_result() — но только для подписчиков и триала.
    """
    try:
        from user_state import get_user_state, has_access
        state = await get_user_state(user_id)
        if not has_access(state):
            return

        job_name = f"followup_{user_id}_{result_id}"
        # Не дублируем если уже запланирован для этого пользователя
        existing = app.job_queue.get_jobs_by_name(f"followup_{user_id}_")
        if len(existing) >= 2:
            return

        when = datetime.now(timezone.utc) + timedelta(hours=72)

        app.job_queue.run_once(
            callback=_followup_job,
            when=when,
            name=job_name,
            data={"user_id": user_id, "agent_key": agent_key, "result_id": result_id},
        )
        logger.info(f"Followup scheduled: user={user_id} agent={agent_key} at {when.strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        logger.warning(f"schedule_followup failed: {e}")


async def _followup_job(ctx) -> None:
    data      = ctx.job.data
    user_id   = data["user_id"]
    agent_key = data.get("agent_key", "post")
    result_id = data.get("result_id", 0)

    text = _FOLLOWUP_MESSAGES.get(agent_key, _DEFAULT_FOLLOWUP)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Сработало отлично!", callback_data=f"fu_good_{result_id}"),
            InlineKeyboardButton("😐 Средне", callback_data=f"fu_mid_{result_id}"),
        ],
        [
            InlineKeyboardButton("😔 Не вышло", callback_data=f"fu_bad_{result_id}"),
            InlineKeyboardButton("Ещё не публиковала", callback_data=f"fu_pending_{result_id}"),
        ],
    ])

    try:
        await ctx.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        logger.info(f"Followup sent: user={user_id} agent={agent_key}")
    except Exception as e:
        err = str(e).lower()
        if "blocked" in err or "deactivated" in err:
            logger.info(f"Followup skipped: user {user_id} blocked bot")
        else:
            logger.warning(f"Followup send failed for {user_id}: {e}")


async def handle_followup_callback(bot, user_id: int, data: str) -> None:
    """
    Обрабатывает ответ пользователя на follow-up.
    data формат: fu_{outcome}_{result_id}
    """
    parts   = data.split("_")
    outcome = parts[1] if len(parts) > 1 else "mid"
    result_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    if outcome == "good":
        response = (
            "🎉 Отлично! Это именно то что должно работать.\n\n"
            "Хочешь продолжить в том же духе? Могу сделать ещё один пост/рилс на похожую тему "
            "или переупаковать этот в другой формат."
        )
    elif outcome == "mid":
        response = (
            "Понятно — «средне» это часто про формат или момент публикации, "
            "не про качество контента.\n\n"
            "Расскажи подробнее: что смотрела — охваты, сохранения, комменты? "
            "Посмотрю где именно можно было сильнее."
        )
    elif outcome == "bad":
        response = (
            "Бывает. Давай разберём — обычно причина в одном из трёх: "
            "хук, момент публикации, или аудитория пока не прогрета.\n\n"
            "Что конкретно случилось? Посмотрю и предложу что изменить."
        )
    else:  # pending
        response = (
            "Понятно — когда опубликуешь, напиши как зашло. "
            "Если нужна помощь с публикацией или захочешь что-то поправить — я здесь."
        )

    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    try:
        await bot.send_message(
            chat_id=user_id,
            text=response,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("☰ В меню", callback_data="menu_main")],
            ]),
        )
    except Exception as e:
        logger.warning(f"followup response failed for {user_id}: {e}")
