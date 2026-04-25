from __future__ import annotations

import types

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.Signature import eddsa

from atv_player.plugins.spider_crypto.errors import (
    SecSpiderDecryptError,
    SecSpiderHashError,
    SecSpiderSignatureError,
)
from atv_player.plugins.spider_crypto.keyring import StaticSpiderKeyring
from atv_player.plugins.spider_crypto.package import SecSpiderPackage


class SecSpiderRuntime:
    def __init__(self, keyring) -> None:
        self._keyring = keyring

    @classmethod
    def from_dicts(cls, *, public_keys: dict[str, object], master_secrets: dict[str, bytes]) -> "SecSpiderRuntime":
        return cls(StaticSpiderKeyring(public_keys=public_keys, master_secrets=master_secrets))

    def _derive_wrap_material(self, package: SecSpiderPackage) -> tuple[bytes, bytes]:
        kid = package.header("kid")
        name = package.header("name")
        version = package.header("version")
        master_secret = self._keyring.get_master_secret(kid)
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
        return wrap_key, wrap_nonce

    def _verify_signature(self, package: SecSpiderPackage) -> None:
        verifier = eddsa.new(self._keyring.get_public_key(package.header("kid")), "rfc8032")
        try:
            verifier.verify(package.signing_bytes(), package.decoded_header_bytes("sig"))
        except ValueError as exc:
            raise SecSpiderSignatureError("signature verify failed") from exc

    def _decrypt_gcm(self, *, key: bytes, nonce: bytes, blob: bytes) -> bytes:
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        try:
            return cipher.decrypt_and_verify(blob[:-16], blob[-16:])
        except ValueError as exc:
            raise SecSpiderDecryptError("gcm decrypt failed") from exc

    def load_module(self, package: SecSpiderPackage, module_name: str) -> types.ModuleType:
        self._verify_signature(package)
        wrap_key, wrap_nonce = self._derive_wrap_material(package)
        content_key = self._decrypt_gcm(
            key=wrap_key,
            nonce=wrap_nonce,
            blob=package.decoded_header_bytes("ek"),
        )
        source_bytes = self._decrypt_gcm(
            key=content_key,
            nonce=package.decoded_header_bytes("nonce"),
            blob=package.payload_bytes(),
        )
        source_hash = "sha256:" + SHA256.new(source_bytes).hexdigest()
        if source_hash != package.header("hash"):
            raise SecSpiderHashError("source hash mismatch")
        module = types.ModuleType(module_name)
        module.__file__ = f"<secspider:{package.header('name')}>"
        exec(compile(source_bytes.decode("utf-8"), module.__file__, "exec"), module.__dict__)
        return module
