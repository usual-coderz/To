"""Telethon: login via LZT code, session check, change mail, terminate."""
import asyncio
import logging
import re

from telethon import TelegramClient, functions
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession

from app.config import API_HASH, API_ID, CODE_TIMEOUT
from app.lzt import LztError

log = logging.getLogger("session")

DEVICE = dict(device_model="AccStore", system_version="1.0",
              app_version="1.0", lang_code="en")


def _proxy(cfg: str):
    if not cfg:
        return None
    m = re.match(r"(socks5|socks4|http)://(?:([^:@]+):([^@]+)@)?([^:]+):(\d+)", cfg.strip())
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


async def _await_code(lzt, item_id: int, timeout: int) -> str:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            got, _mode = await lzt.telegram_login_code(item_id)
        except LztError as e:
            log.info(f"code poll {item_id}: {e}")
            got = ""
        if got and len(got) >= 4:
            return got
        await asyncio.sleep(4)
    return ""


async def login_with_lzt(phone: str, password: str, item_id: int, lzt, proxy_cfg: str = ""):
    c = client("", proxy_cfg)
    await c.connect()
    try:
        sent = await c.send_code_request(phone)
        last: Exception | None = None

        for attempt in range(3):
            code = await _await_code(lzt, item_id, CODE_TIMEOUT)
            if not code:
                raise RuntimeError("LZT login code not received")
            try:
                await c.sign_in(phone=phone, code=code,
                                phone_code_hash=sent.phone_code_hash)
                break
            except SessionPasswordNeededError:
                if not password:
                    raise RuntimeError("2FA required but no password on item")
                await c.sign_in(password=password)
                break
            except PhoneCodeInvalidError as e:
                last = e
                log.info(f"code rejected for {item_id}, retrying ({attempt + 1}/3)")
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


async def check_session(session_string: str, proxy_cfg: str = ""):
    """(alive, me) — always disconnects."""
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


async def terminate(c: TelegramClient):
    try:
        auths = await c(functions.account.GetAuthorizationsRequest())
        for a in auths.authorizations:
            if a.current:
                continue
            try:
                await c(functions.account.ResetAuthorizationRequest(hash=a.hash))
                await asyncio.sleep(0.3)
            except Exception as e:
                log.info(f"reset auth {a.hash}: {e}")
    except Exception as e:
        log.warning(f"reset auths: {e}")
    try:
        await c.log_out()
    except Exception as e:
        log.warning(f"logout: {e}")