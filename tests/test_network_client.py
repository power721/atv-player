from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from unittest.mock import MagicMock

import httpx
import pytest
import requests

from atv_player.network_client import NetworkClient
from atv_player.network_proxy import ProxyConfig, ProxyDecider


def _make_decider(
    mode: str = "direct",
    proxy_url: str = "",
    bypass: Iterable[str] = (),
) -> ProxyDecider:
    return ProxyDecider(
        ProxyConfig(mode=mode, proxy_url=proxy_url, bypass_rules=list(bypass))
    )


def _client_factory() -> tuple[Callable[[str, int], MagicMock], dict[str, MagicMock]]:
    clients: dict[str, MagicMock] = {}

    def factory(key: str, pool_limit: int) -> MagicMock:
        clients[key] = MagicMock(name=f"client[{key}]")
        return clients[key]

    return factory, clients


def _session_factory() -> tuple[Callable[[int], MagicMock], list[MagicMock]]:
    sessions: list[MagicMock] = []

    def factory(pool_limit: int) -> MagicMock:
        sessions.append(MagicMock(name=f"session#{len(sessions)}"))
        return sessions[-1]

    return factory, sessions


def test_get_with_none_decider_uses_direct_client() -> None:
    factory, clients = _client_factory()
    network = NetworkClient(lambda: None, client_factory=factory)

    network.get("https://example.com/")

    assert list(clients.keys()) == ["direct"]
    clients["direct"].get.assert_called_once_with("https://example.com/")


def test_get_caches_client_by_url_decision() -> None:
    factory, clients = _client_factory()
    network = NetworkClient(lambda: None, client_factory=factory)

    network.get("https://example.com/a")
    network.get("https://example.com/b")
    network.get("https://example.org/c")

    assert list(clients.keys()) == ["direct"]
    assert clients["direct"].get.call_count == 3


def test_get_routes_bypass_and_proxy_to_separate_clients() -> None:
    decider = _make_decider("http", "http://p:7890", bypass=["localhost"])
    factory, clients = _client_factory()
    network = NetworkClient(lambda: decider, client_factory=factory)

    network.get("http://localhost/api")
    network.get("https://api.themoviedb.org/")

    assert set(clients.keys()) == {"direct", "manual:http://p:7890"}
    clients["direct"].get.assert_called_once_with("http://localhost/api")
    clients["manual:http://p:7890"].get.assert_called_once_with(
        "https://api.themoviedb.org/"
    )


def test_get_routes_system_mode_to_system_client() -> None:
    decider = _make_decider("system")
    factory, clients = _client_factory()
    network = NetworkClient(lambda: decider, client_factory=factory)

    network.get("https://api.bgm.tv/")

    assert list(clients.keys()) == ["system"]


def test_get_with_non_http_url_uses_direct() -> None:
    decider = _make_decider("http", "http://p:7890")
    factory, clients = _client_factory()
    network = NetworkClient(lambda: decider, client_factory=factory)

    network.get("file:///tmp/x")

    assert list(clients.keys()) == ["direct"]


def test_get_passes_kwargs_to_underlying_client() -> None:
    factory, clients = _client_factory()
    network = NetworkClient(lambda: None, client_factory=factory)

    network.get("https://example.com/", timeout=5, headers={"X": "1"})

    clients["direct"].get.assert_called_once_with(
        "https://example.com/", timeout=5, headers={"X": "1"}
    )


def test_post_uses_same_client_pool() -> None:
    factory, clients = _client_factory()
    network = NetworkClient(lambda: None, client_factory=factory)

    network.post("https://example.com/", json={"a": 1})

    clients["direct"].post.assert_called_once_with("https://example.com/", json={"a": 1})


def test_stream_uses_correct_client() -> None:
    factory, clients = _client_factory()
    network = NetworkClient(lambda: None, client_factory=factory)

    network.stream("GET", "https://example.com/")

    clients["direct"].stream.assert_called_once_with("GET", "https://example.com/")


def test_invalidate_proxy_keeps_direct_client_open() -> None:
    decider_box = {"d": _make_decider("http", "http://p:7890", bypass=["localhost"])}
    factory, clients = _client_factory()
    network = NetworkClient(lambda: decider_box["d"], client_factory=factory)
    network.get("http://localhost/")
    network.get("https://other.com/")
    direct_client = clients["direct"]

    network.invalidate_proxy()

    direct_client.close.assert_not_called()


def test_invalidate_proxy_closes_proxied_clients() -> None:
    decider_box = {"d": _make_decider("http", "http://p:7890")}
    factory, clients = _client_factory()
    network = NetworkClient(lambda: decider_box["d"], client_factory=factory)
    network.get("https://x.com/")
    manual_client = clients["manual:http://p:7890"]

    network.invalidate_proxy()

    manual_client.close.assert_called_once()


def test_invalidate_proxy_rebuilds_manual_client_for_next_request() -> None:
    decider_box = {"d": _make_decider("http", "http://a:7890")}
    factory, clients = _client_factory()
    network = NetworkClient(lambda: decider_box["d"], client_factory=factory)
    network.get("https://x.com/")

    decider_box["d"] = _make_decider("http", "http://b:7890")
    network.invalidate_proxy()
    network.get("https://x.com/")

    assert set(clients.keys()) == {"manual:http://a:7890", "manual:http://b:7890"}


def test_invalidate_proxy_re_reads_decider_factory() -> None:
    decider_calls = 0

    def decider_factory() -> ProxyDecider | None:
        nonlocal decider_calls
        decider_calls += 1
        return _make_decider("direct")

    factory, _ = _client_factory()
    network = NetworkClient(decider_factory, client_factory=factory)

    network.get("https://a/")
    network.get("https://b/")
    assert decider_calls == 1

    network.invalidate_proxy()
    network.get("https://c/")
    assert decider_calls == 2


def test_close_closes_all_clients_and_session() -> None:
    decider = _make_decider("http", "http://p:7890", bypass=["localhost"])
    factory, clients = _client_factory()
    session_factory_fn, sessions = _session_factory()
    network = NetworkClient(
        lambda: decider,
        client_factory=factory,
        session_factory=session_factory_fn,
    )
    network.get("http://localhost/")
    network.get("https://x.com/")
    network.requests_session()

    network.close()

    clients["direct"].close.assert_called_once()
    clients["manual:http://p:7890"].close.assert_called_once()
    sessions[0].close.assert_called_once()


def test_close_is_idempotent() -> None:
    factory, clients = _client_factory()
    network = NetworkClient(lambda: None, client_factory=factory)
    network.get("https://a/")

    network.close()
    network.close()

    assert clients["direct"].close.call_count == 1


def test_get_after_close_raises() -> None:
    factory, _ = _client_factory()
    network = NetworkClient(lambda: None, client_factory=factory)
    network.close()

    with pytest.raises(RuntimeError):
        network.get("https://a/")


def test_requests_session_reused() -> None:
    factory, _ = _client_factory()
    session_factory_fn, sessions = _session_factory()
    network = NetworkClient(
        lambda: None,
        client_factory=factory,
        session_factory=session_factory_fn,
    )

    s1 = network.requests_session()
    s2 = network.requests_session()

    assert s1 is s2
    assert len(sessions) == 1


def test_requests_session_rebuilt_after_invalidate() -> None:
    factory, _ = _client_factory()
    session_factory_fn, sessions = _session_factory()
    network = NetworkClient(
        lambda: None,
        client_factory=factory,
        session_factory=session_factory_fn,
    )

    s1 = network.requests_session()
    network.invalidate_proxy()
    s2 = network.requests_session()

    assert s1 is not s2
    s1.close.assert_called_once()
    assert len(sessions) == 2


def test_client_factory_receives_pool_limit() -> None:
    received: list[int] = []

    def factory(key: str, pool_limit: int) -> MagicMock:
        received.append(pool_limit)
        return MagicMock()

    network = NetworkClient(lambda: None, client_factory=factory, pool_limit=42)
    network.get("https://x.com/")

    assert received == [42]


def test_session_factory_receives_pool_limit() -> None:
    received: list[int] = []

    def session_factory(pool_limit: int) -> MagicMock:
        received.append(pool_limit)
        return MagicMock()

    factory, _ = _client_factory()
    network = NetworkClient(
        lambda: None,
        client_factory=factory,
        session_factory=session_factory,
        pool_limit=7,
    )
    network.requests_session()

    assert received == [7]


def test_concurrent_get_creates_one_client_per_key() -> None:
    factory, clients = _client_factory()
    network = NetworkClient(lambda: None, client_factory=factory)
    barrier = threading.Barrier(20)

    def worker() -> None:
        barrier.wait()
        for _ in range(5):
            network.get("https://example.com/")

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert list(clients.keys()) == ["direct"]
    assert clients["direct"].get.call_count == 100


def test_default_client_factory_returns_httpx_client() -> None:
    from atv_player.network_client import _default_client_factory

    client = _default_client_factory("direct", 10)
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()


def test_default_client_factory_manual_accepts_proxy_url() -> None:
    from atv_player.network_client import _default_client_factory

    client = _default_client_factory("manual:http://127.0.0.1:9999", 10)
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()


def test_default_client_factory_system() -> None:
    from atv_player.network_client import _default_client_factory

    client = _default_client_factory("system", 10)
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()


def test_default_client_factory_unknown_key_raises() -> None:
    from atv_player.network_client import _default_client_factory

    with pytest.raises(ValueError):
        _default_client_factory("nonsense", 10)


def test_default_session_factory_pool_size_matches_argument() -> None:
    from atv_player.network_client import _default_session_factory

    session = _default_session_factory(7)
    try:
        for prefix in ("http://", "https://"):
            adapter = session.get_adapter(f"{prefix}example.com")
            assert isinstance(adapter, requests.adapters.HTTPAdapter)
            assert adapter._pool_connections == 7
            assert adapter._pool_maxsize == 7
    finally:
        session.close()
