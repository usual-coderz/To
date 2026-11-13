import asyncio
import logging
import random
import time

from telethon import functions
from telethon.sessions import StringSession

from app.config import API_HASH, API_ID, USERBOT_PROXY, USERBOT_SESSION
from app.session_manager import _proxy

log = logging.getLogger("hub")

_client = None
_lock = asyncio.Lock()
_LAST: dict[str, int] = {}

IDLE_GAP = 2.5
MAX_WAIT = 25.0
MAX_MSGS = 60


def running() -> bool:
    return _client is not None


async def start():
    global _client
    if not USERBOT_SESSION:
        log.warning("USERBOT_SESSION missing, receiver hub disabled")
        return None
    async with _lock:
        if _client is not None:
            return _client
        from telethon import TelegramClient
        client = TelegramClient(
            StringSession(USERBOT_SESSION), API_ID, API_HASH,
            proxy=_proxy(USERBOT_PROXY), connection_retries=3, retry_delay=2,
            request_retries=3, timeout=30, flood_sleep_threshold=90,
            device_model="AccStore", system_version="2.1", app_version="2.1")
        await client.connect()
        if not await client.is_user_authorized():
            log.error("userbot session not authorized")
            await client.disconnect()
            return None
        _client = client
        log.info("receiver hub online")
        return client


async def stop():
    global _client
    async with _lock:
        if _client is None:
            return
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None


def _stamp(key: str) -> int:
    return _LAST.get(key, 0)


def _mark(key: str):
    _LAST[key] = int(time.time() * 1000)


async def _resolve(username: str):
    client = _client
    if client is None:
        return None
    try:
        return await client.get_entity("@" + str(username).lstrip("@"))
    except Exception as e:
        log.info(f"resolve {username}: {e}")
        return None


async def send(username: str, text: str) -> bool:
    client = _client
    if client is None:
        return False
    entity = await _resolve(username)
    if entity is None:
        return False
    try:
        await client.send_message(entity, text)
        return True
    except Exception as e:
        log.info(f"send {username}: {e}")
        return False


async def drain(username: str) -> int:
    client = _client
    if client is None:
        return 0
    entity = await _resolve(username)
    if entity is None:
        return 0
    try:
        msgs = await client.get_messages(entity, limit=1)
    except Exception:
        return _stamp(username)
    if msgs:
        _mark(username)
    return _stamp(username)


async def collect(username: str, since: int | None = None, idle: float = IDLE_GAP,
                  max_wait: float = MAX_WAIT, max_msgs: int = MAX_MSGS) -> str:
    client = _client
    if client is None:
        return ""
    entity = await _resolve(username)
    if entity is None:
        return ""
    floor = _stamp(username) if since is None else since
    parts: list[str] = []
    seen: set[int] = set()

    try:
        tail = await client.get_messages(entity, limit=1)
    except Exception:
        tail = []
    if tail:
        text = (tail[0].raw_text or "").strip()
        if text:
            parts.append(text)
            seen.add(int(tail[0].id or 0))

    end = time.monotonic() + max_wait
    quiet = time.monotonic() + idle

    while time.monotonic() < end:
        try:
            msgs = await client.get_messages(entity, limit=25)
        except Exception as e:
            log.info(f"collect {username}: {e}")
            await asyncio.sleep(1.5)
            continue
        fresh = False
        for m in sorted(msgs or [], key=lambda x: int(x.id or 0)):
            mid = int(m.id or 0)
            if mid <= floor or mid in seen:
                continue
            text = (m.raw_text or "").strip()
            if not text:
                continue
            seen.add(mid)
            parts.append(text)
            fresh = True
            if len(parts) >= max_msgs:
                break
        if fresh:
            quiet = time.monotonic() + idle
            _mark(username)
        elif parts and time.monotonic() >= quiet:
            break
        await asyncio.sleep(1.2)

    return "\n".join(parts)


async def wait_reply(username: str, timeout: float, since: int | None = None) -> str:
    return await collect(username, since, IDLE_GAP, timeout)


async def start_bot(username: str, param: str = "") -> bool:
    client = _client
    if client is None:
        return False
    entity = await _resolve(username)
    if entity is None:
        return False
    if param:
        try:
            await client(functions.messages.StartBotRequest(
                bot=entity, peer=entity, random_id=random.getrandbits(63),
                start_param=param))
            return True
        except Exception as e:
            log.info(f"start_bot {username} param={param}: {e}")
    try:
        await client(functions.messages.StartBotRequest(
            bot=entity, peer=entity, random_id=random.getrandbits(63)))
        return True
    except Exception as e:
        log.info(f"start_bot {username} plain: {e}")
    try:
        await client.send_message(entity, "/start")
        return True
    except Exception as e:
        log.info(f"start_fallback {username}: {e}")
        return False


async def command(username: str, cmd: str, timeout: float = MAX_WAIT) -> str:
    if _client is None:
        return ""
    since = await drain(username)
    if not await send(username, cmd):
        return ""
    return await collect(username, since, IDLE_GAP, timeout)


async def start_and_read(username: str, param: str, timeout: float = MAX_WAIT) -> str:
    if _client is None:
        return ""
    since = await drain(username)
    if not await start_bot(username, param):
        return ""
    return await collect(username, since, IDLE_GAP, timeout)


async def send_and_wait(username: str, text: str, timeout: float) -> str:
    if _client is None:
        return ""
    since = await drain(username)
    if not await send(username, text):
        return ""
    return await collect(username, since, IDLE_GAP, timeout)


async def read_last(username: str) -> str:
    client = _client
    if client is None:
        return ""
    entity = await _resolve(username)
    if entity is None:
        return ""
    try:
        msgs = await client.get_messages(entity, limit=3)
    except Exception:
        return ""
    parts = [(m.raw_text or "").strip() for m in reversed(msgs or []) if m.raw_text]
    return "\n".join(parts)


async def wait_code(username: str, timeout: float) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        text = await read_last(username)
        digits = "".join(ch for ch in text if ch.isdigit())
        if 4 <= len(digits) <= 8:
            return digits[:6]
        await asyncio.sleep(2.0)
    return ""