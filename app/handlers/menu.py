import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import db, keyboards as kb
from app.config import OWNER_IDS
from app.deps import lzt
from app.handlers import state
from app.utils import tg
from app.utils import wallet as wallet_view
from app.utils.texts import menu_text, profit_text, ready_text
from app.utils.timeutil import fmt_dt

router = Router()

WALLET_TIMEOUT = 8.0
PROFILE_TIMEOUT = 6.0


def allowed(tg_id: int) -> bool:
    return not OWNER_IDS or tg_id in OWNER_IDS


async def show(cq: CallbackQuery, text: str, markup):
    await tg.edit(cq.message, text, markup)


async def _wallet(currency: str) -> dict:
    try:
        return await asyncio.wait_for(wallet_view.snapshot(lzt, currency), WALLET_TIMEOUT)
    except Exception:
        return {"total": 0.0, "spendable": 0.0, "entries": [], "currency": currency,
                "note": "wallet timeout", "error": ""}


async def _profile() -> dict:
    try:
        return await asyncio.wait_for(lzt.profile(), PROFILE_TIMEOUT)
    except Exception:
        return {"username": "offline"}


async def _last_line(tg_id: int) -> str:
    rows = await db.recent_reports(tg_id, 1)
    if not rows:
        return ""
    r = rows[0]
    stamp = fmt_dt(r.get("ts"))
    if r.get("error"):
        return f"last {stamp}: ⚠️ {r['error'][:60]}"
    if r.get("note"):
        return f"last {stamp}: {r['note'][:60]}"
    if r.get("scanned"):
        return f"last {stamp}: scan {r['scanned']} · buy {r.get('bought', 0)}"
    return ""


async def render_menu(tg_id: int) -> tuple[str, object]:
    s = await db.get_settings(tg_id)
    snap = await _wallet(s["currency"])
    profile = await _profile()

    note = snap["note"]
    if not snap["error"] and snap["spendable"] < db.num(s.get("price_min")):
        hint = f"spendable below min price {s['price_min']}"
        note = f"{note} · {hint}" if note else hint
    if snap["error"]:
        note = f"{note} · {snap['error'][:60]}" if note else snap["error"][:60]

    queued = await db.queue_count(tg_id)
    ready = await db.ready_count(tg_id)
    last = await _last_line(tg_id)
    if last:
        note = f"{note} · {last}" if note else last

    return (menu_text(s, profile, snap["total"], snap["spendable"], queued, ready, note),
            kb.main(s, ready))


@router.message(Command("start"))
async def cmd_start(m: Message):
    await db.ensure_user(m.from_user.id, m.from_user.username or "")
    state.clear(m.from_user.id)
    text, markup = await render_menu(m.from_user.id)
    await tg.send(m.bot, m.chat.id, text, markup)


@router.callback_query(F.data == "m:main")
async def cb_main(cq: CallbackQuery):
    state.clear(cq.from_user.id)
    text, markup = await render_menu(cq.from_user.id)
    await show(cq, text, markup)
    await cq.answer()


@router.callback_query(F.data == "m:profit")
async def cb_profit(cq: CallbackQuery):
    s = await db.get_settings(cq.from_user.id)
    await show(cq, profit_text(s), kb.profit(s))
    await cq.answer()


@router.callback_query(F.data.startswith("t:"))
async def cb_toggle(cq: CallbackQuery):
    key = cq.data.split(":", 1)[1]
    s = await db.get_settings(cq.from_user.id)
    if key not in s:
        return await cq.answer("unknown setting", show_alert=True)
    await db.set_setting(cq.from_user.id, key, "0" if s[key] == "1" else "1")
    if key == "auto_buy":
        text, markup = await render_menu(cq.from_user.id)
        await show(cq, text, markup)
    elif key == "auto_sell":
        s = await db.get_settings(cq.from_user.id)
        count = await db.ready_count(cq.from_user.id)
        await show(cq, ready_text(s, count, await db.sale_stats(cq.from_user.id)),
                   kb.ready(s, count))
    else:
        s = await db.get_settings(cq.from_user.id)
        from app.utils.texts import filters_text
        await show(cq, filters_text(s), kb.filters(s))
    await cq.answer("saved")