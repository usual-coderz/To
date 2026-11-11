import asyncio
import logging
import re
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import CallbackQuery, ErrorEvent, Message

from app import db
from app.config import BOT_TOKEN, MONGO_DB, MONGO_URI, OWNER_IDS
from app.deps import lzt
from app.engine import buy_loop, drain_bg, expiry_loop
from app.handlers import ROUTERS
from app.handlers.menu import allowed

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


async def _supervise(name: str, factory, bot, *args):
    while True:
        try:
            await factory(bot, *args)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(f"{name} crashed, restarting in 10s")
            await asyncio.sleep(10)


async def main():
    if not BOT_TOKEN:
        sys.exit("BOT_TOKEN is not set")
    log.info(f"mongo: {_mask(MONGO_URI)} db={MONGO_DB}")

    await db.init_db()
    for oid in OWNER_IDS:
        await db.ensure_user(oid)

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.message.outer_middleware(access_mw)
    dp.callback_query.outer_middleware(access_mw)

    @dp.errors()
    async def on_error(event: ErrorEvent):
        log.exception(f"update {event.update_id}: {event.exception}")
        return True

    for r in ROUTERS:
        dp.include_router(r)

    tasks = [asyncio.create_task(_supervise("buy_loop", buy_loop, bot, lzt)),
             asyncio.create_task(_supervise("expiry_loop", expiry_loop, bot, lzt))]

    log.info("bot up")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("shutting down")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await drain_bg()
        await lzt.close()
        await db.close_db()
        await bot.session.close()
        log.info("stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass