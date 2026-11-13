import asyncio
import logging
import re

from app import db, receiver_hub
from app.config import CAP_COMMAND, LION_START, PRICE_TTL, PROBE_TIMEOUT
from app.utils import countries

log = logging.getLogger("pricing")

TIE_RANK = {"tgget": 0, "lion": 1, "cm": 2}
LION = "lion"

_FLAG = "[\U0001F1E6-\U0001F1FF]{2}"
_OFFER = re.compile(
    rf"(?P<flag>{_FLAG})?\s*\b(?P<cc>[A-Z]{{2}})\b\s*\+?(?P<dial>\d{{1,4}})?"
    r"\s*(?:→|->|=>|:|=|—|–|\|)?\s*\$\s*(?P<price>\d{1,4}(?:[.,]\d{1,2})?)")
_LOOSE = re.compile(
    rf"(?P<flag>{_FLAG})?\s*\b(?P<cc>[A-Z]{{2}})\b\s*\+?(?P<dial>\d{{1,4}})?"
    r"[^\n\d$]{0,8}(?P<price>\d{1,4}(?:[.,]\d{1,2})?)")
_CAP = re.compile(
    r"(?:cap(?:acity)?|available|free|slots?|stock|limit|qty)\D{0,12}(\d{1,6})", re.I)
_CONFIRM = re.compile(r"(\d{3,5})\s*(?:s\b|sec|second)", re.I)


def _num(raw: str) -> float:
    try:
        return float(str(raw).replace(",", "."))
    except ValueError:
        return 0.0


def _dial(raw) -> str:
    text = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return text[:4]


def parse_offers(text: str) -> list[dict]:
    found: dict[str, dict] = {}
    source = text or ""
    for rx in (_OFFER, _LOOSE):
        for line in source.splitlines():
            clean = line.strip()
            if not clean or "$" not in clean and "→" not in clean:
                continue
            for m in rx.finditer(clean):
                cc = m.group("cc").upper()
                price = _num(m.group("price"))
                if not price or not countries.name_of(cc):
                    continue
                if cc in found and found[cc]["price"] >= price:
                    continue
                flag = m.group("flag") or countries.flag_of(cc)
                found[cc] = {"country": cc, "flag": flag,
                             "dial": _dial(m.group("dial")), "price": price}
        if found:
            break
    return sorted(found.values(), key=lambda o: -o["price"])


def parse_prices(text: str) -> dict:
    return {o["country"]: o["price"] for o in parse_offers(text)}


def parse_cap(text: str) -> int:
    m = _CAP.search(text or "")
    return int(m.group(1)) if m else -1


def parse_confirm(text: str) -> int:
    m = _CONFIRM.search(text or "")
    if not m:
        return 0
    value = int(m.group(1))
    return value if 300 <= value <= 7200 else 0


def kind_of(username: str) -> str:
    key = str(username or "").lower()
    if "lion" in key:
        return LION
    if "get" in key:
        return "tgget"
    return "cm"


def _blank() -> dict:
    return {"cap": -1, "confirm": 0, "offers": [], "replies": 0, "raw": ""}


def _absorb(out: dict, reply: str):
    if not reply:
        return
    out["replies"] += 1
    out["raw"] = (out["raw"] + "\n" + reply)[-4000:]
    known = {o["country"]: o["price"] for o in out["offers"]}
    for offer in parse_offers(reply):
        if offer["country"] in known and known[offer["country"]] >= offer["price"]:
            continue
        out["offers"] = [o for o in out["offers"] if o["country"] != offer["country"]]
        out["offers"].append(offer)
    cap = parse_cap(reply)
    if cap >= 0 and out["cap"] < 0:
        out["cap"] = cap
    confirm = parse_confirm(reply)
    if confirm:
        out["confirm"] = confirm


async def probe(receiver: dict) -> dict:
    username = receiver.get("username") or ""
    kind = receiver.get("kind") or kind_of(username)
    out = _blank()
    if kind == LION:
        _absorb(out, await receiver_hub.start_and_read(username, LION_START, PROBE_TIMEOUT))
        return out
    _absorb(out, await receiver_hub.command(username, CAP_COMMAND, PROBE_TIMEOUT))
    return out


async def refresh_receiver(receiver: dict) -> dict:
    out = await probe(receiver)
    if out["offers"]:
        await db.save_prices(receiver["tg_id"], receiver["_id"], out["offers"])
        await db.set_receiver(receiver["_id"], price_at=db._now())
    if out["cap"] < 0:
        await db.fail_receiver(receiver["_id"])
    else:
        await db.bump_receiver(receiver["_id"], out["cap"], out["confirm"] or None)
    return out


async def refresh_all(tg_id: int) -> dict:
    if not receiver_hub.running():
        await receiver_hub.start()
    rows = await db.list_receivers(tg_id, enabled_only=True)
    out = {"receivers": len(rows), "caps": 0, "prices": 0, "errors": 0, "empty": []}
    sem = asyncio.Semaphore(3)

    async def one(receiver):
        async with sem:
            try:
                result = await refresh_receiver(receiver)
            except Exception as e:
                log.info(f"refresh {receiver.get('username')}: {e}")
                out["errors"] += 1
                out["empty"].append(f"@{receiver.get('username')} error")
                return
            if result["cap"] > 0:
                out["caps"] += 1
            out["prices"] += len(result["offers"])
            if not result["offers"]:
                out["empty"].append(f"@{receiver.get('username')} no offers")

    await asyncio.gather(*[one(r) for r in rows], return_exceptions=True)
    return out


async def offers_map(tg_id: int, max_age: float = PRICE_TTL) -> dict:
    rows = await db.latest_prices(tg_id, max_age)
    receivers = {str(r["_id"]): r for r in await db.list_receivers(tg_id, enabled_only=True)}
    out: dict = {}
    for row in rows:
        rid = str((row.get("_id") or {}).get("receiver_id") or "")
        receiver = receivers.get(rid)
        if not receiver:
            continue
        cc = row["_id"]["country"]
        out.setdefault(cc, []).append({
            "price": float(row.get("price") or 0.0), "ts": int(row.get("ts") or 0),
            "flag": row.get("flag") or countries.flag_of(cc),
            "dial": row.get("dial") or "",
            "receiver_id": rid, "receiver": receiver.get("username") or "",
            "kind": receiver.get("kind") or "cm", "cap": int(receiver.get("cap") or 0),
            "confirm": int(receiver.get("confirm_seconds") or 0),
        })
    return out


def rank(offer: dict) -> tuple:
    return (-round(offer["price"], 4), TIE_RANK.get(offer.get("kind"), 9),
            -offer.get("cap", 0), -offer.get("ts", 0))


async def offers_for(tg_id: int, country: str, max_age: float = PRICE_TTL) -> list[dict]:
    cc = str(country or "").strip().upper()
    if not cc:
        return []
    return sorted((await offers_map(tg_id, max_age)).get(cc, []), key=rank)


async def best_offer(tg_id: int, country: str, floor: float = 0.0) -> dict | None:
    for offer in await offers_for(tg_id, country):
        if offer["price"] < floor:
            continue
        if offer.get("cap") == 0:
            continue
        return offer
    return None


async def best_table(tg_id: int, max_age: float = PRICE_TTL) -> list[dict]:
    data = await offers_map(tg_id, max_age)
    rows = []
    for cc, offers in data.items():
        ranked = sorted(offers, key=rank)
        if ranked:
            rows.append({"country": cc, "offers": ranked})
    rows.sort(key=lambda r: -r["offers"][0]["price"])
    return rows


async def receiver_table(tg_id: int, rid: str, max_age: float = PRICE_TTL) -> list[dict]:
    data = await offers_map(tg_id, max_age)
    rows = []
    for cc, offers in data.items():
        mine = [o for o in offers if o["receiver_id"] == rid]
        if mine:
            rows.append({"country": cc, "offers": sorted(mine, key=rank)})
    rows.sort(key=lambda r: -r["offers"][0]["price"])
    return rows


async def price_matrix(tg_id: int, max_age: float = PRICE_TTL) -> list[dict]:
    receivers = await db.list_receivers(tg_id, enabled_only=True)
    prices = await offers_map(tg_id, max_age)
    out = []
    for cc, offers in prices.items():
        ranked = sorted(offers, key=rank)
        if not ranked:
            continue
        best = ranked[0]
        line = {"country": cc, "name": countries.name_of(cc),
                "flag": best.get("flag") or countries.flag_of(cc),
                "dial": best.get("dial") or "", "best": best, "cells": []}
        for receiver in receivers:
            hit = next((o for o in offers if o["receiver_id"] == str(receiver["_id"])), None)
            line["cells"].append({"receiver": receiver.get("username"),
                                  "kind": receiver.get("kind"),
                                  "price": hit["price"] if hit else None})
        out.append(line)
    out.sort(key=lambda r: -r["best"]["price"])
    return out