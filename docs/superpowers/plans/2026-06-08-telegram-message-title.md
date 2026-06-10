# Telegram Message Title Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the first paragraph of a Telegram video message as the local video list title when available.

**Architecture:** Keep title derivation inside `telegram_media.py`, next to message parsing. `_resources_from_message()` will keep storing the file name separately while using a helper-derived display title for video resources.

**Tech Stack:** Python dataclasses, pytest, existing Telegram media repository and controller tests.

---

### Task 1: Add Failing Coverage

**Files:**
- Test: `tests/test_telegram_search_controller.py`

- [ ] **Step 1: Write the failing test**

Add this test near the existing `_resources_from_message()` tests:

```python
def test_telegram_video_resource_uses_first_message_paragraph_as_title() -> None:
    msg = SimpleNamespace(
        id=5,
        message=(
            "《恐怖地窖》地窖台阶永无止境？全家人陷入“数数”诅咒\n"
            "\n"
            " #恐怖   《#恐怖地窖》\n"
            "\n"
            "主演： 伊丽莎·库斯伯特/欧文·马肯/Dylan Fitzmaurice Brady"
        ),
        file=SimpleNamespace(
            name="The.Cellar.2022.1080p.mkv",
            size=123,
            mime_type="video/x-matroska",
        ),
    )

    resource = _resources_from_message(msg, chat_id=-100, chat_title="频道")[0]

    assert resource.title == "《恐怖地窖》地窖台阶永无止境？全家人陷入“数数”诅咒"
    assert resource.file_name == "The.Cellar.2022.1080p.mkv"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_telegram_search_controller.py::test_telegram_video_resource_uses_first_message_paragraph_as_title -q
```

Expected: FAIL because `resource.title` is currently the file name.

### Task 2: Implement Title Derivation

**Files:**
- Modify: `src/atv_player/telegram_media.py`
- Test: `tests/test_telegram_search_controller.py`

- [ ] **Step 1: Add helper**

Add this helper near `_link_title()`:

```python
def _message_title(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for paragraph in re.split(r"\n\s*\n+", normalized):
        cleaned = " ".join(line.strip() for line in paragraph.splitlines()).strip()
        if not cleaned:
            continue
        for link in (
            *extract_drive_links(cleaned),
            *extract_magnet_links(cleaned),
            *extract_ed2k_links(cleaned),
        ):
            cleaned = cleaned.replace(link, "").strip(" -:：\t")
        if cleaned:
            return cleaned[:120]
    return ""
```

- [ ] **Step 2: Use helper for video resources**

Change the media resource title assignment in `_resources_from_message()` from:

```python
title=file_name,
```

to:

```python
title=_message_title(text) or file_name,
```

- [ ] **Step 3: Run focused test**

Run:

```bash
uv run pytest tests/test_telegram_search_controller.py::test_telegram_video_resource_uses_first_message_paragraph_as_title -q
```

Expected: PASS.

### Task 3: Verify Telegram Regressions

**Files:**
- Test: `tests/test_telegram_search_controller.py`

- [ ] **Step 1: Run related tests**

Run:

```bash
uv run pytest tests/test_telegram_search_controller.py -q
```

Expected: PASS.
