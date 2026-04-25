from __future__ import annotations

from base64 import b64encode

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from atv_player.plugins.spider_crypto.keyring import StaticSpiderKeyring


def build_secspider_package(
    source_text: str,
    *,
    name: str = "Fixture Spider",
    version: str = "1",
    remark: str = "",
    kid: str = "fixture-kid",
    hash_override: str | None = None,
) -> tuple[str, StaticSpiderKeyring]:
    signing_key = ECC.generate(curve="Ed25519")
    public_key = signing_key.public_key()
    master_secret = b"0123456789abcdef0123456789abcdef"
    source_bytes = source_text.encode("utf-8")
    source_hash = hash_override or SHA256.new(source_bytes).hexdigest()
    content_key = b"abcdef0123456789abcdef0123456789"
    payload_nonce = b"payload-nonce"
    wrap_key = HKDF(
        master=master_secret,
        key_len=32,
        salt=kid.encode("utf-8"),
        hashmod=SHA256,
        num_keys=1,
        context=f"secspider:{name}:{version}:wrap-key".encode("utf-8"),
    )
    wrap_nonce = HKDF(
        master=master_secret,
        key_len=12,
        salt=kid.encode("utf-8"),
        hashmod=SHA256,
        num_keys=1,
        context=f"secspider:{name}:{version}:wrap-nonce".encode("utf-8"),
    )

    wrap_cipher = AES.new(wrap_key, AES.MODE_GCM, nonce=wrap_nonce)
    wrapped_key, wrapped_tag = wrap_cipher.encrypt_and_digest(content_key)

    payload_cipher = AES.new(content_key, AES.MODE_GCM, nonce=payload_nonce)
    payload_ciphertext, payload_tag = payload_cipher.encrypt_and_digest(source_bytes)

    payload_b64 = b64encode(payload_ciphertext + payload_tag).decode("ascii")
    headers = {
        "name": name,
        "version": version,
        "remark": remark,
        "format": "secspider/1",
        "alg": "aes-256-gcm",
        "wrap": "hkdf-aes-keywrap",
        "sign": "ed25519",
        "kid": kid,
        "nonce": "base64:" + b64encode(payload_nonce).decode("ascii"),
        "ek": "base64:" + b64encode(wrapped_key + wrapped_tag).decode("ascii"),
        "hash": f"sha256:{source_hash}",
    }
    signing_bytes = "\n".join(
        [
            f"//@{key}:{headers[key]}"
            for key in ("name", "version", "remark", "format", "alg", "wrap", "sign", "kid", "nonce", "ek", "hash")
        ]
        + [f"payload.base64:{payload_b64}"]
    ).encode("utf-8")
    headers["sig"] = "base64:" + b64encode(eddsa.new(signing_key, "rfc8032").sign(signing_bytes)).decode("ascii")
    package_text = "\n".join(
        [
            "// ignore",
            f"//@name:{headers['name']}",
            f"//@version:{headers['version']}",
            f"//@remark:{headers['remark']}",
            f"//@format:{headers['format']}",
            f"//@alg:{headers['alg']}",
            f"//@wrap:{headers['wrap']}",
            f"//@sign:{headers['sign']}",
            f"//@kid:{headers['kid']}",
            f"//@nonce:{headers['nonce']}",
            f"//@ek:{headers['ek']}",
            f"//@hash:{headers['hash']}",
            f"//@sig:{headers['sig']}",
            "// ignore",
            f"payload.base64:{payload_b64}",
        ]
    )
    keyring = StaticSpiderKeyring(public_keys={kid: public_key}, master_secrets={kid: master_secret})
    return package_text, keyring
