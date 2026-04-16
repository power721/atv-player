# Python Spider Plugin Design

## Summary

Add a first-class Python spider plugin system to the desktop player so users can load TVBox-style `Spider` plugins from either a local `.py` file or a remote `.py` URL. Each enabled plugin should appear as its own home tab, for example `红果短剧.py` becoming a `红果短剧` tab, and should reuse the existing poster-grid browsing flow for categories, item lists, search, detail playback, and final playback URL resolution.

The first release should include an in-app plugin manager with add/remove, enable/disable, display-name editing, tab-order adjustment, refresh, and load-log viewing. The runtime only needs to support trusted TVBox-style plugins that work with the app's bundled Python dependencies and the provided `base.spider.Spider` compatibility shim.

## Goals

- Load spider plugins from local file paths and remote Python file URLs.
- Show each enabled plugin as a dynamic home tab in the main window.
- Map TVBox `Spider` methods into the existing app flow:
  - `homeContent`
  - `categoryContent`
  - `detailContent`
  - `playerContent`
  - `searchContent`
- Reuse `PosterGridPage` instead of adding a new browsing page.
- Add an in-app plugin manager that supports:
  - add local file
  - add remote URL
  - edit display name
  - enable or disable plugin
  - move plugin tab order up or down
  - refresh and reload plugin
  - view load and runtime logs
- Keep the app running when individual plugins fail to load or execute.

## Non-Goals

- Installing third-party dependencies on behalf of plugins.
- Sandboxing or isolating plugin execution in a separate process.
- Automatically checking remote plugins for updates in the background.
- Supporting arbitrary non-TVBox plugin interfaces.
- Replacing the existing built-in tabs or their controllers.

## Scope

Primary implementation lives in:

- `src/atv_player/models.py`
- `src/atv_player/storage.py`
- `src/atv_player/app.py`
- `src/atv_player/ui/main_window.py`
- `src/atv_player/ui/help_dialog.py`
- `src/atv_player/ui/poster_grid_page.py`
- `src/atv_player/plugins/__init__.py`
- `src/atv_player/plugins/compat/base/spider.py`
- `src/atv_player/plugins/loader.py`
- `src/atv_player/plugins/controller.py`
- `src/atv_player/plugins/repository.py`
- `src/atv_player/ui/plugin_manager_dialog.py`

Primary verification lives in:

- `tests/test_storage.py`
- `tests/test_app.py`
- `tests/test_main_window_ui.py`
- `tests/test_poster_grid_page_ui.py`
- `tests/test_spider_plugin_loader.py`
- `tests/test_spider_plugin_controller.py`
- `tests/test_plugin_manager_dialog.py`

## Design

### Architecture

The feature should be split into five units with clear boundaries:

1. `SpiderPluginRepository`
   Stores plugin metadata, tab ordering, cached remote file location, last error, and recent log entries.

2. `SpiderPluginLoader`
   Resolves a configured plugin source into a live Python spider instance. Local plugins load from the configured path. Remote plugins download into the app data directory cache and load from the cached file.

3. `SpiderPluginController`
   Adapts a loaded spider instance into the controller contract expected by `PosterGridPage`.

4. `PluginManagerDialog`
   Provides in-app management for plugin sources, display names, ordering, enabled state, refresh, and log viewing.

5. Dynamic tab assembly in `AppCoordinator` and `MainWindow`
   Loads enabled plugins at startup, builds a `PosterGridPage` per successful plugin, and inserts tabs in saved order.

This keeps plugin execution details out of the main window, keeps persistence concerns out of the loader, and makes controller behavior directly testable without Qt.

### Persistence Model

Plugin configuration should not be stored in `app_config`. Add dedicated SQLite tables managed through `SettingsRepository` or a focused repository wrapper:

- `spider_plugins`
  - `id INTEGER PRIMARY KEY`
  - `source_type TEXT NOT NULL` with values `local` or `remote`
  - `source_value TEXT NOT NULL`
  - `display_name TEXT NOT NULL DEFAULT ''`
  - `enabled INTEGER NOT NULL DEFAULT 1`
  - `sort_order INTEGER NOT NULL`
  - `cached_file_path TEXT NOT NULL DEFAULT ''`
  - `last_loaded_at INTEGER NOT NULL DEFAULT 0`
  - `last_error TEXT NOT NULL DEFAULT ''`

- `spider_plugin_logs`
  - `id INTEGER PRIMARY KEY`
  - `plugin_id INTEGER NOT NULL`
  - `level TEXT NOT NULL`
  - `message TEXT NOT NULL`
  - `created_at INTEGER NOT NULL`

`sort_order` controls tab placement among dynamic plugin tabs. The repository should expose small operations for insert, update, reorder, enable or disable, delete, refresh metadata, append log entry, and list logs for one plugin.

### Plugin Source Handling

Local plugin sources should store the absolute file path and load directly from disk. Refreshing a local plugin should simply re-import from the configured path.

Remote plugin sources should store the source URL and download the file into an application-owned cache directory such as:

- `~/.local/share/atv-player/plugins/cache/`

The cached filename should be deterministic per plugin, for example using the plugin id plus a sanitized suffix, so repeated refreshes replace the same file. If a remote refresh fails, the loader should keep the last successful cached file and mark the refresh failure in logs and plugin status.

The first release should not auto-refresh remote plugins in the background. Refresh only happens when the user explicitly requests it or when a remote plugin is added for the first time and must be validated.

### TVBox Compatibility Layer

The app should ship a minimal `base.spider.Spider` compatibility module under the app package so TVBox-style plugins can import:

```python
from base.spider import Spider
```

The compatibility shim only needs to support the subset required by the target plugins:

- lifecycle and interface stubs matching the TVBox base class
- helper methods such as `fetch`, `post`, `html`, `regStr`, `removeHtmlTags`, `cleanText`, and `log`
- module loading helpers only if needed by the plugin set

The shim should use bundled dependencies only. Missing third-party imports in a plugin should surface as load failures with a clear message such as `缺少依赖: pyquery`.

### Main Window Integration

`MainWindow` currently assembles a static tab list. Extend it so built-in tabs are still created first, then dynamic plugin tabs are inserted from loaded plugin definitions.

Each dynamic plugin tab should be a `PosterGridPage` configured in open mode with:

- category list enabled
- pagination enabled
- search controls enabled only if the plugin supports `searchContent`

Tab label selection order should be:

1. saved `display_name` from plugin config
2. plugin `getName()`
3. source filename without `.py`
4. source URL basename without `.py`

Plugin management should be opened from the main window through a dedicated button near the existing top-right controls. Closing the dialog after changes should trigger a dynamic-tab rebuild so enable, disable, reorder, rename, add, remove, and refresh all become visible without restarting the app.

### Poster Grid Controller Contract

The plugin controller should implement the same page-facing methods used by the shared poster grid:

- `load_categories() -> list[DoubanCategory]`
- `load_items(category_id: str, page: int) -> tuple[list[VodItem], int]`
- `search_items(keyword: str, page: int) -> tuple[list[VodItem], int]`
- `build_request(vod_id: str) -> OpenPlayerRequest`

Folder navigation is not a primary requirement for generic spider plugins because the requested TVBox interface already splits category browsing and detail playback. If a plugin returns items with `vod_tag == "folder"` and a category-like `vod_id`, the controller may treat that id as a nested `categoryContent` request, but the first release does not need a separate plugin-specific folder model beyond what the existing poster grid already supports.

### Spider Method Mapping

#### `homeContent`

Call `homeContent(False)` during initial load.

Use the payload as follows:

- `class` becomes the category list
- `list` becomes the recommended or home item list

To keep behavior consistent with existing tabs, prepend a synthetic `推荐` category using a reserved type id such as `home` when `list` contains items. Selecting that category should show the cached home list instead of calling `categoryContent`.

If `class` is empty and `list` is non-empty, the plugin should still load and show a single `推荐` category.

#### `categoryContent`

For normal categories, call:

```python
categoryContent(tid, str(page), False, {})
```

Map the returned `list` into `VodItem` entries and compute total count from:

1. `total` if provided
2. `pagecount * page_size` if provided
3. `len(list)` as a fallback

The controller should tolerate missing optional metadata such as `vod_pic`, `vod_year`, or `vod_remarks`.

#### `detailContent`

When the user opens a card, call:

```python
detailContent([vod_id])
```

Use the first item in the returned `list` as the canonical detail model. Map metadata fields into `VodItem` so the player window can continue to show title, poster, remarks, area, year, actors, director, and description when available.

Parse `vod_play_from` and `vod_play_url` into `PlayItem` entries. Multi-route payloads separated by `$$$` should be flattened into a single playlist while preserving route labels in the generated episode title, for example `线路A | 第1集`.

#### `playerContent`

The controller should defer final playback resolution until the player is about to open or switch episode. This avoids eagerly resolving every episode in a long playlist.

For each parsed `PlayItem`:

- if the raw value already looks like a direct media URL, store it as `PlayItem.url`
- otherwise store it as `PlayItem.vod_id` and leave `PlayItem.url` empty

When a `PlayItem` without `url` is selected, call:

```python
playerContent(flag, value, [])
```

Use the returned payload to fill:

- final `url`
- request `headers`

If the payload sets `parse=1` but still only returns an intermediate page URL, the first release should treat that as unsupported and show a user-facing error rather than silently failing. The goal of this release is direct TVBox-style playback URL extraction, not a general-purpose web parser.

#### `searchContent`

If the plugin class overrides `searchContent`, enable search in the tab and call:

```python
searchContent(keyword, False, str(page))
```

Map the returned `list` the same way as category items and compute totals using the same fallback order. If the method raises `NotImplementedError` or only returns the compatibility base-class stub behavior, hide or disable search for that plugin.

### Plugin Manager UI

The first release should add a modal `PluginManagerDialog` with a table-based management view. Each row should represent one configured plugin and show:

- display name
- source type
- source value
- enabled state
- current status
- last loaded time

The dialog should support these actions:

- `添加本地插件`
  - opens a file picker for `.py`
  - validates by loading immediately

- `添加远程插件`
  - prompts for a URL
  - downloads and validates immediately

- `编辑名称`
  - updates only the app-side display name

- `启用/禁用`
  - toggles whether the plugin becomes a tab

- `上移/下移`
  - swaps `sort_order`

- `刷新`
  - reloads the selected plugin
  - re-downloads remote source first

- `查看日志`
  - opens a simple log viewer for the selected plugin

- `删除`
  - removes the plugin configuration
  - leaves remote cache cleanup as best-effort

Validation and failure feedback should be immediate in the dialog. Successful changes should persist before the dialog closes.

### Error Handling

The plugin system should never abort the whole application because of one bad plugin.

Load failures should result in:

- no tab for that plugin
- `last_error` updated
- a log entry appended with method and exception context

Runtime failures in `homeContent`, `categoryContent`, `detailContent`, `playerContent`, or `searchContent` should result in:

- the page showing a user-facing error string
- the player not opening when playback cannot be resolved
- a runtime log entry persisted for the plugin

Remote plugin download failures should preserve the last successful cached file path so an already-working plugin can continue to load from cache.

### Trust and Safety Boundary

Plugins should be treated as trusted code chosen by the user. The app should not claim sandboxing or isolation. The plugin manager should surface a clear warning that remote plugins execute local Python code with the user's permissions.

This warning is sufficient for the first release. No extra permission prompts or subprocess isolation are required in this design.

## Testing Strategy

Add focused tests in `tests/test_storage.py` for:

- plugin table migration on existing databases
- insert, update, enable or disable, delete, and reorder operations
- plugin log append and retrieval

Add focused tests in `tests/test_spider_plugin_loader.py` for:

- loading a local plugin file
- downloading and loading a remote plugin into cache
- remote refresh failure falling back to prior cached file
- missing `Spider` class
- import failure from missing dependency

Add focused tests in `tests/test_spider_plugin_controller.py` for:

- `homeContent` producing `推荐` plus backend categories
- category paging mapped from `categoryContent`
- detail playlist parsing from `vod_play_from` and `vod_play_url`
- direct URL play items skipping `playerContent`
- deferred `playerContent` resolution producing final URL and headers
- search-enabled and search-disabled plugin behavior

Add focused UI tests in `tests/test_plugin_manager_dialog.py` for:

- adding local and remote plugins
- editing display name
- reordering rows
- enabling or disabling plugins
- showing logs

Add integration coverage in `tests/test_app.py` and `tests/test_main_window_ui.py` for:

- dynamic plugin tabs inserted after built-in media tabs in saved order
- disabled or failed plugins omitted from tabs
- plugin manager changes rebuilding the visible tabs
- plugin tab search visibility matching plugin capability

Use at least two fake plugin fixtures:

- a minimal plugin with categories, detail, and direct URLs
- a plugin close to `红果短剧.py` behavior with search and deferred `playerContent`

## Implementation Order

1. Add failing storage tests for plugin tables and log persistence.
2. Add failing loader tests for local loading, remote caching, and error handling.
3. Add failing controller tests for home, category, detail, player, and search mapping.
4. Implement the repository and loader.
5. Implement the compatibility shim and plugin controller.
6. Add the plugin manager dialog and its focused UI tests.
7. Wire dynamic plugin tabs and plugin-manager entry into `MainWindow` and `AppCoordinator`.
8. Run focused verification and then the relevant broader app test suites.
