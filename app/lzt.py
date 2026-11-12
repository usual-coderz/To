import asyncio
import base64
import json
import logging
import time

import httpx

from app.config import (AUTH_KEY_LEN, CACHE_TTL, HTTP_BACKOFF, HTTP_RETRIES, HTTP_TIMEOUT,
                        LZT_BASE, LZT_TOKEN)
from app.utils.filters import search_query
from app.utils.money import parse_balances, parse_user, same_currency

log = logging.getLogger("lzt")

PAYMENT_PATHS = ("/user/payments", "/payments", "/balance/payments")
AUTH_KEY_NAMES = {"telegram_auth_key", "auth_key", "authkey", "auth_key_base64",
                  "telegram_auth_key_base64", "session", "session_string",
                  "telegram_session", "string_session", "auth"}
DC_NAMES = {"telegram_dc_id", "dc_id", "dcid", "dc"}

RESET_PATH = "/{}/telegram-reset-authorizations"
QR_PATH = "/{}/qr-login"
CHECK_PATH = "/{}/check-account"
BUY_PATH = "/{}/fast-buy"

RETRY_ATTEMPTS = 40
RETRY_DELAY = 0.4
MAX_DEPTH = 5


class LztError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def classify(e: LztError) -> str:
    text = str(e).lower()
    if "daily limit" in text or "limit of checks" in text:
        return "check_limit"
    if "is sold" in text or "sold." in text:
        return "sold"
    if "permission to view" in text or "not available" in text:
        return "sold"
    if "removed" in text or "deleted" in text:
        return "deleted"
    if "blacklist" in text:
        return "blacklist"
    if "enough money" in text or "enough balance" in text:
        return "no_balance"
    if "session is invalid" in text or "logged out" in text or "blocked" in text:
        return "dead"
    return "check_failed"


def _message(payload) -> str:
    def dig(node, depth: int = 0) -> str:
        if depth > 4:
            return ""
        if isinstance(node, str):
            return node.strip()
        if isinstance(node, dict):
            for key in ("message", "error", "errors", "detail", "title", "text"):
                if key in node:
                    found = dig(node[key], depth + 1)
                    if found:
                        return found
            for value in node.values():
                found = dig(value, depth + 1)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = dig(value, depth + 1)
                if found:
                    return found
        return ""

    return dig(payload)[:300]


def key_ok(text) -> bool:
    raw = "".join(str(text or "").split()).strip('"').strip("'")
    if len(raw) == AUTH_KEY_LEN * 2 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return True
    try:
        blob = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    except Exception:
        return False
    return len(blob) == AUTH_KEY_LEN


def _candidate(value) -> str:
    if isinstance(value, dict):
        inner = value.get("auth_key") or value.get("authKey") or value.get("key")
        if inner and key_ok(inner):
            try:
                return json.dumps(value)
            except Exception:
                return ""
        return ""
    if not isinstance(value, str):
        return ""
    text = value.strip().strip('"').strip("'")
    if not text:
        return ""
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return _candidate(parsed)
    return text if key_ok(text) else ""


def _walk_keys(node, depth: int = 0, named_only: bool = False) -> str:
    if depth > MAX_DEPTH:
        return ""
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() not in AUTH_KEY_NAMES:
                continue
            cand = _candidate(value)
            if cand:
                return cand
        if not named_only:
            for value in node.values():
                cand = _candidate(value)
                if cand:
                    return cand
        for value in node.values():
            found = _walk_keys(value, depth + 1, named_only)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _walk_keys(value, depth + 1, named_only)
            if found:
                return found
    return ""


def extract_auth_key(item) -> str:
    if not isinstance(item, dict):
        return ""
    return _walk_keys(item, 0, True) or _walk_keys(item, 0, False)


def _walk_dc(node, depth: int = 0) -> int:
    if depth > MAX_DEPTH:
        return 0
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() not in DC_NAMES:
                continue
            try:
                number = int(str(value).strip())
            except (TypeError, ValueError):
                continue
            if 1 <= number <= 5:
                return number
        for value in node.values():
            found = _walk_dc(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _walk_dc(value, depth + 1)
            if found:
                return found
    return 0


def extract_dc_id(item) -> int:
    return _walk_dc(item) if isinstance(item, dict) else 0


class LztClient:
    def __init__(self, token: str = LZT_TOKEN):
        self._token = token
        self._cache: dict[str, tuple[float, object]] = {}
        self._http = httpx.AsyncClient(
            base_url=LZT_BASE,
            timeout=HTTP_TIMEOUT,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json",
                     "User-Agent": "AccStore/3.2"},
        )

    async def close(self):
        try:
            await self._http.aclose()
        except Exception:
            pass

    def invalidate(self, key: str | None = None):
        if key is None:
            self._cache.clear()
            return
        for k in [k for k in self._cache if k.startswith(key)]:
            self._cache.pop(k, None)

    def _cached(self, key: str):
        hit = self._cache.get(key)
        if not hit:
            return None
        stamp, value = hit
        if time.monotonic() - stamp > CACHE_TTL:
            self._cache.pop(key, None)
            return None
        return value

    def _store(self, key: str, value):
        self._cache[key] = (time.monotonic(), value)
        return value

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        delay = HTTP_BACKOFF
        last: LztError | None = None

        for attempt in range(HTTP_RETRIES):
            try:
                r = await self._http.request(method, path, **kwargs)
            except httpx.HTTPError as e:
                last = LztError(f"network: {e}")
                if attempt + 1 < HTTP_RETRIES:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise last

            try:
                payload = r.json() if r.content else {}
            except ValueError:
                payload = {}

            retryable = r.status_code == 429 or r.status_code >= 500
            if retryable and attempt + 1 < HTTP_RETRIES:
                wait = delay
                try:
                    wait = max(wait, float(r.headers.get("Retry-After") or 0))
                except (TypeError, ValueError):
                    pass
                log.info(f"{path}: HTTP {r.status_code}, retry in {wait:.1f}s")
                await asyncio.sleep(min(wait, 20.0))
                delay *= 2
                continue

            if r.status_code >= 400:
                raise LztError(_message(payload) or f"HTTP {r.status_code}", r.status_code)

            if isinstance(payload, dict) and payload.get("errors"):
                raise LztError(
                    _message(payload) or f"errors: {payload.get('errors')!r}"[:300],
                    r.status_code)

            return payload if isinstance(payload, dict) else {}

        raise last or LztError("request failed")

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params or {})

    async def _post(self, path: str, params: dict | None = None,
                    json: dict | None = None) -> dict:
        return await self._request("POST", path, params=params or {}, json=json)

    async def _buy_call(self, path: str, payload: dict | None) -> dict:
        last: LztError | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return await self._request("POST", path, json=payload)
            except LztError as e:
                last = e
                if e.status == 403 and "retry_request" in str(e).lower():
                    await asyncio.sleep(min(RETRY_DELAY + attempt * 0.1, 3.0))
                    continue
                raise
        raise last or LztError("retry_request exhausted", 403)

    async def profile(self, use_cache: bool = True) -> dict:
        if use_cache:
            hit = self._cached("me")
            if hit is not None:
                return dict(hit)
        data = parse_user(await self._get("/me"))
        return dict(self._store("me", data))

    async def balances(self, use_cache: bool = True) -> list[dict]:
        if use_cache:
            hit = self._cached("balances")
            if hit is not None:
                return [dict(b) for b in hit]
        data = parse_balances(await self._get("/balance/exchange"))
        return [dict(b) for b in self._store("balances", data)]

    async def balance_total(self, currency: str = "usd") -> float:
        user = await self.profile()
        if not same_currency(user["currency"], currency):
            return 0.0
        return max(0.0, user["balance"] - user["hold"])

    async def payments(self, page: int = 1) -> list[dict]:
        params = {"page": int(page), "limit": 20}
        for path in PAYMENT_PATHS:
            try:
                data = await self._get(path, params)
            except LztError as e:
                log.info(f"payments {path}: {e}")
                continue
            rows = data.get("payments") or data.get("items") or data.get("operations")
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        return []

    async def exchange(self, from_id: int, to_id: int, amount: float) -> dict:
        data = await self._post("/balance/exchange", {
            "balance_id_from": int(from_id),
            "balance_id_to": int(to_id),
            "amount": float(amount),
        })
        self.invalidate()
        return data

    @staticmethod
    def _enum(value: str) -> str:
        return "nomatter" if value == "any" else value

    def _search_params(self, s: dict) -> dict:
        params: dict = {
            "pmin": s.get("price_min"),
            "pmax": s.get("price_max"),
            "currency": s.get("currency", "usd"),
            "order_by": "price_to_up",
            "spam": self._enum(s.get("spam_mode", "no")),
            "email": self._enum(s.get("mail_mode", "any")),
            "password": self._enum(s.get("password_mode", "any")),
        }
        if s.get("nsb_only") == "1":
            params["nsb"] = "true"

        allow = [c for c in (s.get("countries") or "").split(",") if c]
        deny = [c for c in (s.get("deny_countries") or "").split(",") if c]
        if allow:
            params["country[]"] = allow
        if deny:
            params["not_country[]"] = deny

        params.update(search_query(s))
        return {k: v for k, v in params.items() if v not in (None, "", [])}

    async def search(self, s: dict) -> list[dict]:
        data = await self._get("/telegram", self._search_params(s))
        return data.get("items") or []

    async def item(self, item_id: int, use_cache: bool = False) -> dict:
        key = f"item:{int(item_id)}"
        if use_cache:
            hit = self._cached(key)
            if hit is not None:
                return dict(hit)
        data = await self._get(f"/{int(item_id)}")
        item = data.get("item") or data
        self._store(key, item)
        return item

    async def check_account(self, item_id: int) -> dict:
        data = await self._buy_call(CHECK_PATH.format(int(item_id)), None)
        item = data.get("item")
        if isinstance(item, dict):
            self._store(f"item:{int(item_id)}", item)
        return data

    async def fast_buy(self, item_id: int, price: float,
                       balance_id: int | None = None) -> dict:
        payload: dict = {"price": float(price)}
        if balance_id:
            payload["balance_id"] = int(balance_id)
        data = await self._buy_call(BUY_PATH.format(int(item_id)), payload)
        self.invalidate("item:")
        self.invalidate("balances")
        return data

    async def reset_authorizations(self, item_id: int) -> dict:
        data = await self._post(RESET_PATH.format(int(item_id)))
        self.invalidate(f"item:{int(item_id)}")
        return data

    async def qr_login(self, item_id: int, challenge: str) -> dict:
        return await self._post(QR_PATH.format(int(item_id)),
                                json={"challenge": challenge})

    async def auth_key(self, item_id: int, hint: dict | None = None) -> str:
        found = extract_auth_key(hint)
        if found:
            return found
        try:
            full = await self.item(item_id)
        except LztError as e:
            log.info(f"auth key item {item_id}: {e}")
            return ""
        return extract_auth_key(full)

    async def dc_id(self, item_id: int, hint: dict | None = None) -> int:
        found = extract_dc_id(hint)
        if found:
            return found
        try:
            full = await self.item(item_id)
        except LztError:
            return 0
        return extract_dc_id(full)