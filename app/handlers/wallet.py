import html
import math

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import keyboards as kb
from app.deps import lzt
from app.handlers import state
from app.handlers.menu import show
from app.lzt import LztError
from app.utils import tg
from app.utils import wallet as wallet_view
from app.utils.money import fmt_money, free_of
from app.utils.texts import payment_text, wallet_text

router = Router()


def _st(uid: int) -> dict:
    hit = state.get(uid)
    if not hit or hit["kind"] != "exchange":
        return {}
    return hit


async def _snap_or_reply(target, edit: bool = False) -> dict | None:
    snap = await wallet_view.snapshot(lzt, "usd")
    if snap["error"] and not snap["entries"]:
        text = f"error <code>{tg.esc(snap['error'][:200])}</code>"
        if edit:
            await show(target, text, kb.back("m:main"))
        else:
            await target.answer(text)
        return None
    return snap


async def _render(cq: CallbackQuery, snap: dict):
    await show(cq, wallet_text(snap), kb.wallet(wallet_view.exchangable(snap)))


async def _deliver(bot, uid: int, amount: float, chat_id: int = 0, message_id: int = 0):
    text = await _do(uid, bot, amount)
    markup = kb.back("m:wallet")
    if chat_id and message_id:
        try:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id,
                                        reply_markup=markup, link_preview_options=tg.LP)
            return
        except Exception:
            pass
    await tg.send(bot, chat_id or uid, text, markup)


@router.message(Command("wallet"))
async def cmd_wallet(m: Message):
    state.clear(m.from_user.id)
    snap = await _snap_or_reply(m)
    if snap is None:
        return
    await tg.send(m.bot, m.chat.id, wallet_text(snap),
                  kb.wallet(wallet_view.exchangable(snap)))


@router.callback_query(F.data == "m:wallet")
async def cb_wallet(cq: CallbackQuery):
    state.clear(cq.from_user.id)
    snap = await _snap_or_reply(cq, edit=True)
    if snap is None:
        return await cq.answer()
    await _render(cq, snap)
    await cq.answer()


@router.callback_query(F.data == "m:payments")
async def cb_payments(cq: CallbackQuery):
    try:
        rows = await lzt.payments(page=1)
    except LztError as e:
        await show(cq, f"payments error <code>{tg.esc(str(e)[:200])}</code>",
                   kb.back("m:wallet"))
        return await cq.answer()
    if not rows:
        await show(cq, "<b>Payments</b>\nnothing returned by the API", kb.back("m:wallet"))
        return await cq.answer()
    body = "\n".join(payment_text(p) for p in rows[:10])
    await show(cq, f"<b>Payments</b>\n{tg.pre(body, 3000)}", kb.back("m:wallet"))
    await cq.answer()


@router.callback_query(F.data == "m:exchange")
async def cb_exchange(cq: CallbackQuery):
    snap = await _snap_or_reply(cq, edit=True)
    if snap is None:
        return await cq.answer()
    pool = wallet_view.exchangable(snap)
    if len(pool) < 2:
        await show(cq, "need two spendable balances", kb.back("m:wallet"))
        return await cq.answer()
    state.set(cq.from_user.id, "exchange")
    state.PENDING[cq.from_user.id]["pool"] = pool
    await show(cq, "<b>Exchange</b> — source balance", kb.exchange(pool, "from"))
    await cq.answer()


@router.callback_query(F.data.startswith("p:"))
async def cb_page(cq: CallbackQuery):
    parts = cq.data.split(":")
    if len(parts) != 3:
        return await cq.answer("bad callback", show_alert=True)
    _, step, offset = parts
    st = _st(cq.from_user.id)
    if not st:
        return await cq.answer("restart exchange", show_alert=True)
    pool = st.get("pool") or []
    if step == "to" and "from_id" in st:
        pool = [b for b in pool if b["balance_id"] != st["from_id"]]
    if not pool:
        return await cq.answer("nothing to pick", show_alert=True)
    await show(cq, f"<b>Exchange</b> — {step}", kb.exchange(pool, step, int(offset)))
    await cq.answer()


@router.callback_query(F.data.startswith("x:"))
async def cb_step(cq: CallbackQuery):
    parts = cq.data.split(":")
    if len(parts) != 3:
        return await cq.answer("bad callback", show_alert=True)
    _, step, idx_raw = parts
    st = _st(cq.from_user.id)
    if not st:
        return await cq.answer("restart exchange", show_alert=True)
    try:
        idx = int(idx_raw)
    except ValueError:
        return await cq.answer("bad index", show_alert=True)

    snap = await _snap_or_reply(cq, edit=True)
    if snap is None:
        return await cq.answer()

    live = {b["balance_id"]: b for b in wallet_view.exchangable(snap) if b.get("balance_id")}
    st["pool"] = [b for b in st.get("pool") or [] if b.get("balance_id") in live]
    pool = list(st["pool"])
    if step == "to":
        pool = [b for b in pool if b["balance_id"] != st.get("from_id")]
    if not pool:
        state.clear(cq.from_user.id)
        return await cq.answer("balances changed, restart", show_alert=True)
    if idx >= len(pool):
        return await cq.answer("pick again", show_alert=True)

    st["ts"] = __import__("time").time()
    pick = pool[idx]

    if step == "from":
        st["from_id"] = pick["balance_id"]
        st["cur"] = pick["currency"]
        st.pop("to_id", None)
        targets = [b for b in pool if b["balance_id"] != st["from_id"]]
        if not targets:
            return await cq.answer("no target balance", show_alert=True)
        await show(cq, f"from <b>{st['cur'].upper()}</b> — pick target",
                   kb.exchange(targets, "to"))
        return await cq.answer()

    if step != "to":
        return await cq.answer("bad step", show_alert=True)
    if "from_id" not in st:
        return await cq.answer("pick a source first", show_alert=True)

    st["to_id"] = pick["balance_id"]
    st["to_cur"] = pick["currency"]
    src = live.get(st["from_id"])
    avail = free_of(src) if src else 0.0
    msg = await cq.message.edit_text(
        f"<b>{st['cur']} → {st['to_cur']}</b>\n"
        f"available: {fmt_money(avail, st['cur'])}\n\n"
        f"Send amount in <b>{st['cur']}</b>, or tap:",
        reply_markup=kb.amount(st["cur"], avail), link_preview_options=tg.LP)
    st["prompt"] = (msg.chat.id, msg.message_id)
    await cq.answer()


@router.callback_query(F.data.startswith("xa:"))
async def cb_amount_btn(cq: CallbackQuery):
    raw = cq.data.split(":", 1)[1]
    st = _st(cq.from_user.id)
    if not st or "to_id" not in st:
        return await cq.answer("restart exchange", show_alert=True)
    if st.get("busy"):
        return await cq.answer("busy…")

    if raw == "max":
        snap = await _snap_or_reply(cq, edit=True)
        if snap is None:
            return await cq.answer()
        src = next((b for b in snap["entries"] if b["balance_id"] == st.get("from_id")), None)
        if not src:
            return await cq.answer("source gone, restart", show_alert=True)
        amount = free_of(src)
    else:
        try:
            amount = float(raw)
        except ValueError:
            return await cq.answer("bad amount", show_alert=True)

    chat_id, message_id = st.get("prompt") or (cq.message.chat.id, cq.message.message_id)
    await _deliver(cq.bot, cq.from_user.id, amount, chat_id, message_id)
    await cq.answer("done")


@router.callback_query(F.data == "xc")
async def cb_exch_cancel(cq: CallbackQuery):
    state.clear(cq.from_user.id)
    snap = await _snap_or_reply(cq, edit=True)
    if snap is None:
        return await cq.answer()
    await _render(cq, snap)
    await cq.answer("cancelled")


@router.message(Command("amount"))
async def cmd_amount(m: Message):
    arg = (m.text or "").split(maxsplit=1)
    if len(arg) < 2:
        return await m.answer("usage: <code>/amount 25</code>")
    try:
        amount = float(arg[1].replace(",", ".").strip())
    except ValueError:
        return await m.answer("number expected")
    chat_id, message_id = (_st(m.from_user.id).get("prompt") or (0, 0))
    await _deliver(m.bot, m.from_user.id, amount, chat_id, message_id)


async def on_amount(m: Message):
    st = _st(m.from_user.id)
    if st.get("busy"):
        return await m.answer("busy…")
    try:
        amount = float((m.text or "").replace(",", ".").strip())
    except ValueError:
        return await m.answer("number expected · /cancel")
    chat_id, message_id = (st.get("prompt") or (0, 0))
    await _deliver(m.bot, m.from_user.id, amount, chat_id, message_id)


async def _do(uid: int, bot, amount: float) -> str:
    st = _st(uid)
    if not st or "from_id" not in st or "to_id" not in st:
        return "⚠️ restart exchange"
    if st.get("busy"):
        return "⚠️ already processing"
    if not math.isfinite(amount) or amount <= 0:
        return "⚠️ amount must be a positive number"

    try:
        balances = await lzt.balances(use_cache=False)
    except LztError as e:
        return f"⚠️ {tg.esc(str(e))}"

    src = next((b for b in balances if b.get("balance_id") == st["from_id"]), None)
    if not src:
        state.clear(uid)
        return "⚠️ source balance gone, restart"
    if not any(b.get("balance_id") == st["to_id"] for b in balances):
        state.clear(uid)
        return "⚠️ target balance gone, restart"

    avail = free_of(src)
    if amount < 0.01:
        return f"⚠️ minimum is {fmt_money(0.01, st['cur'])}"
    if amount > avail + 1e-9:
        return f"⚠️ only {fmt_money(avail, st['cur'])} available"

    st["busy"] = True
    try:
        await lzt.exchange(st["from_id"], st["to_id"], amount)
    except LztError as e:
        st["busy"] = False
        return f"⚠️ failed: {tg.esc(str(e))}"

    state.clear(uid)
    snap = await wallet_view.snapshot(lzt, st["cur"])
    return (f"✅ exchanged {fmt_money(amount, st['cur'])} → {st['to_cur']}\n\n"
            + wallet_text(snap))