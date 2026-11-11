import asyncio
import base64
import json
import logging
import re

from telethon import TelegramClient, functions
from telethon.crypto import AuthKey
from telethon.errors import (FloodWaitError, PasswordHashInvalidError,
                             PhoneCodeExpiredError, PhoneCodeInvalidError,
                             SessionPasswordNeededError)
from telethon.sessions import StringSession

from app.config import API_HASH, API_ID, CODE_TIMEOUT
from app.lzt import LztError

log = logging.getLogger("session")

DEVICE = dict(device_model="AccStore", system_version="2.1",
              app_version="2.1", lang_code="en")

_PROXY_RE = re.compile(r"(socks5|socks4|http)://(?:([^:@]+):([^@]+)@)?([^:]+):(\d+)")

_FALLBACK_DC = (2, "149.154.167.51", 443)


def normalize_phone(raw) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if text.startswith("00") and len(digits) > 2:
        digits = digits[2:]
    if not 8 <= len(digits) <= 17:
        return ""
    return "+" + digits


def _proxy(cfg: str):
    if not cfg:
        return None
    m = _PROXY_RE.match(cfg.strip())
    if not m:
        return None
    import socks
    proto, user, pwd, host, port = m.groups()
    kind = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4, "http": socks.HTTP}[proto]
    return (kind, host, int(port), True, user, pwd)


def client(session_string: str = "", proxy_cfg: str = ""):
    return TelegramClient(StringSession(session_string), API_ID, API_HASH,
                          proxy=_proxy(proxy_cfg), connection_retries=3,
                          retry_delay=2, timeout=30, use_ipv6=False,
                          flood_sleep_threshold=90, **DEVICE)


def _key_bytes(value: str) -> bytes:
    value = value.strip()
    if not value:
        return b""
    try:
        return bytes.fromhex(value)
    except ValueError:
        pass
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4))
    except Exception:
        return b""


def session_from_auth_key(raw: str) -> str:
    raw = (raw or "").strip().strip('"')
    if not raw:
        raise ValueError("empty auth key")
    if raw.startswith("1") and len(raw) > 300:
        return raw

    data = None
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        inner = parsed.get("data")
        data = inner if isinstance(inner, dict) else parsed
    elif raw.count(":") == 3:
        dc, addr, port, key = raw.split(":")
        data = {"dc_id": dc, "server_address": addr, "port": port, "auth_key": key}
    if not isinstance(data, dict):
        data = {"auth_key": raw}

    key = _key_bytes(str(data.get("auth_key") or data.get("authKey") or ""))
    if len(key) < 200:
        raise ValueError("short auth key")

    dc_id = int(data.get("dc_id") or data.get("dcId") or _FALLBACK_DC[0])
    addr = str(data.get("server_address") or data.get("serverAddress") or _FALLBACK_DC[1])
    port = int(data.get("port") or _FALLBACK_DC[2])

    session = StringSession()
    session.set_dc(dc_id, addr, port)
    session.auth_key = AuthKey(key)
    return session.save()


async def _reset(lzt, item_id: int) -> bool:
    try:
        await lzt.reset_authorizations(item_id)
        return True
    except LztError as e:
        log.info(f"reset auths {item_id}: {e}")
        return False


async def _alive(c: TelegramClient) -> bool:
    try:
        await c.get_me()
        return True
    except Exception as e:
        log.info(f"alive check: {e}")
        return False


async def _send_code(c: TelegramClient, number: str):
    try:
        return await c.send_code_request(number)
    except FloodWaitError as e:
        wait = min(int(e.seconds) + 2, 120)
        log.info(f"flood wait {wait}s")
        await asyncio.sleep(wait)
        return await c.send_code_request(number)


async def _await_code(lzt, item_id: int, timeout: int) -> tuple[str, str]:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            value, mode = await lzt.telegram_login_code(item_id)
        except LztError as e:
            log.info(f"code poll {item_id}: {e}")
            value, mode = "", ""
        if value and mode in ("auth_key", "authkey", "session") and len(value) > 100:
            return value, "auth_key"
        if value and len(value) >= 4:
            return value, "code"
        await asyncio.sleep(4)
    return "", ""


async def _from_key(raw: str, proxy_cfg: str):
    c = client(session_from_auth_key(raw), proxy_cfg)
    await c.connect()
    if not await _alive(c):
        try:
            await c.disconnect()
        except Exception:
            pass
        raise RuntimeError("auth key rejected")
    return c, c.session.save()


async def _code_login(number: str, password: str, item_id: int, lzt, proxy_cfg: str):
    c = client("", proxy_cfg)
    await c.connect()
    try:
        sent = await _send_code(c, number)
        last: Exception | None = None

        for attempt in range(3):
            value, mode = await _await_code(lzt, item_id, CODE_TIMEOUT)

            if mode == "auth_key" and value:
                try:
                    key_c, key_str = await _from_key(value, proxy_cfg)
                    return key_c, key_str, 0
                except Exception as e:
                    last = e
                    continue

            if not value:
                if attempt == 0:
                    await _reset(lzt, item_id)
                    await asyncio.sleep(2.0)
                    sent = await _send_code(c, number)
                    continue
                raise RuntimeError("LZT login code not received")

            try:
                await c.sign_in(phone=number, code=value,
                                phone_code_hash=sent.phone_code_hash)
                break
            except SessionPasswordNeededError:
                if not password:
                    raise RuntimeError("2FA required but no password on item")
                await c.sign_in(password=password)
                break
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
                last = e
                log.info(f"code rejected for {item_id} ({attempt + 1}/3)")
                if isinstance(e, PhoneCodeExpiredError):
                    sent = await _send_code(c, number)
                await asyncio.sleep(3)
        else:
            raise RuntimeError(f"login code rejected: {last}")

        if not await c.is_user_authorized():
            raise RuntimeError("not authorized after sign_in")

        me = await c.get_me()
        return c, c.session.save(), me.id
    except Exception:
        try:
            await c.disconnect()
        except Exception:
            pass
        raise


async def login_with_lzt(phone: str, password: str, item_id: int, lzt, proxy_cfg: str = ""):
    number = normalize_phone(phone)
    if not number:
        raise RuntimeError("no phone number on item")

    try:
        key = await lzt.auth_key(item_id)
    except LztError as e:
        log.info(f"auth key {item_id}: {e}")
        key = ""

    if key:
        try:
            c, session_str = await _from_key(key, proxy_cfg)
            await _reset(lzt, item_id)
            await asyncio.sleep(1.5)
            if await _alive(c):
                return c, session_str, 0
            await c.disconnect()
        except Exception as e:
            log.info(f"auth key login {item_id}: {e}")

    await _reset(lzt, item_id)
    await asyncio.sleep(2.5)

    c, session_str, uid = await _code_login(number, password, item_id, lzt, proxy_cfg)

    await _reset(lzt, item_id)
    await asyncio.sleep(1.5)
    if await _alive(c):
        return c, session_str, uid

    try:
        await c.disconnect()
    except Exception:
        pass
    log.info(f"session died after reset {item_id}, reissuing")
    return await _code_login(number, password, item_id, lzt, proxy_cfg)


async def check_session(session_string: str, proxy_cfg: str = ""):
    if not session_string:
        return False, None
    c = client(session_string, proxy_cfg)
    try:
        await c.connect()
        if not await c.is_user_authorized():
            return False, None
        return True, await c.get_me()
    except Exception as e:
        log.info(f"check_session: {e}")
        return False, None
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass


async def change_email(c: TelegramClient, new_email: str, mail_client,
                       code_timeout: int = CODE_TIMEOUT) -> str:
    await c(functions.account.SendVerifyEmailCodeRequest(email=new_email))
    code = await mail_client.fetch_verification_code(new_email, timeout=code_timeout,
                                                     expected_length=6)
    await c(functions.account.VerifyEmailRequest(code=code))
    return code


async def reset_others(c: TelegramClient) -> int:
    killed = 0
    try:
        auths = await c(functions.account.GetAuthorizationsRequest())
    except Exception as e:
        log.warning(f"get auths: {e}")
        return 0
    for a in auths.authorizations:
        if a.current:
            continue
        try:
            await c(functions.account.ResetAuthorizationRequest(hash=a.hash))
            killed += 1
            await asyncio.sleep(0.3)
        except FloodWaitError as e:
            await asyncio.sleep(min(int(e.seconds) + 2, 60))
            try:
                await c(functions.account.ResetAuthorizationRequest(hash=a.hash))
                killed += 1
            except Exception as inner:
                log.info(f"reset auth {a.hash}: {inner}")
        except Exception as e:
            log.info(f"reset auth {a.hash}: {e}")
    return killed


async def set_2fa(c: TelegramClient, password: str, current: str = ""):
    last: Exception | None = None
    for attempt in (current or None, None):
        try:
            await c.edit_2fa(new_password=password, current_password=attempt, hint="")
            return
        except FloodWaitError as e:
            await asyncio.sleep(min(int(e.seconds) + 2, 60))
            try:
                await c.edit_2fa(new_password=password, current_password=attempt, hint="")
                return
            except Exception as inner:
                last = inner
        except (PasswordHashInvalidError, SessionPasswordNeededError) as e:
            last = e
    if last:
        raise last


async def spambot_status(c: TelegramClient) -> str:
    try:
        bot = await c.get_entity("spambot")
    except Exception:
        try:
            bot = await c.get_input_entity("spambot")
        except Exception as e:
            log.info(f"spambot entity: {e}")
            return ""
    try:
        async with c.conversation(bot, timeout=30) as conv:
            await conv.send_message("/start")
            msg = await conv.get_response()
            return (msg.raw_text or "").strip()[:400]
    except Exception as e:
        log.info(f"spambot: {e}")
        return ""


async def device_list(c: TelegramClient) -> list[str]:
    try:
        auths = await c(functions.account.GetAuthorizationsRequest())
    except Exception as e:
        log.info(f"device list: {e}")
        return []

    current: list[str] = []
    other: list[str] = []
    for a in auths.authorizations:
        place = ", ".join(x for x in (a.country, a.region) if x) or "unknown"
        line = (f"{'🟢' if a.current else '⚪'} {a.device_model or 'Unknown'}"
                f" ({a.platform}) [{place}]")
        (current if a.current else other).append(line)
    return current + other


async def owned_counts(c: TelegramClient) -> tuple[int, int, int]:
    channels = groups = bots = 0
    try:
        res = await c(functions.channels.GetAdminedPublicChannelsRequest())
        channels = len(res.chats)
    except Exception as e:
        log.info(f"owned channels: {e}")
    try:
        async for d in c.iter_dialogs(limit=120):
            if d.is_group and getattr(d.entity, "creator", False):
                groups += 1
            elif d.is_user and getattr(d.entity, "bot", False):
                bots += 1
    except Exception as e:
        log.info(f"owned dialogs: {e}")
    return channels, groups, bots


async def terminate(c: TelegramClient):
    await reset_others(c)
    try:
        await c.log_out()
    except FloodWaitError as e:
        await asyncio.sleep(min(int(e.seconds) + 2, 60))
        try:
            await c.log_out()
        except Exception as inner:
            log.warning(f"logout: {inner}")
    except Exception as e:
        log.warning(f"logout: {e}")