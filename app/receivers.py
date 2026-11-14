import asyncio
import logging
import time

from app import db, pricing, receiver_hub
from app.config import CAP_REFRESH, PRICE_REFRESH, WARM_TTL

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
            await db.mark_warm(receiver["_id"])
            done += 1
        await asyncio.sleep(1.5)
    return done


async def capacity(tg_id: int, rid: str) -> dict:
    blank = {"cap": -1, "confirm": 0, "offers": [], "replies": 0, "raw": "", "warm": False}
    receiver = await db.get_receiver(rid)
    if not receiver or receiver["tg_id"] != tg_id:
        return blank
    if not receiver_hub.running():
        await receiver_hub.start()
    return await pricing.record(receiver)


async def capacity_all(tg_id: int) -> dict:
    rows = await db.list_receivers(tg_id, enabled_only=True)
    if not receiver_hub.running():
        await receiver_hub.start()
    out = {}
    for receiver in rows:
        out[receiver["username"]] = (await pricing.record(receiver))["cap"]
    return out


async def capacity_sweep(tg_id: int) -> dict:
    rows = await db.list_receivers(tg_id, enabled_only=True)
    if not receiver_hub.running():
        await receiver_hub.start()
    out = {"total": len(rows), "ok": 0, "missing": 0, "prices": 0, "empty": []}
    for receiver in rows:
        try:
            result = await pricing.record(receiver, True)
        except Exception as e:
            log.info(f"cap sweep {receiver.get('username')}: {e}")
            out["missing"] += 1
            out["empty"].append(f"@{receiver.get('username')} error {e}"[:90])
            continue
        out["prices"] += len(result["offers"])
        if result["cap"] >= 0:
            out["ok"] += 1
        else:
            out["missing"] += 1
            out["empty"].append(f"@{receiver.get('username')} no cap answer")
        await asyncio.sleep(2.5)
    return out


async def due_warm(tg_id: int) -> list[dict]:
    now = int(time.time())
    rows = await db.list_receivers(tg_id, enabled_only=True)
    return [r for r in rows if now - int(r.get("warm_at") or 0) > WARM_TTL * 3600]


async def refresh_stale(tg_id: int) -> int:
    rows = await due_warm(tg_id)
    if not rows:
        return 0
    if not receiver_hub.running():
        await receiver_hub.start()
    done = 0
    for receiver in rows:
        try:
            await pricing.record(receiver)
            done += 1
        except Exception as e:
            log.info(f"stale refresh {receiver.get('username')}: {e}")
        await asyncio.sleep(2.0)
    return done


async def sync(tg_id: int) -> dict:
    return await pricing.refresh_all(tg_id)


def stale(receiver: dict, now: int) -> bool:
    return now - int(receiver.get("cap_at") or 0) > CAP_REFRESH


def price_stale(receiver: dict, now: int) -> bool:
    return now - int(receiver.get("price_at") or 0) > PRICE_REFRESH


def warm_stale(receiver: dict, now: int) -> bool:
    return now - int(receiver.get("warm_at") or 0) > WARM_TTL * 3600