import pytest

from atv_player.plugins.spider_crypto.errors import (
    SecSpiderHashError,
    SecSpiderKeyError,
    SecSpiderSignatureError,
)
from atv_player.plugins.spider_crypto.package import SecSpiderPackage
from atv_player.plugins.spider_crypto.runtime import SecSpiderRuntime
from tests.secspider_fixtures import build_secspider_package


def test_runtime_loads_signed_package_into_module() -> None:
    package_text, keyring = build_secspider_package(
        """
class Spider:
    def getName(self):
        return "Fixture Spider"
"""
    )
    runtime = SecSpiderRuntime(keyring)
    package = SecSpiderPackage.parse(package_text)

    module = runtime.load_module(package, "spider_plugin_fixture")

    assert module.Spider().getName() == "Fixture Spider"


def test_runtime_rejects_unknown_kid() -> None:
    package_text, _ = build_secspider_package("class Spider:\n    pass\n")
    package = SecSpiderPackage.parse(package_text.replace("//@kid:fixture-kid", "//@kid:missing-kid"))
    runtime = SecSpiderRuntime.from_dicts(public_keys={}, master_secrets={})

    with pytest.raises(SecSpiderKeyError, match="missing key material"):
        runtime.load_module(package, "spider_plugin_missing")


def test_runtime_rejects_tampered_signature() -> None:
    package_text, keyring = build_secspider_package("class Spider:\n    pass\n")
    package = SecSpiderPackage.parse(package_text.replace("payload.base64:", "payload.base64:A", 1))
    runtime = SecSpiderRuntime(keyring)

    with pytest.raises(SecSpiderSignatureError, match="signature verify failed"):
        runtime.load_module(package, "spider_plugin_bad_sig")


def test_runtime_rejects_hash_mismatch() -> None:
    package_text, keyring = build_secspider_package("class Spider:\n    pass\n", hash_override="deadbeef" * 8)
    package = SecSpiderPackage.parse(package_text)
    runtime = SecSpiderRuntime(keyring)

    with pytest.raises(SecSpiderHashError, match="source hash mismatch"):
        runtime.load_module(package, "spider_plugin_bad_hash")
