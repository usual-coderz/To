import asyncio
import logging
import re

from app import db, receiver_hub
from app.config import (CAP_COMMANDS, CAP_REFRESH, LION_BOT, LION_START, PRICE_REFRESH,
                        PRICE_TTL)
from app.utils import countries

log = logging.getLogger("pricing")

TIE_RANK = {"tgget": 0, "lion": 1, "cm": 2}

_FLAG = "[\U0001F1E6-\U0001F1FF]{2}"
_LINE = re.compile(
    rf"(?:{_FLAG}\s*)?\b([A-Z]{{2}})\b\s*(?:\+\d{{1,4}})?\s*(?:→|->|=>|:|=|—|–|\|)?\s*"
    r"\$?\s*(\d{1,4}(?:[.,]\d{1,2})?)")
_PLAIN = re.compile(r"(?<![\w$])([A-Z]{2})\s+\$\s*(\d{1,4}(?:[.,]\d{1,2})?)")
_CAP = re.compile(
    r"(?:cap(?:acity)?|available|free|slots?|stock|limit|qty)\D{0,12}(\d{1,6})", re.I)
_CONFIRM = re.compile(r"(\d{3,5})\s*(?:s\b|sec|second)", re.I)
_STOP = {"OK", "TG", "ID", "PR", "US", "NO"}


def _price(raw: str) -> float:
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return 0.0


def parse_prices(text: str) -> dict:
    out: dict = {}
    for line in (text or "").splitlines():
        clean = line.strip()
        if not clean or "$" not in clean and "→" not in clean and "->" not in clean:
            continue
        for m in _LINE.finditer(clean):
            cc = m.group(1).upper()
            value = _price(m.group(2))
            if not value or not countries.name_of(cc):
                continue
            if cc in out and out[cc] >= value:
                continue
            out[cc] = value
    if not out:
        for m in _PLAIN.finditer(text or ""):
            cc = m.group(1).upper()
            value = _price(m.group(2))
            if value and countries.name_of(cc):
                out.setdefault(cc, value)
    return out


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
        return "lion"
    if "get" in key:
        return "tgget"
    return "cm"


async def refresh_receiver(receiver: dict) -> int:
    username = receiver.get("username") or ""
    kind = receiver.get("kind") or kind_of(username)
    cap = -1
    if kind == "lion":
        await receiver_hub.start_bot(username, LION_START)
        reply = await receiver_hub.wait_reply(username, 25.0)
        prices = parse_prices(reply)
        if prices:
            await db.save_prices(receiver["tg_id"], receiver["_id"], prices)
            await db.set_receiver(receiver["_id"], price_at=db._now())
        cap = parse_cap(reply)
    else:
        for cmd in CAP_COMMANDS:
            reply = await receiver_hub.command(username, cmd)
            if not reply:
                continue
            prices = parse_prices(reply)
            if prices:
                await db.save_prices(receiver["tg_id"], receiver["_id"], prices)
                await db.set_receiver(receiver["_id"], price_at=db._now())
            cap = parse_cap(reply)
            confirm = parse_confirm(reply)
            if cap >= 0 or confirm:
                await db.bump_receiver(receiver["_id"], cap if cap >= 0 else None, confirm)
                return cap
        if cap < 0:
            await db.fail_receiver(receiver["_id"])
        return cap

    if cap < 0:
        await db.fail_receiver(receiver["_id"])
    await db.bump_receiver(receiver["_id"], cap if cap >= 0 else None)
    return cap


async def refresh_all(tg_id: int) -> dict:
    if not receiver_hub.running():
        await receiver_hub.start()
    rows = await db.list_receivers(tg_id, enabled_only=True)
    out = {"receivers": len(rows), "caps": 0, "prices": 0, "errors": 0}
    sem = asyncio.Semaphore(3)

    async def one(receiver):
        async with sem:
            try:
                cap = await refresh_receiver(receiver)
            except Exception as e:
                log.info(f"refresh {receiver.get('username')}: {e}")
                out["errors"] += 1
                return
            if cap > 0:
                out["caps"] += 1

    await asyncio.gather(*[one(r) for r in rows], return_exceptions=True)
    offers = await offers_map(tg_id, PRICE_TTL)
    out["prices"] = sum(len(v) for v in offers.values())
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
        if not ranked:
            continue
        rows.append({"country": cc, "offers": ranked})
    rows.sort(key=lambda r: -r["offers"][0]["price"])
    return rows


async def price_matrix(tg_id: int) -> list[dict]:
    receivers = await db.list_receivers(tg_id, enabled_only=True)
    prices = await offers_map(tg_id, PRICE_TTL)
    out = []
    for cc, offers in prices.items():
        line = {"country": cc, "name": countries.name_of(cc),
                "flag": countries.flag_of(cc), "best": None, "cells": []}
        ranked = sorted(offers, key=rank)
        line["best"] = ranked[0] if ranked else None
        for receiver in receivers:
            hit = next((o for o in offers if o["receiver_id"] == str(receiver["_id"])), None)
            line["cells"].append({"receiver": receiver.get("username"),
                                  "kind": receiver.get("kind"),
                                  "price": hit["price"] if hit else None})
        out.append(line)
    out.sort(key=lambda r: -(r["best"]["price"] if r["best"] else 0.0))
    return out