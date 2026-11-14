import logging
import re
import time

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient

from app.config import (MONGO_DB, MONGO_URI, PROFIT_DEFAULT, PROFIT_MAX, PROFIT_MIN,
                        REPORT_KEEP_HOURS, STALE_CLEAN_SECONDS, TARGET_HOURS,
                        TWOFA_PASSWORD)
from app.utils.money import _num

log = logging.getLogger("db")

DEFAULT_2FA = "AccStore2fa"
LOGIN_STALE = 900

ACTIVE = ("login", "queued", "cleaning", "ready")
SELLABLE = ("", "queued", "failed", "no_offer", "no_profit", "no_country")

DEFAULTS = {
    "price_min": "0.05", "price_max": "1.00",
    "spam_mode": "no", "mail_mode": "yes", "password_mode": "no",
    "session_min_hours": "0", "session_max_hours": "0",
    "countries": "", "deny_countries": "IR,RU",
    "nsb_only": "1", "daily_spend_cap": "15", "auto_buy": "1",
    "terminate_on_expire": "1", "change_mail_on_expire": "1",
    "verify_spam": "1", "currency": "usd", "proxy": "",
    "mail_provider": "auto", "twofa_password": (TWOFA_PASSWORD or DEFAULT_2FA),
    "auto_sell": "1", "sell_confirm_seconds": "600", "sell_min_price": "0.00",
    "profit_mult": f"{PROFIT_DEFAULT:g}",
}

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}

_client: AsyncMongoClient | None = None
_db = None


def _now() -> int:
    return int(time.time())


def _oid(pid):
    try:
        return ObjectId(str(pid))
    except (InvalidId, TypeError):
        return None


def num(value, default: float = 0.0) -> float:
    got = _num(value, None)
    return float(default) if got is None else got


def intval(value, default: int = 0) -> int:
    try:
        return int(num(value, default))
    except (TypeError, ValueError):
        return int(default)


def _fmt(f: float) -> str:
    return f"{f:.4f}".rstrip("0").rstrip(".") or "0"


def _f(default: str, lo: float | None = None, hi: float | None = None):
    def norm(v) -> str:
        got = _num(v, None)
        if got is None:
            return default
        if lo is not None:
            got = max(lo, got)
        if hi is not None:
            got = min(hi, got)
        return _fmt(got)
    return norm


def _enum(allowed: tuple[str, ...], default: str):
    def norm(v) -> str:
        t = str(v).strip().lower()
        return t if t in allowed else default
    return norm


def _flag(default: str = "0"):
    def norm(v) -> str:
        t = str(v).strip().lower()
        if t in _TRUE:
            return "1"
        if t in _FALSE:
            return "0"
        return default
    return norm


def _pwd(default: str):
    def norm(v) -> str:
        t = "".join(str(v or "").split())
        if 8 <= len(t) <= 64 and t.isascii():
            return t
        return default
    return norm


def _codes(v) -> str:
    seen = {c.strip().upper() for c in str(v or "").split(",") if c.strip()}
    return ",".join(sorted(seen))


NORMALIZERS = {
    "price_min": _f(DEFAULTS["price_min"], 0.0),
    "price_max": _f(DEFAULTS["price_max"], 0.0),
    "spam_mode": _enum(("yes", "no", "any"), "no"),
    "mail_mode": _enum(("yes", "no", "any"), "yes"),
    "password_mode": _enum(("yes", "no", "any"), "no"),
    "session_min_hours": _f("0", 0.0, 87_600.0),
    "session_max_hours": _f("0", 0.0, 87_600.0),
    "countries": _codes,
    "deny_countries": _codes,
    "nsb_only": _flag("1"),
    "daily_spend_cap": _f("15", 0.0),
    "auto_buy": _flag("1"),
    "terminate_on_expire": _flag("1"),
    "change_mail_on_expire": _flag("1"),
    "verify_spam": _flag("1"),
    "currency": _enum(("usd", "rub", "uah", "eur", "kzt", "byn", "gbp"), "usd"),
    "proxy": lambda v: str(v or "").strip()[:300],
    "mail_provider": _enum(("auto", "native", "temp"), "auto"),
    "twofa_password": _pwd(DEFAULTS["twofa_password"]),
    "auto_sell": _flag("1"),
    "sell_confirm_seconds": _f("600", 60.0),
    "sell_min_price": _f("0.00", 0.0),
    "profit_mult": _f(f"{PROFIT_DEFAULT:g}", PROFIT_MIN, PROFIT_MAX),
}


def normalize_setting(key: str, value) -> str | None:
    fn = NORMALIZERS.get(key)
    return None if fn is None else fn(value)


def normalize_settings(raw) -> tuple[dict, dict]:
    source = raw if isinstance(raw, dict) else {}
    clean, fixes = {}, {}
    for key, default in DEFAULTS.items():
        norm = NORMALIZERS[key](source.get(key, default))
        if key in source and norm != str(source.get(key)):
            fixes[key] = norm
        clean[key] = norm
    if num(clean["price_max"]) < num(clean["price_min"]):
        clean["price_max"] = clean["price_min"]
        fixes["price_max"] = clean["price_max"]
    cap = num(clean["session_max_hours"])
    if cap and num(clean["session_min_hours"]) > cap:
        clean["session_max_hours"] = clean["session_min_hours"]
        fixes["session_max_hours"] = clean["session_max_hours"]
    return clean, fixes


_INDEXES = (
    ("purchases", [("status", ASCENDING), ("expires_at", ASCENDING)], {}),
    ("purchases", [("status", ASCENDING), ("checked_at", ASCENDING)], {}),
    ("purchases", [("status", ASCENDING), ("started_at", ASCENDING)], {}),
    ("purchases", [("tg_id", ASCENDING)], {}),
    ("purchases", [("tg_id", ASCENDING), ("phone", ASCENDING)], {}),
    ("purchases", [("started_at", ASCENDING)], {}),
    ("purchases", [("tg_id", ASCENDING), ("status", ASCENDING), ("sell_state", ASCENDING)], {}),
    ("ledger", [("tg_id", ASCENDING), ("created_at", ASCENDING)], {}),
    ("ledger", [("purchase_id", ASCENDING)], {}),
    ("settings", [("tg_id", ASCENDING)], {"unique": True}),
    ("reports", [("tg_id", ASCENDING), ("ts", DESCENDING)], {}),
    ("receivers", [("tg_id", ASCENDING), ("key", ASCENDING)], {"unique": True}),
    ("receivers", [("tg_id", ASCENDING), ("enabled", ASCENDING)], {}),
    ("receivers", [("tg_id", ASCENDING), ("warm_at", ASCENDING)], {}),
    ("prices", [("tg_id", ASCENDING), ("country", ASCENDING), ("ts", DESCENDING)], {}),
    ("prices", [("tg_id", ASCENDING), ("ts", DESCENDING)], {}),
    ("prices", [("receiver_id", ASCENDING), ("ts", DESCENDING)], {}),
    ("sales", [("tg_id", ASCENDING), ("created_at", DESCENDING)], {}),
    ("sales", [("purchase_id", ASCENDING)], {}),
    ("sales", [("status", ASCENDING), ("created_at", ASCENDING)], {}),
    ("charts", [("tg_id", ASCENDING), ("ts", DESCENDING)], {}),
    ("caps", [("tg_id", ASCENDING), ("ts", DESCENDING)], {}),
)


def _spec_key(spec: dict) -> tuple:
    return tuple((k, int(v)) for k, v in (spec.get("key") or {}).items() if k != "_id")


async def _ensure_indexes():
    for coll_name, keys, opts in _INDEXES:
        coll = _db[coll_name]
        try:
            cursor = await coll.list_indexes()
            existing = [spec async for spec in cursor]
        except Exception as e:
            log.debug(f"list_indexes {coll_name}: {e}")
            existing = []

        want = tuple((k, int(d)) for k, d in keys)
        hit = next((s for s in existing if _spec_key(s) == want), None)

        if hit:
            if opts.get("unique") and not hit.get("unique"):
                try:
                    await coll.drop_index(hit["name"])
                    await coll.create_index(keys, name=hit["name"], **opts)
                    log.info(f"{coll_name}: {hit['name']} upgraded to unique")
                except Exception as e:
                    log.warning(f"{coll_name}: unique upgrade failed: {e}")
            continue

        name = "_".join(f"{k}_{d}" for k, d in keys)
        try:
            await coll.create_index(keys, name=name, **opts)
        except Exception as e:
            log.warning(f"{coll_name}: index {name}: {e}")


async def init_db():
    global _client, _db
    _client = AsyncMongoClient(MONGO_URI, tz_aware=False, serverSelectionTimeoutMS=8000)
    _db = _client[MONGO_DB]
    await _ensure_indexes()


async def close_db():
    global _client, _db
    if _client:
        try:
            await _client.close()
        finally:
            _client, _db = None, None


async def _seed_settings(tg_id: int):
    clean, _ = normalize_settings({})
    await _db.settings.update_one(
        {"tg_id": tg_id}, {"$setOnInsert": {**clean, "tg_id": tg_id}}, upsert=True)


async def ensure_user(tg_id: int, username: str = ""):
    await _db.users.update_one(
        {"_id": tg_id},
        {"$set": {"username": username or ""},
         "$setOnInsert": {"created_at": _now()}},
        upsert=True)
    await _seed_settings(tg_id)


async def get_username(tg_id: int) -> str:
    doc = await _db.users.find_one({"_id": tg_id}, {"username": 1})
    return (doc or {}).get("username") or ""


async def get_settings(tg_id: int) -> dict:
    doc = await _db.settings.find_one({"tg_id": tg_id})
    if not doc:
        await _seed_settings(tg_id)
        clean, _ = normalize_settings({})
        return clean
    clean, fixes = normalize_settings(doc)
    if fixes:
        await _db.settings.update_one({"tg_id": tg_id}, {"$set": fixes})
        log.info(f"settings repaired tg={tg_id} keys={sorted(fixes)}")
    return clean


async def set_setting(tg_id: int, key: str, value) -> str | None:
    norm = normalize_setting(key, value)
    if norm is None:
        return None
    await _db.settings.update_one(
        {"tg_id": tg_id}, {"$set": {key: norm}, "$setOnInsert": {"tg_id": tg_id}},
        upsert=True)
    return norm


async def all_owners() -> list[dict]:
    return await _db.users.find({}, {"_id": 1}).to_list(None)


def _day_start() -> int:
    now = _now()
    return now - (now % 86_400)


async def spent_today(tg_id: int) -> float:
    total = 0.0
    cursor = _db.purchases.find(
        {"tg_id": tg_id, "started_at": {"$gte": _day_start()}}, {"price": 1})
    async for doc in cursor:
        total += float(doc.get("price") or 0.0)
    return total


def due_from(age_hours: float) -> int:
    left = TARGET_HOURS - max(0.0, float(age_hours))
    if left <= 0:
        return _now()
    return _now() + int(left * 3600)


def current_age(p: dict, at: int | None = None) -> float:
    now = at or _now()
    created = int(p.get("account_created_ts") or 0)
    if created:
        return max(0.0, (now - created) / 3600.0)
    base = float(p.get("age_at_buy_hours") or 0.0)
    started = int(p.get("started_at") or 0)
    if started:
        base += max(0.0, (now - started) / 3600.0)
    return base


async def add_purchase(tg_id, item_id, phone, country, price, password="", mail="",
                       mail_password="", session_string="", expires_at=0,
                       age_at_buy_hours=0.0, account_created_ts=0, auth_key="",
                       auth_dc=0, spam_flag=False, status="login", note="") -> str:
    now = _now()
    pid = (await _db.purchases.insert_one({
        "tg_id": tg_id, "item_id": item_id, "phone": phone or "", "country": country or "",
        "price": float(price), "password": password or "", "mail": mail or "",
        "mail_password": mail_password or "", "session_string": session_string,
        "auth_key": auth_key or "", "auth_dc": int(auth_dc or 0),
        "status": status, "started_at": now, "expires_at": int(expires_at),
        "target_age_hours": TARGET_HOURS,
        "age_at_buy_hours": float(age_at_buy_hours),
        "account_created_ts": int(account_created_ts or 0),
        "spam_flag": bool(spam_flag),
        "expiry_tries": 0, "session_state": "pending", "checked_at": 0,
        "closed_at": 0, "note": note, "sell_state": "", "sell_reason": "",
        "sell_try": 0, "sell_next": 0,
    })).inserted_id
    await _db.ledger.insert_one({
        "tg_id": tg_id, "purchase_id": pid, "item_id": item_id,
        "buy_price": float(price), "sell_price": 0.0, "status": "open",
        "created_at": now, "closed_at": 0, "extra": note,
    })
    return str(pid)


async def due_purchases(limit: int = 200) -> list[dict]:
    now = _now()
    return await _db.purchases.find({
        "$or": [
            {"status": "queued", "expires_at": {"$lte": now}},
            {"status": "cleaning", "checked_at": {"$lt": now - STALE_CLEAN_SECONDS}},
            {"status": "login", "started_at": {"$lt": now - LOGIN_STALE}},
        ]
    }).sort("expires_at", ASCENDING).limit(limit).to_list(None)


async def claim_purchase(pid) -> bool:
    oid = _oid(pid)
    if not oid:
        return False
    now = _now()
    doc = await _db.purchases.find_one_and_update(
        {"_id": oid,
         "$or": [{"status": "queued"},
                 {"status": "cleaning", "checked_at": {"$lt": now - STALE_CLEAN_SECONDS}},
                 {"status": "login", "started_at": {"$lt": now - LOGIN_STALE}}]},
        {"$set": {"status": "cleaning", "checked_at": now}})
    return bool(doc)


async def queue_purchase(pid, expires_at: int):
    oid = _oid(pid)
    if oid:
        await _db.purchases.update_one(
            {"_id": oid},
            {"$set": {"status": "queued", "expires_at": int(expires_at), "checked_at": 0}})


async def release_purchase(pid, seconds: int, inc_tries: bool = False):
    oid = _oid(pid)
    if not oid:
        return
    upd = {"$set": {"status": "queued", "expires_at": _now() + max(0, int(seconds)),
                    "checked_at": 0}}
    if inc_tries:
        upd["$inc"] = {"expiry_tries": 1}
    await _db.purchases.update_one({"_id": oid}, upd)


async def active_purchases(tg_id: int, limit: int = 30) -> list[dict]:
    return await _db.purchases.find(
        {"tg_id": tg_id, "status": {"$in": list(ACTIVE)}}
    ).sort("started_at", DESCENDING).limit(limit).to_list(None)


async def queue_purchases(tg_id: int, limit: int = 100) -> list[dict]:
    return await _db.purchases.find(
        {"tg_id": tg_id, "status": {"$in": ["login", "queued", "cleaning"]}}
    ).sort("expires_at", ASCENDING).limit(limit).to_list(None)


async def ready_purchases(tg_id: int, limit: int = 100) -> list[dict]:
    return await _db.purchases.find(
        {"tg_id": tg_id, "status": "ready"}
    ).sort("closed_at", DESCENDING).limit(limit).to_list(None)


async def ready_count(tg_id: int) -> int:
    return await _db.purchases.count_documents({"tg_id": tg_id, "status": "ready"})


async def sell_queue(tg_id: int, limit: int = 5) -> list[dict]:
    now = _now()
    return await _db.purchases.find({
        "tg_id": tg_id, "status": "ready", "session_string": {"$ne": ""},
        "sell_state": {"$in": list(SELLABLE)},
        "sell_next": {"$lte": now},
    }).sort("closed_at", ASCENDING).limit(limit).to_list(None)


async def set_sell(pid, state: str, delay: int = 0, reason: str = ""):
    oid = _oid(pid)
    if oid:
        upd = {"sell_state": state, "sell_next": _now() + max(0, int(delay))}
        if reason:
            upd["sell_reason"] = reason[:300]
        await _db.purchases.update_one({"_id": oid}, {"$set": upd})


async def bump_sell_try(pid, delay: int, reason: str = ""):
    oid = _oid(pid)
    if oid:
        upd = {"$set": {"sell_state": "failed",
                        "sell_next": _now() + max(0, int(delay))}}
        if reason:
            upd["$set"]["sell_reason"] = reason[:300]
        await _db.purchases.update_one({"_id": oid}, {"$inc": {"sell_try": 1}, **upd})


async def over_age(tg_id: int, hours: float = TARGET_HOURS, limit: int = 500) -> list[dict]:
    now = _now()
    cutoff = now - int(hours * 3600)
    rows = await _db.purchases.find(
        {"tg_id": tg_id, "status": {"$in": list(ACTIVE)},
         "$or": [{"account_created_ts": {"$gt": 0, "$lte": cutoff}},
                 {"account_created_ts": 0, "started_at": {"$lte": cutoff}}]}
    ).sort("started_at", ASCENDING).limit(limit).to_list(None)
    return [p for p in rows if current_age(p, now) >= hours]


async def queue_count(tg_id: int) -> int:
    return await _db.purchases.count_documents(
        {"tg_id": tg_id, "status": {"$in": ["login", "queued"]}})


async def session_stats(tg_id: int) -> dict:
    now = _now()
    out = {"total": 0, "queue": 0, "over": 0, "ready": 0, "sold": 0, "live": 0,
           "dead": 0, "queued_sell": 0, "sell_failed": 0}
    cursor = _db.purchases.find(
        {"tg_id": tg_id, "status": {"$in": list(ACTIVE) + ["sold"]}},
        {"status": 1, "account_created_ts": 1, "started_at": 1, "age_at_buy_hours": 1,
         "session_state": 1, "sell_state": 1})
    async for p in cursor:
        state = p.get("status")
        if state == "sold":
            out["sold"] += 1
            continue
        out["total"] += 1
        if state in ("login", "queued", "cleaning"):
            out["queue"] += 1
        if state == "ready":
            out["ready"] += 1
        if p.get("session_state") == "live":
            out["live"] += 1
        elif p.get("session_state") == "dead":
            out["dead"] += 1
        if p.get("sell_state") in ("queued", "selling"):
            out["queued_sell"] += 1
        elif p.get("sell_state") in ("failed", "dead", "no_offer", "no_profit",
                                     "no_country"):
            out["sell_failed"] += 1
        if current_age(p, now) >= TARGET_HOURS:
            out["over"] += 1
    return out


async def get_purchase(pid) -> dict | None:
    oid = _oid(pid)
    return await _db.purchases.find_one({"_id": oid}) if oid else None


async def find_by_phone(tg_id: int, needle: str) -> dict | None:
    pattern = re.escape((needle or "").strip().lstrip("+"))
    if not pattern:
        return None
    return await _db.purchases.find_one(
        {"tg_id": tg_id, "phone": {"$regex": pattern}}, sort=[("_id", -1)])


async def set_purchase(pid, **fields):
    oid = _oid(pid)
    if oid and fields:
        await _db.purchases.update_one({"_id": oid}, {"$set": fields})


async def set_session(pid, session_string: str):
    await set_purchase(pid, session_string=session_string, session_state="live",
                       checked_at=_now())


async def bump_expiry(pid, seconds: int, inc_tries: bool = False):
    oid = _oid(pid)
    if not oid:
        return
    inc = {"expires_at": int(seconds)}
    if inc_tries:
        inc["expiry_tries"] = 1
    await _db.purchases.update_one({"_id": oid}, {"$inc": inc})


async def reschedule(tg_id: int):
    now = _now()
    cursor = _db.purchases.find({"tg_id": tg_id, "status": "queued"})
    async for p in cursor:
        await _db.purchases.update_one(
            {"_id": p["_id"]},
            {"$set": {"expires_at": max(now, due_from(current_age(p, now))),
                      "target_age_hours": TARGET_HOURS}})


async def close_purchase(pid, status: str, note: str = "", new_mail: str = ""):
    oid = _oid(pid)
    if not oid:
        return
    upd = {"status": status, "closed_at": _now(), "note": note, "expires_at": 0,
           "checked_at": 0}
    if new_mail:
        upd["mail"] = new_mail
    await _db.purchases.update_one({"_id": oid}, {"$set": upd})
    await _db.ledger.update_one(
        {"purchase_id": oid},
        {"$set": {"status": status, "closed_at": _now(), "extra": note}})


async def mark_sold(pid, sell_price: float, tg_id: int | None = None) -> bool:
    oid = _oid(pid)
    if not oid:
        return False
    query = {"_id": oid}
    if tg_id is not None:
        query["tg_id"] = tg_id
    if not await _db.purchases.find_one(query, {"_id": 1}):
        return False
    now = _now()
    await _db.ledger.update_one(
        {"purchase_id": oid},
        {"$set": {"sell_price": float(sell_price), "status": "sold", "closed_at": now}})
    await _db.purchases.update_one(
        {"_id": oid},
        {"$set": {"status": "sold", "closed_at": now, "expires_at": 0,
                  "sell_state": "sold", "sell_next": 0, "sell_reason": ""}})
    return True


async def ledger_rows(tg_id: int) -> list[dict]:
    return await _db.ledger.find({"tg_id": tg_id}).sort("_id", -1).limit(500).to_list(None)


async def ledger_stats(tg_id: int) -> dict:
    total = done = 0
    spent = revenue = 0.0
    cursor = _db.ledger.find({"tg_id": tg_id},
                             {"buy_price": 1, "sell_price": 1, "status": 1})
    async for row in cursor:
        total += 1
        spent += float(row.get("buy_price") or 0.0)
        revenue += float(row.get("sell_price") or 0.0)
        if row.get("status") in ("terminated", "sold", "ready"):
            done += 1
    profit = revenue - spent
    return {"total": total, "spent": spent, "revenue": revenue, "profit": profit,
            "roi": (profit / spent * 100) if spent else 0.0, "done": done}


async def save_report(tg_id: int, *, scanned: int = 0, bought: int = 0,
                      reasons: dict | None = None, note: str = "", error: str = ""):
    now = _now()
    await _db.reports.insert_one({
        "tg_id": tg_id, "ts": now, "scanned": int(scanned), "bought": int(bought),
        "skipped": int(sum((reasons or {}).values())), "reasons": reasons or {},
        "note": (note or "")[:200], "error": (error or "")[:300],
    })
    await _db.reports.delete_many(
        {"tg_id": tg_id, "ts": {"$lt": now - REPORT_KEEP_HOURS * 3600}})


async def recent_reports(tg_id: int, limit: int = 10) -> list[dict]:
    return await _db.reports.find({"tg_id": tg_id}) \
        .sort("ts", DESCENDING).limit(limit).to_list(None)


async def prune_purchases(days: int):
    if days <= 0:
        return
    await _db.purchases.delete_many(
        {"status": {"$in": ["sold", "failed", "ready"]},
         "closed_at": {"$gt": 0, "$lt": _now() - days * 86_400}})


def receiver_key(username: str) -> str:
    return str(username or "").strip().lstrip("@").lower()


async def add_receiver(tg_id: int, username: str, kind: str = "", title: str = "") -> str | None:
    key = receiver_key(username)
    if not key:
        return None
    now = _now()
    existing = await _db.receivers.find_one({"tg_id": tg_id, "key": key})
    if existing:
        await _db.receivers.update_one(
            {"_id": existing["_id"]},
            {"$set": {"title": title or existing.get("title") or key,
                      "kind": kind or existing.get("kind") or "cm",
                      "enabled": True}})
        return str(existing["_id"])
    rid = (await _db.receivers.insert_one({
        "tg_id": tg_id, "key": key, "username": key, "kind": kind or "cm",
        "title": title or key, "enabled": True, "cap": -1, "cap_at": 0,
        "price_at": 0, "confirm_seconds": 0, "fails": 0, "warm_at": 0,
        "created_at": now,
    })).inserted_id
    return str(rid)


async def get_receiver(rid) -> dict | None:
    oid = _oid(rid)
    return await _db.receivers.find_one({"_id": oid}) if oid else None


async def get_receiver_by_key(tg_id: int, key: str) -> dict | None:
    return await _db.receivers.find_one({"tg_id": tg_id, "key": receiver_key(key)})


async def list_receivers(tg_id: int, enabled_only: bool = False) -> list[dict]:
    query = {"tg_id": tg_id}
    if enabled_only:
        query["enabled"] = True
    return await _db.receivers.find(query).sort("kind", ASCENDING).to_list(None)


async def set_receiver(rid, **fields):
    oid = _oid(rid)
    if oid and fields:
        await _db.receivers.update_one({"_id": oid}, {"$set": fields})


async def mark_warm(rid):
    oid = _oid(rid)
    if oid:
        await _db.receivers.update_one({"_id": oid}, {"$set": {"warm_at": _now()}})


async def drop_receiver(rid) -> bool:
    oid = _oid(rid)
    if not oid:
        return False
    res = await _db.receivers.delete_one({"_id": oid})
    await _db.prices.delete_many({"receiver_id": oid})
    await _db.caps.delete_many({"receiver_id": oid})
    return bool(res.deleted_count)


async def bump_receiver(rid, cap: int | None = None, confirm: int | None = None):
    oid = _oid(rid)
    if not oid:
        return
    upd = {"$set": {"fails": 0, "cap_at": _now()}}
    if cap is not None:
        upd["$set"]["cap"] = int(cap)
    if confirm:
        upd["$set"]["confirm_seconds"] = int(confirm)
    await _db.receivers.update_one({"_id": oid}, upd)


async def fail_receiver(rid):
    oid = _oid(rid)
    if oid:
        await _db.receivers.update_one({"_id": oid}, {"$inc": {"fails": 1}})


async def save_prices(tg_id: int, receiver_id, offers: list[dict]) -> int:
    if not offers:
        return 0
    now = _now()
    rows = []
    for offer in offers:
        cc = str(offer.get("country") or "").strip().upper()
        value = num(offer.get("price"))
        if not cc or value <= 0:
            continue
        rows.append({"tg_id": tg_id, "receiver_id": _oid(receiver_id), "country": cc,
                     "flag": str(offer.get("flag") or ""),
                     "dial": str(offer.get("dial") or ""),
                     "price": float(value), "ts": now})
    if not rows:
        return 0
    await _db.prices.insert_many(rows)
    await _db.prices.delete_many({"tg_id": tg_id, "ts": {"$lt": now - 172_800}})
    return len(rows)


async def latest_prices(tg_id: int, max_age: float) -> list[dict]:
    cutoff = _now() - int(max_age)
    pipeline = [
        {"$match": {"tg_id": tg_id, "ts": {"$gte": cutoff}}},
        {"$sort": {"ts": DESCENDING}},
        {"$group": {"_id": {"receiver_id": "$receiver_id", "country": "$country"},
                    "price": {"$first": "$price"}, "ts": {"$first": "$ts"},
                    "flag": {"$first": "$flag"}, "dial": {"$first": "$dial"}}},
    ]
    cursor = await _db.prices.aggregate(pipeline)
    return await cursor.to_list(None)


async def add_cap(tg_id: int, receiver_id, username: str, cap: int) -> str:
    cid = (await _db.caps.insert_one({
        "tg_id": tg_id, "receiver_id": _oid(receiver_id), "username": receiver_key(username),
        "cap": int(cap), "ts": _now(),
    })).inserted_id
    await _db.caps.delete_many({"tg_id": tg_id, "ts": {"$lt": _now() - 604_800}})
    return str(cid)


async def last_caps(tg_id: int, max_age: float = 172_800) -> list[dict]:
    cutoff = _now() - int(max_age)
    pipeline = [
        {"$match": {"tg_id": tg_id, "ts": {"$gte": cutoff}}},
        {"$sort": {"ts": DESCENDING}},
        {"$group": {"_id": "$username", "cap": {"$first": "$cap"}, "ts": {"$first": "$ts"}}},
        {"$sort": {"ts": DESCENDING}},
    ]
    cursor = await _db.caps.aggregate(pipeline)
    return await cursor.to_list(None)


async def cap_history(tg_id: int, username: str, limit: int = 24) -> list[dict]:
    return await _db.caps.find(
        {"tg_id": tg_id, "username": receiver_key(username)}
    ).sort("ts", DESCENDING).limit(limit).to_list(None)


async def add_sale(tg_id: int, purchase_id, country: str, receiver_id, receiver: str,
                   buy_price: float, sell_price: float, confirm_seconds: int) -> str:
    now = _now()
    sid = (await _db.sales.insert_one({
        "tg_id": tg_id, "purchase_id": _oid(purchase_id), "country": country or "",
        "receiver_id": _oid(receiver_id), "receiver": receiver_key(receiver),
        "buy_price": float(buy_price), "sell_price": float(sell_price),
        "confirm_seconds": int(confirm_seconds), "status": "pending",
        "job": f"TRADE-{now:x}", "created_at": now, "closed_at": 0, "note": "",
    })).inserted_id
    await _db.purchases.update_one(
        {"_id": _oid(purchase_id)},
        {"$set": {"sell_state": "selling", "sell_next": 0, "sell_reason": "",
                  "sell_receiver": receiver_key(receiver)}})
    return str(sid)


async def set_sale_price(sid, price: float):
    oid = _oid(sid)
    if oid:
        await _db.sales.update_one({"_id": oid}, {"$set": {"sell_price": float(price)}})


async def set_sale_confirm(sid, seconds: int):
    oid = _oid(sid)
    if oid:
        await _db.sales.update_one({"_id": oid}, {"$set": {"confirm_seconds": int(seconds)}})


async def close_sale(sid, status: str, note: str = ""):
    oid = _oid(sid)
    if not oid:
        return
    await _db.sales.update_one(
        {"_id": oid},
        {"$set": {"status": status, "closed_at": _now(), "note": note[:300]}})


async def sell_totals(tg_id: int) -> dict:
    investment = sold = 0.0
    count = 0
    cursor = _db.sales.find({"tg_id": tg_id, "status": "sold"},
                            {"buy_price": 1, "sell_price": 1})
    async for row in cursor:
        count += 1
        investment += float(row.get("buy_price") or 0.0)
        sold += float(row.get("sell_price") or 0.0)
    profit = sold - investment
    return {"investment": investment, "sold": sold, "profit": profit, "count": count,
            "roi": (profit / investment * 100) if investment else 0.0}


async def sale_stats(tg_id: int) -> dict:
    out = {"total": 0, "sold": 0, "failed": 0}
    cursor = _db.sales.find({"tg_id": tg_id}, {"status": 1})
    async for row in cursor:
        out["total"] += 1
        if row.get("status") == "sold":
            out["sold"] += 1
        elif row.get("status") == "failed":
            out["failed"] += 1
    return out


async def save_chart(tg_id: int, rows: list[dict]) -> str:
    cid = (await _db.charts.insert_one(
        {"tg_id": tg_id, "ts": _now(), "rows": rows})).inserted_id
    await _db.charts.delete_many({"tg_id": tg_id, "ts": {"$lt": _now() - 604_800}})
    return str(cid)


async def recent_chart(tg_id: int) -> dict | None:
    return await _db.charts.find_one({"tg_id": tg_id}, sort=[("ts", DESCENDING)])