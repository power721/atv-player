# Telegram Thumbnail Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display local Telegram video thumbnails through the existing local HTTP proxy.

**Architecture:** Add lazy thumbnail URL generation in the Telegram list controller, a thumbnail HTTP endpoint in `LocalHlsProxyServer`, and a small Telethon-backed thumbnail byte method in `TelegramMediaService`. No database schema change is needed.

**Tech Stack:** Python, Telethon, pytest, existing local proxy and poster loading pipeline.

---

### Task 1: Controller Thumbnail URL Mapping

**Files:**
- Modify: `src/atv_player/controllers/telegram_search_controller.py`
- Test: `tests/test_telegram_search_controller.py`

- [ ] Add a failing test that a local media result gets `vod_pic` from a thumbnail URL factory.
- [ ] Add an optional `telegram_thumbnail_url_factory` argument to `TelegramSearchController`.
- [ ] Pass the factory into `_map_local_resource()` for local browse/search results.
- [ ] Set `vod_pic` only for `media` resources.

### Task 2: Proxy Thumbnail Endpoint

**Files:**
- Modify: `src/atv_player/proxy/server.py`
- Test: `tests/test_hls_proxy_server.py`

- [ ] Add a failing test for `create_telegram_thumbnail_url()`.
- [ ] Add a failing test for `GET /tg/thumb/{chat_id}/{msg_id}` streaming image bytes.
- [ ] Implement URL creation and thumbnail response handling.
- [ ] Wire the thumbnail handler into HTTP `GET`.

### Task 3: Telegram Thumbnail Download

**Files:**
- Modify: `src/atv_player/telegram_media.py`
- Test: `tests/test_telegram_search_controller.py`

- [ ] Add a failing test for `TelegramMediaService.get_media_thumbnail_bytes()`.
- [ ] Implement it with `client.get_messages()` and `client.download_media(msg, file=bytes, thumb=-1)`.
- [ ] Return `b""` when Telethon returns no thumbnail.

### Task 4: App Wiring And Verification

**Files:**
- Modify: `src/atv_player/app.py`
- Test: existing Telegram and proxy tests

- [ ] Pass the proxy server thumbnail URL factory to both Telegram controllers.
- [ ] Run focused Telegram and proxy tests.
