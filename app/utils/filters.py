from __future__ import annotations

import math
import re
import time
from decimal import Decimal, InvalidOperation

from app.config import TARGET_HOURS
from app.utils.timeutil import age_hours, parse_ts

_FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]{2}")
_COUNTRY_RE = re.compile(r"\(([A-Za-z]{2})\)")
_PHONE_RE = re.compile(r"\+\d{6,16}")

_CREATED_KEYS = (
    "telegram_registration_date", "telegram_created_at", "registration_date",
    "creation_date", "account_created_at", "registered_at", "telegram_registered_at",
    "created_at", "item_created_at",
)
_CREATED_NESTED = ("telegram", "item_origin", "origin", "account", "meta", "item")

_AGE_NUM_KEYS = ("telegram_session_age", "session_age", "session_age_hours",
                 "session_age_seconds")
_PERIOD_KEYS = ("session_age_period", "telegram_session_age_period")

_SPAM_KEYS = ("telegram_spam_block", "spam_block", "is_spam_blocked", "spam")
_GEO_SPAM_KEYS = ("telegram_spam_geo", "spam_geo", "telegram_geo_spam",
                  "is_geo_spam", "geo_spam", "telegram_geo_block",
                  "telegram_restriction_geo")

_PERIOD_HOURS = {
    "hour": 1.0, "hours": 1.0, "h": 1.0, "hr": 1.0, "hrs": 1.0,
    "час": 1.0, "часа": 1.0, "часов": 1.0, "ч": 1.0,
    "day": 24.0, "days": 24.0, "d": 24.0,
    "д": 24.0, "дн": 24.0, "дня": 24.0, "дней": 24.0, "сут": 24.0, "суток": 24.0,
    "week": 168.0, "weeks": 168.0, "w": 168.0, "нед": 168.0, "недели": 168.0,
    "month": 730.0, "months": 730.0, "mo": 730.0, "mos": 730.0,
    "мес": 730.0, "месяц": 730.0, "месяца": 730.0, "месяцев": 730.0,
    "year": 8760.0, "years": 8760.0, "y": 8760.0, "yr": 8760.0, "yrs": 8760.0,
    "г": 8760.0, "год": 8760.0, "года": 8760.0, "лет": 8760.0,
}

_UNIT_ALT = "|".join(re.escape(u) for u in sorted(_PERIOD_HOURS, key=len, reverse=True))

_AGE_TEXT_RES = (
    re.compile(r"(?:session\s*age|возраст\s*сесси[ия]|сесси[яи])\s*[:\-–—]?\s*"
               r"(\d+(?:[.,]\d+)?)\s*(" + _UNIT_ALT + r")?(?![A-Za-zА-Яа-я])"),
    re.compile(r"(\d+(?:[.,]\d+)?)\s*(" + _UNIT_ALT + r")?\s*(?:session|сесси[ия])"),
)

_SYMS = "$€₽₴£₸¥₺"

_TRUE = {"1", "true", "yes", "y", "on", "да", "blocked", "ban", "spam"}
_FALSE = {"0", "false", "no", "n", "off", "", "none", "nomatter", "-"}

PRICE_EPS = 1e-6


def _clean(text: str) -> str:
    s = str(text).strip()
    for ch in ("\u00a0", "\u202f", " ", "'"):
        s = s.replace(ch, "")
    for sym in _SYMS:
        s = s.replace(sym, "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.rsplit(",", 1)[1]
        s = s.replace(",", ".") if len(tail) in (1, 2) else s.replace(",", "")
    return s


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    try:
        f = float(Decimal(_clean(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, dict):
        for key in ("is_blocked", "blocked", "status", "value", "result"):
            if key in value:
                return _flag(value[key])
        return None
    if isinstance(value, str):
        t = value.strip().lower()
        if t in _TRUE:
            return True
        if t in _FALSE:
            return False
    return None


def _flag_to_iso(match) -> str:
    return "".join(chr(ord(ch) - 0x1F1E6 + 65) for ch in match.group(0))


def country_of(item: dict) -> str:
    for key in ("country", "country_code", "telegram_country"):
        explicit = str(item.get(key) or "").strip().upper()
        if len(explicit) == 2 and explicit.isalpha():
            return explicit
    text = f"{item.get('title') or ''} {item.get('description') or ''}"
    flag = _FLAG_RE.search(text)
    if flag:
        return _flag_to_iso(flag)
    m = _COUNTRY_RE.search(text)
    return m.group(1).upper() if m else ""


def phone_of(item: dict) -> str:
    m = _PHONE_RE.search(str(item.get("title") or ""))
    return m.group(0) if m else ""


def account_created_ts(item: dict, depth: int = 0) -> int | None:
    if depth > 3 or not isinstance(item, dict):
        return None
    for key in _CREATED_KEYS:
        if key in item:
            ts = parse_ts(item[key])
            if ts and ts <= time.time() + 86_400:
                return ts
    for key in _CREATED_NESTED:
        nested = item.get(key)
        if isinstance(nested, dict):
            ts = account_created_ts(nested, depth + 1)
            if ts:
                return ts
    return None


def _period_hours(item: dict) -> float | None:
    for key in _PERIOD_KEYS:
        raw = item.get(key)
        if isinstance(raw, str):
            mult = _PERIOD_HOURS.get(raw.strip().lower())
            if mult:
                return mult
    return None


def _unit_from_key(key: str) -> float | None:
    if key.endswith("_seconds"):
        return 1 / 3600
    if key.endswith("_hours"):
        return 1.0
    if key.endswith("_days"):
        return 24.0
    return None


def _age_from_text(item: dict) -> float | None:
    text = f"{item.get('title') or ''} {item.get('description') or ''}"
    if not text.strip():
        return None
    for rx in _AGE_TEXT_RES:
        for m in rx.finditer(text):
            amount = _num(m.group(1))
            if amount is None or amount <= 0:
                continue
            unit = (m.group(2) or "").strip().lower()
            mult = _PERIOD_HOURS.get(unit)
            if mult:
                return amount * mult
            return amount * 24.0
    return None


def session_hours(item: dict) -> float | None:
    for key in _AGE_NUM_KEYS:
        v = _num(item.get(key))
        if v is None or v <= 0:
            continue
        mult = _period_hours(item) or _unit_from_key(key)
        if mult:
            return v * mult
        if v > 100_000:
            return v / 3600
        if v > 1_000:
            return v
        return v * 24.0
    return _age_from_text(item)


def age_hours_of(item: dict, at: int | None = None) -> float | None:
    declared = session_hours(item)
    if declared is not None:
        return declared
    created = account_created_ts(item)
    if created:
        return age_hours(created, at)
    return None


def age_for_queue(item: dict, floor: float = 0.0) -> float:
    declared = session_hours(item)
    if declared is not None:
        return max(0.0, declared)
    created = account_created_ts(item)
    if created:
        return age_hours(created) or 0.0
    if floor and floor >= TARGET_HOURS:
        return max(0.0, floor)
    return 0.0


def is_mature(item: dict, target: float = TARGET_HOURS) -> bool:
    age = age_hours_of(item)
    return age is not None and age >= target


def search_query(s: dict) -> dict:
    floor = _num(s.get("session_min_hours")) or 0.0
    days = int(floor // 24)
    if days < 1:
        return {}
    if days % 30 == 0:
        return {"session_age": days // 30, "session_age_period": "month"}
    return {"session_age": days, "session_age_period": "day"}


def spam_state(item: dict) -> bool | None:
    for key in _SPAM_KEYS:
        if key in item:
            state = _flag(item[key])
            if state is not None:
                return state
    return None


def geo_spam_state(item: dict) -> bool | None:
    for key in _GEO_SPAM_KEYS:
        if key in item:
            state = _flag(item[key])
            if state is not None:
                return state
    return None


def mail_state(item: dict) -> bool | None:
    for key in ("telegram_email", "email", "has_email"):
        if key not in item:
            continue
        raw = item[key]
        if isinstance(raw, str) and "@" in raw:
            return True
        state = _flag(raw)
        if state is not None:
            return state
    if "email_type" in item:
        et = str(item.get("email_type") or "").strip().lower()
        return et not in ("", "no", "none", "nomatter", "-")
    return None


def pass_state(item: dict) -> bool | None:
    for key in ("telegram_password", "password", "has_password"):
        if key not in item:
            continue
        raw = item[key]
        if isinstance(raw, str) and raw.strip() and raw.strip().lower() not in _FALSE:
            return True
        state = _flag(raw)
        if state is not None:
            return state
    return None


def _codes(raw: str) -> set[str]:
    return {c.strip().upper() for c in (raw or "").split(",") if c.strip()}


def passes(item: dict, s: dict, authoritative: bool = False) -> tuple[bool, str]:
    price = _num(item.get("price"))
    if price is not None:
        floor = _num(s.get("price_min")) or 0.0
        cap_price = _num(s.get("price_max")) or 0.0
        if floor and price + PRICE_EPS < floor:
            return False, "price_min"
        if cap_price and price - PRICE_EPS > cap_price:
            return False, "price_max"

    if s.get("nsb_only") == "1" and _flag(item.get("nsb")) is False:
        return False, "resold"

    cc = country_of(item)
    if cc:
        if cc in _codes(s.get("deny_countries", "")):
            return False, "country_excluded"
        allow = _codes(s.get("countries", ""))
        if allow and cc not in allow:
            return False, "country_not_included"

    floor = _num(s.get("session_min_hours")) or 0.0
    cap = _num(s.get("session_max_hours")) or 0.0
    age = age_hours_of(item) if (floor or cap) else None
    if age is not None:
        if floor and age < floor:
            return False, "too_fresh"
        if cap and age > cap:
            return False, "too_old"

    if not authoritative:
        return True, "ok"

    unknown = False

    spam = spam_state(item)
    geo = geo_spam_state(item)
    mode = s.get("spam_mode", "no")
    strict = s.get("spam_free", "1") == "1"
    if mode == "any":
        if spam is True and s.get("verify_spam", "1") == "1":
            return False, "spam"
        if spam is None:
            unknown = True
    elif mode == "yes":
        if spam is False:
            return False, "spam_missing"
        if spam is None:
            unknown = True
    else:
        if spam is True:
            return False, "spam"
        if geo is True:
            return False, "geo_spam"
        if spam is None:
            unknown = True
        elif strict and geo is None:
            unknown = True

    mail = mail_state(item)
    mode = s.get("mail_mode", "any")
    if mode == "yes" and mail is False:
        return False, "no_mail"
    elif mode == "no" and mail is True:
        return False, "has_mail"
    elif mail is None:
        unknown = True

    has_pw = pass_state(item)
    mode = s.get("password_mode", "any")
    if mode == "yes" and has_pw is False:
        return False, "no_2fa"
    elif mode == "no" and has_pw is True:
        return False, "has_2fa"
    elif has_pw is None:
        unknown = True

    if not cc:
        unknown = True

    if floor and age is None:
        unknown = True

    return True, ("ok_unknown" if unknown else "ok")


def clean_delay_hours(item: dict, target: float = TARGET_HOURS,
                      early: bool = True) -> float:
    target = max(0.0, float(target))
    if not early:
        return target
    age = age_hours_of(item) or 0.0
    return min(target, max(0.0, target - age))