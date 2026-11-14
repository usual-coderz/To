import asyncio
import logging
import re
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import CallbackQuery, ErrorEvent, Message

from app import db, receiver_hub, seller
from app.config import BOT_TOKEN, MONGO_DB, MONGO_URI, OWNER_IDS, PURCHASE_KEEP_DAYS
from app.deps import lzt
from app.engine import buy_loop, cleaner_loop, drain_bg, price_loop
from app.handlers import ROUTERS
from app.handlers.menu import allowed
from app.scheduler import loop as chart_loop

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


def _mask(uri: str) -> str:
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", uri or "")


async def access_mw(handler, event, data):
    user = getattr(event, "from_user", None)
    if user is None:
        return await handler(event, data)
    if not allowed(user.id):
        if isinstance(event, CallbackQuery):
            await event.answer(f"Not allowed. id {user.id}", show_alert=True)
        elif isinstance(event, Message):
            await event.answer(f"Not allowed. id <code>{user.id}</code>")
        return None
    try:
        await db.ensure_user(user.id, user.username or "")
    except Exception as e:
        log.warning(f"ensure_user {user.id}: {e}")
    return await handler(event, data)


async def _supervise(name: str, factory, *args):
    while True:
        try:
            await factory(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(f"{name} crashed, restarting in 10s")
            await asyncio.sleep(10)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set")
    if not OWNER_IDS:
        log.warning("OWNER_IDS empty — every user is allowed")
    log.info(f"mongo: {_mask(MONGO_URI)} db={MONGO_DB}")

    await db.init_db()
    for oid in OWNER_IDS:
        await db.ensure_user(oid)
    await receiver_hub.start()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.message.outer_middleware(access_mw)
    dp.callback_query.outer_middleware(access_mw)

    @dp.errors()
    async def on_error(event: ErrorEvent):
        update = getattr(event, "update", None)
        uid = getattr(update, "update_id", "?")
        log.error(f"update {uid}: {event.exception!r}")
        return True

    for r in ROUTERS:
        dp.include_router(r)

    tasks = [
        asyncio.create_task(_supervise("buy_loop", buy_loop, bot, lzt)),
        asyncio.create_task(_supervise("cleaner_loop", cleaner_loop, bot, lzt)),
        asyncio.create_task(_supervise("sell_worker", seller.worker, bot, lzt)),
        asyncio.create_task(_supervise("price_loop", price_loop)),
        asyncio.create_task(_supervise("chart_loop", chart_loop, bot)),
    ]

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    log.info("bot up")
    polling = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(),
                         drop_pending_updates=True))
    waiter = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({polling, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        log.info("shutting down")
        polling.cancel()
        waiter.cancel()
        await asyncio.gather(polling, waiter, return_exceptions=True)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await seller.drain()
        await drain_bg()
        try:
            await db.prune_purchases(PURCHASE_KEEP_DAYS)
        except Exception as e:
            log.warning(f"prune: {e}")
        await receiver_hub.stop()
        await lzt.close()
        await db.close_db()
        await bot.session.close()
        log.info("stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass