from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import db, keyboards as kb, pricing, seller
from app.deps import lzt
from app.handlers.menu import show
from app.utils import tg
from app.utils.texts import prices_text, ready_detail_text, ready_row, ready_text

router = Router()


async def _render(bot, chat_id: int, tg_id: int, edit=None):
    s = await db.get_settings(tg_id)
    count = await db.ready_count(tg_id)
    stats = await db.sale_stats(tg_id)
    text = ready_text(s, count, stats)
    markup = kb.ready(s, count)
    if edit is not None:
        await show(edit, text, markup)
    else:
        await tg.send(bot, chat_id, text, markup)


@router.message(Command("ready"))
async def cmd_ready(m: Message):
    await _render(m.bot, m.chat.id, m.from_user.id)


@router.callback_query(F.data == "m:ready")
async def cb_ready(cq: CallbackQuery):
    await _render(cq.bot, cq.message.chat.id, cq.from_user.id, cq)
    await cq.answer()


@router.callback_query(F.data == "rd:list")
async def cb_list(cq: CallbackQuery):
    rows = await db.ready_purchases(cq.from_user.id, 30)
    if not rows:
        return await cq.answer("nothing ready", show_alert=True)
    body = "<b>Ready</b> · " + str(len(rows)) + "\n\n" + "\n\n".join(ready_row(p) for p in rows[:15])
    await show(cq, body, kb.ready_list(rows))
    await cq.answer()


@router.callback_query(F.data == "rd:prices")
async def cb_prices(cq: CallbackQuery):
    rows = await pricing.best_table(cq.from_user.id)
    await show(cq, prices_text(rows), kb.back("m:ready"))
    await cq.answer()


@router.callback_query(F.data == "rd:all")
async def cb_all(cq: CallbackQuery):
    rows = await db.sell_queue(cq.from_user.id, 100)
    if not rows:
        return await cq.answer("nothing sellable", show_alert=True)
    for p in rows:
        await db.set_sell(p["_id"], "queued", 0)
    await cq.answer(f"queued {len(rows)}")
    await _render(cq.bot, cq.message.chat.id, cq.from_user.id, cq)


@router.callback_query(F.data.startswith("rv:"))
async def cb_view(cq: CallbackQuery):
    p = await db.get_purchase(cq.data.split(":", 1)[1])
    if not p or p["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    await show(cq, ready_detail_text(p), kb.ready_detail(p))
    await cq.answer()


@router.callback_query(F.data.startswith("sl:"))
async def cb_sell(cq: CallbackQuery):
    p = await db.get_purchase(cq.data.split(":", 1)[1])
    if not p or p["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    if p.get("status") != "ready":
        return await cq.answer(f"status {p.get('status')}", show_alert=True)
    await cq.answer("selling…")
    ok, note = await seller.manual(cq.bot, lzt, p)
    if not ok:
        await tg.send(cq.bot, cq.from_user.id, f"⚠️ sell failed · {tg.esc(note)}")
    p = await db.get_purchase(p["_id"])
    if p:
        await show(cq, ready_detail_text(p) + f"\n\n💱 {tg.esc(note)}", kb.ready_detail(p))


@router.message(Command("sellall"))
async def cmd_sellall(m: Message):
    rows = await db.sell_queue(m.from_user.id, 100)
    if not rows:
        return await m.answer("nothing sellable")
    for p in rows:
        await db.set_sell(p["_id"], "queued", 0)
    await m.answer(f"queued {len(rows)}")