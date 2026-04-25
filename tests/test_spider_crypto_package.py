import pytest

from atv_player.plugins.spider_crypto.errors import SecSpiderFormatError
from atv_player.plugins.spider_crypto.package import SecSpiderPackage


def _package_text(*, payload: str = "payload.base64:QUJD") -> str:
    lines = [
        "// ignore",
        "//@name:[直] omofun",
        "//@version:1",
        "//@remark:",
        "//@format:secspider/1",
        "//@alg:aes-256-gcm",
        "//@wrap:hkdf-aes-keywrap",
        "//@sign:ed25519",
        "//@kid:test-kid",
        "//@nonce:base64:bm9uY2U=",
        "//@ek:base64:ZWs=",
        "//@hash:sha256:" + ("a" * 64),
        "//@sig:base64:c2ln",
        "// ignore",
        payload,
    ]
    return "\n".join(lines)


def test_parse_secspider_package_reads_minimal_metadata_and_payload() -> None:
    package = SecSpiderPackage.parse(_package_text())

    assert package.header("name") == "[直] omofun"
    assert package.header("version") == "1"
    assert package.header("remark") == ""
    assert package.header("format") == "secspider/1"
    assert package.payload_b64 == "QUJD"
    assert package.payload_bytes() == b"ABC"


def test_parse_secspider_package_rejects_missing_required_header() -> None:
    text = _package_text().replace("//@kid:test-kid\n", "")

    with pytest.raises(SecSpiderFormatError, match="missing required header: kid"):
        SecSpiderPackage.parse(text)


def test_signing_bytes_use_stable_order_and_exclude_sig() -> None:
    package = SecSpiderPackage.parse(_package_text().replace("//@remark:", "//@remark:hello", 1))

    assert package.signing_bytes() == "\n".join(
        [
            "//@name:[直] omofun",
            "//@version:1",
            "//@remark:hello",
            "//@format:secspider/1",
            "//@alg:aes-256-gcm",
            "//@wrap:hkdf-aes-keywrap",
            "//@sign:ed25519",
            "//@kid:test-kid",
            "//@nonce:base64:bm9uY2U=",
            "//@ek:base64:ZWs=",
            "//@hash:sha256:" + ("a" * 64),
            "payload.base64:QUJD",
        ]
    ).encode("utf-8")
