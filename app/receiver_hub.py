import asyncio
import logging
import random
import time

from telethon import events, functions
from telethon.sessions import StringSession

from app.config import API_HASH, API_ID, USERBOT_PROXY, USERBOT_SESSION
from app.session_manager import _proxy

log = logging.getLogger("hub")

_client = None
_lock = asyncio.Lock()
_LAST: dict[str, int] = {}

_BUF: dict[int, list[tuple[int, str]]] = {}
_EV: dict[int, asyncio.Event] = {}
_STAMP: dict[int, int] = {}
_ID: dict[str, int] = {}

IDLE_GAP = 3.0
MAX_WAIT = 25.0
MAX_MSGS = 60
BUF_MAX = 200
WAIT_SLICE = 4.0
POLL_EVERY = 8.0


def running() -> bool:
    return _client is not None


def _ev(peer: int) -> asyncio.Event:
    ev = _EV.get(peer)
    if ev is None:
        ev = asyncio.Event()
        _EV[peer] = ev
    return ev


async def _on_new(event):
    try:
        peer = int(event.sender_id or 0)
    except Exception:
        return
    if not peer:
        return
    mid = int(event.id or 0)
    text = (event.raw_text or "").strip()
    buf = _BUF.setdefault(peer, [])
    buf.append((mid, text))
    if len(buf) > BUF_MAX:
        del buf[:-BUF_MAX]
    if mid > _STAMP.get(peer, 0):
        _STAMP[peer] = mid
    _ev(peer).set()


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
            request_retries=3, timeout=30, flood_sleep_threshold=120,
            device_model="AccStore", system_version="2.1", app_version="2.1")
        await client.connect()
        if not await client.is_user_authorized():
            log.error("userbot session not authorized")
            await client.disconnect()
            return None
        client.add_event_handler(_on_new, events.NewMessage(incoming=True))
        _client = client
        log.info("receiver hub online")
        return client


async def stop():
    global _client
    async with _lock:
        if _client is None:
            return
        try:
            _client.remove_event_handler(_on_new)
        except Exception:
            pass
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None
        _BUF.clear()
        _EV.clear()
        _STAMP.clear()
        _ID.clear()


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


async def peer_id(username: str) -> int:
    key = str(username or "").lstrip("@").lower()
    hit = _ID.get(key)
    if hit:
        return hit
    entity = await _resolve(username)
    if entity is None:
        return 0
    peer = int(entity.id or 0)
    if peer:
        _ID[key] = peer
    return peer


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


async def tail_id(username: str) -> int:
    peer = await peer_id(username)
    if not peer:
        return 0
    return _STAMP.get(peer, 0)


async def _catch_up(peer: int, cursor: int):
    client = _client
    if client is None:
        return
    try:
        entity = await client.get_entity(peer)
    except Exception:
        return
    try:
        msgs = await client.get_messages(entity, limit=15, min_id=cursor)
    except Exception as e:
        log.info(f"catch_up {peer}: {e}")
        return
    buf = _BUF.setdefault(peer, [])
    for m in sorted(msgs or [], key=lambda x: int(x.id or 0)):
        mid = int(m.id or 0)
        if getattr(m, "out", False):
            continue
        buf.append((mid, (m.raw_text or "").strip()))
        if mid > _STAMP.get(peer, 0):
            _STAMP[peer] = mid
    if msgs:
        _ev(peer).set()


async def wait_new(username: str, since: int, timeout: float) -> tuple[str, int]:
    if _client is None:
        return "", since
    peer = await peer_id(username)
    if not peer:
        return "", since

    cursor = int(since or _STAMP.get(peer, 0) or 0)
    end = time.monotonic() + timeout
    last_poll = time.monotonic()
    parts: list[str] = []
    ev = _ev(peer)

    while time.monotonic() < end:
        for mid, text in list(_BUF.get(peer, [])):
            if mid > cursor:
                cursor = mid
                if text:
                    parts.append(text)
        if parts:
            return "\n".join(parts), cursor

        if time.monotonic() - last_poll >= POLL_EVERY:
            last_poll = time.monotonic()
            await _catch_up(peer, cursor)
            continue

        left = end - time.monotonic()
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), max(0.5, min(left, WAIT_SLICE)))
        except asyncio.TimeoutError:
            pass

    return "", cursor


async def drain(username: str) -> int:
    peer = await peer_id(username)
    if not peer:
        return 0
    _BUF.pop(peer, None)
    return _STAMP.get(peer, 0)


async def collect(username: str, since: int | None = None, idle: float = IDLE_GAP,
                  max_wait: float = MAX_WAIT, max_msgs: int = MAX_MSGS) -> str:
    client = _client
    if client is None:
        return ""
    entity = await _resolve(username)
    if entity is None:
        return ""
    peer = int(entity.id or 0)
    floor = _stamp(username) if since is None else since
    parts: list[str] = []
    seen: set[int] = set()

    try:
        tail = await client.get_messages(entity, limit=3)
    except Exception:
        tail = []
    for msg in reversed(tail or []):
        if getattr(msg, "out", False):
            continue
        text = (msg.raw_text or "").strip()
        if text:
            parts.append(text)
            seen.add(int(msg.id or 0))

    end = time.monotonic() + max_wait
    quiet = time.monotonic() + idle

    while time.monotonic() < end:
        for mid, text in list(_BUF.get(peer, [])):
            if mid in seen or not text:
                continue
            seen.add(mid)
            parts.append(text)
        if len(parts) >= max_msgs:
            break
        if parts and time.monotonic() >= quiet:
            break
        try:
            await asyncio.wait_for(_ev(peer).wait(), 2.0)
        except asyncio.TimeoutError:
            pass

    _mark(username)
    return "\n".join(parts)


async def wait_reply(username: str, timeout: float, since: int | None = None) -> str:
    text, _cursor = await wait_new(username, int(since or 0), timeout)
    return text


async def start_bot(username: str, param: str = "") -> bool:
    client = _client
    if client is None:
        return False
    entity = await _resolve(username)
    if entity is None:
        return False
    param = (param or "").strip()
    if param:
        try:
            await client(functions.messages.StartBotRequest(
                bot=entity, peer=entity, random_id=random.getrandbits(63),
                start_param=param))
            return True
        except Exception as e:
            log.info(f"start_bot {username} param={param}: {e}")
        try:
            await client.send_message(entity, "/start " + param)
            return True
        except Exception as e:
            log.info(f"start_param_fallback {username}: {e}")
    try:
        await client.send_message(entity, "/start")
        return True
    except Exception as e:
        log.info(f"start_plain {username}: {e}")
        return False


async def command(username: str, cmd: str, timeout: float = MAX_WAIT) -> str:
    if _client is None:
        return ""
    peer = await peer_id(username)
    cursor = _STAMP.get(peer, 0)
    if not await send(username, cmd):
        return ""
    text, _cursor = await wait_new(username, cursor, timeout)
    return text


async def start_and_read(username: str, param: str, timeout: float = MAX_WAIT) -> str:
    if _client is None:
        return ""
    peer = await peer_id(username)
    cursor = _STAMP.get(peer, 0)
    if not await start_bot(username, param):
        return ""
    text, _cursor = await wait_new(username, cursor, timeout)
    return text


async def send_and_wait(username: str, text: str, timeout: float) -> str:
    if _client is None:
        return ""
    peer = await peer_id(username)
    cursor = _STAMP.get(peer, 0)
    if not await send(username, text):
        return ""
    reply, _cursor = await wait_new(username, cursor, timeout)
    return reply


async def read_last(username: str) -> str:
    peer = await peer_id(username)
    if not peer:
        return ""
    texts = [t for _mid, t in _BUF.get(peer, [])[-6:] if t]
    return "\n".join(texts)


async def wait_code(username: str, timeout: float) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        text = await read_last(username)
        digits = "".join(ch for ch in text if ch.isdigit())
        if 4 <= len(digits) <= 8:
            return digits[:6]
        await asyncio.sleep(2.0)
    return ""