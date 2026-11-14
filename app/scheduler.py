import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import db, pricing, receiver_hub, receivers
from app.config import CHART_TTL, SCHEDULE_HOURS, SCHEDULE_TZ, WARM_HOURS
from app.utils import tg
from app.utils.texts import caps_text, chart_csv, chart_text

log = logging.getLogger("scheduler")

_TZ = ZoneInfo(SCHEDULE_TZ)
_RUN: dict[tuple[int, str, str], int] = {}


def _next_slot(now: datetime, hours: tuple[int, ...]) -> tuple[str, datetime] | None:
    best = None
    for hour in hours:
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        if best is None or target < best[1]:
            best = (f"{hour:02d}", target)
    return best


async def _chart(bot, tg_id: int):
    try:
        rows = await pricing.price_matrix(tg_id, CHART_TTL)
        if not rows:
            await tg.send(bot, tg_id,
                          "<b>Price chart</b>\nno cached prices yet\n"
                          "warm sweep chalta hai 00:00 aur 16:00 pe")
            return
        s = await db.get_settings(tg_id)
        await db.save_chart(tg_id, [
            {"country": r["country"], "name": r["name"], "dial": r.get("dial", ""),
             "price": (r.get("best") or {}).get("price", 0.0),
             "receiver": (r.get("best") or {}).get("receiver", "")} for r in rows])
        await tg.send(bot, tg_id, chart_text(rows, pricing.profit_mult(s)))
        await tg.send_document(bot, tg_id, chart_csv(rows).encode(),
                               f"prices_{datetime.now(_TZ).strftime('%Y%m%d_%H%M')}.csv")
    except Exception as e:
        log.exception(f"chart {tg_id}: {e}")
        await tg.send(bot, tg_id, f"⚠️ chart failed: {tg.esc(str(e)[:150])}")


async def run_now(bot, tg_id: int):
    await _chart(bot, tg_id)


async def _warm_fetch(bot, tg_id: int):
    try:
        out = await receivers.capacity_sweep(tg_id)
        rows = await db.last_caps(tg_id, 3600)
        text = (f"🔥 <b>warm sweep</b> · {datetime.now(_TZ).strftime('%H:%M')}\n"
                f"bots {out['total']} · answered {out['ok']}"
                f" · missing {out['missing']} · prices {out['prices']}\n\n"
                + caps_text(rows))
        if out["empty"]:
            text += "\n\n" + "\n".join(tg.esc(e) for e in out["empty"])
        await tg.send(bot, tg_id, text)
        await _chart(bot, tg_id)
    except Exception as e:
        log.exception(f"warm sweep {tg_id}: {e}")
        await tg.send(bot, tg_id, f"⚠️ warm sweep failed: {tg.esc(str(e)[:150])}")


async def loop(bot):
    while True:
        try:
            now = datetime.now(_TZ)
            day = now.strftime("%Y-%m-%d")
            for owner in await db.all_owners():
                tg_id = owner["_id"]
                jobs = [("chart", _next_slot(now, SCHEDULE_HOURS), _chart),
                        ("warm", _next_slot(now, WARM_HOURS), _warm_fetch)]
                for kind, slot, fn in jobs:
                    if not slot:
                        continue
                    stamp, target = slot
                    key = (tg_id, kind, f"{day}-{stamp}")
                    if _RUN.get(key):
                        continue
                    if (target - now).total_seconds() > 40:
                        continue
                    _RUN[key] = int(time.time())
                    asyncio.create_task(fn(bot, tg_id))
            for key in [k for k, ts in _RUN.items() if time.time() - ts > 172_800]:
                _RUN.pop(key, None)
        except Exception as e:
            log.exception(f"scheduler: {e}")
        await asyncio.sleep(20)