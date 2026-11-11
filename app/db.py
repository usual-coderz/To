import logging
import re
import time

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient

from app.config import MONGO_DB, MONGO_URI, REPORT_KEEP_HOURS

log = logging.getLogger("db")

DEFAULTS = {
    "price_min": "0.05", "price_max": "1.00",
    "spam_mode": "no", "mail_mode": "yes", "mail_type": "any", "password_mode": "no",
    "session_min_hours": "0", "session_max_hours": "0", "session_life_hours": "24",
    "countries": "", "deny_countries": "IR,RU",
    "nsb_only": "1", "max_buy_per_cycle": "2", "daily_spend_cap": "15",
    "poll_interval": "25", "auto_buy": "0",
    "early_clean_if_aged": "1",
    "terminate_on_expire": "1", "change_mail_on_expire": "1",
    "verify_spam": "1", "currency": "usd", "proxy": "", "mail_provider": "auto",
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
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return float(default)


def intval(value, default: int = 0) -> int:
    try:
        return int(num(value, default))
    except (TypeError, ValueError):
        return int(default)


def _fmt(f: float) -> str:
    return f"{f:.4f}".rstrip("0").rstrip(".") or "0"


def _f(default: str, lo: float | None = None, hi: float | None = None):
    def norm(v) -> str:
        try:
            f = float(str(v).replace(",", ".").strip())
        except (TypeError, ValueError):
            return default
        if lo is not None:
            f = max(lo, f)
        if hi is not None:
            f = min(hi, f)
        return _fmt(f)
    return norm


def _i(default: str, lo: int | None = None, hi: int | None = None):
    def norm(v) -> str:
        try:
            i = int(float(str(v).replace(",", ".").strip()))
        except (TypeError, ValueError):
            return default
        if lo is not None:
            i = max(lo, i)
        if hi is not None:
            i = min(hi, i)
        return str(i)
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


def _codes(v) -> str:
    seen = {c.strip().upper() for c in str(v or "").split(",") if c.strip()}
    return ",".join(sorted(seen))


NORMALIZERS = {
    "price_min": _f(DEFAULTS["price_min"], 0.0),
    "price_max": _f(DEFAULTS["price_max"], 0.0),
    "spam_mode": _enum(("yes", "no", "any"), "no"),
    "mail_mode": _enum(("yes", "no", "any"), "yes"),
    "mail_type": _enum(("any", "native", "temp"), "any"),
    "password_mode": _enum(("yes", "no", "any"), "no"),
    "session_min_hours": _f("0", 0.0, 87_600.0),
    "session_max_hours": _f("0", 0.0, 87_600.0),
    "session_life_hours": _f("24", 0.0, 87_600.0),
    "countries": _codes,
    "deny_countries": _codes,
    "nsb_only": _flag("1"),
    "max_buy_per_cycle": _i("2", 1, 100),
    "daily_spend_cap": _f("15", 0.0),
    "poll_interval": _i("25", 5, 3_600),
    "auto_buy": _flag("0"),
    "early_clean_if_aged": _flag("1"),
    "terminate_on_expire": _flag("1"),
    "change_mail_on_expire": _flag("1"),
    "verify_spam": _flag("1"),
    "currency": _enum(("usd", "rub", "uah", "eur", "kzt", "byn", "gbp"), "usd"),
    "proxy": lambda v: str(v or "").strip()[:300],
    "mail_provider": _enum(("auto", "native", "temp"), "auto"),
}


def normalize_setting(key: str, value) -> str | None:
    fn = NORMALIZERS.get(key)
    return None if fn is None else fn(value)


def normalize_settings(raw: dict) -> tuple[dict, dict]:
    clean, fixes = {}, {}
    for key, default in DEFAULTS.items():
        norm = NORMALIZERS[key](raw.get(key, default))
        if key in raw and norm != str(raw.get(key)):
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
    ("purchases", [("tg_id", ASCENDING)], {}),
    ("purchases", [("tg_id", ASCENDING), ("phone", ASCENDING)], {}),
    ("ledger", [("tg_id", ASCENDING), ("created_at", ASCENDING)], {}),
    ("settings", [("tg_id", ASCENDING)], {"unique": True}),
    ("reports", [("tg_id", ASCENDING), ("ts", DESCENDING)], {}),
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
        hit = next(
            (s for s in existing if _spec_key(s) == want),
            None
        )

        if hit:
            if opts.get("unique") and not hit.get("unique"):
                try:
                    await coll.drop_index(hit["name"])
                    await coll.create_index(
                        keys,
                        name=hit["name"],
                        **opts
                    )
                    log.info(
                        f"{coll_name}: {hit['name']} upgraded to unique"
                    )
                except Exception as e:
                    log.warning(
                        f"{coll_name}: unique upgrade failed: {e}"
                    )

            continue

        name = "_".join(f"{k}_{d}" for k, d in keys)

        try:
            await coll.create_index(
                keys,
                name=name,
                **opts
            )
        except Exception as e:
            log.warning(
                f"{coll_name}: index {name}: {e}"
            )


async def init_db():
    global _client, _db
    _client = AsyncMongoClient(MONGO_URI, tz_aware=False)
    _db = _client[MONGO_DB]
    await _ensure_indexes()


async def close_db():
    global _client, _db
    if _client:
        try:
            await _client.close()
        finally:
            _client, _db = None, None


async def ensure_user(tg_id: int, username: str = ""):
    await _db.users.update_one(
        {"_id": tg_id}, {"$setOnInsert": {"username": username, "created_at": _now()}},
        upsert=True)
    clean, _ = normalize_settings({})
    await _db.settings.update_one(
        {"tg_id": tg_id}, {"$setOnInsert": {**clean, "tg_id": tg_id}}, upsert=True)


async def get_settings(tg_id: int) -> dict:
    doc = await _db.settings.find_one({"tg_id": tg_id})
    if not doc:
        clean, _ = normalize_settings({})
        await _db.settings.update_one(
            {"tg_id": tg_id}, {"$setOnInsert": {**clean, "tg_id": tg_id}}, upsert=True)
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


async def spent_today(tg_id: int) -> float:
    total = 0.0
    cursor = _db.purchases.find(
        {"tg_id": tg_id, "started_at": {"$gte": _now() - 86_400}}, {"price": 1})
    async for doc in cursor:
        total += float(doc.get("price") or 0.0)
    return total


async def add_purchase(tg_id, item_id, phone, country, price, mail="", mail_password="",
                       session_string="", expires_at=0, target_age_hours=24.0,
                       age_at_buy_hours=0.0, account_created_ts=0, spam_flag=False,
                       status="active", note="") -> str:
    now = _now()
    pid = (await _db.purchases.insert_one({
        "tg_id": tg_id, "item_id": item_id, "phone": phone or "", "country": country or "",
        "price": float(price), "mail": mail or "", "mail_password": mail_password or "",
        "session_string": session_string, "status": status,
        "started_at": now, "expires_at": int(expires_at),
        "target_age_hours": float(target_age_hours),
        "age_at_buy_hours": float(age_at_buy_hours),
        "account_created_ts": int(account_created_ts or 0),
        "spam_flag": bool(spam_flag),
        "expiry_tries": 0, "session_state": "pending", "checked_at": 0,
        "closed_at": 0, "note": note,
    })).inserted_id
    await _db.ledger.insert_one({
        "tg_id": tg_id, "purchase_id": pid, "item_id": item_id,
        "buy_price": float(price), "sell_price": 0.0, "status": "open",
        "created_at": now, "closed_at": 0, "extra": note,
    })
    return str(pid)


async def due_purchases(limit: int = 200) -> list[dict]:
    return await _db.purchases.find(
        {"status": "active", "expires_at": {"$gt": 0, "$lte": _now()}}
    ).sort("expires_at", ASCENDING).limit(limit).to_list(None)


async def active_purchases(tg_id: int) -> list[dict]:
    return await _db.purchases.find({"tg_id": tg_id, "status": "active"}) \
        .sort("expires_at", ASCENDING).to_list(None)


async def queue_count(tg_id: int) -> int:
    return await _db.purchases.count_documents({"tg_id": tg_id, "status": "active"})


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
    await set_purchase(pid, session_string=session_string, session_state="live")


async def bump_expiry(pid, seconds: int, inc_tries: bool = False):
    oid = _oid(pid)
    if not oid:
        return
    inc = {"expires_at": int(seconds)}
    if inc_tries:
        inc["expiry_tries"] = 1
    await _db.purchases.update_one({"_id": oid}, {"$inc": inc})


async def close_purchase(pid, status: str, note: str = "", new_mail: str = ""):
    oid = _oid(pid)
    if not oid:
        return
    upd = {"status": status, "closed_at": _now(), "note": note, "expires_at": 0}
    if new_mail:
        upd["mail"] = new_mail
    await _db.purchases.update_one({"_id": oid}, {"$set": upd})
    await _db.ledger.update_one(
        {"purchase_id": oid},
        {"$set": {"status": status, "closed_at": _now(), "extra": note}})


async def mark_sold(pid, sell_price: float) -> bool:
    oid = _oid(pid)
    if not oid:
        return False
    if not await _db.purchases.find_one({"_id": oid}, {"_id": 1}):
        return False
    now = _now()
    await _db.ledger.update_one(
        {"purchase_id": oid},
        {"$set": {"sell_price": float(sell_price), "status": "sold", "closed_at": now}})
    await _db.purchases.update_one(
        {"_id": oid}, {"$set": {"status": "sold", "closed_at": now, "expires_at": 0}})
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
        if row.get("status") in ("terminated", "sold"):
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