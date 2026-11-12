from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import db, keyboards as kb, pricing, seller
from app.deps import lzt
from app.handlers.menu import show
from app.utils import tg
from app.utils.texts import prices_text, sell_menu_text

router = Router()


async def _render(bot, chat_id: int, tg_id: int, edit=None):
    s = await db.get_settings(tg_id)
    ready = await db.ready_purchases(tg_id, 200)
    text = sell_menu_text(s, len(ready))
    markup = kb.sell_menu(s, len(ready))
    if edit is not None:
        await show(edit, text, markup)
    else:
        await tg.send(bot, chat_id, text, markup)


@router.message(Command("sell"))
async def cmd_sell(m: Message):
    await _render(m.bot, m.chat.id, m.from_user.id)


@router.callback_query(F.data == "m:sell")
async def cb_sell(cq: CallbackQuery):
    await _render(cq.bot, cq.message.chat.id, cq.from_user.id, cq)
    await cq.answer()


@router.callback_query(F.data == "sl:prices")
async def cb_prices(cq: CallbackQuery):
    rows = await pricing.best_table(cq.from_user.id)
    await show(cq, prices_text(rows), kb.back("m:sell"))
    await cq.answer()


@router.callback_query(F.data == "sl:all")
async def cb_all(cq: CallbackQuery):
    rows = await seller.sellable(cq.from_user.id)
    if not rows:
        return await cq.answer("nothing ready", show_alert=True)
    await cq.answer(f"queued {len(rows)}")
    for p in rows:
        seller.schedule(cq.bot, lzt, p)
    await _render(cq.bot, cq.message.chat.id, cq.from_user.id, cq)


@router.message(Command("sellall"))
async def cmd_sellall(m: Message):
    rows = await seller.sellable(m.from_user.id)
    if not rows:
        return await m.answer("nothing ready")
    for p in rows:
        seller.schedule(m.bot, lzt, p)
    await m.answer(f"queued {len(rows)}")