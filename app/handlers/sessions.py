from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import db, keyboards as kb, seller
from app.handlers.menu import show
from app.session_manager import check_session
from app.utils import tg
from app.utils.texts import (session_bundle, session_detail, session_file,
                             session_stats_text)
from app.utils.timeutil import fmt_dt, now

router = Router()

PAGES = {"list": "queue", "over": "24h+", "ready": "ready"}


async def _rows(tg_id: int, page: str) -> list[dict]:
    if page == "over":
        rows = await db.over_age(tg_id)
    elif page == "ready":
        rows = await db.ready_purchases(tg_id)
    else:
        rows = await db.queue_purchases(tg_id)
    stamp = now()
    for p in rows:
        p["_age"] = db.current_age(p, stamp)
    return rows


async def _panel(bot, chat_id: int, tg_id: int, edit=None):
    stats = await db.session_stats(tg_id)
    text = (f"<b>Sessions</b>\n"
            f"queue <code>{stats['queue']}</code>"
            f" · 24h+ <code>{stats['over']}</code>"
            f" · ready <code>{stats['ready']}</code>\n"
            f"live <code>{stats['live']}</code> · dead <code>{stats['dead']}</code>"
            f" · sold <code>{stats['sold']}</code>")
    markup = kb.sessions(stats)
    if edit is not None:
        await show(edit, text, markup)
    else:
        await tg.send(bot, chat_id, text, markup)


async def _send_file(bot, chat_id: int, tg_id: int, page: str) -> int:
    rows = await _rows(tg_id, page)
    if not rows:
        return 0
    label = PAGES.get(page, page)
    body = session_bundle(rows, label)
    if not body.strip():
        return -1
    await tg.send_document(bot, chat_id, body.encode(),
                           f"{label.replace('+', 'p')}_{now()}.session")
    return len(rows)


@router.message(Command("session"))
async def cmd_session(m: Message):
    arg = (m.text or "").split(maxsplit=1)
    if len(arg) > 1:
        p = await db.find_by_phone(m.from_user.id, arg[1].strip())
        if not p:
            return await m.answer("not found")
        return await tg.send(m.bot, m.chat.id, session_detail(p), kb.session_detail(p))
    await _panel(m.bot, m.chat.id, m.from_user.id)


@router.callback_query(F.data == "m:sessions")
async def cb_sessions(cq: CallbackQuery):
    await _panel(cq.bot, cq.message.chat.id, cq.from_user.id, cq)
    await cq.answer()


@router.callback_query(F.data.startswith("q:"))
async def cb_page(cq: CallbackQuery):
    page = cq.data.split(":", 1)[1]
    if page == "stats":
        st = await db.session_stats(cq.from_user.id)
        sold = await db.sale_stats(cq.from_user.id)
        await show(cq, session_stats_text(st, sold), kb.back("m:sessions"))
        return await cq.answer()
    label = PAGES.get(page, page)
    count = await _send_file(cq.bot, cq.from_user.id, cq.from_user.id, page)
    if count == 0:
        await show(cq, f"<b>{label}</b>\nempty", kb.back("m:sessions"))
        return await cq.answer("empty", show_alert=True)
    if count == -1:
        await show(cq, f"<b>{label}</b>\nno session stored", kb.back("m:sessions"))
        return await cq.answer("no session", show_alert=True)
    await show(cq, f"<b>{label}</b> · {count} session(s) sent as "
                   f"<code>.session</code>", kb.session_file_bar(page))
    await cq.answer(f"{count} sent")


@router.callback_query(F.data.startswith("ql:"))
async def cb_list(cq: CallbackQuery):
    page = cq.data.split(":", 1)[1]
    rows = await _rows(cq.from_user.id, page)
    if not rows:
        await show(cq, f"<b>{PAGES.get(page, page)}</b>\nempty", kb.back("m:sessions"))
        return await cq.answer("empty", show_alert=True)
    await show(cq, f"<b>{PAGES.get(page, page)}</b> · {len(rows)}",
               kb.session_list(rows[:20], page))
    await cq.answer()


@router.callback_query(F.data.startswith("v:"))
async def cb_view(cq: CallbackQuery):
    p = await db.get_purchase(cq.data.split(":", 1)[1])
    if not p or p["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    await show(cq, session_detail(p), kb.session_detail(p))
    await cq.answer()


@router.callback_query(F.data.startswith("c:"))
async def cb_check(cq: CallbackQuery):
    pid = cq.data.split(":", 1)[1]
    p = await db.get_purchase(pid)
    if not p or p["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    if not p.get("session_string"):
        return await cq.answer("no session stored yet", show_alert=True)

    await cq.answer("checking…")
    s = await db.get_settings(cq.from_user.id)
    alive, me = await check_session(p["session_string"], s.get("proxy", ""))

    note = "session live" if alive else "session dead"
    if me:
        note += f" · @{me.username or me.id}"
    await db.set_purchase(pid, session_state="live" if alive else "dead", checked_at=now())
    p = await db.get_purchase(pid)
    await show(cq, session_detail(p, f"🔎 {note} · {fmt_dt(now())}"),
               kb.session_detail(p))


@router.callback_query(F.data.startswith("f:"))
async def cb_file(cq: CallbackQuery):
    p = await db.get_purchase(cq.data.split(":", 1)[1])
    if not p or p["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    if not p.get("session_string"):
        return await cq.answer("no session stored", show_alert=True)
    name = (p.get("phone") or str(p["item_id"])).lstrip("+") or "session"
    await tg.send_document(cq.bot, cq.from_user.id, session_file(p).encode(), f"{name}.py")
    await cq.answer("sent")


@router.callback_query(F.data.startswith("sell:"))
async def cb_sell(cq: CallbackQuery):
    from app.deps import lzt
    p = await db.get_purchase(cq.data.split(":", 1)[1])
    if not p or p["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    if p.get("status") != "ready":
        return await cq.answer(f"status {p.get('status')}", show_alert=True)
    await cq.answer("selling…")
    ok, note = await seller.manual(cq.bot, lzt, p)
    if not ok:
        await tg.send(cq.bot, cq.from_user.id, f"⚠️ sell failed: {tg.esc(note)}")
    p = await db.get_purchase(p["_id"])
    if p:
        await show(cq, session_detail(p, f"💱 {note}"), kb.session_detail(p))