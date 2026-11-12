import asyncio
import email as emaillib
import email.header
import html
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
from app.config import (DATA_DIR, MAIL_POLL, MAIL_PREFER_DOMAIN, MAIL_PROVIDERS,
                        MAIL_PROXY_RETRY, MAIL_TIMEOUT, MAIL_USE_PROXY)

log = logging.getLogger("mail")

VAULT = os.path.join(DATA_DIR, "temp_mail_vault.json")
BLOCKED_FILE = os.path.join(DATA_DIR, "blocked_mail_domains.json")
CODE_LEN = 6
VAULT_MAX = 400
MAILTM = "https://api.mail.tm"
MAILGW = "https://api.mail.gw"
GUERRILLA = "https://api.guerrillamail.com/ajax.php"
DROPMail = "https://dropmail.me/api/graphql"
PLUS = "https://tempmail.plus/api"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
PLUS_DOMAINS = ["fexbox.org", "fexpost.com", "mailbox.in.ua", "chitthi.in",
                "rover.info", "mailto.plus", "fextemp.com", "anyalias.com"]

# domains Telegram answers EMAIL_NOT_ALLOWED for. guerrillamailblock.com is what
# Guerrilla hands out by default, so it is dead on arrival and seeded as such.
SEED_BLOCKED = {
    "guerrillamailblock.com", "grr.la", "sharklasers.com", "spam4.me",
    "pokemail.net", "guerrillamail.biz", "guerrillamail.net",
    "guerrillamail.org", "guerrillamail.de", "guerrillamail.info",
}
GUERRILLA_DOMAINS = (
    "guerrillamail.com", "guerrillamail.info", "guerrillamail.de",
    "guerrillamail.net", "guerrillamail.org", "guerrillamail.biz",
    "sharklasers.com", "grr.la", "spam4.me", "pokemail.net",
    "guerrillamailblock.com",
)

RETRY_STATUS = {402, 403, 408, 425, 429, 500, 502, 503, 504}
RETRY_TRIES = 4
DOWN_COOLDOWN = 240.0
PROVIDERS = tuple(p.strip() for p in MAIL_PROVIDERS.split(",") if p.strip()) or (
    "guerrilla", "dropmail", "mailtm", "mailgw", "plus")

_DOWN: dict[str, float] = {}


def _down(name: str):
    _DOWN[name] = time.time() + DOWN_COOLDOWN


def _is_down(name: str) -> bool:
    return _DOWN.get(name, 0.0) > time.time()


def _norm_domain(value: str) -> str:
    return str(value or "").lower().split("@")[-1].strip()


def blocked_domains() -> set[str]:
    out = set(SEED_BLOCKED)
    try:
        if os.path.exists(BLOCKED_FILE):
            with open(BLOCKED_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                out |= {str(d).lower().strip() for d in data if d}
    except Exception as e:
        log.info(f"blocked read: {e}")
    return out


def block_domain(value: str):
    domain = _norm_domain(value)
    if not domain:
        return
    data = blocked_domains()
    if domain in data:
        return
    data.add(domain)
    try:
        with open(BLOCKED_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(data), f, indent=2)
        log.info(f"domain blocked: {domain}")
    except Exception as e:
        log.info(f"blocked write: {e}")


class MailError(Exception):
    pass


def extract_telegram_code(text: str, expected_length: int = None):
    if not text:
        return None
    t = html.unescape(text)
    t = t.replace("&nbsp;", " ").replace("\u00a0", " ").replace("\u200b", " ")
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
        self._sid = ""
        self._url_token = ""
        self._base = ""
        self._provider = ""
        self._seen: set[str] = set()
        self._blocked: set[str] = blocked_domains()
        self._index = random.randrange(max(1, len(proxies.POOL) or 1))
        self._clients: dict[str, httpx.AsyncClient] = {}

    @property
    def current_email(self) -> str:
        return self._email

    def block_domain(self, value: str):
        domain = _norm_domain(value)
        if not domain:
            return
        self._blocked.add(domain)
        block_domain(domain)

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

    def _targets(self) -> list[str]:
        if not MAIL_USE_PROXY or not proxies.POOL:
            return [""]
        proxy, self._index = proxies.pick(self._index)
        return [proxy, ""] if proxy else [""]

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for _ in range(RETRY_TRIES):
            for proxy in self._targets():
                try:
                    r = await self._http(proxy).request(method, url, **kw)
                except httpx.HTTPError as e:
                    last = e
                    proxies.mark(proxy, MAIL_PROXY_RETRY)
                    continue
                if r.status_code in RETRY_STATUS:
                    last = MailError(f"{r.status_code} {url}")
                    proxies.mark(proxy, MAIL_PROXY_RETRY)
                    continue
                return r
            await asyncio.sleep(0.4)
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

    async def _create_api(self, base: str, name: str, exclude: set) -> str:
        doms = [d for d in await self._domains(base, name) if d.lower() not in exclude]
        doms.sort(key=lambda d: 0 if d.lower() == MAIL_PREFER_DOMAIN else 1)
        for domain in doms[:4]:
            for _ in range(2):
                address = self._rand("tg_", 12) + "@" + domain
                pwd = self._rand("AccStore_", 16)
                try:
                    r = await self._request("POST", f"{base}/accounts",
                                            json={"address": address, "password": pwd})
                except MailError as e:
                    log.info(f"claim {address}: {e}")
                    _down(name)
                    return ""
                if r.status_code not in (200, 201, 422):
                    _down(name)
                    return ""
                token = await self._token_for(base, address, pwd)
                if not token:
                    continue
                self._token, self._base, self._provider = token, base, name
                self._remember(address, pwd, name)
                return address
        return ""

    async def _create_guerrilla(self, exclude: set) -> str:
        skip = {d.lower() for d in exclude} | self._blocked
        order = [d for d in GUERRILLA_DOMAINS if d not in skip]
        order += [d for d in GUERRILLA_DOMAINS if d in skip]

        for domain in order:
            try:
                r = await self._request(
                    "GET", f"{GUERRILLA}?f=get_email_address&lang=en&domain={domain}")
            except MailError as e:
                log.info(f"guerrilla start {domain}: {e}")
                _down("guerrilla")
                return ""
            if r.status_code != 200:
                _down("guerrilla")
                return ""
            data = r.json() or {}
            sid = str(data.get("sid_token") or "")
            address = str(data.get("email_addr") or "")
            if not sid or "@" not in address:
                _down("guerrilla")
                return ""
            got = _norm_domain(address)
            if got in self._blocked:
                log.info(f"guerrilla gave blocked domain {got}, next")
                continue
            name = self._rand("tg", 10)
            try:
                r2 = await self._request(
                    "GET", f"{GUERRILLA}?f=set_email_user&email_user={name}"
                           f"&sid_token={sid}&lang=en&domain={got}")
                if r2.status_code == 200:
                    fresh = str((r2.json() or {}).get("email_addr") or "")
                    if "@" in fresh:
                        address = fresh
            except MailError:
                pass
            if "@" not in address:
                _down("guerrilla")
                return ""
            self._token, self._sid, self._provider = sid, sid, "guerrilla"
            self._remember(address, "", "guerrilla")
            return address
        return ""

    async def _create_dropmail(self, exclude: set) -> str:
        url_token = "accstore-" + self._rand("", 12)
        query = {"query": "mutation {introduceSession {id, expiresAt, addresses {address}}}"}
        try:
            r = await self._request("POST", f"{DROPMail}/{url_token}", json=query)
        except MailError as e:
            log.info(f"dropmail start: {e}")
            _down("dropmail")
            return ""
        if r.status_code != 200:
            _down("dropmail")
            return ""
        session = ((r.json() or {}).get("data") or {}).get("introduceSession") or {}
        sid = str(session.get("id") or "")
        addrs = session.get("addresses") or []
        address = str((addrs[0] or {}).get("address") or "") if addrs else ""
        if not (sid and address):
            _down("dropmail")
            return ""
        if _norm_domain(address) in self._blocked:
            log.info(f"dropmail gave blocked domain {address}, next")
            return ""
        self._token, self._sid = sid, sid
        self._url_token = url_token
        self._provider = "dropmail"
        self._remember(address, "", "dropmail")
        return address

    async def create_temp_email(self, exclude_domains: list = None) -> str:
        exclude = {d.lower().strip() for d in (exclude_domains or []) if d}
        exclude |= blocked_domains()
        order = [p for p in PROVIDERS if not _is_down(p)] or list(PROVIDERS)

        self._seen.clear()

        for name in order:
            address = ""
            try:
                if name == "mailtm":
                    address = await self._create_api(MAILTM, name, exclude)
                elif name == "mailgw":
                    address = await self._create_api(MAILGW, name, exclude)
                elif name == "guerrilla":
                    address = await self._create_guerrilla(exclude)
                elif name == "dropmail":
                    address = await self._create_dropmail(exclude)
            except Exception as e:
                log.info(f"create {name}: {e}")
            if address:
                self._email = address
                log.info(f"mailbox {address} via {name}")
                return address

        pool = [d for d in PLUS_DOMAINS if d.lower() not in exclude] or PLUS_DOMAINS
        random.shuffle(pool)
        address = self._rand("tg_", 12) + "@" + pool[0]
        self._email, self._token, self._sid = address, "", ""
        self._base, self._url_token, self._provider = "", "", "plus"
        self._remember(address, "", "plus")
        log.info(f"mailbox {address} via plus")
        return address

    async def fetch_verification_code(self, target_email: str = None,
                                      timeout: int = MAIL_TIMEOUT,
                                      poll_interval: float = MAIL_POLL,
                                      expected_length: int = None) -> str:
        target = target_email or self._email
        if not target:
            raise MailError("no active mailbox")
        if target != self._email:
            self._email = target
            self._seen.clear()
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
        # exactly one provider is queried per poll; no cross-provider fallthrough,
        # so a guerrilla mailbox never gets looked up on tempmail.plus.
        want = expected_length or CODE_LEN
        if self._provider == "guerrilla" and self._sid:
            return await self._check_guerrilla(want)
        if self._provider == "dropmail" and self._sid:
            return await self._check_dropmail(want)
        if self._provider == "plus":
            return await self._check_plus(target, want)
        if self._token and self._base and not _is_down(self._provider):
            return await self._check_api(self._base, self._provider, want)
        return None

    async def _check_guerrilla(self, expected_length=None):
        try:
            r = await self._request(
                "GET", f"{GUERRILLA}?f=check_email&seq=0&sid_token={self._sid}")
        except MailError as e:
            log.info(f"guerrilla list: {e}")
            _down("guerrilla")
            return None
        if r.status_code != 200:
            _down("guerrilla")
            return None
        for m in (r.json() or {}).get("list", []) or []:
            mid = m.get("mail_id")
            if not mid or mid in self._seen:
                continue
            self._seen.add(mid)
            try:
                full = await self._request(
                    "GET", f"{GUERRILLA}?f=fetch_email&email_id={mid}"
                           f"&sid_token={self._sid}")
            except MailError as e:
                log.info(f"guerrilla fetch {mid}: {e}")
                continue
            if full.status_code != 200:
                continue
            data = full.json() or {}
            text = f"{data.get('mail_subject', '')} {data.get('mail_body', '')}"
            log.info(f"mail {mid} via guerrilla: {text[:160]!r}")
            code = extract_telegram_code(text, expected_length)
            if code:
                return code
        return None

    async def _check_dropmail(self, expected_length=None):
        query = {"query": "query($id: ID!){session(id:$id){mails{fromAddr,headerSubject,text}}}",
                 "variables": {"id": self._sid}}
        try:
            r = await self._request("POST", f"{DROPMail}/{self._url_token}", json=query)
        except MailError as e:
            log.info(f"dropmail list: {e}")
            _down("dropmail")
            return None
        if r.status_code != 200:
            _down("dropmail")
            return None
        session = ((r.json() or {}).get("data") or {}).get("session") or {}
        for m in session.get("mails") or []:
            text = f"{m.get('headerSubject', '')} {m.get('text', '')}"
            key = f"{m.get('fromAddr', '')}|{text[:80]}"
            if key in self._seen:
                continue
            self._seen.add(key)
            code = extract_telegram_code(text, expected_length)
            if code:
                return code
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
            if not mid or mid in self._seen:
                continue
            self._seen.add(mid)
            try:
                full = await self._request("GET", f"{base}/messages/{mid}", headers=headers)
            except MailError:
                continue
            if full.status_code != 200:
                continue
            data = full.json() or {}
            body = f"{data.get('subject', '')} {data.get('text', '') or data.get('html', '')}"
            code = extract_telegram_code(body, expected_length)
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
            if mid is None or str(mid) in self._seen:
                continue
            self._seen.add(str(mid))
            try:
                full = await self._request("GET", f"{PLUS}/mails/{mid}?email={target}&epin=")
            except MailError:
                continue
            if full.status_code != 200:
                continue
            data = full.json() or {}
            code = extract_telegram_code(
                f"{data.get('subject', '')} {data.get('text', '')}", expected_length)
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

    @property
    def current_email(self) -> str:
        return self.addr

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
                    code = extract_telegram_code(f"{subj} {body}", expected_length or CODE_LEN)
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