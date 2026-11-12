import re
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import db, keyboards as kb
from app.handlers.menu import show
from app.utils import tg
from app.utils.texts import filters_text

router = Router()

PENDING_TTL = 300.0
PENDING: dict[int, dict] = {}

NUMERIC = {
    "price_min": ("Min price", "cheapest allowed account price", 0.01, 10_000.0, 2),
    "price_max": ("Max price", "most expensive allowed account price", 0.01, 10_000.0, 2),
    "session_min_hours": ("Min account age", "0 = any · hours", 0.0, 87_600.0, 1),
    "session_max_hours": ("Max account age", "0 = no cap · hours", 0.0, 87_600.0, 1),
    "daily_spend_cap": ("Daily spend cap", "0 = unlimited", 0.0, 100_000.0, 2),
    "sell_confirm_seconds": ("Confirm window", "300-7200 seconds", 300.0, 7200.0, 0),
    "sell_min_price": ("Min sell price", "0 = sell anything", 0.0, 10_000.0, 2),
}

TEXT_KEYS = {"twofa_password": "8-64 characters, used as the new cloud password"}

COUNTRY_KEYS = {
    "countries": "only these countries get bought",
    "deny_countries": "these countries are skipped",
}

AGE_KEYS = {"session_min_hours", "session_max_hours"}

MAIN_KEYS = {"auto_buy", "auto_sell"}

_SPLIT_RE = re.compile(r"[,\s;|]+")


def screen_for(key: str) -> str:
    return "main" if key in MAIN_KEYS else "filters"


async def render_screen(cq: CallbackQuery, key: str):
    if screen_for(key) == "main":
        from app.handlers.menu import render_menu
        text, markup = await render_menu(cq.from_user.id)
        return await show(cq, text, markup)
    s = await db.get_settings(cq.from_user.id)
    return await show(cq, filters_text(s), kb.filters(s))


def _prune():
    cutoff = time.time() - PENDING_TTL
    for uid in [u for u, v in PENDING.items() if v["ts"] < cutoff]:
        PENDING.pop(uid, None)


def _set_pending(uid: int, key: str, chat_id: int = 0, message_id: int = 0):
    _prune()
    PENDING[uid] = {"key": key, "ts": time.time(),
                    "chat_id": chat_id, "message_id": message_id}


def _pop_pending(uid: int) -> dict | None:
    hit = PENDING.pop(uid, None)
    if not hit:
        return None
    return hit if time.time() - hit["ts"] <= PENDING_TTL else None


def _fmt(key: str, value: float) -> str:
    places = NUMERIC[key][4]
    if places == 0:
        return str(int(value))
    return f"{value:.{places}f}".rstrip("0").rstrip(".") or "0"


async def _replace(bot, chat_id: int, message_id: int, text: str, markup):
    if chat_id and message_id:
        try:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id,
                                        reply_markup=markup, link_preview_options=tg.LP)
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=markup)


@router.callback_query(F.data == "m:filters")
async def cb_filters(cq: CallbackQuery):
    s = await db.get_settings(cq.from_user.id)
    await show(cq, filters_text(s), kb.filters(s))
    await cq.answer()


@router.callback_query(F.data == "m:price")
async def cb_price(cq: CallbackQuery):
    await show(cq, "<b>Price</b>\nmax picks top, min picks bottom", kb.price())
    await cq.answer()


@router.callback_query(F.data == "m:spam")
async def cb_spam(cq: CallbackQuery):
    await show(cq, "<b>Spam filter</b>\n"
                   "<i>no</i> — server filters, item flag only noted\n"
                   "<i>yes</i> — only spam-blocked accounts\n"
                   "<i>any</i> — no server filter, local check enforces",
               kb.pick("s:spam_mode", ["no", "yes", "any"]))
    await cq.answer()


@router.callback_query(F.data == "m:mail")
async def cb_mail(cq: CallbackQuery):
    await show(cq, "<b>Mail filter</b>", kb.pick("s:mail_mode", ["yes", "no", "any"]))
    await cq.answer()


@router.callback_query(F.data == "m:pass")
async def cb_pass(cq: CallbackQuery):
    await show(cq, "<b>2FA filter</b>", kb.pick("s:password_mode", ["no", "yes", "any"]))
    await cq.answer()


@router.callback_query(F.data == "m:mailprov")
async def cb_mailprov(cq: CallbackQuery):
    await show(cq, "<b>Mail source on clean</b>",
               kb.pick("s:mail_provider", ["auto", "temp", "native"]))
    await cq.answer()


@router.callback_query(F.data.startswith("s:"))
async def cb_set(cq: CallbackQuery):
    parts = cq.data.split(":", 2)
    if len(parts) != 3:
        return await cq.answer("bad callback")
    _, key, value = parts
    stored = await db.set_setting(cq.from_user.id, key, value)
    if stored is None:
        return await cq.answer("unknown setting", show_alert=True)
    await render_screen(cq, key)
    await cq.answer("saved")


@router.callback_query(F.data.startswith("in:"))
async def cb_input(cq: CallbackQuery):
    key = cq.data.split(":", 1)[1]
    if key in COUNTRY_KEYS:
        hint = COUNTRY_KEYS[key]
        body = ("Send ISO codes, comma separated (e.g. <code>AE,LY,JO</code>)\n"
                "<code>all</code> = clear · /cancel")
        label = "Include countries" if key == "countries" else "Exclude countries"
    elif key in NUMERIC:
        label, hint, lo, hi, _ = NUMERIC[key]
        body = f"Send a number ({lo:g}-{hi:g}) · /cancel"
    elif key in TEXT_KEYS:
        label, hint = "2FA password", TEXT_KEYS[key]
        body = "Send the new password · /cancel"
    else:
        return await cq.answer("unknown setting", show_alert=True)

    msg = await cq.message.answer(f"<b>{label}</b>\n{hint}\n\n{body}",
                                  reply_markup=kb.kb([[("Cancel", "x")]]))
    _set_pending(cq.from_user.id, key, msg.chat.id, msg.message_id)
    await cq.answer()


@router.callback_query(F.data == "x")
async def cb_cancel(cq: CallbackQuery):
    PENDING.pop(cq.from_user.id, None)
    from app.handlers.wallet import EXCH
    from app.handlers.receivers import PENDING as RPENDING
    EXCH.pop(cq.from_user.id, None)
    RPENDING.pop(cq.from_user.id, None)
    s = await db.get_settings(cq.from_user.id)
    await show(cq, filters_text(s), kb.filters(s))
    await cq.answer("cancelled")


@router.message(Command("cancel"))
async def cmd_cancel(m: Message):
    PENDING.pop(m.from_user.id, None)
    from app.handlers.wallet import EXCH
    from app.handlers.receivers import PENDING as RPENDING
    EXCH.pop(m.from_user.id, None)
    RPENDING.pop(m.from_user.id, None)
    s = await db.get_settings(m.from_user.id)
    await m.answer("cancelled", reply_markup=kb.filters(s))


@router.message(F.text)
async def on_text(m: Message):
    if (m.text or "").startswith("/"):
        return

    from app.handlers import wallet
    if wallet.has_pending(m.from_user.id):
        return await wallet.on_amount(m)

    hit = _pop_pending(m.from_user.id)
    if not hit:
        return
    key = hit["key"]
    raw = (m.text or "").strip()

    if key in COUNTRY_KEYS:
        tokens = [t for t in _SPLIT_RE.split(raw) if t]
        if len(tokens) == 1 and tokens[0].lower() in ("all", "-", "clear", "none"):
            val = ""
        else:
            codes = [t.upper() for t in tokens if len(t) == 2 and t.isalpha()]
            if not codes:
                _set_pending(m.from_user.id, key, hit["chat_id"], hit["message_id"])
                return await m.answer("need ISO codes like AE,LY · /cancel")
            val = ",".join(dict.fromkeys(codes))
        stored = await db.set_setting(m.from_user.id, key, val)
    elif key in TEXT_KEYS:
        val = "".join(raw.split())
        if not 8 <= len(val) <= 64 or not val.isascii():
            _set_pending(m.from_user.id, key, hit["chat_id"], hit["message_id"])
            return await m.answer("8-64 ascii characters · /cancel")
        stored = await db.set_setting(m.from_user.id, key, val)
    else:
        label, _, lo, hi, _ = NUMERIC[key]
        try:
            number = float(raw.replace(" ", "").replace(",", "."))
        except ValueError:
            _set_pending(m.from_user.id, key, hit["chat_id"], hit["message_id"])
            return await m.answer(f"{label}: number expected · /cancel")
        if not lo <= number <= hi:
            _set_pending(m.from_user.id, key, hit["chat_id"], hit["message_id"])
            return await m.answer(f"{label}: use {lo:g}-{hi:g} · /cancel")
        stored = await db.set_setting(m.from_user.id, key, _fmt(key, number))

    if stored is None:
        return await m.answer("unknown setting")

    if key in AGE_KEYS:
        await db.reschedule(m.from_user.id)

    note = ""
    if key == "session_min_hours" and 0 < db.num(stored) < 24:
        note = ("\n⚠️ below 24h the market filter cannot apply (day granularity) — "
                "checked locally after check-account")

    s = await db.get_settings(m.from_user.id)
    await _replace(m.bot, hit["chat_id"], hit["message_id"],
                   f"✅ saved <code>{tg.esc(stored or '-')}</code>{note}\n\n{filters_text(s)}",
                   kb.filters(s))