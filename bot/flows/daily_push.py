"""
flows/daily_push.py — утренние пуши в стиле Persona.

Что делает:
  • Каждое утро присылает идею дня + формат + задачу — персонализированно
    под нишу, аудиторию и тон из профиля пользователя.
  • Доступно ВСЕМ: TRIAL, SUBSCRIBED, ONBOARDED — чтобы даже пробные
    пользователи чувствовали ценность продукта.
  • Генерирует «крючок» в дерзком тоне (как Persona) — пост-провокация
    которая вызывает реакцию и комментарии.
  • Batch-пре-генерация в 06:00 UTC → к моменту доставки уже готово.

Архитектура:
  1. setup_daily_push_jobs(app) — вызывается из main.py post_init.
  2. _batch_pregen_job — в 06:00 UTC генерирует для всех включённых юзеров.
  3. schedule_daily_push(app, user_id, hour, minute) — планирует или
     обновляет задание для конкретного пользователя.
  4. daily_push_send_now(ctx, user_id, bot) — отправляет пуш (кэш или live).

Интеграция в handlers:
  - В daily_menu добавить кнопки «✅ Включить пуш|daily_push_on»,
    «❌ Выключить|daily_push_off», «⏰ Изменить время|daily_push_time».
  - В callback handler добавить ветки daily_push_*.
  - В cmd_start / cmd_daily показывать статус.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import json

logger = logging.getLogger(__name__)

# TTL кэша дайджеста — 20 часов (переживает утреннюю доставку)
_DIGEST_CACHE_TTL = 72_000
_CACHE_KEY = "__daily_push_cache__"
_SETTINGS_KEY = "__daily_push__"

# ─────────────────────────────────────────────────────────────────────────────
# Промпт — стиль Persona: бодрый, провокационный, с конкретной задачей
# ─────────────────────────────────────────────────────────────────────────────

DAILY_PUSH_SYSTEM = """Ты — жёсткий, честный контент-советник. Присылаешь короткое утреннее сообщение эксперту.

НЕ мотивационный спич. НЕ «всё получится». Конкретная польза — что писать СЕГОДНЯ и почему.

СТРУКТУРА (строго, без отступлений):

☀️ Доброе утро, [обращение из ниши — «психолог», «маркетолог», «коуч» и т.п.]!

📅 Сегодня [дата, день недели]

💡 ИДЕЯ ДНЯ:
"[Провокационный заголовок в кавычках — конкретный, немного дерзкий]"
[1–2 предложения: угол подачи + почему цепляет именно эту аудиторию]

🎯 ФОРМАТ ДНЯ:
[Пост-провокация / Рилс / Карусель / Сторис] — [1 предложение почему этот формат сегодня]
[1 предложение про реакцию аудитории — комментарии, сохранения, охват]

✅ ЗАДАЧА:
[Одно конкретное действие прямо сейчас — написать первые 2 предложения / записать голосовое / набросать структуру]
[Одна короткая подбадривающая фраза — дерзкая, не приторная]

Дерзай! 🔥

ТОНАЛЬНОСТЬ: женщина 25–45, умная, уставшая от шаблонного контента. Говори как умный друг-маркетолог, не как коуч из инстаграма 2018 года.
ДЛИНА: 150–200 слов. Не больше. Каждое слово работает.
ЯЗЫК: русский."""

# ─────────────────────────────────────────────────────────────────────────────
# Helpers: кэш
# ─────────────────────────────────────────────────────────────────────────────

async def _cache_push(user_id: int, text: str) -> None:
    from db import kv_set
    try:
        await kv_set(user_id, _CACHE_KEY, text, ttl=_DIGEST_CACHE_TTL)
    except Exception as e:
        logger.warning(f"[daily_push] cache write failed uid={user_id}: {e}")


async def _get_cached_push(user_id: int) -> str | None:
    from db import kv_get
    try:
        return await kv_get(user_id, _CACHE_KEY)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Настройки пуша — отдельно от daily_settings чтобы не ломать существующий flow
# ─────────────────────────────────────────────────────────────────────────────

async def get_push_settings(user_id: int) -> dict:
    """Возвращает настройки daily push. По умолчанию выключен."""
    from db import kv_get
    try:
        raw = await kv_get(user_id, _SETTINGS_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {"enabled": False, "hour": 9, "minute": 0, "hour_local": 9}


async def save_push_settings(user_id: int, settings: dict) -> None:
    from db import kv_set
    await kv_set(user_id, _SETTINGS_KEY, json.dumps(settings, ensure_ascii=False))


async def get_all_push_users() -> list[tuple[int, dict]]:
    """
    Возвращает список (user_id, settings) для всех включённых пушей.
    Используется при восстановлении заданий после рестарта.
    """
    from db import get_redis
    r = await get_redis()
    result = []
    async for key in r.scan_iter(f"bot:kv:*:{_SETTINGS_KEY}"):
        try:
            raw = await r.get(key)
            if not raw:
                continue
            s = json.loads(raw)
            if s.get("enabled"):
                parts = key.split(":")
                user_id = int(parts[2])
                result.append((user_id, s))
        except Exception as e:
            logger.warning(f"[daily_push] get_all_push_users key={key}: {e}")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Генерация
# ─────────────────────────────────────────────────────────────────────────────

def _build_push_prompt(profile: dict, name: str) -> str:
    today = datetime.date.today()
    weekdays_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months_ru = [
        "", "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря"
    ]
    date_str = f"{today.day} {months_ru[today.month]}, {weekdays_ru[today.weekday()]}"

    name_line = f"Имя пользователя: {name}\n" if name else ""
    return (
        f"{name_line}"
        f"Ниша: {profile.get('niche', 'эксперт')}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\n"
        f"Тон: {profile.get('tone', 'дружелюбный, экспертный')}\n"
        f"Дата: {date_str}"
    )


async def pregen_push(user_id: int) -> str | None:
    """
    Генерирует пуш и кладёт в кэш Redis.
    Вызывается из batch-задания в 06:00 UTC.
    """
    from db import get_profile, get_user_name
    from llm import complete

    try:
        profile = await get_profile(user_id)
        name = await get_user_name(user_id)
        prompt = _build_push_prompt(profile, name)
        result = await complete(DAILY_PUSH_SYSTEM, prompt, temperature=0.9)
        if result and result.strip():
            await _cache_push(user_id, result)
            return result
    except Exception as e:
        logger.error(f"[daily_push] pregen failed uid={user_id}: {e}")
    return None


async def daily_push_send_now(ctx, user_id: int, bot) -> None:
    """
    Отправляет утренний пуш пользователю.
    Сначала проверяет кэш (батч 06:00 UTC), при промахе — генерирует на лету.

    Только для TRIAL и SUBSCRIBED — не тратим LLM на пользователей без доступа.
    """
    from user_state import get_user_state, has_access
    state = await get_user_state(user_id)
    if not has_access(state):
        logger.info(f"[daily_push] skip uid={user_id} — no active access (state={state.value})")
        return

    result = await _get_cached_push(user_id)

    if not result:
        logger.info(f"[daily_push] cache miss uid={user_id}, generating on-demand")
        result = await pregen_push(user_id)

    if not result:
        logger.error(f"[daily_push] no content for uid={user_id}")
        return

    try:
        await bot.send_message(
            chat_id=user_id,
            text=result,
            parse_mode="Markdown",
        )
        logger.info(f"[daily_push] sent to uid={user_id}")
    except Exception as e:
        logger.error(f"[daily_push] send error uid={user_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Планировщик заданий
# ─────────────────────────────────────────────────────────────────────────────

async def _daily_push_job(ctx) -> None:
    user_id = ctx.job.data
    await daily_push_send_now(ctx, user_id, ctx.bot)


async def schedule_daily_push(app, user_id: int, hour: int, minute: int) -> None:
    """
    Планирует или перепланирует ежедневный пуш для пользователя.
    hour/minute — UTC.
    Jitter ±5 мин чтобы размазать пики LLM при массовой доставке.
    """
    job_name = f"daily_push_{user_id}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    jitter_sec = (user_id % 601) - 300   # ±300 сек = ±5 мин
    base_sec = hour * 3600 + minute * 60 + jitter_sec
    base_sec = base_sec % 86400
    j_hour, rem = divmod(base_sec, 3600)
    j_minute, j_sec = divmod(rem, 60)

    app.job_queue.run_daily(
        callback=_daily_push_job,
        time=datetime.time(
            hour=j_hour, minute=j_minute, second=j_sec,
            tzinfo=datetime.timezone.utc,
        ),
        name=job_name,
        data=user_id,
    )
    logger.info(
        f"[daily_push] scheduled uid={user_id} "
        f"at {j_hour:02d}:{j_minute:02d}:{j_sec:02d} UTC (jitter={jitter_sec:+d}s)"
    )


async def _batch_pregen_job(ctx) -> None:
    """
    Запускается в 06:00 UTC, генерирует пуши для всех включённых пользователей.
    К моменту доставки (обычно 07:00–09:00) уже в кэше.
    """
    users = await get_all_push_users()
    logger.info(f"[daily_push] batch pregen for {len(users)} users")

    # Семафор: не более 5 параллельных LLM-запросов
    sem = asyncio.Semaphore(5)

    async def _gen_one(uid: int) -> None:
        async with sem:
            try:
                await pregen_push(uid)
                await asyncio.sleep(0.2)   # небольшой throttle
            except Exception as e:
                logger.warning(f"[daily_push] pregen_one uid={uid}: {e}")

    await asyncio.gather(*[_gen_one(uid) for uid, _ in users])
    logger.info("[daily_push] batch pregen done")


def setup_daily_push_jobs(app) -> None:
    """
    Вызывается из post_init в main.py.
    Восстанавливает задания для всех пользователей + регистрирует batch pregen.
    """
    import asyncio as _aio

    # Batch pregen в 06:00 UTC каждый день
    app.job_queue.run_daily(
        callback=_batch_pregen_job,
        time=datetime.time(hour=6, minute=0, tzinfo=datetime.timezone.utc),
        name="daily_push_batch_pregen",
    )
    logger.info("[daily_push] batch pregen job registered at 06:00 UTC")


async def restore_daily_push_jobs(app) -> None:
    """
    Асинхронная версия восстановления — вызывается из post_init.
    Отдельно от setup_daily_push_jobs чтобы не блокировать синхронный вызов.
    """
    users = await get_all_push_users()
    for uid, settings in users:
        try:
            await schedule_daily_push(
                app, uid,
                settings.get("hour", 9),
                settings.get("minute", 0),
            )
        except Exception as e:
            logger.warning(f"[daily_push] restore failed uid={uid}: {e}")
    logger.info(f"[daily_push] restored {len(users)} jobs")


# ─────────────────────────────────────────────────────────────────────────────
# UI helpers — меню настройки пуша
# ─────────────────────────────────────────────────────────────────────────────

async def push_menu(update, user_id: int) -> None:
    """Показывает меню настройки утренних пушей."""
    from utils import send, kb

    settings = await get_push_settings(user_id)
    enabled = settings.get("enabled", False)
    status_icon = "✅" if enabled else "❌"
    hour_display = settings.get("hour_local", settings.get("hour", 9))
    minute_display = settings.get("minute", 0)

    text = (
        f"☀️ *Утренние пуши*\n\n"
        f"Статус: {status_icon} {'Включены' if enabled else 'Выключены'}\n"
        f"Время: {hour_display:02d}:{minute_display:02d}\n\n"
        "_Каждое утро — идея дня + формат + конкретная задача. "
        "Персонально под твою нишу и аудиторию._\n\n"
        "_Работает даже в пробном периоде — хочим чтобы ты почувствовал ценность_ 👇"
    )

    toggle = "❌ Выключить|daily_push_off" if enabled else "✅ Включить|daily_push_on"
    await send(update, text, parse_mode="Markdown",
               reply_markup=kb(
                   [toggle],
                   ["⏰ Изменить время|daily_push_time"],
                   ["🔔 Тест — прислать сейчас|daily_push_test"],
                   ["← Меню|menu_main"],
               ))


# ─────────────────────────────────────────────────────────────────────────────
# Callback handlers — добавить в _callback_inner в handlers/callbacks.py
# ─────────────────────────────────────────────────────────────────────────────
#
# elif data == "daily_push_menu":
#     from flows.daily_push import push_menu
#     await push_menu(update, user_id)
#
# elif data == "daily_push_on":
#     from flows.daily_push import (
#         get_push_settings, save_push_settings, schedule_daily_push, push_menu
#     )
#     s = await get_push_settings(user_id)
#     s["enabled"] = True
#     await save_push_settings(user_id, s)
#     await schedule_daily_push(ctx.application, user_id, s.get("hour", 9), s.get("minute", 0))
#     await push_menu(update, user_id)
#
# elif data == "daily_push_off":
#     from flows.daily_push import get_push_settings, save_push_settings, push_menu
#     s = await get_push_settings(user_id)
#     s["enabled"] = False
#     await save_push_settings(user_id, s)
#     jobs = ctx.application.job_queue.get_jobs_by_name(f"daily_push_{user_id}")
#     for job in jobs: job.schedule_removal()
#     await push_menu(update, user_id)
#
# elif data == "daily_push_time":
#     from db import save_agent_session
#     await save_agent_session(user_id, "daily_push_time_flow", {"step": "await_time"})
#     await send(update,
#                "⏰ Напиши время для утреннего пуша:\n_Например: 9:00 или 8:30_",
#                parse_mode="Markdown", reply_markup=kb(["← Назад|daily_push_menu"]))
#
# elif data == "daily_push_test":
#     from flows.daily_push import daily_push_send_now
#     await daily_push_send_now(ctx, user_id, ctx.bot)
#
# ─────────────────────────────────────────────────────────────────────────────
# Обработка ввода времени — добавить в _route_inner в handlers/messages.py
# (до блока "Активный кастомный flow")
# ─────────────────────────────────────────────────────────────────────────────
#
#     # Daily push — установка времени
#     push_time_s = await get_agent_session(user_id, "daily_push_time_flow")
#     if push_time_s and push_time_s.get("step") == "await_time":
#         import re
#         m = re.match(r"(\d{1,2}):(\d{2})", text.strip())
#         if m:
#             hour_local, minute = int(m.group(1)), int(m.group(2))
#             tz_offset = 2  # UTC+2 (Spain/Croatia/Serbia)
#             hour_utc = (hour_local - tz_offset) % 24
#             from flows.daily_push import get_push_settings, save_push_settings, schedule_daily_push
#             s = await get_push_settings(user_id)
#             s["hour"] = hour_utc
#             s["minute"] = minute
#             s["hour_local"] = hour_local
#             await save_push_settings(user_id, s)
#             await clear_agent_session(user_id, "daily_push_time_flow")
#             if s.get("enabled"):
#                 await schedule_daily_push(ctx.application, user_id, hour_utc, minute)
#             await send(update,
#                        f"✅ Время установлено: *{hour_local:02d}:{minute:02d}*",
#                        parse_mode="Markdown",
#                        reply_markup=kb(["☀️ Настройки пушей|daily_push_menu", "← Меню|menu_main"]))
#         else:
#             await send(update, "Не понял формат. Напиши например: `9:00` или `08:30`",
#                        parse_mode="Markdown")
#         return
