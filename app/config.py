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

USERBOT_SESSION: str = os.getenv("USERBOT_SESSION", "").strip()
USERBOT_PROXY: str = os.getenv("USERBOT_PROXY", "").strip()

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB: str = os.getenv("MONGO_DB", "accstore")

LZT_BASE: str = "https://api.lzt.market"
HTTP_TIMEOUT: int = 40
HTTP_RETRIES: int = 3
HTTP_BACKOFF: float = 1.6

TICK: float = 10.0
CYCLE_INTERVAL: float = 20.0
MAX_SCAN: int = 80
TARGET_HOURS: float = 24.0
CACHE_TTL: float = 20.0

LOGIN_CONCURRENCY: int = _int(os.getenv("LOGIN_CONCURRENCY"), 4)
LOGIN_TRIES: int = 2
LOGIN_RETRY_DELAY: float = 5.0

AUTH_KEY_LEN: int = 256
AUTH_PROBE_TIMEOUT: int = 15
AUTH_PROBE_DEADLINE: float = 45.0
AUTH_DC_SWEEP: int = 3
AUTH_CLEAN_TIMEOUT: int = 30

EXPIRY_RETRY: int = 8
EXPIRY_RETRY_DELAY: int = 300
SESSION_RETRY: int = 3
STALE_CLEAN_SECONDS: int = 900

REPORT_KEEP_HOURS: int = 72
PURCHASE_KEEP_DAYS: int = _int(os.getenv("PURCHASE_KEEP_DAYS"), 90)

CODE_TIMEOUT: int = _int(os.getenv("CODE_TIMEOUT"), 150)
MAIL_TIMEOUT: int = _int(os.getenv("MAIL_TIMEOUT"), CODE_TIMEOUT)
MAIL_POLL: float = _float(os.getenv("MAIL_POLL"), 3.0)
MAIL_PROXY_RETRY: float = _float(os.getenv("MAIL_PROXY_RETRY"), 8.0)
MAIL_PREFER_DOMAIN: str = os.getenv("MAIL_PREFER_DOMAIN", "").strip().lower()
MAIL_USE_PROXY: int = _int(os.getenv("MAIL_USE_PROXY"), 0)
MAIL_PROVIDERS: str = os.getenv(
    "MAIL_PROVIDERS", "guerrilla,dropmail,mailtm,mailgw,plus").strip().lower()

TWOFA_PASSWORD: str = (os.getenv("TWOFA_PASSWORD", "AccStore2fa").strip() or "AccStore2fa")

SELL_CONCURRENCY: int = _int(os.getenv("SELL_CONCURRENCY"), 2)
SELL_OTP_TIMEOUT: int = _int(os.getenv("SELL_OTP_TIMEOUT"), 180)
SELL_REPLY_TIMEOUT: int = _int(os.getenv("SELL_REPLY_TIMEOUT"), 240)
SELL_CONFIRM_DEFAULT: int = _int(os.getenv("SELL_CONFIRM_DEFAULT"), 600)
SELL_CONFIRM_MIN: int = 300
SELL_CONFIRM_MAX: int = 7200
SELL_MIN_PROFIT: float = _float(os.getenv("SELL_MIN_PROFIT"), 0.0)

PRICE_TTL: float = _float(os.getenv("PRICE_TTL"), 900.0)
PRICE_REFRESH: float = _float(os.getenv("PRICE_REFRESH"), 240.0)
CAP_REFRESH: float = _float(os.getenv("CAP_REFRESH"), 120.0)
PROBE_TIMEOUT: float = _float(os.getenv("PROBE_TIMEOUT"), 25.0)

CAP_COMMAND: str = "/cap"

SCHEDULE_TZ: str = os.getenv("SCHEDULE_TZ", "Asia/Kolkata").strip()
SCHEDULE_HOURS: tuple[int, ...] = tuple(
    sorted({_int(x, 0) for x in os.getenv("SCHEDULE_HOURS", "0,12,16").split(",")
            if x.strip().isdigit() and 0 <= _int(x, 0) <= 23}))
INR_RATE: float = _float(os.getenv("INR_RATE"), 83.0)

LION_BOT: str = os.getenv("LION_BOT", "TGLionAPI_bot").strip().lstrip("@")
LION_START: str = os.getenv("LION_START", "Prices").strip()
TGGET_BOT: str = os.getenv("TGGET_BOT", "tg_get2_bot").strip().lstrip("@")
CM_BOT: str = os.getenv("CM_BOT", "CM_Receiver6_Bot").strip().lstrip("@")