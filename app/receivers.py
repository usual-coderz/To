import logging

from app import db, receiver_hub
from app.config import CAP_REFRESH, PRICE_REFRESH
from app.pricing import kind_of, refresh_all

log = logging.getLogger("receivers")


async def add(tg_id: int, username: str, title: str = "") -> str | None:
    key = str(username or "").strip().lstrip("@")
    if not key:
        return None
    return await db.add_receiver(tg_id, key, kind_of(key), title)


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


async def capacity(tg_id: int, rid: str) -> int:
    receiver = await db.get_receiver(rid)
    if not receiver or receiver["tg_id"] != tg_id:
        return -1
    if not receiver_hub.running():
        await receiver_hub.start()
    for cmd in ("/cap", "/capacity"):
        reply = await receiver_hub.command(receiver["username"], cmd)
        if not reply:
            continue
        from app.pricing import parse_cap, parse_confirm
        cap = parse_cap(reply)
        confirm = parse_confirm(reply)
        if cap >= 0:
            await db.bump_receiver(rid, cap, confirm or None)
            return cap
    await db.fail_receiver(rid)
    return -1


async def capacity_all(tg_id: int) -> dict:
    rows = await db.list_receivers(tg_id, enabled_only=True)
    if not receiver_hub.running():
        await receiver_hub.start()
    out = {}
    for receiver in rows:
        out[receiver["username"]] = await capacity(tg_id, str(receiver["_id"]))
    return out


async def sync(tg_id: int) -> dict:
    return await refresh_all(tg_id)


def stale(receiver: dict, now: int) -> bool:
    return now - int(receiver.get("cap_at") or 0) > CAP_REFRESH


def price_stale(receiver: dict, now: int) -> bool:
    return now - int(receiver.get("price_at") or 0) > PRICE_REFRESH