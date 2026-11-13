import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import db, pricing, receiver_hub
from app.config import CHART_TTL, INR_RATE, SCHEDULE_HOURS, SCHEDULE_TZ
from app.utils import tg
from app.utils.texts import chart_csv, chart_text

log = logging.getLogger("scheduler")

_TZ = ZoneInfo(SCHEDULE_TZ)
_RUN: dict[tuple[int, str], int] = {}


def _slot(now: datetime) -> tuple[str, datetime]:
    best = None
    for hour in SCHEDULE_HOURS:
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        if best is None or target < best[1]:
            best = (f"{hour:02d}", target)
    return best


async def _matrix(tg_id: int) -> tuple[list[dict], dict]:
    if not receiver_hub.running():
        await receiver_hub.start()
    out = await pricing.refresh_all(tg_id)
    rows = await pricing.price_matrix(tg_id, CHART_TTL)
    if rows:
        return rows, out
    await asyncio.sleep(5.0)
    out = await pricing.refresh_all(tg_id)
    return await pricing.price_matrix(tg_id, CHART_TTL), out


async def _build(bot, tg_id: int):
    rows, diag = await _matrix(tg_id)
    if not rows:
        hint = "\n".join(diag.get("empty") or []) or "no receivers enabled"
        body = ("<b>Price chart</b>\nno data\n\n"
                f"receivers {diag.get('receivers', 0)} · prices {diag.get('prices', 0)}"
                f" · errors {diag.get('errors', 0)}\n{hint}\n\n"
                "check /hub, then /receivers → Warm all, phir Refresh")
        await tg.send(bot, tg_id, body)
        return
    await db.save_chart(tg_id, [
        {"country": r["country"], "name": r["name"], "dial": r.get("dial", ""),
         "price": (r.get("best") or {}).get("price", 0.0),
         "receiver": (r.get("best") or {}).get("receiver", "")} for r in rows])
    await tg.send(bot, tg_id, chart_text(rows, INR_RATE))
    await tg.send_document(bot, tg_id, chart_csv(rows).encode(),
                           f"prices_{datetime.now(_TZ).strftime('%Y%m%d_%H%M')}.csv")


async def run_now(bot, tg_id: int):
    try:
        await _build(bot, tg_id)
    except Exception as e:
        log.exception(f"chart {tg_id}: {e}")
        await tg.send(bot, tg_id, f"⚠️ chart failed: {tg.esc(str(e)[:150])}")


async def loop(bot):
    while True:
        try:
            now = datetime.now(_TZ)
            day = now.strftime("%Y-%m-%d")
            for owner in await db.all_owners():
                tg_id = owner["_id"]
                stamp, target = _slot(now)
                key = (tg_id, f"{day}-{stamp}")
                if _RUN.get(key):
                    continue
                if (target - now).total_seconds() > 40:
                    continue
                _RUN[key] = int(time.time())
                asyncio.create_task(run_now(bot, tg_id))
            for key in [k for k, ts in _RUN.items() if time.time() - ts > 172_800]:
                _RUN.pop(key, None)
        except Exception as e:
            log.exception(f"scheduler: {e}")
        await asyncio.sleep(20)