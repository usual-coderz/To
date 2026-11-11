from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import db, engine, keyboards as kb
from app.deps import lzt
from app.handlers.menu import show
from app.lzt import LztError
from app.session_manager import check_session, client, terminate
from app.utils import tg
from app.utils.texts import session_detail, session_file
from app.utils.timeutil import fmt_dt, now

router = Router()


@router.message(Command("session"))
async def cmd_session(m: Message):
    arg = (m.text or "").split(maxsplit=1)
    if len(arg) > 1:
        p = await db.find_by_phone(m.from_user.id, arg[1].strip())
        if not p:
            return await m.answer("not found")
        return await tg.send(m.bot, m.chat.id, session_detail(p), kb.session_detail(p))
    rows = await db.active_purchases(m.from_user.id)
    text = f"<b>Sessions</b> · {len(rows)}" if rows else "no sessions queued"
    await tg.send(m.bot, m.chat.id, text, kb.sessions(rows) if rows else kb.back())


@router.callback_query(F.data == "m:sessions")
async def cb_sessions(cq: CallbackQuery):
    rows = await db.active_purchases(cq.from_user.id)
    text = f"<b>Sessions</b> · {len(rows)}" if rows else "no sessions queued"
    await show(cq, text, kb.sessions(rows) if rows else kb.back())
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
    name = (p.get("phone") or str(p["item_id"])).lstrip("+") or "session"
    await tg.send_document(cq.bot, cq.from_user.id, session_file(p).encode(), f"{name}.py")
    await cq.answer("sent")


@router.callback_query(F.data.startswith("k:"))
async def cb_kill(cq: CallbackQuery):
    pid = cq.data.split(":", 1)[1]
    p = await db.get_purchase(pid)
    if not p or p["tg_id"] != cq.from_user.id:
        return await cq.answer("gone", show_alert=True)
    if p.get("status") == "queued":
        await cq.answer("cleaning…")
        await engine.clean_purchase(cq.bot, lzt, p)
        return
    if not p.get("session_string"):
        return await cq.answer("no session", show_alert=True)

    s = await db.get_settings(cq.from_user.id)
    await cq.answer("cleaning…")
    c = client(p["session_string"], s.get("proxy", ""))
    try:
        await c.connect()
        await terminate(c)
    except Exception:
        pass
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass
    try:
        await lzt.reset_authorizations(p["item_id"])
    except LztError:
        pass
    await db.close_purchase(pid, "killed", "manual clean")
    await show(cq, f"cleaned <code>{tg.esc(p.get('phone') or p['item_id'])}</code>",
               kb.back("m:sessions"))