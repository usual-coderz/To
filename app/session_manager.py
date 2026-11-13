import asyncio
import base64
import inspect
import json
import logging
import re
import time

from telethon import TelegramClient, functions
from telethon.crypto import AuthKey
from telethon.errors import (AuthKeyDuplicatedError, EmailUnconfirmedError,
                             FloodWaitError, PasswordHashInvalidError,
                             SessionPasswordNeededError)
from telethon.network import (ConnectionTcpAbridged, ConnectionTcpFull,
                              ConnectionTcpIntermediate)
from telethon.sessions import StringSession
from telethon.tl.types import EmailVerifyPurposeLoginChange

try:
    from telethon.tl.types import EmailVerificationCode
except ImportError:
    EmailVerificationCode = None

from app.config import (API_HASH, API_ID, AUTH_CLEAN_TIMEOUT, AUTH_DC_SWEEP, AUTH_KEY_LEN,
                        AUTH_PROBE_DEADLINE, AUTH_PROBE_TIMEOUT, CODE_TIMEOUT)
from app.lzt import LztError, extract_auth_key, extract_dc_id

log = logging.getLogger("session")

DEVICE = dict(device_model="AccStore", system_version="2.1",
              app_version="2.1", lang_code="en")

_PROXY_RE = re.compile(r"(socks5|socks4|http)://(?:([^:@]+):([^@]+)@)?([^:]+):(\d+)")

_FALLBACK_DCS = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}

_DCS = None
try:
    from telethon.client.telegrambaseclient import (DEFAULT_DC_ID, DEFAULT_IPV4,
                                                    DEFAULT_PORT, DCList)
    _DCS = DCList(DEFAULT_DC_ID, DEFAULT_IPV4, DEFAULT_PORT)
except Exception as e:
    log.info(f"DCList unavailable, static DC table in use: {e}")

_TRANSPORTS = (ConnectionTcpFull, ConnectionTcpIntermediate, ConnectionTcpAbridged)

MAILBOX_TRIES = 4


class DeadKey(RuntimeError):
    pass


def dc_addr(dc: int):
    if dc not in _FALLBACK_DCS:
        return None
    if _DCS is not None:
        try:
            ip, port = _DCS[dc]
            return str(ip), int(port)
        except Exception:
            pass
    return _FALLBACK_DCS[dc]


def _dc_order(dc: int) -> list[int]:
    order = [dc] if dc in _FALLBACK_DCS else []
    order += [d for d in (2, 4, 5, 1, 3) if d != dc]
    return order[:AUTH_DC_SWEEP]


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


def client(session_string: str = "", proxy_cfg: str = "", *,
           timeout: int = 25, retries: int = 2, connection=None,
           auto_reconnect: bool = True):
    return TelegramClient(
        StringSession(session_string), API_ID, API_HASH,
        proxy=_proxy(proxy_cfg),
        connection=connection or ConnectionTcpFull,
        connection_retries=retries, retry_delay=1, request_retries=3,
        timeout=timeout, use_ipv6=False, auto_reconnect=auto_reconnect,
        flood_sleep_threshold=90, **DEVICE)


def _key_bytes(value) -> bytes:
    text = "".join(str(value or "").split()).strip('"').strip("'")
    if not text:
        return b""
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        try:
            raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
        except Exception:
            return b""
    return raw


def session_from_auth_key(raw: str, dc_id: int = 0) -> str:
    raw = str(raw or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("empty auth key")

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

    key = _key_bytes(data.get("auth_key") or data.get("authKey") or "")
    if len(key) != AUTH_KEY_LEN:
        raise ValueError(f"bad auth key length {len(key)}")

    try:
        dc = int(str(data.get("dc_id") or data.get("dcId") or dc_id or 0).strip())
    except (TypeError, ValueError):
        dc = 0
    if dc not in _FALLBACK_DCS:
        dc = 0

    if dc:
        addr, port = dc_addr(dc)
    else:
        addr = str(data.get("server_address") or data.get("serverAddress")
                   or _FALLBACK_DCS[2][0])
        try:
            port = int(data.get("port") or 443)
        except (TypeError, ValueError):
            port = 443
        dc = 2

    session = StringSession()
    session.set_dc(dc, addr, port)
    session.auth_key = AuthKey(key)
    return session.save()


async def _reset(lzt, item_id: int) -> bool:
    try:
        await lzt.reset_authorizations(item_id)
        return True
    except LztError as e:
        log.info(f"reset auths {item_id}: {e}")
        return False


async def _drop(c: TelegramClient):
    try:
        await c.disconnect()
    except Exception:
        pass


async def _fetch_item(lzt, item_id: int) -> dict:
    try:
        lzt.invalidate(f"item:{int(item_id)}")
    except Exception:
        pass
    try:
        return await lzt.item(item_id) or {}
    except LztError as e:
        log.info(f"item refetch {item_id}: {e}")
        return {}


async def _try_key(raw: str, dc: int, proxy_cfg: str = ""):
    proxies = [proxy_cfg, ""] if proxy_cfg else [""]
    dcs = _dc_order(int(dc or 0))
    deadline = time.monotonic() + AUTH_PROBE_DEADLINE
    last: Exception | None = None

    for transport in _TRANSPORTS:
        for proxy in proxies:
            for target_dc in dcs:
                if time.monotonic() > deadline:
                    raise last or TimeoutError("auth key probe deadline")
                session_str = session_from_auth_key(raw, target_dc)
                conn = client(session_str, proxy, connection=transport, retries=1,
                              timeout=AUTH_PROBE_TIMEOUT, auto_reconnect=False)
                try:
                    await conn.connect()
                    if await conn.is_user_authorized() and await conn.get_me() is not None:
                        return conn, conn.session.save()
                    raise DeadKey(f"auth key not authorized on dc{target_dc}")
                except DeadKey as e:
                    last = e
                    await _drop(conn)
                    raise
                except AuthKeyDuplicatedError as e:
                    await _drop(conn)
                    raise DeadKey(str(e))
                except Exception as e:
                    last = e
                    log.info(f"key dc{target_dc}/{transport.__name__}"
                             f"{' via proxy' if proxy else ''}: {e}")
                    await _drop(conn)

    raise last or RuntimeError("auth key rejected on every DC")


async def login_with_auth_key(phone: str, password: str, item_id: int, lzt,
                              proxy_cfg: str = "", hint: dict | None = None,
                              raw_key: str = "", dc: int = 0):
    key = (raw_key or "").strip() or extract_auth_key(hint)
    dc = int(dc or 0) or extract_dc_id(hint)
    errors: list[str] = []

    if key:
        try:
            conn, session_str = await _try_key(key, dc, proxy_cfg)
            return conn, session_str, 0
        except DeadKey as e:
            errors.append(f"dead: {e}")
            log.info(f"auth key dead {item_id}: {e}")
        except Exception as e:
            errors.append(f"key: {e}")
            log.info(f"auth key rejected {item_id}: {e}")

    fresh = await _fetch_item(lzt, item_id)
    fresh_key = extract_auth_key(fresh)
    fresh_dc = extract_dc_id(fresh)
    if fresh_key and (fresh_key != key or fresh_dc != dc):
        try:
            conn, session_str = await _try_key(fresh_key, fresh_dc or dc, proxy_cfg)
            return conn, session_str, 0
        except Exception as e:
            errors.append(f"fresh: {e}")
            log.info(f"auth key refetch rejected {item_id}: {e}")
    key = fresh_key or key
    dc = fresh_dc or dc

    if not key:
        raise RuntimeError("no auth key on item")

    if not await _reset(lzt, item_id):
        raise RuntimeError("login failed: " + ("; ".join(errors) or "unknown"))

    new_key, new_dc = "", 0
    for _ in range(3):
        await asyncio.sleep(2.0)
        again = await _fetch_item(lzt, item_id)
        new_key, new_dc = extract_auth_key(again), extract_dc_id(again)
        if new_key and (new_key != key or new_dc):
            break
    if new_key:
        key, dc = new_key, new_dc or dc

    conn, session_str = await _try_key(key, dc, proxy_cfg)
    return conn, session_str, 0


async def check_session(session_string: str, proxy_cfg: str = ""):
    if not session_string:
        return False, None
    c = client(session_string, proxy_cfg, retries=1, timeout=20)
    try:
        await c.connect()
        if not await c.is_user_authorized():
            return False, None
        me = await c.get_me()
        return (me is not None), me
    except AuthKeyDuplicatedError as e:
        log.info(f"check_session duplicated: {e}")
        return False, None
    except Exception as e:
        log.info(f"check_session: {e}")
        return False, None
    finally:
        await _drop(c)


def _send_verify_request(purpose, email):
    params = inspect.signature(
        functions.account.SendVerifyEmailCodeRequest.__init__).parameters
    if "purpose" in params:
        return functions.account.SendVerifyEmailCodeRequest(purpose=purpose, email=email)
    return functions.account.SendVerifyEmailCodeRequest(email=email)


def _verify_email_request(purpose, code):
    params = inspect.signature(
        functions.account.VerifyEmailRequest.__init__).parameters
    if "verification" in params and EmailVerificationCode is not None:
        return functions.account.VerifyEmailRequest(
            purpose=purpose, verification=EmailVerificationCode(code=code))
    if "verification" in params:
        return functions.account.VerifyEmailRequest(purpose=purpose, verification=code)
    return functions.account.VerifyEmailRequest(purpose=purpose, code=code)


def _not_allowed(e: Exception) -> bool:
    return "EMAIL_NOT_ALLOWED" in str(e)


async def _send_code(c: TelegramClient, purpose, address: str) -> None:
    last: Exception | None = None
    for _ in range(3):
        try:
            await c(_send_verify_request(purpose, address))
            return
        except FloodWaitError as e:
            last = e
            await asyncio.sleep(min(int(e.seconds) + 2, 60))
        except Exception as e:
            last = e
            if _not_allowed(e):
                raise
            log.info(f"send verify code {address}: {e}")
            await asyncio.sleep(2.0)
    raise last or RuntimeError("send verify code failed")


async def _submit_code(c: TelegramClient, purpose, address: str, code: str) -> None:
    last: Exception | None = None
    for _ in range(3):
        try:
            await c(_verify_email_request(purpose, code))
            return
        except FloodWaitError as e:
            last = e
            await asyncio.sleep(min(int(e.seconds) + 2, 60))
        except Exception as e:
            last = e
            if _not_allowed(e):
                raise
            log.info(f"verify email code {address}: {e}")
            await asyncio.sleep(2.0)
    raise last or RuntimeError("verify email failed")


async def change_email(c: TelegramClient, new_email: str, mail_client,
                       code_timeout: int = CODE_TIMEOUT, remail=None) -> str:
    purpose = EmailVerifyPurposeLoginChange()
    address = new_email
    rejected: list[str] = []
    last: Exception | None = None

    for attempt in range(MAILBOX_TRIES):
        if attempt:
            if remail is None:
                raise last or RuntimeError("no mailbox factory for retry")
            try:
                address = await remail()
            except Exception as e:
                raise last or e
            if not address:
                raise last or RuntimeError("mailbox rotation failed")

        try:
            await _send_code(c, purpose, address)
        except Exception as e:
            if _not_allowed(e) and attempt + 1 < MAILBOX_TRIES:
                rejected.append(address)
                mail_client.block_domain(address)
                log.info(f"domain rejected at send: {address}")
                last = e
                continue
            raise

        try:
            code = await mail_client.fetch_verification_code(
                address, timeout=code_timeout, expected_length=6)
        except Exception as e:
            last = e
            if attempt + 1 < MAILBOX_TRIES:
                continue
            raise

        log.info(f"verify code for {address}: {code}")

        try:
            await _submit_code(c, purpose, address, code)
            if rejected:
                log.info(f"email accepted after {len(rejected)} rejected: {address}")
            return code
        except Exception as e:
            if _not_allowed(e) and attempt + 1 < MAILBOX_TRIES:
                rejected.append(address)
                mail_client.block_domain(address)
                log.info(f"domain rejected at verify: {address}")
                last = e
                continue
            raise

    raise last or RuntimeError("email change failed")


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


async def _edit_2fa(c: TelegramClient, password: str, current: str | None):
    await c.edit_2fa(current_password=current or None, new_password=password, hint="")


async def set_2fa(c: TelegramClient, password: str, current: str = ""):
    info = None
    try:
        info = await c(functions.account.GetPasswordRequest())
    except Exception as e:
        log.info(f"get password state: {e}")

    if bool(getattr(info, "has_password", False)):
        candidates: list[str | None] = [current, ""] if current else [""]
    else:
        candidates = [None]

    last: Exception | None = None
    for cand in candidates:
        for _ in range(2):
            try:
                await _edit_2fa(c, password, cand)
                return
            except FloodWaitError as e:
                await asyncio.sleep(min(int(e.seconds) + 2, 60))
                continue
            except (PasswordHashInvalidError, SessionPasswordNeededError) as e:
                last = e
                break
            except EmailUnconfirmedError as e:
                last = e
                break
            except Exception as e:
                last = e
                break
    if last:
        raise last


async def remove_2fa(c: TelegramClient, current: str = "") -> bool:
    info = None
    try:
        info = await c(functions.account.GetPasswordRequest())
    except Exception as e:
        log.info(f"password state: {e}")
        return False
    if not bool(getattr(info, "has_password", False)):
        return True

    candidates: list[str] = [current, ""] if current else [""]
    for cand in candidates:
        for _ in range(2):
            try:
                await c.edit_2fa(current_password=cand or None, new_password=None, hint="")
                return True
            except FloodWaitError as e:
                await asyncio.sleep(min(int(e.seconds) + 2, 60))
            except (PasswordHashInvalidError, SessionPasswordNeededError) as e:
                log.info(f"remove 2fa wrong password: {e}")
                break
            except Exception as e:
                log.info(f"remove 2fa: {e}")
                break
    return False


async def spambot_status(c: TelegramClient) -> str:
    bot = None
    for needle in ("spambot", "@SpamBot"):
        try:
            bot = await c.get_entity(needle)
            break
        except Exception as e:
            log.info(f"spambot entity {needle}: {e}")
    if bot is None:
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


def clean_client(session_string: str, proxy_cfg: str = ""):
    return client(session_string, proxy_cfg, retries=2, timeout=AUTH_CLEAN_TIMEOUT)