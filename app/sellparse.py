import re

CODE_REQUEST = re.compile(
    r"(code\s+(?:has\s+been\s+|was\s+|is\s+)?sent|sent\s+(?:the\s+)?(?:code|otp)"
    r"|enter\s+(?:the\s+)?(?:code|otp)|/cancel|verification\s+code)", re.I)
ACCEPT = re.compile(
    r"(account\s+received|received\s+successfully|account\s+accepted"
    r"|account\s+will\s+be\s+(?:automatically\s+)?(?:verified|confirmed))", re.I)
REJECT = re.compile(
    r"(fail|error|invalid|wrong|expired|denied|not\s+received|already\s+(?:sold|added)"
    r"|blocked|try\s+again|cannot|can't|no\s+capacity|limit)", re.I)
PRICE = re.compile(r"price[^\d$]{0,12}\$?\s*(\d{1,6}(?:[.,]\d{1,3})?)", re.I)
SECONDS = re.compile(r"(\d{1,7})\s*(?:s\b|secs?\b|seconds?\b)", re.I)
MINUTES = re.compile(r"(\d{1,5})\s*(?:mins?\b|minutes?\b)", re.I)
OTP = re.compile(r"\b(\d{4,6})\b")
PHONE = re.compile(r"(\+\d{7,16})")
COUNTRY = re.compile(r"([A-Z]{2})")

SKIP_CC = {"OK", "TG", "ID", "PR", "OTP", "US", "AM", "PM", "NA"}


def price_of(text: str) -> float:
    m = PRICE.search(text or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return 0.0


def confirm_of(text: str) -> int:
    m = MINUTES.search(text or "")
    if m:
        return int(m.group(1)) * 60
    m = SECONDS.search(text or "")
    if m:
        return int(m.group(1))
    return 0


def otp_of(text: str) -> str:
    m = OTP.search(text or "")
    return m.group(1) if m else ""


def wants_code(text: str) -> bool:
    return bool(CODE_REQUEST.search(text or ""))


def accepted(text: str) -> bool:
    return bool(ACCEPT.search(text or ""))


def rejected(text: str) -> bool:
    return bool(REJECT.search(text or "")) and not accepted(text)


def phone_of(text: str) -> str:
    m = PHONE.search(text or "")
    return m.group(1) if m else ""


def country_of(text: str) -> str:
    for m in COUNTRY.finditer(text or ""):
        if m.group(1) not in SKIP_CC:
            return m.group(1)
    return ""


def reason_of(text: str, limit: int = 140) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit] or "unknown"