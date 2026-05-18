from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

import atv_player.ui.poster_loader as poster_loader_module
from atv_player.ui.poster_loader import (
    load_remote_poster_image,
    set_http_get_loader,
)


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


def _png_bytes(width: int = 20, height: int = 40) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODeviceBase

    png = QImage(width, height, QImage.Format.Format_RGB32)
    png.fill(0x00FF00)
    data = QByteArray()
    qbuffer = QBuffer(data)
    qbuffer.open(QIODeviceBase.OpenModeFlag.WriteOnly)
    png.save(qbuffer, "PNG")
    return bytes(data)


@pytest.fixture(autouse=True)
def _reset_loader():
    yield
    set_http_get_loader(None)


def test_set_http_get_loader_supplies_default_when_get_not_passed(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse(_png_bytes())

    set_http_get_loader(lambda: fake_get)

    loaded = load_remote_poster_image(
        "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
        QSize(90, 130),
    )

    assert loaded is not None
    assert calls == ["https://img3.doubanio.com/view/photo/m/public/p123.jpg"]


def test_explicit_get_argument_takes_precedence_over_loader(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)
    loader_calls: list[str] = []
    explicit_calls: list[str] = []

    def loader_get(url: str, **kwargs: Any) -> FakeResponse:
        loader_calls.append(url)
        return FakeResponse(_png_bytes())

    def explicit_get(url: str, **kwargs: Any) -> FakeResponse:
        explicit_calls.append(url)
        return FakeResponse(_png_bytes())

    set_http_get_loader(lambda: loader_get)

    loaded = load_remote_poster_image(
        "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
        QSize(90, 130),
        get=explicit_get,
    )

    assert loaded is not None
    assert explicit_calls == ["https://img3.doubanio.com/view/photo/m/public/p123.jpg"]
    assert loader_calls == []


def test_falls_back_to_httpx_get_when_loader_unset(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)
    monkeypatch.setattr(poster_loader_module, "_http_get_loader", None, raising=False)
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse(_png_bytes())

    import httpx as httpx_module

    monkeypatch.setattr(httpx_module, "get", fake_get)

    loaded = load_remote_poster_image(
        "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
        QSize(90, 130),
    )

    assert loaded is not None
    assert calls == ["https://img3.doubanio.com/view/photo/m/public/p123.jpg"]


def test_falls_back_to_httpx_get_when_loader_returns_none(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse(_png_bytes())

    import httpx as httpx_module

    monkeypatch.setattr(httpx_module, "get", fake_get)
    set_http_get_loader(lambda: None)

    loaded = load_remote_poster_image(
        "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
        QSize(90, 130),
    )

    assert loaded is not None
    assert calls == ["https://img3.doubanio.com/view/photo/m/public/p123.jpg"]
