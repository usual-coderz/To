import asyncio
import logging
import time
from collections import Counter

from app import db, keyboards as kb
from app.config import (EXPIRY_RETRY, EXPIRY_RETRY_DELAY, LOGIN_CONCURRENCY,
                        MAX_SCAN, QUEUE_TARGET_HOURS, TICK)
from app.lzt import LztError
from app.mail_engine import IMAPMailClient, TempMailClient
from app.session_manager import (change_email, client, device_list, login_with_lzt,
                                owned_counts, reset_others, set_2fa, spambot_status)
from app.utils import tg
from app.utils import wallet as wallet_view
from app.utils.filters import (account_created_ts, age_hours_of, country_of, passes,
                              phone_of, spam_state)
from app.utils.money import fmt_money, pick_for_buy
from app.utils.texts import audit_text, bought_text, session_file

log = logging.getLogger("engine")

_BAL_WARNED: dict[int, float] = {}
WARN_COOLDOWN = 900.0

_BG: set[asyncio.Task] = set()
_NEXT_RUN: dict[int, float] = {}
_SEM: asyncio.Semaphore | None = None


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


async def _notify_card(bot, tg_id, p: dict, stage: str):
    markup = kb.bought(p) if p.get("_id") else None
    ok = await tg.send(bot, tg_id, bought_text(p, stage), markup)
    if not ok:
        log.warning(f"card {stage} not delivered tg={tg_id} pid={p.get('_id')}")


async def _balance_warn(bot, tg_id, text):
    if time.time() - _BAL_WARNED.get(tg_id, 0.0) < WARN_COOLDOWN:
        return
    _BAL_WARNED[tg_id] = time.time()
    await _notify(bot, tg_id, text)


async def _fast_buy(lzt, item_id: int, price: float, balance_id: int):
    if balance_id:
        try:
            return await lzt.fast_buy(item_id, price, balance_id=balance_id)
        except TypeError:
            pass
        except LztError as e:
            if "balance" not in str(e).lower():
                raise
            log.info(f"buy {item_id}: balance id rejected ({e}), retrying default")
    return await lzt.fast_buy(item_id, price)


def _due_at(created: int, age_hours: float, target: float) -> int:
    now = time.time()
    if created:
        return max(int(now), created + int(target * 3600))
    return int(now + max(0.0, target - age_hours) * 3600)


async def buy_one(bot, lzt, tg_id: int, item: dict, s: dict, snap: dict) -> tuple[bool, str]:
    item_id = item["item_id"]
    search_price = db.num(item.get("price"))
    currency = s["currency"]

    try:
        chk = await lzt.check_account(item_id)
    except LztError as e:
        log.info(f"check {item_id}: {e}")
        return False, "check_failed"

    fresh = dict(item)
    fresh.update({k: v for k, v in (chk.get("item") or {}).items() if v not in (None, "")})

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
                            f"💳 Low balance {fmt_money(snap['total'], currency)}"
                            f" for {fmt_money(price, currency)}")
        return False, "no_balance"

    try:
        res = await _fast_buy(lzt, item_id, price, int(wallet.get("balance_id") or 0))
    except LztError as e:
        log.info(f"buy {item_id}: {e}")
        return False, "buy_failed"

    try:
        snap.update(await wallet_view.snapshot(lzt, currency))
    except Exception:
        pass

    login_data = (res.get("item") or {}).get("loginData") or {}
    phone = login_data.get("login") or phone_of(fresh) or phone_of(item)
    password = login_data.get("password") or fresh.get("telegram_password") or ""

    target = db.num(s.get("queue_target_hours"), QUEUE_TARGET_HOURS) or QUEUE_TARGET_HOURS
    created = account_created_ts(fresh) or 0
    age = age_hours_of(fresh) or 0.0
    due = _due_at(created, age, target)

    pid = await db.add_purchase(
        tg_id, item_id, phone, country_of(fresh), price,
        expires_at=due, target_age_hours=target, age_at_buy_hours=age,
        account_created_ts=created, spam_flag=(soft == "spam_flag"), note="bought")

    purchase = await db.get_purchase(pid)
    if purchase:
        purchase["password"] = password
        await _notify_card(bot, tg_id, purchase, "bought")

    _spawn(_login_task(bot, lzt, tg_id, pid, item_id, phone, password, s))
    return True, (soft or "bought")


async def _login_task(bot, lzt, tg_id, pid, item_id, phone, password, s):
    async with _sem():
        try:
            c, session_str, _uid = await login_with_lzt(
                phone, password, item_id, lzt, s.get("proxy", ""))
        except Exception as e:
            log.info(f"login {item_id}: {e}")
            await db.close_purchase(pid, "login_failed", str(e)[:200])
            p = await db.get_purchase(pid)
            if p:
                p["password"] = password
                await _notify_card(bot, tg_id, p, "failed")
            return

        await db.set_session(pid, session_str)
        try:
            await c.disconnect()
        except Exception:
            pass

        p = await db.get_purchase(pid)
        if not p:
            return
        p["password"] = password
        if int(p.get("expires_at") or 0) <= time.time():
            await clean_purchase(bot, lzt, p)
        else:
            await _notify_card(bot, tg_id, p, "queued")


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
    return mc, await mc.create_temp_email(exclude_domains=[current] if current else None)


async def _send_audit(bot, tg_id, p, data):
    sup = await db.get_username(tg_id)
    await tg.send(bot, tg_id, audit_text(p, data, sup), kb.audit(p))
    name = (p.get("phone") or str(p["item_id"])).lstrip("+") or "session"
    await tg.send_document(bot, tg_id, session_file(p).encode(), f"{name}.py")


async def clean_purchase(bot, lzt, p: dict):
    s = await db.get_settings(p["tg_id"])
    tries = int(p.get("expiry_tries") or 0)
    phone = p.get("phone") or p.get("item_id")

    if not p.get("session_string"):
        if tries < EXPIRY_RETRY:
            await db.bump_expiry(p["_id"], EXPIRY_RETRY_DELAY, inc_tries=True)
            return
        await db.close_purchase(p["_id"], "failed", "no session")
        await _notify(bot, p["tg_id"],
                      f"⚠️ <b>Clean failed</b> <code>{tg.esc(phone)}</code>\nno session")
        return

    data: dict = {}
    note: list[str] = []
    c = client(p["session_string"], s.get("proxy", ""))
    try:
        await c.connect()
        if not await c.is_user_authorized():
            raise RuntimeError("session dead")

        mc, addr = await _mail_client_for(p, s)
        if mc is None:
            addr = ""
        password = s.get("twofa_password") or db.DEFAULT_2FA

        killed = await reset_others(c)
        note.append(f"devices reset {killed}")

        try:
            await set_2fa(c, password, p.get("password") or "")
            note.append("2fa set")
        except Exception as e:
            note.append(f"2fa fail: {e}")

        if addr and s.get("change_mail_on_expire") == "1":
            try:
                await change_email(c, addr, mc)
                note.append("mail changed")
            except Exception as e:
                note.append(f"mail fail: {e}")

        data["spam"] = await spambot_status(c)
        data["devices"] = await device_list(c)
        data["owned"] = await owned_counts(c)
        data["mail"] = addr
        data["password"] = password
    except Exception as e:
        note.append(f"clean err: {e}")
        if tries < 3:
            await db.bump_expiry(p["_id"], EXPIRY_RETRY_DELAY, inc_tries=True)
            return
        await db.close_purchase(p["_id"], "failed", "; ".join(note)[:300])
        await _notify(bot, p["tg_id"],
                      f"⚠️ <b>Clean failed</b> <code>{tg.esc(phone)}</code>\n"
                      f"{tg.esc('; '.join(note)[:250])}")
        return
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass

    try:
        await lzt.reset_authorizations(p["item_id"])
        note.append("lzt auth reset")
    except LztError as e:
        note.append(f"reset: {e}")

    await db.close_purchase(p["_id"], "ready", "; ".join(note)[:300],
                            new_mail=data.get("mail", ""))
    fresh = await db.get_purchase(p["_id"])
    if fresh:
        fresh["password"] = data.get("password", "")
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
                            f"💳 Low balance {fmt_money(snap['total'], currency)}"
                            f" · spendable {fmt_money(snap['spendable'], currency)}")
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
    max_buys = max(1, db.intval(s.get("max_buy_per_cycle"), 2))

    for it in items[:MAX_SCAN]:
        ok, why = passes(it, s)
        if not ok:
            reasons[why] += 1
            continue
        try:
            done, why = await buy_one(bot, lzt, tg_id, it, s, snap)
        except Exception as e:
            log.exception(f"buy_one {it.get('item_id')}: {e}")
            reasons["error"] += 1
            continue
        if done:
            bought += 1
            if why in ("spam_flag", "ok_unknown"):
                reasons[why] += 1
        else:
            reasons[why] += 1
        if bought >= max_buys:
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