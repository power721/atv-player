from __future__ import annotations

from dataclasses import dataclass, field

from atv_player.plugins.spider_crypto.errors import SecSpiderKeyError


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
