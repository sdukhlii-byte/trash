"""
flows/misc.py — вспомогательные flows: идеи, доработка, планировщик, дейли, стиль.

Изменения v2:
- _schedule_daily: jitter расширен с ±30 сек до ±5 мин.
  При 1000 юзерах с одинаковым временем это даёт ~1.7 LLM-вызова/сек вместо пика 8/сек.
- Статусные сообщения с голосом Миры.
- Ошибки рефайна/регена — с контекстом.
"""
import asyncio
import datetime
import logging
import random

from telegram import Update

from db import (
    get_agent_session, save_agent_session, clear_agent_session,
    get_profile, save_result, get_results, get_result_by_id,
    get_stats, get_schedule, save_schedule, add_to_schedule, mark_done,
    get_daily_settings, save_daily_settings, get_style_examples,
    add_style_example, clear_style_examples,
    kv_get, kv_set,
)
from llm import complete
from security import protect
from prompt_editor import get_prompt
from utils import send, kb, safe_delete
from config import (
    QUICK_IDEAS_SYSTEM, REFINE_SYSTEM, REGEN_SYSTEM,
    PLANNER_IDEAS_SYSTEM, DAILY_DIGEST_SYSTEM,
)

logger = logging.getLogger(__name__)

# ── Keys ──────────────────────────────────────────────────────────────────────
_QI_KEY      = "quick_ideas_flow"
_REFINE_KEY  = "refine_flow"
_PLANNER_KEY = "planner_flow"
_STYLE_KEY   = "style_flow"

# ══════════════════════════════════════════════════════════════════════════════
# БЫСТРЫЕ ИДЕИ
# ══════════════════════════════════════════════════════════════════════════════

async def qi_start(update: Update, user_id: int) -> None:
    await clear_agent_session(user_id, _QI_KEY)
    await save_agent_session(user_id, _QI_KEY, {"step": "await_niche"})
    profile = await get_profile(user_id)
    niche = profile.get("niche", "")
    if niche:
        await qi_generate(update, user_id, niche)
    else:
        caption = "🧠 *Мозговой штурм*\n\nДля какой ниши генерировать идеи?\n_Можешь написать любую тему_"
        await send(update, caption, parse_mode="Markdown", reply_markup=kb(["← Меню|menu_main"]))


async def qi_generate(update: Update, user_id: int, niche: str) -> None:
    profile = await get_profile(user_id)
    prompt = (
        f"Ниша: {niche}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\n"
        f"Тон: {profile.get('tone', 'живой')}\n\n"
        "Дай 10 конкретных идей для постов. Нумерованный список, без вступлений."
    )
    status = await update.effective_chat.send_message("Генерирую 10 идей под твою нишу...")
    try:
        qi_sys = protect(user_id, await get_prompt(user_id, "quick_ideas", QUICK_IDEAS_SYSTEM))
        result = await complete(qi_sys, prompt, temperature=0.85)
    except Exception as e:
        logger.error(f"[qi] generate error: {e}")
        result = "Что-то сломалось — нажми ещё раз 🔁"
    await safe_delete(status)
    await clear_agent_session(user_id, _QI_KEY)
    try:
        await save_result(user_id, "quick_ideas", "10 идей быстро", result)
    except Exception as e:
        logger.warning(f"save_result qi failed: {e}")
    await send(
        update,
        f"💡 *10 идей для постов:*\n\n{result}",
        parse_mode="Markdown",
        reply_markup=kb(["🔄 Ещё 10 идей|quick_ideas", "← Меню|menu_main"]),
    )
    # Voice feedback — узнаём звучит ли как автор
    try:
        import asyncio
        from db import get_results as _gr
        from voice_learner import voice_feedback_kb
        _recent = await _gr(user_id, limit=1)
        if _recent:
            await asyncio.sleep(0.5)
            await send(update, "Звучит как твой голос?",
                       reply_markup=voice_feedback_kb(_recent[0]["id"]))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# ДОРАБОТКА И ДРУГОЙ ВАРИАНТ
# ══════════════════════════════════════════════════════════════════════════════

async def refine_start(update: Update, user_id: int, result_id: int = 0) -> None:
    if result_id:
        r = await get_result_by_id(user_id, result_id)
    else:
        results = await get_results(user_id, limit=1)
        r = results[0] if results else None
    if not r:
        await send(update, "Нет материалов для доработки. Сначала создай что-нибудь.",
                   reply_markup=kb(["← Меню|menu_main"]))
        return
    await save_agent_session(user_id, _REFINE_KEY, {
        "step":       "await_instruction",
        "result_id":  r["id"],
        "original":   r["content"],
        "agent_name": r["agent_name"],
    })
    preview = r["content"][:300]
    await send(
        update,
        f"✏️ *Доработка*\n\n*{r['agent_name']}:*\n_{preview}..._\n\n"
        "Что изменить? Напиши инструкцию:\n"
        "_«Сделай короче», «Добавь юмора», «Усиль CTA»..._",
        parse_mode="Markdown",
        reply_markup=kb(["← Меню|menu_main"]),
    )


async def refine_do(update: Update, user_id: int, instruction: str, s: dict) -> None:
    original = s.get("original", "")
    prompt = f"Оригинальный текст:\n{original}\n\nЗадача: {instruction}"
    status = await update.effective_chat.send_message("Дорабатываю — держи...")
    try:
        refine_sys = protect(user_id, await get_prompt(user_id, "refine", REFINE_SYSTEM))
        result = await complete(refine_sys, prompt, temperature=0.7)
    except Exception as e:
        logger.error(f"[refine] error: {e}")
        result = None
    await safe_delete(status)
    await clear_agent_session(user_id, _REFINE_KEY)
    if not result:
        await send(update, "Что-то сломалось — нажми ещё раз 🔁",
                   reply_markup=kb(["✏️ Повторить|refine_last", "← Меню|menu_main"]))
        return
    try:
        await save_result(user_id, "refine", s.get("agent_name", "Доработка"), result)
    except Exception:
        pass
    await send(
        update,
        f"✏️ *Доработано:*\n\n{result}",
        parse_mode="Markdown",
        reply_markup=kb(
            ["✏️ Ещё раз доработать|refine_last", "🔄 Другой вариант|regen_last"],
            ["← Меню|menu_main"],
        ),
    )
    try:
        import asyncio
        from db import get_results as _gr
        from voice_learner import voice_feedback_kb
        _recent = await _gr(user_id, limit=1)
        if _recent:
            await asyncio.sleep(0.5)
            await send(update, "Звучит как твой голос?",
                       reply_markup=voice_feedback_kb(_recent[0]["id"]))
    except Exception:
        pass


async def regen_last(update: Update, user_id: int) -> None:
    results = await get_results(user_id, limit=1)
    if not results:
        await send(update, "Нет материалов. Создай что-нибудь сначала.",
                   reply_markup=kb(["← Меню|menu_main"]))
        return
    await regen_by_id(update, user_id, results[0]["id"])


async def regen_by_id(update: Update, user_id: int, result_id: int) -> None:
    r = await get_result_by_id(user_id, result_id)
    if not r:
        await send(update, "Материал не найден.", reply_markup=kb(["← Меню|menu_main"]))
        return
    prompt = f"Оригинал:\n{r['content']}"
    status = await update.effective_chat.send_message("Пишу другой вариант — ищу другой угол...")
    try:
        regen_sys = protect(user_id, await get_prompt(user_id, "regen", REGEN_SYSTEM))
        result = await complete(regen_sys, prompt, temperature=0.9, presence_penalty=0.4)
    except Exception as e:
        logger.error(f"[regen] error: {e}")
        result = None
    await safe_delete(status)
    if not result:
        await send(update, "Что-то сломалось — нажми ещё раз 🔁",
                   reply_markup=kb(["🔄 Ещё раз|regen_last", "← Меню|menu_main"]))
        return
    try:
        await save_result(user_id, r["agent_key"], r["agent_name"], result)
    except Exception:
        pass
    await send(
        update,
        f"🔄 *Другой вариант:*\n\n{result}",
        parse_mode="Markdown",
        reply_markup=kb(
            ["✏️ Доработать|refine_last", "🔄 Ещё вариант|regen_last"],
            ["← Меню|menu_main"],
        ),
    )
    try:
        import asyncio
        from db import get_results as _gr
        from voice_learner import voice_feedback_kb
        _recent = await _gr(user_id, limit=1)
        if _recent:
            await asyncio.sleep(0.5)
            await send(update, "Звучит как твой голос?",
                       reply_markup=voice_feedback_kb(_recent[0]["id"]))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# БИБЛИОТЕКА КОНТЕНТА
# ══════════════════════════════════════════════════════════════════════════════

async def show_results(update: Update, user_id: int, page: int = 0) -> None:
    from db import delete_result
    results = await get_results(user_id, limit=50)
    if not results:
        await send(
            update,
            "📚 *Мои материалы*\n\n_Ещё ничего нет. Создай что-нибудь с помощью агентов!_",
            parse_mode="Markdown",
            reply_markup=kb(["← Меню|menu_main"]),
        )
        return

    PAGE_SIZE = 5
    total_pages = (len(results) - 1) // PAGE_SIZE + 1
    page = max(0, min(page, total_pages - 1))
    items = results[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]

    lines = [f"📚 *Мои материалы* (стр. {page + 1}/{total_pages})\n"]
    open_buttons = []
    for r in items:
        preview = r["content"][:100].replace("\n", " ")
        ts = r["ts"][:10] if r["ts"] else ""
        lines.append(f"*{r['id']}. {r['agent_name']}* [{ts}]\n_{preview}..._\n")
        open_buttons.append(f"📖 №{r['id']} {r['agent_name'][:20]}|result_open_{r['id']}")

    nav = []
    if page > 0:
        nav.append(f"◀️ Назад|results_page_{page - 1}")
    if page < total_pages - 1:
        nav.append(f"Вперёд ▶️|results_page_{page + 1}")

    rows = [[btn] for btn in open_buttons]
    if nav:
        rows.append(nav)
    rows.append(["← Меню|menu_main"])
    await send(update, "\n".join(lines), parse_mode="Markdown", reply_markup=kb(*rows))


async def show_result_full(update: Update, user_id: int, result_id: int) -> None:
    r = await get_result_by_id(user_id, result_id)
    if not r:
        await send(update, "Материал не найден.", reply_markup=kb(["← Мои материалы|my_results"]))
        return
    ts = r["ts"][:10] if r["ts"] else ""
    full = f"📖 *{r['agent_name']}* [{ts}]\n\n{r['content']}"
    CHUNK = 3800
    action_kb = kb(
        [f"✏️ Доработать|refine_id_{result_id}", f"🔄 Другой вариант|regen_id_{result_id}"],
        [f"🗑 Удалить|result_del_{result_id}", "← Мои материалы|my_results"],
    )
    if len(full) <= CHUNK:
        await send(update, full, parse_mode="Markdown", reply_markup=action_kb)
    else:
        preview = full[:CHUNK].rsplit("\n", 1)[0]
        await send(update, preview + "\n\n_...продолжение ниже_", parse_mode="Markdown")
        rest = full[len(preview):]
        for chunk in [rest[i:i + 4000] for i in range(0, len(rest), 4000)]:
            await asyncio.sleep(0.3)
            await send(update, chunk, parse_mode="Markdown")
        await send(update, "—", reply_markup=action_kb)


async def show_stats(update: Update, user_id: int) -> None:
    stats = await get_stats(user_id)
    total = stats["total"]

    if total == 0:
        text = "📊 *Твой прогресс*\n\n_Пока ничего не создано. Запусти любого агента!_"
    else:
        from ui.home import _get_streak
        from voice_learner import get_voice_stats
        streak     = await _get_streak(user_id)
        voice_s    = await get_voice_stats(user_id)
        voice_sig  = voice_s.get("total_signals", 0)

        lines = [f"📊 *Твой прогресс*\n"]

        if streak >= 2:
            lines.append(f"🔥 Стрик: *{streak} дней подряд*")

        lines.append(f"✅ Создано материалов: *{total}*")

        # Voice calibration
        if voice_sig == 0:
            lines.append("🎤 Голос Миры: _не настроен_ — после генерации появится кнопка оценки")
        elif voice_sig < 5:
            bar = "▓" * voice_sig + "░" * (5 - voice_sig)
            lines.append(f"🎤 Голос Миры: [{bar}] {voice_sig}/5")
        else:
            lines.append(f"🎤 Голос Миры: *точный* ({voice_sig} сигналов) 🎯")

        if stats["by_agent"]:
            lines.append("\n*По инструментам:*")
            for agent_name, count in stats["by_agent"][:5]:
                lines.append(f"  · {agent_name}: {count}")

        text = "\n".join(lines)

    await send(update, text, parse_mode="Markdown",
               reply_markup=kb(
                   ["📚 Мои материалы|my_results"],
                   ["🎤 Настроить голос|style_menu", "← Меню|menu_main"],
               ))


# ══════════════════════════════════════════════════════════════════════════════
# ПЛАНИРОВЩИК
# ══════════════════════════════════════════════════════════════════════════════

async def planner_show(update: Update, user_id: int) -> None:
    schedule = await get_schedule(user_id)
    pending = [s for s in schedule if not s.get("done")]
    done    = [s for s in schedule if s.get("done")]

    if not pending and not done:
        text = (
            "📅 *Планировщик публикаций*\n\n"
            "_Расписание пустое._\n\nДобавь пост вручную или сгенерируй план на неделю 👇"
        )
    else:
        lines = ["📅 *Планировщик публикаций*\n"]
        if pending:
            lines.append("*Запланировано:*")
            for i, item in enumerate(schedule):
                if not item.get("done"):
                    lines.append(
                        f"  {i + 1}. {item['date']} [{item.get('platform', '—')}]\n"
                        f"     _{item['idea']}_"
                    )
        if done:
            lines.append(f"\n✅ Выполнено: {len(done)}")
        text = "\n".join(lines)

    await send(update, text, parse_mode="Markdown",
               reply_markup=kb(
                   ["📝 Добавить пост|planner_add",   "🗓 План на неделю|planner_week"],
                   ["✅ Отметить выполненным|planner_done", "🗑 Очистить|planner_clear"],
                   ["← Меню|menu_main"],
               ))


async def planner_gen_week(update: Update, user_id: int) -> None:
    profile = await get_profile(user_id)
    today = datetime.date.today()
    dates = [(today + datetime.timedelta(days=i)).strftime("%d.%m (%a)") for i in range(7)]
    prompt = (
        f"Ниша: {profile.get('niche', 'не указана')}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\n\n"
        f"Даты: {', '.join(dates)}\n\nСоставь план на 7 дней."
    )
    status = await update.effective_chat.send_message("Составляю план на неделю...")
    try:
        result = await complete(protect(user_id, PLANNER_IDEAS_SYSTEM), prompt, temperature=0.85)
    except Exception as e:
        logger.error(f"[planner] week error: {e}")
        result = None
    await safe_delete(status)
    if not result:
        await send(update, "Что-то сломалось — нажми ещё раз 🔁",
                   reply_markup=kb(["← Планировщик|planner_show"]))
        return
    await send(update, f"📅 *План на неделю:*\n\n{result}", parse_mode="Markdown",
               reply_markup=kb(["📅 Мой планировщик|planner_show", "← Меню|menu_main"]))


async def planner_add_start(update: Update, user_id: int) -> None:
    await save_agent_session(user_id, _PLANNER_KEY, {"step": "await_date"})
    await send(
        update,
        "📝 *Добавить пост в расписание*\n\nНапиши дату (например: `25.05` или `завтра`):",
        parse_mode="Markdown",
        reply_markup=kb(["← Планировщик|planner_show"]),
    )


async def route_planner_text(update: Update, user_id: int, text: str, s: dict) -> bool:
    step = s.get("step", "")
    if step == "await_date":
        s["date"] = text.strip()
        s["step"] = "await_platform"
        await save_agent_session(user_id, _PLANNER_KEY, s)
        await send(update, "Платформа? _Instagram / Telegram / Reels / Сторис_",
                   parse_mode="Markdown", reply_markup=kb(["← Планировщик|planner_show"]))
        return True
    elif step == "await_platform":
        s["platform"] = text.strip()
        s["step"] = "await_idea"
        await save_agent_session(user_id, _PLANNER_KEY, s)
        await send(update, "Тема или идея поста (коротко):",
                   reply_markup=kb(["← Планировщик|planner_show"]))
        return True
    elif step == "await_idea":
        await add_to_schedule(user_id, s.get("date", "?"), s.get("platform", "?"), text.strip())
        await clear_agent_session(user_id, _PLANNER_KEY)
        await send(update, "✅ Добавлено в расписание!",
                   reply_markup=kb(["📅 Планировщик|planner_show", "← Меню|menu_main"]))
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# ДЕЙЛИ-БРИФИНГ
# ══════════════════════════════════════════════════════════════════════════════

async def daily_menu(update: Update, user_id: int) -> None:
    settings = await get_daily_settings(user_id)
    status_icon = "✅" if settings.get("enabled") else "❌"
    hour_display   = settings.get("hour_local", settings.get("hour", 9))
    minute_display = settings.get("minute", 0)
    text = (
        f"☀️ *Утренний брифинг*\n\n"
        f"Статус: {status_icon} {'Включён' if settings.get('enabled') else 'Выключен'}\n"
        f"Время: {hour_display:02d}:{minute_display:02d}\n\n"
        "_Каждое утро получай идею дня + формат + одну задачу на сегодня_"
    )
    toggle = "❌ Выключить|daily_off" if settings.get("enabled") else "✅ Включить|daily_on"
    await send(update, text, parse_mode="Markdown",
               reply_markup=kb([toggle], ["⏰ Изменить время|daily_set_time"], ["← Меню|menu_main"]))


async def daily_send_now(ctx, user_id: int, bot) -> None:
    profile = await get_profile(user_id)
    today = datetime.date.today().strftime("%d %B")
    prompt = (
        f"Ниша: {profile.get('niche', 'эксперт')}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\n"
        f"Тон: {profile.get('tone', 'дружелюбный')}\n"
        f"Дата: {today}"
    )
    try:
        result = await complete(DAILY_DIGEST_SYSTEM, prompt, temperature=0.85)
        await bot.send_message(chat_id=user_id, text=result, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[daily] send error for {user_id}: {e}")


async def schedule_daily(app, user_id: int, hour: int, minute: int) -> None:
    """
    Планирует ежедневное задание с jitter ±5 минут (было ±30 сек).

    Почему ±5 мин а не ±30 сек:
    При 1000 пользователях с одинаковым временем ±30 сек даёт пик 8 LLM-вызовов/сек.
    Heavy-семафор = 6 → очередь, последние получают брифинг с опозданием 10+ мин.
    ±5 мин распределяет 1000 вызовов по 600 секундам → 1.7 вызовов/сек, без очереди.
    Пользователь не заметит разницу в 3-4 минуты.
    """
    job_name = f"daily_{user_id}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    # Детерминированный jitter ±300 сек (5 мин) на основе user_id
    jitter_sec = (user_id % 601) - 300
    base_sec   = hour * 3600 + minute * 60 + jitter_sec
    base_sec   = base_sec % 86400
    j_hour, rem      = divmod(base_sec, 3600)
    j_minute, j_sec  = divmod(rem, 60)

    app.job_queue.run_daily(
        callback=_daily_job,
        time=datetime.time(hour=j_hour, minute=j_minute, second=j_sec,
                           tzinfo=datetime.timezone.utc),
        name=job_name,
        data=user_id,
    )
    logger.info(
        f"Daily job scheduled: user={user_id} at {j_hour:02d}:{j_minute:02d}:{j_sec:02d} UTC "
        f"(jitter={jitter_sec:+d}sec)"
    )


async def _daily_job(ctx) -> None:
    user_id = ctx.job.data
    await daily_send_now(ctx, user_id, ctx.bot)


# ══════════════════════════════════════════════════════════════════════════════
# ОБУЧЕНИЕ СТИЛЮ
# ══════════════════════════════════════════════════════════════════════════════

async def style_menu(update: Update, user_id: int) -> None:
    examples = await get_style_examples(user_id)
    count = len(examples)
    text = (
        f"📝 *Примеры стиля*\n\n"
        f"Добавлено: *{count}/10*\n\n"
        "Скинь 2-3 своих поста — агенты будут писать в твоём стиле.\n\n"
        "_Примеры работают точнее чем «тон» в профиле: агент видит реальные тексты "
        "и копирует структуру, лексику, ритм._"
    )
    rows = [["➕ Добавить пример|style_add"]]
    if count > 0:
        rows.append(["👁 Посмотреть примеры|style_view", "🗑 Очистить|style_clear"])
    rows.append(["← Кабинет|sub_cabinet"])
    await send(update, text, parse_mode="Markdown", reply_markup=kb(*rows))


async def style_add_start(update: Update, user_id: int) -> None:
    await save_agent_session(user_id, _STYLE_KEY, {"step": "await_example"})
    await send(
        update,
        "✍️ *Добавь пример своего поста*\n\n"
        "Скопируй и отправь текст поста — лучше целиком.\n"
        "_Можно добавить до 10 примеров_",
        parse_mode="Markdown",
        reply_markup=kb(["← Кабинет|sub_cabinet"]),
    )


async def style_save_example(update: Update, user_id: int, text: str) -> None:
    count = await add_style_example(user_id, text)
    await clear_agent_session(user_id, _STYLE_KEY)
    await send(
        update,
        f"✅ Пример добавлен! Всего: *{count}/10*\n\n"
        "_Все агенты теперь учитывают твой стиль письма_",
        parse_mode="Markdown",
        reply_markup=kb(["➕ Ещё пример|style_add", "← Меню|menu_main"]),
    )
