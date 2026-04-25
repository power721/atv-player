# SecSpider Loader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `secspider/1` encrypted spider loading to `atv-player` while keeping existing plain Python spider plugins working unchanged.

**Architecture:** Keep the current `SpiderPluginLoader` as the single entry point, but split encrypted-plugin support into a focused internal `spider_crypto` package that parses package text, verifies signatures, derives wrap material, decrypts source, and executes the module in memory. This plan only covers the host-side runtime plus a test-only package builder helper; the production signing/publishing tool should live in a separate private build workflow, not in this public app repo.

**Tech Stack:** Python 3.12+, PyCryptodome (`AES`, `HKDF`, `SHA256`, `ECC`, `eddsa`), pytest, httpx

---

## File Structure

- `src/atv_player/plugins/loader.py`
  - Keep plain `.py` plugin loading intact.
  - Detect `secspider/1` package headers.
  - Route encrypted plugins through the new runtime and map low-level crypto failures to stable `ValueError` messages.
- `src/atv_player/plugins/spider_crypto/__init__.py`
  - Re-export the internal crypto package entry points used by tests and the loader.
- `src/atv_player/plugins/spider_crypto/errors.py`
  - Define focused exceptions for format, signature, key, decrypt, hash, and runtime failures.
- `src/atv_player/plugins/spider_crypto/package.py`
  - Parse `//@` headers, validate required fields, expose decoded base64 helpers, and produce canonical signing bytes.
- `src/atv_player/plugins/spider_crypto/keyring.py`
  - Define the keyring protocol and a simple in-memory implementation for loader injection and tests.
- `src/atv_player/plugins/spider_crypto/runtime.py`
  - Verify package signatures, derive wrap key and wrap nonce, unwrap the content key, decrypt payload bytes, verify source hash, and `compile/exec` the module.
- `tests/secspider_fixtures.py`
  - Build signed encrypted package text for tests only, using ephemeral Ed25519 keys and in-memory keyrings.
- `tests/test_spider_crypto_package.py`
  - Lock down parsing, canonicalization, and required-field validation.
- `tests/test_spider_crypto_runtime.py`
  - Lock down the runtime’s success path and failure paths without involving `SpiderPluginLoader`.
- `tests/test_spider_plugin_loader.py`
  - Add loader integration coverage for local and remote encrypted plugins, plus friendly error mapping.

### Scope Note

The approved spec describes both a host loader and a release-side builder. In this codebase, only the host loader belongs here. The test helper in `tests/secspider_fixtures.py` exists solely to generate fixture packages for TDD; it is not the production publishing tool.

### Protocol Refinement

The approved package header keeps business metadata to `name`, `version`, and `remark`, and does not include a separate wrap nonce field. To keep that header unchanged while still using `AES-GCM` for `ek`, this plan derives both `wrap_key` and `wrap_nonce` from `master_secret + kid + name + version`:

```python
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
```

This keeps the package text unchanged while making `ek` decryptable.

### Task 1: Add `secspider/1` Package Parsing And Canonicalization

**Files:**
- Create: `src/atv_player/plugins/spider_crypto/__init__.py`
- Create: `src/atv_player/plugins/spider_crypto/errors.py`
- Create: `src/atv_player/plugins/spider_crypto/package.py`
- Test: `tests/test_spider_crypto_package.py`

- [ ] **Step 1: Write the failing package parser tests**

Create `tests/test_spider_crypto_package.py` with these tests:

```python
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
```

- [ ] **Step 2: Run the package parser tests to verify they fail**

Run: `uv run pytest tests/test_spider_crypto_package.py -v`

Expected: FAIL because `atv_player.plugins.spider_crypto` does not exist yet.

- [ ] **Step 3: Write the minimal parser implementation**

Create `src/atv_player/plugins/spider_crypto/errors.py`:

```python
class SecSpiderError(Exception):
    pass


class SecSpiderFormatError(SecSpiderError):
    pass


class SecSpiderSignatureError(SecSpiderError):
    pass


class SecSpiderKeyError(SecSpiderError):
    pass


class SecSpiderDecryptError(SecSpiderError):
    pass


class SecSpiderHashError(SecSpiderError):
    pass


class SecSpiderRuntimeError(SecSpiderError):
    pass
```

Create `src/atv_player/plugins/spider_crypto/package.py`:

```python
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
                key, sep, value = line[4:].partition(":")
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
```

Create `src/atv_player/plugins/spider_crypto/__init__.py`:

```python
from atv_player.plugins.spider_crypto.errors import (
    SecSpiderDecryptError,
    SecSpiderError,
    SecSpiderFormatError,
    SecSpiderHashError,
    SecSpiderKeyError,
    SecSpiderRuntimeError,
    SecSpiderSignatureError,
)
from atv_player.plugins.spider_crypto.package import SecSpiderPackage

__all__ = [
    "SecSpiderDecryptError",
    "SecSpiderError",
    "SecSpiderFormatError",
    "SecSpiderHashError",
    "SecSpiderKeyError",
    "SecSpiderPackage",
    "SecSpiderRuntimeError",
    "SecSpiderSignatureError",
]
```

- [ ] **Step 4: Run the package parser tests to verify they pass**

Run: `uv run pytest tests/test_spider_crypto_package.py -v`

Expected: PASS for all `test_spider_crypto_package.py` tests.

- [ ] **Step 5: Commit the parser slice**

```bash
git add tests/test_spider_crypto_package.py src/atv_player/plugins/spider_crypto/__init__.py src/atv_player/plugins/spider_crypto/errors.py src/atv_player/plugins/spider_crypto/package.py
git commit -m "feat: add secspider package parser"
```

### Task 2: Add Keyring, Runtime, And Test Fixture Packer

**Files:**
- Create: `src/atv_player/plugins/spider_crypto/keyring.py`
- Create: `src/atv_player/plugins/spider_crypto/runtime.py`
- Create: `tests/secspider_fixtures.py`
- Test: `tests/test_spider_crypto_runtime.py`

- [ ] **Step 1: Write the failing runtime tests and test-only packer helper**

Create `tests/secspider_fixtures.py`:

```python
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
) -> tuple[str, StaticSpiderKeyring]:
    signing_key = ECC.generate(curve="Ed25519")
    public_key = signing_key.public_key()
    master_secret = b"0123456789abcdef0123456789abcdef"
    source_bytes = source_text.encode("utf-8")
    source_hash = SHA256.new(source_bytes).hexdigest()
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
        [f"//@{key}:{headers[key]}" for key in ("name", "version", "remark", "format", "alg", "wrap", "sign", "kid", "nonce", "ek", "hash")]
        + [f"payload.base64:{b64encode(payload_ciphertext + payload_tag).decode('ascii')}"]
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
            "payload.base64:" + b64encode(payload_ciphertext + payload_tag).decode("ascii"),
        ]
    )
    keyring = StaticSpiderKeyring(public_keys={kid: public_key}, master_secrets={kid: master_secret})
    return package_text, keyring
```

Create `tests/test_spider_crypto_runtime.py`:

```python
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
    package_text, keyring = build_secspider_package("class Spider:\n    pass\n")
    package = SecSpiderPackage.parse(package_text.replace("//@hash:sha256:", "//@hash:sha256:deadbeef", 1))
    runtime = SecSpiderRuntime(keyring)

    with pytest.raises(SecSpiderHashError, match="source hash mismatch"):
        runtime.load_module(package, "spider_plugin_bad_hash")
```

- [ ] **Step 2: Run the runtime tests to verify they fail**

Run: `uv run pytest tests/test_spider_crypto_runtime.py -v`

Expected: FAIL because `StaticSpiderKeyring` and `SecSpiderRuntime` do not exist yet.

- [ ] **Step 3: Write the minimal keyring and runtime implementation**

Create `src/atv_player/plugins/spider_crypto/keyring.py`:

```python
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
```

Create `src/atv_player/plugins/spider_crypto/runtime.py`:

```python
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
```

Append exports in `src/atv_player/plugins/spider_crypto/__init__.py`:

```python
from atv_player.plugins.spider_crypto.keyring import StaticSpiderKeyring
from atv_player.plugins.spider_crypto.runtime import SecSpiderRuntime

__all__ += ["SecSpiderRuntime", "StaticSpiderKeyring"]
```

- [ ] **Step 4: Run the runtime tests to verify they pass**

Run: `uv run pytest tests/test_spider_crypto_runtime.py -v`

Expected: PASS for all `test_spider_crypto_runtime.py` tests.

- [ ] **Step 5: Commit the runtime slice**

```bash
git add tests/secspider_fixtures.py tests/test_spider_crypto_runtime.py src/atv_player/plugins/spider_crypto/keyring.py src/atv_player/plugins/spider_crypto/runtime.py src/atv_player/plugins/spider_crypto/__init__.py
git commit -m "feat: add secspider runtime"
```

### Task 3: Wire `SpiderPluginLoader` To Support Plain And Encrypted Plugins

**Files:**
- Modify: `src/atv_player/plugins/loader.py`
- Test: `tests/test_spider_plugin_loader.py`

- [ ] **Step 1: Write the failing loader integration tests**

Append these tests to `tests/test_spider_plugin_loader.py`:

```python
from tests.secspider_fixtures import build_secspider_package


def test_loader_loads_local_secspider_plugin(tmp_path: Path) -> None:
    package_text, keyring = build_secspider_package(
        """
from base.spider import Spider

class Spider(Spider):
    def init(self, extend=""):
        self.extend = extend

    def getName(self):
        return f"加密:{self.extend}"
""",
        name="红果短剧",
    )
    plugin_path = tmp_path / "encrypted_plugin.py"
    plugin_path.write_text(package_text, encoding="utf-8")
    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache", keyring=keyring)
    config = SpiderPluginConfig(
        id=41,
        source_type="local",
        source_value=str(plugin_path),
        display_name="",
        enabled=True,
        sort_order=0,
        config_text="site=https://example.com",
    )

    loaded = loader.load(config)

    assert loaded.plugin_name == "加密:site=https://example.com"


def test_loader_loads_remote_secspider_plugin_and_persists_cache(tmp_path: Path) -> None:
    package_text, keyring = build_secspider_package(
        """
from base.spider import Spider

class Spider(Spider):
    def getName(self):
        return "远程加密"
""",
        name="远程加密",
    )

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        return httpx.Response(200, text=package_text)

    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache", get=fake_get, keyring=keyring)
    config = SpiderPluginConfig(
        id=42,
        source_type="remote",
        source_value="https://example.com/encrypted.py",
        display_name="",
        enabled=True,
        sort_order=0,
    )

    loaded = loader.load(config, force_refresh=True)

    assert loaded.plugin_name == "远程加密"
    assert Path(loaded.config.cached_file_path).read_text(encoding="utf-8").startswith("// ignore")


def test_loader_reports_secspider_signature_failure(tmp_path: Path) -> None:
    package_text, keyring = build_secspider_package("class Spider:\n    pass\n")
    plugin_path = tmp_path / "broken_encrypted.py"
    plugin_path.write_text(package_text.replace("payload.base64:", "payload.base64:Z", 1), encoding="utf-8")
    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache", keyring=keyring)
    config = SpiderPluginConfig(
        id=43,
        source_type="local",
        source_value=str(plugin_path),
        display_name="",
        enabled=True,
        sort_order=0,
    )

    with pytest.raises(ValueError, match="插件签名校验失败"):
        loader.load(config)


def test_loader_reports_missing_spider_class_after_secspider_decrypt(tmp_path: Path) -> None:
    package_text, keyring = build_secspider_package("class NotSpider:\n    pass\n")
    plugin_path = tmp_path / "missing_spider.py"
    plugin_path.write_text(package_text, encoding="utf-8")
    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache", keyring=keyring)
    config = SpiderPluginConfig(
        id=44,
        source_type="local",
        source_value=str(plugin_path),
        display_name="",
        enabled=True,
        sort_order=0,
    )

    with pytest.raises(ValueError, match="缺少 Spider 类"):
        loader.load(config)
```

- [ ] **Step 2: Run the loader integration tests to verify they fail**

Run: `uv run pytest tests/test_spider_plugin_loader.py -k "secspider" -v`

Expected: FAIL because `SpiderPluginLoader` does not accept a `keyring` argument and does not detect encrypted packages.

- [ ] **Step 3: Write the minimal dual-stack loader implementation**

Update `src/atv_player/plugins/loader.py` like this:

```python
from atv_player.plugins.spider_crypto.errors import (
    SecSpiderDecryptError,
    SecSpiderFormatError,
    SecSpiderHashError,
    SecSpiderKeyError,
    SecSpiderSignatureError,
)
from atv_player.plugins.spider_crypto.package import SecSpiderPackage
from atv_player.plugins.spider_crypto.runtime import SecSpiderRuntime
```

```python
class SpiderPluginLoader:
    def __init__(self, cache_dir: Path, get=httpx.get, keyring=None) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._get = get
        self._runtime = SecSpiderRuntime(keyring) if keyring is not None else None
```

```python
    def load(self, config: SpiderPluginConfig, force_refresh: bool = False) -> LoadedSpiderPlugin:
        compat_spider_module.set_cache_root(self._cache_dir / "spider-cache")
        self._install_compat_modules()
        source_path = self._resolve_source_path(config, force_refresh=force_refresh)
        module_name = f"spider_plugin_{config.id}_{source_path.stem}"
        try:
            package_format = self._detect_package_format(source_path)
            if package_format == "secspider/1":
                module = self._load_secspider_module(module_name, source_path)
            else:
                module = self._load_plain_module(module_name, source_path)
        except ModuleNotFoundError as exc:
            raise ValueError(f"缺少依赖: {exc.name}") from exc
        except SecSpiderFormatError as exc:
            raise ValueError("插件格式不支持") from exc
        except SecSpiderSignatureError as exc:
            raise ValueError("插件签名校验失败") from exc
        except SecSpiderKeyError as exc:
            raise ValueError("插件密钥不可用") from exc
        except SecSpiderDecryptError as exc:
            raise ValueError("插件解密失败") from exc
        except SecSpiderHashError as exc:
            raise ValueError("插件源码校验失败") from exc
        spider_cls = getattr(module, "Spider", None)
        if spider_cls is None:
            raise ValueError("缺少 Spider 类")
        ...
```

```python
    def _detect_package_format(self, source_path: Path) -> str:
        for raw_line in source_path.read_text(encoding="utf-8").splitlines()[:16]:
            line = raw_line.strip()
            if line.startswith("//@format:"):
                return line.removeprefix("//@format:")
        return "plain"

    def _load_plain_module(self, module_name: str, source_path: Path):
        spec = importlib.util.spec_from_file_location(module_name, source_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"无法加载插件文件: {source_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules.pop(module_name, None)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _load_secspider_module(self, module_name: str, source_path: Path):
        if self._runtime is None:
            raise SecSpiderKeyError("missing key material for encrypted spider loading")
        package = SecSpiderPackage.parse(source_path.read_text(encoding="utf-8"))
        module = self._runtime.load_module(package, module_name)
        sys.modules.pop(module_name, None)
        sys.modules[module_name] = module
        return module
```

Keep `_resolve_source_path()` unchanged in this first implementation so remote cache semantics stay stable and existing tests do not regress.

- [ ] **Step 4: Run the loader integration tests to verify they pass**

Run: `uv run pytest tests/test_spider_plugin_loader.py -k "secspider" -v`

Expected: PASS for all encrypted loader tests.

- [ ] **Step 5: Run the broader loader regression tests**

Run: `uv run pytest tests/test_spider_crypto_package.py tests/test_spider_crypto_runtime.py tests/test_spider_plugin_loader.py -v`

Expected: PASS for all parser, runtime, and loader tests, including the existing plain-plugin loader coverage.

- [ ] **Step 6: Commit the loader integration slice**

```bash
git add tests/test_spider_plugin_loader.py src/atv_player/plugins/loader.py
git commit -m "feat: support secspider loader"
```

### Task 4: Verify Composition And Document The Remaining Split

**Files:**
- Modify: `docs/superpowers/plans/2026-04-25-secspider-loader.md`

- [ ] **Step 1: Run the targeted quality checks one more time**

Run: `uv run pytest tests/test_spider_crypto_package.py tests/test_spider_crypto_runtime.py tests/test_spider_plugin_loader.py -v`

Expected: PASS.

- [ ] **Step 2: Record the post-implementation split explicitly in this plan**

Append this note to the bottom of this plan once implementation is done:

```markdown
## Follow-up

The host-side loader is complete. The remaining builder/publishing work should be implemented in a separate private tool or repo so signing keys and release automation stay outside the public app tree.
```

- [ ] **Step 3: Commit the final plan note if it changed**

```bash
git add docs/superpowers/plans/2026-04-25-secspider-loader.md
git commit -m "docs: note secspider builder follow-up"
```

## Follow-up

The host-side loader is complete. The remaining builder/publishing work should be implemented in a separate private tool or repo so signing keys and release automation stay outside the public app tree.
