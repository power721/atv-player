# Douban Card Width and Cursor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Douban movie cards wider, show a pointing-hand cursor on hover, and let the grid reduce columns on narrower windows instead of forcing at least five cards per row.

**Architecture:** Keep the change localized to `DoubanPage` and its UI tests. Reuse the existing card-building and relayout flow, but replace the hardcoded poster-size duplication with a shared card-poster constant and relax the minimum-column rule from five to one so the existing resize logic can handle narrower layouts naturally.

**Tech Stack:** Python 3.12, PySide6 widgets, pytest, pytest-qt

---

## File Structure

- `src/atv_player/ui/douban_page.py`
  - Widen the card and poster constants, set the card cursor to `PointingHandCursor`, and relax the grid's minimum column count to one.
- `tests/test_douban_page_ui.py`
  - Add focused UI coverage for the widened card dimensions and pointing cursor, and update the responsive-column test to assert narrower widths produce fewer columns than wider widths.

### Task 1: Widen Douban Cards And Make The Grid Responsive

**Files:**
- Modify: `src/atv_player/ui/douban_page.py:32-40`
- Modify: `src/atv_player/ui/douban_page.py:205-210`
- Modify: `src/atv_player/ui/douban_page.py:231-260`
- Modify: `tests/test_douban_page_ui.py:1-6`
- Modify: `tests/test_douban_page_ui.py:133-179`
- Test: `tests/test_douban_page_ui.py`

- [ ] **Step 1: Write the failing Douban card-layout tests**

Update the imports at the top of `tests/test_douban_page_ui.py` so the tests can assert cursor shape:

```python
import threading

from PySide6.QtCore import Qt

from atv_player.api import ApiError
from atv_player.models import DoubanCategory, VodItem
import atv_player.ui.douban_page as douban_page_module
from atv_player.ui.douban_page import DoubanPage
```

Add this new test after `test_douban_page_renders_loaded_poster_icon_on_card`:

```python
def test_douban_page_cards_use_wider_size_and_pointing_cursor(qtbot) -> None:
    page = DoubanPage(FakeDoubanController())
    qtbot.addWidget(page)
    page.show()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 1)
    button = page.card_buttons[0]

    assert button.width() == DoubanPage._CARD_WIDTH
    assert button.height() == DoubanPage._CARD_HEIGHT
    assert button.iconSize() == DoubanPage._CARD_POSTER_SIZE
    assert button.cursor().shape() == Qt.CursorShape.PointingHandCursor
```

Replace `test_douban_page_uses_five_then_six_columns_based_on_width` with this responsive version:

```python
def test_douban_page_reduces_columns_when_width_is_tighter(qtbot) -> None:
    controller = FakeDoubanController()
    controller.items_by_category["suggestion"] = (
        [
            VodItem(vod_id=str(index), vod_name=f"Movie {index}", vod_pic="", vod_remarks="9.0")
            for index in range(6)
        ],
        30,
    )
    page = DoubanPage(controller)
    qtbot.addWidget(page)
    page.resize(1300, 900)
    page.show()

    qtbot.waitUntil(lambda: len(page.card_buttons) == 6)
    narrow_columns = page._current_card_columns

    assert narrow_columns < 6
    assert page.cards_layout.getItemPosition(5)[:2] == (1, 1)

    page.resize(2200, 900)
    qtbot.waitUntil(lambda: page._current_card_columns > narrow_columns)

    assert page._current_card_columns == 6
    assert page.cards_layout.getItemPosition(5)[:2] == (0, 5)
```

- [ ] **Step 2: Run the focused Douban card tests to verify they fail**

Run:

```bash
uv run pytest tests/test_douban_page_ui.py::test_douban_page_cards_use_wider_size_and_pointing_cursor tests/test_douban_page_ui.py::test_douban_page_reduces_columns_when_width_is_tighter -q
```

Expected: FAIL because `DoubanPage` does not define `_CARD_POSTER_SIZE`, cards still use the old dimensions and arrow cursor, and the grid still enforces a minimum of five columns.

- [ ] **Step 3: Write the minimal Douban page implementation**

Update the class constants in `src/atv_player/ui/douban_page.py`:

```python
class DoubanPage(QWidget):
    search_requested = Signal(str)
    unauthorized = Signal()
    _CARD_WIDTH = 220
    _CARD_HEIGHT = 360
    _CARD_POSTER_SIZE = QSize(190, 285)
    _CARD_SPACING = 16
    _MIN_CARD_COLUMNS = 1
    _MAX_CARD_COLUMNS = 6
```

Keep the relayout formula but let the lower bound come from the new `1`-column minimum:

```python
    def _column_count_for_width(self, available_width: int) -> int:
        if available_width <= 0:
            return self._MIN_CARD_COLUMNS
        fit_columns = (available_width + self._CARD_SPACING) // (self._CARD_WIDTH + self._CARD_SPACING)
        fit_columns = max(self._MIN_CARD_COLUMNS, fit_columns)
        return min(fit_columns, self._MAX_CARD_COLUMNS)
```

Update the card button construction and poster loading methods to use the shared poster-size constant and pointing cursor:

```python
    def _build_card_button(self, item) -> QToolButton:
        text = item.vod_name if not item.vod_remarks else f"{item.vod_name}\n{item.vod_remarks}"
        button = QToolButton()
        button.setText(text)
        button.setFixedSize(self._CARD_WIDTH, self._CARD_HEIGHT)
        button.setToolTip(item.vod_name)
        button.setIconSize(self._CARD_POSTER_SIZE)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet("padding: 10px;")
        button.clicked.connect(lambda _checked=False, keyword=item.vod_name: self.search_requested.emit(keyword))
        return button

    def _start_card_poster_load(self, button: QToolButton, item) -> None:
        image_url = normalize_poster_url(item.vod_pic)
        if not image_url:
            return

        def load() -> None:
            image = load_remote_poster_image(image_url, self._CARD_POSTER_SIZE)
            if image is not None:
                self._signals.poster_loaded.emit(button, image)

        threading.Thread(target=load, daemon=True).start()

    def _handle_poster_loaded(self, button: QToolButton, image) -> None:
        if button not in self.card_buttons:
            return
        pixmap = QPixmap.fromImage(image)
        button.setIcon(QIcon(pixmap))
        button.setIconSize(self._CARD_POSTER_SIZE)
```

- [ ] **Step 4: Run the focused Douban card tests to verify they pass**

Run:

```bash
uv run pytest tests/test_douban_page_ui.py::test_douban_page_cards_use_wider_size_and_pointing_cursor tests/test_douban_page_ui.py::test_douban_page_reduces_columns_when_width_is_tighter -q
```

Expected: PASS with wider cards, a larger poster icon size, a pointing-hand cursor on each card, and a grid that wraps to fewer columns on narrower widths.

- [ ] **Step 5: Run the broader Douban page regression slice**

Run:

```bash
uv run pytest tests/test_douban_page_ui.py::test_douban_page_clicking_card_emits_search_requested tests/test_douban_page_ui.py::test_douban_page_renders_loaded_poster_icon_on_card tests/test_douban_page_ui.py::test_douban_page_cards_use_wider_size_and_pointing_cursor tests/test_douban_page_ui.py::test_douban_page_reduces_columns_when_width_is_tighter tests/test_douban_page_ui.py::test_douban_page_centers_content_container -q
```

Expected: PASS with click-to-search, poster loading, widened-card behavior, responsive columns, and centered content all intact.

- [ ] **Step 6: Commit**

```bash
git add tests/test_douban_page_ui.py src/atv_player/ui/douban_page.py
git commit -m "feat: widen douban movie cards"
```
