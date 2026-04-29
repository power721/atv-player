# Spider Plugin Placeholder Player Design

When a user clicks a spider-plugin card, the app should open the player window immediately instead of waiting for `detailContent()` or `playerContent()`. The player window should use the card's existing `vod_name` and `vod_pic` as placeholder metadata, then hydrate the real playlist and playback state asynchronously.

## Goals

- Open the player window immediately after a spider-plugin card click.
- Keep the window open if plugin detail or playback resolution fails.
- Show loading and failure state in the player window log instead of a modal error.
- Resolve plugin playback URLs asynchronously after the window is already visible.

## Scope

- Spider-plugin card click flow in `MainWindow`
- Spider-plugin request construction in `SpiderPluginController`
- Placeholder and async playback-loader behavior in `PlayerWindow`
- Supporting request/session metadata in models and player controller

## Design

### Immediate placeholder open

`MainWindow` creates a placeholder `OpenPlayerRequest` from the clicked `VodItem`, with:

- no playlist
- `use_local_history=False`
- `source_kind="plugin"`
- `source_mode="detail"`
- `initial_log_message="正在加载详情..."`
- `is_placeholder=True`

That request is converted to a lightweight player session synchronously and applied immediately so the existing `PlayerWindow` instance can render placeholder metadata without waiting for background work.

### Async plugin request hydration

After the placeholder session is visible, `MainWindow` starts a background plugin request that still uses the normal `controller.build_request(vod_id)` path. On success, the resulting request replaces the placeholder session through the existing `open_player()` path. On failure, the player window remains open and receives a log message like `详情加载失败: ...`.

### Async playback URL loading

Spider-plugin requests now mark their `playback_loader` as asynchronous. `PlayerWindow` detects this flag and, when the selected `PlayItem` has no resolved URL yet, it:

- logs `正在加载播放地址: <title>`
- resolves `playback_loader` off the UI thread
- applies any replacement playlist on the main thread
- starts playback only after a URL is ready

If async playback resolution fails or produces no playable URL, the player restores the previous selection and appends a playback failure log entry.

## Failure handling

- `detailContent()` failure: keep placeholder window open, append `详情加载失败: ...`
- `playerContent()` failure: keep hydrated player open, append `播放失败: ...`
- missing resolved URL: append `播放失败: 没有可用的播放地址: ...`

## Tests

- plugin card opens placeholder player immediately and later hydrates real session
- plugin detail failure keeps placeholder player open and logs the error
- player window can open a placeholder session with an empty playlist
- async playback loader does not block `open_session()` and starts playback after background resolution
