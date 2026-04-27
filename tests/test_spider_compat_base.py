import types
import time

import atv_player.plugins.compat.base.spider as compat_spider_module
from atv_player.plugins.compat.base.spider import Spider


class DummyResponse:
    def __init__(self) -> None:
        self.encoding = ""


class ClosableDummyResponse:
    def __init__(self, payload: bytes = b"payload") -> None:
        self.encoding = ""
        self._payload = payload
        self.content_reads = 0
        self.close_calls = 0

    @property
    def content(self) -> bytes:
        self.content_reads += 1
        return self._payload

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8")

    def close(self) -> None:
        self.close_calls += 1


def test_compat_spider_fetch_uses_requests_and_forwards_request_options(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    response = DummyResponse()

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(
        compat_spider_module,
        "requests",
        types.SimpleNamespace(get=fake_get),
        raising=False,
    )

    spider = Spider()
    result = spider.fetch(
        "https://example.com/api",
        params={"k": "v"},
        cookies={"sid": "1"},
        headers={"User-Agent": "UA"},
        timeout=7,
        verify=False,
        stream=True,
        allow_redirects=False,
    )

    assert result is response
    assert response.encoding == "utf-8"
    assert calls == [
        {
            "url": "https://example.com/api",
            "params": {"k": "v"},
            "cookies": {"sid": "1"},
            "headers": {"User-Agent": "UA"},
            "timeout": 7,
            "verify": False,
            "stream": True,
            "allow_redirects": False,
        }
    ]


def test_compat_spider_fetch_preloads_and_closes_response(monkeypatch) -> None:
    response = ClosableDummyResponse()

    monkeypatch.setattr(
        compat_spider_module,
        "requests",
        types.SimpleNamespace(get=lambda url, **kwargs: response),
        raising=False,
    )

    result = Spider().fetch("https://example.com/api", stream=True)

    assert result is response
    assert result.text == "payload"
    assert response.content_reads >= 1
    assert response.close_calls == 1


def test_compat_spider_post_uses_requests_and_forwards_request_options(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    response = DummyResponse()

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return response

    monkeypatch.setattr(
        compat_spider_module,
        "requests",
        types.SimpleNamespace(post=fake_post),
        raising=False,
    )

    spider = Spider()
    result = spider.post(
        "https://example.com/api",
        params={"page": "1"},
        data={"a": "b"},
        json={"x": 1},
        cookies={"sid": "1"},
        headers={"User-Agent": "UA"},
        timeout=9,
        verify=False,
        stream=True,
        allow_redirects=False,
    )

    assert result is response
    assert response.encoding == "utf-8"
    assert calls == [
        {
            "url": "https://example.com/api",
            "params": {"page": "1"},
            "data": {"a": "b"},
            "json": {"x": 1},
            "cookies": {"sid": "1"},
            "headers": {"User-Agent": "UA"},
            "timeout": 9,
            "verify": False,
            "stream": True,
            "allow_redirects": False,
        }
    ]


def test_compat_spider_post_preloads_and_closes_response(monkeypatch) -> None:
    response = ClosableDummyResponse()

    monkeypatch.setattr(
        compat_spider_module,
        "requests",
        types.SimpleNamespace(post=lambda url, **kwargs: response),
        raising=False,
    )

    result = Spider().post("https://example.com/api", stream=True)

    assert result is response
    assert result.text == "payload"
    assert response.content_reads >= 1
    assert response.close_calls == 1


def test_compat_spider_cache_round_trip_and_delete_use_local_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(compat_spider_module, "_CACHE_ROOT", tmp_path / "spider-cache", raising=False)
    spider = Spider()
    value = {"token": "abc", "expiresAt": int(time.time()) + 60}

    assert spider.getCache("session") is None
    assert spider.setCache("session", value) == "succeed"
    assert spider.getCache("session") == value
    assert spider.delCache("session") == "succeed"
    assert spider.getCache("session") is None


def test_compat_spider_get_cache_removes_expired_structured_values(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(compat_spider_module, "_CACHE_ROOT", tmp_path / "spider-cache", raising=False)
    spider = Spider()

    assert spider.setCache("expired", {"expiresAt": int(time.time()) - 1, "value": "stale"}) == "succeed"
    assert spider.getCache("expired") is None
