import asyncio
import logging
import re
import time

from app import db, pricing, receiver_hub
from app.config import (SELL_CONFIRM_DEFAULT, SELL_CONFIRM_MAX, SELL_CONFIRM_MIN,
                        SELL_CONCURRENCY, SELL_OTP_TIMEOUT, SELL_REPLY_TIMEOUT, TARGET_HOURS)
from app.session_manager import check_session, clean_client, device_list, reset_others, set_2fa
from app.utils import countries, tg
from app.utils.texts import sell_fail_line, trade_text

log = logging.getLogger("seller")

CODE_RE = re.compile(r"\b(\d{5,6})\b")
OK_RE = re.compile(r"(received|accepted|success|done|confirmed|added|✅|👍)", re.I)
BAD_RE = re.compile(r"(fail|error|invalid|wrong|expired|denied|❌)", re.I)

_SEM: asyncio.Semaphore | None = None
_BG: set[asyncio.Task] = set()


def _sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(SELL_CONCURRENCY)
    return _SEM


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return task


async def drain(timeout: float = 10.0) -> None:
    if not _BG:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*list(_BG), return_exceptions=True), timeout)
    except asyncio.TimeoutError:
        log.info(f"shutdown: {len(_BG)} sell task(s) still running")


def _digits(text: str) -> str:
    m = CODE_RE.search(text or "")
    return m.group(1) if m else ""


def _masked(phone: str) -> str:
    raw = (phone or "").lstrip("+")
    if len(raw) < 7:
        return raw or "—"
    return f"{raw[:4]}{'*' * max(0, len(raw) - 7)}{raw[-3:]}"


async def _single_session(session_string: str, proxy: str) -> tuple[bool, int]:
    conn = clean_client(session_string, proxy)
    try:
        await conn.connect()
        if not await conn.is_user_authorized():
            return False, 0
        devices = await device_list(conn)
        return True, len(devices)
    except Exception as e:
        log.info(f"precheck: {e}")
        return False, 0
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass


async def _wait_otp(conn, timeout: int) -> str:
    end = time.monotonic() + timeout
    seen = 0
    try:
        msgs = await conn.get_messages(777000, limit=1)
        if msgs:
            seen = int(msgs[0].id or 0)
    except Exception:
        pass
    while time.monotonic() < end:
        try:
            msgs = await conn.get_messages(777000, limit=5, min_id=seen)
        except Exception:
            await asyncio.sleep(3.0)
            continue
        for msg in reversed(msgs or []):
            seen = max(seen, int(msg.id or 0))
            code = _digits(msg.raw_text or "")
            if code:
                return code
        await asyncio.sleep(3.0)
    return ""


async def sell(bot, lzt, purchase: dict, offer: dict, settings: dict) -> bool:
    async with _sem():
        return await _run(bot, lzt, purchase, offer, settings)


async def _run(bot, lzt, p: dict, offer: dict, s: dict) -> bool:
    tg_id = p["tg_id"]
    pid = str(p["_id"])
    phone = p.get("phone") or ""
    country = (p.get("country") or "").upper()
    receiver = offer.get("receiver") or ""
    proxy = s.get("proxy", "")
    buy_price = float(p.get("price") or 0.0)
    sell_price = float(offer.get("price") or 0.0)

    if not phone:
        await _notify(bot, tg_id, sell_fail_line(country, "no phone"))
        return False
    if not p.get("session_string"):
        await _notify(bot, tg_id, sell_fail_line(country, "no session"))
        return False
    if not receiver_hub.running():
        await receiver_hub.start()
    if not receiver_hub.running():
        await _notify(bot, tg_id, sell_fail_line(country, "userbot offline"))
        return False

    live, devices = await _single_session(p["session_string"], proxy)
    if not live:
        await db.set_purchase(pid, sell_state="dead")
        await _notify(bot, tg_id, sell_fail_line(country, "session dead"))
        return False

    if devices > 1:
        conn = clean_client(p["session_string"], proxy)
        try:
            await conn.connect()
            await reset_others(conn)
        except Exception as e:
            log.info(f"reset before sell {phone}: {e}")
        finally:
            try:
                await conn.disconnect()
            except Exception:
                pass

    password = s.get("twofa_password") or db.DEFAULT_2FA
    conn = clean_client(p["session_string"], proxy)
    try:
        await conn.connect()
        if not await conn.is_user_authorized():
            await db.set_purchase(pid, sell_state="dead")
            await _notify(bot, tg_id, sell_fail_line(country, "session dead"))
            return False
        await reset_others(conn)
        try:
            await set_2fa(conn, password, p.get("password") or "")
        except Exception as e:
            log.info(f"2fa prep {phone}: {e}")
        await db.set_purchase(pid, password=password)

        confirm = int(offer.get("confirm") or 0) or int(s.get("sell_confirm_seconds")
                                                       or SELL_CONFIRM_DEFAULT)
        confirm = max(SELL_CONFIRM_MIN, min(SELL_CONFIRM_MAX, confirm))

        sid = await db.add_sale(tg_id, pid, country, offer.get("receiver_id"),
                                receiver, buy_price, sell_price, confirm)

        await receiver_hub.drain(receiver)
        if not await receiver_hub.send(receiver, phone):
            await db.close_sale(sid, "failed", "dm failed")
            await db.set_purchase(pid, sell_state="")
            await _notify(bot, tg_id, sell_fail_line(country, "receiver dm failed"))
            return False

        reply = await receiver_hub.wait_reply(receiver, SELL_REPLY_TIMEOUT)
        if not reply:
            await db.close_sale(sid, "failed", "no reply")
            await db.set_purchase(pid, sell_state="")
            await _notify(bot, tg_id, sell_fail_line(country, "no receiver reply"))
            return False

        code = _digits(reply)
        if not code:
            code = await _wait_otp(conn, SELL_OTP_TIMEOUT)
            if not code:
                await db.close_sale(sid, "failed", "no otp")
                await db.set_purchase(pid, sell_state="")
                await _notify(bot, tg_id, sell_fail_line(country, "no otp"))
                return False
            await receiver_hub.send(receiver, code)
            reply = await receiver_hub.wait_reply(receiver, SELL_REPLY_TIMEOUT)

        if reply and BAD_RE.search(reply) and not OK_RE.search(reply):
            await db.close_sale(sid, "failed", reply[:200])
            await db.set_purchase(pid, sell_state="")
            await _notify(bot, tg_id, sell_fail_line(country, reply[:120]))
            return False

        try:
            await conn.log_out()
        except Exception as e:
            log.info(f"logout {phone}: {e}")

        await db.set_purchase(pid, session_state="dead")
        await db.close_purchase(pid, "ready", "sold to " + receiver)

        verdict = await receiver_hub.wait_reply(receiver, confirm)
        if verdict and BAD_RE.search(verdict) and not OK_RE.search(verdict):
            await db.close_sale(sid, "failed", verdict[:200])
            await db.set_purchase(pid, sell_state="failed")
            await _notify(bot, tg_id, sell_fail_line(country, verdict[:120]))
            return False

        await db.mark_sold(pid, sell_price, tg_id)
        await db.close_sale(sid, "sold", verdict[:200] or "reconciled")
        fresh = await db.get_purchase(pid)
        sup = await db.get_username(tg_id)
        await _notify(bot, tg_id, trade_text(fresh or p, offer, sup,
                                             devices + 1, confirm, sid))
        return True
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass


async def _notify(bot, tg_id, text):
    await tg.send(bot, tg_id, text)


async def auto_sell(bot, lzt, purchase: dict) -> bool:
    s = await db.get_settings(purchase["tg_id"])
    if s.get("auto_sell") != "1":
        return False
    country = (purchase.get("country") or "").upper()
    if not country:
        return False
    floor = db.num(s.get("sell_min_price"))
    offer = await pricing.best_offer(purchase["tg_id"], country, floor)
    if offer is None:
        await db.set_purchase(purchase["_id"], sell_state="no_offer")
        return False
    if offer["price"] <= float(purchase.get("price") or 0.0):
        await db.set_purchase(purchase["_id"], sell_state="no_profit")
        return False
    return await sell(bot, lzt, purchase, offer, s)


def schedule(bot, lzt, purchase: dict):
    _spawn(auto_sell(bot, lzt, purchase))


async def manual(bot, lzt, purchase: dict, country: str | None = None) -> tuple[bool, str]:
    s = await db.get_settings(purchase["tg_id"])
    cc = (country or purchase.get("country") or "").upper()
    if not cc:
        return False, "country unknown"
    offer = await pricing.best_offer(purchase["tg_id"], cc, db.num(s.get("sell_min_price")))
    if offer is None:
        return False, f"no live offer for {cc}"
    ok = await sell(bot, lzt, purchase, offer, s)
    return ok, f"{cc} → @{offer['receiver']} ${offer['price']:.2f}"


async def sellable(tg_id: int, limit: int = 40) -> list[dict]:
    rows = await db.ready_purchases(tg_id, limit)
    return [p for p in rows if p.get("session_string")
            and (p.get("sell_state") or "") not in ("selling", "sold")]