import asyncio
import logging
import time

from app import db, pricing, receiver_hub, sellparse
from app.config import (SELL_CONFIRM_DEFAULT, SELL_CONFIRM_MIN, SELL_CONCURRENCY,
                        SELL_MAX_TRIES, SELL_OTP_TIMEOUT, SELL_QUEUE_BATCH,
                        SELL_QUEUE_TICK, SELL_REPLY_TIMEOUT, SELL_RETRY_DELAY)
from app.session_manager import clean_client, device_list, reset_others, set_2fa
from app.utils import countries, tg
from app.utils.texts import sell_fail_line, trade_text

log = logging.getLogger("seller")

_SEM: asyncio.Semaphore | None = None
_BG: set[asyncio.Task] = set()
_RUNNING: set[str] = set()


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


async def _notify(bot, tg_id, text):
    await tg.send(bot, tg_id, text)


async def _cancel(receiver: str):
    if receiver:
        await receiver_hub.send(receiver, "/cancel")


async def _dm(receiver: str, phone: str) -> bool:
    await receiver_hub.drain(receiver)
    if await receiver_hub.send(receiver, phone):
        return True
    await receiver_hub.start_bot(receiver)
    await asyncio.sleep(2.5)
    return await receiver_hub.send(receiver, phone)


async def _precheck(session_string: str, proxy: str) -> tuple[bool, int]:
    conn = clean_client(session_string, proxy)
    try:
        await conn.connect()
        if not await conn.is_user_authorized():
            return False, 0
        return True, len(await device_list(conn))
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
            code = sellparse.otp_of(msg.raw_text or "")
            if code:
                return code
        await asyncio.sleep(3.0)
    return ""


async def _fail(bot, tg_id, pid, country, sid, receiver, reason, retry=True):
    text = sellparse.reason_of(reason)
    if sid:
        await db.close_sale(sid, "failed", text)
    if retry:
        await db.bump_sell_try(pid, SELL_RETRY_DELAY, text)
    else:
        await db.set_sell(pid, "failed", SELL_RETRY_DELAY, text)
    await _notify(bot, tg_id, sell_fail_line(country, text))
    await _cancel(receiver)
    return False


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
    sid = ""

    if pid in _RUNNING:
        return False
    _RUNNING.add(pid)
    try:
        if not phone:
            return await _fail(bot, tg_id, pid, country, "", "", "no phone", False)
        if not p.get("session_string"):
            return await _fail(bot, tg_id, pid, country, "", "", "no session", False)
        if not receiver_hub.running():
            await receiver_hub.start()
        if not receiver_hub.running():
            return await _fail(bot, tg_id, pid, country, "", "", "userbot offline")

        live, devices = await _precheck(p["session_string"], proxy)
        if not live:
            await db.set_sell(pid, "dead", SELL_RETRY_DELAY, "session dead")
            await _notify(bot, tg_id, sell_fail_line(country, "session dead"))
            return False

        password = s.get("twofa_password") or db.DEFAULT_2FA
        conn = clean_client(p["session_string"], proxy)
        try:
            await conn.connect()
            if not await conn.is_user_authorized():
                await db.set_sell(pid, "dead", SELL_RETRY_DELAY, "session dead")
                await _notify(bot, tg_id, sell_fail_line(country, "session dead"))
                return False

            await reset_others(conn)
            try:
                await set_2fa(conn, password, p.get("password") or "")
            except Exception as e:
                log.info(f"2fa prep {phone}: {e}")
            await db.set_purchase(pid, password=password)

            sid = await db.add_sale(tg_id, pid, country, offer.get("receiver_id"),
                                    receiver, buy_price, sell_price, 0)

            if not await _dm(receiver, phone):
                return await _fail(bot, tg_id, pid, country, sid, "", "receiver dm failed")

            reply = await receiver_hub.collect(receiver, None, 3.0, SELL_REPLY_TIMEOUT)
            if not reply:
                return await _fail(bot, tg_id, pid, country, sid, receiver, "no receiver reply")
            if sellparse.rejected(reply):
                return await _fail(bot, tg_id, pid, country, sid, receiver, reply)

            if not sellparse.accepted(reply) and not sellparse.wants_code(reply):
                extra = await receiver_hub.collect(receiver, None, 3.0, 45.0)
                if extra:
                    reply = reply + "\n" + extra

            if not sellparse.accepted(reply):
                code = sellparse.otp_of(reply) or await _wait_otp(conn, SELL_OTP_TIMEOUT)
                if not code:
                    return await _fail(bot, tg_id, pid, country, sid, receiver, "no otp")
                await receiver_hub.send(receiver, code)
                reply = await receiver_hub.collect(receiver, None, 3.0, SELL_REPLY_TIMEOUT)
                if not reply:
                    return await _fail(bot, tg_id, pid, country, sid, receiver, "no accept")
                if sellparse.rejected(reply) and not sellparse.accepted(reply):
                    return await _fail(bot, tg_id, pid, country, sid, receiver, reply)

            parsed = sellparse.price_of(reply)
            if parsed > 0:
                sell_price = parsed
                await db.set_sale_price(sid, sell_price)

            confirm = sellparse.confirm_of(reply) or int(
                offer.get("confirm") or 0) or int(s.get("sell_confirm_seconds")
                                                  or SELL_CONFIRM_DEFAULT)
            if confirm < SELL_CONFIRM_MIN:
                confirm = SELL_CONFIRM_MIN
            await db.set_sale_confirm(sid, confirm)

            try:
                await conn.log_out()
            except Exception as e:
                log.info(f"logout {phone}: {e}")
            await db.set_purchase(pid, session_state="dead")
            await db.close_purchase(pid, "ready", "sold to " + receiver)

            verdict = await receiver_hub.collect(receiver, None, 3.0, confirm)
            if verdict and sellparse.rejected(verdict) and not sellparse.accepted(verdict):
                return await _fail(bot, tg_id, pid, country, sid, receiver, verdict)

            await db.mark_sold(pid, sell_price, tg_id)
            await db.close_sale(sid, "sold", (verdict or "reconciled")[:200])
            fresh = await db.get_purchase(pid) or p
            totals = await db.sell_totals(tg_id)
            sup = await db.get_username(tg_id)
            await _notify(bot, tg_id, trade_text(fresh, offer, sup, devices + 1,
                                                 confirm, sid, sell_price, totals))
            return True
        finally:
            try:
                await conn.disconnect()
            except Exception:
                pass
    finally:
        _RUNNING.discard(pid)


async def try_one(bot, lzt, purchase: dict) -> bool:
    s = await db.get_settings(purchase["tg_id"])
    if s.get("auto_sell") != "1":
        return False
    cc = (purchase.get("country") or "").upper()
    if not cc:
        await db.set_sell(purchase["_id"], "no_country", SELL_RETRY_DELAY, "country unknown")
        return False
    if int(purchase.get("sell_try") or 0) >= SELL_MAX_TRIES:
        await db.set_sell(purchase["_id"], "failed", SELL_RETRY_DELAY,
                          "max tries reached")
        return False
    offer = await pricing.best_offer(purchase["tg_id"], cc, db.num(s.get("sell_min_price")))
    if offer is None:
        await db.set_sell(purchase["_id"], "no_offer", 300, f"no live offer for {cc}")
        return False
    if offer["price"] <= float(purchase.get("price") or 0.0):
        await db.set_sell(purchase["_id"], "no_profit", 600,
                          f"best ${offer['price']:.2f} below buy")
        return False
    return await sell(bot, lzt, purchase, offer, s)


def schedule(bot, lzt, purchase: dict):
    _spawn(try_one(bot, lzt, purchase))


async def manual(bot, lzt, purchase: dict) -> tuple[bool, str]:
    s = await db.get_settings(purchase["tg_id"])
    cc = (purchase.get("country") or "").upper()
    if not cc:
        return False, "country unknown"
    offer = await pricing.best_offer(purchase["tg_id"], cc, db.num(s.get("sell_min_price")))
    if offer is None:
        return False, f"no live offer for {cc}"
    ok = await sell(bot, lzt, purchase, offer, s)
    return ok, f"{cc} → @{offer['receiver']} ${offer['price']:.2f}"


async def worker(bot, lzt):
    while True:
        try:
            for owner in await db.all_owners():
                tg_id = owner["_id"]
                s = await db.get_settings(tg_id)
                if s.get("auto_sell") != "1":
                    continue
                for p in await db.sell_queue(tg_id, SELL_QUEUE_BATCH):
                    pid = str(p["_id"])
                    if pid in _RUNNING:
                        continue
                    _spawn(try_one(bot, lzt, p))
                    await asyncio.sleep(0.4)
        except Exception as e:
            log.exception(f"worker: {e}")
        await asyncio.sleep(SELL_QUEUE_TICK)


def offer_label(offer: dict) -> str:
    return (f"{countries.flag_of(offer.get('country', ''))}"
            f" @{offer.get('receiver')} ${offer.get('price', 0):.2f}")