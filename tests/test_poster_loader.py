from pathlib import Path

import atv_player.ui.poster_loader as poster_loader_module
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

from atv_player.ui.poster_loader import (
    build_poster_request_headers,
    load_remote_poster_image,
    normalize_poster_url,
)


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


def _png_bytes(width: int = 20, height: int = 40) -> bytes:
    png = QImage(width, height, QImage.Format.Format_RGB32)
    png.fill(0x00FF00)

    from PySide6.QtCore import QBuffer, QByteArray, QIODeviceBase

    data = QByteArray()
    qbuffer = QBuffer(data)
    qbuffer.open(QIODeviceBase.OpenModeFlag.WriteOnly)
    png.save(qbuffer, "PNG")
    return bytes(data)


def test_normalize_poster_url_upgrades_douban_ratio_path() -> None:
    result = normalize_poster_url("https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg")
    assert result == "https://img3.doubanio.com/view/photo/m/public/p123.jpg"


def test_build_poster_request_headers_uses_site_specific_referers() -> None:
    assert build_poster_request_headers("https://img3.doubanio.com/view/photo/m/public/p123.jpg")["Referer"] == "https://movie.douban.com/"
    assert build_poster_request_headers("https://i.ytimg.com/vi/123/maxresdefault.jpg")["Referer"] == "https://www.youtube.com/"
    assert build_poster_request_headers("https://cc.163.com/cover.png")["Referer"] == "https://cc.163.com/"


def test_load_remote_poster_image_scales_downloaded_image() -> None:
    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        return FakeResponse(_png_bytes())

    loaded = load_remote_poster_image(
        "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
        QSize(90, 130),
        get=fake_get,
    )

    assert loaded is not None
    assert loaded.isNull() is False
    assert loaded.width() <= 90
    assert loaded.height() <= 130


def test_load_remote_poster_image_reuses_cached_file(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    cache_dir.mkdir()
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)

    image_url = "https://img3.doubanio.com/view/photo/m/public/p123.jpg"
    cache_path = poster_loader_module.poster_cache_path(image_url)
    cache_path.write_bytes(_png_bytes())

    def fail_get(*args, **kwargs):
        raise AssertionError("network should not be used when cache file exists")

    loaded = load_remote_poster_image(image_url, QSize(90, 130), get=fail_get)

    assert loaded is not None
    assert loaded.isNull() is False
    assert loaded.width() <= 90
    assert loaded.height() <= 130


def test_load_remote_poster_image_writes_downloaded_bytes_to_cache(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)
    poster_bytes = _png_bytes()

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        return FakeResponse(poster_bytes)

    image_url = "https://img3.doubanio.com/view/photo/m/public/p123.jpg"
    loaded = load_remote_poster_image(image_url, QSize(90, 130), get=fake_get)
    cache_path = poster_loader_module.poster_cache_path(image_url)

    assert loaded is not None
    assert loaded.isNull() is False
    assert cache_path.read_bytes() == poster_bytes


def test_load_remote_poster_image_refetches_when_cached_bytes_are_corrupt(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    cache_dir.mkdir()
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)

    image_url = "https://img3.doubanio.com/view/photo/m/public/p123.jpg"
    poster_loader_module.poster_cache_path(image_url).write_bytes(b"not-an-image")
    poster_bytes = _png_bytes()
    calls: list[str] = []

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        calls.append(url)
        return FakeResponse(poster_bytes)

    loaded = load_remote_poster_image(image_url, QSize(90, 130), get=fake_get)

    assert loaded is not None
    assert loaded.isNull() is False
    assert calls == ["https://img3.doubanio.com/view/photo/m/public/p123.jpg"]
    assert poster_loader_module.poster_cache_path(image_url).read_bytes() == poster_bytes


def test_load_remote_poster_image_refetches_when_cache_read_fails(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    cache_dir.mkdir()
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)

    image_url = "https://img3.doubanio.com/view/photo/m/public/p123.jpg"
    cache_path = poster_loader_module.poster_cache_path(image_url)
    cache_path.write_bytes(_png_bytes())
    poster_bytes = _png_bytes()
    calls: list[str] = []
    original_read_bytes = Path.read_bytes

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        calls.append(url)
        return FakeResponse(poster_bytes)

    def fake_read_bytes(self: Path) -> bytes:
        if self == cache_path:
            raise OSError("permission denied")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    loaded = load_remote_poster_image(image_url, QSize(90, 130), get=fake_get)

    assert loaded is not None
    assert loaded.isNull() is False
    assert calls == ["https://img3.doubanio.com/view/photo/m/public/p123.jpg"]


def test_load_remote_poster_image_returns_image_when_cache_write_fails(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)
    poster_bytes = _png_bytes()

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        return FakeResponse(poster_bytes)

    monkeypatch.setattr(
        poster_loader_module,
        "_write_poster_cache_bytes",
        lambda cache_path, image_bytes: (_ for _ in ()).throw(OSError("disk full")),
    )

    loaded = load_remote_poster_image(
        "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
        QSize(90, 130),
        get=fake_get,
    )

    assert loaded is not None
    assert loaded.isNull() is False
