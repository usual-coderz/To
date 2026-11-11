"""Timestamp parsing + human formatting. Everything is UTC epoch seconds.

Parsing is strict about range and shape: anything outside
946684800..4102444800 (2000-01-01 .. 2100-01-01) is rejected, so a price or an
item id can never be mistaken for a date.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone

_DMY = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})"
                  r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?")
_YMD_DOT = re.compile(r"^(\d{4})[./](\d{1,2})[./](\d{1,2})"
                      r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?")
_EPOCH_TEXT = re.compile(r"^\d{9,14}(?:\.\d+)?$")

_MIN_TS = 946_684_800
_MAX_TS = 4_102_444_800


def _epoch(value) -> int | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if v > 1e11:                 # milliseconds
        v /= 1000.0
    return int(v) if _MIN_TS <= v <= _MAX_TS else None


def _mk(y, mo, d, h=0, mi=0, s=0) -> int | None:
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s),
                      tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None
    ts = int(dt.timestamp())
    return ts if _MIN_TS <= ts <= _MAX_TS else None


def _iso(text: str) -> int | None:
    candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = int(dt.timestamp())
    return ts if _MIN_TS <= ts <= _MAX_TS else None


def parse_ts(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _epoch(value)
    text = str(value).strip()
    if not text:
        return None
    if _EPOCH_TEXT.match(text):
        return _epoch(text)

    ts = _iso(text)
    if ts:
        return ts

    m = _YMD_DOT.match(text)
    if m:
        y, mo, d, h, mi, s = m.groups(default=0)
        return _mk(y, mo, d, h, mi, s)

    m = _DMY.match(text)
    if m:
        d, mo, y, h, mi, s = m.groups(default=0)
        return _mk(y, mo, d, h, mi, s)
    return None


def now() -> int:
    return int(time.time())


def age_hours(ts: int | None, at: int | None = None) -> float | None:
    if not ts or not isinstance(ts, int) or not _MIN_TS <= ts <= _MAX_TS:
        return None
    return max(0.0, ((at or now()) - ts) / 3600.0)


def fmt_age(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    try:
        minutes = int(max(0.0, float(hours)) * 60)
    except (TypeError, ValueError):
        return "unknown"
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    if h < 24:
        return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h:02d}h"


def fmt_dt(ts: int | None) -> str:
    if not ts or not isinstance(ts, int) or not _MIN_TS <= ts <= _MAX_TS:
        return "—"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_left(expires_at: int | None) -> str:
    if not expires_at or not isinstance(expires_at, int):
        return "—"
    left = expires_at - now()
    return "cleaning" if left <= 0 else fmt_age(left / 3600.0)