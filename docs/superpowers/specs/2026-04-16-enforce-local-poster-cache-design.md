# Enforce Local Poster Cache Design

## Summary

Remote poster URLs should remain the source of truth in model data, but every UI surface that renders a remote poster must go through the shared local file cache path first. The cache layer continues to live in `poster_loader.py`; the change here is behavioral tightening, not a new caching system.

This design updates the current poster-cache behavior in one important way: a bad or unreadable cache file should no longer terminate poster loading. The loader should fall back to downloading fresh bytes, replace the bad cache file when possible, and keep the UI rendering path consistent across the home poster grid and the player window.

## Goals

- Keep `vod_pic` as a remote URL when the backend provides one.
- Force all remote poster rendering through the shared on-disk cache loader.
- Reuse existing cache files before any network request.
- Recover from corrupt or unreadable cache files by retrying the remote download.
- Keep `DoubanPage`, the Telegram poster grid, and `PlayerWindow` on the same loader path.

## Non-Goals

- Replacing `vod_pic` with local file paths in controller or model code.
- Adding manual cache management UI.
- Introducing memory-cache coordination or prefetch queues.
- Changing poster layout, sizing, or request-header rules.

## Scope

Primary implementation lives in:

- `src/atv_player/ui/poster_loader.py`
- `src/atv_player/ui/douban_page.py`
- `src/atv_player/ui/player_window.py`

Primary verification lives in:

- `tests/test_poster_loader.py`
- `tests/test_douban_page_ui.py`
- `tests/test_player_window_ui.py`

No controller or API changes are required.

## Design

### Shared Loader Contract

`load_remote_poster_image()` stays the single entry point for remote poster rendering. UI code should not check cache files directly, should not rewrite `vod_pic`, and should not branch between local-vs-remote behavior on its own.

The contract becomes:

1. Normalize the incoming URL.
2. Resolve the deterministic cache file path for that URL.
3. If the cache file exists, try to load and scale it from local bytes.
4. If local decode succeeds, return that image immediately.
5. If the cache file is missing, unreadable, or decodes to a null image, continue to the remote download path.
6. If the remote download succeeds and decodes, write the fresh bytes back to the cache file and return the scaled image.
7. If the remote download fails, return `None`.

This ensures all poster consumers keep the same behavior while making the cache layer self-healing.

### Local Cache First

The cache file remains the preferred source for every remote poster render. The local file is not an optimization that some views may bypass; it is the required first step whenever the source is a remote URL.

The current `DoubanPage` and `PlayerWindow` already rely on `poster_loader.py`. That should remain unchanged structurally. The main behavioral change is making the loader resilient enough that these views never need special-case cache recovery logic.

Because the Telegram poster tab reuses `DoubanPage`, it will inherit the same local-cache-first behavior automatically.

### Corrupt Cache Recovery

The current cache behavior treats corrupt cached bytes as a terminal miss. This design changes that.

If the cache file exists but cannot be read or decoded into a valid image:

- do not return early
- do not raise an error to the UI
- continue to remote download using the normalized URL
- if remote download succeeds, overwrite the cache file with the new bytes when possible

If cache overwrite fails after a successful download, the UI should still receive the decoded image for the current render attempt.

### UI Expectations

No page-specific cache logic should be added.

`DoubanPage` and `PlayerWindow` should continue to do exactly this:

- normalize the poster URL if needed
- call `load_remote_poster_image()`
- render the returned image when available

The only visible effect should be that cached files are used consistently and stale/corrupt local bytes no longer cause avoidable blank posters when the remote source is still available.

### Error Handling

Failure handling should remain quiet and local to the loader:

- empty URL: return `None`
- unreadable cache file: fall through to remote fetch
- corrupt cached image bytes: fall through to remote fetch
- remote HTTP failure: return `None`
- cache write failure after successful download: still return the downloaded image

No new dialogs, status text, or user-facing error messaging should be added.

## Testing Strategy

Add focused tests in `tests/test_poster_loader.py` for:

- cache hit returns the image without calling the network function
- missing cache downloads and writes the cache file
- corrupt cached bytes trigger a remote retry instead of returning `None`
- unreadable cache bytes or read failures still allow a remote retry
- cache write failures still return a valid image for the current load

Add UI-level confidence tests only where needed to guard the shared integration points:

- `DoubanPage` still renders a poster icon when the shared loader returns an image
- `PlayerWindow` still renders poster images through the shared loader path

The goal is to keep most coverage in `poster_loader.py`, not duplicate cache tests across every UI surface.

## Implementation Order

1. Add failing `poster_loader` tests for corrupt-cache fallback and unreadable-cache fallback.
2. Confirm the new tests fail for the current early-return behavior.
3. Refine `load_remote_poster_image()` so cache misses and cache decode failures both continue to the remote fetch path.
4. Re-run focused poster-loader tests.
5. Re-run the existing Douban and player poster tests to confirm the shared loader contract still holds.
