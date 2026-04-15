from __future__ import annotations

import httpx
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage

POSTER_REQUEST_TIMEOUT_SECONDS = 10.0
POSTER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
DEFAULT_POSTER_REFERER = "https://movie.douban.com/"


def normalize_poster_url(source: str) -> str:
    normalized = source or ""
    if "doubanio.com" in normalized:
        normalized = normalized.replace("s_ratio_poster", "m")
    return normalized


def build_poster_request_headers(image_url: str) -> dict[str, str]:
    referer = DEFAULT_POSTER_REFERER
    if "ytimg.com" in image_url:
        referer = "https://www.youtube.com/"
    elif "netease.com" in image_url or "163.com" in image_url:
        referer = "https://cc.163.com/"
    return {
        "Referer": referer,
        "User-Agent": POSTER_USER_AGENT,
    }


def load_remote_poster_image(
    image_url: str,
    target_size: QSize,
    timeout: float = POSTER_REQUEST_TIMEOUT_SECONDS,
    get=httpx.get,
) -> QImage | None:
    try:
        response = get(
            image_url,
            headers=build_poster_request_headers(image_url),
            timeout=timeout,
        )
        response.raise_for_status()
    except Exception:
        return None
    image = QImage()
    image.loadFromData(response.content)
    if image.isNull():
        return None
    return image.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
