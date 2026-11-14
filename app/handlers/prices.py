from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import db, keyboards as kb, pricing, receivers
from app.handlers.menu import show
from app.utils import tg
from app.utils.texts import caps_text, chart_copy, chart_text

router = Router()


async def _render(cq: CallbackQuery, note: str = ""):
    s = await db.get_settings(cq.from_user.id)
    profit = pricing.profit_mult(s)
    rows = await pricing.chart_rows(cq.from_user.id, s)
    text = chart_text(rows, profit)
    if note:
        text += f"\n{note}"
    await show(cq, text, kb.chart())


@router.callback_query(F.data.in_({"m:chart", "ch:copy", "ch:caps", "ch:sweep"}))
async def cb_chart(cq: CallbackQuery):
    action = cq.data
    if action == "ch:copy":
        s = await db.get_settings(cq.from_user.id)
        rows = await pricing.chart_rows(cq.from_user.id, s)
        body = chart_copy(rows, pricing.profit_mult(s))
        await tg.send(cq.bot, cq.from_user.id, tg.pre(body, 3600))
        return await cq.answer("copied")
    if action == "ch:caps":
        rows = await db.last_caps(cq.from_user.id)
        await show(cq, caps_text(rows), kb.caps())
        return await cq.answer()
    if action == "ch:sweep":
        await cq.answer("sweeping…")
        out = await receivers.capacity_sweep(cq.from_user.id)
        rows = await db.last_caps(cq.from_user.id)
        note = f"\nsweep · ok {out['ok']} · missing {out['missing']}"
        await show(cq, caps_text(rows) + note, kb.caps())
        return
    await cq.answer()
    await _render(cq)


@router.message(Command("prices"))
async def cmd_prices(m: Message):
    s = await db.get_settings(m.from_user.id)
    rows = await pricing.chart_rows(m.from_user.id, s)
    await tg.send(m.bot, m.chat.id, chart_text(rows, pricing.profit_mult(s)), kb.chart())


@router.message(Command("caps"))
async def cmd_caps(m: Message):
    rows = await db.last_caps(m.from_user.id)
    await tg.send(m.bot, m.chat.id, caps_text(rows), kb.caps())