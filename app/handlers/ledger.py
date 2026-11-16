import io
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app import db, keyboards as kb
from app.handlers import state
from app.handlers.menu import show
from app.utils.texts import ledger_text, report_text, stats_text

router = Router()


@router.message(Command("report"))
async def cmd_report(m: Message):
    await m.answer(report_text(await db.recent_reports(m.from_user.id)),
                   reply_markup=kb.report())


@router.callback_query(F.data.in_({"m:report", "r:view"}))
async def cb_report(cq: CallbackQuery):
    await show(cq, report_text(await db.recent_reports(cq.from_user.id)), kb.report())
    await cq.answer()


@router.callback_query(F.data == "m:stats")
async def cb_stats(cq: CallbackQuery):
    s = await db.get_settings(cq.from_user.id, 1)
    fid = state.fid(cq.from_user.id)
    await show(cq, stats_text(await db.ledger_stats(cq.from_user.id, fid), s["currency"], fid),
               kb.back())
    await cq.answer()


@router.callback_query(F.data == "m:ledger")
async def cb_ledger(cq: CallbackQuery):
    await show(cq, ledger_text(await db.ledger_stats(cq.from_user.id)), kb.ledger())
    await cq.answer()


@router.callback_query(F.data == "l:csv")
async def cb_csv(cq: CallbackQuery):
    rows = await db.ledger_rows(cq.from_user.id)
    if not rows:
        return await cq.answer("ledger empty", show_alert=True)
    buf = io.StringIO()
    buf.write("purchase_id,item_id,fid,buy,sell,status,created,closed,note\n")
    for r in rows:
        note = (r.get("extra") or "").replace('"', "'")[:60]
        buf.write(f"{r['purchase_id']},{r['item_id']},{r.get('fid') or 1},"
                  f"{r['buy_price']},{r['sell_price']},{r['status']},"
                  f"{r['created_at']},{r['closed_at']},\"{note}\"\n")
    await cq.message.answer_document(
        BufferedInputFile(buf.getvalue().encode(), filename=f"ledger_{int(time.time())}.csv"))
    await cq.answer("sent")