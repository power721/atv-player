from __future__ import annotations

import time
from datetime import datetime

_MIN_PLAUSIBLE_UNIX_TIMESTAMP = 946684800
_REFRESH_STALE_SECONDS = 4 * 60 * 60


def normalize_refresh_timestamp(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        timestamp = int(text)
        if timestamp >= 1_000_000_000_000:
            timestamp //= 1000
        if timestamp < _MIN_PLAUSIBLE_UNIX_TIMESTAMP:
            return 0
        return timestamp
    return 0


def format_local_datetime(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    if text.isdigit():
        timestamp = int(text)
        if timestamp >= 1_000_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def format_refresh_timestamp(value: object) -> str:
    timestamp = normalize_refresh_timestamp(value)
    if not timestamp:
        return ""
    return format_local_datetime(str(timestamp))


def is_refresh_stale(value: object, *, now: int | None = None) -> bool:
    timestamp = normalize_refresh_timestamp(value)
    if not timestamp:
        return True
    current = int(time.time()) if now is None else int(now)
    return current - timestamp >= _REFRESH_STALE_SECONDS
