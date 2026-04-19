# Spider Plugin Drive Playlist Replacement Design

## Summary

Allow spider-plugin drive-share routes such as `quark` and `baidu` to expand into a real episode playlist and replace the clicked drive route's original placeholder items.

When a plugin detail contains a route like `查看$https://pan.quark.cn/...`, clicking that item should resolve the drive link through `/tg-search/{token}?ac=gui&id={link}`, read the backend-returned `items`, and swap the current route's playlist from a single `查看` placeholder into the flattened episode list returned by the backend.

## Goals

- Detect supported drive-share links inside spider-plugin route playlists.
- Resolve the clicked drive link lazily through the existing Telegram-search backend detail endpoint.
- Replace the clicked route's original playlist entries with the backend-expanded episode list.
- Preserve the backend `items` order exactly as returned.
- Keep ordinary direct-media routes unchanged.

## Non-Goals

- Preloading every drive route during detail-page load.
- Grouping expanded drive items by folder such as `S1` and `S2`.
- Replacing all original routes in the request once one drive route is clicked.
- Changing Telegram, browse, Emby, Jellyfin, or live controllers.
- Adding new UI for manual drive-link input.

## Current Behavior

Spider-plugin detail parsing currently produces one playlist per `vod_play_from` route.

For a drive route such as:

- `vod_play_from`: `quark`
- `vod_play_url`: `查看$https://pan.quark.cn/s/14a405a9bb0d`

the playlist contains one placeholder `PlayItem`:

- title: `查看`
- vod_id: raw drive-share link
- url: empty

At playback time, the current implementation can resolve that drive link through `/tg-search/{token}?ac=gui&id={link}`, but it only extracts one playable URL from the backend detail and assigns it to the clicked item. The player never receives the full expanded episode list, so the placeholder route is not replaced.

## Design

### Resolution Trigger

Drive-route expansion remains lazy.

The app should not expand drive routes when building the initial detail request. Expansion should happen only when the user clicks a placeholder drive item in that route.

This keeps detail loading cheap and avoids unnecessary backend requests for routes the user never opens.

### Backend Detail Mapping

The backend response may contain:

- top-level detail metadata such as `vod_id`, `vod_name`, `path`
- `items`, where each item already contains title, path, size, and a playable `url`

For drive-route replacement, the controller should prioritize `detail.items` over `vod_play_url` parsing. The flattened replacement playlist should be built directly from `items` in the returned order.

Each replacement `PlayItem` should map:

- `title` from backend item title/name
- `url` from backend item `url`
- `path` from backend item `path`
- `size` from backend item `size`
- `index` from the replacement list order
- `vod_id` from backend item `vod_id` when present, otherwise empty
- `play_source` inherited from the clicked route name such as `quark` or `baidu`

### Playlist Replacement Semantics

When a clicked plugin item is a supported drive link:

1. Resolve the link through the injected drive-detail loader.
2. Read the backend detail payload.
3. Build a flattened playlist from backend `items`.
4. Replace the current route playlist inside the active request/session flow with that flattened playlist.
5. Start playback from index `0` of the replacement playlist.

Replacement scope:

- replace only the clicked route's playlist group
- keep other original routes unchanged
- remove the placeholder `查看` item from the replaced route

For the concrete example:

- original routes: `播放源 1`, `播放源 2`, `播放源 3`, `播放源 4`, `quark`, `baidu`
- original `quark` route: one placeholder item `查看`

after clicking the `quark` item:

- `播放源 1-4` remain unchanged
- `baidu` remains unchanged
- `quark` becomes the flattened episode list returned by backend `items`

### Controller Boundary

The spider-plugin controller already owns:

- initial route playlist construction
- deferred play-item resolution

This feature should stay in that controller layer.

The controller should extend drive-link resolution so it can return either:

- a single playable URL for normal deferred playback
- a full replacement playlist for drive-route expansion

The player-facing request/session model should then adopt the replacement list for the active route without requiring unrelated controllers to know about drive-link semantics.

### Player Behavior

Once a drive route expands:

- the visible playlist for the active route should refresh to the replacement episode list
- playback should begin from the first replacement item
- next/previous navigation should stay within that replacement list
- switching away to another route and back should continue to show the replaced route contents for the lifetime of the current player session

No persistence across future app launches is required for this feature.

### Error Handling

If drive-detail resolution fails:

- missing `list[0]` should raise the existing "没有可播放的项目" style error
- empty backend `items` and empty fallback playlist should raise the same error
- backend `items` with no playable `url` values should also raise the same error

If the clicked route is not a supported drive link, keep the current direct `playerContent()` resolution path.

## Testing Strategy

Add or update tests to cover:

- a clicked `quark` placeholder route expands into a flattened replacement playlist from backend `items`
- a clicked `baidu` placeholder route uses the same replacement behavior
- replacement order exactly matches backend `items` order
- the replacement route no longer contains the original `查看` placeholder
- ordinary media routes still keep their original playlist items and playback behavior
- the active route updates to the replacement list for the rest of the current session

## Risks And Mitigations

- Risk: replacing the route playlist in-place could desynchronize player state from request/session state.
  Mitigation: perform replacement through the same active playlist/group objects the player already renders.

- Risk: some backend drive details may only expose `vod_play_url` and no `items`.
  Mitigation: keep the existing fallback parsing path, but prefer `items` whenever present.

- Risk: expanded drive playlists may be much longer than the original placeholder route.
  Mitigation: reset playback to replacement index `0` and regenerate item indexes from the replacement list.
