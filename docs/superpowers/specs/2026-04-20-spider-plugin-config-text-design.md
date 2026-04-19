# Spider Plugin Config Text Design

## Summary

Add per-plugin config text storage for Python spider plugins. Users should edit this config inside the plugin manager, the app should persist the raw text per plugin, and the loader should initialize each spider with `Spider.init(config_text)` instead of the current empty string.

## Goals

- Store one raw config text blob per spider plugin.
- Let users edit that config inside the existing plugin manager dialog.
- Pass the stored config text into `Spider.init(...)` for both local and remote plugins.
- Keep existing plugin records and existing databases working through migration.

## Non-Goals

- Parsing, validating, or normalizing plugin config text.
- Defining a shared config schema across different spiders.
- Showing long config text directly in the plugin list table.
- Supporting multiple config variants per plugin.

## Scope

Primary implementation lives in:

- `src/atv_player/models.py`
- `src/atv_player/plugins/repository.py`
- `src/atv_player/plugins/__init__.py`
- `src/atv_player/plugins/loader.py`
- `src/atv_player/ui/plugin_manager_dialog.py`

Primary verification lives in:

- `tests/test_storage.py`
- `tests/test_spider_plugin_loader.py`
- `tests/test_spider_plugin_manager.py`
- `tests/test_plugin_manager_dialog.py`

## Design

### Persistence

Extend `SpiderPluginConfig` with a new `config_text: str = ""` field.

Add a `config_text TEXT NOT NULL DEFAULT ''` column to the `spider_plugins` table. The repository should keep using the existing table and migrate older databases with `PRAGMA table_info` plus `ALTER TABLE` when the column is missing.

`add_plugin()` should insert an empty config by default. `get_plugin()` and `list_plugins()` should read the new column. `update_plugin()` should support updating `config_text` alongside the existing mutable fields so callers do not need a separate table or repository path.

### Plugin Manager

The plugin list table should stay compact and continue to show operational metadata only. Config editing should be exposed as a separate action button, for example `编辑配置`, instead of adding a long-text column to the table.

The edit flow should use a modal multi-line text editor dialog seeded with the current plugin config. Saving should preserve the raw text exactly as entered, including newlines and spaces, except for the normal distinction between save and cancel. Empty config remains valid.

### Loader Initialization

`SpiderPluginLoader.load()` should keep the existing module import flow, instantiate the spider, and then call:

```python
spider.init(config.config_text)
```

If the spider does not implement `init`, the loader should continue to tolerate that. This keeps backward compatibility with simpler plugins while enabling plugins that require external configuration.

### Plugin Manager Service

The plugin management service should expose a focused method to persist config text for one plugin, for example `set_plugin_config(plugin_id, config_text)`. The service should reuse the existing repository update path so config edits do not bypass current metadata handling.

### Error Handling

Editing config should not trigger immediate plugin reload. Config changes are just persisted. The existing refresh or startup loading flow remains responsible for surfacing `init` failures, and those failures should keep appearing through the current `last_error` and log mechanisms.

### Testing

Tests should cover:

- repository migration for older `spider_plugins` tables without `config_text`
- repository round-trip for saving and reading config text
- loader passing stored config text into `Spider.init(...)`
- plugin manager dialog wiring for opening the config editor and saving through the manager service
- plugin manager service persisting config text without regressing existing plugin operations

## Result

After this change, each spider plugin has one persisted raw config text. Users can edit it in the plugin manager, and every successful plugin load will initialize the spider with that stored text.
