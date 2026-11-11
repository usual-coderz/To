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
TICK: float = 15.0
CODE_TIMEOUT: int = 180
LOGIN_CONCURRENCY: int = 4

MAX_SCAN: int = 80
POLL_MIN: int = 5
POLL_MAX: int = 3600

QUEUE_TARGET_HOURS: float = _float(os.getenv("QUEUE_TARGET_HOURS"), 24.0) or 24.0
TWOFA_PASSWORD: str = os.getenv("TWOFA_PASSWORD", "").strip()

CACHE_TTL: float = 20.0
PAYMENTS_PAGE: int = 10

EXPIRY_RETRY: int = 8
EXPIRY_RETRY_DELAY: int = 300

REPORT_KEEP_HOURS: int = 72

QUICK_COUNTRIES = ["AE", "LY", "JO", "EG", "IQ", "SA", "KW", "QA", "SD", "YE", "MA", "DZ"]