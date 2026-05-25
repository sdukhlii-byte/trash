"""
image_registry.py — реестр Telegram file_id для изображений.

Заполни file_id запустив: python scripts/upload_images.py

После заполнения:
1. Удали все .png файлы из репозитория
2. Замени все вызовы _send_photo(update, "posti4.png", ...) на send_registered_photo(...)

Telegram кэширует изображения по file_id → нулевой трафик, мгновенная доставка.
"""
import logging
from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)

# Заполни после запуска scripts/upload_images.py
IMAGE_FILE_IDS: dict[str, str] = {
    # "posti4.png": "AgACAgIAAxkBAAI...",
    # "posti9.png": "AgACAgIAAxkBAAI...",
    # "posti8.png": "AgACAgIAAxkBAAI...",
    # ... и т.д.
}


async def send_registered_photo(
    bot: Bot,
    chat_id: int,
    filename: str,
    caption: str = "",
    reply_markup=None,
    parse_mode: str = "Markdown",
) -> bool:
    """
    Отправляет фото по file_id (если зарегистрировано).
    Возвращает True при успехе, False если file_id не найден.

    Использование:
        ok = await send_registered_photo(ctx.bot, chat_id, "posti4.png", caption, kb)
        if not ok:
            await send(update, caption, reply_markup=kb)  # fallback на текст
    """
    file_id = IMAGE_FILE_IDS.get(filename)
    if not file_id:
        logger.debug(f"No file_id for {filename} — skipping photo send")
        return False

    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=caption,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        return True
    except TelegramError as e:
        logger.warning(f"send_registered_photo({filename}): {e}")
        return False
