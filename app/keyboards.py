from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.utils.money import free_of, fmt_money

BACK_MAIN = "m:main"
BACK_FILTERS = "m:filters"
BACK_WALLET = "m:wallet"
BACK_SESSIONS = "m:sessions"
BACK_READY = "m:ready"
BACK_RECEIVERS = "m:receivers"
BACK_PROFIT = "m:profit"
BACK_CHART = "m:chart"

MAX_DATA = 64


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    widths: list[int] = []
    for row in rows:
        clean = [(t, d) for t, d in row if t and d and len(d.encode()) <= MAX_DATA]
        if not clean:
            continue
        for text, data in clean:
            b.button(text=text, callback_data=data)
        widths.append(len(clean))
    if widths:
        b.adjust(*widths)
    return b.as_markup()


def back(to: str = BACK_MAIN) -> InlineKeyboardMarkup:
    return kb([[("‹ Back", to)]])


def _onoff(flag: str) -> str:
    return "🟢 on" if flag == "1" else "🔴 off"


def main(s: dict, ready: int) -> InlineKeyboardMarkup:
    return kb([
        [("⏸ Stop buying" if s["auto_buy"] == "1" else "▶️ Start buying", "t:auto_buy")],
        [("Sessions", BACK_SESSIONS), (f"Ready {ready}", BACK_READY)],
        [("📊 Prices", BACK_CHART), (f"Profit x{s['profit_mult']}", BACK_PROFIT)],
        [("Receivers", BACK_RECEIVERS), ("Wallet", BACK_WALLET)],
        [("Stats", "m:stats"), ("Report", "m:report")],
        [("Filters", BACK_FILTERS)],
    ])


def filters(s: dict) -> InlineKeyboardMarkup:
    return kb([
        [(f"Price {s['price_min']}–{s['price_max']}", "m:price")],
        [(f"Spam {s['spam_mode']}", "m:spam"), (f"Mail {s['mail_mode']}", "m:mail")],
        [(f"2FA {s['password_mode']}", "m:pass")],
        [(f"Min age {s['session_min_hours']}h", "in:session_min_hours"),
         (f"Max age {s['session_max_hours'] or 'off'}", "in:session_max_hours")],
        [("Include", "in:countries"), ("Exclude", "in:deny_countries")],
        [(f"Cap ${s['daily_spend_cap']}", "in:daily_spend_cap")],
        [(f"Terminate {s['terminate_on_expire']}", "t:terminate_on_expire"),
         (f"Mail chg {s['change_mail_on_expire']}", "t:change_mail_on_expire")],
        [(f"Mail src {s['mail_provider']}", "m:mailprov"), ("2FA pwd", "in:twofa_password")],
        [("‹ Back", BACK_MAIN)],
    ])


def price() -> InlineKeyboardMarkup:
    return kb([
        [(f"max {v}", f"s:price_max:{v}") for v in ("0.10", "0.30", "1.00")],
        [(f"min {v}", f"s:price_min:{v}") for v in ("0.05", "0.15", "0.25")],
        [("custom max", "in:price_max"), ("custom min", "in:price_min")],
        [("‹ Back", BACK_FILTERS)],
    ])


def profit(s: dict) -> InlineKeyboardMarkup:
    return kb([
        [(f"x{v}", f"s:profit_mult:{v}") for v in ("1", "1.1", "1.2")],
        [(f"x{v}", f"s:profit_mult:{v}") for v in ("1.5", "2", "3")],
        [("custom", "in:profit_mult")],
        [("‹ Back", BACK_MAIN)],
    ])


def chart() -> InlineKeyboardMarkup:
    return kb([
        [("📋 Copy code", "ch:copy")],
        [("↻ Refresh", BACK_CHART), ("Caps", "ch:caps")],
        [("‹ Back", BACK_MAIN)],
    ])


def caps() -> InlineKeyboardMarkup:
    return kb([[("↻ Sweep now", "ch:sweep")], [("‹ Back", BACK_CHART)]])


def pick(prefix: str, values: list[str], back_to: str = BACK_FILTERS) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for v in values:
        row.append((v, f"{prefix}:{v}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("‹ Back", back_to)])
    return kb(rows)


def sessions(stats: dict) -> InlineKeyboardMarkup:
    return kb([
        [(f"Queue {stats['queue']}", "q:list"), (f"24h+ {stats['over']}", "q:over")],
        [(f"Ready {stats['ready']}", "q:ready"), ("Stats", "q:stats")],
        [("‹ Back", BACK_MAIN)],
    ])


def session_file_bar(page: str) -> InlineKeyboardMarkup:
    return kb([
        [("↻ Resend", f"q:{page}"), ("List", f"ql:{page}")],
        [("‹ Back", BACK_SESSIONS)],
    ])


def session_list(rows: list[dict], page: str) -> InlineKeyboardMarkup:
    out = []
    for p in rows:
        label = f"{p.get('phone') or p['item_id']} · {p.get('status')}"
        out.append([(label[:38], f"v:{p['_id']}")])
    out.append([(f"⤓ {page}.zip", f"q:{page}"), ("↻", f"ql:{page}")])
    out.append([("‹ Back", BACK_SESSIONS)])
    return kb(out)


def session_detail(p: dict) -> InlineKeyboardMarkup:
    pid = p["_id"]
    rows = [[(".session", f"f:{pid}"), ("Check", f"c:{pid}")]]
    rows.append([(".py script", f"g:{pid}")])
    if p.get("status") == "ready":
        rows.append([("Sell now", f"sl:{pid}")])
    rows.append([("‹ Back", BACK_SESSIONS)])
    return kb(rows)


def ready(s: dict, count: int) -> InlineKeyboardMarkup:
    return kb([
        [(f"Auto sell {_onoff(s['auto_sell'])}", "t:auto_sell")],
        [(f"Sell all ({count})", "rd:all")],
        [("Prices", "rd:prices"), ("List", "rd:list")],
        [("‹ Back", BACK_MAIN)],
    ])


def ready_list(rows: list[dict]) -> InlineKeyboardMarkup:
    out = []
    for p in rows:
        label = f"{p.get('phone') or p['item_id']} · {p.get('sell_state') or 'idle'}"
        out.append([(label[:38], f"rd:{p['_id']}")])
    out.append([("↻ Refresh", BACK_READY)])
    out.append([("‹ Back", BACK_MAIN)])
    return kb(out)


def ready_detail(p: dict) -> InlineKeyboardMarkup:
    pid = p["_id"]
    return kb([
        [("Sell now", f"sl:{pid}")],
        [(".session", f"f:{pid}"), ("Check", f"c:{pid}")],
        [("‹ Back", BACK_READY)],
    ])


def receivers(rows: list[dict]) -> InlineKeyboardMarkup:
    out = []
    for r in rows:
        mark = "🟢" if r.get("enabled") else "⚪"
        cap = r.get("cap")
        cap_text = "?" if cap is None or cap < 0 else str(cap)
        out.append([(f"{mark} @{r['username']} · {cap_text}", f"rcv:{r['_id']}")])
    out.append([("Add", "r:add"), ("↻ Sync", "r:sync")])
    out.append([("Warm all", "r:warm"), ("‹ Back", BACK_MAIN)])
    return kb(out)


def receiver_detail(r: dict) -> InlineKeyboardMarkup:
    rid = r["_id"]
    probe = [("Prices (link)", f"rcpr:{rid}")] if r.get("kind") == "lion" else [
        ("/cap", f"rcp:{rid}"), ("Prices", f"rcpr:{rid}")]
    return kb([
        probe,
        [("Warm", f"rcw:{rid}"), ("History", f"rch:{rid}")],
        [("🟢 On" if r.get("enabled") else "⚪ Off", f"rct:{rid}"), ("Delete", f"rcd:{rid}")],
        [("‹ Back", BACK_RECEIVERS)],
    ])


def wallet(entries: list[dict]) -> InlineKeyboardMarkup:
    rows = [[("↻", BACK_WALLET), ("Payments", "m:payments")]]
    if len(entries) > 1:
        rows.append([("Exchange", "m:exchange")])
    rows.append([("‹ Back", BACK_MAIN)])
    return kb(rows)


def exchange(pool: list[dict], step: str, offset: int = 0,
             page_size: int = 8) -> InlineKeyboardMarkup:
    chunk = pool[offset:offset + page_size]
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for i, b in enumerate(chunk, start=offset):
        row.append((f"{fmt_money(free_of(b), b['currency'])} {b['currency'].upper()}",
                    f"x:{step}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav: list[tuple[str, str]] = []
    if offset > 0:
        nav.append(("‹", f"p:{step}:{max(0, offset - page_size)}"))
    if offset + page_size < len(pool):
        nav.append(("›", f"p:{step}:{offset + page_size}"))
    if nav:
        rows.append(nav)

    rows.append([("‹ Back", BACK_WALLET)])
    return kb(rows)


def amount(currency: str, available: float) -> InlineKeyboardMarkup:
    quick = [(str(v), f"xa:{v}") for v in (10, 50, 100) if v <= available]
    if not quick:
        quick = [(fmt_money(max(0.01, min(available, 10)), currency), "xa:max")]
    return kb([quick, [("Max", "xa:max"), ("Cancel", "xc")]])


def report() -> InlineKeyboardMarkup:
    return kb([
        [("↻", "r:view")],
        [("Export ledger", "l:csv")],
        [("‹ Back", BACK_MAIN)],
    ])


def ledger() -> InlineKeyboardMarkup:
    return kb([[("Export CSV", "l:csv")], [("‹ Back", BACK_MAIN)]])