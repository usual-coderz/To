import html
import logging
import re

from aiogram.types import LinkPreviewOptions

log = logging.getLogger("tg")

_TAG_RE = re.compile(r"<[^>]+>")
LP = LinkPreviewOptions(is_disabled=True)
LIMIT = 4000


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def strip_tags(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or ""))


def clip(text: str, limit: int = LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def send(bot, chat_id, text, markup=None) -> bool:
    text = clip(text)
    try:
        await bot.send_message(chat_id, text, reply_markup=markup, link_preview_options=LP)
        return True
    except Exception as e:
        log.info(f"send html failed chat={chat_id}: {e}")
    try:
        await bot.send_message(chat_id, clip(strip_tags(text)), reply_markup=markup,
                               parse_mode=None)
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
    try:
        await message.edit_text(text, reply_markup=markup, link_preview_options=LP)
        return True
    except Exception as e:
        if "not modified" in str(e):
            return True
    try:
        await message.edit_text(clip(strip_tags(text)), reply_markup=markup,
                                parse_mode=None, link_preview_options=LP)
        return True
    except Exception as e:
        if "not modified" in str(e):
            return True
    return await send(message.bot, message.chat.id, text, markup)