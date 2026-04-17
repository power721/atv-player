# Live Default Source Naming And Ordering Design

## Summary

Adjust the custom live-source behavior in two small ways:

- rename the default example source from `示例直播源` to `IPTV`
- move enabled custom live sources to the end of the `网络直播` category list instead of the beginning

The default example source should still be auto-created on first initialization and should still use the configured remote `m3u` URL. Existing database rows and custom source ordering should remain unchanged.

## Goals

- Default auto-created source name becomes `IPTV`
- `网络直播` category order becomes:
  1. `推荐`
  2. backend live categories
  3. enabled custom live sources
- Keep current enable/disable and sort behavior for custom sources themselves

## Non-Goals

- Changing database schema
- Changing custom-source internal ordering
- Changing source-type display or source URLs beyond the already updated default URL

## Design

### Default Source Naming

Update the default source constant in `src/atv_player/live_source_repository.py`:

- `_DEFAULT_SOURCE_NAME = "IPTV"`

This affects only newly initialized databases. Existing rows already stored in user databases are left untouched.

### Live Category Ordering

Update `LiveController.load_categories()` so that custom categories are appended after backend live categories rather than prepended before them.

Current desired order:

1. `DoubanCategory(type_id="0", type_name="推荐")`
2. mapped backend live categories
3. custom categories from `custom_live_service.load_categories()`

This keeps custom sources visible without displacing the built-in live categories.

## Testing

Update focused tests:

- `tests/test_live_source_repository.py`
  Assert the default source name is `IPTV`
- `tests/test_live_controller.py`
  Assert custom live categories are appended after backend categories

No broader architecture or UI tests are needed beyond the existing focused checks because this change only affects one constant and one category ordering rule.

## Risks And Mitigations

- Risk: users expect existing stored example sources to be renamed automatically.
  Mitigation: keep this change scoped to new default initialization only; avoid hidden data migration.
- Risk: category ordering change unintentionally affects backend categories.
  Mitigation: keep the test explicit about final order.
