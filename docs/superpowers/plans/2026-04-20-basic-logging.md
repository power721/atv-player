# Basic Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add minimal console logging with default `INFO` level across app startup, API requests, player lifecycle, and plugin loading/resolution paths without leaking secrets.

**Architecture:** Introduce a single logging bootstrap helper and initialize it from the application entrypoint. Use per-module loggers at the application boundary layers so the logs stay useful and low-volume while keeping sensitive fields out of log messages.

**Tech Stack:** Python, stdlib `logging`, pytest `caplog`

---

### Task 1: Logging Bootstrap

**Files:**
- Create: `src/atv_player/logging_utils.py`
- Modify: `src/atv_player/main.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Write the failing test**

```python
def test_main_configures_logging_before_start(monkeypatch):
    configured_levels = []

    monkeypatch.setattr("atv_player.main.configure_logging", configured_levels.append)

    class DummyApp:
        def exec(self):
            return 0

    class DummyWidget:
        def show(self):
            return None

    class DummyCoordinator:
        def __init__(self, repo):
            self.repo = repo

        def start(self):
            return DummyWidget()

    monkeypatch.setattr("atv_player.main.build_application", lambda: (DummyApp(), object()))
    monkeypatch.setattr("atv_player.main.AppCoordinator", DummyCoordinator)

    assert main() == 0
    assert configured_levels == ["INFO"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main.py::test_main_configures_logging_before_start -v`
Expected: FAIL because `configure_logging` is not defined or not called.

- [ ] **Step 3: Write minimal implementation**

```python
def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
```

```python
def main() -> int:
    configure_logging("INFO")
    app, repo = build_application()
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main.py::test_main_configures_logging_before_start -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-04-20-basic-logging.md tests/test_main.py src/atv_player/main.py src/atv_player/logging_utils.py
git commit -m "feat: add basic logging bootstrap"
```

### Task 2: API Request Logging

**Files:**
- Modify: `src/atv_player/api.py`
- Test: `tests/test_api_client.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_request_logs_request_start_without_sensitive_payload(caplog):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    client = ApiClient("https://example.com", token="secret-token", transport=transport)

    with caplog.at_level(logging.INFO):
        client.login("alice", "super-secret")

    assert "API request" in caplog.text
    assert "/api/accounts/login" in caplog.text
    assert "secret-token" not in caplog.text
    assert "super-secret" not in caplog.text


def test_request_logs_failure(caplog):
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    client = ApiClient("https://example.com", transport=transport)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ApiError):
            client.get_capabilities()

    assert "API request failed" in caplog.text
    assert "/api/capabilities" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_client.py::test_request_logs_request_start_without_sensitive_payload tests/test_api_client.py::test_request_logs_failure -v`
Expected: FAIL because no matching log messages are emitted.

- [ ] **Step 3: Write minimal implementation**

```python
logger = logging.getLogger(__name__)

def _request(self, method: str, url: str, **kwargs: Any) -> Any:
    logger.info("API request %s %s params=%s", method, url, self._summarize_params(kwargs.get("params")))
    ...
    if response.is_error:
        logger.warning("API request failed %s %s status=%s", method, url, response.status_code)
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_client.py::test_request_logs_request_start_without_sensitive_payload tests/test_api_client.py::test_request_logs_failure -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_api_client.py src/atv_player/api.py
git commit -m "feat: log api request boundaries"
```

### Task 3: Player Lifecycle Logging

**Files:**
- Modify: `src/atv_player/controllers/player_controller.py`
- Test: `tests/test_player_controller.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_create_session_logs_session_details(caplog):
    controller = PlayerController(FakeApiClient())
    vod = VodItem(vod_id="vod-1", vod_name="Demo", vod_pic="", vod_remarks="")
    playlist = [PlayItem(title="EP1", url="https://media/1.m3u8", index=0)]

    with caplog.at_level(logging.INFO):
        controller.create_session(vod, playlist, clicked_index=0)

    assert "Create player session" in caplog.text
    assert "vod-1" in caplog.text


def test_report_progress_logs_progress(caplog):
    ...
    with caplog.at_level(logging.INFO):
        controller.report_progress(session, 0, 12, 1.0, 0, 0, paused=False)

    assert "Report playback progress" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_player_controller.py::test_create_session_logs_session_details tests/test_player_controller.py::test_report_progress_logs_progress -v`
Expected: FAIL because no matching player log messages are emitted.

- [ ] **Step 3: Write minimal implementation**

```python
logger = logging.getLogger(__name__)

logger.info(
    "Create player session vod_id=%s playlist_size=%s start_index=%s restore_history=%s",
    vod.vod_id,
    len(active_playlist),
    start_index,
    matched_history,
)
```

```python
logger.info(
    "Report playback progress vod_id=%s index=%s position_ms=%s paused=%s",
    session.vod.vod_id,
    current_index,
    position_ms,
    paused,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_player_controller.py::test_create_session_logs_session_details tests/test_player_controller.py::test_report_progress_logs_progress -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_player_controller.py src/atv_player/controllers/player_controller.py
git commit -m "feat: log player lifecycle events"
```

### Task 4: Plugin Loading and Resolution Logging

**Files:**
- Modify: `src/atv_player/plugins/loader.py`
- Modify: `src/atv_player/plugins/controller.py`
- Test: `tests/test_spider_plugin_loader.py`
- Test: `tests/test_spider_plugin_controller.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_loader_logs_loaded_plugin(tmp_path, caplog):
    loader = SpiderPluginLoader(tmp_path)
    config = SpiderPluginConfig(...)

    with caplog.at_level(logging.INFO):
        loaded = loader.load(config)

    assert loaded.plugin_name
    assert "Loaded spider plugin" in caplog.text


def test_plugin_controller_logs_search_failure(caplog):
    controller = SpiderPluginController(FailingSpider(), "demo", search_enabled=True)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ApiError):
            controller.search_items("hello", 1)

    assert "Spider plugin search failed" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_spider_plugin_loader.py::test_loader_logs_loaded_plugin tests/test_spider_plugin_controller.py::test_plugin_controller_logs_search_failure -v`
Expected: FAIL because no matching plugin log messages are emitted.

- [ ] **Step 3: Write minimal implementation**

```python
logger = logging.getLogger(__name__)

logger.info(
    "Loaded spider plugin id=%s name=%s source_type=%s search_enabled=%s",
    config.id,
    plugin_name or config.display_name,
    config.source_type,
    search_enabled,
)
```

```python
except Exception as exc:
    logger.exception("Spider plugin search failed plugin=%s keyword=%s", self._plugin_name, keyword)
    raise ApiError(str(exc)) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_spider_plugin_loader.py::test_loader_logs_loaded_plugin tests/test_spider_plugin_controller.py::test_plugin_controller_logs_search_failure -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_spider_plugin_loader.py tests/test_spider_plugin_controller.py src/atv_player/plugins/loader.py src/atv_player/plugins/controller.py
git commit -m "feat: log spider plugin lifecycle"
```
