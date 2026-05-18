from __future__ import annotations

from typing import Any

import pytest

import atv_player.ui.main_window as main_window_module
from atv_player.ui.main_window import (
    load_360_hot_searches,
    load_360_search_suggestions,
    load_direct_parse_detail,
    load_iqiyi_hot_search_sections,
    load_tencent_hot_search_sections,
    set_main_window_http_get_loader,
    set_main_window_http_post_loader,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_main_window_loaders():
    yield
    set_main_window_http_get_loader(None)
    set_main_window_http_post_loader(None)


def test_load_direct_parse_detail_uses_registered_loader() -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse({"list": []})

    set_main_window_http_get_loader(lambda: fake_get)

    payload = load_direct_parse_detail("https://example.com/video")

    assert payload == {"list": []}
    assert calls == [main_window_module._DIRECT_PARSE_DETAIL_API]


def test_load_360_hot_searches_uses_registered_loader() -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse({"data": [{"title": "热门"}]})

    set_main_window_http_get_loader(lambda: fake_get)

    result = load_360_hot_searches()

    assert result == [{"title": "热门", "query": "热门"}]
    assert calls == [main_window_module._HOTKEY_360_API]


def test_load_iqiyi_hot_search_sections_uses_registered_loader() -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse({})

    set_main_window_http_get_loader(lambda: fake_get)

    load_iqiyi_hot_search_sections()

    assert calls == [main_window_module._HOTKEY_IQIYI_API]


def test_load_360_search_suggestions_uses_registered_loader() -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse({"result": []})

    set_main_window_http_get_loader(lambda: fake_get)

    load_360_search_suggestions("keyword")

    assert calls == [main_window_module._SUGGESTION_360_API]


def test_load_tencent_hot_search_sections_uses_registered_post_loader() -> None:
    calls: list[str] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse({})

    set_main_window_http_post_loader(lambda: fake_post)

    load_tencent_hot_search_sections()

    assert calls == [main_window_module._HOTKEY_TENCENT_API]


def test_get_loader_returning_none_falls_back_to_httpx(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse({})

    import httpx as httpx_module

    monkeypatch.setattr(httpx_module, "get", fake_get)
    set_main_window_http_get_loader(lambda: None)

    load_360_search_suggestions("kw")

    assert calls == [main_window_module._SUGGESTION_360_API]


def test_post_loader_returning_none_falls_back_to_httpx(monkeypatch) -> None:
    calls: list[str] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append(url)
        return FakeResponse({})

    import httpx as httpx_module

    monkeypatch.setattr(httpx_module, "post", fake_post)
    set_main_window_http_post_loader(lambda: None)

    load_tencent_hot_search_sections()

    assert calls == [main_window_module._HOTKEY_TENCENT_API]
