# Folder Item Detail Resolution Design

## Summary

When playback starts from the file browser on a video file, the app must no longer trust the folder listing item as the final playback source. Instead, it should resolve that episode through `/vod/<token>?ac=web&ids=<vod_id>` to fetch the playable URL and richer metadata before starting playback.

This detail lookup must also happen when moving to the previous or next episode from the player window. Resolved detail payloads should be cached in memory for the lifetime of the active player session so the same episode is not fetched repeatedly.

## Goals

- Resolve playable URLs for folder-played files through `get_detail()` before playback begins.
- Re-resolve the target episode through `get_detail()` when switching episodes in the player window.
- Cache per-episode detail results in memory for the active player session.
- Refresh the current episode metadata from resolved detail data when it becomes available.
- Stop episode navigation if detail resolution fails instead of falling back to stale folder-list URLs.

## Non-Goals

- Persist the detail cache outside the active player session.
- Change search-result playback behavior.
- Change the standalone detail-page playback path.
- Prefetch detail for every file in a folder before the user plays it.
- Add background resolution or asynchronous player navigation in this change.

## Scope

Primary implementation lives in:

- `src/atv_player/controllers/browse_controller.py`
- `src/atv_player/controllers/player_controller.py`
- `src/atv_player/models.py`
- `src/atv_player/ui/player_window.py`

Primary verification lives in:

- `tests/test_browse_controller.py`
- `tests/test_player_window_ui.py`
- `tests/test_app.py`

## Root Cause

Current folder playback is built from `BrowseController.build_request_from_folder_item()`, which copies data directly from the folder listing into an `OpenPlayerRequest`. The resulting playlist uses each folder item's `vod_play_url` as the playback URL.

That works only when the folder-listing API already includes a valid playable URL. For file items that require a detail lookup, `vod_play_url` may be blank or incomplete. The player window then attempts to play that unresolved URL directly, and previous/next episode navigation repeats the same mistake because it only walks the prebuilt playlist.

The actual playable URL and richer per-file metadata are available from `get_detail(ids=<vod_id>)`, but that path is currently used only for explicit detail-page playback.

## Design

### Request Construction

Folder-playback requests should still preserve folder ordering and clicked index, but each playlist item needs enough information to resolve itself later.

Extend `PlayItem` with a `vod_id` field. For folder playback, populate that field from the folder item's `vod_id`. Keep the existing `title`, `path`, `index`, and `size` fields.

`BrowseController.build_request_from_folder_item()` should no longer assume that `clicked_item.vod_play_url` is sufficient. Instead, it should:

- build the folder playlist with `vod_id` included on each playable file
- resolve the clicked item through `get_detail(clicked_item.vod_id)` immediately
- use the resolved detail to seed the initial request `vod`
- replace the clicked playlist item's URL with the resolved playable URL before opening the player

If the initial clicked-item detail lookup fails, request construction should fail and let the existing open-player error path display the error.

### Session-Level Detail Resolver And Cache

The player session needs a way to resolve any playlist item on demand without coupling the player window directly to the browse controller.

Extend `PlayerSession` with:

- an in-memory cache keyed by episode `vod_id`
- a resolver callback that accepts a `PlayItem` and returns a resolved `VodItem`

The resolver callback should come from `BrowseController` when the request originates from a folder. Detail-page playback can provide a no-op resolver or a resolver that returns the already-known data.

The cache stores resolved `VodItem` objects for the lifetime of the current player window session only. Closing the player discards it.

### Resolution Flow

When a folder-originated episode is about to play:

1. look up `play_item.vod_id` in the session cache
2. if present, use the cached `VodItem`
3. otherwise call the resolver callback, then cache the returned `VodItem`
4. extract the actual playable URL from the resolved detail payload
5. update the target `PlayItem.url` with that resolved URL before loading mpv

This same flow applies to:

- the first clicked episode when opening the player
- `play_next()`
- `play_previous()`
- direct playlist clicks in the player
- automatic advance after natural playback completion

### Metadata Refresh

Resolved detail data should update the player's current metadata, not just the playback URL.

When the current item resolves successfully, update the session and player window with the resolved `VodItem` fields used by the existing metadata pane:

- `vod_name`
- `vod_pic`
- `vod_remarks`
- `type_name`
- `vod_content`
- `vod_year`
- `vod_area`
- `vod_lang`
- `vod_director`
- `vod_actor`
- `dbid`

This refresh should happen each time a newly resolved episode becomes current. If a cached detail entry exists, the metadata refresh should use that cached value without a new network call.

### Failure Behavior

If detail resolution fails while attempting to switch episodes:

- do not update `current_index`
- do not load the target item into mpv
- keep the player window open on the current episode
- append a concise error line to the playback log

There is no fallback to the folder-listing `vod_play_url` for failed episode switches.

If the failure happens on the initial clicked episode before the player opens, propagate the exception to the existing page/window open flow so the user sees the normal error dialog instead of an empty player.

## Testing Strategy

Add focused tests in `tests/test_browse_controller.py` for:

- folder-playlist items preserving `vod_id`
- `build_request_from_folder_item()` calling `get_detail()` for the clicked file
- initial folder-playback requests using the resolved playable URL instead of the folder-list URL
- resolved detail metadata overriding sparse folder-list metadata

Add focused tests in `tests/test_player_window_ui.py` for:

- moving to the next episode triggering detail resolution for that episode
- repeated navigation to the same episode using the in-memory cache instead of re-fetching detail
- failed next/previous episode resolution keeping the current index unchanged
- failed resolution logging an error and preventing a new load

Add focused tests in `tests/test_app.py` for:

- restore-from-folder mode continuing to use folder playback requests that can resolve items on demand

## Implementation Order

1. Add failing browse-controller tests covering clicked-item detail resolution and `vod_id` preservation.
2. Extend the request and playlist models to carry per-item `vod_id` and resolved clicked-item metadata.
3. Add failing player-window tests for on-demand per-episode resolution, cache reuse, and failure behavior.
4. Extend the player session/controller boundary with a detail resolver and in-memory cache.
5. Update `PlayerWindow` to resolve items before loading them and to keep episode navigation stable on failure.
6. Run full regression tests for browse, app, and player flows.
