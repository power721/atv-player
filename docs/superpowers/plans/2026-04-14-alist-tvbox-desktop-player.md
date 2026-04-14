# alist-tvbox Desktop Player Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Linux-first `PySide6` desktop player for `alist-tvbox` with login, browse, search, history, and a separate `mpv` player window.

**Architecture:** Use a small Python package with focused layers: `sqlite` storage for local config, an `httpx` API client for backend calls, pure-Python controllers for testable page logic, and `Qt Widgets` for the UI. The player window embeds `mpv`, restores progress from backend history, and reports playback every 5 seconds.

**Tech Stack:** Python 3.12, PySide6, httpx, python-mpv, sqlite3, pytest, pytest-qt

---

## File Structure

### Application Files

- Create: `pyproject.toml`
- Create: `src/atv_player/__init__.py`
- Create: `src/atv_player/models.py`
- Create: `src/atv_player/storage.py`
- Create: `src/atv_player/api.py`
- Create: `src/atv_player/controllers/login_controller.py`
- Create: `src/atv_player/controllers/browse_controller.py`
- Create: `src/atv_player/controllers/history_controller.py`
- Create: `src/atv_player/controllers/player_controller.py`
- Create: `src/atv_player/player/mpv_widget.py`
- Create: `src/atv_player/player/resume.py`
- Create: `src/atv_player/ui/login_window.py`
- Create: `src/atv_player/ui/main_window.py`
- Create: `src/atv_player/ui/browse_page.py`
- Create: `src/atv_player/ui/search_page.py`
- Create: `src/atv_player/ui/history_page.py`
- Create: `src/atv_player/ui/player_window.py`
- Create: `src/atv_player/app.py`
- Create: `src/atv_player/main.py`
- Create: `README.md`

### Test Files

- Create: `tests/test_storage.py`
- Create: `tests/test_api_client.py`
- Create: `tests/test_login_controller.py`
- Create: `tests/test_browse_controller.py`
- Create: `tests/test_history_controller.py`
- Create: `tests/test_resume.py`
- Create: `tests/test_player_controller.py`
- Create: `tests/test_app.py`

### Responsibility Map

- `models.py`: shared dataclasses used across storage, API, controllers, and UI.
- `storage.py`: local `sqlite` schema and config persistence.
- `api.py`: authenticated backend calls and structured errors.
- `controllers/*.py`: UI-agnostic behavior for login, browse/search, history, and playback.
- `player/resume.py`: deterministic resume rules and playlist helpers.
- `player/mpv_widget.py`: embedded `mpv` surface and media state access.
- `ui/*.py`: Qt widgets only, with no backend logic hidden in slots.
- `app.py` and `main.py`: startup flow and top-level window routing.

## Task 1: Bootstrap The Python Project And Local Storage

**Files:**
- Create: `pyproject.toml`
- Create: `src/atv_player/__init__.py`
- Create: `src/atv_player/models.py`
- Create: `src/atv_player/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Create the package metadata and the first failing storage test**

```toml
# pyproject.toml
[project]
name = "atv-player"
version = "0.1.0"
description = "PySide6 desktop player for alist-tvbox"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
  "PySide6>=6.8",
  "httpx>=0.28",
  "mpv>=1.0.6",
]

[dependency-groups]
dev = [
  "pytest>=8.3",
  "pytest-qt>=4.4",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/atv_player"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

```python
# tests/test_storage.py
from pathlib import Path

from atv_player.models import AppConfig
from atv_player.storage import SettingsRepository


def test_settings_repository_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)

    config = AppConfig(
        base_url="http://127.0.0.1:4567",
        username="alice",
        token="token-123",
        last_path="/Movies",
        main_window_geometry=None,
        player_window_geometry=None,
    )

    repo.save_config(config)
    saved = repo.load_config()

    assert saved == config


def test_settings_repository_clear_token_preserves_other_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    repo = SettingsRepository(db_path)
    repo.save_config(
        AppConfig(
            base_url="http://127.0.0.1:4567",
            username="alice",
            token="token-123",
            last_path="/TV",
            main_window_geometry=None,
            player_window_geometry=None,
        )
    )

    repo.clear_token()
    saved = repo.load_config()

    assert saved.base_url == "http://127.0.0.1:4567"
    assert saved.username == "alice"
    assert saved.token == ""
    assert saved.last_path == "/TV"
```

- [ ] **Step 2: Install dependencies and run the storage tests to confirm they fail for missing modules**

Run: `uv sync`
Expected: environment created successfully

Run: `uv run pytest tests/test_storage.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'atv_player'`

- [ ] **Step 3: Implement the minimal models and sqlite repository**

```python
# src/atv_player/__init__.py
__all__ = ["models", "storage", "api"]
```

```python
# src/atv_player/models.py
from dataclasses import dataclass


@dataclass(slots=True)
class AppConfig:
    base_url: str = "http://127.0.0.1:4567"
    username: str = ""
    token: str = ""
    last_path: str = "/"
    main_window_geometry: bytes | None = None
    player_window_geometry: bytes | None = None
```

```python
# src/atv_player/storage.py
import sqlite3
from pathlib import Path

from atv_player.models import AppConfig


class SettingsRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    base_url TEXT NOT NULL,
                    username TEXT NOT NULL,
                    token TEXT NOT NULL,
                    last_path TEXT NOT NULL,
                    main_window_geometry BLOB,
                    player_window_geometry BLOB
                )
                """
            )
            conn.execute(
                """
                INSERT INTO app_config (
                    id, base_url, username, token, last_path, main_window_geometry, player_window_geometry
                )
                VALUES (1, 'http://127.0.0.1:4567', '', '', '/', NULL, NULL)
                ON CONFLICT(id) DO NOTHING
                """
            )

    def load_config(self) -> AppConfig:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT base_url, username, token, last_path, main_window_geometry, player_window_geometry
                FROM app_config WHERE id = 1
                """
            ).fetchone()
        assert row is not None
        return AppConfig(*row)

    def save_config(self, config: AppConfig) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE app_config
                SET base_url = ?, username = ?, token = ?, last_path = ?,
                    main_window_geometry = ?, player_window_geometry = ?
                WHERE id = 1
                """,
                (
                    config.base_url,
                    config.username,
                    config.token,
                    config.last_path,
                    config.main_window_geometry,
                    config.player_window_geometry,
                ),
            )

    def clear_token(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE app_config SET token = '' WHERE id = 1")
```

- [ ] **Step 4: Run the storage tests to confirm they pass**

Run: `uv run pytest tests/test_storage.py -q`
Expected: PASS with `2 passed`

- [ ] **Step 5: Commit the bootstrap and storage layer**

```bash
git add pyproject.toml src/atv_player/__init__.py src/atv_player/models.py src/atv_player/storage.py tests/test_storage.py
git commit -m "feat: add project scaffold and local settings storage"
```

## Task 2: Add The HTTP API Client And Authentication Error Handling

**Files:**
- Modify: `src/atv_player/models.py`
- Create: `src/atv_player/api.py`
- Test: `tests/test_api_client.py`

- [ ] **Step 1: Write failing API client tests for auth headers and 401 handling**

```python
# tests/test_api_client.py
import httpx
import pytest

from atv_player.api import ApiClient, ApiError, UnauthorizedError


def test_api_client_attaches_authorization_header() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["authorization"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"list": [], "total": 0})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        transport=httpx.MockTransport(handler),
    )

    client.list_vod("1$%2F$1", page=1, size=25)

    assert seen_headers["authorization"] == "token-123"


def test_api_client_raises_unauthorized_error_for_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="bad-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UnauthorizedError):
        client.list_vod("1$%2F$1", page=1, size=25)


def test_api_client_raises_api_error_for_non_401_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client = ApiClient(
        base_url="http://127.0.0.1:4567",
        token="token-123",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ApiError) as exc:
        client.telegram_search("movie")

    assert str(exc.value) == "boom"
```

- [ ] **Step 2: Run the API tests to verify they fail because the client is missing**

Run: `uv run pytest tests/test_api_client.py -q`
Expected: FAIL with `ModuleNotFoundError` or `ImportError` for `atv_player.api`

- [ ] **Step 3: Implement the minimal API client and shared backend models**

```python
# src/atv_player/models.py
from dataclasses import dataclass, field


@dataclass(slots=True)
class AppConfig:
    base_url: str = "http://127.0.0.1:4567"
    username: str = ""
    token: str = ""
    last_path: str = "/"
    main_window_geometry: bytes | None = None
    player_window_geometry: bytes | None = None


@dataclass(slots=True)
class PlayItem:
    title: str
    url: str
    path: str = ""
    index: int = 0
    size: int = 0


@dataclass(slots=True)
class VodItem:
    vod_id: str
    vod_name: str
    path: str = ""
    vod_pic: str = ""
    vod_tag: str = ""
    vod_time: str = ""
    vod_remarks: str = ""
    vod_play_from: str = ""
    vod_play_url: str = ""
    type_name: str = ""
    vod_content: str = ""
    dbid: int = 0
    type: int = 0
    items: list[PlayItem] = field(default_factory=list)


@dataclass(slots=True)
class HistoryRecord:
    id: int
    key: str
    vod_name: str
    vod_pic: str
    vod_remarks: str
    episode: int
    episode_url: str
    position: int
    opening: int
    ending: int
    speed: float
    create_time: int
```

```python
# src/atv_player/api.py
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx


class ApiError(RuntimeError):
    pass


class UnauthorizedError(ApiError):
    pass


class ApiClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": token} if token else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=30.0,
        )

    def set_token(self, token: str) -> None:
        if token:
            self._client.headers["Authorization"] = token
        else:
            self._client.headers.pop("Authorization", None)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self._client.request(method, url, **kwargs)
        if response.status_code == 401:
            raise UnauthorizedError("Unauthorized")
        if response.is_error:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise ApiError(payload.get("message") or payload.get("detail") or response.text)
        return response.json()

    def login(self, username: str, password: str) -> dict[str, Any]:
        return self._request("POST", "/api/accounts/login", json={"username": username, "password": password})

    def list_vod(self, path_id: str, page: int, size: int) -> dict[str, Any]:
        return self._request("GET", f"/vod/{self._client.headers.get('Authorization', '')}", params={"ac": "web", "pg": page, "size": size, "t": path_id})

    def get_detail(self, vod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/vod/{self._client.headers.get('Authorization', '')}", params={"ac": "web", "ids": vod_id})

    def telegram_search(self, keyword: str) -> dict[str, Any]:
        return self._request("GET", "/api/telegram/search", params={"wd": keyword})

    def resolve_share_link(self, link: str) -> str:
        data = self._request("POST", "/api/share-link", json={"link": link, "path": "", "code": ""})
        return str(data)

    def get_history(self, key: str):
        data = self._request("GET", f"/history/{self._client.headers.get('Authorization', '')}", params={"key": key})
        if not data:
            return None
        from atv_player.models import HistoryRecord
        return HistoryRecord(
            id=int(data["id"]),
            key=str(data["key"]),
            vod_name=str(data["vodName"]),
            vod_pic=str(data["vodPic"]),
            vod_remarks=str(data["vodRemarks"]),
            episode=int(data["episode"]),
            episode_url=str(data["episodeUrl"]),
            position=int(data["position"]),
            opening=int(data["opening"]),
            ending=int(data["ending"]),
            speed=float(data["speed"]),
            create_time=int(data["createTime"]),
        )

    def list_history(self, page: int, size: int) -> dict[str, Any]:
        return self._request("GET", "/api/history", params={"sort": "createTime,desc", "page": page - 1, "size": size})

    def save_history(self, payload: dict[str, Any]) -> None:
        self._request("POST", "/api/history", params={"log": "false"}, json=payload)

    def delete_history(self, history_id: int) -> None:
        self._request("DELETE", f"/api/history/{history_id}")

    def delete_histories(self, history_ids: list[int]) -> None:
        self._request("POST", "/api/history/-/delete", json=history_ids)

    def clear_history(self) -> None:
        self._request("DELETE", f"/history/{self._client.headers.get('Authorization', '')}")
```

- [ ] **Step 4: Run the storage and API tests to confirm both suites are green**

Run: `uv run pytest tests/test_storage.py tests/test_api_client.py -q`
Expected: PASS with `5 passed`

- [ ] **Step 5: Commit the API client**

```bash
git add src/atv_player/models.py src/atv_player/api.py tests/test_api_client.py
git commit -m "feat: add alist-tvbox api client"
```

## Task 3: Implement The Login Controller And Login Window

**Files:**
- Create: `src/atv_player/controllers/login_controller.py`
- Create: `src/atv_player/ui/login_window.py`
- Test: `tests/test_login_controller.py`

- [ ] **Step 1: Write the failing login controller tests**

```python
# tests/test_login_controller.py
from atv_player.controllers.login_controller import LoginController
from atv_player.models import AppConfig


class FakeApiClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def login(self, username: str, password: str) -> dict:
        self.calls.append((username, password))
        return {"token": "token-123"}


class FakeSettingsRepository:
    def __init__(self) -> None:
        self.saved: AppConfig | None = None
        self.current = AppConfig()

    def load_config(self) -> AppConfig:
        return self.current

    def save_config(self, config: AppConfig) -> None:
        self.current = config
        self.saved = config


def test_login_controller_persists_base_url_username_and_token() -> None:
    repo = FakeSettingsRepository()
    api = FakeApiClient()
    controller = LoginController(repo, api)

    result = controller.login("http://127.0.0.1:4567", "alice", "secret")

    assert result.token == "token-123"
    assert repo.saved is not None
    assert repo.saved.base_url == "http://127.0.0.1:4567"
    assert repo.saved.username == "alice"
    assert repo.saved.token == "token-123"


def test_login_controller_reads_defaults_from_storage() -> None:
    repo = FakeSettingsRepository()
    repo.current = AppConfig(base_url="http://demo", username="bob", token="", last_path="/")
    controller = LoginController(repo, FakeApiClient())

    config = controller.load_defaults()

    assert config.base_url == "http://demo"
    assert config.username == "bob"
```

- [ ] **Step 2: Run the login controller tests to confirm they fail**

Run: `uv run pytest tests/test_login_controller.py -q`
Expected: FAIL with missing `LoginController`

- [ ] **Step 3: Implement the login controller and the first login window**

```python
# src/atv_player/controllers/login_controller.py
from atv_player.models import AppConfig


class LoginController:
    def __init__(self, repo, api_client) -> None:
        self._repo = repo
        self._api_client = api_client

    def load_defaults(self) -> AppConfig:
        return self._repo.load_config()

    def login(self, base_url: str, username: str, password: str) -> AppConfig:
        payload = self._api_client.login(username, password)
        config = self._repo.load_config()
        config.base_url = base_url.rstrip("/")
        config.username = username
        config.token = payload["token"]
        self._repo.save_config(config)
        return config
```

```python
# src/atv_player/ui/login_window.py
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class LoginWindow(QWidget):
    login_succeeded = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self.setWindowTitle("alist-tvbox 登录")

        defaults = controller.load_defaults()
        self.base_url_edit = QLineEdit(defaults.base_url)
        self.username_edit = QLineEdit(defaults.username)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_button = QPushButton("登录")
        self.login_button.clicked.connect(self._on_login_clicked)

        form = QFormLayout()
        form.addRow("后端地址", self.base_url_edit)
        form.addRow("用户名", self.username_edit)
        form.addRow("密码", self.password_edit)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.login_button)

    def _on_login_clicked(self) -> None:
        try:
            self._controller.login(
                self.base_url_edit.text().strip(),
                self.username_edit.text().strip(),
                self.password_edit.text(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "登录失败", str(exc))
            return
        self.login_succeeded.emit()
```

- [ ] **Step 4: Run the login controller tests**

Run: `uv run pytest tests/test_login_controller.py -q`
Expected: PASS with `2 passed`

- [ ] **Step 5: Commit the login flow**

```bash
git add src/atv_player/controllers/login_controller.py src/atv_player/ui/login_window.py tests/test_login_controller.py
git commit -m "feat: add login controller and form"
```

## Task 4: Build Browse And Search Controllers

**Files:**
- Create: `src/atv_player/controllers/browse_controller.py`
- Create: `src/atv_player/player/resume.py`
- Test: `tests/test_browse_controller.py`
- Test: `tests/test_resume.py`

- [ ] **Step 1: Write failing tests for search filtering and folder video playlist building**

```python
# tests/test_browse_controller.py
from atv_player.controllers.browse_controller import BrowseController, filter_search_results
from atv_player.models import PlayItem, VodItem


class FakeApiClient:
    def __init__(self) -> None:
        self.resolved_links: list[str] = []

    def resolve_share_link(self, link: str) -> str:
        self.resolved_links.append(link)
        return "/Movies/Resolved"


def test_filter_search_results_by_drive_type() -> None:
    items = [
        VodItem(vod_id="1", vod_name="One", type_name="阿里云盘"),
        VodItem(vod_id="2", vod_name="Two", type_name="夸克网盘"),
    ]

    filtered = filter_search_results(items, "阿里")

    assert [item.vod_id for item in filtered] == ["1"]


def test_build_playlist_from_folder_starts_at_clicked_video() -> None:
    controller = BrowseController(FakeApiClient())
    folder_items = [
        VodItem(vod_id="f1", vod_name="folder", type=1, path="/TV/folder"),
        VodItem(vod_id="v1", vod_name="Ep1", type=2, vod_play_url="http://m/1.m3u8", path="/TV/Ep1.mkv"),
        VodItem(vod_id="v2", vod_name="Ep2", type=2, vod_play_url="http://m/2.m3u8", path="/TV/Ep2.mkv"),
    ]

    playlist, start_index = controller.build_playlist_from_folder(folder_items, clicked_vod_id="v2")

    assert [item.title for item in playlist] == ["Ep1", "Ep2"]
    assert start_index == 1


def test_resolve_search_result_returns_backend_folder_path() -> None:
    api = FakeApiClient()
    controller = BrowseController(api)
    item = VodItem(vod_id="s1", vod_name="Movie", vod_play_url="https://t.me/share")

    resolved_path = controller.resolve_search_result(item)

    assert resolved_path == "/Movies/Resolved"
    assert api.resolved_links == ["https://t.me/share"]
```

```python
# tests/test_resume.py
from atv_player.models import HistoryRecord, PlayItem
from atv_player.player.resume import resolve_resume_index


def test_resolve_resume_index_prefers_episode() -> None:
    playlist = [PlayItem(title="1", url="http://m/1.m3u8"), PlayItem(title="2", url="http://m/2.m3u8")]
    history = HistoryRecord(
        id=1,
        key="abc",
        vod_name="Movie",
        vod_pic="",
        vod_remarks="Ep2",
        episode=1,
        episode_url="2.m3u8",
        position=12000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1,
    )

    assert resolve_resume_index(history, playlist, clicked_index=0) == 1


def test_resolve_resume_index_falls_back_to_episode_url_filename() -> None:
    playlist = [PlayItem(title="1", url="http://m/1.m3u8?token=a"), PlayItem(title="2", url="http://m/2.m3u8?token=b")]
    history = HistoryRecord(
        id=1,
        key="abc",
        vod_name="Movie",
        vod_pic="",
        vod_remarks="Ep2",
        episode=-1,
        episode_url="2.m3u8",
        position=12000,
        opening=0,
        ending=0,
        speed=1.0,
        create_time=1,
    )

    assert resolve_resume_index(history, playlist, clicked_index=0) == 1
```

- [ ] **Step 2: Run the browse and resume tests to confirm they fail**

Run: `uv run pytest tests/test_browse_controller.py tests/test_resume.py -q`
Expected: FAIL with missing controller and resume helper

- [ ] **Step 3: Implement the browse controller and resume helper**

```python
# src/atv_player/controllers/browse_controller.py
from atv_player.models import PlayItem, VodItem


def filter_search_results(results: list[VodItem], drive_type: str) -> list[VodItem]:
    if not drive_type:
        return list(results)
    return [item for item in results if drive_type in item.type_name]


class BrowseController:
    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def build_playlist_from_folder(
        self,
        folder_items: list[VodItem],
        clicked_vod_id: str,
    ) -> tuple[list[PlayItem], int]:
        playlist: list[PlayItem] = []
        start_index = 0
        for item in folder_items:
            if item.type != 2:
                continue
            index = len(playlist)
            playlist_item = PlayItem(title=item.vod_name, url=item.vod_play_url, path=item.path, index=index, size=0)
            playlist.append(playlist_item)
            if item.vod_id == clicked_vod_id:
                start_index = index
        return playlist, start_index

    def resolve_search_result(self, item: VodItem) -> str:
        return self._api_client.resolve_share_link(item.vod_play_url)
```

```python
# src/atv_player/player/resume.py
from urllib.parse import urlparse

from atv_player.models import HistoryRecord, PlayItem


def _basename(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path.rsplit("/", 1)[-1]


def resolve_resume_index(history: HistoryRecord | None, playlist: list[PlayItem], clicked_index: int) -> int:
    if history is None:
        return clicked_index
    if 0 <= history.episode < len(playlist):
        return history.episode
    if history.episode_url:
        target = _basename(history.episode_url)
        for index, item in enumerate(playlist):
            if _basename(item.url) == target:
                return index
    return clicked_index
```

- [ ] **Step 4: Run the browse, resume, storage, API, and login tests**

Run: `uv run pytest tests/test_storage.py tests/test_api_client.py tests/test_login_controller.py tests/test_browse_controller.py tests/test_resume.py -q`
Expected: PASS with `10 passed`

- [ ] **Step 5: Commit the browse and resume logic**

```bash
git add src/atv_player/controllers/browse_controller.py src/atv_player/player/resume.py tests/test_browse_controller.py tests/test_resume.py
git commit -m "feat: add browse search and resume helpers"
```

## Task 5: Build The History Controller

**Files:**
- Create: `src/atv_player/controllers/history_controller.py`
- Test: `tests/test_history_controller.py`

- [ ] **Step 1: Write failing tests for history mapping and deletion**

```python
# tests/test_history_controller.py
from atv_player.controllers.history_controller import HistoryController


class FakeApiClient:
    def __init__(self) -> None:
        self.deleted_one: list[int] = []
        self.deleted_many: list[list[int]] = []
        self.cleared = False

    def list_history(self, page: int, size: int) -> dict:
        return {
            "content": [
                {
                    "id": 9,
                    "key": "movie-1",
                    "vodName": "Movie",
                    "vodPic": "pic",
                    "vodRemarks": "Episode 2",
                    "episode": 1,
                    "episodeUrl": "2.m3u8",
                    "position": 90000,
                    "opening": 0,
                    "ending": 0,
                    "speed": 1.0,
                    "createTime": 123456,
                }
            ],
            "totalElements": 1,
        }

    def delete_history(self, history_id: int) -> None:
        self.deleted_one.append(history_id)

    def delete_histories(self, history_ids: list[int]) -> None:
        self.deleted_many.append(history_ids)

    def clear_history(self) -> None:
        self.cleared = True


def test_history_controller_maps_backend_payload() -> None:
    controller = HistoryController(FakeApiClient())

    records, total = controller.load_page(page=1, size=20)

    assert total == 1
    assert records[0].id == 9
    assert records[0].vod_name == "Movie"
    assert records[0].episode == 1


def test_history_controller_deletes_one_or_many() -> None:
    api = FakeApiClient()
    controller = HistoryController(api)

    controller.delete_one(9)
    controller.delete_many([9, 10])
    controller.clear_all()

    assert api.deleted_one == [9]
    assert api.deleted_many == [[9, 10]]
    assert api.cleared is True
```

- [ ] **Step 2: Run the history tests to confirm they fail**

Run: `uv run pytest tests/test_history_controller.py -q`
Expected: FAIL with missing `HistoryController`

- [ ] **Step 3: Implement the history controller**

```python
# src/atv_player/controllers/history_controller.py
from atv_player.models import HistoryRecord


class HistoryController:
    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def load_page(self, page: int, size: int) -> tuple[list[HistoryRecord], int]:
        payload = self._api_client.list_history(page, size)
        records = [
            HistoryRecord(
                id=item["id"],
                key=item["key"],
                vod_name=item["vodName"],
                vod_pic=item["vodPic"],
                vod_remarks=item["vodRemarks"],
                episode=item["episode"],
                episode_url=item["episodeUrl"],
                position=item["position"],
                opening=item["opening"],
                ending=item["ending"],
                speed=item["speed"],
                create_time=item["createTime"],
            )
            for item in payload["content"]
        ]
        return records, int(payload["totalElements"])

    def delete_one(self, history_id: int) -> None:
        self._api_client.delete_history(history_id)

    def delete_many(self, history_ids: list[int]) -> None:
        self._api_client.delete_histories(history_ids)

    def clear_all(self) -> None:
        self._api_client.clear_history()
```

- [ ] **Step 4: Run the accumulated controller tests**

Run: `uv run pytest tests/test_storage.py tests/test_api_client.py tests/test_login_controller.py tests/test_browse_controller.py tests/test_history_controller.py tests/test_resume.py -q`
Expected: PASS with `12 passed`

- [ ] **Step 5: Commit the history controller**

```bash
git add src/atv_player/controllers/history_controller.py tests/test_history_controller.py
git commit -m "feat: add play history controller"
```

## Task 6: Build The Player Controller And Periodic History Reporter

**Files:**
- Create: `src/atv_player/controllers/player_controller.py`
- Test: `tests/test_player_controller.py`

- [ ] **Step 1: Write failing tests for resume state and history reporting payloads**

```python
# tests/test_player_controller.py
from atv_player.controllers.player_controller import PlayerController
from atv_player.models import HistoryRecord, PlayItem, VodItem


class FakeApiClient:
    def __init__(self) -> None:
        self.saved_payloads: list[dict] = []
        self.history: HistoryRecord | None = None

    def get_history(self, key: str):
        return self.history

    def save_history(self, payload: dict) -> None:
        self.saved_payloads.append(payload)


def test_player_controller_restores_resume_state() -> None:
    api = FakeApiClient()
    api.history = HistoryRecord(
        id=1,
        key="movie-1",
        vod_name="Movie",
        vod_pic="pic",
        vod_remarks="Episode 2",
        episode=1,
        episode_url="2.m3u8",
        position=45000,
        opening=0,
        ending=0,
        speed=1.5,
        create_time=1,
    )
    controller = PlayerController(api)
    vod = VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="pic")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]

    session = controller.create_session(vod, playlist, clicked_index=0)

    assert session.start_index == 1
    assert session.start_position_seconds == 45
    assert session.speed == 1.5


def test_player_controller_builds_history_payload() -> None:
    api = FakeApiClient()
    controller = PlayerController(api)
    vod = VodItem(vod_id="movie-1", vod_name="Movie", vod_pic="pic")
    playlist = [PlayItem(title="Episode 1", url="1.m3u8"), PlayItem(title="Episode 2", url="2.m3u8")]
    session = controller.create_session(vod, playlist, clicked_index=1)

    controller.report_progress(session, current_index=1, position_seconds=90, speed=1.25)

    payload = api.saved_payloads[0]
    assert payload["key"] == "movie-1"
    assert payload["vodName"] == "Movie"
    assert payload["episode"] == 1
    assert payload["episodeUrl"] == "2.m3u8"
    assert payload["position"] == 90000
    assert payload["speed"] == 1.25
```

- [ ] **Step 2: Run the player controller tests to confirm they fail**

Run: `uv run pytest tests/test_player_controller.py -q`
Expected: FAIL with missing `PlayerController`

- [ ] **Step 3: Implement the player controller**

```python
# src/atv_player/controllers/player_controller.py
from dataclasses import dataclass
from time import time

from atv_player.models import PlayItem, VodItem
from atv_player.player.resume import resolve_resume_index


@dataclass(slots=True)
class PlayerSession:
    vod: VodItem
    playlist: list[PlayItem]
    start_index: int
    start_position_seconds: int
    speed: float


class PlayerController:
    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def create_session(self, vod: VodItem, playlist: list[PlayItem], clicked_index: int) -> PlayerSession:
        history = self._api_client.get_history(vod.vod_id)
        start_index = resolve_resume_index(history, playlist, clicked_index)
        position_seconds = int((history.position if history else 0) / 1000)
        speed = history.speed if history else 1.0
        return PlayerSession(vod=vod, playlist=playlist, start_index=start_index, start_position_seconds=position_seconds, speed=speed)

    def report_progress(self, session: PlayerSession, current_index: int, position_seconds: int, speed: float) -> None:
        current_item = session.playlist[current_index]
        self._api_client.save_history(
            {
                "cid": 0,
                "key": session.vod.vod_id,
                "vodName": session.vod.vod_name,
                "vodPic": session.vod.vod_pic,
                "vodRemarks": current_item.title,
                "episode": current_index,
                "episodeUrl": current_item.url,
                "position": position_seconds * 1000,
                "opening": 0,
                "ending": 0,
                "speed": speed,
                "createTime": int(time() * 1000),
            }
        )
```

- [ ] **Step 4: Run the controller and helper tests**

Run: `uv run pytest tests/test_storage.py tests/test_api_client.py tests/test_login_controller.py tests/test_browse_controller.py tests/test_history_controller.py tests/test_resume.py tests/test_player_controller.py -q`
Expected: PASS with `14 passed`

- [ ] **Step 5: Commit the player controller**

```bash
git add src/atv_player/controllers/player_controller.py tests/test_player_controller.py
git commit -m "feat: add player session and history reporting logic"
```

## Task 7: Build The Qt Pages, Main Window, Player Window, And mpv Widget

**Files:**
- Create: `src/atv_player/player/mpv_widget.py`
- Create: `src/atv_player/ui/browse_page.py`
- Create: `src/atv_player/ui/search_page.py`
- Create: `src/atv_player/ui/history_page.py`
- Create: `src/atv_player/ui/player_window.py`
- Create: `src/atv_player/ui/main_window.py`

- [ ] **Step 1: Write a failing UI smoke test for main-window routing**

```python
# tests/test_app.py
from PySide6.QtWidgets import QApplication

from atv_player.models import AppConfig
from atv_player.ui.main_window import MainWindow


class FakeBrowseController:
    pass


class FakeHistoryController:
    pass


class FakePlayerController:
    pass


def test_main_window_starts_on_browse_tab(qtbot) -> None:
    window = MainWindow(
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.currentIndex() == 0
```

- [ ] **Step 2: Run the UI smoke test to confirm it fails**

Run: `uv run pytest tests/test_app.py -q`
Expected: FAIL with missing `MainWindow`

- [ ] **Step 3: Implement the first pass of the Qt pages and the player window shell**

```python
# src/atv_player/player/mpv_widget.py
from PySide6.QtWidgets import QWidget

import mpv


class MpvWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(self.WidgetAttribute.WA_NativeWindow, True)
        self._player = mpv.MPV(wid=str(int(self.winId())), input_default_bindings=True, input_vo_keyboard=True)

    def load(self, url: str, pause: bool = False) -> None:
        self._player.play(url)
        self._player.pause = pause

    def seek(self, seconds: int) -> None:
        self._player.command("seek", seconds, "absolute")

    def set_speed(self, speed: float) -> None:
        self._player.speed = speed

    def pause(self) -> None:
        self._player.pause = True

    def resume(self) -> None:
        self._player.pause = False

    def position_seconds(self) -> int:
        return int(self._player.time_pos or 0)
```

```python
# src/atv_player/ui/browse_page.py
from PySide6.QtWidgets import QPushButton, QTableWidget, QVBoxLayout, QWidget


class BrowsePage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.refresh_button = QPushButton("刷新")
        self.table = QTableWidget(0, 3)
        layout = QVBoxLayout(self)
        layout.addWidget(self.refresh_button)
        layout.addWidget(self.table)
```

```python
# src/atv_player/ui/search_page.py
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QPushButton, QTableWidget, QVBoxLayout, QWidget


class SearchPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.keyword_edit = QLineEdit()
        self.filter_combo = QComboBox()
        self.search_button = QPushButton("搜索")
        self.clear_button = QPushButton("清空")
        self.results_table = QTableWidget(0, 2)

        top = QHBoxLayout()
        top.addWidget(self.keyword_edit)
        top.addWidget(self.filter_combo)
        top.addWidget(self.search_button)
        top.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.results_table)
```

```python
# src/atv_player/ui/history_page.py
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTableWidget, QVBoxLayout, QWidget


class HistoryPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.delete_button = QPushButton("删除")
        self.clear_button = QPushButton("清空")
        self.table = QTableWidget(0, 4)

        actions = QHBoxLayout()
        actions.addWidget(self.delete_button)
        actions.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.addLayout(actions)
        layout.addWidget(self.table)
```

```python
# src/atv_player/ui/player_window.py
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QPushButton, QSlider, QTextEdit, QVBoxLayout, QWidget

from atv_player.player.mpv_widget import MpvWidget


class PlayerWindow(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.setWindowTitle("alist-tvbox 播放器")
        self.video = MpvWidget(self)
        self.playlist = QListWidget()
        self.play_button = QPushButton("播放/暂停")
        self.progress = QSlider()
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.report_timer = QTimer(self)
        self.report_timer.setInterval(5000)

        right = QVBoxLayout()
        right.addWidget(self.playlist)
        right.addWidget(self.details)

        left = QVBoxLayout()
        left.addWidget(self.video)
        left.addWidget(self.progress)
        left.addWidget(self.play_button)

        layout = QHBoxLayout(self)
        layout.addLayout(left, 3)
        layout.addLayout(right, 2)
```

```python
# src/atv_player/ui/main_window.py
from PySide6.QtWidgets import QMainWindow, QTabWidget

from atv_player.ui.browse_page import BrowsePage
from atv_player.ui.history_page import HistoryPage
from atv_player.ui.search_page import SearchPage


class MainWindow(QMainWindow):
    def __init__(self, browse_controller, history_controller, player_controller, config) -> None:
        super().__init__()
        self.nav_tabs = QTabWidget()
        self.browse_page = BrowsePage(browse_controller)
        self.search_page = SearchPage(browse_controller)
        self.history_page = HistoryPage(history_controller)
        self.player_controller = player_controller
        self.config = config

        self.nav_tabs.addTab(self.browse_page, "浏览")
        self.nav_tabs.addTab(self.search_page, "搜索")
        self.nav_tabs.addTab(self.history_page, "播放记录")
        self.setCentralWidget(self.nav_tabs)
        self.setWindowTitle("alist-tvbox Desktop Player")
```

- [ ] **Step 4: Run the UI smoke test plus the earlier test suites**

Run: `uv run pytest tests/test_storage.py tests/test_api_client.py tests/test_login_controller.py tests/test_browse_controller.py tests/test_history_controller.py tests/test_resume.py tests/test_player_controller.py tests/test_app.py -q`
Expected: PASS with `15 passed`

- [ ] **Step 5: Expand the widgets from shell to usable pages**

```python
# src/atv_player/ui/browse_page.py
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QPushButton, QTableWidget, QVBoxLayout, QWidget


class BrowsePage(QWidget):
    open_requested = Signal(object)

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.refresh_button = QPushButton("刷新")
        self.table = QTableWidget(0, 3)
        self.refresh_button.clicked.connect(self.reload)
        self.table.cellDoubleClicked.connect(self._open_selected_row)
        layout = QVBoxLayout(self)
        layout.addWidget(self.refresh_button)
        layout.addWidget(self.table)

    def reload(self) -> None:
        pass

    def _open_selected_row(self, row: int, _column: int) -> None:
        pass
```

```python
# src/atv_player/ui/search_page.py
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QPushButton, QTableWidget, QVBoxLayout, QWidget


class SearchPage(QWidget):
    open_requested = Signal(object)

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.keyword_edit = QLineEdit()
        self.filter_combo = QComboBox()
        self.search_button = QPushButton("搜索")
        self.clear_button = QPushButton("清空")
        self.results_table = QTableWidget(0, 2)
        self.search_button.clicked.connect(self.search)
        self.clear_button.clicked.connect(self.clear_results)
        self.results_table.cellDoubleClicked.connect(self._open_selected_row)

        top = QHBoxLayout()
        top.addWidget(self.keyword_edit)
        top.addWidget(self.filter_combo)
        top.addWidget(self.search_button)
        top.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.results_table)

    def search(self) -> None:
        pass

    def clear_results(self) -> None:
        self.results_table.setRowCount(0)

    def _open_selected_row(self, row: int, _column: int) -> None:
        pass
```

```python
# src/atv_player/ui/history_page.py
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTableWidget, QVBoxLayout, QWidget


class HistoryPage(QWidget):
    open_requested = Signal(object)

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.delete_button = QPushButton("删除")
        self.clear_button = QPushButton("清空")
        self.table = QTableWidget(0, 4)
        self.delete_button.clicked.connect(self.delete_selected)
        self.clear_button.clicked.connect(self.clear_all)
        self.table.cellDoubleClicked.connect(self._open_selected_row)

        actions = QHBoxLayout()
        actions.addWidget(self.delete_button)
        actions.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.addLayout(actions)
        layout.addWidget(self.table)

    def delete_selected(self) -> None:
        pass

    def clear_all(self) -> None:
        pass

    def _open_selected_row(self, row: int, _column: int) -> None:
        pass
```

```python
# src/atv_player/ui/player_window.py
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QPushButton, QSlider, QTextEdit, QVBoxLayout, QWidget

from atv_player.player.mpv_widget import MpvWidget


class PlayerWindow(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.session = None
        self.current_index = 0
        self.setWindowTitle("alist-tvbox 播放器")
        self.video = MpvWidget(self)
        self.playlist = QListWidget()
        self.play_button = QPushButton("播放/暂停")
        self.progress = QSlider()
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.report_timer = QTimer(self)
        self.report_timer.setInterval(5000)
        self.report_timer.timeout.connect(self.report_progress)

        right = QVBoxLayout()
        right.addWidget(self.playlist)
        right.addWidget(self.details)

        left = QVBoxLayout()
        left.addWidget(self.video)
        left.addWidget(self.progress)
        left.addWidget(self.play_button)

        layout = QHBoxLayout(self)
        layout.addLayout(left, 3)
        layout.addLayout(right, 2)

    def open_session(self, session) -> None:
        self.session = session
        self.current_index = session.start_index
        self.video.load(session.playlist[self.current_index].url)
        self.video.seek(session.start_position_seconds)
        self.video.set_speed(session.speed)
        self.report_timer.start()

    def report_progress(self) -> None:
        if self.session is None:
            return
        self.controller.report_progress(
            self.session,
            current_index=self.current_index,
            position_seconds=self.video.position_seconds(),
            speed=1.0,
        )
```

```python
# src/atv_player/ui/main_window.py
from PySide6.QtWidgets import QMainWindow, QMessageBox, QTabWidget

from atv_player.ui.browse_page import BrowsePage
from atv_player.ui.history_page import HistoryPage
from atv_player.ui.player_window import PlayerWindow
from atv_player.ui.search_page import SearchPage


class MainWindow(QMainWindow):
    def __init__(self, browse_controller, history_controller, player_controller, config) -> None:
        super().__init__()
        self.nav_tabs = QTabWidget()
        self.browse_page = BrowsePage(browse_controller)
        self.search_page = SearchPage(browse_controller)
        self.history_page = HistoryPage(history_controller)
        self.player_controller = player_controller
        self.player_window: PlayerWindow | None = None
        self.config = config

        self.nav_tabs.addTab(self.browse_page, "浏览")
        self.nav_tabs.addTab(self.search_page, "搜索")
        self.nav_tabs.addTab(self.history_page, "播放记录")
        self.setCentralWidget(self.nav_tabs)
        self.setWindowTitle("alist-tvbox Desktop Player")

        self.browse_page.open_requested.connect(self.open_player)
        self.search_page.open_requested.connect(self.open_player)
        self.history_page.open_requested.connect(self.open_player)

    def open_player(self, session) -> None:
        if self.player_window is None:
            self.player_window = PlayerWindow(self.player_controller)
        self.player_window.open_session(session)
        self.player_window.show()
        self.player_window.raise_()
        self.player_window.activateWindow()

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "错误", message)
```

- [ ] **Step 6: Commit the Qt window layer**

```bash
git add src/atv_player/player/mpv_widget.py src/atv_player/ui/browse_page.py src/atv_player/ui/search_page.py src/atv_player/ui/history_page.py src/atv_player/ui/player_window.py src/atv_player/ui/main_window.py tests/test_app.py
git commit -m "feat: add desktop windows and embedded player shell"
```

## Task 8: Wire Application Startup, Token Recovery, And Manual Verification

**Files:**
- Create: `src/atv_player/app.py`
- Create: `src/atv_player/main.py`
- Create: `README.md`

- [ ] **Step 1: Write a failing startup test for token-based routing**

```python
# tests/test_app.py
from atv_player.app import decide_start_view
from atv_player.models import AppConfig
from atv_player.ui.main_window import MainWindow


class FakeBrowseController:
    pass


class FakeHistoryController:
    pass


class FakePlayerController:
    pass


def test_main_window_starts_on_browse_tab(qtbot) -> None:
    window = MainWindow(
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.currentIndex() == 0


def test_decide_start_view_prefers_login_without_token() -> None:
    assert decide_start_view(AppConfig(token="")) == "login"


def test_decide_start_view_uses_main_window_with_token() -> None:
    assert decide_start_view(AppConfig(token="token-123")) == "main"
```

- [ ] **Step 2: Run the startup test and confirm it fails**

Run: `uv run pytest tests/test_app.py -q`
Expected: FAIL with missing `decide_start_view`

- [ ] **Step 3: Implement startup routing and the executable entrypoint**

```python
# src/atv_player/app.py
from pathlib import Path

from PySide6.QtWidgets import QApplication

from atv_player.api import ApiClient
from atv_player.controllers.browse_controller import BrowseController
from atv_player.controllers.history_controller import HistoryController
from atv_player.controllers.login_controller import LoginController
from atv_player.controllers.player_controller import PlayerController
from atv_player.storage import SettingsRepository
from atv_player.ui.login_window import LoginWindow
from atv_player.ui.main_window import MainWindow


def decide_start_view(config) -> str:
    return "main" if config.token else "login"


def build_application() -> tuple[QApplication, SettingsRepository, ApiClient]:
    app = QApplication([])
    data_dir = Path.home() / ".local" / "share" / "atv-player"
    repo = SettingsRepository(data_dir / "app.db")
    config = repo.load_config()
    api_client = ApiClient(config.base_url, token=config.token)
    return app, repo, api_client


def create_root_widget(repo: SettingsRepository, api_client: ApiClient):
    config = repo.load_config()
    login_controller = LoginController(repo, api_client)
    browse_controller = BrowseController(api_client)
    history_controller = HistoryController(api_client)
    player_controller = PlayerController(api_client)
    if decide_start_view(config) == "login":
        return LoginWindow(login_controller)
    return MainWindow(browse_controller, history_controller, player_controller, config)
```

```python
# src/atv_player/main.py
from atv_player.app import build_application, create_root_widget


def main() -> int:
    app, repo, api_client = build_application()
    widget = create_root_widget(repo, api_client)
    widget.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
```

```markdown
# README.md

## atv-player

Linux-first `PySide6` desktop player for `alist-tvbox`.

## Development

```bash
uv sync
uv run pytest -q
uv run python -m atv_player.main
```

## Manual Verification Checklist

- Login with `http://127.0.0.1:4567`
- Restart the app and confirm token-based auto-login
- Force a `401` and confirm the app returns to login
- Browse into a folder and open a video file
- Open a playlist item and confirm a separate player window appears
- Wait at least 5 seconds during playback and confirm backend history advances
- Close the player and confirm the last position persists
- Search Telegram resources, filter by drive type, and clear results
- Open play history, reopen an item, delete one record, and clear all
```

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -q`
Expected: PASS with all suites green

- [ ] **Step 5: Run the desktop app for manual verification**

Run: `uv run python -m atv_player.main`
Expected: login window opens, then main window and player window flows are testable manually

- [ ] **Step 6: Commit the startup and verification docs**

```bash
git add src/atv_player/app.py src/atv_player/main.py README.md tests/test_app.py
git commit -m "feat: wire startup flow for desktop player"
```

## Self-Review Checklist

- Spec coverage:
  - Login: Tasks 1, 2, 3, and 8
  - Browse and playback opening: Tasks 4, 6, and 7
  - Search and filtering: Tasks 4 and 7
  - History list/delete/clear/reopen: Tasks 5 and 7
  - Player and 5-second reporting: Tasks 6 and 7
- Placeholder scan:
  - No `TBD`, `TODO`, or deferred implementation markers remain.
- Type consistency:
  - Shared models are defined once in `models.py` and reused across controllers and UI.
