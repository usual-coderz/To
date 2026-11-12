import asyncio
import email as emaillib
import email.header
import imaplib
import json
import logging
import os
import random
import re
import string
import time

import httpx

from app import proxies
from app.config import (DATA_DIR, MAIL_POLL, MAIL_PREFER_DOMAIN, MAIL_PROXY_RETRY,
                        MAIL_TIMEOUT)

log = logging.getLogger("mail")

VAULT = os.path.join(DATA_DIR, "temp_mail_vault.json")
CODE_LEN = 5
VAULT_MAX = 400
MAILTM = "https://api.mail.tm"
MAILGW = "https://api.mail.gw"
PLUS = "https://tempmail.plus/api"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
PLUS_DOMAINS = ["fexbox.org", "fexpost.com", "mailbox.in.ua", "chitthi.in",
                "rover.info", "mailto.plus", "fextemp.com", "anyalias.com"]

RETRY_STATUS = {402, 403, 408, 425, 429, 500, 502, 503, 504}
RETRY_TRIES = 6
DOWN_COOLDOWN = 300.0
PROVIDERS = (("mailtm", MAILTM), ("mailgw", MAILGW))

_DOWN: dict[str, float] = {}


def _down(name: str):
    _DOWN[name] = time.time() + DOWN_COOLDOWN


def _is_down(name: str) -> bool:
    return _DOWN.get(name, 0.0) > time.time()


class MailError(Exception):
    pass


def extract_telegram_code(text: str, expected_length: int = None):
    if not text:
        return None
    t = text.replace("&nbsp;", " ").replace("\u00a0", " ").replace("\u200b", " ")
    t = re.sub(r"<[^>]+>", " ", t)
    ctx = (r"(?:your\s+(?:telegram\s+)?code|verification\s+code|login\s+code"
           r"|telegram\s+code|security\s+code|confirm(?:ation)?\s+code|código|код|رمز\w*)")
    patterns = (
        ctx + r"(?:\s+(?:is|was|to|هو))?[\s:=-]{1,10}(\d{3})[\s-](\d{3})\b",
        ctx + r"(?:\s+(?:is|was|to|هو))?[\s:=-]{1,10}(\d{5,6})\b",
        r"\b(\d{3})[\s-](\d{3})\b\s+(?:is\s+your\s+(?:telegram|verification|login|code))",
        r"\b(\d{5,6})\b\s+(?:is\s+your\s+(?:telegram|verification|login|code))",
    )
    for pattern in patterns:
        m = re.search(pattern, t, re.I)
        if not m:
            continue
        cand = "".join(m.groups())
        if expected_length is None or len(cand) == expected_length:
            return cand
    if expected_length in (None, 6):
        m = re.search(r"\b(\d{3})[\s-](\d{3})\b", t)
        if m:
            return m.group(1) + m.group(2)
    for cand in re.findall(r"\b(\d{5,6})\b", t):
        if expected_length is None or len(cand) == expected_length:
            return cand
    return None


def _vault_read() -> dict:
    try:
        if os.path.exists(VAULT):
            with open(VAULT, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        log.info(f"vault read: {e}")
    return {}


def _vault_write(data: dict):
    try:
        items = list(data.items())[-VAULT_MAX:]
        with open(VAULT, "w", encoding="utf-8") as f:
            json.dump(dict(items), f, indent=2)
    except Exception as e:
        log.info(f"vault write: {e}")


class TempMailClient:
    def __init__(self):
        self._email = ""
        self._token = ""
        self._base = ""
        self._provider = ""
        self._index = random.randrange(max(1, len(proxies.POOL) or 1))
        self._cooldown: dict[str, float] = {}
        self._clients: dict[str, httpx.AsyncClient] = {}

    def _http(self, proxy: str) -> httpx.AsyncClient:
        client = self._clients.get(proxy)
        if client is None:
            client = httpx.AsyncClient(
                timeout=15, follow_redirects=True, headers={"User-Agent": UA},
                proxy=proxy or None)
            self._clients[proxy] = client
        return client

    async def close(self):
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception:
                pass
        self._clients.clear()

    def _pick(self) -> str:
        if not proxies.POOL:
            return ""
        now = time.time()
        for _ in range(len(proxies.POOL)):
            proxy, self._index = proxies.rotate(self._index)
            if self._cooldown.get(proxy, 0.0) <= now:
                return proxy
        return ""

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for _ in range(RETRY_TRIES):
            proxy = self._pick()
            try:
                r = await self._http(proxy).request(method, url, **kw)
            except httpx.HTTPError as e:
                last = e
                self._cooldown[proxy] = time.time() + 2.0
                await asyncio.sleep(0.3)
                continue
            if r.status_code in RETRY_STATUS:
                last = MailError(f"{r.status_code} {url}")
                self._cooldown[proxy] = time.time() + MAIL_PROXY_RETRY
                await asyncio.sleep(0.5)
                continue
            return r
        raise MailError(f"request failed: {last}")

    @staticmethod
    def _rand(prefix: str, n: int) -> str:
        return prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

    def _remember(self, address: str, password: str, provider: str):
        data = _vault_read()
        data[address] = {"password": password, "provider": provider,
                         "created_at": int(time.time())}
        _vault_write(data)

    async def _domains(self, base: str, name: str) -> list[str]:
        try:
            r = await self._request("GET", f"{base}/domains")
        except MailError as e:
            log.info(f"domains {name}: {e}")
            _down(name)
            return []
        if r.status_code != 200:
            _down(name)
            return []
        rows = (r.json() or {}).get("hydra:member", [])
        doms = [str(d.get("domain")) for d in rows if d.get("isActive") and d.get("domain")]
        if not doms:
            _down(name)
        return doms

    async def _token_for(self, base: str, address: str, password: str) -> str:
        try:
            t = await self._request("POST", f"{base}/token",
                                    json={"address": address, "password": password})
        except MailError as e:
            log.info(f"token {address}: {e}")
            return ""
        if t.status_code != 200:
            return ""
        return (t.json() or {}).get("token") or ""

    async def claim(self, address: str, password: str = "", base: str = MAILTM,
                    name: str = "mailtm") -> bool:
        pwd = password or self._rand("AccStore_", 16)
        body = {"address": address, "password": pwd}
        try:
            r = await self._request("POST", f"{base}/accounts", json=body)
        except MailError as e:
            log.info(f"claim {address}: {e}")
            _down(name)
            return False
        if r.status_code not in (200, 201, 422):
            _down(name)
            return False
        token = await self._token_for(base, address, pwd)
        if not token:
            return False
        self._email, self._token, self._base, self._provider = address, token, base, name
        self._remember(address, pwd, name)
        return True

    async def create_temp_email(self, exclude_domains: list = None) -> str:
        exclude = {d.lower().strip() for d in (exclude_domains or []) if d}
        order = [p for p in PROVIDERS if not _is_down(p[0])] or list(PROVIDERS)

        for name, base in order:
            doms = [d for d in await self._domains(base, name) if d.lower() not in exclude]
            doms.sort(key=lambda d: 0 if d.lower() == MAIL_PREFER_DOMAIN else 1)
            for domain in doms[:4]:
                for _ in range(2):
                    address = self._rand("tg_", 12) + "@" + domain
                    if await self.claim(address, base=base, name=name):
                        return address

        if _is_down("plus"):
            _DOWN.pop("plus", None)
        pool = [d for d in PLUS_DOMAINS if d.lower() not in exclude] or PLUS_DOMAINS
        random.shuffle(pool)
        address = self._rand("tg_", 12) + "@" + pool[0]
        self._email, self._token, self._base, self._provider = address, "", "", "plus"
        self._remember(address, "", "plus")
        return address

    async def fetch_verification_code(self, target_email: str = None,
                                      timeout: int = MAIL_TIMEOUT,
                                      poll_interval: float = MAIL_POLL,
                                      expected_length: int = None) -> str:
        target = target_email or self._email
        if not target:
            raise MailError("no active mailbox")
        end = time.time() + timeout
        while time.time() < end:
            try:
                code = await self._check_inbox(target, expected_length)
                if code:
                    return code
            except Exception as e:
                log.info(f"inbox: {e}")
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"no code in {timeout}s for {target}")

    async def _check_inbox(self, target: str, expected_length=None):
        if self._token and self._base and not _is_down(self._provider):
            code = await self._check_api(self._base, self._provider, expected_length)
            if code:
                return code
        if not _is_down("plus"):
            code = await self._check_plus(target, expected_length)
            if code:
                return code
        if self._token and self._base:
            return await self._check_api(self._base, self._provider, expected_length)
        return None

    async def _check_api(self, base: str, name: str, expected_length=None):
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            r = await self._request("GET", f"{base}/messages", headers=headers)
        except MailError as e:
            log.info(f"messages {name}: {e}")
            _down(name)
            return None
        if r.status_code == 401:
            self._token = await self._token_for(base, self._email, "")
            return None
        if r.status_code != 200:
            _down(name)
            return None
        for m in (r.json() or {}).get("hydra:member", []):
            mid = m.get("id")
            if not mid:
                continue
            try:
                full = await self._request("GET", f"{base}/messages/{mid}", headers=headers)
            except MailError:
                continue
            if full.status_code != 200:
                continue
            data = full.json() or {}
            body = f"{data.get('subject', '')} {data.get('text', '') or data.get('html', '')}"
            code = extract_telegram_code(body, expected_length or CODE_LEN)
            if code:
                return code
        return None

    async def _check_plus(self, target: str, expected_length=None):
        try:
            r = await self._request("GET", f"{PLUS}/mails?email={target}&epin=&limit=15")
        except MailError as e:
            log.info(f"plus list: {e}")
            _down("plus")
            return None
        if r.status_code != 200:
            _down("plus")
            return None
        for m in (r.json() or {}).get("mail_list", []) or []:
            mid = m.get("id")
            if mid is None:
                continue
            try:
                full = await self._request("GET", f"{PLUS}/mails/{mid}?email={target}&epin=")
            except MailError:
                continue
            if full.status_code != 200:
                continue
            data = full.json() or {}
            code = extract_telegram_code(
                f"{data.get('subject', '')} {data.get('text', '')}",
                expected_length or CODE_LEN)
            if code:
                return code
        return None


HOSTS = {"gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
         "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com",
         "live.com": "outlook.office365.com", "msn.com": "outlook.office365.com",
         "yahoo.com": "imap.mail.yahoo.com", "ymail.com": "imap.mail.yahoo.com",
         "aol.com": "imap.aol.com", "gmx.com": "imap.gmx.com", "gmx.net": "imap.gmx.net",
         "web.de": "imap.web.de", "zoho.com": "imap.zoho.com", "icloud.com": "imap.mail.me.com",
         "me.com": "imap.mail.me.com", "yandex.com": "imap.yandex.com",
         "yandex.ru": "imap.yandex.com", "ya.ru": "imap.yandex.com",
         "mail.ru": "imap.mail.ru", "bk.ru": "imap.mail.ru", "inbox.ru": "imap.mail.ru",
         "list.ru": "imap.mail.ru", "internet.ru": "imap.mail.ru",
         "rambler.ru": "imap.rambler.ru", "ukr.net": "imap.ukr.net"}


class IMAPMailClient:
    def __init__(self, addr: str, password: str, host: str = None, port: int = 993):
        self.addr, self.password = addr, password
        self.host = host or HOSTS.get(addr.split("@")[-1].lower())
        self.port = port
        if not self.host:
            raise ValueError(f"no IMAP host known for {addr}")

    def _blocking(self, timeout: int, expected_length=None):
        m = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            m.login(self.addr, self.password)
            m.select("INBOX")
            end = time.time() + timeout
            while time.time() < end:
                _typ, data = m.search(None, "UNSEEN")
                for uid in reversed(data[0].split() if data and data[0] else []):
                    typ, msg_data = m.fetch(uid, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    msg = emaillib.message_from_bytes(msg_data[0][1])
                    subj = str(email.header.make_header(
                        email.header.decode_header(msg["Subject"] or "")))
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                payload = part.get_payload(decode=True) or b""
                                body += payload.decode("utf-8", "replace")
                    else:
                        body = (msg.get_payload(decode=True) or b"").decode("utf-8", "replace")
                    code = extract_telegram_code(f"{subj} {body}",
                                                 expected_length or 6)
                    if code:
                        return code
                time.sleep(3)
            return None
        finally:
            try:
                m.logout()
            except Exception:
                pass

    async def fetch_verification_code(self, target_email: str = None,
                                      timeout: int = MAIL_TIMEOUT,
                                      poll_interval: float = MAIL_POLL,
                                      expected_length=None) -> str:
        loop = asyncio.get_running_loop()
        last: Exception | None = None
        for _ in range(2):
            try:
                code = await loop.run_in_executor(None, self._blocking, timeout,
                                                  expected_length)
                if code:
                    return code
            except Exception as e:
                last = e
                log.info(f"imap {self.addr}: {e}")
                await asyncio.sleep(2.0)
        if last:
            raise last
        raise TimeoutError(f"no IMAP code in {timeout}s")