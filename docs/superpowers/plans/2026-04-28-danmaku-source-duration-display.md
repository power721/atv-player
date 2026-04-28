# Danmaku Source Duration Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each danmaku source candidate's duration in the player window dialog as `标题 · 时长` while keeping unknown-duration items unchanged.

**Architecture:** Keep the change display-only in `PlayerWindow`. Add one private formatter for seconds-to-label conversion, then reuse it when building each `QListWidgetItem` in the danmaku source option list. Verify both positive-duration rendering and the existing URL-selection behavior through focused UI tests.

**Tech Stack:** Python, PySide6, pytest-qt

---

### Task 1: Add UI coverage for duration display in danmaku source options

**Files:**
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Write the failing tests**

Add these tests near the existing danmaku source dialog coverage:

```python
def test_player_window_shows_danmaku_source_option_duration_in_dialog(qtbot) -> None:
    item = PlayItem(
        title="正片",
        url="https://stream.example/movie.m3u8",
        media_title="疯狂动物城2",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(
                        provider="tencent",
                        name="疯狂动物城2",
                        url="https://v.qq.com/movie",
                        duration_seconds=5935,
                    )
                ],
            )
        ],
        selected_danmaku_provider="tencent",
        selected_danmaku_url="https://v.qq.com/movie",
        selected_danmaku_title="疯狂动物城2",
        danmaku_search_query="疯狂动物城2",
    )
    session = PlayerSession(
        vod=VodItem(vod_id="movie-1", vod_name="疯狂动物城2"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert window._danmaku_source_option_list is not None
    assert window._danmaku_source_option_list.count() == 1
    assert window._danmaku_source_option_list.item(0).text() == "疯狂动物城2 · 1:38:55"


def test_player_window_keeps_danmaku_source_option_url_when_duration_is_displayed(qtbot) -> None:
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[
                    DanmakuSourceOption(
                        provider="tencent",
                        name="红果短剧 第1集",
                        url="https://v.qq.com/demo",
                        duration_seconds=1458,
                    )
                ],
            )
        ],
        selected_danmaku_provider="tencent",
        selected_danmaku_url="https://v.qq.com/demo",
        selected_danmaku_title="红果短剧 第1集",
        danmaku_search_query="红果短剧 1集",
    )
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert window._selected_danmaku_source_url_from_dialog() == "https://v.qq.com/demo"
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run:

```bash
uv run pytest tests/test_player_window_ui.py -k "danmaku_source_option_duration or keeps_danmaku_source_option_url_when_duration_is_displayed"
```

Expected:

```text
FAILED tests/test_player_window_ui.py::test_player_window_shows_danmaku_source_option_duration_in_dialog
```

The first failure should show the old title-only text without the duration suffix.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_player_window_ui.py
git commit -m "test: cover danmaku source duration display"
```

### Task 2: Render formatted durations in the danmaku source option list

**Files:**
- Modify: `src/atv_player/ui/player_window.py`
- Modify: `tests/test_player_window_ui.py`
- Test: `tests/test_player_window_ui.py`

- [ ] **Step 1: Add a private duration formatter**

In `PlayerWindow`, add a helper near the danmaku source dialog methods:

```python
    def _format_danmaku_source_duration(self, duration_seconds: int) -> str:
        if duration_seconds <= 0:
            return ""
        hours, remainder = divmod(int(duration_seconds), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"
```

- [ ] **Step 2: Use the formatter when populating the option list**

Update `_populate_danmaku_source_option_list()` so each visible label includes the duration when available:

```python
        for index, option in enumerate(target_group.options):
            label = option.name
            duration_text = self._format_danmaku_source_duration(option.duration_seconds)
            if duration_text:
                label = f"{label} · {duration_text}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, option.url)
            self._danmaku_source_option_list.addItem(item)
            if option.url == selected_url:
                selected_index = index
```

- [ ] **Step 3: Add a regression test for unknown durations staying title-only**

Add this test beside the two new duration tests:

```python
def test_player_window_keeps_title_only_for_unknown_danmaku_source_duration(qtbot) -> None:
    item = PlayItem(
        title="第1集",
        url="https://stream.example/1.m3u8",
        media_title="红果短剧",
        danmaku_candidates=[
            DanmakuSourceGroup(
                provider="tencent",
                provider_label="腾讯",
                options=[DanmakuSourceOption(provider="tencent", name="红果短剧 第1集", url="https://v.qq.com/demo")],
            )
        ],
        selected_danmaku_provider="tencent",
        selected_danmaku_url="https://v.qq.com/demo",
        selected_danmaku_title="红果短剧 第1集",
        danmaku_search_query="红果短剧 1集",
    )
    session = PlayerSession(
        vod=VodItem(vod_id="1", vod_name="红果短剧"),
        playlist=[item],
        start_index=0,
        start_position_seconds=0,
        speed=1.0,
    )
    window = PlayerWindow(FakePlayerController())
    qtbot.addWidget(window)

    window.open_session(session)
    window._open_danmaku_source_dialog()

    assert window._danmaku_source_option_list is not None
    assert window._danmaku_source_option_list.item(0).text() == "红果短剧 第1集"
```

- [ ] **Step 4: Run the focused UI tests to verify they pass**

Run:

```bash
uv run pytest tests/test_player_window_ui.py -k "danmaku_source_option_duration or keeps_danmaku_source_option_url_when_duration_is_displayed or keeps_title_only_for_unknown_danmaku_source_duration"
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Run the existing danmaku source dialog coverage to verify no regression**

Run:

```bash
uv run pytest tests/test_player_window_ui.py -k "opens_danmaku_source_dialog or loading_cached_search_result or switches_danmaku_source"
```

Expected:

```text
all selected tests pass
```

- [ ] **Step 6: Commit the implementation**

```bash
git add src/atv_player/ui/player_window.py tests/test_player_window_ui.py
git commit -m "feat: show danmaku source durations"
```
