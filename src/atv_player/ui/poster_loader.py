from __future__ import annotations

from io import BytesIO
from hashlib import sha256
from pathlib import Path

import httpx
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage

from atv_player.paths import app_cache_dir

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError:  # pragma: no cover - exercised when optional decoder deps are absent.
    Image = None
    ImageOps = None
    UnidentifiedImageError = OSError

try:
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - exercised when optional decoder deps are absent.
    register_heif_opener = None
else:
    register_heif_opener()

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


def poster_cache_dir() -> Path:
    cache_dir = app_cache_dir() / "posters"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def poster_cache_path(image_url: str) -> Path:
    normalized_url = normalize_poster_url(image_url)
    digest = sha256(normalized_url.encode("utf-8")).hexdigest()
    return poster_cache_dir() / f"{digest}.img"


def _write_poster_cache_bytes(cache_path: Path, image_bytes: bytes) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(image_bytes)


def _decode_image_fallback_from_bytes(image_bytes: bytes) -> QImage | None:
    if Image is None or ImageOps is None:
        return None

    try:
        with Image.open(BytesIO(image_bytes)) as pil_image:
            normalized = ImageOps.exif_transpose(pil_image)
            has_alpha = "A" in normalized.getbands()
            converted = normalized.convert("RGBA" if has_alpha else "RGB")
            encoded = BytesIO()
            converted.save(encoded, format="PNG")
    except (OSError, UnidentifiedImageError, ValueError):
        return None

    image = QImage()
    image.loadFromData(encoded.getvalue())
    if image.isNull():
        return None
    return image


def _decode_image_from_bytes(image_bytes: bytes) -> QImage | None:
    image = QImage()
    image.loadFromData(image_bytes)
    if image.isNull():
        image = _decode_image_fallback_from_bytes(image_bytes)
    if image is None or image.isNull():
        return None
    return image


def _load_scaled_image_from_bytes(image_bytes: bytes, target_size: QSize) -> QImage | None:
    image = _decode_image_from_bytes(image_bytes)
    if image is None:
        return None
    return image.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _load_cached_poster_image(cache_path: Path, target_size: QSize) -> QImage | None:
    try:
        cached_bytes = cache_path.read_bytes()
    except OSError:
        return None
    return _load_scaled_image_from_bytes(cached_bytes, target_size)


def load_local_poster_image(source: str, target_size: QSize) -> QImage | None:
    if not source:
        return None
    source_path = Path(source)
    if not source_path.is_file():
        return None

    image = QImage(str(source_path))
    if image.isNull():
        try:
            return _load_scaled_image_from_bytes(source_path.read_bytes(), target_size)
        except OSError:
            return None
    return image.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def load_remote_poster_image(
    image_url: str,
    target_size: QSize,
    timeout: float = POSTER_REQUEST_TIMEOUT_SECONDS,
    get=httpx.get,
) -> QImage | None:
    normalized_url = normalize_poster_url(image_url)
    if not normalized_url:
        return None

    cache_path = poster_cache_path(normalized_url)
    cached_image = _load_cached_poster_image(cache_path, target_size)
    if cached_image is not None:
        return cached_image

    try:
        response = get(
            normalized_url,
            headers=build_poster_request_headers(normalized_url),
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception:
        return None

    image = _load_scaled_image_from_bytes(response.content, target_size)
    if image is None:
        return None
    try:
        _write_poster_cache_bytes(cache_path, response.content)
    except OSError:
        pass
    return image
