# Live Playback Without Local History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make network live playback skip both loading local resume history and saving local playback history.

**Architecture:** Keep the history policy at the request-construction layer by having `LiveController.build_request()` explicitly return `OpenPlayerRequest(use_local_history=False)`. Rely on the existing `MainWindow` pass-through and `PlayerController` skip-history behavior, and lock the contract with one controller test and one app-flow test.

**Tech Stack:** Python 3.12, pytest, PySide6

---

### Task 1: Add Failing Tests For Live Request History Policy

**Files:**
- Modify: `tests/test_live_controller.py`
- Modify: `tests/test_app.py`
- Reference: `src/atv_player/controllers/live_controller.py`

- [ ] **Step 1: Add the failing controller test**

In `tests/test_live_controller.py`, extend `test_build_request_parses_title_url_playlist_from_detail_payload()` with the new assertion, or add a dedicated test immediately after it:

```python
def test_build_request_disables_local_history() -> None:
    from atv_player.controllers.live_controller import LiveController

    api = FakeApiClient()
    api.detail_payload = {
        "list": [
            {
                "vod_id": "bili$1785607569",
                "vod_name": "主播直播间",
                "vod_play_url": "线路 1$https://stream.example/live.m3u8",
            }
        ]
    }
    controller = LiveController(api)

    request = controller.build_request("bili$1785607569")

    assert request.use_local_history is False
```

- [ ] **Step 2: Add the failing app-flow test assertion**

In `tests/test_app.py`, extend `test_main_window_opens_player_from_live_card_signal()` with:

```python
    assert opened[0][0].use_local_history is False
```

- [ ] **Step 3: Run the focused tests to verify they fail**

Run: `uv run pytest tests/test_live_controller.py::test_build_request_disables_local_history tests/test_app.py::test_main_window_opens_player_from_live_card_signal -q`

Expected: FAIL because live requests currently leave `use_local_history` at its default `True`.

- [ ] **Step 4: Commit the red tests**

```bash
git add tests/test_live_controller.py tests/test_app.py
git commit -m "test: cover live playback history policy"
```

### Task 2: Disable Local History In Live Requests

**Files:**
- Modify: `src/atv_player/controllers/live_controller.py`
- Test: `tests/test_live_controller.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Implement the minimal request change**

In `src/atv_player/controllers/live_controller.py`, update the `OpenPlayerRequest` construction in `build_request()` to set the history flag explicitly:

```python
        return OpenPlayerRequest(
            vod=detail,
            playlist=playlist,
            clicked_index=0,
            source_mode="detail",
            source_vod_id=detail.vod_id,
            use_local_history=False,
        )
```

- [ ] **Step 2: Run the focused tests to verify they pass**

Run: `uv run pytest tests/test_live_controller.py::test_build_request_disables_local_history tests/test_app.py::test_main_window_opens_player_from_live_card_signal -q`

Expected: PASS with live requests now carrying `use_local_history=False`.

- [ ] **Step 3: Run the relevant full files**

Run: `uv run pytest tests/test_live_controller.py tests/test_app.py -q`

Expected: PASS so the live request change does not break existing main-window or live-controller behavior.

- [ ] **Step 4: Commit the implementation**

```bash
git add src/atv_player/controllers/live_controller.py tests/test_live_controller.py tests/test_app.py
git commit -m "feat: disable local history for live playback"
```

### Task 3: Final Verification And Scope Check

**Files:**
- Modify: `src/atv_player/controllers/live_controller.py`
- Modify: `tests/test_live_controller.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Inspect the final diff**

Run: `git diff -- src/atv_player/controllers/live_controller.py tests/test_live_controller.py tests/test_app.py`

Expected: only the live request history flag and its tests are included.

- [ ] **Step 2: Run the complete verification command**

Run: `uv run pytest -q`

Expected: PASS with the full test suite green.

- [ ] **Step 3: Commit the verified result**

```bash
git add src/atv_player/controllers/live_controller.py tests/test_live_controller.py tests/test_app.py
git commit -m "feat: skip local history for network live playback"
```
