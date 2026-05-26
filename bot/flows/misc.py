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
from utils import send, kb, safe_delete, typing_loop
from config import (
    QUICK_IDEAS_SYSTEM, REFINE_SYSTEM, REGEN_SYSTEM,
    PLANNER_IDEAS_SYSTEM, DAILY_DIGEST_SYSTEM,
    CONTENT_DIAGNOSTIC_SYSTEM,
)

logger = logging.getLogger(__name__)
async def _typing_loop(chat) -> None:
    """Typing indicator пока идёт LLM-вызов."""
    try:
        while True:
            await chat.send_action("typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


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

    # Build CIP context for novelty filtering
    cip_ctx_parts = []
    try:
        from db import get_cip
        cip = await get_cip(user_id)
        if cip.get("recent_topics"):
            cip_ctx_parts.append(f"Недавние темы (избегай повторов): {', '.join(cip['recent_topics'][-5:])}")
        if cip.get("current_funnel_phase"):
            cip_ctx_parts.append(f"Текущий этап воронки: {cip['current_funnel_phase']}")
        if cip.get("audience_trust_level"):
            cip_ctx_parts.append(f"Уровень доверия аудитории: {cip['audience_trust_level']}/10")
    except Exception:
        pass

    cip_block = ("\n\nСТРАТЕГИЧЕСКИЙ КОНТЕКСТ:\n" + "\n".join(cip_ctx_parts)) if cip_ctx_parts else ""

    prompt = (
        f"Ниша: {niche}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\n"
        f"Тон: {profile.get('tone', 'живой')}"
        + cip_block +
        "\n\nДай 10 конкретных идей. Каждая — готовый хук, не абстрактная тема. Без вступлений."
    )
    status = await update.effective_chat.send_message("Генерирую 10 идей под твою нишу...")
    try:
        from voice_learner import build_voice_context
        from niche_intel import build_niche_context
        voice_ctx = await build_voice_context(user_id)
        niche_ctx = build_niche_context(profile.get("niche", ""))
        qi_sys = protect(user_id, await get_prompt(user_id, "quick_ideas", QUICK_IDEAS_SYSTEM))
        qi_sys = qi_sys + voice_ctx + niche_ctx
        result = await complete(qi_sys, prompt, temperature=0.85)
    except Exception as e:
        logger.error(f"[qi] generate error: {e}")
        result = None
    await safe_delete(status)
    if not result:
        await send(update, "Не вышло с первого раза — данные сохранены, жми ещё раз 🔁",
                   reply_markup=kb(["🔄 Повторить|quick_ideas", "← Меню|menu_main"]))
        return
    await clear_agent_session(user_id, _QI_KEY)

    # Save topic to CIP recent_topics
    try:
        from db import add_recent_topic
        await add_recent_topic(user_id, niche[:100])
    except Exception:
        pass

    try:
        await save_result(user_id, "quick_ideas", "10 идей быстро", result)
    except Exception as e:
        logger.warning(f"save_result qi failed: {e}")

    # Store result in session for backlog action
    try:
        await save_agent_session(user_id, _QI_KEY, {"last_ideas": result, "step": "done"})
    except Exception:
        pass

    await send(
        update,
        f"💡 *10 идей для постов:*\n\n{result}",
        parse_mode="Markdown",
        reply_markup=kb(
            ["🔄 Ещё 10 идей|quick_ideas", "💾 В банк идей|qi_save_backlog"],
            ["← Меню|menu_main"],
        ),
    )
    # Стрик
    try:
        from ui.home import update_streak_on_result as _usr
        await _usr(user_id)
    except Exception:
        pass

    # Voice feedback — only while useful (fatigue guard: skip if well-trained)
    try:
        import asyncio
        from db import get_results as _gr, get_stats as _gs
        from voice_learner import voice_feedback_kb, get_voice_stats, should_show_voice_feedback
        _recent = await _gr(user_id, limit=1)
        if _recent:
            await asyncio.sleep(0.5)
            _vs    = await get_voice_stats(user_id)
            _total = _vs.get("total_signals", 0)
            _gen_count = (await _gs(user_id)).get("total", 1)
            if should_show_voice_feedback(_total, _gen_count):
                if _total < 5:
                    _filled = "▓" * _total if _total else ""
                    _empty  = "░" * (5 - _total)
                    _hint   = f"\n\n_Учу твой стиль: [{_filled}{_empty}] {_total}/5_" if _total else \
                              "\n\n_Оцени — и я запомню как ты пишешь._"
                else:
                    _hint = f"\n\n_Пишу как ты: точно ({_total} примеров) 🎯_"
                await send(update, f"Звучит как твой голос?{_hint}",
                           parse_mode="Markdown",
                           reply_markup=voice_feedback_kb(_recent[0]["id"]))
    except Exception:
        pass

    # Usage-based конверсионный триггер (3-я генерация в триале)
    try:
        from user_state import get_user_state, UserState
        _state = await get_user_state(user_id)
        if _state == UserState.TRIAL:
            _stats = await get_stats(user_id)
            if _stats.get("total", 0) == 3:
                import asyncio as _aio
                await _aio.sleep(1.5)
                from lava_payments import get_payment_link
                _link = get_payment_link(user_id)
                from telegram import InlineKeyboardMarkup, InlineKeyboardButton
                _pay_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("💳 Оформить подписку", url=_link)
                ]] if _link else [])
                await update.effective_chat.send_message(
                    "Ты уже создала 3 материала — видно что пошло.\n\n"
                    "Триал заканчивается. Хочешь продолжить без ограничений?",
                    reply_markup=_pay_kb,
                )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# ДОРАБОТКА И ДРУГОЙ ВАРИАНТ
# ══════════════════════════════════════════════════════════════════════════════

async def refine_start(update: Update, user_id: int, result_id: int = 0) -> None:
    # BUG #5 FIX: inform user if they're refining a freshly regenerated result
    try:
        from db import kv_get as _kvg
        _just_regen = await _kvg(user_id, "__regen_just_ran__")
    except Exception:
        _just_regen = None

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
    _regen_note = "\n\n⚠️ _Ты только что перегенерировал(а) — дорабатываю новый вариант._" if _just_regen else ""
    await send(
        update,
        f"✏️ *Доработка*\n\n*{r['agent_name']}:*\n_{preview}..._\n\n"
        "Что изменить? Напиши инструкцию:\n"
        "_«Сделай короче», «Добавь юмора», «Усиль CTA»..._"
        f"{_regen_note}",
        parse_mode="Markdown",
        reply_markup=kb(["← Меню|menu_main"]),
    )


async def refine_do(update: Update, user_id: int, instruction: str, s: dict) -> None:
    original = s.get("original", "")

    # Build strategic context block for REFINE_SYSTEM
    _strat_ctx = ""
    try:
        from db import get_cip as _gcip
        _cip = await _gcip(user_id)
        _parts = []
        _agent_name = s.get("agent_name", "")
        if _agent_name:
            _parts.append(f"Инструмент: {_agent_name}")
        _funnel = _cip.get("current_funnel_phase", "")
        if _funnel:
            _parts.append(f"Этап воронки: {_funnel}")
        _trust = _cip.get("audience_trust_level")
        if _trust:
            _parts.append(f"Доверие аудитории: {_trust}/10")
        # Warmup day detection
        _wday = s.get("warmup_day", 0)
        if not _wday and "ДЕНЬ 1" in original[:500]:
            _wday = 1
        elif not _wday and "ДЕНЬ 2" in original[:500]:
            _wday = 2
        elif not _wday and "ДЕНЬ 3" in original[:500]:
            _wday = 3
        if _wday:
            _parts.append(f"warmup_day: {_wday}")
        if _parts:
            _strat_ctx = "\n\nСТРАТЕГИЧЕСКИЙ КОНТЕКСТ МАТЕРИАЛА:\n" + "\n".join(_parts)
    except Exception:
        pass

    prompt = f"Оригинальный текст:\n{original}\n\nЗадача: {instruction}{_strat_ctx}"

    # Голосовой статус — живой, в стиле Миры (фаза 2.3)
    try:
        from voice_refine_parser import detect_refine_intent, get_refine_status
        _refine_label = detect_refine_intent(instruction)
        _status_text = get_refine_status(_refine_label) if _refine_label else "Дорабатываю — держи..."
    except Exception:
        _status_text = "Дорабатываю — держи..."

    status = await update.effective_chat.send_message(_status_text)
    try:
        from voice_learner import build_voice_context
        profile    = await get_profile(user_id)
        voice_ctx  = await build_voice_context(user_id)
        refine_sys = protect(user_id, await get_prompt(user_id, "refine", REFINE_SYSTEM))
        refine_sys = refine_sys + voice_ctx
        _tt = asyncio.create_task(_typing_loop(update.effective_chat))
        try:
            result = await complete(refine_sys, prompt, temperature=0.7)
        finally:
            _tt.cancel()
    except Exception as e:
        logger.error(f"[refine] error: {e}")
        result = None
    await safe_delete(status)
    await clear_agent_session(user_id, _REFINE_KEY)
    if not result:
        await send(update, "Связь прервалась — текст на месте, жми ещё раз 🔁",
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
    # Стрик
    try:
        from ui.home import update_streak_on_result as _usr
        await _usr(user_id)
    except Exception:
        pass
    # Voice feedback
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
    """Deprecated: использует limit=1 — небезопасно при конкурентных сессиях.
    Оставлен для обратной совместимости со старыми legacy кнопками.
    Новые кнопки используют regen_by_id(result_id) через scoped callbacks.
    """
    results = await get_results(user_id, limit=1)
    if not results:
        await send(update, "Нет материалов. Создай что-нибудь сначала.",
                   reply_markup=kb(["← Меню|menu_main"]))
        return
    await regen_by_id(update, user_id, results[0]["id"])


async def regen_by_id(update: Update, user_id: int, result_id: int) -> None:
    """PHASE 9: безопасная регенерация по конкретному result_id.
    Использует source_input из БД — не берёт 'последний' результат по времени.
    """
    r = await get_result_by_id(user_id, result_id)
    if not r:
        await send(update, "Материал не найден.", reply_markup=kb(["← Меню|menu_main"]))
        return

    # PHASE 9 FIX: используем source_input если есть — это исходник до правок
    source = r.get("source_input") or r["content"]
    prompt = f"Оригинал:\n{source}"

    status = await update.effective_chat.send_message("Пишу другой вариант — ищу другой угол...")
    import asyncio as _aio
    _tt = _aio.create_task(typing_loop(update.effective_chat))
    try:
        from voice_learner import build_voice_context
        voice_ctx  = await build_voice_context(user_id)
        regen_sys  = protect(user_id, await get_prompt(user_id, "regen", REGEN_SYSTEM))
        regen_sys  = regen_sys + voice_ctx
        result = await complete(regen_sys, prompt, temperature=0.9, presence_penalty=0.4)
    except Exception as e:
        logger.error(f"[regen] error: {e}")
        result = None
    finally:
        _tt.cancel()

    await safe_delete(status)

    if not result:
        await send(update, "Не вышло с первого раза — все данные на месте, жми 🔁",
                   reply_markup=kb([f"🔄 Ещё раз|regen:{result_id}:1", "← Меню|menu_main"]))
        return

    # Сохраняем с parent_result_id и source_input для цепочки
    _new_regen_id = 0
    try:
        _new_regen_id = await save_result(
            user_id, r["agent_key"], r["agent_name"], result,
            source_input=source,
            parent_result_id=result_id,
            allowed_actions=r.get("allowed_actions"),
        ) or 0
    except Exception:
        pass

    # Стрик
    try:
        from ui.home import update_streak_on_result as _usr
        await _usr(user_id)
    except Exception:
        pass

    # Scoped кнопки для нового результата
    if _new_regen_id:
        _regen_cb  = f"regen:{_new_regen_id}:1"
        _refine_cb = f"refine:{_new_regen_id}:1"
    else:
        _regen_cb  = f"regen:{result_id}:1"
        _refine_cb = "refine_last"

    await send(
        update,
        f"🔄 *Другой вариант:*\n\n{result}",
        parse_mode="Markdown",
        reply_markup=kb(
            [f"✏️ Доработать|{_refine_cb}", f"🔄 Ещё вариант|{_regen_cb}"],
            ["← Меню|menu_main"],
        ),
    )

    try:
        import asyncio
        from voice_learner import voice_feedback_kb
        if _new_regen_id:
            await asyncio.sleep(0.5)
            await send(update, "Звучит как твой голос?",
                       reply_markup=voice_feedback_kb(_new_regen_id))
    except Exception:
        pass
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
            "📚 *Мои материалы*\n\n"
            "Здесь появится всё что ты создашь.\n\n"
            "_С чего начать? Выбери инструмент 👇_",
            parse_mode="Markdown",
            reply_markup=kb(
                ["✍️ Написать пост|agent_start_post",
                 "🎬 Хуки для рилса|flow_reels_short"],
                ["🎠 Карусель|flow_carousel",
                 "📸 Сторис|agent_start_stories"],
                ["← Меню|menu_main"],
            ),
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
            lines.append("🎤 Твой стиль: _ещё не изучен_ — после генерации появится кнопка оценки")
        elif voice_sig < 5:
            bar = "▓" * voice_sig + "░" * (5 - voice_sig)
            lines.append(f"🎤 Твой стиль: [{bar}] {voice_sig}/5 — учусь")
        else:
            lines.append(f"🎤 Твой стиль: *пишу как ты* ({voice_sig} примеров) 🎯")

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




async def qi_save_backlog(update: Update, user_id: int) -> None:
    """Сохраняет последние идеи из браинсторма в content_backlog."""
    try:
        session = await get_agent_session(user_id, _QI_KEY)
        ideas_text = (session or {}).get("last_ideas", "")
        if not ideas_text:
            await send(update, "Нет свежих идей для сохранения. Сначала сгенерируй идеи.",
                       reply_markup=kb(["← Меню|menu_main"]))
            return
        from db import add_to_backlog
        # Parse numbered ideas and add each
        lines = [l.strip() for l in ideas_text.split("\n") if l.strip()]
        saved = 0
        for line in lines:
            # Match lines starting with number+dot or number+period
            if line and (line[0].isdigit() or line.startswith("•")):
                # Extract just the idea text (first quoted or after number)
                idea = line.lstrip("0123456789. •").strip()
                if idea and len(idea) > 10:
                    await add_to_backlog(user_id, idea)
                    saved += 1
        if saved:
            await send(update,
                f"✅ *{saved} идей сохранено в банк идей!*\n\n"
                "Планировщик будет использовать их при создании следующей недели.",
                parse_mode="Markdown",
                reply_markup=kb(["🔄 Ещё идеи|quick_ideas", "← Меню|menu_main"]))
        else:
            await send(update, "Не удалось распарсить идеи. Попробуй ещё раз.",
                       reply_markup=kb(["← Меню|menu_main"]))
    except Exception as e:
        logger.error(f"[qi_save_backlog] error: {e}")
        await send(update, "Ошибка при сохранении. Попробуй позже.",
                   reply_markup=kb(["← Меню|menu_main"]))

async def planner_gen_week(update: Update, user_id: int) -> None:
    profile = await get_profile(user_id)
    session = await get_agent_session(user_id, _PLANNER_KEY) or {}

    # If we don't have the week goal yet — ask for it first (unless urgency)
    if not session.get("week_goal"):
        # Check urgency from intent context
        urgency = session.get("urgency", False)
        if not urgency:
            try:
                from db import kv_get as _kv_get
                import json as _json
                _ctx_raw = await _kv_get(user_id, "__intent_ctx__")
                if _ctx_raw:
                    _ctx = _json.loads(_ctx_raw)
                    urgency = bool(_ctx.get("urgency"))
            except Exception:
                pass

        if urgency:
            # Bypass: генерируем без цели
            session["week_goal"] = "без конкретной цели — сбалансированный план"
            session["urgency"] = True
            await save_agent_session(user_id, _PLANNER_KEY, session)
        else:
            await save_agent_session(user_id, _PLANNER_KEY, {"step": "await_week_goal"})
            await send(
                update,
                "🗓 *План на неделю*\n\nЧтобы план был стратегическим, а не просто списком — один вопрос:\n\n"
                "*Что должна почувствовать или сделать аудитория в конце этой недели?*\n\n"
                "_Например: записаться на консультацию / понять что им нужна твоя услуга / увидеть тебя как эксперта в X_\n\n"
                "Если сейчас идёт прогрев или запуск — напиши об этом тоже.",
                parse_mode="Markdown",
                reply_markup=kb(["⏭ Без цели, просто план|planner_week_skip", "← Планировщик|planner_show"]),
            )
            return

    today = datetime.date.today()
    dates = [(today + datetime.timedelta(days=i)).strftime("%d.%m (%a)") for i in range(7)]
    week_goal = session.get("week_goal", "")
    active_launch = session.get("active_launch", "")
    prompt = (
        f"Ниша: {profile.get('niche', 'не указана')}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\n"
        f"Тон: {profile.get('tone', 'живой')}\n\n"
        f"Цель недели (что должна почувствовать/сделать аудитория): {week_goal}\n"
        + (f"Активный запуск/прогрев: {active_launch}\n" if active_launch else "")
        + f"\nДаты: {', '.join(dates)}\n\nСоставь стратегический план на 7 дней с нарративной дугой."
    )
    await clear_agent_session(user_id, _PLANNER_KEY)
    status = await update.effective_chat.send_message("Выстраиваю нарратив недели...")
    _tt = asyncio.create_task(_typing_loop(update.effective_chat))
    try:
        result = await complete(protect(user_id, PLANNER_IDEAS_SYSTEM), prompt, temperature=0.85)
    except Exception as e:
        logger.error(f"[planner] week error: {e}")
        result = None
    finally:
        _tt.cancel()
    await safe_delete(status)
    if not result:
        await send(update, "Связь прервалась — жми ещё раз 🔁",
                   reply_markup=kb(["← Планировщик|planner_show"]))
        return

    # Mark backlog ideas that were injected as used
    try:
        from db import get_content_backlog, mark_backlog_used
        _backlog = await get_content_backlog(user_id)
        for _entry in _backlog:
            if not _entry.get("used") and _entry.get("idea", "") in result:
                await mark_backlog_used(user_id, _entry["idea"])
    except Exception:
        pass

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
    if step == "await_week_goal":
        # Urgency bypass: если флаг выставлен — генерируем план без цели
        if s.get("urgency"):
            s["week_goal"] = ""
            await save_agent_session(user_id, _PLANNER_KEY, s)
            await planner_gen_week(update, user_id)
            return True
        # User answered the week goal question — store and generate
        s["week_goal"] = text.strip()
        # Check if launch mentioned
        lower = text.lower()
        if any(w in lower for w in ["прогрев", "запуск", "лонч", "продажи", "оффер"]):
            s["active_launch"] = text.strip()
        await save_agent_session(user_id, _PLANNER_KEY, s)
        await planner_gen_week(update, user_id)
        return True
    elif step == "await_date":
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


_DIGEST_CACHE_TTL = 23 * 3600  # 23 часа — чуть меньше суток


async def _get_cached_digest(user_id: int) -> str | None:
    """Читает пре-генерированный дайджест из Redis."""
    try:
        from db import kv_get
        val = await kv_get(user_id, "__daily_digest__")
        return val if val else None
    except Exception:
        return None


async def _cache_digest(user_id: int, text: str) -> None:
    """Кэширует дайджест в Redis на 23 часа."""
    try:
        from db import kv_set
        await kv_set(user_id, "__daily_digest__", text, ttl=_DIGEST_CACHE_TTL)
    except Exception:
        pass


async def pregen_digest(user_id: int) -> str | None:
    """
    Пре-генерирует дайджест и кладёт в Redis.
    Вызывается из batch-задания в 06:00 UTC — до того как пользователи просыпаются.
    """
    from db import get_user_name
    profile = await get_profile(user_id)
    name    = await get_user_name(user_id)
    today   = datetime.date.today().strftime("%d %B")
    name_line = f"Имя пользователя: {name}\n" if name else ""
    prompt  = (
        f"{name_line}"
        f"Ниша: {profile.get('niche', 'эксперт')}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\n"
        f"Тон: {profile.get('tone', 'дружелюбный')}\n"
        f"Дата: {today}"
    )
    try:
        result = await complete(DAILY_DIGEST_SYSTEM, prompt, temperature=0.85)
        if result and result.strip():
            await _cache_digest(user_id, result)
            return result
    except Exception as e:
        logger.error(f"[daily pregen] user={user_id}: {e}")
    return None


async def daily_send_now(ctx, user_id: int, bot) -> None:
    """
    Отправляет дайджест пользователю.
    Сначала проверяет кэш (пре-генерация батча в 06:00 UTC).
    Если кэша нет — генерирует на лету (fallback).
    """
    result = await _get_cached_digest(user_id)

    if not result:
        # Fallback: генерация на лету если батч не успел
        logger.info(f"[daily] cache miss for {user_id}, generating on-demand")
        result = await pregen_digest(user_id)

    if not result:
        logger.error(f"[daily] no digest for {user_id}")
        return

    try:
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
    from voice_learner import get_voice_stats
    examples = await get_style_examples(user_id)
    ex_count = len(examples)
    vs = await get_voice_stats(user_id)
    signals = vs.get("total_signals", 0)

    # Статус системы обучения — единый нарратив
    if signals == 0 and ex_count == 0:
        status = (
            "Пока не знаю как ты пишешь.\n\n"
            "Чем больше дашь — тем точнее буду попадать в твой голос с первого раза."
        )
    elif ex_count > 0 and signals < 5:
        status = (
            f"Есть {ex_count} {'пример' if ex_count == 1 else 'примера' if ex_count < 5 else 'примеров'} твоих постов "
            f"и {signals} {'оценка' if signals == 1 else 'оценки' if signals < 5 else 'оценок'} результатов.\n\n"
            "Хорошее начало. Чем больше оцениваешь результаты — тем точнее становлюсь."
        )
    else:
        status = (
            f"Примеров постов: *{ex_count}/10*\n"
            f"Оценок результатов: *{signals}*\n\n"
            "Чем больше оцениваешь — тем меньше правок нужно."
        )

    text = (
        f"✨ *Мой стиль*\n\n"
        f"{status}\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "*Два способа научить Миру:*\n\n"
        "📝 *Скинь свои посты* — я буду писать твоими словами, "
        "твоей структурой, твоим ритмом. Самый быстрый способ.\n\n"
        "✅ *Оценивай результаты* — после каждого текста нажимай "
        "«Звучит как я» или «Не совсем + объясни что не так». "
        "Я запоминаю и корректирую."
    )
    rows = [["📝 Добавить свои посты|style_add"]]
    if ex_count > 0:
        rows.append(["👁 Посмотреть добавленные|style_view", "🗑 Очистить|style_clear"])
    rows.append(["← Меню|menu_main"])
    await send(update, text, parse_mode="Markdown", reply_markup=kb(*rows))


async def style_add_start(update: Update, user_id: int) -> None:
    """
    Пункт 4: batch-режим сбора постов через __collecting_posts__ в Redis.
    Пункт 5: проактивный совет про скриншоты — удобнее чем копировать текст.
    """
    import json as _json
    collecting_session = {
        "mode": "collecting_posts",
        "collected": [],
        "target_count": 10,
    }
    await kv_set(user_id, "__collecting_posts__",
                 _json.dumps(collecting_session, ensure_ascii=False), ttl=3600)
    await clear_agent_session(user_id, _STYLE_KEY)
    await send(
        update,
        "📝 *Обучение голосу*\n\n"
        "Пришли мне 10 своих лучших постов — скинь все одним за другим, я подожду 🙌\n\n"
        "💡 *Совет:* Скриншоты удобнее всего — сделай скрин прямо в Instagram "
        "и кидай сюда. Не надо ничего копировать, я сама прочитаю текст 📸\n\n"
        "_Можно смешивать: скриншоты + текстом. "
        "Чем больше примеров — тем точнее буду писать в твоём стиле._\n\n"
        "Когда пришлёшь все — напиши «Готово» или я остановлюсь сама на 10.",
        parse_mode="Markdown",
        reply_markup=kb(["⏹ Завершить сбор|style_collect_done", "← Назад|style_menu"]),
    )


async def style_save_example(update: Update, user_id: int, text: str) -> None:
    count = await add_style_example(user_id, text)
    await clear_agent_session(user_id, _STYLE_KEY)
    await send(
        update,
        f"✅ Добавила. Всего постов: *{count}/10*\n\n"
        "_Теперь в каждой генерации буду смотреть на твои тексты "
        "и писать так же — твоими словами, твоим ритмом._\n\n"
        "Можно добавить ещё — чем больше примеров, тем точнее.",
        parse_mode="Markdown",
        reply_markup=kb(["📝 Добавить ещё|style_add", "← Меню|menu_main"]),
    )


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT DIAGNOSTIC MODE (#29)
# ══════════════════════════════════════════════════════════════════════════════

_DIAG_KEY = "diagnostic_flow"


async def diagnostic_start(update: Update, user_id: int) -> None:
    """Запускает режим диагностики контента."""
    await clear_agent_session(user_id, _DIAG_KEY)
    await save_agent_session(user_id, _DIAG_KEY, {"step": "await_content"})
    await send(
        update,
        "🔍 *Диагностика контента*\n\n"
        "Отправь текст поста, рилса или карусели который не сработал так как ты ожидала.\n\n"
        "_Мира разберёт: почему хук не зацепил, выполнено ли обещание, правильный ли CTA, "
        "и что одно изменение даст наибольший прирост._",
        parse_mode="Markdown",
        reply_markup=kb(["← Меню|menu_main"]),
    )


async def diagnostic_run(update: Update, user_id: int, content: str) -> None:
    """Запускает диагностику по переданному контенту."""
    profile = await get_profile(user_id)
    prompt = (
        f"Ниша автора: {profile.get('niche', 'не указана')}\n"
        f"Аудитория: {profile.get('audience', 'не указана')}\n\n"
        f"КОНТЕНТ ДЛЯ ДИАГНОЗА:\n{content}"
    )
    status = await update.effective_chat.send_message("Анализирую — ищу главную проблему...")
    _tt = asyncio.create_task(_typing_loop(update.effective_chat))
    try:
        result = await complete(CONTENT_DIAGNOSTIC_SYSTEM, prompt, temperature=0.5)
    except Exception as e:
        logger.error(f"[diagnostic] error: {e}")
        result = None
    finally:
        _tt.cancel()
    await safe_delete(status)
    await clear_agent_session(user_id, _DIAG_KEY)

    if not result:
        await send(update, "Не получилось — попробуй ещё раз 🔁",
                   reply_markup=kb(["🔍 Попробовать снова|diagnostic_start", "← Меню|menu_main"]))
        return

    try:
        await save_result(user_id, "diagnostic", "Диагностика контента", result)
    except Exception:
        pass

    await send(
        update,
        f"🔍 *Диагноз:*\n\n{result}",
        parse_mode="Markdown",
        reply_markup=kb(
            ["✏️ Доработать с учётом диагноза|refine_last"],
            ["🔍 Другой контент|diagnostic_start", "← Меню|menu_main"],
        ),
    )


async def route_diagnostic_text(update: Update, user_id: int, text: str, s: dict) -> bool:
    """Роутер текстовых сообщений для диагностики."""
    if s.get("step") == "await_content":
        await diagnostic_run(update, user_id, text)
        return True
    return False
