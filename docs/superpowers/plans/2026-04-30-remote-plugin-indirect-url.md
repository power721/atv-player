# Remote Plugin Indirect URL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let remote spider plugins load from a configured URL whose response body is either Python source code or a single indirect `http(s)` URL that points to the real source.

**Architecture:** Keep the plugin manager and repository unchanged, and confine the behavior change to `SpiderPluginLoader`. Add focused loader tests first, then introduce a narrow remote-source resolution helper that performs at most one extra fetch and still reuses the existing cached-file fallback path.

**Tech Stack:** Python, httpx, pytest, existing spider-plugin loader/cache flow

---

## File Structure

- Modify: `src/atv_player/plugins/loader.py`
  Add a narrow helper for detecting a single indirect URL in remote response text, fetch the final source once, and keep cache writes/fallback behavior in the loader path.
- Modify: `tests/test_spider_plugin_loader.py`
  Add coverage for one-hop indirect URL success, direct-source no-extra-fetch behavior, and fallback to cached plugin when the second fetch fails.

### Task 1: Lock The New Remote-Source Rules With Failing Tests

**Files:**
- Modify: `tests/test_spider_plugin_loader.py`
- Test: `tests/test_spider_plugin_loader.py`

- [ ] **Step 1: Write the failing indirect-URL and cache-fallback tests**

Add these tests to `tests/test_spider_plugin_loader.py` below the existing remote-loader tests:

```python
def test_loader_resolves_one_indirect_remote_url_before_loading_plugin(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        calls.append(url)
        if url == "https://example.com/plugin.txt":
            return httpx.Response(200, text="\nhttps://cdn.example.com/real-plugin.py\n")
        if url == "https://cdn.example.com/real-plugin.py":
            return httpx.Response(200, text=PLUGIN_SOURCE)
        raise AssertionError(f"Unexpected URL: {url}")

    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache", get=fake_get)
    config = SpiderPluginConfig(
        id=41,
        source_type="remote",
        source_value="https://example.com/plugin.txt",
        display_name="",
        enabled=True,
        sort_order=0,
    )

    loaded = loader.load(config, force_refresh=True)

    assert loaded.plugin_name == "红果短剧"
    assert calls == [
        "https://example.com/plugin.txt",
        "https://cdn.example.com/real-plugin.py",
    ]
    assert "class Spider(Spider):" in Path(loaded.config.cached_file_path).read_text(encoding="utf-8")


def test_loader_treats_python_text_as_source_instead_of_indirect_url(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, text=PLUGIN_SOURCE)

    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache", get=fake_get)
    config = SpiderPluginConfig(
        id=42,
        source_type="remote",
        source_value="https://example.com/direct.py",
        display_name="",
        enabled=True,
        sort_order=0,
    )

    loaded = loader.load(config, force_refresh=True)

    assert loaded.plugin_name == "红果短剧"
    assert calls == ["https://example.com/direct.py"]


def test_loader_reuses_cached_plugin_when_indirect_second_fetch_fails(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_get(url: str, timeout: float = 15.0, follow_redirects: bool = False) -> httpx.Response:
        calls.append(url)
        if url == "https://example.com/plugin.txt":
            return httpx.Response(200, text="https://cdn.example.com/real-plugin.py")
        if url == "https://cdn.example.com/real-plugin.py":
            if len(calls) == 2:
                return httpx.Response(200, text=PLUGIN_SOURCE)
            raise httpx.ConnectError("cdn down")
        raise AssertionError(f"Unexpected URL: {url}")

    loader = SpiderPluginLoader(cache_dir=tmp_path / "cache", get=fake_get)
    config = SpiderPluginConfig(
        id=43,
        source_type="remote",
        source_value="https://example.com/plugin.txt",
        display_name="",
        enabled=True,
        sort_order=0,
    )

    first = loader.load(config, force_refresh=True)
    second = loader.load(first.config, force_refresh=True)

    assert first.plugin_name == "红果短剧"
    assert second.plugin_name == "红果短剧"
    assert calls == [
        "https://example.com/plugin.txt",
        "https://cdn.example.com/real-plugin.py",
        "https://example.com/plugin.txt",
        "https://cdn.example.com/real-plugin.py",
    ]
```

- [ ] **Step 2: Run the targeted loader tests to verify the new behavior is not implemented yet**

Run:

```bash
uv run pytest tests/test_spider_plugin_loader.py::test_loader_resolves_one_indirect_remote_url_before_loading_plugin tests/test_spider_plugin_loader.py::test_loader_treats_python_text_as_source_instead_of_indirect_url tests/test_spider_plugin_loader.py::test_loader_reuses_cached_plugin_when_indirect_second_fetch_fails -q
```

Expected: the first and third tests FAIL because the loader currently writes the first response body directly to cache instead of resolving one indirect URL.

- [ ] **Step 3: Commit the red test state**

Run:

```bash
git add tests/test_spider_plugin_loader.py
git commit -m "test: cover indirect remote plugin urls"
```

### Task 2: Implement One-Hop Indirect URL Resolution In The Loader

**Files:**
- Modify: `src/atv_player/plugins/loader.py`
- Test: `tests/test_spider_plugin_loader.py`

- [ ] **Step 1: Add narrow remote-source resolution helpers**

Update `src/atv_player/plugins/loader.py` by adding these helpers inside `SpiderPluginLoader`:

```python
    def _fetch_remote_text(self, url: str) -> str:
        response = self._get(url, timeout=15.0, follow_redirects=True)
        if response.status_code >= 300:
            raise httpx.HTTPStatusError(
                f"Error response {response.status_code} while requesting {url}",
                request=response.request,
                response=response,
            )
        return response.text

    def _extract_indirect_url(self, text: str) -> str:
        candidate = text.strip()
        if not candidate.startswith(("http://", "https://")):
            return ""
        if any(char.isspace() for char in candidate):
            return ""
        return candidate

    def _resolve_remote_source_text(self, url: str) -> str:
        source_text = self._fetch_remote_text(url)
        indirect_url = self._extract_indirect_url(source_text)
        if not indirect_url:
            return source_text
        return self._fetch_remote_text(indirect_url)
```

- [ ] **Step 2: Route remote source loading through the new helper**

Replace the current remote-download block in `_resolve_source_path()` with:

```python
    def _resolve_source_path(self, config: SpiderPluginConfig, force_refresh: bool) -> Path:
        if config.source_type == "local":
            return Path(config.source_value)
        cache_path = self._cache_dir / f"plugin_{config.id}.py"
        if not force_refresh and config.cached_file_path:
            cached = Path(config.cached_file_path)
            if cached.is_file() and cached.stat().st_size > 0:
                logger.info("Use cached spider plugin id=%s path=%s", config.id, cached)
                return cached
        try:
            logger.info(
                "Download spider plugin id=%s source=%s force_refresh=%s",
                config.id,
                config.source_value,
                force_refresh,
            )
            source_text = self._resolve_remote_source_text(config.source_value)
            cache_path.write_text(source_text, encoding="utf-8")
            return cache_path
        except Exception:
            if cache_path.is_file() and cache_path.stat().st_size > 0:
                logger.warning("Spider plugin refresh failed, fallback to cache id=%s path=%s", config.id, cache_path)
                return cache_path
            raise
```

- [ ] **Step 3: Run the focused loader tests to verify the new behavior passes**

Run:

```bash
uv run pytest tests/test_spider_plugin_loader.py::test_loader_resolves_one_indirect_remote_url_before_loading_plugin tests/test_spider_plugin_loader.py::test_loader_treats_python_text_as_source_instead_of_indirect_url tests/test_spider_plugin_loader.py::test_loader_reuses_cached_plugin_when_indirect_second_fetch_fails -q
```

Expected: PASS

- [ ] **Step 4: Run the full spider-plugin loader test file to check for regressions**

Run:

```bash
uv run pytest tests/test_spider_plugin_loader.py -q
```

Expected: PASS

- [ ] **Step 5: Commit the loader implementation**

Run:

```bash
git add src/atv_player/plugins/loader.py tests/test_spider_plugin_loader.py
git commit -m "feat: resolve indirect remote plugin urls"
```

## Self-Review

- Spec coverage: the plan covers one-hop URL resolution, direct-source no-extra-fetch behavior, second-fetch cache fallback, unchanged storage/UI, and regression verification.
- Placeholder scan: no `TODO`, `TBD`, or unnamed “handle errors” steps remain; each code-changing step includes concrete code.
- Type consistency: the plan only introduces private loader helpers returning `str`, and keeps `_resolve_source_path()` returning `Path`, matching the existing loader contract.
