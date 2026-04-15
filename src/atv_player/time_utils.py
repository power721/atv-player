from __future__ import annotations

from datetime import datetime


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
