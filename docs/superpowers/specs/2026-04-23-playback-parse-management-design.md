# Playback Parse Management Design

## Summary

Add first-class playback parse source management for spider-plugin playback. Users should be able to manage parse interfaces inside the app, persist them in SQLite, automatically resolve `playerContent()` results that return `{"parse": 1}`, and manually override the preferred parse source from the player window through a dedicated combo box.

## Goals

- Add an in-app parse manager next to the existing plugin manager.
- Persist parse sources in SQLite instead of hardcoding them in memory.
- Seed the database with five built-in parse interfaces on first run.
- When a spider plugin returns `parse=1`, resolve the real playback URL through a parse source automatically.
- Use `User-Agent: okhttp/4.1.0` by default for parse-source HTTP requests.
- Let users manually choose a parse source in the player window.
- Persist the user-selected parse source as the global default and fall back to automatic probing when that source fails.

## Non-Goals

- Converting external JavaScript parse implementations into local Python spider plugins.
- Supporting parse sources outside spider-plugin playback.
- Health checks, latency ranking, or background availability probing for parse sources.
- Caching parse results across sessions.
- Parsing arbitrary custom response schemas beyond the direct-play URL cases the app already accepts.

## Scope

Primary implementation lives in:

- `src/atv_player/models.py`
- `src/atv_player/storage.py`
- `src/atv_player/app.py`
- `src/atv_player/ui/main_window.py`
- `src/atv_player/ui/player_window.py`
- `src/atv_player/plugins/controller.py`
- `src/atv_player/plugins/repository.py`
- `src/atv_player/plugins/__init__.py`
- `src/atv_player/ui/plugin_manager_dialog.py`
- new parse-management modules under `src/atv_player/`

Primary verification lives in:

- `tests/test_storage.py`
- `tests/test_spider_plugin_controller.py`
- `tests/test_main_window_ui.py`
- `tests/test_player_window_ui.py`
- `tests/test_parse_manager_dialog.py`

## Built-In Parse Sources

Seed the following parse sources into the database if they are not already present:

- `云` -> `https://yparse.ik9.cc/index.php?url=`
- `虾米` -> `https://jx.hls.one/?url=`
- `m3u8TV` -> `https://jx.m3u8.tv/jiexi/?url=`
- `77` -> `https://jx.77flv.cc/?url=`
- `咸鱼` -> `https://jx.xymp4.cc/?url=`

Seeded sources should default to enabled, preserve insertion order as sort order, and remain user-editable after creation.

## Design

### Persistence

Add a dedicated parse-source model with fields for:

- `id`
- `name`
- `url`
- `headers_text`
- `enabled`
- `sort_order`
- `is_builtin`

Create a new `parse_sources` table in the existing SQLite database. The table should store one row per parse source and support older databases through the existing migration pattern already used in repository code.

Add a global default parse-source id to application settings so manual selection in the player window can survive app restarts.

### Repository and Service Boundaries

Keep parse-source persistence separate from spider-plugin metadata even though both live in the same SQLite file.

The repository layer should support:

- list parse sources ordered by `sort_order`
- add parse source
- update parse source
- rename parse source
- enable or disable parse source
- move parse source up or down
- delete parse source
- seed built-in parse sources if missing

Add a small parse manager or resolver service above the repository. That service owns selection order, HTTP request behavior, and response normalization so the player window and spider controller stay thin.

### Parse Resolution Flow

When `SpiderPluginController` calls `playerContent(...)`, keep the current direct-play path for:

- `parse == 0`
- direct media URLs already present in `url`

When the payload returns `parse == 1`, treat `payload["url"]` as the unresolved target URL and pass it into the parse resolver.

Resolution order should be:

1. If the user manually selected a parse source in the player window, try that source first.
2. Otherwise, if a global default parse source exists, try that source first.
3. Then try the remaining enabled parse sources in configured sort order.

For each parse source request:

- build the request URL as `parse_source.url + quote(target_url, safe="")`
- send default header `User-Agent: okhttp/4.1.0`
- merge user-configured parse-source headers on top of the default header
- accept redirects
- treat the final response URL or response body as successful only when a direct playable media URL can be extracted using the app's existing media-URL heuristics

The initial implementation should prioritize the simple interface form the user requested: parse source URL acts as a prefix and returns a final media URL through normal fetch behavior. If a particular source later needs custom HTML scraping or JSON decoding, that should be a future enhancement in the parse resolver only.

### Response Normalization

The resolver should return a structured result containing:

- resolved media URL
- headers to use for playback
- parse source id and name that succeeded

Playback headers should include the effective parse-source headers, because some parse interfaces require the same headers on subsequent media requests.

If all enabled parse sources fail, surface a clear error such as `没有可用的解析接口` or `解析失败` and include per-source failure messages in player logs.

### Player Window UI

Add a parse combo box immediately after the audio-track combo box.

Behavior:

- first item is fixed text `解析`
- following items are enabled parse sources in current sort order
- if no parse sources are enabled, keep the combo disabled and show only the placeholder item
- when the user selects a concrete parse source, persist that source id as the global default
- if the current play item is still unresolved and depends on parse resolution, retry playback using the newly selected source
- if the selected source fails, keep playback behavior resilient by falling back to the remaining enabled sources

The combo box is a preference control, not a per-episode history field. It should reflect the current global default when a player window opens.

### Main Window UI

Add a `解析管理` button immediately after `插件管理`.

The dialog should follow the existing plugin-manager interaction style:

- table view of saved sources
- add
- edit
- rename
- enable or disable
- move up
- move down
- delete

Editing should include:

- name
- URL
- optional HTTP headers as raw text

Header text can be stored as JSON text. The first release only needs light validation: valid empty value or valid JSON object string. Invalid JSON should be rejected in the dialog before saving.

### Parse Manager Dialog

Use a separate dialog rather than overloading the plugin manager dialog.

Suggested columns:

- 名称
- 地址
- 请求头
- 启用
- 内置

Show a compact header summary in the table, not a large multi-line blob. The full header JSON remains editable in a modal multi-line editor.

### App Wiring

Instantiate the parse-source repository and manager in `AppCoordinator` alongside the existing spider-plugin repository and manager.

Pass the parse manager into:

- `MainWindow` so it can open the parse-manager dialog
- `PlayerWindow` so it can populate the parse combo and persist manual selection
- `SpiderPluginController` or the player-session playback loader path so `parse=1` playback can resolve through the shared service

Keep one shared manager instance for the whole app so UI state and playback resolution use the same enabled-source ordering and saved default.

### Error Handling

Failure cases should be explicit:

- no enabled parse source: fail fast with a user-readable error
- invalid configured headers JSON: reject save in the dialog
- parse source timeout or request failure: log source-specific error and continue to the next source
- parse response does not contain a playable URL: treat as a failed source and continue

The app should not mark parse sources permanently unhealthy from a single failure. Each playback attempt starts fresh from current configuration.

## Testing

Tests should cover:

- repository migration and built-in source seeding
- parse-source CRUD, reorder, rename, update, enable or disable, and delete
- persistence of the global default parse-source id
- resolver preferring manual or saved default source first
- resolver falling back to later sources after failure
- resolver merging default `User-Agent` with configured headers
- spider plugin playback resolving `parse=1` payloads through the shared resolver
- main window wiring for the new `解析管理` button
- player window rendering and updating the parse combo box

## Result

After this change, spider plugins that return `{"parse": 1}` can be played through app-managed parse interfaces. Users can manage parse endpoints in the UI, the player window exposes a persistent parse preference, and playback automatically falls back across enabled parse sources until one resolves a real media URL.
