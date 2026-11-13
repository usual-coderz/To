from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import db, keyboards as kb, pricing, receivers
from app.handlers.menu import show
from app.utils import tg
from app.utils.texts import (caps_text, extracted_text, prices_text, receiver_detail_text,
                             receivers_text)
from app.utils.timeutil import fmt_dt

router = Router()

PENDING: dict[int, str] = {}

RAW_LIMIT = 1200


def _raw(out: dict) -> str:
    body = out.get("raw") or "empty"
    return "<b>Raw</b> · {n} reply\n<code>{t}</code>".format(
        n=out.get("replies", 0), t=tg.esc(body[:RAW_LIMIT]))


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


@router.callback_query(F.data.startswith("rv:"))
async def cb_detail(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    r = await db.get_receiver(rid)
    if not r or r["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    count = len(await pricing.receiver_table(cq.from_user.id, rid))
    await show(cq, receiver_detail_text(r, count), kb.receiver_detail(r))
    await cq.answer()


@router.callback_query(F.data.startswith("rc:"))
async def cb_cap(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    r = await db.get_receiver(rid)
    if not r or r["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    if r.get("kind") == "lion":
        return await cq.answer("lion uses the price link", show_alert=True)
    await cq.answer("/cap sent, reading…")
    out = await receivers.capacity(cq.from_user.id, rid)
    r = await db.get_receiver(rid)
    if not r:
        return
    head = receiver_detail_text(r, len(out["offers"]))
    note = "no reply" if not out["replies"] else f"cap {out['cap']} · parsed {len(out['offers'])}"
    if out["offers"]:
        body = extracted_text(out["offers"], r["username"])
    else:
        body = _raw(out)
    await show(cq, f"{head}\n\n🔄 {note}\n\n{body}", kb.receiver_detail(r))


@router.callback_query(F.data.startswith("rh:"))
async def cb_history(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    r = await db.get_receiver(rid)
    if not r or r["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    rows = await db.cap_history(cq.from_user.id, r["username"])
    if not rows:
        await show(cq, receiver_detail_text(r) + "\n\nno cap record", kb.receiver_detail(r))
        return await cq.answer("empty")
    lines = [f"{fmt_dt(x.get('ts'))} · cap <code>{x.get('cap')}</code>" for x in rows]
    await show(cq, receiver_detail_text(r) + "\n\n<b>Cap history</b>\n" + "\n".join(lines),
               kb.receiver_detail(r))
    await cq.answer()


@router.callback_query(F.data.startswith("rp:"))
async def cb_prices(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    r = await db.get_receiver(rid)
    if not r or r["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    await cq.answer("fetching…")
    out = await receivers.capacity(cq.from_user.id, rid)
    rows = await pricing.receiver_table(cq.from_user.id, rid)
    r = await db.get_receiver(rid)
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


@router.callback_query(F.data.startswith("rw:"))
async def cb_warm_one(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    ok = await receivers.warm(cq.from_user.id, rid)
    r = await db.get_receiver(rid)
    if not r:
        return await cq.answer("gone", show_alert=True)
    tail = "\n\n✅ /start sent" if ok else "\n\n❌ failed"
    await show(cq, receiver_detail_text(r) + tail, kb.receiver_detail(r))
    await cq.answer("done")


@router.callback_query(F.data.startswith("rs:"))
async def cb_confirm(cq: CallbackQuery):
    rid = cq.data.split(":", 1)[1]
    r = await db.get_receiver(rid)
    if not r or r["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    await db.set_receiver(rid, confirm_seconds=0)
    r = await db.get_receiver(rid)
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
    await m.answer(f"✅ added @{key} as {pricing.kind_of(key)}")
    await _render(m.bot, m.chat.id, m.from_user.id)