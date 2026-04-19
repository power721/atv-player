# Player Loading Dialog Design

## Summary

Add a modal loading dialog to the player when playback cannot start immediately because the app is still resolving a plugin play item or preparing a remote `m3u8` URL.

The dialog is intentionally minimal: it blocks repeated interaction during asynchronous playback preparation, shows a short loading message, and closes automatically when playback starts or the preparation flow fails.

## Goals

- Show clear feedback when plugin playback resolution is slow.
- Cover both existing asynchronous preparation stages in `PlayerWindow`:
  - play-item detail resolution
  - `m3u8` ad-filter preparation
- Keep the dialog visible across consecutive preparation stages without flicker.
- Preserve existing playback, logging, and request-invalidation behavior.
- Keep the change local to player-window UI state and tests.

## Non-Goals

- Adding a cancel button or cancellation flow for background tasks.
- Redesigning the player layout or replacing the existing log-based failure feedback.
- Introducing a reusable app-wide loading-dialog framework.
- Changing controller interfaces, plugin APIs, or threading architecture.
- Showing the dialog for immediate playback paths that already have a ready URL.

## Current Behavior

`PlayerWindow` already runs playback preparation asynchronously:

- `_start_play_item_resolution()` resolves deferred play-item detail in a background thread.
- `_start_playback_prepare()` queues `m3u8` ad-filter preparation onto the controller task queue.

While these steps are running, the UI does not show a dedicated waiting state. If plugin parsing or remote playlist preparation is slow, the user only sees delayed playback and may assume the click did not register.

The existing request-id guards are already correct for stale async completions:

- `_play_item_request_id`
- `_playback_prepare_request_id`

Those guards should remain the authority for deciding whether a completion still belongs to the current playback attempt.

## Proposed Design

### Dialog Type

Add a lightweight custom modal `QDialog` owned by `PlayerWindow`.

Characteristics:

- modal and window-local
- no cancel button
- no progress percentage
- short static loading text
- safe to show repeatedly without creating duplicate dialogs

The dialog should be implemented close to `PlayerWindow`, not as a new shared UI abstraction. This keeps the scope narrow and avoids premature reuse.

### User-Facing Behavior

Show the dialog when playback enters an asynchronous preparation stage:

- when a play item must be resolved before it has a playable URL
- when a resolved or direct URL still requires `m3u8` preparation

Dialog text:

- default text: `正在解析播放地址，请稍候...`

The same dialog instance stays open if the flow transitions directly from detail resolution into `m3u8` preparation. This avoids a distracting close-open flicker during a single playback attempt.

Close the dialog when:

- playback is about to call the video widget load path
- detail resolution fails
- playback preparation fails
- the current async request becomes invalid because the user started another playback attempt
- the player window closes

### State Management

`PlayerWindow` should maintain explicit loading-dialog helpers:

- create/show dialog
- close and clear dialog reference
- optionally keep a small counter or stage token if needed, but prefer a simpler request-aware approach

The preferred implementation is to treat the dialog as a presentation of “current playback preparation is in flight”, not as a generic task tracker.

Rules:

- showing the dialog is idempotent
- closing the dialog is idempotent
- stale async callbacks must not close a dialog that belongs to a newer request

The existing request-id guards already handle most of this. The new dialog helpers should align with those guards instead of adding a second competing validity model.

### Integration Points

Display the dialog at the start of:

- `_start_play_item_resolution()`
- `_start_playback_prepare()`

Hide the dialog in:

- `_handle_play_item_resolve_succeeded()` only if playback will continue immediately without entering another async stage
- `_handle_play_item_resolve_failed()`
- `_handle_playback_prepare_succeeded()`
- `_handle_playback_prepare_failed()`
- `_invalidate_play_item_resolution()`
- `closeEvent()` or equivalent shutdown path

When detail resolution succeeds and immediately transitions into `_start_playback_prepare()`, the dialog must remain visible.

When direct playback does not need either async stage, no dialog should appear.

## Error Handling

- If dialog creation fails unexpectedly, playback should continue with existing behavior rather than blocking playback startup.
- Failure during detail resolution or playlist preparation should still use the current log messages and index restoration behavior.
- If a stale callback arrives after the user switched episodes, the callback should continue to be ignored and should not affect dialog visibility for the newer request.
- Closing the player window while background work is still running should safely dismiss the dialog and ignore later callbacks through the existing window-validity checks.

## Testing Strategy

Add or update `PlayerWindow` UI tests to cover:

- the dialog appears during deferred detail resolution and closes after playback starts
- the dialog appears during `m3u8` preparation and closes after playback starts
- the dialog closes when `m3u8` preparation fails and playback falls back to the original URL
- the dialog remains visible across resolution followed immediately by preparation
- a stale completion from an older request does not incorrectly close or reuse the dialog for the current request

Tests should keep using the existing fake controller and fake video patterns in `tests/test_player_window_ui.py`.

## Risks And Mitigations

- Risk: dialog lifecycle becomes inconsistent across overlapping async requests.
  Mitigation: reuse the current request-id guards and keep dialog show/close helpers idempotent.

- Risk: the dialog closes briefly between resolution and `m3u8` preparation, causing flicker.
  Mitigation: only close after a final transition to playback or terminal failure, not between chained async stages.

- Risk: the new UI state leaks on window close.
  Mitigation: explicitly dismiss the dialog during player-window teardown and rely on existing validity guards for late callbacks.
