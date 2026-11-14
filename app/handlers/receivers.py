from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import db, keyboards as kb, pricing, receivers
from app.handlers import state
from app.handlers.menu import show
from app.utils import tg
from app.utils.texts import (caps_text, extracted_text, prices_text,
                             receiver_detail_text, receivers_text)
from app.utils.timeutil import fmt_dt

router = Router()

RAW_LIMIT = 1200


def _raw(out: dict) -> str:
    body = out.get("raw") or "empty"
    return f"<b>Raw</b> · {out.get('replies', 0)} reply\n{tg.pre(body[:RAW_LIMIT])}"


async def _render(bot, chat_id: int, tg_id: int, edit=None):
    rows = await db.list_receivers(tg_id)
    text = receivers_text(rows)
    markup = kb.receivers(rows)
    if edit is not None:
        await show(edit, text, markup)
    else:
        await tg.send(bot, chat_id, text, markup)


async def _detail(cq: CallbackQuery, rid: str) -> dict | None:
    r = await db.get_receiver(rid)
    if not r or r["tg_id"] != cq.from_user.id:
        await cq.answer("gone", show_alert=True)
        return None
    return r


@router.message(Command("receivers"))
async def cmd_receivers(m: Message):
    await _render(m.bot, m.chat.id, m.from_user.id)


@router.callback_query(F.data == "m:receivers")
async def cb_receivers(cq: CallbackQuery):
    state.clear(cq.from_user.id)
    await _render(cq.bot, cq.message.chat.id, cq.from_user.id, cq)
    await cq.answer()


@router.callback_query(F.data == "r:add")
async def cb_add(cq: CallbackQuery):
    state.set(cq.from_user.id, "receiver", "add")
    await show(cq, "<b>Add receiver</b>\nsend @username · /cancel", kb.back("m:receivers"))
    await cq.answer()


@router.callback_query(F.data == "r:warm")
async def cb_warm(cq: CallbackQuery):
    await cq.answer("warming…")
    done = await receivers.warm(cq.from_user.id)
    rows = await db.list_receivers(cq.from_user.id)
    await show(cq, f"/start sent to {done}\n\n" + receivers_text(rows), kb.receivers(rows))


@router.callback_query(F.data == "r:sync")
async def cb_sync(cq: CallbackQuery):
    await cq.answer("syncing…")
    out = await receivers.sync(cq.from_user.id)
    rows = await db.list_receivers(cq.from_user.id)
    text = (f"sync: receivers {out['receivers']} · caps {out['caps']}"
            f" · prices {out['prices']} · errors {out['errors']}\n\n"
            + receivers_text(rows))
    if out.get("empty"):
        text += "\n\n" + "\n".join(tg.esc(e) for e in out["empty"])
    await show(cq, text, kb.receivers(rows))


@router.callback_query(F.data.startswith("rcv:"))
async def cb_detail(cq: CallbackQuery):
    r = await _detail(cq, cq.data.split(":", 1)[1])
    if not r:
        return
    count = len(await pricing.receiver_table(cq.from_user.id, r["_id"]))
    await show(cq, receiver_detail_text(r, count), kb.receiver_detail(r))
    await cq.answer()


@router.callback_query(F.data.startswith("rcp:"))
async def cb_cap(cq: CallbackQuery):
    r = await _detail(cq, cq.data.split(":", 1)[1])
    if not r:
        return
    if r.get("kind") == "lion":
        return await cq.answer("lion uses the price link", show_alert=True)
    await cq.answer("/cap sent, reading…")
    out = await receivers.capacity(cq.from_user.id, r["_id"])
    r = await db.get_receiver(r["_id"])
    if not r:
        return
    head = receiver_detail_text(r, len(out["offers"]))
    note = "no reply" if not out["replies"] else f"cap {out['cap']} · parsed {len(out['offers'])}"
    body = extracted_text(out["offers"], r["username"]) if out["offers"] else _raw(out)
    await show(cq, f"{head}\n\n🔄 {note}\n\n{body}", kb.receiver_detail(r))


@router.callback_query(F.data.startswith("rch:"))
async def cb_history(cq: CallbackQuery):
    r = await _detail(cq, cq.data.split(":", 1)[1])
    if not r:
        return
    rows = await db.cap_history(cq.from_user.id, r["username"])
    if not rows:
        await show(cq, receiver_detail_text(r) + "\n\nno cap record", kb.receiver_detail(r))
        return await cq.answer("empty")
    lines = "\n".join(f"{fmt_dt(x.get('ts'))} · cap {x.get('cap')}" for x in rows)
    await show(cq, receiver_detail_text(r) + "\n\n<b>Cap history</b>\n" + tg.pre(lines, 2000),
               kb.receiver_detail(r))
    await cq.answer()


@router.callback_query(F.data.startswith("rcpr:"))
async def cb_prices(cq: CallbackQuery):
    r = await _detail(cq, cq.data.split(":", 1)[1])
    if not r:
        return
    await cq.answer("fetching…")
    out = await receivers.capacity(cq.from_user.id, r["_id"])
    rows = await pricing.receiver_table(cq.from_user.id, r["_id"])
    r = await db.get_receiver(r["_id"])
    if not r:
        return
    head = receiver_detail_text(r, len(rows))
    if out["offers"]:
        body = extracted_text(out["offers"], r["username"])
    elif rows:
        body = prices_text(rows)
    else:
        body = _raw(out)
    await show(cq, f"{head}\n\n{body}", kb.receiver_detail(r))


@router.callback_query(F.data.startswith("rcw:"))
async def cb_warm_one(cq: CallbackQuery):
    r = await _detail(cq, cq.data.split(":", 1)[1])
    if not r:
        return
    ok = await receivers.warm(cq.from_user.id, r["_id"])
    tail = "\n\n✅ /start sent" if ok else "\n\n❌ failed"
    await show(cq, receiver_detail_text(r) + tail, kb.receiver_detail(r))
    await cq.answer("done")


@router.callback_query(F.data.startswith("rcs:"))
async def cb_confirm(cq: CallbackQuery):
    r = await _detail(cq, cq.data.split(":", 1)[1])
    if not r:
        return
    await db.set_receiver(r["_id"], confirm_seconds=0)
    r = await db.get_receiver(r["_id"])
    await show(cq, receiver_detail_text(r), kb.receiver_detail(r))
    await cq.answer("confirm reset to auto")


@router.callback_query(F.data.startswith("rct:"))
async def cb_toggle(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    if not await receivers.toggle(cq.from_user.id, rid):
        return await cq.answer("gone", show_alert=True)
    r = await db.get_receiver(rid)
    await show(cq, receiver_detail_text(r), kb.receiver_detail(r))
    await cq.answer("saved")


@router.callback_query(F.data.startswith("rcd:"))
async def cb_drop(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    await receivers.remove(cq.from_user.id, rid)
    await _render(cq.bot, cq.message.chat.id, cq.from_user.id, cq)
    await cq.answer("deleted")


async def on_add(m: Message, hit: dict):
    parts = (m.text or "").split()
    if not parts:
        return
    key = parts[0].lstrip("@")
    title = " ".join(parts[1:])[:40]
    rid = await receivers.add(m.from_user.id, key, title)
    if not rid:
        return await m.answer("bad username · /cancel")
    state.clear(m.from_user.id)
    await m.answer(f"✅ added @{key} as {pricing.kind_of(key)}")
    await _render(m.bot, m.chat.id, m.from_user.id)