# Poster File Cache Design

## Summary

All remote poster loads should use a shared on-disk cache so the app can reuse previously downloaded images across restarts. The cache should live under `~/.cache/atv-player/posters/`, and application startup should remove poster cache files older than 7 days.

This change should stay narrow. Poster caching belongs in the shared poster-loading helper so both the Douban page and the player window pick up the behavior without duplicating cache code.

## Goals

- Cache all remotely downloaded posters in local files under `~/.cache/atv-player/posters/`.
- Reuse cached poster files on later loads instead of issuing another HTTP request.
- Keep the existing poster rendering behavior for both `DoubanPage` and `PlayerWindow`.
- Delete poster cache files older than 7 days during application startup.
- Ensure cache failures do not block app startup or break poster rendering.

## Non-Goals

- Cache posters in memory beyond the existing widget state.
- Add cache size limits, LRU eviction, or manual cache management UI.
- Change poster sizing, scaling, or referer/header behavior.
- Persist cache metadata in the database.

## Scope

Primary implementation lives in:

- `src/atv_player/ui/poster_loader.py`
- `src/atv_player/app.py`

Primary verification lives in:

- `tests/test_poster_loader.py`
- `tests/test_app.py`

No UI layout changes are required.

## Design

### Cache Location

Use a dedicated poster cache directory at:

- `~/.cache/atv-player/posters/`

Implementation should resolve this through `Path.home()` so tests can redirect the home directory cleanly.

The cache directory should be created on demand before reads or writes and also ensured during app startup.

### Cache Key and File Naming

Use the normalized poster URL as the cache key. This keeps the existing Douban URL normalization behavior and ensures `s_ratio_poster` and the upgraded `m` path map to the same cache file.

Generate the cache filename from a stable hash of the normalized URL. This avoids unsafe path characters, query-string issues, and collisions caused by trying to preserve remote filenames directly.

The exact filename format does not need to be user-readable, but it must be deterministic for the same normalized URL.

### Load Flow

`load_remote_poster_image()` remains the single shared entry point for remote poster loads.

Updated behavior:

1. Normalize the incoming poster URL.
2. Resolve the cache file path for that normalized URL.
3. If a cache file exists, read its bytes locally and attempt to decode the image.
4. If no cache file exists, perform the current HTTP request with the same headers and timeout.
5. When the download succeeds and the image bytes decode correctly, write the original response bytes to the cache file.
6. Return a scaled `QImage` matching the existing sizing behavior.

This keeps all cache concerns behind the helper and requires no cache logic in `DoubanPage` or `PlayerWindow`.

### Failure Handling

Failure behavior must stay fail-closed:

- unreadable cache file: return `None`
- corrupt cache image bytes: return `None`
- HTTP failure: return `None`
- cache write failure after a successful download: still return the decoded image if possible

The cache is an optimization layer, not a new source of UI errors.

### Startup Cleanup

App startup should trigger one cleanup pass for the poster cache directory.

Cleanup behavior:

- ensure `~/.cache/atv-player/posters/` exists
- scan files directly inside that directory
- delete files whose modification time is older than 7 days from startup time
- leave newer files untouched
- ignore deletion failures and unexpected directory contents

This cleanup should run from the application initialization path rather than from a specific UI page, so stale cache files are handled consistently even if the user never opens the Douban tab or player window in a given run.

## Testing Strategy

Add focused tests in `tests/test_poster_loader.py` for:

- cached poster file being reused without issuing a network request
- first-time remote load writing a cache file
- cached bytes decoding into the same scaled image behavior used today
- corrupt cached bytes returning `None`
- cache write failures not preventing a successful image result from being returned

Add focused tests in `tests/test_app.py` for:

- application startup ensuring the poster cache directory exists under `~/.cache/atv-player/posters/`
- startup cleanup deleting files older than 7 days
- startup cleanup keeping newer files

## Implementation Order

1. Add failing poster-loader tests for cache hit, cache write, and cache failure behavior.
2. Add failing app tests for startup cache directory creation and 7-day cleanup.
3. Implement shared cache helpers in `poster_loader.py`.
4. Implement startup cache cleanup in `app.py`.
5. Run the focused poster-loader and app test suites.
