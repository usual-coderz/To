import os
import time
import urllib.parse

from app.config import DATA_DIR

_RAW = """
148.135.177.250:5784:versenautobot:sliamhxywwwsf
191.96.254.169:6216:versenautobot:sliamhxywwwsf
198.37.98.183:5713:versenautobot:sliamhxywwwsf
136.0.180.126:6147:versenautobot:sliamhxywwwsf
172.120.102.183:6343:versenautobot:sliamhxywwwsf
64.137.37.104:6694:versenautobot:sliamhxywwwsf
82.22.211.40:5848:versenautobot:sliamhxywwwsf
45.41.173.98:6465:versenautobot:sliamhxywwwsf
2.57.21.151:7388:versenautobot:sliamhxywwwsf
172.121.139.194:5373:versenautobot:sliamhxywwwsf
45.38.122.36:6944:versenautobot:sliamhxywwwsf
82.23.206.205:6011:versenautobot:sliamhxywwwsf
148.135.179.27:6086:versenautobot:sliamhxywwwsf
31.193.188.126:7143:versenautobot:sliamhxywwwsf
161.123.65.124:6833:versenautobot:sliamhxywwwsf
104.143.245.167:6407:versenautobot:sliamhxywwwsf
64.137.96.95:6662:versenautobot:sliamhxywwwsf
82.23.230.220:8024:versenautobot:sliamhxywwwsf
206.206.71.243:5883:versenautobot:sliamhxywwwsf
206.206.124.11:6592:versenautobot:sliamhxywwwsf
136.0.105.59:6069:versenautobot:sliamhxywwwsf
149.57.17.245:5713:versenautobot:sliamhxywwwsf
45.61.118.220:5917:versenautobot:sliamhxywwwsf
107.174.136.125:6067:versenautobot:sliamhxywwwsf
173.214.176.166:6137:versenautobot:sliamhxywwwsf
151.245.205.138:5333:versenautobot:sliamhxywwwsf
192.186.186.218:6260:versenautobot:sliamhxywwwsf
142.111.161.109:6480:versenautobot:sliamhxywwwsf
45.43.65.177:6691:versenautobot:sliamhxywwwsf
193.25.167.59:5590:versenautobot:sliamhxywwwsf
107.174.4.7:5976:versenautobot:sliamhxywwwsf
82.29.233.253:8110:versenautobot:sliamhxywwwsf
142.111.227.45:6240:versenautobot:sliamhxywwwsf
185.171.254.125:6157:versenautobot:sliamhxywwwsf
142.111.227.219:6414:versenautobot:sliamhxywwwsf
171.22.251.90:5620:versenautobot:sliamhxywwwsf
154.6.11.163:5632:versenautobot:sliamhxywwwsf
45.83.59.31:7047:versenautobot:sliamhxywwwsf
45.83.57.114:6631:versenautobot:sliamhxywwwsf
45.39.15.225:6655:versenautobot:sliamhxywwwsf
23.129.254.80:6062:versenautobot:sliamhxywwwsf
64.137.59.231:6824:versenautobot:sliamhxywwwsf
82.21.245.28:6352:versenautobot:sliamhxywwwsf
104.253.180.42:6455:versenautobot:sliamhxywwwsf
92.112.238.164:7043:versenautobot:sliamhxywwwsf
206.232.103.189:6346:versenautobot:sliamhxywwwsf
151.245.211.5:6200:versenautobot:sliamhxywwwsf
206.206.73.85:6701:versenautobot:sliamhxywwwsf
104.245.244.43:6483:versenautobot:sliamhxywwwsf
94.176.212.11:6527:versenautobot:sliamhxywwwsf
45.131.102.26:5678:versenautobot:sliamhxywwwsf
142.202.254.127:6105:versenautobot:sliamhxywwwsf
45.115.195.162:6140:versenautobot:sliamhxywwwsf
92.112.91.57:6292:versenautobot:sliamhxywwwsf
104.239.91.116:5840:versenautobot:sliamhxywwwsf
82.25.213.7:5359:versenautobot:sliamhxywwwsf
104.232.209.29:5987:versenautobot:sliamhxywwwsf
172.120.120.145:5300:versenautobot:sliamhxywwwsf
104.222.185.127:5690:versenautobot:sliamhxywwwsf
45.43.94.241:7491:versenautobot:sliamhxywwwsf
216.74.80.63:6635:versenautobot:sliamhxywwwsf
"""

SCHEMES = ("socks5h", "socks5", "socks4", "http", "https")
DEFAULT_SCHEME = "http"

_COOLDOWN: dict[str, float] = {}
POOL: list[str] = []


def _lines(raw: str) -> list[str]:
    cleaned = raw.replace(",", "\n").replace(";", "\n")
    return [x.strip().strip('"').strip("'") for x in cleaned.splitlines() if x.strip()]


def _split_scheme(raw: str) -> tuple[str, str]:
    low = raw.lower()
    for scheme in SCHEMES:
        prefix = scheme + "://"
        if low.startswith(prefix):
            return scheme, raw[len(prefix):]
    return "", raw


def normalize(raw: str) -> str:
    text = raw.strip().strip('"').strip("'")
    if not text:
        return ""

    scheme, text = _split_scheme(text)
    scheme = DEFAULT_SCHEME if not scheme else scheme
    if scheme == "socks5h":
        scheme = "socks5"

    user = ""
    pwd = ""
    if "@" in text:
        creds, _, text = text.rpartition("@")
        user, _, pwd = creds.partition(":")
    else:
        parts = text.split(":")
        if len(parts) == 4:
            text, user, pwd = f"{parts[0]}:{parts[1]}", parts[2], parts[3]
        elif len(parts) == 3:
            text, user = f"{parts[0]}:{parts[1]}", parts[2]

    host, _, port = text.rpartition(":")
    if not host:
        host, port = text, "80"
    host = host.strip().strip("[]")
    if not host:
        return ""
    try:
        number = int(port)
    except ValueError:
        return ""
    if not 1 <= number <= 65535:
        return ""

    if user:
        cred = urllib.parse.quote(user, safe="")
        if pwd:
            cred += ":" + urllib.parse.quote(pwd, safe="")
        return f"{scheme}://{cred}@{host}:{number}"
    return f"{scheme}://{host}:{number}"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _source() -> str:
    path = os.path.join(DATA_DIR, "proxies.txt")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return os.getenv("PROXIES") or _RAW


def reload() -> list[str]:
    global POOL
    POOL = _dedupe([normalize(x) for x in _lines(_source())])
    _COOLDOWN.clear()
    return POOL


def get(index: int) -> str:
    if not POOL:
        return ""
    return POOL[index % len(POOL)]


def rotate(index: int) -> tuple[str, int]:
    if not POOL:
        return "", index
    i = index % len(POOL)
    return POOL[i], i + 1


def mark(proxy: str, seconds: float):
    if proxy:
        _COOLDOWN[proxy] = time.time() + max(0.0, float(seconds))


def pick(index: int) -> tuple[str, int]:
    if not POOL:
        return "", index
    now = time.time()
    for _ in range(len(POOL)):
        proxy, index = rotate(index)
        if _COOLDOWN.get(proxy, 0.0) <= now:
            return proxy, index
    return "", index


def healthy() -> list[str]:
    now = time.time()
    return [p for p in POOL if _COOLDOWN.get(p, 0.0) <= now]


reload()