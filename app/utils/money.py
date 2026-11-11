"""Balance parsing that survives unknown payload shapes.

Rules:
  * never raise — every parser degrades to a default
  * a balance is only spendable when its identity says market/other, never a
    TG wallet
  * money is normalised before arithmetic, so symbols/spaces/commas cannot
    poison a float()
"""
import math
import re
from decimal import Decimal, InvalidOperation

SYMBOLS = {"usd": "$", "eur": "€", "rub": "₽", "uah": "₴", "gbp": "£",
           "kzt": "₸", "byn": "Br", "cny": "¥", "try": "₺", "jpy": "¥", "brl": "R$"}

ALIASES = {"usdt": "usd", "usdc": "usd", "busd": "usd", "usdtt": "usd",
           "rubr": "rub", "eurc": "eur"}

CUR_KEYS = ("currency", "cur", "code", "currency_code")
AMT_KEYS = ("balance", "amount", "value", "sum", "available", "free")
HOLD_KEYS = ("hold", "held", "frozen", "pending", "locked")
ID_KEYS = ("balance_id", "id", "wallet_id", "balanceid")

_ID_FIELDS = ("balance_type", "type", "kind", "slug", "title", "name", "label",
              "description", "payment_method")

_MARKET_TOKENS = {"market", "main", "trade", "trading", "spend"}
_TG_TOKENS = {"tg", "tgwallet", "telegram"}

_STRIP = str.maketrans({
    " ": "", "\u00a0": "", "\u202f": "", ",": ".", "'": "",
    "$": "", "€": "", "₽": "", "₴": "", "£": "", "₸": "", "¥": "", "₺": "", "R": "",
})

MAX_ENTRIES = 200
MAX_DEPTH = 6


def canon(currency: str) -> str:
    c = str(currency or "usd").strip().lower()
    return ALIASES.get(c, c)


def same_currency(a: str, b: str) -> bool:
    return canon(a) == canon(b)


def _num(value, default=None):
    """Tolerant float. Bools and non-finite values fall back to default."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else default
    try:
        f = float(Decimal(str(value).translate(_STRIP)))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def fmt_money(amount, currency: str = "usd") -> str:
    cur = str(currency or "usd").strip().lower()
    value = _num(amount, 0.0) or 0.0
    if cur in SYMBOLS:
        return f"{SYMBOLS[cur]}{value:,.2f}"
    return f"{value:,.2f} {cur.upper()}"


def parse_user(payload: dict) -> dict:
    u = (payload or {}).get("user") or payload or {}
    if not isinstance(u, dict):
        u = {}
    return {
        "user_id": _num(u.get("user_id") or u.get("id"), 0) or 0,
        "username": str(u.get("username") or "?"),
        "currency": canon(u.get("currency")),
        "balance": max(0.0, _num(u.get("balance"), 0.0) or 0.0),
        "hold": max(0.0, _num(u.get("hold"), 0.0) or 0.0),
        "sympathies": _num(u.get("sympathies"), 0.0) or 0.0,
        "like_count": _num(u.get("like_count"), 0.0) or 0.0,
        "items_count": _num(u.get("items_count"), 0.0) or 0.0,
    }


def _is_currency(value) -> bool:
    return (isinstance(value, str) and 2 <= len(value) <= 5
            and value.replace("_", "").isalpha())


def _pick(d: dict, keys) -> tuple[str | None, object]:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return k, d[k]
    return None, None


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def _identity(d: dict) -> str:
    return " ".join(str(d.get(k)) for k in _ID_FIELDS
                    if d.get(k) not in (None, "")).lower()


def _kind(d: dict) -> str:
    """Only identity fields decide; no identity info → usable ('other')."""
    raw = _identity(d)
    if not raw.strip():
        return "other"
    toks = _tokens(raw)
    if "telegram" in raw or toks & _TG_TOKENS:
        return "telegram"
    if toks & _MARKET_TOKENS:
        return "market"
    if "wallet" in toks:
        return "telegram"
    return "other"


def _label(d: dict) -> str:
    for k in ("title", "name", "label", "type", "balance_type", "slug", "description"):
        v = d.get(k)
        if isinstance(v, str) and v.strip() and not _is_currency(v):
            return v.strip()[:24]
    return ""


def _entry(d: dict, currency: str) -> dict:
    kind = _kind(d)
    _, hold = _pick(d, HOLD_KEYS)
    _, bid = _pick(d, ID_KEYS)
    amount = _num(_pick(d, AMT_KEYS)[1], 0.0) or 0.0
    return {
        "balance_id": int(_num(bid, 0) or 0),
        "currency": canon(currency),
        "amount": amount,
        "hold": max(0.0, _num(hold, 0.0) or 0.0),
        "kind": kind,
        "label": _label(d) or {"market": "Market balance",
                               "telegram": "TG wallet"}.get(kind, "Balance"),
        "usable_for_buy": kind != "telegram",
    }


def _walk(node, out: list, depth: int = 0):
    if depth > MAX_DEPTH or len(out) >= MAX_ENTRIES:
        return
    if isinstance(node, dict):
        _, cur = _pick(node, CUR_KEYS)
        if _is_currency(cur) and _pick(node, AMT_KEYS)[1] is not None:
            out.append(_entry(node, cur))
            return
        for v in node.values():
            _walk(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out, depth + 1)


def parse_balances(payload: dict) -> list[dict]:
    out: list[dict] = []
    _walk(payload or {}, out)

    seen, unique = set(), []
    for b in out:
        key = (b["balance_id"], b["currency"], round(b["amount"], 6), b["kind"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(b)

    if not unique:
        user = (payload or {}).get("user") or {}
        if isinstance(user, dict) and _is_currency(user.get("currency")) \
                and user.get("balance") is not None:
            unique = [_entry(user, user["currency"])]
    return unique


def free_of(b: dict) -> float:
    return max(0.0, (_num(b.get("amount"), 0.0) or 0.0) - (_num(b.get("hold"), 0.0) or 0.0))


def _rank(b: dict) -> tuple:
    order = {"market": 0, "other": 1, "telegram": 2}
    return (order.get(b.get("kind"), 1), 0 if b.get("balance_id") else 1, -free_of(b))


def spendable(balances: list[dict], currency: str) -> float:
    return sum(free_of(b) for b in balances
               if b.get("usable_for_buy") and same_currency(b.get("currency"), currency))


def total_of(payload: dict, currency: str) -> float:
    user = parse_user(payload)
    if same_currency(user["currency"], currency):
        return max(0.0, user["balance"] - user["hold"])
    return 0.0


def pick_for_buy(balances: list[dict], currency: str, price: float) -> dict | None:
    need = _num(price, 0.0) or 0.0
    for b in sorted(balances, key=_rank):
        if b.get("usable_for_buy") and same_currency(b.get("currency"), currency) \
                and free_of(b) >= need:
            return b
    return None