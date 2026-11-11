import asyncio
import logging
import time

import httpx

from app.config import (CACHE_TTL, HTTP_BACKOFF, HTTP_RETRIES, HTTP_TIMEOUT, LZT_BASE,
                        LZT_TOKEN)
from app.utils.filters import search_query
from app.utils.money import parse_balances, parse_user, same_currency

log = logging.getLogger("lzt")

PAYMENT_PATHS = ("/user/payments", "/payments", "/balance/payments")
AUTH_KEY_FIELDS = ("telegram_auth_key", "auth_key", "telegram_session", "session",
                   "authKey", "auth_key_json")

RESET_PATHS = ("/{}/reset-authorizations", "/{}/telegram-reset-authorizations",
               "/telegram/{}/reset-authorizations", "/{}/reset-sessions",
               "/{}/telegram-reset-sessions")
AUTH_KEY_PATHS = ("/{}/telegram-auth-key", "/{}/auth-key", "/{}/telegram-session",
                  "/{}/telegram-auth-key-json")

SOFT_STATUS = (403, 404, 405)


class LztError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


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


def _dig_key(node, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(node, dict):
        for key in AUTH_KEY_FIELDS:
            raw = node.get(key)
            if isinstance(raw, str) and len(raw) > 40:
                return raw.strip()
        for value in node.values():
            found = _dig_key(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _dig_key(value, depth + 1)
            if found:
                return found
    return ""


class LztClient:
    def __init__(self, token: str = LZT_TOKEN):
        self._token = token
        self._cache: dict[str, tuple[float, object]] = {}
        self._reset_path: str = ""
        self._code_verb: str = ""
        self._http = httpx.AsyncClient(
            base_url=LZT_BASE,
            timeout=HTTP_TIMEOUT,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json",
                     "User-Agent": "AccStore/2.3"},
        )

    async def close(self):
        try:
            await self._http.aclose()
        except Exception:
            pass

    def invalidate(self):
        self._cache.clear()

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

    async def _probe(self, templates: tuple[str, ...], item_id: int,
                     verbs: tuple[str, ...]) -> tuple[dict, str]:
        last: LztError | None = None
        for template in templates:
            path = template.format(int(item_id))
            for verb in verbs:
                try:
                    data = await self._request(verb, path)
                except LztError as e:
                    last = e
                    if e.status in SOFT_STATUS:
                        continue
                    raise
                return data, template
        raise last or LztError("endpoint not found", 404)

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

    async def item(self, item_id: int) -> dict:
        data = await self._get(f"/{int(item_id)}")
        return data.get("item") or data

    async def check_account(self, item_id: int) -> dict:
        path = f"/{int(item_id)}/check-account"
        last: LztError | None = None
        for attempt in range(2):
            try:
                return await self._post(path)
            except LztError as e:
                last = e
                if e.status not in (403, 429) or attempt:
                    raise
                await asyncio.sleep(1.5)
        raise last or LztError("check failed")

    async def fast_buy(self, item_id: int, price: float,
                       balance_id: int | None = None) -> dict:
        params = {"price": float(price)}
        if balance_id:
            params["balance_id"] = int(balance_id)
        data = await self._post(f"/{int(item_id)}/fast-buy", params=params)
        self.invalidate()
        return data

    async def reset_authorizations(self, item_id: int) -> dict:
        if self._reset_path:
            try:
                return await self._request("POST", self._reset_path.format(int(item_id)))
            except LztError as e:
                if e.status not in SOFT_STATUS:
                    raise
                self._reset_path = ""
        data, template = await self._probe(RESET_PATHS, item_id, ("POST", "GET"))
        self._reset_path = template
        log.info(f"reset endpoint resolved: {template}")
        return data

    async def telegram_login_code(self, item_id: int) -> tuple[str, str]:
        path = f"/{int(item_id)}/telegram-login-code"
        verbs = [self._code_verb] if self._code_verb else []
        verbs += [v for v in ("GET", "POST") if v not in verbs]
        last: LztError | None = None
        for verb in verbs:
            try:
                data = await self._request(verb, path)
            except LztError as e:
                last = e
                if e.status in SOFT_STATUS:
                    continue
                raise
            if not self._code_verb:
                self._code_verb = verb
            item = data.get("item") or data
            value = str(item.get("code") or "").strip()
            mode = str(item.get("mode") or "").strip().lower()
            if value or not self._code_verb:
                return value, mode
        if last and last.status not in SOFT_STATUS:
            log.info(f"code {item_id}: {last}")
        return "", ""

    async def auth_key(self, item_id: int) -> str:
        value, mode = await self.telegram_login_code(item_id)
        if value and mode in ("auth_key", "authkey", "session", "key"):
            return value

        for template in AUTH_KEY_PATHS:
            try:
                data = await self._request("GET", template.format(int(item_id)))
            except LztError as e:
                if e.status in SOFT_STATUS:
                    continue
                raise
            found = _dig_key(data)
            if found:
                return found

        try:
            item = await self.item(item_id)
        except LztError:
            return ""
        return _dig_key(item)