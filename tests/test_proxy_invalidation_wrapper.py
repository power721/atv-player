from __future__ import annotations

from types import SimpleNamespace

from atv_player.app import _make_proxy_invalidation_wrapper, _proxy_signature
from atv_player.models import AppConfig


def _make_config(mode: str = "direct", proxy_url: str = "", bypass: list[str] | None = None) -> AppConfig:
    return AppConfig(
        network_proxy_mode=mode,
        network_proxy_url=proxy_url,
        network_proxy_bypass_rules=list(bypass) if bypass is not None else [],
    )


def _make_network_stub() -> tuple[SimpleNamespace, list[str]]:
    calls: list[str] = []
    stub = SimpleNamespace(invalidate_proxy=lambda: calls.append("invalidate"))
    return stub, calls


def test_save_runs_underlying_callback() -> None:
    config = _make_config()
    network, _ = _make_network_stub()
    save_calls = [0]

    def save() -> None:
        save_calls[0] += 1

    wrapped = _make_proxy_invalidation_wrapper(save, config, network)
    wrapped()
    wrapped()

    assert save_calls[0] == 2


def test_save_does_not_invalidate_when_proxy_unchanged() -> None:
    config = _make_config("direct", "", ["localhost"])
    network, calls = _make_network_stub()

    wrapped = _make_proxy_invalidation_wrapper(lambda: None, config, network)
    wrapped()
    wrapped()

    assert calls == []


def test_save_invalidates_after_proxy_mode_changes() -> None:
    config = _make_config("direct")
    network, calls = _make_network_stub()
    wrapped = _make_proxy_invalidation_wrapper(lambda: None, config, network)
    wrapped()

    config.network_proxy_mode = "http"
    config.network_proxy_url = "http://127.0.0.1:7890"
    wrapped()

    assert calls == ["invalidate"]


def test_save_invalidates_after_proxy_url_changes() -> None:
    config = _make_config("http", "http://a:7890")
    network, calls = _make_network_stub()
    wrapped = _make_proxy_invalidation_wrapper(lambda: None, config, network)
    wrapped()

    config.network_proxy_url = "http://b:7890"
    wrapped()

    assert calls == ["invalidate"]


def test_save_invalidates_after_bypass_rules_change() -> None:
    config = _make_config("http", "http://a:7890", ["localhost"])
    network, calls = _make_network_stub()
    wrapped = _make_proxy_invalidation_wrapper(lambda: None, config, network)
    wrapped()

    config.network_proxy_bypass_rules = ["localhost", "10.0.0.0/8"]
    wrapped()

    assert calls == ["invalidate"]


def test_save_invalidates_once_per_change_then_stays_quiet() -> None:
    config = _make_config("direct")
    network, calls = _make_network_stub()
    wrapped = _make_proxy_invalidation_wrapper(lambda: None, config, network)
    wrapped()

    config.network_proxy_mode = "http"
    config.network_proxy_url = "http://a:7890"
    wrapped()
    wrapped()
    wrapped()

    assert calls == ["invalidate"]


def test_proxy_signature_is_tuple_with_bypass_rules_normalized() -> None:
    config = _make_config("http", "http://a:7890", ["localhost", "10.0.0.0/8"])

    sig = _proxy_signature(config)

    assert sig == ("http", "http://a:7890", ("localhost", "10.0.0.0/8"))
