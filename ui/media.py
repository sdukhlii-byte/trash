"""
ui/media.py — визуальная магия Telegram.

v3: GIF через URL (не нужны файлы в репо) + стикеры по file_id.

GIF-файлы заливаются на Imgbb/Giphy и указываются URL — никакого хранилища.
Стикеры — Telegram file_id (получаешь один раз, кэшируется навсегда).
"""
from __future__ import annotations
import logging
import os
import re

logger = logging.getLogger(__name__)

# ── GIF через URL ─────────────────────────────────────────────────────────────
# Загрузи GIF на https://imgbb.com или https://ezgif.com/upload (бесплатно)
# и вставь прямые ссылки на .gif файл.
# Формат: "https://i.ibb.co/XXXXX/name.gif"
#
# КАК ПОЛУЧИТЬ URL:
# 1. imgbb.com → загрузи GIF → нажми "Копировать прямую ссылку"
# 2. Ссылка должна заканчиваться на .gif
#
# ПОКА URL НЕ ЗАПОЛНЕНЫ — бот тихо пропускает GIF (graceful degradation)

_GIF_URLS: dict[str, str] = {
    "trial_activated":   "https://i.ibb.co/n8qtKcf8/trial-activated.gif",
    "first_generation":  "https://i.ibb.co/HDtzRXcS/Not-Bad-Pop-Tv-GIF-by-Schitt-s-Creek.gif",
    "voice_level_up":    "https://i.ibb.co/xthRszmr/voice-level-up.gif",
    "subscription_paid": "https://i.ibb.co/ynyf4J93/subscription-paid.gif",
    "streak_7":          "https://i.ibb.co/mCTcYmkq/streak-7.gif",
}

# Fallback: локальные файлы если есть (для тестов)
_ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "gifs"
)

_GIF_CAPTIONS: dict[str, str] = {
    "trial_activated":   "🎉 7 дней — твои! Давай начнём.",
    "first_generation":  "✨ Первый материал готов. Добро пожаловать в клуб.",
    "voice_level_up":    "🎯 Голос Миры стал точнее — тексты будут ещё лучше.",
    "subscription_paid": "❤️ Ты с нами. Это только начало.",
    "streak_7":          "🔥 7 дней подряд. Это уже привычка.",
}

# ── Стикеры по file_id ────────────────────────────────────────────────────────
# КАК ПОЛУЧИТЬ file_id:
# 1. Отправь стикер боту @userinfobot — он вернёт file_id
# 2. Или: переслать стикер в @RawDataBot
# Стикеры из публичных паков: @NelaStickers, @CuteAnimals и т.д.

_STICKER_IDS: dict[str, str] = {
    # Ключ → момент, значение → Telegram file_id стикера
    # "greeting":         "CAACAgIAAxkBAAFKpI5qFMCSk_tWhwwCA66M3JUJpiWxEwAC5C8AAhMNcUvuzRMluGpnhzsE",  # приветствие
    # "thinking":         "CAACAgQAAxkBAAFKpJBqFMDLGOQPOdweTsTxPyg7gxNUnQAC5REAAvMQ-FBfSb9o5UeImDsE",  # думаю...
    # "done":             "CAACAgIAAxkBAAFKpIBqFMAZI7L6qjp7Ccc6i-bKtVOBxwACN24AArarAUtyPWFAnRi3ljsE",  # готово!
    # "voice_yes":        "CAACAgQAAxkBAAFKpJdqFMEX2Jsmb18NFWiW0fKVuEgPJAACZgAD133WJbaWlBdQOvxsOwQ",  # одобрение голоса
    # "trial_welcome":    "CAACAgIAAxkBAAFKpJ1qFMF12hxpMdbawnHI3G9WLxyeFwACVycAAgcLgUruw48OoJ7eaDsE",  # добро пожаловать
    # "level_up":         "CAACAgIAAxkBAAFKpKBqFMG_wKtKnyTUJ7jqD0vS6vYVbgAC00sAAp7OCwABm_7lR2o7-eE7BA",  # новый уровень
}


async def send_gif(update, event: str, extra_caption: str = "") -> bool:
    """
    Отправляет GIF для события.
    Сначала пробует URL, затем локальный файл.
    Если ничего нет — возвращает False (caller показывает текст).
    """
    caption = _GIF_CAPTIONS.get(event, "")
    if extra_caption:
        caption = f"{caption}\n\n{extra_caption}" if caption else extra_caption

    chat = update.effective_chat

    # 1. Пробуем URL
    url = _GIF_URLS.get(event)
    if url:
        try:
            await chat.send_animation(
                animation=url,
                caption=caption,
                parse_mode="Markdown",
            )
            return True
        except Exception as e:
            logger.warning(f"[media] GIF URL failed ({event}): {e}")

    # 2. Пробуем локальный файл
    local_path = os.path.join(_ASSETS_DIR, f"{event}.gif")
    if os.path.exists(local_path):
        try:
            with open(local_path, "rb") as f:
                await chat.send_animation(
                    animation=f,
                    caption=caption,
                    parse_mode="Markdown",
                )
            return True
        except Exception as e:
            logger.warning(f"[media] GIF local failed ({event}): {e}")

    return False


async def send_sticker(update, event: str) -> bool:
    """
    Отправляет стикер для события.
    Возвращает False если file_id не задан.
    """
    file_id = _STICKER_IDS.get(event)
    if not file_id:
        return False
    try:
        await update.effective_chat.send_sticker(sticker=file_id)
        return True
    except Exception as e:
        logger.warning(f"[media] sticker failed ({event}): {e}")
        return False


async def send_visual(update, event: str, caption: str = "") -> bool:
    """
    Универсальная точка: сначала стикер, потом GIF.
    Удобно вызывать когда не важно что именно придёт.
    """
    if await send_sticker(update, event):
        return True
    return await send_gif(update, event, caption)


# ── Реакции ───────────────────────────────────────────────────────────────────

async def set_reaction(bot, chat_id: int, message_id: int, emoji: str = "❤") -> None:
    """Ставит реакцию на сообщение. PTB 21.6+"""
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
    """❤️ на сообщение пользователя когда нажал 'Звучит как я'."""
    try:
        query   = update.callback_query
        chat_id = query.message.chat_id
        msg_id  = query.message.message_id
        for candidate_id in [msg_id, msg_id - 1, msg_id - 2]:
            try:
                await set_reaction(bot, chat_id, candidate_id, "❤")
                return
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"[media] react_to_voice_feedback failed: {e}")


# ── Форматирование результатов ────────────────────────────────────────────────

def format_result(text: str, agent_key: str = "") -> str:
    """
    Улучшает форматирование результата:
    - Первая значимая строка → моноширинный хук
    - Spoiler на последнюю строку (для постов)
    - Пустые строки перед жирными заголовками
    """
    if not text or not text.strip():
        return text

    lines  = text.split("\n")
    result = []
    hook_done = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Хук — первая длинная непустая строка без спецсимволов
        if not hook_done and stripped and not stripped.startswith(("*", "#", "—", "-", "•")):
            if len(stripped) > 25 and i < 5:
                line      = f"`{stripped}`"
                hook_done = True
                result.append(line)
                continue

        # Разделитель перед жирными заголовками
        if stripped.startswith("**") and stripped.endswith("**") and i > 0:
            if result and result[-1] != "":
                result.append("")

        result.append(line)

    # Spoiler на последнюю строку (только для постов, не структурного контента)
    _NO_SPOILER = {"tg_plan", "carousel", "profile", "competitor"}
    if agent_key not in _NO_SPOILER:
        result = _add_spoiler_ending(result)

    return "\n".join(result)


def _add_spoiler_ending(lines: list[str]) -> list[str]:
    last_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last_idx = i
            break
    if last_idx is None:
        return lines
    last = lines[last_idx].strip()
    if len(last) < 15 or last.startswith("`") or last.startswith("||"):
        return lines
    clean = last.strip("*_`")
    if len(clean) > 10:
        lines[last_idx] = f"||{clean}||"
    return lines


# ── Inline Query ──────────────────────────────────────────────────────────────

async def handle_inline_query(update, context) -> None:
    """@МираBot тема поста — возвращает хук прямо в чат."""
    from telegram import InlineQueryResultArticle, InputTextMessageContent
    import hashlib

    query = update.inline_query
    if not query:
        return

    user_id = query.from_user.id
    text    = query.query.strip()

    if len(text) < 3:
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

    try:
        from user_state import get_user_state, has_access
        state = await get_user_state(user_id)
        if not has_access(state):
            no_access = InlineQueryResultArticle(
                id="no_access",
                title="❌ Нет доступа",
                description="Активируй триал в боте",
                input_message_content=InputTextMessageContent(
                    "Открой бота и активируй 7 дней бесплатно 👇"
                ),
            )
            await query.answer([no_access], cache_time=5)
            return
    except Exception:
        pass

    try:
        from llm import complete
        from db import get_profile
        profile = await get_profile(user_id)
        hook = await complete(
            "Ты — Мира, AI для контент-мейкеров. Дай 1 сильный хук (одна строка, до 100 символов). Без вводных. Только текст хука.",
            f"Тема: {text}\nНиша: {profile.get('niche', '')}",
            temperature=0.9,
        )
        hook = hook.strip().strip('"')[:200]
        result_id = hashlib.md5(f"{user_id}:{text}".encode()).hexdigest()[:8]
        article = InlineQueryResultArticle(
            id=result_id,
            title=f"💡 {hook[:60]}",
            description="Хук от Миры — нажми чтобы вставить",
            input_message_content=InputTextMessageContent(
                f"*Хук:*\n`{hook}`\n\n_Создано Мирой_",
                parse_mode="Markdown",
            ),
        )
        await query.answer([article], cache_time=30)
    except Exception as e:
        logger.error(f"[inline] error: {e}")
        await query.answer([], cache_time=5)


# ── TTS ───────────────────────────────────────────────────────────────────────

async def send_as_voice(update, text: str, voice_name: str = "nova") -> bool:
    openai_key = os.environ.get("OPENAI_KEY", "")
    if not openai_key:
        return False
    clean = _strip_markdown(text[:2000])
    if len(clean) < 20:
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {openai_key}"},
                json={"model": "tts-1", "input": clean, "voice": voice_name,
                      "response_format": "ogg_opus"},
            )
            resp.raise_for_status()
            audio = resp.content
        await update.effective_chat.send_voice(
            voice=audio,
            caption="🎙 Послушай как это звучит",
        )
        return True
    except Exception as e:
        logger.warning(f"[media] TTS failed: {e}")
        return False


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\*{1,2}(.*?)\*{1,2}", r"\1", text)
    text = re.sub(r"_{1,2}(.*?)_{1,2}", r"\1", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    text = re.sub(r"\|\|(.*?)\|\|", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    return text.strip()
