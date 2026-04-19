# Spider Plugin Multiple Playlists Design

## Summary

Change spider-plugin detail playback from one flattened playlist into multiple explicit playlists grouped by route.

The player should expose those route groups directly so the user can switch lines without losing the episode structure inside each line. Existing single-playlist sources must keep working without behavior changes.

## Goals

- Preserve spider-plugin `vod_play_from` groups as separate playlists instead of flattening them into one list.
- Let the player switch between grouped playlists for spider-plugin playback.
- Keep existing controllers that only produce one playlist compatible with the new request/session model.
- Keep the change scoped to request/session/player wiring plus spider-plugin playlist construction.

## Non-Goals

- Redesigning the player sidebar beyond adding a route selector.
- Changing the persisted playback-history payload format.
- Adding cross-route resume matching rules.
- Changing Emby, Jellyfin, browse, Telegram, or live playback semantics beyond single-group compatibility.
- Introducing a generic playlist-group abstraction across unrelated data models outside the player request/session flow.

## Current Behavior

`SpiderPluginController._build_playlist()` parses `vod_play_from` and `vod_play_url`, but it appends every route's episodes into one flat `list[PlayItem]`.

To preserve route context, each generated item prefixes its title with `线路名 | 选集名`, and the item's `play_source` stores the original route. This makes the data technically distinguishable, but the player still treats everything as one continuous list:

- one index space
- one visible episode list
- next/previous navigation crosses route boundaries
- no direct UI to switch routes while keeping per-route episode numbering

## Design

### Spider Playlist Construction

`SpiderPluginController._build_playlist()` should return `list[list[PlayItem]]`, where each inner list represents one route from `vod_play_from`.

For each route group:

- preserve the route name separately from episode titles
- generate item titles from the chunk title only, without prefixing `线路名 |`
- keep `play_source` equal to the route name for deferred `playerContent()` resolution
- reset `PlayItem.index` inside each group so the first episode in every route starts at `0`

If route names are missing, blank, or fewer than `vod_play_url` groups, fallback names should be generated as `线路 1`, `线路 2`, and so on.

Empty route groups should be skipped instead of producing empty playlists.

### Request And Session Model

Extend the player request/session flow to carry both:

- `playlist`: the currently active playlist, kept for compatibility with existing logic
- `playlists`: all available grouped playlists
- `playlist_index`: the currently active playlist group index

Compatibility rules:

- request producers that only know about one playlist can omit `playlists`; the model should treat their existing `playlist` as a single grouped playlist
- player/session creation should normalize to at least one playlist group
- existing consumers that read `session.playlist` continue to work against the active group

This keeps the majority of playback logic unchanged while making group switching explicit.

### Player UI And Behavior

`PlayerWindow` should add one route-selection control above the episode list.

Behavior:

- hide or disable the control when only one playlist group exists
- populate it with route names derived from the active group's `play_source`, falling back to `线路 N`
- when the user switches routes, replace the visible episode list with that group's items
- selecting a new route should keep the current row if it exists in the target group; otherwise clamp to the last available row; if the target group is empty, no playback starts
- once the target row is chosen, start playback from the selected group's item using the same play-item loading flow as today

Next/previous navigation should remain within the active playlist group. It must not automatically jump into another route.

### History And Resume Semantics

This feature should not change the existing history payload schema.

Resume behavior remains scoped to the active playlist group:

- `PlayerController.create_session()` resolves the start index against the active `playlist`
- spider-plugin requests still default to the first available route and first episode unless history within that active group says otherwise
- no new cross-route mapping is added for `episode_url`, `episode`, or route names

This is intentionally conservative. It preserves current history compatibility and avoids making route switching change how old history rows are interpreted.

### Source Compatibility

Spider-plugin requests should populate:

- `playlists` with one entry per route
- `playlist` with the initially selected route
- `playlist_index` with `0`

Other request builders should keep producing their current `playlist`. The model/session layer should normalize that into a single grouped playlist automatically so no controller-specific UI branching is required.

## Error Handling

- If spider detail parsing yields no playable items across all groups, keep raising the existing "没有可播放的项目" error.
- If a selected route cannot resolve its current item URL, keep using the existing item-resolution failure path and playback log behavior.
- If the route selector is changed while an async item resolution is in flight, stale completion results should remain ignored by the existing request-id guards.
- If a route name is missing, show a deterministic fallback label instead of an empty entry.

## Testing Strategy

Add or update tests to cover:

- spider-plugin playlist building returns multiple route-group playlists
- route-group item titles no longer include the route prefix
- spider-plugin request construction exposes grouped playlists plus the active playlist
- player-session creation normalizes single-playlist requests into one grouped playlist
- player window renders and switches grouped playlists correctly
- next/previous playback remains bounded to the active group

## Risks And Mitigations

- Risk: introducing grouped playlists breaks existing callers that only set `playlist`.
  Mitigation: make request/session normalization accept legacy single-playlist inputs transparently.

- Risk: route switching interferes with async play-item resolution.
  Mitigation: keep all route-switch playback on the existing `_play_item_request_id` invalidation path.

- Risk: history appears inconsistent when the same episode number exists across multiple routes.
  Mitigation: explicitly keep resume semantics limited to the active route for this feature.
