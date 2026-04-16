# Enforce Local Poster Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all remote poster rendering consistently go through the shared local file cache and recover from corrupt or unreadable cache files by retrying the remote download.

**Architecture:** Keep `load_remote_poster_image()` in `poster_loader.py` as the single poster-loading entry point for all remote images. Tighten that helper so cache reads are attempted first, corrupt or unreadable cache files fall through to a fresh download, and successful downloads still write back to disk without pushing any cache logic into `DoubanPage` or `PlayerWindow`.

**Tech Stack:** Python, PySide6, httpx, pytest

---

## File Structure

- `src/atv_player/ui/poster_loader.py`
  Keeps URL normalization, cache key generation, disk reads/writes, remote download, and `QImage` scaling in one place. This is the only production file that should change for this feature.
- `tests/test_poster_loader.py`
  Adds regression coverage for corrupt-cache and unreadable-cache fallback behavior while preserving the existing cache-hit and cache-write expectations.
- `tests/test_douban_page_ui.py`
  Existing shared-entry-point confidence coverage for poster cards. No new tests should be necessary unless the poster-loader contract changes unexpectedly.
- `tests/test_player_window_ui.py`
  Existing shared-entry-point confidence coverage for player posters. No new tests should be necessary unless the poster-loader contract changes unexpectedly.

### Task 1: Lock the fallback behavior with failing poster-loader tests

**Files:**
- Modify: `tests/test_poster_loader.py`
- Test: `src/atv_player/ui/poster_loader.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_poster_loader.py`:

```python
def test_load_remote_poster_image_refetches_when_cached_bytes_are_corrupt(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    cache_dir.mkdir()
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)

    image_url = "https://img3.doubanio.com/view/photo/m/public/p123.jpg"
    poster_loader_module.poster_cache_path(image_url).write_bytes(b"not-an-image")
    poster_bytes = _png_bytes()
    calls: list[str] = []

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        calls.append(url)
        return FakeResponse(poster_bytes)

    loaded = load_remote_poster_image(image_url, QSize(90, 130), get=fake_get)

    assert loaded is not None
    assert loaded.isNull() is False
    assert calls == ["https://img3.doubanio.com/view/photo/m/public/p123.jpg"]
    assert poster_loader_module.poster_cache_path(image_url).read_bytes() == poster_bytes


def test_load_remote_poster_image_refetches_when_cache_read_fails(monkeypatch, tmp_path) -> None:
    cache_dir = tmp_path / "posters"
    cache_dir.mkdir()
    monkeypatch.setattr(poster_loader_module, "poster_cache_dir", lambda: cache_dir)

    image_url = "https://img3.doubanio.com/view/photo/m/public/p123.jpg"
    poster_bytes = _png_bytes()
    calls: list[str] = []

    def fake_read_bytes() -> bytes:
        raise OSError("permission denied")

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        calls.append(url)
        return FakeResponse(poster_bytes)

    monkeypatch.setattr(
        poster_loader_module.poster_cache_path(image_url),
        "read_bytes",
        fake_read_bytes,
    )

    loaded = load_remote_poster_image(image_url, QSize(90, 130), get=fake_get)

    assert loaded is not None
    assert loaded.isNull() is False
    assert calls == ["https://img3.doubanio.com/view/photo/m/public/p123.jpg"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_poster_loader.py -k "corrupt or read_fails" -v`
Expected: FAIL because `load_remote_poster_image()` currently returns early when cached bytes decode to `None`, so the corrupt-cache test should never hit the network path.

- [ ] **Step 3: Write minimal implementation**

Update `src/atv_player/ui/poster_loader.py` so cache decode failures fall through instead of returning early:

```python
def _load_cached_poster_image(cache_path: Path, target_size: QSize) -> QImage | None:
    try:
        cached_bytes = cache_path.read_bytes()
    except OSError:
        return None
    return _load_scaled_image_from_bytes(cached_bytes, target_size)


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
    cached_image = _load_cached_poster_image(cache_path, target_size)
    if cached_image is not None:
        return cached_image

    try:
        response = get(
            normalized_url,
            headers=build_poster_request_headers(normalized_url),
            timeout=timeout,
        )
        response.raise_for_status()
    except Exception:
        return None

    image = _load_scaled_image_from_bytes(response.content, target_size)
    if image is None:
        return None
    try:
        _write_poster_cache_bytes(cache_path, response.content)
    except OSError:
        pass
    return image
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_poster_loader.py -k "corrupt or read_fails" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_poster_loader.py src/atv_player/ui/poster_loader.py
git commit -m "fix: refetch posters when cache is unreadable"
```

### Task 2: Preserve existing cache behavior and UI integration

**Files:**
- Modify: `tests/test_poster_loader.py`
- Test: `tests/test_douban_page_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Keep the existing poster-loader regression set intact**

Ensure these existing tests remain in `tests/test_poster_loader.py` and still describe the required behavior:

```python
def test_load_remote_poster_image_reuses_cached_file(monkeypatch, tmp_path) -> None:
    ...


def test_load_remote_poster_image_writes_downloaded_bytes_to_cache(monkeypatch, tmp_path) -> None:
    ...


def test_load_remote_poster_image_returns_image_when_cache_write_fails(monkeypatch, tmp_path) -> None:
    ...
```

If the corrupt-cache test still exists under the old expectation:

```python
def test_load_remote_poster_image_returns_none_for_corrupt_cached_bytes(...):
```

replace it with the new refetch expectation from Task 1 rather than keeping both tests.

- [ ] **Step 2: Run the focused poster-loader suite**

Run: `uv run pytest tests/test_poster_loader.py -v`
Expected: PASS with cache-hit, cache-write, corrupt-cache fallback, and cache-write-failure coverage all green.

- [ ] **Step 3: Run the Douban poster integration test**

Run: `uv run pytest tests/test_douban_page_ui.py -k "renders_loaded_poster_icon_on_card" -v`
Expected: PASS, confirming the poster grid still works through the shared loader path.

- [ ] **Step 4: Run the player poster integration tests**

Run: `uv run pytest tests/test_player_window_ui.py -k "renders_poster or remote_poster or poster_overlay" -v`
Expected: PASS, confirming the player window still renders poster images through the same loader contract.

- [ ] **Step 5: Commit**

```bash
git add tests/test_poster_loader.py
git commit -m "test: cover local poster cache fallback behavior"
```

### Task 3: Final verification

**Files:**
- Test: `tests/test_poster_loader.py`
- Test: `tests/test_douban_page_ui.py`
- Test: `tests/test_player_window_ui.py`
- Modify: `src/atv_player/ui/poster_loader.py`

- [ ] **Step 1: Run the full verification command**

Run: `uv run pytest tests/test_poster_loader.py tests/test_douban_page_ui.py tests/test_player_window_ui.py -v`
Expected: PASS

- [ ] **Step 2: Commit the finished change**

```bash
git add src/atv_player/ui/poster_loader.py tests/test_poster_loader.py
git commit -m "fix: enforce local poster cache fallback"
```
