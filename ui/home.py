"""
ui/home.py — персонализированный домашний экран.

Вместо «Что делаем? 👇» + 14 кнопок — экран который показывает:
• стрик (сколько дней подряд создаётся контент)
• общий прогресс (сколько создано)
• превью последнего результата
• состояние голоса Миры (сколько сигналов накоплено)

Это создаёт идентичность и ощущение роста — ключевые для retention.

Правило: показываем персональный экран если у пользователя >= 2 результатов.
Иначе — обычное меню (чтобы не пугать новых пользователей статистикой нуля).
"""
import logging
from datetime import datetime, timezone, timedelta

from telegram import Update

from db import get_results, get_stats, kv_get, kv_set
from voice_learner import get_voice_stats
from utils import send, kb

logger = logging.getLogger(__name__)

_STREAK_KEY = "__content_streak__"


async def _get_streak(user_id: int) -> int:
    """Считает стрик: сколько дней подряд создавался контент."""
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
                return streak  # ещё не обновлялся сегодня — сохраняем
            else:
                return 0  # стрик сломан
    except Exception:
        pass
    return 0


async def _update_streak(user_id: int) -> int:
    """Обновляет стрик при генерации. Возвращает новое значение."""
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
                return streak  # уже обновляли сегодня
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
    """Вызывается при каждом save_result. Обновляет стрик."""
    await _update_streak(user_id)


async def show_home(update: Update, user_id: int) -> None:
    """
    Персональный домашний экран с живым голосом Миры.
    Приветствие по времени суток, цветные прогресс-бары, живые промпты.
    """
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
        from ui.progress_bar import _bar, materials_count

        lines = []

        # Стрик с эмоцией
        if streak >= 7:
            lines.append(f"🔥🔥 *{streak} дней подряд* — это уже привычка!")
        elif streak >= 3:
            lines.append(f"🔥 *{streak} дня подряд* — ты в ритме!")
        elif streak >= 1:
            lines.append(f"📅 {streak} {'день' if streak == 1 else 'дня'} подряд")

        # Прогресс с баром
        mat_bar = materials_count(total)
        lines.append(f"\n✅ {mat_bar}")

        # Голос — цветной бар
        if voice_sig == 0:
            lines.append("🎤 Голос Миры: 🤍🤍🤍🤍🤍 _оцени результат — начну запоминать_")
        elif voice_sig < 5:
            bar = _bar(voice_sig, 5, "pink", width=5)
            lines.append(f"🎤 Голос Миры: {bar} {voice_sig}/5 — настраивается")
        elif voice_sig < 10:
            bar = _bar(voice_sig - 5, 5, "purple", width=5)
            lines.append(f"🎤 Голос Миры: 🟣🟣🟣🟣🟣 {bar} — стабильный")
        elif voice_sig < 20:
            lines.append("🎤 Голос Миры: 🟣🟣🟣🟣🟣 🟡🟡🟡🟡🟡 — точный 🎯")
        else:
            lines.append("🎤 Голос Миры: 🟢🟢🟢🟢🟢 — *идеальный* ✨")

        # Последний результат
        if results:
            r       = results[0]
            preview = r["content"][:100].replace("\n", " ").strip()
            ts      = r["ts"][:10] if r.get("ts") else ""
            lines.append(
                f"\n*Последнее:* {r['agent_name']} {ts}\n"
                f"_{preview}..._"
            )

        # Живой промпт вместо "Что делаем? 👇"
        lines.append(f"\n{menu_prompt()}")

        await send(
            update,
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_home_kb(total, voice_sig),
        )

        # GIF на 7-дневный стрик
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

    # Если голос не настроен — первой кнопкой ставим Style
    if voice_signals < 3:
        rows.insert(0, ["🎤 Настроить мой голос|style_menu"])

    rows += [
        ["📚 Мои материалы|my_results",           "📈 Прогресс|my_stats"],
        ["💬 Спроси Миру|mode_chat",              "👤 Кабинет|sub_cabinet"],
    ]
    return kb(*rows)
