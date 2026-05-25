"""
flows/conversion.py — конверсионная воронка.

v3: Переход от in-memory job_queue к DB-флагам.

Проблема оригинала: job_queue.run_once() живёт в памяти процесса.
При рестарте Railway (деплой, краш, OOM) все запланированные нуджи теряются.
Пользователь который прошёл онбординг за 5 минут до рестарта — никогда не
получит Day-2 нудж. Молча. Без ошибок в логах.

Решение: храним флаги в Redis (kv_set с TTL). Два APScheduler-задания
(run_hourly_conversion, run_trial_warnings) проверяют кого уже пора нуджить.
Это идентично паттерну в retention.py и переживает любые рестарты.

Закрывает четыре главные утечки:
  УТЕЧКА 1: Онбординг → не нажали "активировать триал"
    → run_hourly_conversion проверяет пользователей onboarded >15 мин назад без триала
  УТЕЧКА 2: Триал день 1 → не вернулись на день 2
    → run_hourly_conversion проверяет активных триальщиков >20ч без генерации
  УТЕЧКА 3: Триал истекает — последний день
    → run_trial_warnings (уже есть в retention.py) — за 24ч/6ч
  УТЕЧКА 4: Триал истёк → не купили
    → retention.py EXPIRED_CHAIN
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Redis-ключи (TTL-флаги вместо in-memory jobs)
_KEY_ONBOARDING_NUDGE = "__cv_onb_nudge__"
_KEY_DAY2_SENT        = "__cv_day2__"
_KEY_LASTDAY_SENT     = "__cv_lastday__"

# ── Утечка 1: онбординг без триала ────────────────────────────────────────────

async def _send_onboarding_nudge(bot, user_id: int) -> None:
    """Отправляет нудж пользователям которые прошли онбординг но не взяли триал."""
    from user_state import get_user_state, has_access
    from db import get_profile, kv_set

    try:
        state = await get_user_state(user_id)
        if has_access(state):
            return  # уже активировал

        profile = await get_profile(user_id)
        niche   = profile.get("niche", "твоей теме")

        from niche_intel import get_niche_intel
        intel   = get_niche_intel(niche)
        works   = intel.get("works", "")
        example = works.split("\n")[1].strip("• ").split("\n")[0] if works else "создания контента"

        text = (
            f"Кстати.\n\n"
            f"Для твоей ниши «{niche[:30]}» лучше всего работает: {example.lower()}\n\n"
            f"Хочешь посмотреть как Мира напишет это конкретно под твою аудиторию?\n\n"
            f"*3 дня бесплатно — без карты.*"
        )

        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎁 Да, показывай", callback_data="sub_trial"),
        ]])

        await bot.send_message(chat_id=user_id, text=text,
                               parse_mode="Markdown", reply_markup=kb)
        # Ставим флаг что отправили — больше не трогаем
        await kv_set(user_id, _KEY_ONBOARDING_NUDGE, "sent", ttl=86400 * 7)
        from flows.utm import track_event
        await track_event(user_id, "onboarding_nudge_sent")
        logger.info(f"Onboarding nudge sent: user={user_id}")
    except Exception as e:
        logger.warning(f"_send_onboarding_nudge failed uid={user_id}: {e}")


async def schedule_onboarding_nudge(app, user_id: int) -> None:
    """
    Регистрирует намерение отправить нудж.
    Фактическая отправка — через run_hourly_conversion() который видит флаг.
    Переживает рестарты: флаг в Redis, не в памяти.
    """
    from db import kv_get, kv_set
    try:
        existing = await kv_get(user_id, _KEY_ONBOARDING_NUDGE)
        if existing:
            return
        # Флаг "pending" с временем онбординга — job прочитает и решит когда слать
        now_ts = int(datetime.now(timezone.utc).timestamp())
        await kv_set(user_id, _KEY_ONBOARDING_NUDGE, f"pending:{now_ts}", ttl=86400)
        logger.info(f"Onboarding nudge flagged: user={user_id}")
    except Exception as e:
        logger.warning(f"schedule_onboarding_nudge: {e}")


# ── Утечка 2: триал день 2 — не вернулись ─────────────────────────────────────

async def _send_day2_nudge(bot, user_id: int) -> None:
    """Нудж для триальщиков которые не вернулись на день 2."""
    from user_state import get_user_state, UserState
    from db import get_profile, get_results, kv_set

    try:
        state = await get_user_state(user_id)
        if state == UserState.SUBSCRIBED:
            return  # уже купил

        profile = await get_profile(user_id)
        results = await get_results(user_id, limit=3)
        niche   = profile.get("niche", "")

        used_agents = {r["agent_key"] for r in results}
        untried     = _get_untried_recommendation(niche, used_agents)

        if results:
            created_count = len(results)
            noun = "материал" if created_count == 1 else "материала"
            text = (
                f"Вчера создала {created_count} {noun}.\n\n"
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

        await bot.send_message(chat_id=user_id, text=text,
                               parse_mode="Markdown", reply_markup=kb)
        await kv_set(user_id, _KEY_DAY2_SENT, "sent", ttl=86400 * 4)
        from flows.utm import track_event
        await track_event(user_id, "day2_nudge_sent")
        logger.info(f"Day-2 nudge sent: user={user_id}")
    except Exception as e:
        logger.warning(f"_send_day2_nudge failed uid={user_id}: {e}")


async def schedule_day2_nudge(app, user_id: int) -> None:
    """
    Регистрирует намерение отправить Day-2 нудж.
    Фактическая отправка — через run_hourly_conversion() через 20ч.
    """
    from db import kv_get, kv_set
    try:
        existing = await kv_get(user_id, _KEY_DAY2_SENT)
        if existing:
            return
        now_ts = int(datetime.now(timezone.utc).timestamp())
        await kv_set(user_id, _KEY_DAY2_SENT, f"pending:{now_ts}", ttl=86400 * 4)
        logger.info(f"Day-2 nudge flagged: user={user_id}")
    except Exception as e:
        logger.warning(f"schedule_day2_nudge: {e}")


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

    if not candidates:
        return {
            "name": "Разбор профиля",
            "pitch": "Честный аудит аккаунта — где теряются клиенты.",
            "cb":    "agent_start_profile",
        }

    # Приоритет: запуск/курс → прогрев
    if any(kw in niche_l for kw in ["запуск", "курс", "продукт", "коуч", "тренер"]):
        warmup = next((c for c in candidates if c["cb"] == "agent_start_warmup"), None)
        if warmup:
            return warmup

    return candidates[0]


# ── Утечка 3: последний день триала ───────────────────────────────────────────

async def schedule_lastday_nudge(app, user_id: int, expires_at: str) -> None:
    """
    Регистрирует намерение отправить нудж за 6ч до истечения триала.
    run_trial_warnings() в retention.py уже обрабатывает это — здесь
    только ставим флаг чтобы не было дублирования.
    """
    from db import kv_get, kv_set
    try:
        existing = await kv_get(user_id, _KEY_LASTDAY_SENT)
        if existing:
            return
        await kv_set(user_id, _KEY_LASTDAY_SENT, f"pending:{expires_at}", ttl=86400 * 5)
    except Exception as e:
        logger.warning(f"schedule_lastday_nudge: {e}")


# ── APScheduler job: каждый час проверяем кого пора нуджить ───────────────────

async def run_hourly_conversion(bot) -> None:
    """
    Запускается каждый час из main.py (APScheduler).
    Проверяет Redis-флаги и отправляет нуджи когда пришло время.
    Переживает рестарты — флаги в Redis, не в памяти.
    """
    from db import get_redis
    logger.info("[conversion] hourly run started")
    sent = 0

    try:
        r = await get_redis()
        now_ts = int(datetime.now(timezone.utc).timestamp())

        # Сканируем pending флаги онбординга (ждём 15 минут)
        async for key in r.scan_iter("bot:kv:*:__cv_onb_nudge__"):
            try:
                val = await r.get(key)
                if not val or not val.startswith("pending:"):
                    continue
                flagged_ts = int(val.split(":")[1])
                if now_ts - flagged_ts < 15 * 60:
                    continue  # ещё не прошло 15 минут
                # Извлекаем user_id из ключа: bot:kv:{user_id}:__cv_onb_nudge__
                parts = key.split(":")
                if len(parts) < 3:
                    continue
                uid = int(parts[2])
                await _send_onboarding_nudge(bot, uid)
                sent += 1
            except Exception as e:
                logger.warning(f"[conversion] onb nudge key error: {e}")

        # Сканируем pending флаги Day-2 (ждём 20 часов)
        async for key in r.scan_iter("bot:kv:*:__cv_day2__"):
            try:
                val = await r.get(key)
                if not val or not val.startswith("pending:"):
                    continue
                flagged_ts = int(val.split(":")[1])
                if now_ts - flagged_ts < 20 * 3600:
                    continue  # ещё не прошло 20 часов
                parts = key.split(":")
                if len(parts) < 3:
                    continue
                uid = int(parts[2])
                await _send_day2_nudge(bot, uid)
                sent += 1
            except Exception as e:
                logger.warning(f"[conversion] day2 nudge key error: {e}")

    except Exception as e:
        logger.error(f"[conversion] hourly run error: {e}")

    logger.info(f"[conversion] hourly run done, sent={sent}")


# ── Хелпер: вызывается из grant_trial() ───────────────────────────────────────

async def on_trial_activated(app, user_id: int, expires_at: str) -> None:
    """
    Единая точка входа при активации триала.
    Регистрирует конверсионные нуджи (переживают рестарты).
    """
    await schedule_day2_nudge(app, user_id)
    await schedule_lastday_nudge(app, user_id, expires_at)
    from flows.utm import track_event
    await track_event(user_id, "trial_activated")
