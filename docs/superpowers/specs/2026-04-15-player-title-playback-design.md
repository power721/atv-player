# Player Title Playback Design

## Summary

Update the player window title to reflect the current video while playback is active.

When the player is playing, the window title should show `影片名 - 当前播放项`. When playback is paused, or when the player is no longer in an active playback state, the title should revert to the application title `alist-tvbox 播放器`.

This behavior is local to the player window UI. It does not require controller, model, or API changes.

## Goals

- Show the active video title in the player window title bar during playback.
- Revert to the default application title when playback is paused.
- Keep the title behavior consistent when opening a session, toggling playback, switching episodes, and returning to the main window.
- Keep the change localized to `PlayerWindow` and focused UI tests.

## Non-Goals

- Change the visible in-window metadata panel.
- Add persistent title state to config or playback history.
- Introduce controller-level title management.
- Change the player title format outside the agreed `影片名 - 当前播放项` pattern.

## Current Behavior

`PlayerWindow` sets a fixed window title of `alist-tvbox 播放器` during initialization.

Playback state is already tracked locally in `PlayerWindow` through `self.is_playing`, `self.session`, and `self.current_index`. The same class already handles opening sessions, pause/resume toggles, playlist item changes, and returning to the main window.

## Proposed Design

### Title Source

Use the currently loaded player session as the source of truth:

- movie or series name from `session.vod.vod_name`
- current episode or play item title from `session.playlist[current_index].title`

When both values are available and playback is active, compose the window title as:

`<vod_name> - <play_item_title>`

If either value is empty, prefer the non-empty value. If no active title can be derived, fall back to `alist-tvbox 播放器`.

### Title Ownership

Keep title formatting and updates inside `PlayerWindow`.

This is a pure UI concern tied to local widget state. Keeping the behavior in the window avoids leaking presentation rules into `PlayerController`, which does not own Qt window lifecycle or pause/play UI state.

### Update Rules

Refresh the window title through one internal helper so the rule is defined in one place.

The helper should apply the following logic:

- if there is no active session, use `alist-tvbox 播放器`
- if `is_playing` is `False`, use `alist-tvbox 播放器`
- if `is_playing` is `True`, use the current video title derived from session data

### Update Triggers

Recompute the title when any event changes the effective playback title or whether that title should be shown:

- after opening a session
- after toggling play or pause
- after switching to a different playlist item
- after playback naturally advances to the next item
- before or during returning to the main window

This keeps the title aligned with the current item and avoids stale episode names after navigation.

## Error Handling

- If playback fails while opening or switching items, keep the fallback application title rather than leaving a stale item title behind.
- Missing `vod_name` or play item title should not raise; the title helper should fall back safely.
- Returning to main should always restore the application title even if the video pause call raises.

## Testing

Add focused UI coverage in `tests/test_player_window_ui.py` for:

- opening a playing session sets the window title to `影片名 - 当前播放项`
- toggling playback to paused restores the title to `alist-tvbox 播放器`
- switching to another playlist item while playing updates the title to the new item
- opening a session paused keeps the application title instead of the video title

## Implementation Notes

- Define a small title helper and avoid duplicating `setWindowTitle()` calls across playback methods.
- Keep the change inside `src/atv_player/ui/player_window.py` unless tests require minor fixture updates.
- Prefer minimal test doubles that assert title changes through existing player window flows.
