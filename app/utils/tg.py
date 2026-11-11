"""Telegram send/edit helpers that never raise and never lose a message."""
import html
import logging
import re

from aiogram.types import LinkPreviewOptions

log = logging.getLogger("tg")

_TAG_RE = re.compile(r"<[^>]+>")
LP = LinkPreviewOptions(is_disabled=True)
LIMIT = 4000


def esc(value) -> str:
    """Escape a dynamic value for HTML parse mode."""
    return html.escape("" if value is None else str(value), quote=False)


def strip_tags(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or ""))


def clip(text: str, limit: int = LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def send(bot, chat_id, text, markup=None) -> bool:
    """HTML first; plain-text fallback so a bad entity never drops the message."""
    text = clip(text)
    try:
        await bot.send_message(chat_id, text, reply_markup=markup, link_preview_options=LP)
        return True
    except Exception as e:
        log.info(f"send html failed chat={chat_id}: {e}")
    try:
        await bot.send_message(chat_id, clip(strip_tags(text)), reply_markup=markup)
        return True
    except Exception as e:
        log.warning(f"send failed chat={chat_id}: {e}")
        return False


async def send_document(bot, chat_id, payload: bytes, filename: str) -> bool:
    from aiogram.types import BufferedInputFile
    try:
        await bot.send_document(chat_id, BufferedInputFile(payload, filename=filename))
        return True
    except Exception as e:
        log.warning(f"send_document failed chat={chat_id}: {e}")
        return False


async def edit(message, text, markup=None) -> bool:
    text = clip(text)
    for body in (text, clip(strip_tags(text))):
        try:
            await message.edit_text(body, reply_markup=markup, link_preview_options=LP)
            return True
        except Exception as e:
            if "not modified" in str(e):
                return True
        try:
            await message.edit_caption(caption=body, reply_markup=markup)
            return True
        except Exception:
            pass
    return await send(message.bot, message.chat.id, text, markup)