from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass

from atv_player.plugins.spider_crypto.errors import SecSpiderFormatError

_REQUIRED_HEADERS = (
    "name",
    "version",
    "remark",
    "format",
    "alg",
    "wrap",
    "sign",
    "kid",
    "nonce",
    "ek",
    "hash",
    "sig",
)
_SIGNING_HEADERS = (
    "name",
    "version",
    "remark",
    "format",
    "alg",
    "wrap",
    "sign",
    "kid",
    "nonce",
    "ek",
    "hash",
)


@dataclass(slots=True)
class SecSpiderPackage:
    headers: dict[str, str]
    payload_b64: str

    @classmethod
    def parse(cls, text: str) -> "SecSpiderPackage":
        headers: dict[str, str] = {}
        payload_b64 = ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line == "// ignore":
                continue
            if line.startswith("//@"):
                key, sep, value = line[3:].partition(":")
                if not sep:
                    raise SecSpiderFormatError(f"invalid header line: {line}")
                if key in headers:
                    raise SecSpiderFormatError(f"duplicate header: {key}")
                headers[key] = value
                continue
            if line.startswith("payload.base64:"):
                payload_b64 = line.removeprefix("payload.base64:")
                continue
            raise SecSpiderFormatError(f"unexpected line: {line}")
        for key in _REQUIRED_HEADERS:
            if key not in headers:
                raise SecSpiderFormatError(f"missing required header: {key}")
        if not payload_b64:
            raise SecSpiderFormatError("missing payload.base64")
        if headers["format"] != "secspider/1":
            raise SecSpiderFormatError(f"unsupported format: {headers['format']}")
        return cls(headers=headers, payload_b64=payload_b64)

    def header(self, key: str) -> str:
        return self.headers[key]

    def payload_bytes(self) -> bytes:
        return b64decode(self.payload_b64)

    def decoded_header_bytes(self, key: str) -> bytes:
        raw = self.header(key)
        if not raw.startswith("base64:"):
            raise SecSpiderFormatError(f"header is not base64 encoded: {key}")
        return b64decode(raw.removeprefix("base64:"))

    def signing_bytes(self) -> bytes:
        lines = [f"//@{key}:{self.header(key)}" for key in _SIGNING_HEADERS]
        lines.append(f"payload.base64:{self.payload_b64}")
        return "\n".join(lines).encode("utf-8")
