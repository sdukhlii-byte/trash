import logging
import registry  # регистрирует все 9 агентов при импорте

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

from config import TELEGRAM_TOKEN, LLM_SEMAPHORE_SIZE, WEBHOOK_URL, PORT
from db import init_db, close_db
from handlers import (
    cmd_start, cmd_menu, cmd_clear, cmd_reset,
    callback, handle_text, handle_voice, handle_photo,
    error_handler,
    _schedule_daily,
)
from llm import init_semaphore, init_http, close_http

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(15)
        .read_timeout(60)
        .write_timeout(30)
        .pool_timeout(10)
        .build()
    )

    async def post_init(application: Application) -> None:
        import agents as ag_module
        from telegram import BotCommand
        try:
            await init_db()
            logger.info("DB init OK")
        except Exception as e:
            logger.error(f"DB init FAILED: {e}", exc_info=True)
        try:
            await init_http()
            logger.info("HTTP client init OK")
        except Exception as e:
            logger.error(f"HTTP init FAILED: {e}", exc_info=True)
        init_semaphore(LLM_SEMAPHORE_SIZE)
        logger.info(f"LLM semaphore ready (size={LLM_SEMAPHORE_SIZE})")
        logger.info(f"Agents registered: {[s.key for s in ag_module.all_specs()]}")

        # Регистрируем команды — появятся в меню "/" в Telegram
        await application.bot.set_my_commands([
            BotCommand("menu",  "☰ Главное меню"),
            BotCommand("start", "🚀 Начать / перезапустить"),
            BotCommand("clear", "🗑 Очистить историю чата"),
            BotCommand("reset", "♻️ Сбросить профиль"),
        ])
        logger.info("Bot commands registered")

        # Восстанавливаем дейли-задания
        from db import get_all_daily_users
        daily_users = await get_all_daily_users()
        for uid, settings in daily_users:
            try:
                await _schedule_daily(application, uid,
                                      settings.get("hour", 9), settings.get("minute", 0))
                logger.info(f"Daily job restored for user {uid}")
            except Exception as e:
                logger.warning(f"Could not restore daily job for {uid}: {e}")

    async def post_shutdown(application: Application) -> None:
        await close_db()
        await close_http()
        logger.info("Bot shut down cleanly")

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("menu",   cmd_menu))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(CommandHandler("reset",  cmd_reset))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    if WEBHOOK_URL:
        # ── Webhook mode (Railway / production) ──────────────────────────────
        logger.info(f"Starting webhook on port {PORT}: {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        # ── Polling mode (local dev) ──────────────────────────────────────────
        logger.info("Starting polling (no RAILWAY_PUBLIC_DOMAIN set)")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )


if __name__ == "__main__":
    main()
