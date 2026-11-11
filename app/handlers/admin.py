import asyncio
import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app import db, engine
from app.config import OWNER_IDS
from app.deps import lzt
from app.utils import tg
from app.utils import wallet as wallet_view
from app.utils.filters import mail_state, pass_state, spam_state
from app.utils.money import fmt_money

router = Router()


def _owner(m: Message) -> bool:
    return not OWNER_IDS or m.from_user.id in OWNER_IDS


@router.message(Command("addowner"))
async def cmd_addowner(m: Message):
    if not _owner(m):
        return
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("Usage: /addowner 123456789")
    await db.ensure_user(int(parts[1]))
    await m.answer(f"✅ Added {parts[1]}")


@router.message(Command("sold"))
async def cmd_sold(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 3:
        return await m.answer("Usage: /sold <purchase_id> <price>")
    try:
        price = float(parts[2].replace(",", "."))
    except ValueError:
        return await m.answer("❌ Price must be a number")
    if not await db.mark_sold(parts[1], price):
        return await m.answer("❌ purchase not found")
    await m.answer(f"✅ Marked sold for ${price:g}")


@router.message(Command("buy"))
async def cmd_buy(m: Message):
    if not _owner(m):
        return
    await db.set_setting(m.from_user.id, "auto_buy", "1")
    await m.answer("▶️ cycle started")
    asyncio.create_task(_run_once(m.bot, m.from_user.id))


async def _run_once(bot, tg_id: int):
    s = await db.get_settings(tg_id)
    try:
        await engine._cycle(bot, lzt, tg_id, s)
    except Exception as e:
        await tg.send(bot, tg_id, f"cycle error: <code>{tg.esc(str(e)[:200])}</code>")
        return
    row = (await db.recent_reports(tg_id, 1) or [{}])[0]
    bits = []
    if row.get("error"):
        bits.append(f"⚠️ {row['error']}")
    if row.get("note"):
        bits.append(row["note"])
    if row.get("scanned"):
        bits.append(f"scan {row['scanned']} · buy {row.get('bought', 0)}")
    reasons = row.get("reasons") or {}
    if reasons:
        bits.append(", ".join(f"{k}×{v}" for k, v in
                              sorted(reasons.items(), key=lambda x: -x[1])[:4]))
    await tg.send(bot, tg_id, "cycle: " + (" · ".join(bits) or "no data"))


@router.message(Command("diag"))
async def cmd_diag(m: Message):
    if not _owner(m):
        return
    s = await db.get_settings(m.from_user.id)
    snap = await wallet_view.snapshot(lzt, s["currency"])
    spent = await db.spent_today(m.from_user.id)
    kinds = ", ".join(f"{b['kind']}:{b['amount']:g}" for b in snap["entries"]) or "none"
    await m.answer(
        "<b>diag</b>\n"
        f"auto_buy {s['auto_buy']} · cap ${s['daily_spend_cap']} · spent {spent:.2f}\n"
        f"price {s['price_min']}–{s['price_max']} · currency {s['currency']}\n"
        f"total {fmt_money(snap['total'], s['currency'])}"
        f" · spendable {fmt_money(snap['spendable'], s['currency'])}\n"
        f"entries {tg.esc(kinds)}\n"
        f"note {tg.esc(snap['note'] or '—')}\n"
        f"error {tg.esc(snap['error'] or '—')}"
    )


@router.message(Command("peek"))
async def cmd_peek(m: Message):
    """Dump real check-account fields and how the filters read them."""
    if not _owner(m):
        return
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("usage: /peek <item_id>")
    try:
        chk = await lzt.check_account(int(parts[1]))
    except Exception as e:
        return await m.answer(f"error {html.escape(str(e))}")
    item = chk.get("item") or {}
    keep = ("price", "telegram_spam_block", "spam_block", "telegram_email", "email_type",
            "telegram_password", "country", "created_at", "registration_date",
            "telegram_session_age", "session_age", "session_age_period", "nsb")
    lines = [f"{k} = {item.get(k)!r}" for k in keep if k in item]
    lines.append("—")
    lines.append(f"spam_state = {spam_state(item)!r}")
    lines.append(f"email_state = {mail_state(item)!r}")
    lines.append(f"pass_state = {pass_state(item)!r}")
    lines.append("—")
    lines.append("keys: " + ", ".join(sorted(item)[:60]))
    await m.answer(f"<code>{html.escape(chr(10).join(lines))}</code>")