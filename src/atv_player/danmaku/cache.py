from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import time

from atv_player.danmaku.subtitle import render_danmaku_ass
from atv_player.paths import app_cache_dir

DANMAKU_CACHE_MAX_AGE_SECONDS = 3 * 24 * 60 * 60
_DANMAKU_ASS_CACHE_VERSION = "v1"
_DANMAKU_XML_CACHE_VERSION = "v1"


def danmaku_cache_dir() -> Path:
    cache_dir = app_cache_dir() / "danmaku"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def danmaku_ass_cache_path(xml_text: str, line_count: int) -> Path:
    digest = sha256(
        "\0".join((_DANMAKU_ASS_CACHE_VERSION, str(max(1, min(int(line_count), 5))), xml_text)).encode("utf-8")
    ).hexdigest()
    return danmaku_cache_dir() / f"{digest}.ass"


def load_or_create_danmaku_ass_cache(xml_text: str, line_count: int) -> Path | None:
    subtitle_text = render_danmaku_ass(xml_text, line_count=line_count)
    if not subtitle_text:
        return None
    cache_path = danmaku_ass_cache_path(xml_text, line_count)
    if not cache_path.exists():
        cache_path.write_text(subtitle_text, encoding="utf-8")
    return cache_path


def _danmaku_xml_cache_key(name: str, reg_src: str) -> str:
    return sha256("\0".join((_DANMAKU_XML_CACHE_VERSION, name.strip(), reg_src.strip())).encode("utf-8")).hexdigest()


def danmaku_xml_cache_path(name: str, reg_src: str) -> Path:
    return danmaku_cache_dir() / f"{_danmaku_xml_cache_key(name, reg_src)}.xml"


def load_cached_danmaku_xml(name: str, reg_src: str) -> str:
    cache_path = danmaku_xml_cache_path(name, reg_src)
    if not cache_path.exists():
        return ""
    try:
        return cache_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_cached_danmaku_xml(name: str, reg_src: str, xml_text: str) -> Path | None:
    normalized_xml = xml_text.strip()
    if not normalized_xml:
        return None
    cache_path = danmaku_xml_cache_path(name, reg_src)
    cache_path.write_text(normalized_xml, encoding="utf-8")
    return cache_path


def purge_stale_danmaku_cache(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - DANMAKU_CACHE_MAX_AGE_SECONDS
    cache_dir = danmaku_cache_dir()
    for entry in cache_dir.iterdir():
        try:
            if not entry.is_file():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue
