import asyncio
import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app import db, engine, pricing, receiver_hub, receivers, seller
from app.config import OWNER_IDS, TARGET_HOURS
from app.deps import lzt
from app.lzt import extract_auth_key, extract_dc_id
from app.session_manager import dc_addr, login_with_auth_key
from app.utils import tg
from app.utils import wallet as wallet_view
from app.utils.filters import geo_spam_state, mail_state, pass_state, spam_state
from app.utils.money import fmt_money
from app.utils.texts import extracted_text
from app.utils.timeutil import fmt_left

router = Router()


def _owner(m: Message) -> bool:
    return not OWNER_IDS or m.from_user.id in OWNER_IDS


def _dc_hint(dc: int) -> str:
    hit = dc_addr(dc) if dc else None
    return f"{hit[0]}:{hit[1]}" if hit else "unknown, will sweep"


@router.message(Command("hub"))
async def cmd_hub(m: Message):
    if not _owner(m):
        return
    if not receiver_hub.running():
        await receiver_hub.start()
    online = receiver_hub.running()
    me = "?"
    if online:
        try:
            entity = await receiver_hub._client.get_me()
            me = f"@{entity.username or entity.id}"
        except Exception:
            pass
    rows = await db.list_receivers(m.from_user.id)
    lines = [f"userbot {'🟢 ' + tg.esc(me) if online else '🔴 offline'}",
             f"receivers {len(rows)}"]
    for r in rows:
        ok = await receiver_hub._resolve(r["username"]) if online else None
        lines.append(f"{'✅' if ok else '❌'} @{r['username']} [{r.get('kind')}]")
    await m.answer("<code>" + tg.esc("\n".join(lines)) + "</code>")


@router.message(Command("probe"))
async def cmd_probe(m: Message):
    if not _owner(m):
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Usage: /probe @username")
    key = parts[1].strip().lstrip("@")
    receiver = await db.get_receiver_by_key(m.from_user.id, key)
    if not receiver:
        return await m.answer("❌ receiver not registered, add it first")
    await m.answer("🔎 probing…")
    out = await pricing.probe(receiver)
    head = (f"@{tg.esc(key)} · kind {receiver.get('kind')}"
            f" · replies {out['replies']} · cap {out['cap']}"
            f" · parsed {len(out['offers'])}")
    body = extracted_text(out["offers"], key)
    raw = (out.get("raw") or "empty")[:2500]
    await tg.send(m.bot, m.chat.id,
                  f"{head}\n\n{body}\n\n<b>Raw</b>\n<code>{tg.esc(raw)}</code>")


@router.message(Command("warm"))
async def cmd_warm(m: Message):
    if not _owner(m):
        return
    done = await receivers.warm(m.from_user.id)
    await m.answer(f"✅ /start sent to {done} receiver(s)")


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
    if not _owner(m):
        return
    parts = (m.text or "").split()
    if len(parts) < 3:
        return await m.answer("Usage: /sold <purchase_id> <price>")
    try:
        price = float(parts[2].replace(",", "."))
    except ValueError:
        return await m.answer("❌ Price must be a number")
    if not await db.mark_sold(parts[1], price, m.from_user.id):
        return await m.answer("❌ purchase not found")
    await m.answer(f"✅ Marked sold for ${price:g}")


@router.message(Command("selltest"))
async def cmd_selltest(m: Message):
    if not _owner(m):
        return
    parts = (m.text or "").split()
    if len(parts) < 2:
        return await m.answer("Usage: /selltest <purchase_id>")
    p = await db.get_purchase(parts[1])
    if not p or p["tg_id"] != m.from_user.id:
        return await m.answer("❌ purchase not found")
    if p.get("status") != "ready":
        return await m.answer(f"❌ status is {p.get('status')}")
    await m.answer("💱 selling…")
    ok, note = await seller.manual(m.bot, lzt, p)
    if not ok:
        await m.answer(f"❌ {tg.esc(note)}")


@router.message(Command("reset"))
async def cmd_reset(m: Message):
    if not _owner(m):
        return
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await m.answer("Usage: /reset <item_id>")
    try:
        await lzt.reset_authorizations(int(parts[1]))
    except Exception as e:
        return await m.answer(f"❌ {html.escape(str(e)[:200])}")
    await m.answer("✅ all authorizations reset")


@router.message(Command("relogin"))
async def cmd_relogin(m: Message):
    if not _owner(m):
        return
    parts = (m.text or "").split()
    if len(parts) < 2:
        return await m.answer("Usage: /relogin <purchase_id>")
    p = await db.get_purchase(parts[1])
    if not p or p["tg_id"] != m.from_user.id:
        return await m.answer("❌ purchase not found")
    if not p.get("phone"):
        return await m.answer("❌ no phone on purchase")
    s = await db.get_settings(p["tg_id"])
    await m.answer("🔑 logging in…")
    try:
        c, session_str, _uid = await login_with_auth_key(
            p["phone"], p.get("password") or "", p["item_id"], lzt,
            s.get("proxy", ""), p, p.get("auth_key") or "", int(p.get("auth_dc") or 0))
    except Exception as e:
        return await m.answer(f"❌ {html.escape(str(e)[:200])}")
    await db.set_session(p["_id"], session_str)
    try:
        await c.disconnect()
    except Exception:
        pass
    await m.answer("✅ session reissued")


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


@router.message(Command("queue"))
async def cmd_queue(m: Message):
    if not _owner(m):
        return
    rows = await db.active_purchases(m.from_user.id, limit=20)
    if not rows:
        return await m.answer("queue empty")
    lines = ["<b>Queue</b>"]
    for p in rows:
        state = p.get("status") or "?"
        left = fmt_left(p.get("expires_at")) if state in ("queued", "cleaning") else "cleaned"
        age = db.current_age(p)
        lines.append(f"<code>{tg.esc(p['_id'])}</code> · {tg.esc(p.get('phone') or '—')}"
                     f" · {state} · age {age:.1f}h · {left}")
    await tg.send(m.bot, m.chat.id, "\n".join(lines))


@router.message(Command("clean"))
async def cmd_clean(m: Message):
    if not _owner(m):
        return
    parts = (m.text or "").split()
    if len(parts) < 2:
        return await m.answer("Usage: /clean <purchase_id>")
    p = await db.get_purchase(parts[1])
    if not p:
        return await m.answer("❌ purchase not found")
    if p.get("status") not in ("login", "queued", "cleaning"):
        return await m.answer(f"❌ status is {p.get('status')}")
    await m.answer("🧼 cleaning…")
    if p.get("status") == "login":
        await db.queue_purchase(p["_id"], db._now())
        p = await db.get_purchase(p["_id"])
    await engine.clean_purchase(m.bot, lzt, p)


@router.message(Command("diag"))
async def cmd_diag(m: Message):
    if not _owner(m):
        return
    s = await db.get_settings(m.from_user.id)
    snap = await wallet_view.snapshot(lzt, s["currency"])
    spent = await db.spent_today(m.from_user.id)
    queued = await db.queue_count(m.from_user.id)
    kinds = ", ".join(f"{b['kind']}:{b['amount']:g}" for b in snap["entries"]) or "none"
    await m.answer(
        "<b>diag</b>\n"
        f"auto_buy {s['auto_buy']} · auto_sell {s['auto_sell']} · cap ${s['daily_spend_cap']}"
        f" · spent {spent:.2f}\n"
        f"price {s['price_min']}–{s['price_max']} · currency {s['currency']}\n"
        f"profit x{s['profit_mult']} · spam free {s['spam_free']}\n"
        f"target {TARGET_HOURS:g}h · min age {s['session_min_hours']}h"
        f" · max age {s['session_max_hours'] or 'off'} · queued {queued}\n"
        f"2fa <code>{tg.esc(s['twofa_password'])}</code>"
        f" · mail src {s['mail_provider']}\n"
        f"hub {'🟢 online' if receiver_hub.running() else '🔴 offline'}\n"
        f"total {fmt_money(snap['total'], s['currency'])}"
        f" · spendable {fmt_money(snap['spendable'], s['currency'])}\n"
        f"entries {tg.esc(kinds)}\n"
        f"note {tg.esc(snap['note'] or '—')}\n"
        f"error {tg.esc(snap['error'] or '—')}"
    )


@router.message(Command("peek"))
async def cmd_peek(m: Message):
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
    key = extract_auth_key(item)
    dc = extract_dc_id(item)
    keep = ("price", "telegram_spam_block", "spam_block", "telegram_spam_geo", "spam_geo",
            "telegram_email", "email_type", "telegram_password", "country", "created_at",
            "registration_date", "telegram_session_age", "session_age",
            "session_age_period", "nsb")
    lines = [f"{k} = {item.get(k)!r}" for k in keep if k in item]
    lines.append("—")
    lines.append(f"auth_key = {'present len ' + str(len(key)) if key else 'MISSING'}")
    lines.append(f"dc_id = {dc or 'MISSING'} -> {_dc_hint(dc)}")
    lines.append(f"spam_state = {spam_state(item)!r}")
    lines.append(f"geo_spam_state = {geo_spam_state(item)!r}")
    lines.append(f"email_state = {mail_state(item)!r}")
    lines.append(f"pass_state = {pass_state(item)!r}")
    lines.append("—")
    lines.append("keys: " + ", ".join(sorted(item)[:60]))
    await m.answer(f"<code>{html.escape(chr(10).join(lines))}</code>")