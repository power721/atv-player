from __future__ import annotations

import types
from typing import Any

import atv_player.plugins.compat.base.spider as compat_spider_module
from atv_player.plugins.compat.base.spider import (
    Spider,
    set_proxy_decider_loader,
    set_session_loader,
)
from atv_player.network_proxy import ProxyConfig, ProxyDecider


class _FakeResponse:
    def __init__(self) -> None:
        self.encoding = ""
        self._content = b""

    @property
    def content(self) -> bytes:
        return self._content

    def close(self) -> None:
        pass


class _FakeSession:
    def __init__(self) -> None:
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []
        self.responses: list[_FakeResponse] = []

    def _record(self, calls: list[dict[str, Any]], url: str, kwargs: dict[str, Any]) -> _FakeResponse:
        calls.append({"url": url, **kwargs})
        response = _FakeResponse()
        self.responses.append(response)
        return response

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._record(self.get_calls, url, kwargs)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._record(self.post_calls, url, kwargs)


def _reset_loaders() -> None:
    set_session_loader(None)
    set_proxy_decider_loader(None)


def test_fetch_uses_injected_session_when_loader_is_set(monkeypatch) -> None:
    session = _FakeSession()
    set_session_loader(lambda: session)
    monkeypatch.setattr(
        compat_spider_module,
        "requests",
        types.SimpleNamespace(get=lambda *a, **k: pytest_fail("requests.get must not be called")),
        raising=False,
    )

    try:
        Spider().fetch("https://example.com/api", headers={"X": "1"}, timeout=5)
    finally:
        _reset_loaders()

    assert len(session.get_calls) == 1
    call = session.get_calls[0]
    assert call["url"] == "https://example.com/api"
    assert call["headers"] == {"X": "1"}
    assert call["timeout"] == 5


def test_post_uses_injected_session_when_loader_is_set(monkeypatch) -> None:
    session = _FakeSession()
    set_session_loader(lambda: session)
    monkeypatch.setattr(
        compat_spider_module,
        "requests",
        types.SimpleNamespace(post=lambda *a, **k: pytest_fail("requests.post must not be called")),
        raising=False,
    )

    try:
        Spider().post("https://example.com/api", json={"x": 1}, timeout=9)
    finally:
        _reset_loaders()

    assert len(session.post_calls) == 1
    call = session.post_calls[0]
    assert call["url"] == "https://example.com/api"
    assert call["json"] == {"x": 1}
    assert call["timeout"] == 9


def test_fetch_passes_per_request_proxies_from_decider(monkeypatch) -> None:
    session = _FakeSession()
    decider = ProxyDecider(
        ProxyConfig(
            mode="http",
            proxy_url="http://127.0.0.1:7890",
            bypass_rules=["localhost"],
        )
    )
    set_session_loader(lambda: session)
    set_proxy_decider_loader(lambda: decider)

    try:
        Spider().fetch("https://api.example.com/")
        Spider().fetch("http://localhost/api")
    finally:
        _reset_loaders()

    assert session.get_calls[0]["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    assert session.get_calls[1]["proxies"] == {"http": None, "https": None}


def test_fetch_falls_back_to_requests_when_session_loader_is_none(monkeypatch) -> None:
    set_session_loader(None)
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse()

    monkeypatch.setattr(
        compat_spider_module,
        "requests",
        types.SimpleNamespace(get=fake_get),
        raising=False,
    )

    try:
        Spider().fetch("https://example.com/api")
    finally:
        _reset_loaders()

    assert calls == ["https://example.com/api"]


def test_fetch_falls_back_to_requests_when_loader_returns_none(monkeypatch) -> None:
    set_session_loader(lambda: None)
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse()

    monkeypatch.setattr(
        compat_spider_module,
        "requests",
        types.SimpleNamespace(get=fake_get),
        raising=False,
    )

    try:
        Spider().fetch("https://example.com/api")
    finally:
        _reset_loaders()

    assert calls == ["https://example.com/api"]


def pytest_fail(message: str) -> None:
    import pytest

    pytest.fail(message)
