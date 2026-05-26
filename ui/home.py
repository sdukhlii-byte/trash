"""
ui/home.py — персонализированный домашний экран.

v3: дашборд-стиль. Единый нарратив стиля через progress_bar.
Убрано несоответствие «учусь» vs «знакомлюсь» — всё идёт через
voice_level_label() из progress_bar, один источник правды.
"""
import logging
from datetime import datetime, timezone

from telegram import Update

from db import get_results, get_stats, kv_get, kv_set
from voice_learner import get_voice_stats
from utils import send, kb

logger = logging.getLogger(__name__)

_STREAK_KEY = "__content_streak__"


async def _get_streak(user_id: int) -> int:
    try:
        raw = await kv_get(user_id, _STREAK_KEY)
        if raw:
            data = __import__("json").loads(raw)
            last_date = datetime.fromisoformat(data["last_date"])
            streak = data.get("streak", 1)
            today = datetime.now(timezone.utc).date()
            diff = (today - last_date.date()).days
            if diff == 0:
                return streak
            elif diff == 1:
                return streak
            else:
                return 0
    except Exception:
        pass
    return 0


async def _update_streak(user_id: int) -> int:
    import json
    try:
        today = datetime.now(timezone.utc)
        raw = await kv_get(user_id, _STREAK_KEY)
        if raw:
            data = json.loads(raw)
            last_date = datetime.fromisoformat(data["last_date"])
            streak = data.get("streak", 1)
            diff = (today.date() - last_date.date()).days
            if diff == 0:
                return streak
            elif diff == 1:
                streak += 1
            else:
                streak = 1
        else:
            streak = 1

        await kv_set(user_id, _STREAK_KEY,
                     json.dumps({"last_date": today.isoformat(), "streak": streak},
                                ensure_ascii=False))
        return streak
    except Exception as e:
        logger.warning(f"_update_streak error: {e}")
        return 0


async def update_streak_on_result(user_id: int) -> None:
    await _update_streak(user_id)


# ── Единый источник правды для уровня стиля ───────────────────────────────────

def _voice_level_label(n: int) -> str:
    """Короткая метка уровня — используется и в home, и в cabinet."""
    if n == 0:   return "не изучен"
    if n < 5:    return "знакомлюсь"
    if n < 10:   return "узнаю тебя"
    if n < 20:   return "пишу как ты"
    return "точно ✨"


def _voice_bar_emoji(n: int) -> str:
    """Эмодзи-бар для блока стиля в дашборде."""
    from ui.progress_bar import _bar
    if n == 0:
        return "🤍🤍🤍🤍🤍"
    elif n < 5:
        return _bar(n, 5, "pink", width=5)
    elif n < 10:
        filled = "🟣" * 5
        rest   = _bar(n - 5, 5, "purple", width=5)
        return f"{filled} {rest}"
    elif n < 20:
        return "🟣🟣🟣🟣🟣 🟡🟡🟡🟡🟡"
    else:
        return "🟢🟢🟢🟢🟢"


# ── Главный экран ─────────────────────────────────────────────────────────────

async def show_home(update: Update, user_id: int) -> None:
    from ui.menu import show_menu

    try:
        stats   = await get_stats(user_id)
        total   = stats.get("total", 0)

        if total < 2:
            await show_menu(update, user_id)
            return

        results   = await get_results(user_id, limit=1)
        streak    = await _get_streak(user_id)
        voice_s   = await get_voice_stats(user_id)
        voice_sig = voice_s.get("total_signals", 0)

        from ui.mira_voice import menu_prompt
        from db import get_user_name
        _uname = await get_user_name(user_id)

        lines = []

        # ── Шапка: стрик ──────────────────────────────────────────────────────
        if streak >= 7:
            lines.append(f"🔥🔥 *{streak} дней подряд* — это уже привычка!")
        elif streak >= 3:
            lines.append(f"🔥 *{streak} дня подряд* — ты в ритме!")
        elif streak >= 1:
            lines.append(f"📅 {streak} {'день' if streak == 1 else 'дня'} подряд")

        # ── Блок 1: материалы ─────────────────────────────────────────────────
        from ui.progress_bar import materials_count
        mat_bar = materials_count(total)
        lines.append(f"\n✅ {mat_bar}")

        # ── Блок 2: стиль — единый нарратив через _voice_level_label ─────────
        bar   = _voice_bar_emoji(voice_sig)
        level = _voice_level_label(voice_sig)

        if voice_sig == 0:
            lines.append(
                f"🎤 *Твой стиль:* {bar}\n"
                "_Оцени результат — и я начну запоминать как ты пишешь_"
            )
        elif voice_sig < 5:
            left = 5 - voice_sig
            lines.append(
                f"🎤 *Твой стиль:* {bar} {voice_sig}/5 — {level}\n"
                f"_ещё {left} {'оценка' if left == 1 else 'оценки'}, и начну попадать стабильно_"
            )
        elif voice_sig < 10:
            lines.append(f"🎤 *Твой стиль:* {bar} — {level}")
        else:
            lines.append(f"🎤 *Твой стиль:* {bar} — *{level}* 🎯")

        # ── Блок 3: последний результат ───────────────────────────────────────
        if results:
            r       = results[0]
            preview = r["content"][:120].replace("\n", " ").strip()
            ts      = r["ts"][:10] if r.get("ts") else ""
            lines.append(
                f"\n┌ *{r['agent_name']}* {ts}\n"
                f"└ _{preview}…_"
            )

        # ── CTA ───────────────────────────────────────────────────────────────
        lines.append(f"\n{menu_prompt(name=_uname)}")

        await send(
            update,
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_home_kb(total, voice_sig),
        )

        if streak == 7:
            from ui.media import send_gif
            await send_gif(update, "streak_7")

    except Exception as e:
        logger.error(f"show_home error: {e}")
        from ui.menu import show_menu as _sm
        await _sm(update, user_id)


def _home_kb(total: int, voice_signals: int):
    rows = [
        ["✍️ Написать пост|agent_start_post",    "🎬 Рилс + хуки|flow_reels_short"],
        ["🎠 Карусель|flow_carousel",             "🔥 Прогрев|agent_start_warmup"],
        ["🧩 Все инструменты|menu_more"],
    ]

    if voice_signals < 3:
        rows.insert(0, ["🎤 Научить Миру моему стилю|style_menu"])

    rows += [
        ["📚 Мои материалы|my_results",           "📈 Прогресс|my_stats"],
        ["💬 Спроси Миру|mode_chat",              "👤 Кабинет|sub_cabinet"],
    ]
    return kb(*rows)
