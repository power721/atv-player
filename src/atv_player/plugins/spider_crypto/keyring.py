from __future__ import annotations

import base64
from dataclasses import dataclass, field

from Crypto.PublicKey import ECC

from atv_player.plugins.spider_crypto.errors import SecSpiderKeyError

DEFAULT_KID = "k2026_04"
_OBFUSCATION_KEY = b"secspider-k2026_04"
_PUBLIC_KEY_B64 = "XkhOXl0rISI7Y0tiZXB6FnMUOCA6Xl1ESUh4YChdR3BnBnR/QTMUMgksJSE7SxsGVUoHEmpCOFEgJRUlKQsnREBzaV1RL2ZBEg0TBRwKIFMBGi8BSgoLVR0ZXkhONj4tRDUnbyd7cxJ9GmkZXkhOXno="
_MASTER_SECRET_B64 = "RVZXSkALBQZfFVNTCB8CalZRXl1USkREUAAQSFJRBFRQPVYH"


@dataclass(slots=True)
class StaticSpiderKeyring:
    public_keys: dict[str, object] = field(default_factory=dict)
    master_secrets: dict[str, bytes] = field(default_factory=dict)

    def get_public_key(self, kid: str):
        key = self.public_keys.get(kid)
        if key is None:
            raise SecSpiderKeyError(f"missing key material for kid={kid}")
        return key

    def get_master_secret(self, kid: str) -> bytes:
        key = self.master_secrets.get(kid)
        if key is None:
            raise SecSpiderKeyError(f"missing key material for kid={kid}")
        return key


def _decode_embedded_secret(value: str) -> bytes:
    raw = base64.b64decode(value)
    return bytes(item ^ _OBFUSCATION_KEY[index % len(_OBFUSCATION_KEY)] for index, item in enumerate(raw))


def load_default_keyring(kid: str = DEFAULT_KID) -> StaticSpiderKeyring:
    try:
        public_key = ECC.import_key(_decode_embedded_secret(_PUBLIC_KEY_B64).decode("utf-8"))
        master_secret = _decode_embedded_secret(_MASTER_SECRET_B64)
    except (ValueError, TypeError) as exc:
        raise SecSpiderKeyError("invalid embedded key material for encrypted spider loading") from exc
    return StaticSpiderKeyring(public_keys={kid: public_key}, master_secrets={kid: master_secret})
