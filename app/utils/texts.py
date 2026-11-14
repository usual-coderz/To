from app.config import API_HASH, API_ID, INR_RATE, TARGET_HOURS
from app.utils import countries
from app.utils.money import fmt_money
from app.utils.timeutil import fmt_age, fmt_dt, fmt_left, fmt_year
from app.utils.tg import esc, pre

BAR = "─" * 34
RULE = "─" * 62


def _row(label: str, value: str) -> str:
    return f"{label:<8}{value}"


def bought_line(country: str) -> str:
    return f"✅ <b>{esc(country or '??')}</b> bought"


def failed_line(country: str, reason: str = "") -> str:
    body = f"⚠️ <b>{esc(country or '??')}</b> login failed"
    if reason:
        body += f"\n<code>{esc(str(reason)[:150])}</code>"
    return body


def sell_fail_line(country: str, reason: str = "") -> str:
    body = f"⚠️ <b>{esc(country or '??')}</b> sell failed"
    if reason:
        body += f"\n<code>{esc(str(reason)[:150])}</code>"
    return body


def _config_block(s: dict, queued: int = 0, ready: int = 0) -> str:
    lines = [
        _row("buying", "🟢 on" if s["auto_buy"] == "1" else "🔴 off"),
        _row("selling", "🟢 on" if s["auto_sell"] == "1" else "🔴 off"),
        _row("queue", f"{queued}  ready {ready}"),
        _row("price", f"{s['price_min']} – {s['price_max']}"),
        _row("cap", f"${s['daily_spend_cap']}"),
        _row("age", f"{s['session_min_hours']}h – {s['session_max_hours'] or 'off'}"),
        _row("spam", s["spam_mode"]),
        _row("mail", s["mail_mode"]),
        _row("2fa", s["password_mode"]),
        _row("include", s["countries"] or "all"),
        _row("exclude", s["deny_countries"] or "—"),
        _row("profit", f"x{s['profit_mult']}"),
    ]
    return pre("\n".join(lines), 1200)


def menu_text(s: dict, profile: dict, total: float, spendable: float,
              queued: int, ready: int, note: str = "") -> str:
    body = (
        "<b>AccStore</b>\n"
        f"{BAR}\n"
        f"👤 <code>{esc(profile.get('username', '?'))}</code>\n"
        f"💵 <b>{fmt_money(total, s['currency'])}</b>"
    )
    if spendable + 1e-9 < total:
        body += f" · {fmt_money(spendable, s['currency'])} spendable"
    body += f"\n{BAR}\n{_config_block(s, queued, ready)}"
    if note:
        body += f"\n⚠️ {esc(note)}"
    return body


def filters_text(s: dict) -> str:
    lines = [
        _row("price", f"{s['price_min']} – {s['price_max']}"),
        _row("spam", s["spam_mode"]),
        _row("mail", s["mail_mode"]),
        _row("2fa", s["password_mode"]),
        _row("min age", f"{s['session_min_hours']}h"),
        _row("max age", f"{s['session_max_hours'] or 'off'}"),
        _row("include", s["countries"] or "all"),
        _row("exclude", s["deny_countries"] or "—"),
        _row("cap", f"${s['daily_spend_cap']}"),
        _row("terminate", s["terminate_on_expire"]),
        _row("mail chg", s["change_mail_on_expire"]),
        _row("mail src", s["mail_provider"]),
        _row("2fa pwd", s["twofa_password"]),
    ]
    return (f"<b>Filters</b>\n{BAR}\n{pre(chr(10).join(lines), 1200)}\n{BAR}\n"
            f"age ≥ {TARGET_HOURS:g}h → audit, warna queued")


def profit_text(s: dict) -> str:
    mult = float(s.get("profit_mult") or 1)
    body = "\n".join([
        _row("current", f"x{mult:g}"),
        _row("max buy", f"sell ÷ {mult:g}"),
        "",
        "buy  $0.25  →  pay $0.50  →  sold at x1",
        "no fixed floor, margin decides",
    ])
    return f"<b>Profit multiplier</b>\n{BAR}\n{pre(body, 800)}"


def ready_text(s: dict, count: int, stats: dict | None = None) -> str:
    lines = [
        _row("auto sell", "🟢 on" if s["auto_sell"] == "1" else "🔴 off"),
        _row("queued", str(count)),
        _row("min sell", f"${s['sell_min_price']}"),
        _row("profit", f"x{s['profit_mult']}"),
        _row("tie break", "TG GET first"),
    ]
    body = f"<b>Ready to sell</b>\n{BAR}\n{pre(chr(10).join(lines), 800)}"
    if stats:
        body += (f"\n{BAR}\nsold <code>{stats['sold']}</code>"
                 f" · failed <code>{stats['failed']}</code>")
    return body


def ready_row(p: dict) -> str:
    return (f"{esc(p.get('phone') or p['item_id'])} · {esc(p.get('country') or '??')}"
            f" · {esc(p.get('sell_state') or 'idle')} · try {p.get('sell_try', 0)}")


def ready_list_text(rows: list[dict]) -> str:
    lines = []
    for p in rows:
        lines.append(ready_row(p))
        reason = p.get("sell_reason") or ""
        if reason:
            lines.append(f"   ↳ {str(reason)[:60]}")
    return f"<b>Ready</b> · {len(rows)}\n{BAR}\n{pre(chr(10).join(lines), 3200)}"


def ready_detail_text(p: dict) -> str:
    created = p.get("account_created_ts") or 0
    lines = [
        _row("phone", p.get("phone") or "—"),
        _row("item", str(p.get("item_id"))),
        _row("country", p.get("country") or "??"),
        _row("bought", f"${float(p.get('price') or 0):.2f}"),
        _row("age", fmt_age(p.get("age_at_buy_hours"))),
        _row("created", fmt_dt(created) if created else "unknown"),
        _row("sell", p.get("sell_state") or "idle"),
        _row("try", str(p.get("sell_try", 0))),
        _row("2fa", p.get("password") or "—"),
    ]
    body = pre("\n".join(lines), 1200)
    reason = p.get("sell_reason") or ""
    if reason:
        body += f"\n⚠️ {esc(reason[:200])}"
    return body


def wallet_text(snap: dict) -> str:
    total, spend = snap["total"], snap["spendable"]
    head = f"<b>Wallet</b> · market\n{BAR}\nbalance <b>{fmt_money(total, snap['currency'])}</b>"
    if spend + 1e-9 < total:
        head += f"\nspendable {fmt_money(spend, snap['currency'])}"
    lines = []
    for b in snap["entries"]:
        row = f"{b['label']} {fmt_money(b.get('amount', 0.0), b['currency'])}"
        if b.get("hold"):
            row += f" · hold {fmt_money(b['hold'], b['currency'])}"
        if not b["usable_for_buy"]:
            row += " · not usable"
        lines.append(row)
    body = head + "\n" + (pre("\n".join(lines), 1500) if lines else "no balance entries")
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
    owned = [x for x in (f"{ch} channel(s)" if ch else "",
                         f"{gr} group(s)" if gr else "",
                         f"{bt} bot(s)" if bt else "") if x]
    head = "\n".join([
        _row("phone", p.get("phone") or str(p.get("item_id"))),
        _row("country", p.get("country") or "??"),
        _row("age", fmt_year(created)),
        _row("spam", _spam_line(d.get("spam") or "")),
        _row("2fa", "ON" if d.get("twofa_ok") else "FAILED"),
        _row("mail", f"added {d.get('mail')}" if d.get("mail_ok") else "not changed"),
        _row("devices", str(len(devices))),
        _row("owned", ", ".join(owned) or "None"),
        _row("2fa pwd", d.get("password") or "—"),
    ])
    return (
        f"🌟 <b>Audit Report</b>\n{BAR}\n{pre(head, 1500)}\n{BAR}\n"
        f"🤖 <b>SpamBot</b>\n{pre(d.get('spam') or 'no reply', 800)}\n"
        f"💻 <b>Devices</b>\n{pre(chr(10).join(devices) or '—', 1200)}\n"
        f"{BAR}\n👑 @{esc(supervisor or '—')}\n🎉 bought successfully"
    )


def sell_audit_text(p: dict, reason: str, stage: str, receiver: str,
                    price: float, supervisor: str = "") -> str:
    cc = (p.get("country") or "??").upper()
    buy = float(p.get("price") or 0.0)
    head = "\n".join([
        _row("phone", p.get("phone") or "—"),
        _row("item", str(p.get("item_id"))),
        _row("buy", f"${buy:.2f}"),
        _row("bid", f"${float(price or 0):.2f}"),
        _row("receiver", f"@{receiver or '—'}"),
        _row("stage", stage),
    ])
    return (
        f"🧾 <b>SELL AUDIT</b> · <code>{esc(cc)}</code>\n{BAR}\n"
        f"{pre(head, 1000)}\n{BAR}\n"
        f"❌ not sold\n📄 {esc(str(reason or 'unknown')[:220])}\n{BAR}\n"
        f"👑 @{esc(supervisor or '—')} · 🕒 {fmt_dt(p.get('closed_at') or 0)}"
    )


def trade_text(p: dict, offer: dict, supervisor: str, sessions: int,
               confirm: int, sale_id: str, sold_price: float, totals: dict) -> str:
    cc = (p.get("country") or "").upper()
    phone = (p.get("phone") or "").lstrip("+")
    masked = f"{phone[:4]}{'*' * max(0, len(phone) - 7)}{phone[-3:]}" if len(phone) > 7 else phone
    buy = float(p.get("price") or 0.0)
    sold = float(sold_price or 0.0)
    profit = sold - buy
    roi = (profit / buy * 100) if buy else 0.0
    sign = "+" if profit >= 0 else "-"
    head = "\n".join([
        _row("sale", sale_id[:14]),
        _row("item", str(p.get("item_id"))),
        _row("phone", masked),
        _row("country", cc or "??"),
        _row("buy", f"${buy:.2f} (₹{buy * INR_RATE:.0f})"),
        _row("sold", f"${sold:.2f}"),
        _row("roi", f"{sign}{abs(roi):.1f}%"),
        _row("profit", f"{sign}${abs(profit):.2f}"),
        _row("sessions", str(sessions)),
        _row("payout", f"{confirm}s"),
    ])
    tail = "\n".join([
        _row("invested", f"${totals['investment']:.2f}"),
        _row("sold", f"${totals['sold']:.2f}"),
        _row("profit", f"${totals['profit']:.2f} ({totals['roi']:.1f}%)"),
        _row("trades", str(totals["count"])),
    ])
    return (
        f"🎉 <b>TRADE COMPLETED</b>\n{BAR}\n"
        f"👑 @{esc(supervisor or '—')} → @{esc(offer.get('receiver') or '—')}\n"
        f"{pre(head, 1200)}\n{BAR}\n{pre(tail, 600)}"
    )


def session_block(p: dict) -> str:
    session = p.get("session_string") or ""
    if not session:
        return "🔌 session: <i>pending</i>"
    return f"🔐 <b>Telethon session</b>\n<code>{esc(session)}</code>"


def session_detail(p: dict, checked: str = "") -> str:
    created = p.get("account_created_ts") or 0
    state = p.get("status") or "queued"
    lines = [
        _row("phone", p.get("phone") or "—"),
        _row("item", str(p.get("item_id"))),
        _row("country", p.get("country") or "??"),
        _row("created", fmt_dt(created) if created else "unknown"),
        _row("age", fmt_age(p.get("age_at_buy_hours"))),
        _row("bought", fmt_dt(p.get("started_at"))),
        _row("price", f"${float(p.get('price') or 0):.2f}"),
        _row("state", state),
        _row("left", fmt_left(p.get("expires_at")) if state in ("queued", "cleaning") else "cleaned"),
        _row("session", p.get("session_state") or "pending"),
        _row("mail", p.get("mail") or "—"),
    ]
    body = pre("\n".join(lines), 1400)
    if p.get("sell_state"):
        body += f"\n💱 {esc(p['sell_state'])}"
        if p.get("sell_reason"):
            body += f"\n⚠️ {esc(str(p['sell_reason'])[:150])}"
    if checked:
        body += f"\n\n{esc(checked)}"
    if p.get("note"):
        body += f"\n📝 {esc(str(p['note'])[:200])}"
    return body


def session_list_text(rows: list[dict], label: str) -> str:
    lines = [f"{esc(p.get('phone') or p['item_id'])} · {esc(p.get('status'))}"
             f" · {p.get('_age', 0):.1f}h" for p in rows]
    return f"<b>{esc(label)}</b> · {len(rows)}\n{BAR}\n{pre(chr(10).join(lines), 3000)}"


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


def session_bundle(rows: list[dict], label: str = "") -> str:
    out = []
    for p in rows:
        session = (p.get("session_string") or "").strip()
        if not session:
            continue
        out.append(f"{p.get('phone') or ''}|{p.get('country') or ''}|{session}")
    body = "\n".join(out)
    return body + "\n" if body else ""


def session_stats_text(st: dict, sold: dict | None = None) -> str:
    lines = [
        _row("total", str(st["total"])),
        _row("live", str(st["live"])),
        _row("dead", str(st["dead"])),
        _row("queue", str(st["queue"])),
        _row("ready", str(st["ready"])),
        _row("sold", str(st["sold"])),
        _row(f"over {TARGET_HOURS:g}h", str(st["over"])),
        _row("selling", str(st["queued_sell"])),
        _row("sell fail", str(st["sell_failed"])),
    ]
    body = f"<b>Session stats</b>\n{BAR}\n{pre(chr(10).join(lines), 900)}"
    if sold:
        body += (f"\n{BAR}\ntrades <code>{sold['total']}</code>"
                 f" · sold <code>{sold['sold']}</code>"
                 f" · failed <code>{sold['failed']}</code>")
    return body


def stats_text(st: dict, currency: str = "usd") -> str:
    lines = [
        _row("trades", str(st["total"])),
        _row("closed", str(st["done"])),
        _row("spent", fmt_money(st["spent"], currency)),
        _row("revenue", fmt_money(st["revenue"], currency)),
        _row("net", fmt_money(st["profit"], currency)),
        _row("roi", f"{st['roi']:.1f}%"),
    ]
    return f"<b>Stats</b>\n{BAR}\n{pre(chr(10).join(lines), 800)}"


def ledger_text(st: dict) -> str:
    lines = [
        _row("rows", str(st["total"])),
        _row("closed", str(st["done"])),
        _row("spent", f"{st['spent']:.2f}"),
        _row("revenue", f"{st['revenue']:.2f}"),
    ]
    return f"<b>Ledger</b>\n{BAR}\n{pre(chr(10).join(lines), 700)}\nexport for the full CSV"


def report_text(rows: list[dict]) -> str:
    if not rows:
        return "<b>Report</b>\nno cycles yet"
    lines = []
    for r in rows:
        stamp = fmt_dt(r.get("ts"))
        if r.get("error"):
            lines.append(f"{stamp} · ⚠️ {str(r['error'])[:80]}")
            continue
        if r.get("note"):
            lines.append(f"{stamp} · {str(r['note'])[:60]}")
            continue
        top = ", ".join(f"{k}×{v}" for k, v in
                        sorted((r.get("reasons") or {}).items(), key=lambda x: -x[1])[:3])
        lines.append(f"{stamp} · scan {r.get('scanned', 0)}"
                     f" · buy {r.get('bought', 0)} · skip {r.get('skipped', 0)}"
                     + (f" · {top}" if top else ""))
    return f"<b>Report</b> · last cycles\n{BAR}\n{pre(chr(10).join(lines), 3200)}"


def payment_text(p: dict) -> str:
    kind = str(p.get("operation_type") or p.get("operation") or p.get("type") or "op")
    sign = "+" if kind.lower() in ("income", "incoming", "in", "deposit") else "−"
    amount = abs(float(p.get("amount") or p.get("sum") or 0))
    note = p.get("comment") or p.get("note") or p.get("description") or ""
    return f"{sign} {fmt_money(amount, p.get('currency') or 'usd')} · {kind} · {str(note)[:30]}"


def _offer_line(cc: str, dial: str, name: str, price: float, receiver: str = "") -> str:
    head = f"{countries.flag_of(cc)} {cc} +{dial}" if dial else f"{countries.flag_of(cc)} {cc}"
    body = f"{head} {name} → ${price:.2f}"
    return f"{body} @{receiver}" if receiver else body


def receivers_text(rows: list[dict]) -> str:
    if not rows:
        return "<b>Receivers</b>\nno receiver added"
    lines = []
    for r in rows:
        cap = r.get("cap")
        cap_text = "?" if cap is None or cap < 0 else str(cap)
        probe = "link" if r.get("kind") == "lion" else "/cap"
        lines.append(f"{'🟢' if r.get('enabled') else '⚪'} @{r['username']}"
                     f" · {r.get('kind')} · {probe} · cap {cap_text}"
                     f" · confirm {r.get('confirm_seconds') or 'auto'}s"
                     f" · fails {r.get('fails', 0)}")
    return f"<b>Receivers</b>\n{BAR}\n{pre(chr(10).join(lines), 3000)}"


def receiver_detail_text(r: dict, offers: int = 0) -> str:
    cap = r.get("cap")
    lines = [
        _row("kind", r.get("kind") or "?"),
        _row("probe", "link" if r.get("kind") == "lion" else "/cap"),
        _row("state", "🟢 on" if r.get("enabled") else "⚪ off"),
        _row("cap", "?" if cap is None or cap < 0 else str(cap)),
        _row("confirm", f"{r.get('confirm_seconds') or 'auto'}s"),
        _row("fails", str(r.get("fails", 0))),
        _row("cached", str(offers)),
    ]
    return f"<b>@{esc(r['username'])}</b>\n{BAR}\n{pre(chr(10).join(lines), 800)}"


def extracted_text(offers: list[dict], receiver: str = "") -> str:
    if not offers:
        return "<b>Extracted</b>\nnothing parsed"
    lines = [_offer_line(o["country"], o.get("dial", ""),
                         countries.name_of(o["country"]), o["price"], receiver)
             for o in offers[:80]]
    return f"<b>Extracted</b> · {len(offers)}\n{BAR}\n{pre(chr(10).join(lines), 3200)}"


def prices_text(rows: list[dict]) -> str:
    if not rows:
        return "<b>Prices</b>\nno live prices"
    lines = []
    for r in rows[:60]:
        offer = r["offers"][0]
        lines.append(_offer_line(r["country"], offer.get("dial", ""),
                                 countries.name_of(r["country"]), offer["price"],
                                 offer["receiver"]))
        alt = ", ".join(f"@{o['receiver']} ${o['price']:.2f}" for o in r["offers"][1:3])
        if alt:
            lines.append(f"   next {alt}")
    return f"<b>Extracted offers</b>\n{BAR}\n{pre(chr(10).join(lines), 3200)}"


def _chart_table(rows: list[dict]) -> str:
    head = (f"{'ISO':<4}{'DIAL':<7}{'COUNTRY':<18}{'SELL':>7}"
            f"{'MAXBUY':>8}{'CAP':>5}  RECEIVER")
    lines = [head, RULE]
    for r in rows:
        dial = f"+{r['dial']}" if r.get("dial") else "-"
        name = (r["name"] or r["country"])[:17]
        lines.append(
            f"{r['country']:<4}{dial:<7}{name:<18}"
            f"{'$' + format(r['sell'], '.2f'):>7}"
            f"{'$' + format(r['maxbuy'], '.2f'):>8}"
            f"{r.get('cap', 0):>5}  @{r['receiver']}")
    return "\n".join(lines)


def chart_text(rows: list[dict], profit: float = 1.0) -> str:
    if not rows:
        return "<b>Price chart</b>\nno live prices"
    head = (f"<b>Price chart</b> · best bid per country\n"
            f"profit <code>x{profit:g}</code> · max buy = sell ÷ {profit:g}\n")
    return head + pre(_chart_table(rows), 3600)


def chart_copy(rows: list[dict], profit: float = 1.0) -> str:
    if not rows:
        return "no live prices"
    return _chart_table(rows)


def chart_csv(rows: list[dict]) -> str:
    out = ["country,name,dial,sell,maxbuy,receiver"]
    for r in rows:
        out.append(f"{r['country']},{r['name']},{r.get('dial', '')},"
                   f"{r['sell']:.2f},{r['maxbuy']:.2f},{r['receiver']}")
    return "\n".join(out) + "\n"


def caps_text(rows: list[dict]) -> str:
    if not rows:
        return "<b>Capacity</b>\nno cap record yet"
    lines = [f"@{r.get('_id') or '—'} · cap {int(r.get('cap') or 0)}"
             f" · {fmt_dt(r.get('ts'))}" for r in rows]
    return f"<b>Capacity · last sweep</b>\n{BAR}\n{pre(chr(10).join(lines), 2600)}"