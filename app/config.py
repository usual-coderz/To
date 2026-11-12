import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)


def _int(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _float(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
LZT_TOKEN: str = os.getenv("LZT_TOKEN", "").strip()
OWNER_IDS: list[int] = [int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()]

API_ID: int = _int(os.getenv("API_ID"), 2040)
API_HASH: str = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = os.getenv("MONGO_DB", "accstore")

LZT_BASE: str = "https://api.lzt.market"
HTTP_TIMEOUT: int = 40
HTTP_RETRIES: int = 3
HTTP_BACKOFF: float = 1.6
TICK: float = 10.0
CYCLE_INTERVAL: float = 20.0
CODE_TIMEOUT: int = 150
LOGIN_CONCURRENCY: int = 4
LOGIN_TRIES: int = 2
LOGIN_RETRY_DELAY: float = 5.0

AUTH_KEY_LEN: int = 256
AUTH_PROBE_TIMEOUT: int = 15
AUTH_PROBE_DEADLINE: float = 45.0
AUTH_DC_SWEEP: int = 3
AUTH_CLEAN_TIMEOUT: int = 30

MAX_SCAN: int = 80
TARGET_HOURS: float = 24.0

CACHE_TTL: float = 20.0

EXPIRY_RETRY: int = 8
EXPIRY_RETRY_DELAY: int = 300
SESSION_RETRY: int = 3
STALE_CLEAN_SECONDS: int = 900

REPORT_KEEP_HOURS: int = 72
PURCHASE_KEEP_DAYS: int = _int(os.getenv("PURCHASE_KEEP_DAYS"), 90)

MAIL_TIMEOUT: int = _int(os.getenv("MAIL_TIMEOUT"), 120)
MAIL_POLL: float = 3.0
MAIL_PROXY_RETRY: float = 6.0
MAIL_PREFER_DOMAIN: str = os.getenv("MAIL_PREFER_DOMAIN", "maxxspace.com").strip().lower()

TWOFA_PASSWORD: str = (os.getenv("TWOFA_PASSWORD", "AccStore2fa").strip() or "AccStore2fa")