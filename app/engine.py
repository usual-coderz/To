import asyncio
import logging
import time
from collections import Counter

from app import db, keyboards as kb
from app.config import (EXPIRY_LOGIN_GRACE, EXPIRY_RETRY, EXPIRY_RETRY_DELAY,
                        LOGIN_CONCURRENCY, MAX_SCAN, TICK)
from app.lzt import LztError
from app.mail_engine import IMAPMailClient, TempMailClient
from app.session_manager import change_email, client, login_with_lzt, terminate
from app.utils import tg
from app.utils import wallet as wallet_view
from app.utils.filters import (
    account_created_ts, age_hours_of, clean_delay_hours, country_of, passes, phone_of,
)
from app.utils.money import fmt_money, pick_for_buy
from app.utils.texts import bought_text, session_file

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
    """Purchase card. The ready stage also ships the session as a file."""
    markup = kb.bought(p) if p.get("_id") else None
    ok = await tg.send(bot, tg_id, bought_text(p, stage), markup)
    if not ok:
        log.warning(f"card {stage} not delivered tg={tg_id} pid={p.get('_id')}")
        return
    if stage == "ready" and p.get("session_string"):
        name = (p.get("phone") or str(p.get("item_id"))).lstrip("+") or "session"
        await tg.send_document(bot, tg_id, session_file(p).encode(), f"{name}.py")


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
        from app.utils.filters import spam_state
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

    # balances changed — refresh for the next buy in this cycle
    try:
        snap.update(await wallet_view.snapshot(lzt, currency))
    except Exception:
        pass

    login_data = (res.get("item") or {}).get("loginData") or {}
    phone = login_data.get("login") or phone_of(fresh) or phone_of(item)
    password = login_data.get("password") or fresh.get("telegram_password") or ""

    target = db.num(s.get("session_life_hours"), 24.0) or 24.0
    created = account_created_ts(fresh) or 0
    age = age_hours_of(fresh) or 0.0
    delay = clean_delay_hours(fresh, target, s.get("early_clean_if_aged") == "1")
    expires = int(time.time() + delay * 3600)

    pid = await db.add_purchase(
        tg_id, item_id, phone, country_of(fresh), price,
        expires_at=expires, target_age_hours=target, age_at_buy_hours=age,
        account_created_ts=created, spam_flag=(soft == "spam_flag"), note="bought")

    purchase = await db.get_purchase(pid)
    if purchase:
        purchase["password"] = password
        await _notify_card(bot, tg_id, purchase, "bought")

    _spawn(_login_task(bot, lzt, tg_id, pid, item_id, phone, password, s, delay))
    return True, (soft or "bought")


async def _login_task(bot, lzt, tg_id, pid, item_id, phone, password, s, delay):
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
        p = await db.get_purchase(pid)
        if p:
            p["password"] = password
            await _notify_card(bot, tg_id, p, "ready")

        try:
            await c.disconnect()
        except Exception:
            pass


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


async def expire_purchase(bot, lzt, p: dict):
    s = await db.get_settings(p["tg_id"])
    tries = int(p.get("expiry_tries") or 0)
    phone = p.get("phone") or p.get("item_id")

    if not p.get("session_string"):
        young = time.time() - (p.get("started_at") or 0) < EXPIRY_LOGIN_GRACE
        if tries < EXPIRY_RETRY and young:
            await db.bump_expiry(p["_id"], EXPIRY_RETRY_DELAY, inc_tries=True)
            return
        await db.close_purchase(p["_id"], "login_failed", "no session string")
        await _notify(bot, p["tg_id"], f"⚠️ <b>No session</b> <code>{tg.esc(phone)}</code>")
        return

    new_mail, note = "", []
    hard_fail = False
    c = client(p["session_string"], s.get("proxy", ""))
    try:
        await c.connect()
        if not await c.is_user_authorized():
            note.append("session dead")
            hard_fail = True
        else:
            if s["change_mail_on_expire"] == "1":
                mc, addr = await _mail_client_for(p, s)
                if mc is None:
                    note.append("mail skipped")
                else:
                    try:
                        await change_email(c, addr, mc)
                        new_mail = addr
                        note.append("mail changed")
                    except Exception as e:
                        note.append(f"mail fail: {e}")
            if s["terminate_on_expire"] == "1":
                await terminate(c)
                note.append("terminated")
    except Exception as e:
        note.append(f"session err: {e}")
        hard_fail = True
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass

    if hard_fail and tries < 3:
        await db.bump_expiry(p["_id"], EXPIRY_RETRY_DELAY, inc_tries=True)
        return

    try:
        await lzt.reset_authorizations(p["item_id"])
        note.append("lzt auth reset")
    except LztError as e:
        note.append(f"reset: {e}")

    joined = "; ".join(note)
    await db.close_purchase(p["_id"], "terminated", joined[:300], new_mail=new_mail)
    await _notify(bot, p["tg_id"],
                  f"🧼 <b>Cleaned</b> <code>{tg.esc(phone)}</code>\n"
                  f"{tg.esc(joined)}\n📧 <code>{tg.esc(new_mail or '-')}</code>")


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


async def expiry_loop(bot, lzt):
    while True:
        try:
            for p in await db.due_purchases():
                try:
                    await expire_purchase(bot, lzt, p)
                except Exception as e:
                    log.exception(f"expire {p.get('_id')}: {e}")
        except Exception as e:
            log.exception(f"expiry_loop: {e}")
        await asyncio.sleep(20)