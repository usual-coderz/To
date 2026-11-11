"""Message formatting — HTML, every dynamic value escaped."""
from app.config import API_HASH, API_ID
from app.utils.money import fmt_money
from app.utils.timeutil import fmt_age, fmt_dt, fmt_left
from app.utils.tg import esc


def menu_text(s: dict, profile: dict, total: float, spendable: float,
              queued: int, note: str = "") -> str:
    body = (
        "<b>AccStore</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 {esc(profile.get('username', '?'))}\n"
        f"💵 <b>{fmt_money(total, s['currency'])}</b>"
    )
    if spendable + 1e-9 < total:
        body += f" · {fmt_money(spendable, s['currency'])} spendable"
    body += (
        "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{'🟢 running' if s['auto_buy'] == '1' else '🔴 stopped'}"
        f" · queue {queued}\n"
        f"price {s['price_min']}–{s['price_max']} · spam {s['spam_mode']}\n"
        f"age {s['session_min_hours']}–{s['session_max_hours'] or '∞'}h"
        f" · life {s['session_life_hours']}h\n"
        f"mail {s['mail_mode']} · 2fa {s['password_mode']}\n"
        f"include {esc(s['countries'] or 'all')} · exclude {esc(s['deny_countries'] or '—')}"
    )
    if note:
        body += f"\n⚠️ {esc(note)}"
    return body


def filters_text(s: dict) -> str:
    return (
        "<b>Filters</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"price {s['price_min']} – {s['price_max']}\n"
        f"spam {s['spam_mode']} · mail {s['mail_mode']} · 2fa {s['password_mode']}\n"
        f"min age {s['session_min_hours']}h · max age {s['session_max_hours'] or '∞'}h\n"
        f"lifetime after buy {s['session_life_hours']}h\n"
        f"include {esc(s['countries'] or 'all')}\n"
        f"exclude {esc(s['deny_countries'] or '—')}"
    )


def limits_text(s: dict) -> str:
    return (
        "<b>Limits</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"daily cap ${s['daily_spend_cap']} (0 = unlimited)\n"
        f"buys per cycle {s['max_buy_per_cycle']} · poll {s['poll_interval']}s\n"
        f"early clean {s['early_clean_if_aged']}\n"
        f"terminate {s['terminate_on_expire']} · change mail {s['change_mail_on_expire']}\n"
        f"mail source {s['mail_provider']}\n"
        f"proxy {'set' if s.get('proxy') else 'off'}"
    )


def wallet_text(snap: dict) -> str:
    total, spend = snap["total"], snap["spendable"]
    head = "<b>Wallet</b> · market\n━━━━━━━━━━━━━━━━━━━━━━"
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


def account_card(p: dict) -> str:
    """Identity block shared by every purchase message."""
    created = p.get("account_created_ts") or 0
    lines = [
        f"📱 <b>{esc(p.get('phone') or '—')}</b>",
        f"🌍 <b>{esc(p.get('country') or '??')}</b> · item <code>{esc(p['item_id'])}</code>",
        f"💵 {fmt_money(p.get('price') or 0.0)} · bought {fmt_dt(p.get('started_at'))}",
        f"🗓 created {fmt_dt(created) if created else 'unknown'}"
        f" · age {fmt_age(p.get('age_at_buy_hours'))}",
    ]
    if p.get("password"):
        lines.append(f"🔑 <code>{esc(p['password'])}</code>")
    if p.get("mail"):
        lines.append(f"📧 {esc(p['mail'])}")
    if p.get("spam_flag"):
        lines.append("🚫 spam flag set")
    if p.get("expires_at"):
        lines.append(f"⏳ closes {fmt_dt(p['expires_at'])}"
                     f" · {fmt_left(p['expires_at'])}")
    return "\n".join(lines)


def session_block(p: dict) -> str:
    session = p.get("session_string") or ""
    if not session:
        return "🔌 session: <i>pending</i>"
    return f"🔐 <b>Telethon session</b>\n<code>{esc(session)}</code>"


def bought_text(p: dict, stage: str = "ready") -> str:
    head = {
        "bought": "🛒 <b>Bought</b> — logging in…",
        "ready": "✅ <b>Session ready</b>",
        "failed": "⚠️ <b>Login failed</b>",
    }.get(stage, "🛒 <b>Bought</b>")
    body = f"{head}\n━━━━━━━━━━━━━━━━━━━━━━\n{account_card(p)}"
    if stage == "ready":
        body += f"\n\n{session_block(p)}"
    if p.get("note"):
        body += f"\n📝 {esc(str(p['note'])[:150])}"
    return body


def session_detail(p: dict, checked: str = "") -> str:
    created = p.get("account_created_ts") or 0
    body = (
        f"📱 <b>{esc(p.get('phone') or '—')}</b>\n"
        f"item <code>{esc(p['item_id'])}</code> · {esc(p.get('country') or '??')}\n"
        f"created {fmt_dt(created) if created else 'unknown'}"
        f" · age {fmt_age(p.get('age_at_buy_hours'))} at buy\n"
        f"bought {fmt_dt(p.get('started_at'))} for {fmt_money(p.get('price') or 0.0)}\n"
        f"lifetime {p.get('target_age_hours', 24):g}h"
        f" · closes {fmt_dt(p.get('expires_at'))}\n"
        f"⏳ {fmt_left(p.get('expires_at'))}\n"
        f"session {esc(p.get('session_state') or 'pending')}"
        f" · checked {fmt_dt(p.get('checked_at')) if p.get('checked_at') else '—'}\n"
        f"📧 {esc(p.get('mail') or '—')}"
    )
    if checked:
        body += f"\n\n{esc(checked)}"
    if p.get("note"):
        body += f"\n📝 {esc(str(p['note'])[:200])}"
    return body


def session_file(p: dict) -> str:
    created = p.get("account_created_ts") or 0
    return (
        f"# AccStore session\n"
        f"phone = {p.get('phone') or ''}\n"
        f"item_id = {p['item_id']}\n"
        f"country = {p.get('country') or ''}\n"
        f"mail = {p.get('mail') or ''}\n"
        f"account_created = {fmt_dt(created) if created else 'unknown'}\n"
        f"age_at_buy_hours = {p.get('age_at_buy_hours', 0)}\n"
        f"bought_at = {fmt_dt(p.get('started_at'))}\n"
        f"expires_at = {fmt_dt(p.get('expires_at'))}\n\n"
        f"from telethon.sync import TelegramClient\n"
        f"from telethon.sessions import StringSession\n\n"
        f"API_ID = {API_ID}\n"
        f"API_HASH = \"{API_HASH}\"\n\n"
        f"session = StringSession(\n    \"{p.get('session_string') or ''}\"\n)\n"
        f"client = TelegramClient(session, API_ID, API_HASH)\n"
        f"client.start()\n"
        f"print(client.get_me())\n"
    )


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
    lines = ["<b>Report</b> · last cycles", "━━━━━━━━━━━━━━━━━━━━━━"]
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
    sign = "+" if (p.get("operation_type") or "") in ("income", "incoming") else "−"
    return (f"{sign} {fmt_money(abs(float(p.get('amount') or 0)), p.get('currency') or 'usd')}"
            f" · {esc(p.get('operation_type') or 'op')} · {esc((p.get('comment') or '')[:30])}")