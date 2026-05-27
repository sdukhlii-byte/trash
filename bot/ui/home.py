"""
ui/home.py — персонализированный домашний экран.

v3: дашборд-стиль. Единый нарратив стиля через progress_bar.
Убрано несоответствие «учусь» vs «знакомлюсь» — всё идёт через
voice_level_label() из progress_bar, один источник правды.
"""
import logging
from datetime import datetime, timezone
from lava_payments import _parse_dt

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
            last_date = _parse_dt(data["last_date"])
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
            last_date = _parse_dt(data["last_date"])
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

        lines = ["📊 *Твой прогресс*\n"]

        # ── Шапка: стрик ──────────────────────────────────────────────────────
        if streak >= 7:
            lines.append(f"🔥 Стрик: *{streak} дней подряд* — это уже привычка!")
        elif streak >= 3:
            lines.append(f"🔥 Стрик: *{streak} дня подряд*")
        elif streak >= 1:
            lines.append(f"🔥 Стрик: *{streak} {'день' if streak == 1 else 'дней'} подряд*")

        # ── Блок 1: материалы ─────────────────────────────────────────────────
        lines.append(f"✅ Создано материалов: *{total}*")

        # ── Блок 2: стиль — формат как в show_stats ───────────────────────────
        if voice_sig == 0:
            lines.append("🎤 Твой стиль: _ещё не изучен_")
        elif voice_sig < 5:
            bar = "▓" * voice_sig + "░" * (5 - voice_sig)
            lines.append(f"🎤 Твой стиль: [{bar}] {voice_sig}/5 — учусь")
        else:
            lines.append(f"🎤 Твой стиль: *пишу как ты* ({voice_sig} примеров) 🎯")

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
            reply_markup=await _home_kb(user_id, total, voice_sig),
        )

        if streak == 7:
            from ui.media import send_gif
            await send_gif(update, "streak_7")

    except Exception as e:
        logger.error(f"show_home error: {e}")
        from ui.menu import show_menu as _sm
        await _sm(update, user_id)


# Маппинг agent_key → (emoji + label, callback_data)
# Только инструменты которые имеет смысл показывать в главном меню
_AGENT_MENU_ITEMS = {
    "carousel":     ("🎠 Карусель",      "flow_carousel"),
    "stories":      ("📸 Сторис",        "agent_start_stories"),
    "warmup":       ("🔥 Прогрев",       "agent_start_warmup"),
    "talking_head": ("🎙 Talking Head",   "agent_start_talking_head"),
    "tg_plan":      ("📅 Контент-план",  "agent_start_tg_plan"),
    "reels_adapt":  ("🔄 Адаптация",     "agent_start_reels_adapt"),
    "cartoon":      ("🎭 Анимация",      "agent_start_cartoon"),
    "profile":      ("🔍 Разбор профиля","agent_start_profile"),
    "competitor":   ("🔎 Разбор конкур.","agent_start_competitor"),
}

# Дефолтные слоты — когда affinity данных ещё нет
_DEFAULT_SLOT_1 = ("🎠 Карусель",  "flow_carousel")
_DEFAULT_SLOT_2 = ("📸 Сторис",    "agent_start_stories")

# Порог affinity для попадания в главное меню
_AFFINITY_THRESHOLD = 0.65


async def _get_adaptive_slots(user_id: int) -> tuple:
    """
    Возвращает два слота для главного меню на основе content_format_affinity.
    Пост и Рилс зафиксированы — варьируются только слоты 3 и 4.
    Если данных недостаточно — возвращает дефолт (Карусель, Сторис).
    """
    try:
        from db import get_cip
        cip = await get_cip(user_id)
        affinity = cip.get("content_format_affinity", {})

        # Исключаем post и reels_short — они зафиксированы в строке выше
        # Берём агентов с affinity выше порога, сортируем по убыванию
        candidates = [
            (key, score) for key, score in affinity.items()
            if key not in ("post", "reels_short")
            and key in _AGENT_MENU_ITEMS
            and score >= _AFFINITY_THRESHOLD
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)

        if len(candidates) >= 2:
            slot1 = _AGENT_MENU_ITEMS[candidates[0][0]]
            slot2 = _AGENT_MENU_ITEMS[candidates[1][0]]
            return slot1, slot2
        elif len(candidates) == 1:
            slot1 = _AGENT_MENU_ITEMS[candidates[0][0]]
            # Второй слот — дефолт если не совпадает с первым
            slot2 = _DEFAULT_SLOT_2 if slot1 != _DEFAULT_SLOT_1 else _DEFAULT_SLOT_1
            return slot1, slot2
    except Exception:
        pass

    return _DEFAULT_SLOT_1, _DEFAULT_SLOT_2


async def _home_kb(user_id: int, total: int, voice_signals: int):
    slot1, slot2 = await _get_adaptive_slots(user_id)

    rows = [
        ["🎙 Говори голосом — пойму и сделаю|voice_hint"],
        ["✍️ Пост|agent_start_post",   "🎬 Рилс + хуки|flow_reels_short"],
        [f"{slot1[0]}|{slot1[1]}",     f"{slot2[0]}|{slot2[1]}"],
        ["🩺 Что буксует в контенте?|diagnostic_start"],
        ["🧩 Все инструменты|menu_more"],
        ["✨ Мой стиль|style_menu", "📚 Материалы|my_results", "👤 Кабинет|sub_cabinet"],
    ]
    return kb(*rows)
