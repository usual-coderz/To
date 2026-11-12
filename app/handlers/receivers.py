from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import db, keyboards as kb, pricing, receivers
from app.handlers.menu import show
from app.utils import tg
from app.utils.texts import prices_text, receiver_detail_text, receivers_text

router = Router()

PENDING: dict[int, str] = {}


async def _render(bot, chat_id: int, tg_id: int, edit=None):
    rows = await db.list_receivers(tg_id)
    text = receivers_text(rows)
    markup = kb.receivers(rows)
    if edit is not None:
        await show(edit, text, markup)
    else:
        await tg.send(bot, chat_id, text, markup)


@router.message(Command("receivers"))
async def cmd_receivers(m: Message):
    await _render(m.bot, m.chat.id, m.from_user.id)


@router.callback_query(F.data == "m:receivers")
async def cb_receivers(cq: CallbackQuery):
    await _render(cq.bot, cq.message.chat.id, cq.from_user.id, cq)
    await cq.answer()


@router.callback_query(F.data == "r:add")
async def cb_add(cq: CallbackQuery):
    PENDING[cq.from_user.id] = "add"
    await show(cq, "<b>Add receiver</b>\nsend @username · /cancel",
               kb.back("m:receivers"))
    await cq.answer()


@router.callback_query(F.data == "r:sync")
async def cb_sync(cq: CallbackQuery):
    await cq.answer("syncing…")
    out = await receivers.sync(cq.from_user.id)
    rows = await db.list_receivers(cq.from_user.id)
    text = (f"sync: receivers {out['receivers']} · caps {out['caps']}"
            f" · prices {out['prices']} · errors {out['errors']}\n\n"
            + receivers_text(rows))
    await show(cq, text, kb.receivers(rows))


@router.callback_query(F.data.startswith("rv:"))
async def cb_detail(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    r = await db.get_receiver(rid)
    if not r or r["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    count = len((await db.latest_prices(cq.from_user.id, pricing.PRICE_TTL)))
    await show(cq, receiver_detail_text(r, count), kb.receiver_detail(r))
    await cq.answer()


@router.callback_query(F.data.startswith("rc:"))
async def cb_cap(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    await cq.answer("checking…")
    cap = await receivers.capacity(cq.from_user.id, rid)
    r = await db.get_receiver(rid)
    if not r:
        return
    await show(cq, receiver_detail_text(r, cap), kb.receiver_detail(r))


@router.callback_query(F.data.startswith("rp:"))
async def cb_prices(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    r = await db.get_receiver(rid)
    if not r:
        return await cq.answer("gone", show_alert=True)
    rows = await pricing.offers_for(cq.from_user.id, r.get("username") and "")
    offers = await pricing.offers_map(cq.from_user.id)
    mine = [{"country": cc, "offers": [o for o in v if o["receiver_id"] == rid]}
            for cc, v in offers.items()]
    mine = [m for m in mine if m["offers"]]
    mine.sort(key=lambda m: -m["offers"][0]["price"])
    await show(cq, prices_text(mine), kb.receiver_detail(r))
    await cq.answer()


@router.callback_query(F.data.startswith("rs:"))
async def cb_confirm(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    r = await db.get_receiver(rid)
    if not r:
        return await cq.answer("gone", show_alert=True)
    await db.set_receiver(rid, confirm_seconds=0)
    await show(cq, receiver_detail_text(r), kb.receiver_detail(r))
    await cq.answer("confirm reset to auto")


@router.callback_query(F.data.startswith("rt:"))
async def cb_toggle(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    if not await receivers.toggle(cq.from_user.id, rid):
        return await cq.answer("gone", show_alert=True)
    r = await db.get_receiver(rid)
    await show(cq, receiver_detail_text(r), kb.receiver_detail(r))
    await cq.answer("saved")


@router.callback_query(F.data.startswith("rd:"))
async def cb_drop(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    await receivers.remove(cq.from_user.id, rid)
    await _render(cq.bot, cq.message.chat.id, cq.from_user.id, cq)
    await cq.answer("deleted")


@router.message(F.text)
async def on_text(m: Message):
    if PENDING.get(m.from_user.id) != "add":
        return
    if (m.text or "").startswith("/"):
        return
    parts = (m.text or "").split()
    if not parts:
        return
    key = parts[0].lstrip("@")
    title = " ".join(parts[1:])[:40]
    rid = await receivers.add(m.from_user.id, key, title)
    if not rid:
        return await m.answer("bad username · /cancel")
    PENDING.pop(m.from_user.id, None)
    await m.answer(f"✅ added @{key}")
    await _render(m.bot, m.chat.id, m.from_user.id)