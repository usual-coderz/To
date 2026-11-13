from app.config import API_HASH, API_ID, TARGET_HOURS
from app.utils import countries
from app.utils.money import fmt_money
from app.utils.timeutil import fmt_age, fmt_dt, fmt_left, fmt_year
from app.utils.tg import esc

BAR = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


def bought_line(country: str) -> str:
    return f"✅ <b>{esc(country or '??')}</b> bought"


def failed_line(country: str, reason: str = "") -> str:
    body = f"⚠️ <b>{esc(country or '??')}</b> login failed"
    if reason:
        body += f"\n{esc(str(reason)[:150])}"
    return body


def sell_fail_line(country: str, reason: str = "") -> str:
    body = f"⚠️ <b>{esc(country or '??')}</b> sell failed"
    if reason:
        body += f"\n{esc(str(reason)[:150])}"
    return body


def menu_text(s: dict, profile: dict, total: float, spendable: float,
              queued: int, note: str = "") -> str:
    body = (
        "<b>AccStore</b>\n"
        f"{BAR}\n"
        f"👤 {esc(profile.get('username', '?'))}\n"
        f"💵 <b>{fmt_money(total, s['currency'])}</b>"
    )
    if spendable + 1e-9 < total:
        body += f" · {fmt_money(spendable, s['currency'])} spendable"
    body += (
        f"\n{BAR}\n"
        f"{'🟢 running' if s['auto_buy'] == '1' else '🔴 stopped'}"
        f" · queue {queued}\n"
        f"auto sell {'🟢 on' if s['auto_sell'] == '1' else '🔴 off'}"
        f" · min ${s['sell_min_price']}\n"
        f"price {s['price_min']}–{s['price_max']} · spam {s['spam_mode']}\n"
        f"target {TARGET_HOURS:g}h · min age {s['session_min_hours']}h"
        f" · max age {s['session_max_hours'] or 'off'}\n"
        f"mail {s['mail_mode']} · 2fa {s['password_mode']}"
        f" · cap ${s['daily_spend_cap']}\n"
        f"include {esc(s['countries'] or 'all')} · exclude {esc(s['deny_countries'] or '—')}"
    )
    if note:
        body += f"\n⚠️ {esc(note)}"
    return body


def filters_text(s: dict) -> str:
    return (
        "<b>Filters</b>\n"
        f"{BAR}\n"
        f"price {s['price_min']} – {s['price_max']}\n"
        f"spam {s['spam_mode']} · mail {s['mail_mode']} · 2fa {s['password_mode']}\n"
        f"min age {s['session_min_hours']}h · max age {s['session_max_hours'] or 'off'}\n"
        f"include {esc(s['countries'] or 'all')}\n"
        f"exclude {esc(s['deny_countries'] or '—')}\n"
        f"cap ${s['daily_spend_cap']} · terminate {s['terminate_on_expire']}"
        f" · mail chg {s['change_mail_on_expire']}\n"
        f"mail src {s['mail_provider']} · 2fa <code>{esc(s['twofa_password'])}</code>\n"
        f"{BAR}\n"
        f"age ≥ {TARGET_HOURS:g}h → straight to audit, else queued"
    )


def wallet_text(snap: dict) -> str:
    total, spend = snap["total"], snap["spendable"]
    head = f"<b>Wallet</b> · market\n{BAR}"
    head += f"\nbalance <b>{fmt_money(total, snap['currency'])}</b>"
    if spend + 1e-9 < total:
        head += f"\nspendable {fmt_money(spend, snap['currency'])}"

    lines = []
    for b in snap["entries"]:
        row = f"• {esc(b['label'])} {fmt_money(b.get('amount', 0.0), b['currency'])}"
        if b.get("hold"):
            row += f" · hold {fmt_money(b['hold'], b['currency'])}"
        if not b["usable_for_buy"]:
            row += " · not usable"
        lines.append(row)

    body = head + ("\n" + "\n".join(lines) if lines else "\nno balance entries")
    if snap.get("note"):
        body += f"\n<i>{esc(snap['note'])}</i>"
    if snap.get("error"):
        body += f"\n⚠️ <code>{esc(snap['error'][:120])}</code>"
    return body


def _spam_line(reply: str) -> str:
    low = (reply or "").lower()
    if not low:
        return "unknown ❔"
    if "good news" in low or "no limits" in low:
        return "Good ✅"
    if "limit" in low or "restrict" in low:
        return "Limited ⚠️"
    return "unknown ❔"


def audit_text(p: dict, d: dict, supervisor: str = "") -> str:
    created = p.get("account_created_ts") or 0
    devices = d.get("devices") or []
    ch, gr, bt = (d.get("owned") or (0, 0, 0))[:3]
    owned = []
    if ch:
        owned.append(f"{ch} channel(s)")
    if gr:
        owned.append(f"{gr} group(s)")
    if bt:
        owned.append(f"{bt} bot(s)")
    device_lines = "\n".join(devices) if devices else "—"
    twofa = "ON" if d.get("twofa_ok") else "FAILED"
    mail = f"Added ({esc(d.get('mail'))})" if d.get("mail_ok") else "not changed"

    return (
        f"🌟 <b>Audit Report</b> | {esc(p.get('phone') or p['item_id'])}\n"
        f"{BAR}\n"
        f" ├ 🌍 Country: {esc(p.get('country') or '??')}\n"
        f" ├ 📅 Age: {fmt_year(created)}\n"
        f" ├ 🚦 Spam: {_spam_line(d.get('spam') or '')}\n"
        f" ├ 🔐 2FA: {twofa}\n"
        f" ├ 📧 Mail: {mail}\n"
        f" └ 📱 Devices: {len(devices)} active\n"
        f"{BAR}\n"
        f"🤖 <b>SpamBot Reply:</b>\n"
        f"{esc(d.get('spam') or 'no reply')}\n\n"
        f"💻 <b>Device List:</b>\n"
        f"{esc(device_lines)}\n\n"
        f"🏛️ <b>Owned Channels / Groups / Bots:</b> "
        f"{esc(', '.join(owned) if owned else 'None')}\n\n"
        f"👑 <b>Supervisor:</b> @{esc(supervisor or '—')}\n\n"
        f"🔑 <b>2FA Password:</b> <code>{esc(d.get('password') or '—')}</code>\n\n"
        f"🎉 <b>Account bought successfully</b>"
    )


def trade_text(p: dict, offer: dict, supervisor: str, sessions: int,
               confirm: int, sale_id: str = "") -> str:
    cc = (p.get("country") or "").upper()
    phone = (p.get("phone") or "").lstrip("+")
    masked = f"{phone[:4]}{'*' * max(0, len(phone) - 7)}{phone[-3:]}" if len(phone) > 7 else phone
    buy = float(p.get("price") or 0.0)
    sold = float(offer.get("price") or 0.0)
    profit = sold - buy
    roi = (profit / buy * 100) if buy else 0.0
    from app.config import INR_RATE
    sign = "+" if profit >= 0 else "-"
    return (
        "🎉 <b>ACCOUNT TRADE COMPLETED SUCCESSFULLY!</b> 💰\n"
        f"{BAR}\n"
        f"👑 Supervisor: @{esc(supervisor or '—')} | "
        f"Receiver: @{esc(offer.get('receiver') or '—')}\n"
        f"• 🆔 Job: {esc(sale_id[:14])} | LZT: {esc(p.get('item_id'))}\n"
        f"• 📱 Phone: {esc(masked)} ({esc(cc or '??')})\n"
        f"• 💵 Buy Price: ${buy:.2f} (₹{buy * INR_RATE:.1f})\n"
        f"• 💰 Sold Price: ${sold:.2f}\n"
        f"• 📈 Net Profit: {sign}${abs(profit):.2f} ({sign}{abs(roi):.1f}%)\n"
        f"• 2FA Password: Verified Active\n"
        f"• Session Count Pre-Term: {sessions} (Ours + Receiver)\n"
        f"• Spam Status: Good ✅\n"
        f"• Local Session: Terminated & Logged Out\n"
        f"• Payout Delay: {confirm}s (Reconciled)\n"
        f"• Mode: 🟢 LIVE TRADE"
    )


def session_block(p: dict) -> str:
    session = p.get("session_string") or ""
    if not session:
        return "🔌 session: <i>pending</i>"
    return f"🔐 <b>Telethon session</b>\n<code>{esc(session)}</code>"


def session_detail(p: dict, checked: str = "") -> str:
    created = p.get("account_created_ts") or 0
    state = p.get("status") or "queued"
    body = (
        f"📱 <b>{esc(p.get('phone') or '—')}</b>\n"
        f"item <code>{esc(p['item_id'])}</code> · {esc(p.get('country') or '??')}\n"
        f"created {fmt_dt(created) if created else 'unknown'}"
        f" · age {fmt_age(p.get('age_at_buy_hours'))} at buy\n"
        f"bought {fmt_dt(p.get('started_at'))} for {fmt_money(p.get('price') or 0.0)}\n"
        f"state <b>{esc(state)}</b> · target {TARGET_HOURS:g}h\n"
        f"⏳ {fmt_left(p.get('expires_at')) if state in ('queued', 'cleaning') else 'cleaned'}\n"
        f"session {esc(p.get('session_state') or 'pending')}"
        f" · checked {fmt_dt(p.get('checked_at')) if p.get('checked_at') else '—'}\n"
        f"📧 {esc(p.get('mail') or '—')}"
    )
    if p.get("sell_state"):
        body += f"\n💱 sell {esc(p['sell_state'])}"
    if checked:
        body += f"\n\n{esc(checked)}"
    if p.get("note"):
        body += f"\n📝 {esc(str(p['note'])[:200])}"
    return body


def session_file(p: dict) -> str:
    created = p.get("account_created_ts") or 0
    return (
        f"phone = {p.get('phone') or ''}\n"
        f"item_id = {p['item_id']}\n"
        f"country = {p.get('country') or ''}\n"
        f"mail = {p.get('mail') or ''}\n"
        f"password = {p.get('password') or ''}\n"
        f"account_created = {fmt_dt(created) if created else 'unknown'}\n"
        f"age_at_buy_hours = {p.get('age_at_buy_hours', 0)}\n"
        f"bought_at = {fmt_dt(p.get('started_at'))}\n\n"
        f"from telethon.sync import TelegramClient\n"
        f"from telethon.sessions import StringSession\n\n"
        f"API_ID = {API_ID}\n"
        f"API_HASH = \"{API_HASH}\"\n\n"
        f"session = StringSession(\n    \"{p.get('session_string') or ''}\"\n)\n"
        f"client = TelegramClient(session, API_ID, API_HASH)\n"
        f"client.start()\n"
        f"print(client.get_me())\n"
    )


def sessions_txt(rows: list[dict], title: str) -> str:
    head = [f"# {title}", f"# count {len(rows)}", ""]
    for p in rows:
        head.append(
            f"{p.get('phone') or '—'} | {p.get('country') or '??'} | "
            f"{p.get('status')} | age {p.get('_age', 0):.1f}h | "
            f"item {p.get('item_id')} | id {p['_id']}")
    return "\n".join(head) + "\n"


def session_stats_text(st: dict, sold: dict | None = None) -> str:
    body = (
        "<b>Session stats</b>\n"
        f"{BAR}\n"
        f"total active {st['total']} · live {st['live']}\n"
        f"queue {st['queue']} · ready {st['ready']} · sold {st['sold']}\n"
        f"older than {TARGET_HOURS:g}h <b>{st['over']}</b>"
    )
    if sold:
        body += (f"\n{BAR}\ntrades {sold['total']} · sold {sold['sold']}"
                 f" · failed {sold['failed']}\n"
                 f"profit {sold['profit']:.2f} · roi {sold['roi']:.1f}%")
    return body


def stats_text(st: dict, currency: str = "usd") -> str:
    return (
        "<b>Stats</b>\n"
        f"trades {st['total']} · closed {st['done']}\n"
        f"spent {fmt_money(st['spent'], currency)}\n"
        f"revenue {fmt_money(st['revenue'], currency)}\n"
        f"net {fmt_money(st['profit'], currency)} ({st['roi']:.1f}% roi)"
    )


def report_text(rows: list[dict]) -> str:
    if not rows:
        return "<b>Report</b>\nno cycles yet"
    lines = ["<b>Report</b> · last cycles", BAR]
    for r in rows:
        stamp = fmt_dt(r.get("ts"))
        if r.get("error"):
            lines.append(f"{stamp} · ⚠️ {esc(str(r['error'])[:80])}")
            continue
        if r.get("note"):
            lines.append(f"{stamp} · {esc(str(r['note'])[:60])}")
            continue
        top = ", ".join(f"{esc(k)}×{v}" for k, v in
                        sorted((r.get("reasons") or {}).items(), key=lambda x: -x[1])[:3])
        lines.append(f"{stamp} · scan {r.get('scanned', 0)}"
                     f" · buy {r.get('bought', 0)} · skip {r.get('skipped', 0)}"
                     + (f" · {top}" if top else ""))
    return "\n".join(lines)


def payment_text(p: dict) -> str:
    kind = str(p.get("operation_type") or p.get("operation") or p.get("type") or "op")
    sign = "+" if kind.lower() in ("income", "incoming", "in", "deposit") else "−"
    amount = abs(float(p.get("amount") or p.get("sum") or 0))
    note = p.get("comment") or p.get("note") or p.get("description") or ""
    return (f"{sign} {fmt_money(amount, p.get('currency') or 'usd')}"
            f" · {esc(kind)} · {esc(str(note)[:30])}")


def _offer_line(cc: str, dial: str, name: str, price: float, receiver: str = "") -> str:
    flag = countries.flag_of(cc)
    head = f"{flag} <b>{cc}</b> +{dial}" if dial else f"{flag} <b>{cc}</b>"
    body = f"{head} {esc(name)} → ${price:.2f}"
    return f"{body} @{esc(receiver)}" if receiver else body


def receivers_text(rows: list[dict]) -> str:
    if not rows:
        return "<b>Receivers</b>\nno receiver added"
    lines = ["<b>Receivers</b>", BAR]
    for r in rows:
        cap = r.get("cap")
        cap_text = "?" if cap is None or cap < 0 else str(cap)
        probe = "link" if r.get("kind") == "lion" else "/cap"
        mark = "🟢" if r.get("enabled") else "⚪"
        lines.append(f"{mark} @{esc(r['username'])} · {esc(r.get('kind'))}"
                     f" · {probe} · cap {cap_text}"
                     f" · confirm {r.get('confirm_seconds') or 'auto'}s"
                     f" · fails {r.get('fails', 0)}")
    return "\n".join(lines)


def receiver_detail_text(r: dict, offers: int = 0) -> str:
    cap = r.get("cap")
    probe = "link" if r.get("kind") == "lion" else "/cap"
    return (
        f"<b>@{esc(r['username'])}</b>\n"
        f"{BAR}\n"
        f"kind {esc(r.get('kind'))} · probe {probe}"
        f" · {'🟢 on' if r.get('enabled') else '⚪ off'}\n"
        f"cap {'?' if cap is None or cap < 0 else cap}"
        f" · confirm {r.get('confirm_seconds') or 'auto'}s\n"
        f"fails {r.get('fails', 0)} · countries cached {offers}"
    )


def extracted_text(offers: list[dict], receiver: str = "") -> str:
    if not offers:
        return "<b>Extracted</b>\nnothing parsed"
    lines = [f"<b>Extracted</b> · {len(offers)}", BAR]
    for o in offers[:80]:
        lines.append(_offer_line(o["country"], o.get("dial", ""),
                                 countries.name_of(o["country"]), o["price"], receiver))
    return "\n".join(lines)


def prices_text(rows: list[dict]) -> str:
    if not rows:
        return "<b>Prices</b>\nno live prices"
    lines = ["<b>Extracted offers</b>", BAR]
    for r in rows[:60]:
        offer = r["offers"][0]
        lines.append(_offer_line(r["country"], offer.get("dial", ""),
                                 countries.name_of(r["country"]), offer["price"],
                                 offer["receiver"]))
        alt = ", ".join(f"@{esc(o['receiver'])} ${o['price']:.2f}"
                        for o in r["offers"][1:3])
        if alt:
            lines.append(f"   next {alt}")
    return "\n".join(lines)


def chart_text(rows: list[dict], inr: float = 0.0) -> str:
    if not rows:
        return "<b>Price chart</b>\nno data"
    top = max((r["best"]["price"] for r in rows if r.get("best")), default=1.0) or 1.0
    lines = ["<b>Price chart</b> · highest bid per country", BAR]
    for r in rows[:60]:
        best = r.get("best")
        if not best:
            continue
        blocks = max(1, int(round(best["price"] / top * 10)))
        dial = f" +{r['dial']}" if r.get("dial") else ""
        lines.append(
            f"{r['flag']} <b>{r['country']}</b>{dial} {esc(r['name'])}\n"
            f"{'▰' * blocks}{'▱' * (10 - blocks)} ${best['price']:.2f}"
            + (f" (₹{best['price'] * inr:.1f})" if inr else "")
            + f" · @{esc(best['receiver'])}")
    return "\n".join(lines)


def chart_csv(rows: list[dict]) -> str:
    out = ["country,name,dial,best_price,best_receiver,all_receivers"]
    for r in rows:
        best = r.get("best") or {}
        others = " | ".join(f"{c['receiver']}:{c['price']}" for c in r["cells"]
                            if c.get("price"))
        out.append(f"{r['country']},{r['name']},{r.get('dial', '')},"
                   f"{best.get('price', 0)},{best.get('receiver', '')},\"{others}\"")
    return "\n".join(out) + "\n"


def sell_menu_text(s: dict, ready: int) -> str:
    return (
        "<b>Sell</b>\n"
        f"{BAR}\n"
        f"auto sell {'🟢 on' if s['auto_sell'] == '1' else '🔴 off'}\n"
        f"min sell price ${s['sell_min_price']} · confirm {s['sell_confirm_seconds']}s\n"
        f"ready accounts {ready}\n"
        f"tie on equal price → TG GET first"
    )