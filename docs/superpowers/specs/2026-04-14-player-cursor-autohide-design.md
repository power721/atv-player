# Player Cursor Autohide Design

## Summary

When playback is active and the mouse stays inside the video area, the player should hide the mouse cursor after a 3 second delay. Any mouse movement inside the video area should immediately show the cursor again and restart the hide timer.

This design is intentionally narrow. It applies only to cursor visibility behavior inside the player video widget and its focused UI tests.

## Goals

- Hide the mouse cursor after a delay while playback is active and the pointer remains inside the video area.
- Show the cursor immediately when the mouse moves inside the video area.
- Show the cursor immediately when playback pauses.
- Show the cursor immediately when the mouse leaves the video area.
- Keep the implementation localized to the player window and avoid changing controller interfaces.

## Non-Goals

- Add overlay playback controls in fullscreen mode.
- Change playback state persistence or session restore behavior.
- Hide the cursor outside the video area.
- Introduce global cursor management for the entire application window.

## Scope

Primary implementation lives in `src/atv_player/ui/player_window.py`.

Primary verification lives in `tests/test_player_window_ui.py`.

## Design

### Ownership

`PlayerWindow` owns the cursor autohide state because the required rules depend on playback state (`is_playing`) and on the video widget's pointer enter, leave, and move lifecycle. `MpvWidget` remains a thin playback surface and does not need playback-state-aware cursor policy.

### Event Handling

Install an event filter on the video widget and enable mouse tracking on that widget so move events arrive without pressing buttons.

Track one narrow piece of UI state:

- whether the pointer is currently inside the video area

Use a single-shot `QTimer` owned by `PlayerWindow` for the hide delay.

Behavior:

- On mouse enter while playing, start or restart the hide timer and keep the cursor visible.
- On mouse move inside the video area, show the cursor immediately and restart the hide timer if playback is active.
- On mouse leave, stop the timer and restore the normal cursor.
- When the timer fires, hide the cursor only if playback is still active and the pointer is still inside the video area.
- When playback pauses or the player is being hidden/closed, stop the timer and restore the normal cursor.

### Cursor Application

Apply cursor changes only to the video widget:

- visible state: `Qt.ArrowCursor`
- hidden state: `Qt.BlankCursor`

Using explicit cursor shapes keeps the behavior deterministic in tests and avoids coupling cursor visibility to unrelated widgets.

## Testing Strategy

Add focused UI tests in `tests/test_player_window_ui.py` to cover:

- moving the mouse in the video area while playing makes the cursor visible and starts the delayed hide timer
- the timer firing while the pointer stays in the video area hides the cursor
- pausing playback restores the cursor and stops pending autohide
- leaving the video area restores the cursor and stops pending autohide

## Implementation Order

1. Add focused failing UI tests for video-area cursor autohide.
2. Add minimal `PlayerWindow` timer and event-filter support.
3. Wire playback pause paths to restore the cursor immediately.
4. Run focused player window UI tests.
