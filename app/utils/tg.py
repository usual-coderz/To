import asyncio
import html
import logging
import re
import time

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, LinkPreviewOptions

log = logging.getLogger("tg")

_TAG_RE = re.compile(r"<[^>]+>")
LP = LinkPreviewOptions(is_disabled=True)
LIMIT = 4000
SEND_TIMEOUT = 40.0
SPACING = 1.4
FLOOD_CAP = 600.0
FLOOD_TRIES = 6

_LOCKS: dict[int, asyncio.Lock] = {}
_UNTIL: dict[int, float] = {}
_LAST: dict[int, float] = {}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def strip_tags(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or ""))


def clip(text: str, limit: int = LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pre(text: str, limit: int = LIMIT) -> str:
    return "<pre>" + esc(clip(text, limit)) + "</pre>"


def _lock(chat_id) -> asyncio.Lock:
    lock = _LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[chat_id] = lock
    return lock


def flood_left(chat_id) -> float:
    return max(0.0, _UNTIL.get(chat_id, 0.0) - time.time())


def _flood(chat_id, seconds: float):
    until = time.time() + min(max(1.0, seconds), FLOOD_CAP)
    if until > _UNTIL.get(chat_id, 0.0):
        _UNTIL[chat_id] = until


def _retry_after(e) -> float:
    value = getattr(e, "retry_after", None)
    try:
        return float(value)
    except (TypeError, ValueError):
        m = re.search(r"retry after (\d+)", str(e))
        return float(m.group(1)) if m else 5.0


async def _slot(chat_id):
    left = flood_left(chat_id)
    if left:
        log.info(f"flood wait chat={chat_id} {left:.0f}s")
        await asyncio.sleep(left)
    gap = SPACING - (time.time() - _LAST.get(chat_id, 0.0))
    if gap > 0:
        await asyncio.sleep(gap)


async def _call(chat_id, coro_factory):
    async with _lock(chat_id):
        await _slot(chat_id)
        try:
            result = await asyncio.wait_for(coro_factory(), SEND_TIMEOUT)
            _LAST[chat_id] = time.time()
            return "ok", result
        except TelegramRetryAfter as e:
            _flood(chat_id, _retry_after(e))
            return "flood", None
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return "ok", None
            log.info(f"bad request chat={chat_id}: {e}")
            return "bad", None
        except Exception as e:
            log.info(f"call failed chat={chat_id}: {e}")
            return "err", None


async def send(bot, chat_id, text, markup=None) -> bool:
    text = clip(text)
    for _ in range(FLOOD_TRIES):
        state, _r = await _call(
            chat_id, lambda: bot.send_message(chat_id, text, reply_markup=markup,
                                              link_preview_options=LP))
        if state == "ok":
            return True
        if state == "flood":
            continue
        if state == "bad":
            state, _r = await _call(
                chat_id, lambda: bot.send_message(chat_id, clip(strip_tags(text)),
                                                  reply_markup=markup, parse_mode=None))
            if state == "ok":
                return True
            if state == "flood":
                continue
        return False
    log.warning(f"send dropped chat={chat_id}")
    return False


async def send_document(bot, chat_id, payload: bytes, filename: str) -> bool:
    for _ in range(FLOOD_TRIES):
        state, _r = await _call(
            chat_id, lambda: bot.send_document(
                chat_id, BufferedInputFile(payload, filename=filename)))
        if state == "ok":
            return True
        if state != "flood":
            return False
    return False


async def edit(message, text, markup=None) -> bool:
    text = clip(text)
    for _ in range(FLOOD_TRIES):
        state, _r = await _call(
            message.chat.id,
            lambda: message.edit_text(text, reply_markup=markup,
                                      link_preview_options=LP))
        if state == "ok":
            return True
        if state == "flood":
            continue
        if state == "bad":
            state, _r = await _call(
                message.chat.id,
                lambda: message.edit_text(clip(strip_tags(text)), reply_markup=markup,
                                          parse_mode=None, link_preview_options=LP))
            if state == "ok":
                return True
            if state == "flood":
                continue
            break
        break
    return await send(message.bot, message.chat.id, text, markup)