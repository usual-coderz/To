import asyncio
import logging
import time
from collections import Counter

from app import db
from app.config import (EXPIRY_RETRY, EXPIRY_RETRY_DELAY, LOGIN_CONCURRENCY, MAX_SCAN,
                        QUEUE_TARGET_HOURS, SESSION_RETRY, TICK)
from app.lzt import LztError, classify
from app.mail_engine import IMAPMailClient, TempMailClient
from app.session_manager import (change_email, check_session, client, device_list,
                                 login_with_lzt, normalize_phone, owned_counts,
                                 reset_others, set_2fa, spambot_status)
from app.utils import tg
from app.utils import wallet as wallet_view
from app.utils.filters import (account_created_ts, age_for_queue, country_of, passes,
                               phone_of, spam_state)
from app.utils.money import fmt_money, pick_for_buy
from app.utils.texts import audit_text, bought_line, failed_line, session_file

log = logging.getLogger("engine")

_BAL_WARNED: dict[int, float] = {}
WARN_COOLDOWN = 900.0

_BG: set[asyncio.Task] = set()
_NEXT_RUN: dict[int, float] = {}
_SEM: asyncio.Semaphore | None = None

PHONE_KEYS = ("login", "telegram_login", "phone", "telegram_phone")

LOGIN_TRIES = 3

CHECK_SOFT = ("no_balance",)


class SessionDead(RuntimeError):
    pass


def _sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(LOGIN_CONCURRENCY)
    return _SEM


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return task


async def drain_bg(timeout: float = 8.0):
    if not _BG:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*list(_BG), return_exceptions=True), timeout)
    except asyncio.TimeoutError:
        log.info(f"shutdown: {len(_BG)} login task(s) still running")


async def _notify(bot, tg_id, text):
    await tg.send(bot, tg_id, text)


async def _balance_warn(bot, tg_id, text):
    if time.time() - _BAL_WARNED.get(tg_id, 0.0) < WARN_COOLDOWN:
        return
    _BAL_WARNED[tg_id] = time.time()
    await _notify(bot, tg_id, text)


def phone_of_item(item: dict) -> str:
    for key in PHONE_KEYS:
        norm = normalize_phone(item.get(key) or "")
        if norm:
            return norm
    data = item.get("loginData")
    if isinstance(data, dict):
        norm = normalize_phone(data.get("login") or "")
        if norm:
            return norm
    return normalize_phone(phone_of(item))


def password_of_item(item: dict) -> str:
    data = item.get("loginData")
    if isinstance(data, dict) and data.get("password"):
        return str(data["password"])
    for key in ("telegram_password", "password"):
        if item.get(key):
            return str(item[key])
    return ""


async def _resolve_login(lzt, item_id: int, fresh: dict, res: dict) -> tuple[str, str]:
    bought = res.get("item") or {}
    phone = phone_of_item(bought) or phone_of_item(fresh)
    password = password_of_item(bought) or password_of_item(fresh)
    if phone and password:
        return phone, password
    try:
        full = await lzt.item(item_id)
    except LztError as e:
        log.info(f"item {item_id}: {e}")
        return phone, password
    return phone or phone_of_item(full), password or password_of_item(full)


async def _fast_buy(lzt, item_id: int, price: float, balance_id: int):
    if balance_id:
        try:
            return await lzt.fast_buy(item_id, price, balance_id=balance_id)
        except LztError as e:
            if "balance_id" not in str(e).lower() and "balance id" not in str(e).lower():
                raise
            log.info(f"buy {item_id}: balance id rejected ({e}), retrying default")
    return await lzt.fast_buy(item_id, price)


async def buy_one(bot, lzt, tg_id: int, item: dict, s: dict, snap: dict) -> tuple[bool, str]:
    item_id = item["item_id"]
    search_price = db.num(item.get("price"))
    currency = s["currency"]

    checked = {}
    try:
        checked = await lzt.check_account(item_id)
    except LztError as e:
        why = classify(e)
        log.info(f"check {item_id}: {why}: {e}")
        if why not in CHECK_SOFT:
            return False, why

    fresh = dict(item)
    fresh.update({k: v for k, v in (checked.get("item") or {}).items() if v not in (None, "")})

    price = db.num(fresh.get("price"), search_price)
    ok, why = passes(fresh, s, authoritative=True)
    if not ok:
        log.info(f"skip {item_id}: {why}")
        return False, why

    soft = ""
    if s.get("spam_mode") == "no" and s.get("verify_spam", "1") == "1":
        if spam_state(fresh) is True:
            soft = "spam_flag"
            log.info(f"note {item_id}: spam flag set but server filter says clean")
    if why == "ok_unknown":
        log.info(f"note {item_id}: fields unknown, relying on server filters")

    wallet = pick_for_buy(snap["entries"], currency, price)
    if wallet is None:
        snap.update(await wallet_view.snapshot(lzt, currency))
        wallet = pick_for_buy(snap["entries"], currency, price)
    if wallet is None:
        await _balance_warn(bot, tg_id,
                            f"💳 low balance {fmt_money(snap['total'], currency)}")
        return False, "no_balance"

    try:
        res = await _fast_buy(lzt, item_id, price, int(wallet.get("balance_id") or 0))
    except LztError as e:
        why = classify(e)
        log.info(f"buy {item_id}: {why}: {e}")
        return False, why

    try:
        snap.update(await wallet_view.snapshot(lzt, currency))
    except Exception:
        pass

    phone, password = await _resolve_login(lzt, item_id, fresh, res)
    country = country_of(fresh)
    age = age_for_queue(fresh)
    created = account_created_ts(fresh) or 0
    target = db.num(s.get("queue_target_hours"), QUEUE_TARGET_HOURS) or QUEUE_TARGET_HOURS
    due = db.due_from(age, target)

    pid = await db.add_purchase(
        tg_id, item_id, phone, country, price, password=password,
        expires_at=due, target_age_hours=target, age_at_buy_hours=age,
        account_created_ts=created, spam_flag=(soft == "spam_flag"), note="bought")

    await _notify(bot, tg_id, bought_line(country))

    if not phone:
        log.info(f"buy {item_id}: no phone on item")
        await db.close_purchase(pid, "login_failed", "no phone number on item")
        await _notify(bot, tg_id, failed_line(country, "no phone on item"))
        return True, "no_phone"

    log.info(f"bought {item_id} pid={pid} age={age:.2f}h due_in={due - time.time():.0f}s")
    _spawn(_login_task(bot, lzt, tg_id, pid, item_id, phone, password, country, s, fresh))
    return True, (soft or "bought")


async def _login_task(bot, lzt, tg_id, pid, item_id, phone, password, country, s, hint):
    async with _sem():
        await db.set_purchase(pid, password=password)
        last: Exception | None = None
        conn = None
        session_str = ""

        for attempt in range(LOGIN_TRIES):
            try:
                conn, session_str, _uid = await login_with_lzt(
                    phone, password, item_id, lzt, s.get("proxy", ""), hint)
                if not session_str:
                    raise RuntimeError("empty session string")
                break
            except Exception as e:
                last = e
                log.info(f"login {item_id} attempt {attempt + 1}/{LOGIN_TRIES}: {e}")
                if attempt + 1 < LOGIN_TRIES:
                    await asyncio.sleep(15 + attempt * 15)
        else:
            await db.close_purchase(pid, "login_failed", str(last)[:200])
            await _notify(bot, tg_id, failed_line(country, str(last)))
            return

        await db.set_session(pid, session_str)
        try:
            if conn is not None:
                await conn.disconnect()
        except Exception:
            pass

        p = await db.get_purchase(pid)
        if not p:
            return
        if int(p.get("expires_at") or 0) <= time.time():
            await clean_purchase(bot, lzt, p)
        else:
            log.info(f"queued {item_id} for {int(p['expires_at']) - int(time.time())}s")


async def _mail_client_for(p: dict, s: dict):
    current = (p.get("mail") or "").split("@")[-1].lower()
    provider = s.get("mail_provider", "auto")
    if provider in ("auto", "native") and p.get("mail") and p.get("mail_password"):
        try:
            return IMAPMailClient(p["mail"], p["mail_password"]), p["mail"]
        except Exception as e:
            log.info(f"imap unavailable: {e}")
    if provider == "native":
        return None, ""
    mc = TempMailClient()
    try:
        addr = await mc.create_temp_email(exclude_domains=[current] if current else None)
    except Exception as e:
        log.info(f"temp mail: {e}")
        await mc.close()
        return None, ""
    return mc, addr


async def _send_audit(bot, tg_id, p, data):
    sup = await db.get_username(tg_id)
    await tg.send(bot, tg_id, audit_text(p, data, sup))
    name = (p.get("phone") or str(p["item_id"])).lstrip("+") or "session"
    await tg.send_document(bot, tg_id, session_file(p).encode(), f"{name}.py")


async def _fail(bot, p, note: list[str]):
    await db.close_purchase(p["_id"], "failed", "; ".join(note)[:300])
    await _notify(bot, p["tg_id"],
                  failed_line(p.get("country"), "; ".join(note)[:200]))


async def clean_purchase(bot, lzt, p: dict):
    pid = p["_id"]
    if not await db.claim_purchase(pid):
        return

    s = await db.get_settings(p["tg_id"])
    tries = int(p.get("expiry_tries") or 0)

    if not p.get("session_string"):
        if tries < EXPIRY_RETRY:
            await db.release_purchase(pid, EXPIRY_RETRY_DELAY, inc_tries=True)
            return
        await db.close_purchase(pid, "failed", "no session")
        await _notify(bot, p["tg_id"], failed_line(p.get("country"), "no session"))
        return

    data: dict = {"mail_ok": False, "twofa_ok": False}
    note: list[str] = []
    conn = client(p["session_string"], s.get("proxy", ""))
    try:
        await conn.connect()
        if not await conn.is_user_authorized():
            raise SessionDead("session dead")

        mc, addr = await _mail_client_for(p, s)
        try:
            if addr and s.get("change_mail_on_expire") == "1":
                try:
                    await change_email(conn, addr, mc)
                    data["mail_ok"] = True
                    data["mail"] = addr
                    note.append("mail changed")
                except Exception as e:
                    note.append(f"mail fail: {e}")
        finally:
            if mc is not None and hasattr(mc, "close"):
                await mc.close()

        password = s.get("twofa_password") or db.DEFAULT_2FA
        try:
            await set_2fa(conn, password, p.get("password") or "")
            data["twofa_ok"] = True
            data["password"] = password
            note.append("2fa set")
        except Exception as e:
            note.append(f"2fa fail: {e}")

        if s.get("terminate_on_expire") == "1":
            killed = await reset_others(conn)
            note.append(f"devices reset {killed}")

        data["spam"] = await spambot_status(conn)
        data["devices"] = await device_list(conn)
        data["owned"] = await owned_counts(conn)
    except SessionDead as e:
        note.append(str(e))
        if tries < EXPIRY_RETRY:
            await db.release_purchase(pid, EXPIRY_RETRY_DELAY, inc_tries=True)
            return
        await _fail(bot, p, note)
        return
    except Exception as e:
        note.append(f"clean err: {e}")
        if tries < SESSION_RETRY:
            await db.release_purchase(pid, EXPIRY_RETRY_DELAY, inc_tries=True)
            return
        await _fail(bot, p, note)
        return
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass

    session_str = p["session_string"]
    if s.get("terminate_on_expire") == "1":
        try:
            await lzt.reset_authorizations(p["item_id"])
            note.append("all sessions reset")
        except LztError as e:
            note.append(f"reset: {e}")

        alive, _me = await check_session(session_str, s.get("proxy", ""))
        if not alive:
            try:
                _c, session_str, _uid = await login_with_lzt(
                    p.get("phone"), p.get("password") or "", p["item_id"], lzt,
                    s.get("proxy", ""))
                await db.set_session(pid, session_str)
                try:
                    await _c.disconnect()
                except Exception:
                    pass
                note.append("session reissued")
            except Exception as e:
                note.append(f"reissue fail: {e}")

    if not note:
        note.append("cleaned")
    new_mail = data.get("mail", "") if data.get("mail_ok") else ""
    await db.close_purchase(pid, "ready", "; ".join(note)[:300], new_mail=new_mail)
    fresh = await db.get_purchase(pid)
    if fresh:
        fresh["password"] = (data.get("password") if data.get("twofa_ok")
                             else (p.get("password") or ""))
        await _send_audit(bot, p["tg_id"], fresh, data)


async def _cycle(bot, lzt, tg_id: int, s: dict):
    if s.get("auto_buy") != "1":
        await db.save_report(tg_id, note="auto-buy off")
        return

    cap = db.num(s.get("daily_spend_cap"))
    if cap > 0:
        spent = await db.spent_today(tg_id)
        if spent >= cap:
            await db.save_report(tg_id, note=f"daily cap hit {spent:.2f}/{cap:.2f}")
            return

    currency = s["currency"]
    floor_price = db.num(s.get("price_min"))

    snap = await wallet_view.snapshot(lzt, currency)
    if snap["spendable"] < floor_price:
        await _balance_warn(bot, tg_id,
                            f"💳 low balance {fmt_money(snap['total'], currency)}")
        note = f"low spendable {snap['spendable']:.2f}/{floor_price:.2f}"
        if snap["error"]:
            note += f" · {snap['error'][:80]}"
        await db.save_report(tg_id, note=note)
        return

    try:
        items = await lzt.search(s)
    except LztError as e:
        await db.save_report(tg_id, error=f"search: {e}")
        return
    if not items:
        await db.save_report(tg_id, note="search returned nothing")
        return

    reasons: Counter = Counter()
    bought = 0
    seen: set[int] = set()
    max_buys = max(1, db.intval(s.get("max_buy_per_cycle"), 2))

    for it in items[:MAX_SCAN]:
        item_id = it.get("item_id")
        if item_id in seen:
            continue
        seen.add(item_id)
        ok, why = passes(it, s)
        if not ok:
            reasons[why] += 1
            continue
        try:
            done, why = await buy_one(bot, lzt, tg_id, it, s, snap)
        except Exception as e:
            log.exception(f"buy_one {item_id}: {e}")
            reasons["error"] += 1
            continue
        reasons[why] += 1
        if done:
            bought += 1
        if bought >= max_buys:
            break
        if why == "check_limit":
            break
        if cap > 0 and await db.spent_today(tg_id) >= cap:
            reasons["cap_hit"] += 1
            break

    log.info(f"[{tg_id}] scanned={len(items)} bought={bought} "
             f"rejects={dict(reasons.most_common(5))}")
    await db.save_report(tg_id, scanned=len(items), bought=bought, reasons=dict(reasons))


async def buy_loop(bot, lzt):
    while True:
        try:
            now = time.time()
            owners = await db.all_owners()
            live_ids = {u["_id"] for u in owners}
            for stale in [k for k in _NEXT_RUN if k not in live_ids]:
                _NEXT_RUN.pop(stale, None)
            for stale in [k for k in _BAL_WARNED if k not in live_ids]:
                _BAL_WARNED.pop(stale, None)
            for tg_id in live_ids:
                if _NEXT_RUN.get(tg_id, 0.0) > now:
                    continue
                try:
                    s = await db.get_settings(tg_id)
                except Exception as e:
                    log.warning(f"settings {tg_id}: {e}")
                    _NEXT_RUN[tg_id] = now + TICK
                    continue
                if s["auto_buy"] != "1":
                    _NEXT_RUN[tg_id] = now + TICK
                    continue
                _NEXT_RUN[tg_id] = now + max(5, db.intval(s.get("poll_interval"), 25))
                try:
                    await _cycle(bot, lzt, tg_id, s)
                except Exception as e:
                    log.exception(f"cycle {tg_id}: {e}")
                    await db.save_report(tg_id, error=f"cycle: {e}")
        except Exception as e:
            log.exception(f"buy_loop: {e}")
        await asyncio.sleep(TICK)


async def cleaner_loop(bot, lzt):
    while True:
        try:
            for p in await db.due_purchases():
                try:
                    await clean_purchase(bot, lzt, p)
                except Exception as e:
                    log.exception(f"clean {p.get('_id')}: {e}")
        except Exception as e:
            log.exception(f"cleaner_loop: {e}")
        await asyncio.sleep(20)