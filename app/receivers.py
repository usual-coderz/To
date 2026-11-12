import logging

from app import db, pricing, receiver_hub
from app.config import CAP_REFRESH, PRICE_REFRESH

log = logging.getLogger("receivers")


async def add(tg_id: int, username: str, title: str = "") -> str | None:
    key = str(username or "").strip().lstrip("@")
    if not key:
        return None
    return await db.add_receiver(tg_id, key, pricing.kind_of(key), title)


async def remove(tg_id: int, rid: str) -> bool:
    receiver = await db.get_receiver(rid)
    if not receiver or receiver["tg_id"] != tg_id:
        return False
    return await db.drop_receiver(rid)


async def toggle(tg_id: int, rid: str) -> bool:
    receiver = await db.get_receiver(rid)
    if not receiver or receiver["tg_id"] != tg_id:
        return False
    await db.set_receiver(rid, enabled=not receiver.get("enabled", True))
    return True


async def warm(tg_id: int, rid: str = "") -> int:
    if not receiver_hub.running():
        await receiver_hub.start()
    rows = ([await db.get_receiver(rid)] if rid else await db.list_receivers(tg_id))
    done = 0
    for receiver in rows:
        if not receiver or receiver["tg_id"] != tg_id:
            continue
        if await receiver_hub.start_bot(receiver["username"]):
            done += 1
    return done


async def capacity(tg_id: int, rid: str) -> int:
    receiver = await db.get_receiver(rid)
    if not receiver or receiver["tg_id"] != tg_id:
        return -1
    if not receiver_hub.running():
        await receiver_hub.start()
    out = await pricing.probe(receiver)
    if out["prices"]:
        await db.save_prices(tg_id, rid, out["prices"])
        await db.set_receiver(rid, price_at=db._now())
    if out["cap"] < 0:
        await db.fail_receiver(rid)
        return -1
    await db.bump_receiver(rid, out["cap"], out["confirm"] or None)
    return out["cap"]


async def capacity_all(tg_id: int) -> dict:
    rows = await db.list_receivers(tg_id, enabled_only=True)
    if not receiver_hub.running():
        await receiver_hub.start()
    out = {}
    for receiver in rows:
        out[receiver["username"]] = await capacity(tg_id, str(receiver["_id"]))
    return out


async def sync(tg_id: int) -> dict:
    return await pricing.refresh_all(tg_id)


def stale(receiver: dict, now: int) -> bool:
    return now - int(receiver.get("cap_at") or 0) > CAP_REFRESH


def price_stale(receiver: dict, now: int) -> bool:
    return now - int(receiver.get("price_at") or 0) > PRICE_REFRESH