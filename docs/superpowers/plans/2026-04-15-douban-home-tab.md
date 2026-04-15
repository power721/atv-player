# Douban Home Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated `豆瓣电影` first tab that loads Douban categories and poster cards, then hands movie clicks off to `文件浏览` by auto-running the existing search flow.

**Architecture:** Keep Douban browsing isolated from file browsing by adding a dedicated `DoubanController` and `DoubanPage`. Reuse the existing browse search implementation by adding a public `BrowsePage.search_keyword()` handoff instead of duplicating search state or search-result rendering.

**Tech Stack:** Python 3.13, PySide6, httpx, pytest-qt

---

## File Map

- Modify: `src/atv_player/models.py`
  - Add `DoubanCategory` dataclass.
- Modify: `src/atv_player/api.py`
  - Add `/tg-db/{token}` client methods for categories and paged items.
- Create: `src/atv_player/controllers/douban_controller.py`
  - Map Douban API payloads into `DoubanCategory` and `VodItem`.
- Create: `tests/test_douban_controller.py`
  - Cover category mapping, item mapping, and fixed page-size calls.
- Create: `src/atv_player/ui/poster_loader.py`
  - Share poster URL normalization and HTTP header logic between player and Douban tab.
- Create: `tests/test_poster_loader.py`
  - Cover Douban URL normalization, referer selection, and image scaling.
- Create: `src/atv_player/ui/douban_page.py`
  - Implement category list, poster grid, async loading, pagination, and click-to-search signal.
- Create: `tests/test_douban_page_ui.py`
  - Cover layout, initial load, pagination, click handoff, and stale async response handling.
- Modify: `src/atv_player/ui/browse_page.py`
  - Add a public `search_keyword()` handoff entry that reuses existing async search logic.
- Modify: `src/atv_player/ui/main_window.py`
  - Add the new first tab and connect `DoubanPage.search_requested` to browse search handoff.
- Modify: `src/atv_player/app.py`
  - Instantiate `DoubanController` and pass it into `MainWindow`.
- Modify: `tests/test_browse_page_ui.py`
  - Add `search_keyword()` coverage.
- Modify: `tests/test_app.py`
  - Update tab expectations and cover the Douban-to-browse handoff.
- Modify: `src/atv_player/ui/player_window.py`
  - Replace duplicated poster header logic with `poster_loader` helper calls.

### Task 1: Douban API and Controller Plumbing

**Files:**
- Modify: `src/atv_player/models.py`
- Modify: `src/atv_player/api.py`
- Create: `src/atv_player/controllers/douban_controller.py`
- Test: `tests/test_douban_controller.py`

- [ ] **Step 1: Write the failing controller tests**

```python
from atv_player.controllers.douban_controller import DoubanController
from atv_player.models import DoubanCategory


class FakeApiClient:
    def __init__(self) -> None:
        self.category_payload = {"class": []}
        self.items_payload = {"list": [], "total": 0}
        self.item_calls: list[tuple[str, int, int]] = []

    def list_douban_categories(self) -> dict:
        return self.category_payload

    def list_douban_items(self, category_id: str, page: int, size: int = 35) -> dict:
        self.item_calls.append((category_id, page, size))
        return self.items_payload


def test_load_categories_maps_backend_class_payload() -> None:
    api = FakeApiClient()
    api.category_payload = {
        "class": [
            {"type_id": "suggestion", "type_name": "推荐"},
            {"type_id": "movie", "type_name": "电影"},
        ]
    }
    controller = DoubanController(api)

    categories = controller.load_categories()

    assert categories == [
        DoubanCategory(type_id="suggestion", type_name="推荐"),
        DoubanCategory(type_id="movie", type_name="电影"),
    ]


def test_load_items_maps_vod_fields_and_total() -> None:
    api = FakeApiClient()
    api.items_payload = {
        "list": [
            {
                "vod_id": "d1",
                "vod_name": "霸王别姬",
                "vod_pic": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p1.jpg",
                "vod_remarks": "9.6",
                "dbid": 1291546,
            }
        ],
        "total": 70,
    }
    controller = DoubanController(api)

    items, total = controller.load_items("movie", page=2)

    assert total == 70
    assert items[0].vod_id == "d1"
    assert items[0].vod_name == "霸王别姬"
    assert items[0].vod_pic.endswith("p1.jpg")
    assert items[0].vod_remarks == "9.6"
    assert items[0].dbid == 1291546


def test_load_items_uses_fixed_desktop_page_size() -> None:
    api = FakeApiClient()
    controller = DoubanController(api)

    controller.load_items("movie", page=3)

    assert api.item_calls == [("movie", 3, 35)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_douban_controller.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'atv_player.controllers.douban_controller'` and missing `DoubanCategory`.

- [ ] **Step 3: Write the minimal implementation**

Add the new model in `src/atv_player/models.py`:

```python
@dataclass(slots=True)
class DoubanCategory:
    type_id: str
    type_name: str
```

Add the API methods in `src/atv_player/api.py`:

```python
    def list_douban_categories(self) -> dict[str, Any]:
        return self._request("GET", f"/tg-db/{self._vod_token}")

    def list_douban_items(self, category_id: str, page: int, size: int = 35) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/tg-db/{self._vod_token}",
            params={"ac": "web", "t": category_id, "pg": page, "size": size},
        )
```

Create `src/atv_player/controllers/douban_controller.py`:

```python
from __future__ import annotations

from atv_player.models import DoubanCategory, VodItem


def _map_category(payload: dict) -> DoubanCategory:
    return DoubanCategory(
        type_id=str(payload.get("type_id") or ""),
        type_name=str(payload.get("type_name") or ""),
    )


def _map_item(payload: dict) -> VodItem:
    return VodItem(
        vod_id=str(payload.get("vod_id") or ""),
        vod_name=str(payload.get("vod_name") or ""),
        vod_pic=str(payload.get("vod_pic") or ""),
        vod_remarks=str(payload.get("vod_remarks") or ""),
        dbid=int(payload.get("dbid") or 0),
        type_name=str(payload.get("type_name") or ""),
        vod_content=str(payload.get("vod_content") or ""),
    )


class DoubanController:
    _PAGE_SIZE = 35

    def __init__(self, api_client) -> None:
        self._api_client = api_client

    def load_categories(self) -> list[DoubanCategory]:
        payload = self._api_client.list_douban_categories()
        return [_map_category(item) for item in payload.get("class", [])]

    def load_items(self, category_id: str, page: int) -> tuple[list[VodItem], int]:
        payload = self._api_client.list_douban_items(category_id, page=page, size=self._PAGE_SIZE)
        items = [_map_item(item) for item in payload.get("list", [])]
        total = int(payload.get("total") or 0)
        if total <= 0:
            total = int(payload.get("pagecount") or 0) * self._PAGE_SIZE
        return items, total
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_douban_controller.py -v`

Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add tests/test_douban_controller.py src/atv_player/models.py src/atv_player/api.py src/atv_player/controllers/douban_controller.py
git commit -m "feat: add douban data controller"
```

### Task 2: Shared Poster Loading Helper

**Files:**
- Create: `src/atv_player/ui/poster_loader.py`
- Modify: `src/atv_player/ui/player_window.py`
- Test: `tests/test_poster_loader.py`

- [ ] **Step 1: Write the failing poster helper tests**

```python
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage

from atv_player.ui.poster_loader import (
    build_poster_request_headers,
    load_remote_poster_image,
    normalize_poster_url,
)


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


def test_normalize_poster_url_upgrades_douban_ratio_path() -> None:
    result = normalize_poster_url("https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg")
    assert result == "https://img3.doubanio.com/view/photo/m/public/p123.jpg"


def test_build_poster_request_headers_uses_site_specific_referers() -> None:
    assert build_poster_request_headers("https://img3.doubanio.com/view/photo/m/public/p123.jpg")["Referer"] == "https://movie.douban.com/"
    assert build_poster_request_headers("https://i.ytimg.com/vi/123/maxresdefault.jpg")["Referer"] == "https://www.youtube.com/"
    assert build_poster_request_headers("https://cc.163.com/cover.png")["Referer"] == "https://cc.163.com/"


def test_load_remote_poster_image_scales_downloaded_image() -> None:
    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        png = QImage(20, 40, QImage.Format.Format_RGB32)
        png.fill(0x00FF00)
        from PySide6.QtCore import QBuffer, QByteArray, QIODeviceBase

        data = QByteArray()
        qbuffer = QBuffer(data)
        qbuffer.open(QIODeviceBase.OpenModeFlag.WriteOnly)
        png.save(qbuffer, "PNG")
        return FakeResponse(bytes(data))

    loaded = load_remote_poster_image(
        "https://img3.doubanio.com/view/photo/m/public/p123.jpg",
        QSize(90, 130),
        get=fake_get,
    )

    assert loaded is not None
    assert loaded.isNull() is False
    assert loaded.width() <= 90
    assert loaded.height() <= 130
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_poster_loader.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'atv_player.ui.poster_loader'`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/atv_player/ui/poster_loader.py`:

```python
from __future__ import annotations

import httpx
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage

POSTER_REQUEST_TIMEOUT_SECONDS = 10.0
POSTER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
DEFAULT_POSTER_REFERER = "https://movie.douban.com/"


def normalize_poster_url(source: str) -> str:
    normalized = source or ""
    if "doubanio.com" in normalized:
        normalized = normalized.replace("s_ratio_poster", "m")
    return normalized


def build_poster_request_headers(image_url: str) -> dict[str, str]:
    referer = DEFAULT_POSTER_REFERER
    if "ytimg.com" in image_url:
        referer = "https://www.youtube.com/"
    elif "netease.com" in image_url or "163.com" in image_url:
        referer = "https://cc.163.com/"
    return {
        "Referer": referer,
        "User-Agent": POSTER_USER_AGENT,
    }


def load_remote_poster_image(
    image_url: str,
    target_size: QSize,
    timeout: float = POSTER_REQUEST_TIMEOUT_SECONDS,
    get=httpx.get,
) -> QImage | None:
    try:
        response = get(
            image_url,
            headers=build_poster_request_headers(image_url),
            timeout=timeout,
        )
        response.raise_for_status()
    except Exception:
        return None
    image = QImage()
    image.loadFromData(response.content)
    if image.isNull():
        return None
    return image.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
```

Update `src/atv_player/ui/player_window.py` to import and use the helper:

```python
from atv_player.ui.poster_loader import load_remote_poster_image, normalize_poster_url
```

Replace the duplicated methods with:

```python
    def _start_poster_load(self, source: str, request_id: int) -> None:
        image_url = normalize_poster_url(source)
        if not image_url:
            return

        def load() -> None:
            image = load_remote_poster_image(image_url, self._POSTER_SIZE)
            self._poster_load_signals.loaded.emit(request_id, image)

        threading.Thread(target=load, daemon=True).start()
```

Delete `_normalize_poster_url()`, `_poster_request_headers()`, and `_load_remote_poster_image()` once the helper is in use.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_poster_loader.py tests/test_player_window_ui.py -k poster -v`

Expected: PASS for the new helper tests and existing poster-related player window tests.

- [ ] **Step 5: Commit**

```bash
git add tests/test_poster_loader.py src/atv_player/ui/poster_loader.py src/atv_player/ui/player_window.py
git commit -m "refactor: share poster loading helpers"
```

### Task 3: DoubanPage UI, Async Loading, and Pagination

**Files:**
- Create: `src/atv_player/ui/douban_page.py`
- Test: `tests/test_douban_page_ui.py`

- [ ] **Step 1: Write the failing page tests**

```python
import threading

from atv_player.api import ApiError
from atv_player.models import DoubanCategory, VodItem
from atv_player.ui.douban_page import DoubanPage


class FakeDoubanController:
    def __init__(self) -> None:
        self.category_calls = 0
        self.item_calls: list[tuple[str, int]] = []
        self.categories = [
            DoubanCategory(type_id="suggestion", type_name="推荐"),
            DoubanCategory(type_id="movie", type_name="电影"),
        ]
        self.items_by_category = {
            "suggestion": (
                [VodItem(vod_id="m1", vod_name="霸王别姬", vod_pic="poster-1", vod_remarks="9.6")],
                70,
            ),
            "movie": (
                [VodItem(vod_id="m2", vod_name="活着", vod_pic="poster-2", vod_remarks="9.3")],
                35,
            ),
        }

    def load_categories(self):
        self.category_calls += 1
        return self.categories

    def load_items(self, category_id: str, page: int):
        self.item_calls.append((category_id, page))
        return self.items_by_category[category_id]


class AsyncDoubanController(FakeDoubanController):
    def __init__(self) -> None:
        super().__init__()
        self._events = {
            ("suggestion", 1): threading.Event(),
            ("movie", 1): threading.Event(),
        }

    def load_items(self, category_id: str, page: int):
        self.item_calls.append((category_id, page))
        self._events[(category_id, page)].wait(timeout=5)
        return self.items_by_category[category_id]

    def release(self, category_id: str, page: int) -> None:
        self._events[(category_id, page)].set()


class FailingDoubanController(FakeDoubanController):
    def load_items(self, category_id: str, page: int):
        if category_id == "movie":
            raise ApiError("获取列表失败")
        return super().load_items(category_id, page)


def test_douban_page_loads_categories_and_first_page(qtbot) -> None:
    page = DoubanPage(FakeDoubanController())
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    assert page.category_list.currentItem().text() == "推荐"
    assert page.page_label.text() == "第 1 / 2 页"
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"


def test_douban_page_clicking_card_emits_search_requested(qtbot) -> None:
    page = DoubanPage(FakeDoubanController())
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)

    with qtbot.waitSignal(page.search_requested, timeout=1000) as signal:
        page.card_buttons[0].click()

    assert signal.args == ["霸王别姬"]


def test_douban_page_category_change_resets_to_first_page(qtbot) -> None:
    controller = FakeDoubanController()
    page = DoubanPage(controller)
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    page.current_page = 3
    page.category_list.setCurrentRow(1)

    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1))
    assert page.current_page == 1


def test_douban_page_ignores_stale_item_response(qtbot) -> None:
    controller = AsyncDoubanController()
    page = DoubanPage(controller)
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: page.category_list.count() == 2)
    controller.release("movie", 1)
    page.category_list.setCurrentRow(1)
    qtbot.waitUntil(lambda: controller.item_calls[-1] == ("movie", 1))
    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    assert page.card_buttons[0].text() == "活着\n9.3"

    controller.release("suggestion", 1)
    qtbot.wait(50)
    assert page.card_buttons[0].text() == "活着\n9.3"


def test_douban_page_keeps_previous_cards_when_new_load_fails(qtbot) -> None:
    page = DoubanPage(FailingDoubanController())
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"

    page.category_list.setCurrentRow(1)

    qtbot.waitUntil(lambda: page.status_label.text() == "获取列表失败")
    assert page.card_buttons[0].text() == "霸王别姬\n9.6"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_douban_page_ui.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'atv_player.ui.douban_page'`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/atv_player/ui/douban_page.py`:

```python
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from atv_player.api import ApiError, UnauthorizedError


class _DoubanSignals(QObject):
    categories_loaded = Signal(int, object)
    items_loaded = Signal(int, object, int)
    failed = Signal(str, int)
    unauthorized = Signal(int)


class DoubanPage(QWidget):
    search_requested = Signal(str)
    unauthorized = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.category_list = QListWidget()
        self.status_label = QLabel("")
        self.prev_page_button = QPushButton("上一页")
        self.next_page_button = QPushButton("下一页")
        self.page_label = QLabel("第 1 / 1 页")
        self.cards_widget = QWidget()
        self.cards_layout = QGridLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(16)
        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setWidget(self.cards_widget)
        self.card_buttons: list[QPushButton] = []
        self.categories = []
        self.items = []
        self.selected_category_id = ""
        self.current_page = 1
        self.page_size = 35
        self.total_items = 0
        self._categories_request_id = 0
        self._items_request_id = 0
        self._signals = _DoubanSignals()
        self._signals.categories_loaded.connect(self._handle_categories_loaded)
        self._signals.items_loaded.connect(self._handle_items_loaded)
        self._signals.failed.connect(self._handle_failed)
        self._signals.unauthorized.connect(self._handle_unauthorized)

        right = QVBoxLayout()
        right.addWidget(self.status_label)
        right.addWidget(self.cards_scroll, 1)
        paging = QHBoxLayout()
        paging.addStretch(1)
        paging.addWidget(self.prev_page_button)
        paging.addWidget(self.page_label)
        paging.addWidget(self.next_page_button)
        right.addLayout(paging)

        layout = QHBoxLayout(self)
        layout.addWidget(self.category_list, 1)
        layout.addLayout(right, 4)

        self.category_list.currentRowChanged.connect(self._handle_category_row_changed)
        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)

        self.reload_categories()

    def reload_categories(self) -> None:
        self._categories_request_id += 1
        request_id = self._categories_request_id
        self.status_label.setText("加载分类中...")

        def run() -> None:
            try:
                categories = self.controller.load_categories()
            except UnauthorizedError:
                self._signals.unauthorized.emit(request_id)
                return
            except ApiError as exc:
                self._signals.failed.emit(str(exc), request_id)
                return
            self._signals.categories_loaded.emit(request_id, categories)

        threading.Thread(target=run, daemon=True).start()

    def load_items(self, category_id: str, page: int) -> None:
        self._items_request_id += 1
        request_id = self._items_request_id
        self.status_label.setText("加载电影中...")

        def run() -> None:
            try:
                items, total = self.controller.load_items(category_id, page)
            except UnauthorizedError:
                self._signals.unauthorized.emit(request_id)
                return
            except ApiError as exc:
                self._signals.failed.emit(str(exc), request_id)
                return
            self._signals.items_loaded.emit(request_id, items, total)

        threading.Thread(target=run, daemon=True).start()

    def _handle_categories_loaded(self, request_id: int, categories) -> None:
        if request_id != self._categories_request_id:
            return
        self.categories = list(categories)
        self.category_list.clear()
        for category in self.categories:
            self.category_list.addItem(category.type_name)
        if not self.categories:
            self.status_label.setText("暂无豆瓣分类")
            return
        self.category_list.setCurrentRow(0)

    def _handle_category_row_changed(self, row: int) -> None:
        if not (0 <= row < len(self.categories)):
            return
        self.selected_category_id = self.categories[row].type_id
        self.current_page = 1
        self.load_items(self.selected_category_id, self.current_page)

    def _handle_items_loaded(self, request_id: int, items, total: int) -> None:
        if request_id != self._items_request_id:
            return
        self.items = list(items)
        self.total_items = total
        self.status_label.setText("" if self.items else "当前分类暂无内容")
        self._render_cards()
        self._update_pagination()

    def _handle_failed(self, message: str, _request_id: int) -> None:
        self.status_label.setText(message)

    def _handle_unauthorized(self, _request_id: int) -> None:
        self.unauthorized.emit()

    def _render_cards(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.card_buttons = []
        for index, item in enumerate(self.items):
            button = QPushButton(f"{item.vod_name}\\n{item.vod_remarks}".strip())
            button.clicked.connect(lambda _checked=False, keyword=item.vod_name: self.search_requested.emit(keyword))
            self.card_buttons.append(button)
            self.cards_layout.addWidget(button, index // 4, index % 4)

    def _update_pagination(self) -> None:
        total_pages = max(1, (self.total_items + self.page_size - 1) // self.page_size)
        self.page_label.setText(f"第 {self.current_page} / {total_pages} 页")
        self.prev_page_button.setEnabled(self.current_page > 1)
        self.next_page_button.setEnabled(self.current_page < total_pages)

    def previous_page(self) -> None:
        if self.current_page <= 1 or not self.selected_category_id:
            return
        self.current_page -= 1
        self.load_items(self.selected_category_id, self.current_page)

    def next_page(self) -> None:
        total_pages = max(1, (self.total_items + self.page_size - 1) // self.page_size)
        if self.current_page >= total_pages or not self.selected_category_id:
            return
        self.current_page += 1
        self.load_items(self.selected_category_id, self.current_page)
```

Keep this first pass intentionally plain. Poster thumbnails get added in the next task if this widget needs visual polish beyond the minimal clickable card text.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_douban_page_ui.py -v`

Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add tests/test_douban_page_ui.py src/atv_player/ui/douban_page.py
git commit -m "feat: add douban browse page"
```

### Task 4: Poster Cards, Browse Handoff, and Main Window Integration

**Files:**
- Modify: `src/atv_player/ui/douban_page.py`
- Modify: `src/atv_player/ui/browse_page.py`
- Modify: `src/atv_player/ui/main_window.py`
- Modify: `src/atv_player/app.py`
- Modify: `tests/test_browse_page_ui.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write the failing integration tests**

Add the public browse handoff test to `tests/test_browse_page_ui.py`:

```python
def test_browse_page_search_keyword_sets_input_and_starts_search(qtbot) -> None:
    controller = AsyncSearchController()
    page = BrowsePage(controller)
    qtbot.addWidget(page)
    page.show()

    page.search_keyword("霸王别姬")

    qtbot.waitUntil(lambda: controller.calls == ["霸王别姬"])
    assert page.keyword_edit.text() == "霸王别姬"
```

Update `tests/test_app.py` with a Douban controller stub and the new tab expectations:

```python
class FakeDoubanController:
    def load_categories(self):
        return []

    def load_items(self, category_id: str, page: int):
        return [], 0
```

```python
def test_main_window_starts_on_douban_tab(qtbot) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )

    qtbot.addWidget(window)
    window.show()

    assert window.nav_tabs.currentIndex() == 0
    assert window.nav_tabs.count() == 3
    assert window.nav_tabs.tabText(0) == "豆瓣电影"
    assert window.nav_tabs.tabText(1) == "文件浏览"
    assert window.nav_tabs.tabText(2) == "播放记录"
```

```python
def test_main_window_switches_to_browse_and_searches_from_douban_signal(qtbot, monkeypatch) -> None:
    window = MainWindow(
        douban_controller=FakeDoubanController(),
        browse_controller=FakeBrowseController(),
        history_controller=FakeHistoryController(),
        player_controller=FakePlayerController(),
        config=AppConfig(),
    )
    qtbot.addWidget(window)
    window.show()

    searched = []
    monkeypatch.setattr(window.browse_page, "search_keyword", lambda keyword: searched.append(keyword))

    window.douban_page.search_requested.emit("霸王别姬")

    assert window.nav_tabs.currentWidget() is window.browse_page
    assert searched == ["霸王别姬"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_browse_page_ui.py tests/test_app.py -k "search_keyword or douban_tab or douban_signal" -v`

Expected: FAIL because `BrowsePage.search_keyword()` does not exist and `MainWindow` does not accept `douban_controller`.

- [ ] **Step 3: Write the minimal implementation**

Add the public handoff in `src/atv_player/ui/browse_page.py`:

```python
    def search_keyword(self, keyword: str) -> None:
        self.keyword_edit.setText(keyword)
        self.search()
```

Update `src/atv_player/ui/main_window.py`:

```python
from atv_player.ui.douban_page import DoubanPage
```

```python
    def __init__(
        self,
        douban_controller,
        browse_controller,
        history_controller,
        player_controller,
        config,
        save_config=None,
    ) -> None:
        super().__init__()
        self._save_config = save_config or (lambda: None)
        self.nav_tabs = QTabWidget()
        self.logout_button = QPushButton("退出登录")
        self.douban_page = DoubanPage(douban_controller)
        self.browse_page = BrowsePage(browse_controller, config=config, save_config=self._save_config)
        self.history_page = HistoryPage(history_controller)
```

```python
        self.nav_tabs.addTab(self.douban_page, "豆瓣电影")
        self.nav_tabs.addTab(self.browse_page, "文件浏览")
        self.nav_tabs.addTab(self.history_page, "播放记录")
```

```python
        self.douban_page.search_requested.connect(self._handle_douban_search_requested)
        self.douban_page.unauthorized.connect(self.logout_requested.emit)
```

```python
    def _handle_douban_search_requested(self, keyword: str) -> None:
        self.nav_tabs.setCurrentWidget(self.browse_page)
        self.browse_page.search_keyword(keyword)
```

Update `src/atv_player/app.py`:

```python
from atv_player.controllers.douban_controller import DoubanController
```

```python
        douban_controller = DoubanController(self._api_client)
        browse_controller = BrowseController(self._api_client)
        history_controller = HistoryController(self._api_client)
        player_controller = PlayerController(self._api_client)
        self.main_window = MainWindow(
            douban_controller=douban_controller,
            browse_controller=browse_controller,
            history_controller=history_controller,
            player_controller=player_controller,
            config=config,
            save_config=lambda: self.repo.save_config(config),
        )
```

Upgrade `src/atv_player/ui/douban_page.py` cards from text-only buttons to poster cards using `poster_loader`:

```python
from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QPixmap

from atv_player.ui.poster_loader import load_remote_poster_image, normalize_poster_url
```

```python
class _DoubanSignals(QObject):
    categories_loaded = Signal(int, object)
    items_loaded = Signal(int, object, int)
    failed = Signal(str, int)
    unauthorized = Signal(int)
    poster_loaded = Signal(object, object)
```

```python
        self._signals.poster_loaded.connect(self._handle_poster_loaded)
```

```python
    def _render_cards(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.card_buttons = []
        for index, item in enumerate(self.items):
            button = self._build_card_button(item)
            self.card_buttons.append(button)
            self.cards_layout.addWidget(button, index // 4, index % 4)
```

```python
    def _build_card_button(self, item) -> QPushButton:
        button = QPushButton()
        button.setFixedSize(180, 320)
        button.clicked.connect(lambda _checked=False, keyword=item.vod_name: self.search_requested.emit(keyword))
        button.setText(f"{item.vod_name}\\n{item.vod_remarks}".strip())
        button.setToolTip(item.vod_name)
        image_url = normalize_poster_url(item.vod_pic)
        if image_url:
            def load() -> None:
                image = load_remote_poster_image(image_url, QSize(160, 240))
                if image is not None:
                    self._signals.poster_loaded.emit(button, image)
            threading.Thread(target=load, daemon=True).start()
        return button

    def _handle_poster_loaded(self, button: QPushButton, image) -> None:
        if button not in self.card_buttons:
            return
        pixmap = QPixmap.fromImage(image)
        button.setIcon(QIcon(pixmap))
        button.setIconSize(QSize(160, 240))
```

Use `_build_card_button()` from `_render_cards()` so the rendered grid becomes a real poster wall instead of plain text buttons.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_browse_page_ui.py tests/test_app.py tests/test_douban_page_ui.py -k "search_keyword or douban" -v`

Expected: PASS for the new browse handoff and main window integration tests.

- [ ] **Step 5: Commit**

```bash
git add tests/test_browse_page_ui.py tests/test_app.py src/atv_player/ui/browse_page.py src/atv_player/ui/main_window.py src/atv_player/app.py src/atv_player/ui/douban_page.py
git commit -m "feat: wire douban tab into main window"
```

### Task 5: Full Verification and Cleanup

**Files:**
- Test: `tests/test_douban_controller.py`
- Test: `tests/test_poster_loader.py`
- Test: `tests/test_douban_page_ui.py`
- Test: `tests/test_browse_page_ui.py`
- Test: `tests/test_app.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Run the focused feature test suite**

Run:

```bash
uv run pytest \
  tests/test_douban_controller.py \
  tests/test_poster_loader.py \
  tests/test_douban_page_ui.py \
  tests/test_browse_page_ui.py \
  tests/test_app.py \
  tests/test_player_window_ui.py -v
```

Expected: PASS with no Douban-tab regressions and no player poster regressions.

- [ ] **Step 2: Run the broader smoke suite**

Run:

```bash
uv run pytest \
  tests/test_browse_controller.py \
  tests/test_browse_page_ui.py \
  tests/test_player_window_ui.py \
  tests/test_app.py -v
```

Expected: PASS for the existing browse/player/app workflows after tab insertion.

- [ ] **Step 3: Manual smoke check**

Run:

```bash
uv run python -m atv_player
```

Expected manual checks:

- app opens on the new `豆瓣电影` tab
- categories load on the left
- clicking a poster switches to `文件浏览`
- the browse search box is populated with the movie name
- search results appear without freezing the UI
- unauthorized responses still return to login

- [ ] **Step 4: Commit the final integrated feature**

```bash
git add src/atv_player/app.py src/atv_player/models.py src/atv_player/api.py src/atv_player/controllers/douban_controller.py src/atv_player/ui/poster_loader.py src/atv_player/ui/douban_page.py src/atv_player/ui/browse_page.py src/atv_player/ui/main_window.py src/atv_player/ui/player_window.py tests/test_douban_controller.py tests/test_poster_loader.py tests/test_douban_page_ui.py tests/test_browse_page_ui.py tests/test_app.py
git commit -m "feat: add douban home tab"
```
