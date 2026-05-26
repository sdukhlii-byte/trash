"""
ui/menu.py — главное меню бота.

Изменения v2:
- 2-уровневое меню: 10 кнопок на первом уровне вместо 18.
  Пользователь не перегружен — видит 4 главных инструмента + 3 служебных.
- "← Меню" НЕ очищает сессии агентов. Очистка происходит только при
  явном старте нового агента.
- При наличии активной незавершённой сессии — показывает баннер "Продолжить?"
"""
import logging

from telegram import Update
from telegram.error import TelegramError

from db import kv_get, kv_set, get_agent_session
from utils import send, kb

logger = logging.getLogger(__name__)


def main_menu_kb() -> object:
    """Главное меню — голос первым, форматы по частоте, служебное внизу."""
    return kb(
        ["🎙 Говори голосом — пойму и сделаю|voice_hint"],
        ["✍️ Пост|agent_start_post",         "🎬 Рилс + хуки|flow_reels_short"],
        ["🎠 Карусель|flow_carousel",         "📸 Сторис|agent_start_stories"],
        ["🩺 Что буксует в контенте?|diagnostic_start"],
        ["🧩 Все инструменты|menu_more"],
        ["✨ Мой стиль|style_menu", "📚 Материалы|my_results", "👤 Кабинет|sub_cabinet"],
    )


def more_menu_kb() -> object:
    """Расширенное меню — всё что не вошло в главный экран."""
    return kb(
        ["🔥 Прогрев|agent_start_warmup",            "🎙 Talking Head|agent_start_talking_head"],
        ["🎭 Анимация|agent_start_cartoon",           "🔄 Адаптация рилса|agent_start_reels_adapt"],
        ["📅 Контент-план TG|agent_start_tg_plan",    "🧠 Мозговой штурм|quick_ideas"],
        ["🔍 Разбор профиля|agent_start_profile",     "🔎 Разбор конкурента|agent_start_competitor"],
        ["✨ Мой стиль|style_menu",                   "🗓 Планировщик|planner_show"],
        ["🔔 Утренние пуши|daily_push_menu",          "📈 Мой прогресс|my_stats"],
        ["← Главное меню|menu_main"],
    )


def model_kb(current: str) -> object:
    rows = []
    for key, name in {"claude": "Claude Sonnet", "gpt4": "GPT-4o", "grok": "Grok 3 Mini"}.items():
        check = "✓ " if key == current else ""
        rows.append([f"{check}{name}|model_{key}"])
    rows.append(["← Меню|menu_main"])
    return kb(*rows)


async def show_menu(update: Update, user_id: int) -> None:
    """
    Показывает главное меню.
    Всегда редактирует одно сохранённое сообщение (не спамит).
    НЕ трогает агент-сессии.
    """
    stored_raw = await kv_get(user_id, "__menu_msg_id__")
    from ui.mira_voice import menu_prompt
    prompt = menu_prompt()

    if stored_raw:
        try:
            await update.effective_chat.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=int(stored_raw),
                text=prompt,
                reply_markup=main_menu_kb(),
            )
            return
        except TelegramError:
            pass

    sent = await update.effective_chat.send_message(
        prompt, reply_markup=main_menu_kb()
    )
    await kv_set(user_id, "__menu_msg_id__", str(sent.message_id))


async def show_more_menu(update, query) -> None:
    """Расширенное меню — вызывается по кнопке 'Ещё инструменты'."""
    from utils import edit
    import random
    prompts = ["Все инструменты 👇", "Выбирай 👇", "Что ещё? 👇"]
    await edit(query, random.choice(prompts), reply_markup=more_menu_kb())


async def maybe_show_resume_banner(update: Update, user_id: int) -> bool:
    """
    Если у пользователя есть незавершённая сессия агента — показывает баннер
    с предложением продолжить или начать новое.
    Возвращает True если баннер показан (caller должен прервать normal flow).
    """
    active = await kv_get(user_id, "__active_agent__")
    if not active:
        return False

    session = await get_agent_session(user_id, active)
    if not session:
        return False

    step = session.get("step", "")
    if step in ("initial", "interview", "pick", "await_photos", "await_details_text"):
        import agents as ag
        import random
        spec = ag.get_spec(active)
        name = spec.name if spec else active
        prompts = [
            f"⏸ У тебя незакрытый разговор про *{name}*\n\nПродолжим или начнём что-то новое?",
            f"⏸ Мы не закончили с *{name}*\n\nВернёмся?",
            f"⏸ *{name}* ждёт — я запомнила где остановились\n\nПродолжим?",
        ]
        await send(
            update,
            random.choice(prompts),
            parse_mode="Markdown",
            reply_markup=kb(
                [f"→ Продолжить {name}|resume_agent_{active}"],
                ["✨ Начать новое|menu_main_clear"],
            ),
        )
        return True

    return False
