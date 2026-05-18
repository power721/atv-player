from __future__ import annotations

from typing import Any

import pytest

import atv_player.ui.player_window as player_window_module
from atv_player.ui.player_window import set_player_window_http_get_loader


class FakeResponse:
    def __init__(self, text: str = "stub") -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_loader():
    yield
    set_player_window_http_get_loader(None)


def test_player_window_http_get_loader_default_falls_back_to_httpx(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse()

    import httpx as httpx_module

    monkeypatch.setattr(httpx_module, "get", fake_get)
    set_player_window_http_get_loader(None)

    get = player_window_module._resolve_http_get()
    get("https://example.com/")

    assert calls == ["https://example.com/"]


def test_player_window_http_get_loader_returns_registered() -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse()

    set_player_window_http_get_loader(lambda: fake_get)

    get = player_window_module._resolve_http_get()
    get("https://example.com/")

    assert calls == ["https://example.com/"]


def test_player_window_http_get_loader_returning_none_falls_back(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse()

    import httpx as httpx_module

    monkeypatch.setattr(httpx_module, "get", fake_get)
    set_player_window_http_get_loader(lambda: None)

    get = player_window_module._resolve_http_get()
    get("https://example.com/")

    assert calls == ["https://example.com/"]
