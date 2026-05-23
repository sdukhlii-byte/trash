import asyncio
import logging
import registry  # регистрирует все 9 агентов при импорте

from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

from lava_payments import setup_lava_webhook

from config import TELEGRAM_TOKEN, LLM_SEMAPHORE_SIZE, WEBHOOK_URL, PORT
from db import init_db, close_db
from handlers import (
    cmd_start, cmd_menu, cmd_clear, cmd_reset, cmd_subscribe, cmd_support,
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
            BotCommand("menu",      "☰ Главное меню"),
            BotCommand("start",     "🚀 Начать / перезапустить"),
            BotCommand("subscribe", "👤 Личный кабинет"),
            BotCommand("support",   "🆘 Поддержка"),
            BotCommand("clear",     "🗑 Очистить историю чата"),
            BotCommand("reset",     "♻️ Сбросить профиль"),
        ])
        logger.info("Bot commands registered")

        # Восстанавливаем дейли-задания пользователей
        from db import get_all_daily_users
        daily_users = await get_all_daily_users()
        for uid, settings in daily_users:
            try:
                await _schedule_daily(application, uid,
                                      settings.get("hour", 9), settings.get("minute", 0))
                logger.info(f"Daily job restored for user {uid}")
            except Exception as e:
                logger.warning(f"Could not restore daily job for {uid}: {e}")

        # Retention пуши — ежедневно + почасовые предупреждения о триале
        from retention import setup_retention_jobs
        setup_retention_jobs(application)
        logger.info("Retention jobs registered")

    async def post_shutdown(application: Application) -> None:
        await close_db()
        await close_http()
        logger.info("Bot shut down cleanly")

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("reset",     cmd_reset))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("support",   cmd_support))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    if WEBHOOK_URL:
        # ── Webhook mode (Railway / production) ──────────────────────────────
        logger.info(f"Starting webhook+aiohttp on port {PORT}: {WEBHOOK_URL}")

        aio_app = web.Application()

        # Lava.top вебхук
        setup_lava_webhook(aio_app, app.bot)

        # Telegram webhook endpoint
        async def tg_webhook(request: web.Request) -> web.Response:
            try:
                data = await request.json()
                update = Update.de_json(data, app.bot)
                await app.process_update(update)
            except Exception as e:
                logger.error(f"tg_webhook error: {e}", exc_info=True)
            return web.Response(status=200)

        aio_app.router.add_post("/webhook", tg_webhook)

        async def run():
            await app.initialize()
            await app.bot.set_webhook(
                url=WEBHOOK_URL,  # WEBHOOK_URL уже содержит /webhook
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            await app.start()

            runner = web.AppRunner(aio_app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", PORT)
            await site.start()

            logger.info(f"aiohttp listening on :{PORT}")
            logger.info(f"Telegram webhook: {WEBHOOK_URL}/webhook")
            logger.info(f"Lava webhook:     {WEBHOOK_URL}/lava/webhook")

            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                await app.stop()
                await app.shutdown()
                await runner.cleanup()

        asyncio.run(run())

    else:
        # ── Polling mode (local dev) ──────────────────────────────────────────
        logger.info("Starting polling (no WEBHOOK_URL set)")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )


if __name__ == "__main__":
    main()
