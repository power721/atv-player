from __future__ import annotations


def is_ad_segment(duration: float | None, absolute_url: str) -> bool:
    candidate = absolute_url.lower()
    if duration is not None and duration < 1.0:
        return True
    return any(marker in candidate for marker in ("/adjump/", "/video/adjump/", "/ad-", "/ad/"))
