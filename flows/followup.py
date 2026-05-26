"""
flows/followup.py — отложенный follow-up через 3 дня после генерации.

Через 3 дня после того как пользователь создал контент — Мира пишет:
«Как зашёл тот пост/рилс?»

Зачем:
1. Создаёт петлю обратной связи — пользователь думает о результате
2. Собирает социальное доказательство (если спросить правильно)
3. Re-engagement без давления — самый органичный тач
4. Возможность предложить следующий шаг на основе результата

Реализация v2 — Redis TTL-флаги вместо job_queue.run_once():
- schedule_followup() пишет флаг в Redis с TTL 72ч
- Периодический job run_check_followups() (каждые 30 мин) сканирует
  у кого флаг истёк (due) и отправляет сообщение
- Флаги переживают рестарты Railway/деплои — не теряем follow-ups

Ключи Redis: followup:{user_id}:{result_id} = agent_key (TTL 72h)
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_FOLLOWUP_TTL   = 72 * 3600        # 72 часа до отправки
_FOLLOWUP_SENT_TTL = 7 * 24 * 3600 # 7 дней — маркер «уже отправлено»
_KEY_PREFIX     = "followup"
_SENT_PREFIX    = "followup_sent"
_MAX_ACTIVE_PER_USER = 2           # не более 2 активных follow-up на пользователя

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


# ─────────────────────────────────────────────────────────────────────────────
# Redis helpers
# ─────────────────────────────────────────────────────────────────────────────

def _flag_key(user_id: int, result_id: int) -> str:
    return f"{_KEY_PREFIX}:{user_id}:{result_id}"

def _sent_key(user_id: int, result_id: int) -> str:
    return f"{_SENT_PREFIX}:{user_id}:{result_id}"


async def _set_followup_flag(user_id: int, result_id: int, agent_key: str) -> None:
    """Записывает флаг в Redis. Значение = agent_key, TTL = 72ч."""
    try:
        from db import get_redis
        r = await get_redis()
        key = _flag_key(user_id, result_id)
        await r.set(key, agent_key, ex=_FOLLOWUP_TTL)
    except Exception as e:
        logger.warning(f"followup set_flag failed uid={user_id} rid={result_id}: {e}")


async def _count_active_flags(user_id: int) -> int:
    """Считает активные (не истёкшие) follow-up флаги для пользователя."""
    try:
        from db import get_redis
        r = await get_redis()
        pattern = f"{_KEY_PREFIX}:{user_id}:*"
        keys = await r.keys(pattern)
        return len(keys)
    except Exception:
        return 0


async def _get_due_followups() -> list[tuple[int, int, str]]:
    """
    Сканирует Redis в поиске флагов у которых TTL истёк (<= 0) —
    это значит 72 часа прошли, пора отправлять.

    Возвращает список (user_id, result_id, agent_key).

    Логика: при записи флаг получает TTL=72h. Когда TTL доходит до 0,
    ключ исчезает из Redis. Нам нужно отправлять ДО исчезновения,
    поэтому проверяем: TTL < CHECK_INTERVAL_SEC (1800 сек = 30 мин).
    Это даёт окно: флаг «созрел» если у него осталось меньше 30 мин.
    """
    CHECK_INTERVAL_SEC = 1800  # совпадает с интервалом run_check_followups
    try:
        from db import get_redis
        r = await get_redis()
        pattern = f"{_KEY_PREFIX}:*:*"
        keys = await r.keys(pattern)
        due = []
        for key in keys:
            ttl = await r.ttl(key)
            # ttl <= 0: ключ истёк (мы опоздали) или не имеет TTL
            # 0 < ttl <= CHECK_INTERVAL_SEC: созрел в следующие 30 мин — отправляем сейчас
            if ttl <= CHECK_INTERVAL_SEC:
                agent_key = await r.get(key)
                if not agent_key:
                    continue
                # Парсим user_id и result_id из ключа followup:{uid}:{rid}
                parts = key.split(":")
                if len(parts) != 3:
                    continue
                try:
                    uid = int(parts[1])
                    rid = int(parts[2])
                except ValueError:
                    continue
                # Проверяем что ещё не отправляли
                sent = await r.get(_sent_key(uid, rid))
                if sent:
                    continue
                due.append((uid, rid, agent_key if isinstance(agent_key, str) else agent_key.decode()))
        return due
    except Exception as e:
        logger.warning(f"get_due_followups error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def schedule_followup(app, user_id: int, agent_key: str, result_id: int) -> None:
    """
    Планирует follow-up через 72 часа через Redis TTL-флаг.
    Переживает рестарты Railway/деплои.
    Вызывается из save_result() — только для подписчиков и триала.
    """
    try:
        from user_state import get_user_state, has_access
        state = await get_user_state(user_id)
        if not has_access(state):
            return

        # Не накапливаем больше MAX_ACTIVE_PER_USER флагов на пользователя
        active = await _count_active_flags(user_id)
        if active >= _MAX_ACTIVE_PER_USER:
            logger.debug(f"followup skip uid={user_id}: {active} already active")
            return

        await _set_followup_flag(user_id, result_id, agent_key)
        logger.info(f"Followup scheduled (Redis): uid={user_id} agent={agent_key} rid={result_id} due=72h")
    except Exception as e:
        logger.warning(f"schedule_followup failed: {e}")


async def run_check_followups(bot) -> None:
    """
    Вызывается каждые 30 минут из job_queue.
    Находит созревшие флаги и отправляет follow-up сообщения.
    """
    due = await _get_due_followups()
    if not due:
        return

    logger.info(f"[followup] checking due: {len(due)} items")

    for user_id, result_id, agent_key in due:
        try:
            await _send_followup(bot, user_id, agent_key, result_id)
        except Exception as e:
            logger.warning(f"[followup] send error uid={user_id}: {e}")
        await asyncio.sleep(0.3)  # мягкий throttle между отправками


async def _send_followup(bot, user_id: int, agent_key: str, result_id: int) -> None:
    """Отправляет follow-up сообщение и ставит маркер 'sent'."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from db import get_redis

    text = _FOLLOWUP_MESSAGES.get(agent_key, _DEFAULT_FOLLOWUP)
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
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        logger.info(f"Followup sent: uid={user_id} agent={agent_key}")

        # Ставим маркер «отправлено» + удаляем флаг
        r = await get_redis()
        await r.set(_sent_key(user_id, result_id), "1", ex=_FOLLOWUP_SENT_TTL)
        await r.delete(_flag_key(user_id, result_id))

    except Exception as e:
        err = str(e).lower()
        if "blocked" in err or "deactivated" in err:
            logger.info(f"Followup skipped: uid={user_id} blocked bot")
            # Удаляем флаг чтобы не пытаться снова
            try:
                r = await get_redis()
                await r.delete(_flag_key(user_id, result_id))
            except Exception:
                pass
        else:
            logger.warning(f"Followup send failed uid={user_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Callback handler (без изменений — только перенесён)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_followup_callback(bot, user_id: int, data: str) -> None:
    """
    Handle user's follow-up response.
    data format: fu_{outcome}_{result_id}

    outcome feeds into voice learner:
      good    → mark_approved (positive reinforcement)
      bad/mid → add_rejection_pattern (negative reinforcement)
    """
    parts     = data.split("_")
    outcome   = parts[1] if len(parts) > 1 else "mid"
    result_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    # Feed signal into voice learner
    if result_id:
        try:
            from db import get_result_by_id
            from voice_learner import mark_approved, add_rejection_pattern
            r = await get_result_by_id(user_id, result_id)
            if r:
                if outcome == "good":
                    await mark_approved(user_id, r["agent_key"], r["content"])
                    logger.debug("Followup good → mark_approved uid=%s rid=%s", user_id, result_id)
                elif outcome == "bad":
                    await add_rejection_pattern(
                        user_id,
                        f"{r['agent_name']}: не сработало в публикации",
                    )
                    logger.debug("Followup bad → add_rejection uid=%s rid=%s", user_id, result_id)
                # "mid" и "pending" — сигнал неоднозначный, не используем
        except Exception as e:
            logger.warning("followup voice signal failed uid=%s: %s", user_id, e)

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
