# Poster File Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add shared on-disk poster caching under `~/.cache/atv-player/posters/` and delete poster cache files older than 7 days during app startup.

**Architecture:** Keep all poster cache keying, local file reuse, and write-through download behavior inside `src/atv_player/ui/poster_loader.py` so existing poster callers stay unchanged. Add startup cache directory creation and stale-file cleanup in `src/atv_player/app.py`, with focused tests covering cache hits, cache writes, decode failures, write failures, and 7-day cleanup.

**Tech Stack:** Python 3.13, PySide6, httpx, pytest, pathlib

---

## File Structure

- Modify: `src/atv_player/ui/poster_loader.py`
  Responsibility: poster URL normalization, cache directory helpers, cache key hashing, cached-image loading, remote download fallback, scaled `QImage` return.
- Modify: `src/atv_player/app.py`
  Responsibility: app initialization, cache directory creation, startup deletion of stale poster cache files.
- Modify: `tests/test_poster_loader.py`
  Responsibility: poster cache hit, cache write, cache decode failure, cache write failure coverage.
- Modify: `tests/test_app.py`
  Responsibility: startup cache directory creation and 7-day cleanup coverage.

### Task 1: Poster Loader File Cache

**Files:**
- Modify: `src/atv_player/ui/poster_loader.py`
- Test: `tests/test_poster_loader.py`

- [ ] **Step 1: Write the failing poster-cache tests**

```python
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

from atv_player.ui import poster_loader as poster_loader_module
from atv_player.ui.poster_loader import load_remote_poster_image


def _png_bytes(width: int = 20, height: int = 40) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(0x00FF00)

    from PySide6.QtCore import QBuffer, QByteArray, QIODeviceBase

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODeviceBase.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def test_load_remote_poster_image_reuses_cached_file(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    cache_dir.mkdir()
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)

    image_url = "https://img3.doubanio.com/view/photo/m/public/p123.jpg"
    cache_path = poster_loader_module.poster_cache_path(image_url)
    cache_path.write_bytes(_png_bytes())

    def fail_get(*args, **kwargs):
        raise AssertionError("network should not be used when cache file exists")

    loaded = load_remote_poster_image(image_url, QSize(90, 130), get=fail_get)

    assert loaded is not None
    assert loaded.isNull() is False
    assert loaded.width() <= 90
    assert loaded.height() <= 130


def test_load_remote_poster_image_writes_downloaded_bytes_to_cache(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)
    poster_bytes = _png_bytes()

    class FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        return FakeResponse(poster_bytes)

    image_url = "https://img3.doubanio.com/view/photo/m/public/p123.jpg"
    loaded = load_remote_poster_image(image_url, QSize(90, 130), get=fake_get)
    cache_path = poster_loader_module.poster_cache_path(image_url)

    assert loaded is not None
    assert loaded.isNull() is False
    assert cache_path.read_bytes() == poster_bytes

def test_load_remote_poster_image_returns_none_for_corrupt_cached_bytes(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    cache_dir.mkdir()
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)

    image_url = "https://img3.doubanio.com/view/photo/m/public/p123.jpg"
    poster_loader_module.poster_cache_path(image_url).write_bytes(b"not-an-image")

    loaded = load_remote_poster_image(image_url, QSize(90, 130), get=lambda *args, **kwargs: None)

    assert loaded is None


def test_load_remote_poster_image_returns_image_when_cache_write_fails(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)
    poster_bytes = _png_bytes()

    class FakeResponse:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        return FakeResponse(poster_bytes)

    monkeypatch.setattr(
        poster_loader_module,
        "_write_poster_cache_bytes",
        lambda cache_path, image_bytes: (_ for _ in ()).throw(OSError("disk full")),
    )

    loaded = load_remote_poster_image(
        "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
        QSize(90, 130),
        get=fake_get,
    )

    assert loaded is not None
    assert loaded.isNull() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_poster_loader.py -k "reuses_cached_file or writes_downloaded_bytes_to_cache or corrupt_cached_bytes or cache_write_fails" -v`
Expected: FAIL because `poster_cache_dir`, `poster_cache_path`, and `_write_poster_cache_bytes` do not exist yet and `load_remote_poster_image()` never reads or writes cache files.

- [ ] **Step 3: Write the minimal cache helpers and cache-aware load flow**

```python
from hashlib import sha256
from pathlib import Path


def poster_cache_dir() -> Path:
    cache_dir = Path.home() / ".cache" / "atv-player" / "posters"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def poster_cache_path(image_url: str) -> Path:
    normalized = normalize_poster_url(image_url)
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    return poster_cache_dir() / f"{digest}.img"


def _write_poster_cache_bytes(cache_path: Path, image_bytes: bytes) -> None:
    cache_path.write_bytes(image_bytes)


def _load_image_from_bytes(image_bytes: bytes, target_size: QSize) -> QImage | None:
    image = QImage()
    image.loadFromData(image_bytes)
    if image.isNull():
        return None
    return image.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def load_remote_poster_image(
    image_url: str,
    target_size: QSize,
    timeout: float = POSTER_REQUEST_TIMEOUT_SECONDS,
    get=httpx.get,
) -> QImage | None:
    normalized_url = normalize_poster_url(image_url)
    if not normalized_url:
        return None

    cache_path = poster_cache_path(normalized_url)
    try:
        cached_bytes = cache_path.read_bytes()
    except OSError:
        cached_bytes = None
    else:
        return _load_image_from_bytes(cached_bytes, target_size)

    try:
        response = get(
            normalized_url,
            headers=build_poster_request_headers(normalized_url),
            timeout=timeout,
        )
        response.raise_for_status()
    except Exception:
        return None

    image = _load_image_from_bytes(response.content, target_size)
    if image is None:
        return None
    try:
        _write_poster_cache_bytes(cache_path, response.content)
    except OSError:
        pass
    return image
```

Keep the helper fail-closed for unreadable or corrupt cached files, but do not let cache-write failures hide a successfully decoded network image.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_poster_loader.py -v`
Expected: PASS for all poster-loader tests.

- [ ] **Step 5: Commit**

```bash
git add tests/test_poster_loader.py src/atv_player/ui/poster_loader.py
git commit -m "feat: cache poster files locally"
```

### Task 2: Startup Cache Directory and 7-Day Cleanup

**Files:**
- Modify: `src/atv_player/app.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing startup cache tests**

```python
import time

import atv_player.app as app_module


def test_build_application_creates_poster_cache_directory(monkeypatch, tmp_path) -> None:
    class FakeApplication:
        def __init__(self, args) -> None:
            self.args = args
            self.application_name = ""
            self.window_icon = QIcon()

        def setApplicationName(self, name: str) -> None:
            self.application_name = name

        def setWindowIcon(self, icon: QIcon) -> None:
            self.window_icon = icon

    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module.Path, "home", staticmethod(lambda: tmp_path))

    app_module.build_application()

    assert (tmp_path / ".cache" / "atv-player" / "posters").is_dir()


def test_build_application_deletes_poster_cache_files_older_than_seven_days(monkeypatch, tmp_path) -> None:
    class FakeApplication:
        def __init__(self, args) -> None:
            self.args = args
            self.application_name = ""
            self.window_icon = QIcon()

        def setApplicationName(self, name: str) -> None:
            self.application_name = name

        def setWindowIcon(self, icon: QIcon) -> None:
            self.window_icon = icon

    monkeypatch.setattr(app_module, "QApplication", FakeApplication)
    monkeypatch.setattr(app_module.Path, "home", staticmethod(lambda: tmp_path))

    cache_dir = tmp_path / ".cache" / "atv-player" / "posters"
    cache_dir.mkdir(parents=True)
    old_file = cache_dir / "old.img"
    new_file = cache_dir / "new.img"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    now = time.time()
    stale_age = now - (8 * 24 * 60 * 60)
    fresh_age = now - (2 * 24 * 60 * 60)
    old_file.touch(times=(stale_age, stale_age))
    new_file.touch(times=(fresh_age, fresh_age))

    app_module.build_application()

    assert old_file.exists() is False
    assert new_file.exists() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py -k "poster_cache_directory or deletes_poster_cache_files_older_than_seven_days" -v`
Expected: FAIL because startup does not create `~/.cache/atv-player/posters/` or remove stale files yet.

- [ ] **Step 3: Add startup cache helpers in `app.py`**

```python
import time
from pathlib import Path

POSTER_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def poster_cache_dir() -> Path:
    cache_dir = Path.home() / ".cache" / "atv-player" / "posters"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def purge_stale_poster_cache(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - POSTER_CACHE_MAX_AGE_SECONDS
    for entry in poster_cache_dir().iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue
```

- [ ] **Step 4: Call startup cleanup from `build_application()`**

```python
def build_application() -> tuple[QApplication, SettingsRepository]:
    app = QApplication([])
    app.setApplicationName("atv-player")
    app.setWindowIcon(QIcon(str(_app_icon_path())))
    data_dir = Path.home() / ".local" / "share" / "atv-player"
    repo = SettingsRepository(data_dir / "app.db")
    purge_stale_poster_cache()
    return app, repo
```

Run cleanup after application setup and repository creation so startup always ensures the cache directory exists and trims files older than 7 days.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_app.py -k "poster_cache_directory or deletes_poster_cache_files_older_than_seven_days" -v`
Expected: PASS

- [ ] **Step 6: Run the focused regression suites**

Run: `uv run pytest tests/test_poster_loader.py tests/test_app.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add tests/test_app.py src/atv_player/app.py
git commit -m "feat: clean stale poster cache at startup"
```

### Task 3: Final Verification and Integration Check

**Files:**
- Modify: `src/atv_player/ui/poster_loader.py`
- Modify: `src/atv_player/app.py`
- Test: `tests/test_poster_loader.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Run the full targeted verification command**

Run: `uv run pytest tests/test_poster_loader.py tests/test_app.py tests/test_player_window_ui.py -k "poster" -v`
Expected: PASS, confirming poster cache changes do not break existing player poster behavior.

- [ ] **Step 2: Inspect the diff before finalizing**

Run: `git diff -- src/atv_player/ui/poster_loader.py src/atv_player/app.py tests/test_poster_loader.py tests/test_app.py`
Expected: Only shared poster caching and startup cleanup changes, with no unrelated UI or controller edits.

- [ ] **Step 3: Commit the final integrated change if Task 1 and Task 2 were squashed locally**

```bash
git add src/atv_player/ui/poster_loader.py src/atv_player/app.py tests/test_poster_loader.py tests/test_app.py
git commit -m "feat: add poster disk cache"
```

Skip this step if the earlier task commits are already the desired history.
