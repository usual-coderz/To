from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.utils.money import free_of, fmt_money

BACK_MAIN = "m:main"
BACK_FILTERS = "m:filters"
BACK_WALLET = "m:wallet"
BACK_SESSIONS = "m:sessions"

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
    return kb([[("Back", to)]])


def _onoff(flag: str, on: str, off: str) -> str:
    return on if flag == "1" else off


def main(s: dict) -> InlineKeyboardMarkup:
    return kb([
        [("⏸ Stop" if s["auto_buy"] == "1" else "▶️ Start", "t:auto_buy")],
        [("Filters", BACK_FILTERS), ("Sessions", BACK_SESSIONS)],
        [("Wallet", BACK_WALLET), ("Report", "m:report")],
        [("Stats", "m:stats")],
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
        [(f"Terminate {_onoff(s['terminate_on_expire'], 'on', 'off')}",
          "t:terminate_on_expire"),
         (f"Mail chg {_onoff(s['change_mail_on_expire'], 'on', 'off')}",
          "t:change_mail_on_expire")],
        [(f"Mail src {s['mail_provider']}", "m:mailprov"), ("2FA pwd", "in:twofa_password")],
        [("Back", BACK_MAIN)],
    ])


def price() -> InlineKeyboardMarkup:
    return kb([
        [(f"max {v}", f"s:price_max:{v}") for v in ("0.10", "0.30", "1.00")],
        [(f"min {v}", f"s:price_min:{v}") for v in ("0.05", "0.15", "0.25")],
        [("custom max", "in:price_max"), ("custom min", "in:price_min")],
        [("Back", BACK_FILTERS)],
    ])


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
    rows.append([("Back", back_to)])
    return kb(rows)


def sessions(items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for p in items:
        state = "✅" if p.get("status") == "ready" else "⏳"
        label = f"{state} {p.get('phone') or p['item_id']}"
        rows.append([(label[:40], f"v:{p['_id']}")])
    rows.append([("Refresh", BACK_SESSIONS)])
    rows.append([("Back", BACK_MAIN)])
    return kb(rows)


def session_detail(p: dict) -> InlineKeyboardMarkup:
    pid = p["_id"]
    return kb([
        [("Session file", f"f:{pid}"), ("Check", f"c:{pid}")],
        [("Back", BACK_SESSIONS)],
    ])


def wallet(entries: list[dict]) -> InlineKeyboardMarkup:
    rows = [[("Refresh", BACK_WALLET), ("Payments", "m:payments")]]
    if len(entries) > 1:
        rows.append([("Exchange", "m:exchange")])
    rows.append([("Back", BACK_MAIN)])
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

    rows.append([("Back", BACK_WALLET)])
    return kb(rows)


def amount(currency: str, available: float) -> InlineKeyboardMarkup:
    quick = [(str(v), f"xa:{v}") for v in (10, 50, 100) if v <= available]
    if not quick:
        quick = [(fmt_money(max(0.01, min(available, 10)), currency), "xa:max")]
    return kb([quick, [("Max", "xa:max"), ("Cancel", "xc")]])


def report() -> InlineKeyboardMarkup:
    return kb([
        [("Refresh", "r:view")],
        [("Export ledger", "l:csv")],
        [("Back", BACK_MAIN)],
    ])


def ledger() -> InlineKeyboardMarkup:
    return kb([[("Export CSV", "l:csv")], [("Back", BACK_MAIN)]])