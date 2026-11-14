import asyncio
import logging
import time

from app import db, mail_engine, pricing, receiver_hub, sellparse
from app.config import (SELL_CONFIRM_DEFAULT, SELL_CONFIRM_MIN, SELL_CONCURRENCY,
                        SELL_LOGOUT_DELAY, SELL_OTP_TIMEOUT, SELL_QUEUE_BATCH,
                        SELL_QUEUE_TICK, SELL_REPLY_TIMEOUT, SELL_RETRY_DELAY,
                        SELL_VERDICT_WAIT)
from app.session_manager import clean_client, device_list, remove_2fa, reset_others
from app.utils import countries, tg
from app.utils.texts import sell_audit_text, sell_fail_line, trade_text

log = logging.getLogger("seller")

_SEM: asyncio.Semaphore | None = None
_BG: set[asyncio.Task] = set()
_RUNNING: set[str] = set()

MAX_CODE_SENDS = 3
MAX_STEPS = 8
SERVICE_ID = 777000
SERVICE_NAMES = ("777000", "Telegram")
NUDGE_AFTER = 40
NUDGE_WORDS = ("resend",)
FIRST_WAIT = 60
HISTORY_GAP = 3.5


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


async def drain(timeout: float = 15.0) -> None:
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


async def _service_entity(conn):
    for target in (SERVICE_ID,) + SERVICE_NAMES:
        try:
            entity = await conn.get_entity(target)
            if entity is not None:
                return entity
        except Exception as e:
            log.info(f"service entity {target}: {e}")
    return SERVICE_ID


async def _baseline(conn, entity) -> int:
    try:
        msgs = await conn.get_messages(entity, limit=1)
        return int(msgs[0].id or 0) if msgs else 0
    except Exception as e:
        log.info(f"otp baseline: {e}")
        return 0


async def _poll_tg(conn, entity, timeout: float, since: int, skip: set[str]) -> str:
    end = time.monotonic() + timeout
    warned = False
    while time.monotonic() < end:
        try:
            msgs = await conn.get_messages(entity, limit=5, min_id=since)
        except Exception as e:
            if not warned:
                log.info(f"otp tg read: {e}")
                warned = True
            await asyncio.sleep(4.0)
            continue
        warned = False
        fresh = sorted(msgs or [], key=lambda m: int(m.id or 0))
        for msg in fresh:
            since = max(since, int(msg.id or 0))
            code = sellparse.otp_of(msg.raw_text or "")
            if code and code not in skip:
                log.info(f"otp tg hit: {code}")
                return code
        await asyncio.sleep(HISTORY_GAP)
    return ""


async def _poll_mail(p: dict, timeout: float, skip: set[str]) -> str:
    client = mail_engine.client_from(p)
    if client is None:
        return ""
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            try:
                code = await client._check_inbox(p.get("mail"), None)
            except Exception as e:
                log.info(f"mail poll {p.get('phone')}: {e}")
                code = None
            if code and code not in skip:
                log.info(f"otp mail hit: {code}")
                return code
            await asyncio.sleep(3.0)
        return ""
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def _nudge(receiver: str, timeout: float):
    await asyncio.sleep(min(NUDGE_AFTER, max(15, timeout // 3)))
    for word in NUDGE_WORDS:
        log.info(f"otp nudge {receiver}: {word}")
        if await receiver_hub.send(receiver, word):
            await asyncio.sleep(2.0)


async def _race(coros, timeout: float) -> str:
    tasks = [asyncio.create_task(c) for c in coros]
    end = time.monotonic() + timeout
    try:
        while tasks:
            left = end - time.monotonic()
            if left <= 0:
                return ""
            done, pending = await asyncio.wait(tasks, timeout=left,
                                               return_when=asyncio.FIRST_COMPLETED)
            tasks = list(pending)
            for task in done:
                try:
                    code = task.result()
                except Exception:
                    continue
                if code:
                    return code
        return ""
    finally:
        for task in tasks:
            task.cancel()


async def _wait_otp(conn, p: dict, receiver: str, timeout: int,
                    since: int, skip: set[str]) -> str:
    entity = await _service_entity(conn)
    nudge = asyncio.create_task(_nudge(receiver, timeout))
    try:
        code = await _race([_poll_tg(conn, entity, timeout, since, skip),
                            _poll_mail(p, timeout, skip)], timeout + 5)
    finally:
        nudge.cancel()
    if not code:
        log.info(f"otp {p.get('phone')}: nothing in {timeout}s")
    return code


async def _audit(bot, p: dict, stage: str, reason: str, receiver: str, price: float):
    sup = await db.get_username(p["tg_id"])
    await _notify(bot, p["tg_id"],
                  sell_audit_text(p, reason, stage, receiver, price, sup))


async def _fail(bot, p: dict, sid, receiver, reason, stage="verification", price=0.0):
    tg_id = p["tg_id"]
    pid = str(p["_id"])
    country = (p.get("country") or "").upper()
    text = sellparse.reason_of(reason)
    if sid:
        await db.close_sale(sid, "failed", text)
    await db.bump_sell_try(pid, SELL_RETRY_DELAY, f"{stage}: {text}")
    fresh = await db.get_purchase(pid) or p
    await _audit(bot, fresh, stage, text, receiver, price)
    await _notify(bot, tg_id, sell_fail_line(country, text))
    await _cancel(receiver)
    return False


async def sell(bot, lzt, purchase: dict, offer: dict, settings: dict) -> bool:
    async with _sem():
        return await _run(bot, lzt, purchase, offer, settings)


async def _handshake(bot, p: dict, conn, receiver: str, s: dict, since: int,
                     sell_price: float) -> tuple[bool, str, str]:
    password = (p.get("password") or s.get("twofa_password") or db.DEFAULT_2FA)

    reply, since = await receiver_hub.wait_new(receiver, since, FIRST_WAIT)
    if not reply:
        reply, since = await receiver_hub.wait_new(receiver, since, SELL_REPLY_TIMEOUT)
    if not reply:
        return False, "no receiver reply", "handover"

    sent: set[str] = set()
    stage = "handover"

    for _ in range(MAX_STEPS):
        if sellparse.accepted(reply):
            return True, reply, stage

        needs_code = sellparse.wants_code(reply) or sellparse.code_wrong(reply)
        if needs_code:
            if len(sent) >= MAX_CODE_SENDS:
                return False, reply, "otp"
            code = await _wait_otp(conn, p, receiver, SELL_OTP_TIMEOUT, since, sent)
            if not code:
                return False, "no otp received", "otp"
            sent.add(code)
            await receiver_hub.send(receiver, code)
            stage = "otp"
        elif sellparse.wants_password(reply) and password:
            log.info(f"password requested {p.get('phone')}")
            await receiver_hub.send(receiver, password)
            stage = "password"
        elif sellparse.rejected(reply):
            return False, reply, stage

        reply, since = await receiver_hub.wait_new(receiver, since, SELL_REPLY_TIMEOUT)
        if not reply:
            return False, "no reply after handover", stage

    return False, reply or "no verdict", stage


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
            return await _fail(bot, p, "", "", "no phone", "precheck", sell_price)
        if not p.get("session_string"):
            return await _fail(bot, p, "", "", "no session", "precheck", sell_price)
        if not receiver_hub.running():
            await receiver_hub.start()
        if not receiver_hub.running():
            return await _fail(bot, p, "", "", "userbot offline", "precheck", sell_price)

        live, devices = await _precheck(p["session_string"], proxy)
        if not live:
            await db.mark_dead(pid, "session revoked")
            await _audit(bot, p, "precheck", "session revoked", receiver, sell_price)
            await _notify(bot, tg_id, sell_fail_line(country, "session revoked"))
            return False

        conn = clean_client(p["session_string"], proxy)
        try:
            await conn.connect()
            if not await conn.is_user_authorized():
                await db.mark_dead(pid, "session revoked")
                await _audit(bot, p, "precheck", "session revoked", receiver, sell_price)
                await _notify(bot, tg_id, sell_fail_line(country, "session revoked"))
                return False

            await reset_others(conn)
            twofa_off = await remove_2fa(
                conn, p.get("password"), s.get("twofa_password"), db.DEFAULT_2FA)
            log.info(f"2fa off {phone}: {twofa_off}")
            if twofa_off:
                await db.set_purchase(pid, password="")

            entity = await _service_entity(conn)
            since = await _baseline(conn, entity)

            if not await receiver_hub.tail_id(receiver):
                await receiver_hub.start_bot(receiver)
                await asyncio.sleep(2.5)

            sid = await db.add_sale(tg_id, pid, country, offer.get("receiver_id"),
                                    receiver, buy_price, sell_price, 0)

            since = await receiver_hub.tail_id(receiver)
            if not await receiver_hub.send(receiver, phone):
                await receiver_hub.start_bot(receiver)
                await asyncio.sleep(2.5)
                since = await receiver_hub.tail_id(receiver)
                if not await receiver_hub.send(receiver, phone):
                    return await _fail(bot, p, sid, receiver, "receiver dm failed",
                                       "handover", sell_price)

            ok, reply, stage = await _handshake(bot, p, conn, receiver, s, since,
                                                sell_price)
            if not ok:
                return await _fail(bot, p, sid, receiver, reply, stage, sell_price)

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

            verdict, since = await receiver_hub.wait_new(receiver, since, SELL_LOGOUT_DELAY)
            if verdict and sellparse.rejected(verdict) and not sellparse.accepted(verdict):
                return await _fail(bot, p, sid, receiver, verdict, "verification",
                                   sell_price)
            if not sellparse.accepted(f"{reply}\n{verdict or ''}"):
                return await _fail(bot, p, sid, receiver, "verification not confirmed",
                                   "verification", sell_price)

            try:
                await conn.log_out()
            except Exception as e:
                log.info(f"self logout {phone}: {e}")
            await db.set_purchase(pid, session_state="dead")
            await db.close_purchase(pid, "ready", "sold to " + receiver)

            tail, _since = await receiver_hub.wait_new(receiver, since, SELL_VERDICT_WAIT)
            if tail and sellparse.rejected(tail) and not sellparse.accepted(tail):
                return await _fail(bot, p, sid, receiver, tail, "payout", sell_price)

            await db.mark_sold(pid, sell_price, tg_id)
            await db.close_sale(sid, "sold", (tail or verdict or "reconciled")[:200])
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


def floor_for(purchase: dict, settings: dict) -> float:
    buy = float(purchase.get("price") or 0.0)
    mult = pricing.profit_mult(settings)
    return max(db.num(settings.get("sell_min_price")), buy * mult)


async def try_one(bot, lzt, purchase: dict) -> bool:
    s = await db.get_settings(purchase["tg_id"])
    if s.get("auto_sell") != "1":
        return False
    if purchase.get("session_state") == "dead":
        await db.mark_dead(purchase["_id"], "session revoked")
        return False
    cc = (purchase.get("country") or "").upper()
    if not cc:
        await db.set_sell(purchase["_id"], "no_country", 0, "country unknown")
        return False

    need = floor_for(purchase, s)
    offer = await pricing.best_offer(purchase["tg_id"], cc, need)
    if offer is None:
        best = await pricing.best_offer(purchase["tg_id"], cc, 0.0)
        if best is None:
            await db.set_sell(purchase["_id"], "no_offer", 0, f"no live offer for {cc}")
            return False
        mult = pricing.profit_mult(s)
        await db.set_sell(purchase["_id"], "no_profit", 0,
                          f"best ${best['price']:.2f} < need ${need:.2f} (x{mult:g})")
        return False
    return await sell(bot, lzt, purchase, offer, s)


def schedule(bot, lzt, purchase: dict):
    _spawn(try_one(bot, lzt, purchase))


async def manual(bot, lzt, purchase: dict) -> tuple[bool, str]:
    s = await db.get_settings(purchase["tg_id"])
    cc = (purchase.get("country") or "").upper()
    if not cc:
        return False, "country unknown"
    need = floor_for(purchase, s)
    offer = await pricing.best_offer(purchase["tg_id"], cc, need)
    if offer is None:
        offer = await pricing.best_offer(purchase["tg_id"], cc, 0.0)
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
                    await asyncio.sleep(0.5)
        except Exception as e:
            log.exception(f"worker: {e}")
        await asyncio.sleep(SELL_QUEUE_TICK)


def offer_label(offer: dict) -> str:
    return (f"{countries.flag_of(offer.get('country', ''))}"
            f" @{offer.get('receiver')} ${offer.get('price', 0):.2f}")