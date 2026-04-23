from __future__ import annotations


def is_ad_segment(duration: float | None, absolute_url: str) -> bool:
    del duration
    candidate = absolute_url.lower()
    return any(marker in candidate for marker in ("/adjump/", "/video/adjump/", "/ad-", "/ad/"))
