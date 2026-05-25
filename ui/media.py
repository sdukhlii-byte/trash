"""
ui/media.py — вся визуальная магия Telegram.

Реализует:
  1. GIF-анимации в ключевые моменты (триал, первая генерация, level-up голоса)
  2. Реакции на сообщения (❤️ когда пользователь ставит 👍)
  3. Форматирование результатов агентов (секции, моноширинные хуки, spoiler-концовки)
  4. Inline query — вызов Миры из любого чата
  5. TTS — результат как голосовое сообщение (опционально)
  6. set_reaction helper
"""
from __future__ import annotations
import logging
import os

logger = logging.getLogger(__name__)

# ── GIF-файлы ─────────────────────────────────────────────────────────────────
# Клади GIF-файлы в папку assets/gifs/ рядом с main.py.
# Если файла нет — функция тихо пропускает (graceful degradation).

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "gifs")

_GIFS = {
    "trial_activated":    "trial_activated.gif",     # конфетти / ура
    "first_generation":   "first_generation.gif",    # sparkle / магия
    "voice_level_up":     "voice_level_up.gif",      # звёздочки / левел ап
    "subscription_paid":  "subscription_paid.gif",   # ❤️ / спасибо
    "streak_7":           "streak_7.gif",             # огонь / серия
}

_CAPTIONS = {
    "trial_activated":   "🎉 7 дней — твои! Давай начнём.",
    "first_generation":  "✨ Первый материал готов. Добро пожаловать в клуб.",
    "voice_level_up":    "🎯 Голос Миры стал точнее. Тексты будут ещё лучше.",
    "subscription_paid": "❤️ Ты с нами. Это только начало.",
    "streak_7":          "🔥 7 дней подряд. Это уже привычка.",
}


async def send_gif(update, event: str, extra_caption: str = "") -> bool:
    """
    Отправляет GIF для события. Возвращает True если отправил.
    Если GIF-файла нет — возвращает False (caller показывает текст).
    """
    filename = _GIFS.get(event)
    if not filename:
        return False

    path = os.path.join(_ASSETS_DIR, filename)
    if not os.path.exists(path):
        logger.debug(f"[media] GIF not found: {path}")
        return False

    caption = _CAPTIONS.get(event, "")
    if extra_caption:
        caption = f"{caption}\n\n{extra_caption}" if caption else extra_caption

    try:
        chat = update.effective_chat
        with open(path, "rb") as f:
            await chat.send_animation(
                animation=f,
                caption=caption,
                parse_mode="Markdown",
            )
        return True
    except Exception as e:
        logger.warning(f"[media] send_gif failed ({event}): {e}")
        return False


# ── Реакции ───────────────────────────────────────────────────────────────────

async def set_reaction(bot, chat_id: int, message_id: int, emoji: str = "❤") -> None:
    """
    Ставит реакцию на сообщение.
    Telegram Bot API 7.0+ — ReactionTypeEmoji.
    Тихо падает если нет прав или API не поддерживает.
    """
    try:
        from telegram import ReactionTypeEmoji
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
            is_big=False,
        )
    except Exception as e:
        logger.debug(f"[media] set_reaction failed: {e}")


async def react_to_voice_feedback(update, bot) -> None:
    """Ставит ❤️ когда пользователь нажал 'Звучит как я'."""
    try:
        msg = update.callback_query.message if update.callback_query else None
        if msg:
            await set_reaction(bot, msg.chat_id, msg.message_id, "❤")
    except Exception:
        pass


# ── Форматирование результатов ────────────────────────────────────────────────

def format_result(text: str, agent_key: str = "") -> str:
    """
    Улучшает форматирование результата агента:
    - Хуки в моноширинном шрифте
    - Разделители между секциями
    - Spoiler для последней строки (вовлечение)
    - Нумерованные списки → эмодзи-списки
    """
    if not text or not text.strip():
        return text

    lines = text.split("\n")
    result_lines = []
    hook_done = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Первая непустая строка после заголовка — хук → моноширинный
        if not hook_done and stripped and not stripped.startswith(("*", "#", "—", "-")):
            if len(stripped) > 20 and i < 4:
                line = f"`{stripped}`"
                hook_done = True
                result_lines.append(line)
                continue

        # Секционные разделители перед жирными заголовками
        if stripped.startswith("**") and stripped.endswith("**") and i > 0:
            if result_lines and result_lines[-1] != "":
                result_lines.append("")

        result_lines.append(line)

    # Spoiler на последнее непустое предложение
    result_lines = _add_spoiler_ending(result_lines, agent_key)

    return "\n".join(result_lines)


def _add_spoiler_ending(lines: list[str], agent_key: str) -> list[str]:
    """
    Прячет последнее предложение/строку под spoiler ||текст||.
    Работает только для постов и хуков — не для планировщика/карусели.
    """
    # Для этих агентов spoiler не нужен — структурированный контент
    skip_agents = {"tg_plan", "carousel", "profile", "competitor", "planner"}
    if agent_key in skip_agents:
        return lines

    # Ищем последнюю непустую строку
    last_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last_idx = i
            break

    if last_idx is None:
        return lines

    last = lines[last_idx].strip()

    # Слишком короткое или уже отформатированное — пропускаем
    if len(last) < 15 or last.startswith("`") or last.startswith("||"):
        return lines

    # Убираем уже существующие теги форматирования если есть
    clean = last.strip("*_`")
    if len(clean) > 10:
        lines[last_idx] = f"||{clean}||"

    return lines


# ── Inline Query handler ───────────────────────────────────────────────────────

async def handle_inline_query(update, context) -> None:
    """
    Пользователь пишет @МираBot тема поста — получает быстрый вариант.
    Работает из любого чата без перехода в бот.
    """
    from telegram import InlineQueryResultArticle, InputTextMessageContent
    import hashlib

    query = update.inline_query
    if not query:
        return

    user_id = query.from_user.id
    text    = query.query.strip()

    if len(text) < 3:
        # Подсказка когда запрос пустой
        hint = InlineQueryResultArticle(
            id="hint",
            title="Напиши тему поста или идею",
            description="Например: «5 ошибок начинающих коучей»",
            input_message_content=InputTextMessageContent(
                "_Напиши тему после @МираBot — и я дам быструю идею для поста._",
                parse_mode="Markdown",
            ),
        )
        await query.answer([hint], cache_time=5)
        return

    # Проверяем доступ
    try:
        from user_state import get_user_state, has_access
        state = await get_user_state(user_id)
        if not has_access(state):
            no_access = InlineQueryResultArticle(
                id="no_access",
                title="❌ Нет доступа",
                description="Активируй триал в боте",
                input_message_content=InputTextMessageContent(
                    "Открой @МираBot и активируй 7 дней бесплатно 👇",
                    parse_mode="Markdown",
                ),
            )
            await query.answer([no_access], cache_time=5)
            return
    except Exception:
        pass

    # Генерируем быстрый хук
    try:
        from llm import complete
        from db import get_profile
        profile = await get_profile(user_id)
        niche   = profile.get("niche", "")

        system = (
            "Ты — Мира, AI для контент-мейкеров. "
            "Дай 1 сильный хук для поста (одна строка, до 100 символов). "
            "Без вводных фраз. Только текст хука."
        )
        prompt = f"Тема: {text}\nНиша: {niche}"
        hook = await complete(system, prompt, temperature=0.9)
        hook = hook.strip().strip('"')[:200] if hook else text

        result_id = hashlib.md5(f"{user_id}:{text}".encode()).hexdigest()[:8]
        article = InlineQueryResultArticle(
            id=result_id,
            title=f"💡 {hook[:60]}",
            description="Хук от Миры — нажми чтобы вставить",
            input_message_content=InputTextMessageContent(
                f"*Хук для поста:*\n`{hook}`\n\n_Создано Мирой_ @МираBot",
                parse_mode="Markdown",
            ),
        )
        await query.answer([article], cache_time=30)
    except Exception as e:
        logger.error(f"[inline] error: {e}")
        await query.answer([], cache_time=5)


# ── TTS — результат голосом ───────────────────────────────────────────────────

async def send_as_voice(update, text: str, voice_name: str = "nova") -> bool:
    """
    Озвучивает текст через OpenAI TTS и присылает как голосовое.
    voice_name: alloy | echo | fable | onyx | nova | shimmer
    nova — женский, звучит естественно для женской аудитории.
    Возвращает True если успешно.
    """
    import os
    openai_key = os.environ.get("OPENAI_KEY", "")
    if not openai_key:
        return False

    # Обрезаем до 2000 символов — разумный лимит для TTS
    clean = _strip_markdown(text[:2000])
    if len(clean) < 20:
        return False

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {openai_key}"},
                json={
                    "model": "tts-1",
                    "input": clean,
                    "voice": voice_name,
                    "response_format": "ogg_opus",
                },
            )
            resp.raise_for_status()
            audio = resp.content

        chat = update.effective_chat
        await chat.send_voice(
            voice=audio,
            caption="🎙 Послушай как это звучит",
            duration=len(clean) // 15,  # грубая оценка длительности
        )
        return True
    except Exception as e:
        logger.warning(f"[media] TTS failed: {e}")
        return False


def _strip_markdown(text: str) -> str:
    """Убирает Markdown-теги для TTS."""
    import re
    text = re.sub(r"\*{1,2}(.*?)\*{1,2}", r"\1", text)
    text = re.sub(r"_{1,2}(.*?)_{1,2}", r"\1", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    text = re.sub(r"\|\|(.*?)\|\|", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    return text.strip()
