# Login Window Centering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the login window open larger by default and keep the login form centered horizontally and vertically without changing login behavior.

**Architecture:** Add a bounded `content_container` inside `LoginWindow`, move the existing form and button into it, and center that container with stretch-based outer layouts. Cover the change with focused Qt UI tests that assert the explicit default size, centered geometry, and unchanged login submission flow.

**Tech Stack:** Python, PySide6, pytest, pytest-qt

---

## File Structure

- Create: `tests/test_login_window_ui.py`
  - Focused UI coverage for login window sizing, centering, and submit wiring.
- Modify: `src/atv_player/ui/login_window.py`
  - Add a centered content container, explicit initial size, and keep existing signal flow intact.

### Task 1: Add failing login window UI coverage

**Files:**
- Create: `tests/test_login_window_ui.py`
- Test: `tests/test_login_window_ui.py`

- [ ] **Step 1: Write the failing tests**

```python
from PySide6.QtCore import QSize

from atv_player.models import AppConfig
from atv_player.ui.login_window import LoginWindow


class FakeLoginController:
    def __init__(self) -> None:
        self.login_calls: list[tuple[str, str, str]] = []

    def load_defaults(self) -> AppConfig:
        return AppConfig(base_url="http://demo", username="alice")

    def login(self, base_url: str, username: str, password: str) -> AppConfig:
        self.login_calls.append((base_url, username, password))
        return AppConfig(base_url=base_url, username=username, token="token-123")


def test_login_window_uses_larger_default_size(qtbot) -> None:
    window = LoginWindow(FakeLoginController())
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(50)

    assert window.size() == QSize(720, 520)


def test_login_window_centers_content_container_both_axes(qtbot) -> None:
    window = LoginWindow(FakeLoginController())
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    qtbot.wait(50)

    container_center = window.content_container.geometry().center()
    window_center = window.rect().center()

    assert abs(container_center.x() - window_center.x()) <= 5
    assert abs(container_center.y() - window_center.y()) <= 5


def test_login_window_click_login_uses_existing_submission_flow(qtbot) -> None:
    controller = FakeLoginController()
    window = LoginWindow(controller)
    qtbot.addWidget(window)
    window.show()

    window.base_url_edit.setText("http://server")
    window.username_edit.setText("bob")
    window.password_edit.setText("secret")

    with qtbot.waitSignal(window.login_succeeded):
        window.login_button.click()

    assert controller.login_calls == [("http://server", "bob", "secret")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_login_window_ui.py -q`
Expected: FAIL because `LoginWindow` does not yet expose `content_container` and does not set an explicit `720x520` default size.

### Task 2: Implement centered login layout and explicit window sizing

**Files:**
- Modify: `src/atv_player/ui/login_window.py`
- Test: `tests/test_login_window_ui.py`

- [ ] **Step 1: Write minimal implementation**

```python
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

...

self.resize(720, 520)

form = QFormLayout()
form.addRow("后端地址", self.base_url_edit)
form.addRow("用户名", self.username_edit)
form.addRow("密码", self.password_edit)

self.content_container = QWidget()
self.content_container.setMaximumWidth(520)
self.content_container.setSizePolicy(
    QSizePolicy.Policy.Expanding,
    QSizePolicy.Policy.Preferred,
)

content_layout = QVBoxLayout(self.content_container)
content_layout.setContentsMargins(0, 0, 0, 0)
content_layout.addLayout(form)
content_layout.addWidget(self.login_button)

centered_row = QHBoxLayout()
centered_row.addStretch(1)
centered_row.addWidget(self.content_container, 100)
centered_row.addStretch(1)

layout = QVBoxLayout(self)
layout.addStretch(1)
layout.addLayout(centered_row)
layout.addStretch(1)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_login_window_ui.py -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_login_window_ui.py src/atv_player/ui/login_window.py
git commit -m "style: center and enlarge login window"
```

### Task 3: Verify related login regressions stay green

**Files:**
- Test: `tests/test_login_window_ui.py`
- Test: `tests/test_login_controller.py`

- [ ] **Step 1: Run targeted regression tests**

Run: `uv run pytest tests/test_login_window_ui.py tests/test_login_controller.py -q`
Expected: PASS
