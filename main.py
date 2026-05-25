"""
main.py — точка входа.

Изменения v2:
- Webhook idempotency: update_id кэшируется в Redis (TTL 5 мин) → повторные
  доставки от Telegram не обрабатываются дважды.
- Graceful shutdown: закрываем httpx client и DB pool.
- Семафоры инициализируются до старта бота.
- Все хендлеры собраны здесь, логика — в handlers/*.
"""
import asyncio
import logging
import os

from aiohttp import web
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters,
)

from db import init_db, close_db, get_redis
from llm import init_http, close_http, init_semaphore
from config import (
    BOT_TOKEN, WEBHOOK_URL, WEBHOOK_SECRET,
    SEM_FAST_SIZE, SEM_HEAVY_SIZE,
)
from handlers.commands import (
    cmd_start, cmd_menu, cmd_clear, cmd_reset, cmd_subscribe, cmd_support, cmd_admin,
)
from handlers.messages import handle_text, handle_voice, handle_photo
from handlers.callbacks import callback

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_DEDUP_TTL = 300   # секунд — окно idempotency для webhook


# ── Application factory ────────────────────────────────────────────────────────

def build_app() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("reset",     cmd_reset))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("support",   cmd_support))
    app.add_handler(CommandHandler("admin",     cmd_admin))

    # Inline Query — вызов Миры из любого чата (@МираBot тема поста)
    from telegram.ext import InlineQueryHandler
    from ui.media import handle_inline_query
    app.add_handler(InlineQueryHandler(handle_inline_query))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE,                   handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback))

    # Error handler
    app.add_error_handler(_error_handler)

    return app


# ── Error handler ──────────────────────────────────────────────────────────────

async def _error_handler(update: object, ctx) -> None:
    from telegram.error import NetworkError, TimedOut, RetryAfter
    err = ctx.error
    if isinstance(err, (NetworkError, TimedOut)):
        return
    if isinstance(err, RetryAfter):
        await asyncio.sleep(err.retry_after + 1)
        return
    logger.error(f"Unhandled: {type(err).__name__}: {err}", exc_info=err)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message(
                "❌ Внутренняя ошибка. Попробуй ещё раз или напиши /menu"
            )
        except Exception:
            pass


# ── Webhook idempotency ────────────────────────────────────────────────────────

async def _is_duplicate(update_id: int) -> bool:
    """Возвращает True если update уже был обработан (повторная доставка от Telegram)."""
    try:
        r = await get_redis()
        key = f"bot:dedup:upd:{update_id}"
        added = await r.set(key, "1", ex=_DEDUP_TTL, nx=True)
        return not added   # nx=True: set returns None если ключ уже был
    except Exception as e:
        logger.warning(f"dedup redis error: {e}")
        return False   # при ошибке Redis — не блокируем обработку


# ── Webhook server ─────────────────────────────────────────────────────────────

async def _make_webhook_server(ptb_app: Application) -> web.Application:
    from lava_payments import lava_webhook_handler as payment_webhook

    web_app = web.Application()

    async def tg_webhook(request: web.Request) -> web.Response:
        # Проверяем секрет
        if WEBHOOK_SECRET:
            secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if secret != WEBHOOK_SECRET:
                logger.warning("Webhook: invalid secret token")
                return web.Response(status=403)

        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400)

        update_id = data.get("update_id")
        if update_id and await _is_duplicate(update_id):
            logger.info(f"[dedup] skipping duplicate update_id={update_id}")
            return web.Response(status=200)

        update = Update.de_json(data, ptb_app.bot)
        asyncio.create_task(ptb_app.process_update(update))
        return web.Response(status=200)

    web_app.router.add_post("/tg", tg_webhook)
    web_app.router.add_post("/webhook/tg", tg_webhook)   # alias — если WEBHOOK_URL включает /webhook
    web_app.router.add_post("/payment", payment_webhook)
    web_app.router.add_post("/lava/webhook", payment_webhook)  # alias из lava setup
    web_app.router.add_get("/health", lambda r: web.Response(text="ok"))

    return web_app


# ── Startup / Shutdown ─────────────────────────────────────────────────────────

async def _set_bot_commands(bot) -> None:
    commands = [
        BotCommand("start",     "Главное меню"),
        BotCommand("menu",      "Открыть меню"),
        BotCommand("clear",     "Очистить историю чата"),
        BotCommand("subscribe", "Управление подпиской"),
        BotCommand("support",   "Поддержка"),
        BotCommand("reset",     "Сбросить профиль"),
    ]
    try:
        await bot.set_my_commands(commands)
        logger.info("Bot commands set")
    except Exception as e:
        logger.warning(f"Could not set commands: {e}")


async def main() -> None:
    # 1. Инициализация HTTP и семафоров
    await init_http()
    init_semaphore(SEM_FAST_SIZE, SEM_HEAVY_SIZE)
    logger.info(f"Semaphores: fast={SEM_FAST_SIZE} heavy={SEM_HEAVY_SIZE}")

    # 2. База данных
    await init_db()

    # 2b. Миграции схемы — применяем новые SQL-файлы из migrations/sql/
    try:
        from migrations.runner import run as run_migrations
        await run_migrations()
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise  # не стартуем с непримененными миграциями — безопаснее

    # 3. Регистрируем всех агентов (ОБЯЗАТЕЛЬНО до build_app)
    import registry  # noqa: F401 — side-effect import, заполняет _REGISTRY
    logger.info(f"Agents registered: {len(__import__('agents')._REGISTRY)}")

    # 4. Строим PTB приложение
    ptb_app = build_app()

    await ptb_app.initialize()
    await ptb_app.start()
    await _set_bot_commands(ptb_app.bot)

    try:
        from db import get_all_daily_users
        from flows.misc import schedule_daily
        daily_users = await get_all_daily_users()
        for uid, settings in daily_users:
            try:
                await schedule_daily(
                    ptb_app, uid,
                    settings.get("hour", 9),
                    settings.get("minute", 0),
                )
            except Exception as e:
                logger.warning(f"Could not restore daily job for {uid}: {e}")
        logger.info(f"Restored {len(daily_users)} daily jobs")
    except Exception as e:
        logger.error(f"Daily job restore failed: {e}")

    # 6. Retention push — запускаем фоновый job
    try:
        from retention import setup_retention_jobs
        setup_retention_jobs(ptb_app)
        logger.info("Retention push scheduled")
    except Exception as e:
        logger.warning(f"Retention push schedule failed: {e}")

    # 6b. Hourly conversion nudges (DB-флаги, переживают рестарты)
    try:
        import datetime as _dt
        async def _run_hourly_conversion(ctx):
            from flows.conversion import run_hourly_conversion
            await run_hourly_conversion(ctx.bot)

        ptb_app.job_queue.run_repeating(
            callback=_run_hourly_conversion,
            interval=3600,
            first=300,  # первый запуск через 5 мин после старта
            name="hourly_conversion",
        )
        logger.info("Hourly conversion job scheduled")
    except Exception as e:
        logger.warning(f"Hourly conversion job failed: {e}")

    # 6c. Batch pre-generation дайджестов в 06:00 UTC
    # Генерирует дайджест для всех подписчиков у которых включён daily
    # До того как они просыпаются — к моменту доставки уже готово из кэша
    try:
        import datetime as _dt

        async def _batch_pregen_digests(ctx) -> None:
            from db import _get_pool, kv_get
            from flows.misc import pregen_digest
            import asyncio as _aio
            logger.info("[daily batch] starting pre-generation")
            pool = _get_pool()
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT DISTINCT user_id FROM daily_settings WHERE enabled=true"
                    )
                user_ids = [r["user_id"] for r in rows]
            except Exception as e:
                logger.error(f"[daily batch] fetch users failed: {e}")
                return

            # Генерируем по 5 параллельно — не перегружаем семафор
            sem = _aio.Semaphore(5)
            async def _pregen_one(uid: int):
                async with sem:
                    await pregen_digest(uid)
                    await _aio.sleep(0.5)  # мягкий rate-limit

            await _aio.gather(*[_pregen_one(uid) for uid in user_ids],
                              return_exceptions=True)
            logger.info(f"[daily batch] pre-generated {len(user_ids)} digests")

        ptb_app.job_queue.run_daily(
            callback=_batch_pregen_digests,
            time=_dt.time(hour=6, minute=0, tzinfo=_dt.timezone.utc),
            name="daily_digest_batch",
        )
        logger.info("Daily digest batch job scheduled at 06:00 UTC")
    except Exception as e:
        logger.warning(f"Daily digest batch job failed: {e}")

    # Follow-up jobs — не восстанавливаем при рестарте (TTL 72h, Railway перезапускается редко)
    # Новые followup планируются при каждом save_result() через ptb_app
    logger.info("Follow-up system ready (scheduled per-generation)")

    # 7. Webhook или polling
    port = int(os.environ.get("PORT", 8080))
    use_webhook = bool(WEBHOOK_URL)

    if use_webhook:
        # Устанавливаем webhook
        await ptb_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/tg",
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["message", "callback_query", "inline_query"],
            drop_pending_updates=True,
        )
        logger.info(f"Webhook set: {WEBHOOK_URL}/tg")

        web_app = await _make_webhook_server(ptb_app)
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"Webhook server listening on port {port}")

        # Ждём сигнала завершения
        stop_event = asyncio.Event()
        import signal
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        await stop_event.wait()
        logger.info("Shutdown signal received")

        await runner.cleanup()
    else:
        logger.info("Starting polling mode")
        await ptb_app.bot.delete_webhook(drop_pending_updates=True)
        await ptb_app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )

        stop_event = asyncio.Event()
        import signal
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        await stop_event.wait()

    # 8. Graceful shutdown
    logger.info("Shutting down...")
    await ptb_app.updater.stop() if not use_webhook else None
    await ptb_app.stop()
    await ptb_app.shutdown()
    await close_db()
    await close_http()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
