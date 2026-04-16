import types

import atv_player.plugins.compat.base.spider as compat_spider_module
from atv_player.plugins.compat.base.spider import Spider


class DummyResponse:
    def __init__(self) -> None:
        self.encoding = ""


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
