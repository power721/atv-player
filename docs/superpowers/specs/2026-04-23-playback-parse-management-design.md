# Playback Parse Resolution Design

## Summary

Add built-in playback parsers for spider-plugin playback. When a spider plugin returns `{"parse": 1}`, the app should resolve the final media URL through a fixed set of built-in Python parsers converted from the JavaScript files under `/home/harold/Downloads/Telegram Desktop/爱优腾芒+movie360+解析js/jx`. The player window should expose those built-in parsers in a dedicated combo box so users can keep automatic probing or manually prefer one parser.

## Goals

- Convert the five JavaScript parsers in the `jx` directory into built-in Python parser implementations.
- Resolve spider-plugin playback URLs through those built-in parsers when `playerContent()` returns `parse=1`.
- Keep a parser combo box in the player window immediately after the audio-track combo box.
- Default to automatic parser probing and let users manually choose a preferred built-in parser.
- Persist the preferred built-in parser in app settings so later playback sessions reuse it.
- Use the parser-specific request behavior from the source JavaScript implementations.

## Non-Goals

- Adding a parse manager dialog.
- Storing parser definitions in a dedicated database table.
- Supporting add, edit, delete, reorder, or custom parser configuration in the UI.
- Supporting parse sources outside spider-plugin playback.
- Converting arbitrary external JavaScript parser files at runtime.

## Scope

Primary implementation lives in:

- `src/atv_player/models.py`
- `src/atv_player/storage.py`
- `src/atv_player/app.py`
- `src/atv_player/ui/main_window.py`
- `src/atv_player/ui/player_window.py`
- `src/atv_player/plugins/controller.py`
- new parser modules under `src/atv_player/`

Primary verification lives in:

- `tests/test_storage.py`
- `tests/test_spider_plugin_controller.py`
- `tests/test_main_window_ui.py`
- `tests/test_player_window_ui.py`

## Built-In Parsers

Implement the following built-in parsers from the JavaScript files in the `jx` directory:

- `fish` from `fish.js`
- `jx1` from `jx1.js`
- `jx2` from `jx2.js`
- `mg1` from `mg1.js`
- `tx1` from `tx1.js`

Each parser should keep the source-specific API endpoint and request behavior from the JavaScript version:

- `fish`
  - endpoint: `https://kalbim.xatut.top/kalbim2025/781718/play/video_player.php`
  - source user agent: `Mozilla/5.0 (Linux; Android 10; MI 8 Build/QKQ1.190828.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/83.0.4103.101 Mobile Safari/537.36 bsl/1.0;webank/h5face;webank/2.0`
- `jx1`
  - endpoint: `http://sspa8.top:8100/api/?key=1060089351&`
  - source user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57`
- `jx2`
  - endpoint: `http://sspa8.top:8100/api/?cat_ext=eyJmbGFnIjpbInFxIiwi6IW+6K6vIiwicWl5aSIsIueIseWlh+iJuiIsIuWlh+iJuiIsInlvdWt1Iiwi5LyY6YW3Iiwic29odSIsIuaQnOeLkCIsImxldHYiLCLkuZDop4YiLCJtZ3R2Iiwi6IqS5p6cIiwidG5tYiIsInNldmVuIiwiYmlsaWJpbGkiLCIxOTA1Il0sImhlYWRlciI6eyJVc2VyLUFnZW50Ijoib2todHRwLzQuOS4xIn19&key=星睿4k&`
  - source user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57`
- `mg1`
  - endpoint: `http://shybot.top/v2/video/jx/?shykey=4595a71a4e7712568edcfa43949236b42fcfcb04997788ebe7984d6da2c6a51c&qn=max&`
  - source user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57`
- `tx1`
  - endpoint: `http://shybot.top/v2/video/jx/?shykey=4595a71a4e7712568edcfa43949236b42fcfcb04997788ebe7984d6da2c6a51c&`
  - source user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36 Edg/110.0.1587.57`

The built-in parser order should match the order above.

## Design

### Parser Architecture

Create a small built-in parser module rather than storing parser definitions in SQLite.

Each parser should expose:

- stable parser key, for example `fish`
- display label for the player combo box
- API endpoint
- request headers
- one `resolve(flag, url)` method that returns:
  - final media URL
  - playback headers from the parser response if present

The parser implementation should mirror the JavaScript behavior:

- send `flag` and `url` as query parameters
- expect JSON response
- accept success when any of the following is true:
  - `parse == 0`
  - `jx == 0`
  - returned `url` already looks like a media URL
- propagate `header` or `headers` from the parser response into playback headers
- raise a parser-specific failure when the response does not contain a playable result

### Persistence

No parser table should be added to SQLite.

Persist only the user's preferred built-in parser key in application settings. This keeps the current requirement of a remembered user choice without introducing parser CRUD or a parser repository.

### Parse Resolution Flow

When `SpiderPluginController` calls `playerContent(...)`, keep the current direct-play path for:

- `parse == 0` with direct media URL already present in `url`
- direct media URLs already returned even if parser flags are missing

When the payload returns `parse == 1`, treat `payload["url"]` as the unresolved target URL and resolve it through the built-in parser service.

Resolution order should be:

1. If the player window currently selected a built-in parser, try that parser first.
2. Otherwise, if app settings contain a preferred parser key, try that parser first.
3. Then try the remaining built-in parsers in fixed order.

If the preferred parser fails, continue to the remaining built-in parsers automatically.

### Response Normalization

The built-in parser service should return:

- resolved media URL
- playback headers
- parser key that succeeded

The playback headers should come from parser response `header` or `headers` when present.

If all built-in parsers fail, raise a clear playback error and include parser-specific failures in player logs.

### Player Window UI

Keep a parser combo box immediately after the audio-track combo box.

Behavior:

- first item is fixed text `解析`
- following items are the fixed built-in parsers in the order `fish`, `jx1`, `jx2`, `mg1`, `tx1`
- selecting the placeholder means automatic probing
- selecting a concrete parser saves that parser key as the preferred parser in app settings
- when the current play item still depends on parse resolution, switching the combo should retry playback with the newly preferred parser first
- if a manually selected parser fails, playback should still fall back to the remaining built-in parsers

The combo box is only a preference control. It does not imply parser management and does not expose add, edit, or delete actions.

### Main Window UI

Do not add a `解析管理` button.

The main window should stay unchanged except for any plumbing needed to pass parser services into the player window or playback controller.

### App Wiring

Instantiate one shared built-in parser service in app startup and pass it into:

- `PlayerWindow` for combo-box population and saving preferred parser key
- `SpiderPluginController` or the playback loader path for `parse=1` resolution

Keep parser definitions in code so both playback and player UI use the same built-in ordering and labels.

### Error Handling

Failure cases should be explicit:

- missing or empty unresolved parse target URL: fail fast
- parser HTTP request failure: log parser-specific error and continue
- parser JSON response without playable URL: treat as parser failure and continue
- all parsers failed: surface a user-readable playback error

The app should not permanently disable a built-in parser after one failure. Each playback attempt should retry from the preferred parser and then continue through the fixed fallback order.

## Testing

Tests should cover:

- persistence of preferred built-in parser key in app settings
- built-in parser service matching the five JavaScript parser request contracts
- parser response normalization for `header` and `headers`
- spider plugin playback resolving `parse=1` payloads through the built-in parser service
- fallback from preferred parser to later built-in parsers after failure
- main window not exposing a parse manager button
- player window rendering the built-in parser combo box and persisting manual selection

## Result

After this change, spider plugins that return `{"parse": 1}` can be played through a fixed built-in set of Python parsers converted from the provided JavaScript files. The player window keeps a parser combo box for auto or manual preference, but the app does not expose parser management UI or parser database records.
