# Player Controls Polish Design

## Summary

Polish the player window playback controls so repeated actions are easier to distinguish, button states are more legible, and fullscreen transitions preserve the prior window state.

This design is intentionally narrow. It applies only to the player window controls and their focused UI tests.

## Goals

- Make seek buttons visually distinct from previous/next episode buttons.
- Make the mute button visually reflect muted versus unmuted state.
- Add a refresh button that restarts the current item.
- Show shortcuts in playback control tooltips.
- Use a pointing-hand cursor for playback control buttons.
- Add a small amount of padding to the playback control area.
- Preserve a maximized window when exiting fullscreen if the window was maximized before entering fullscreen.

## Non-Goals

- Redesign the overall player window layout.
- Change existing shortcut bindings.
- Add fullscreen overlay controls.
- Change controller interfaces or add new controller methods.
- Refactor the player window into a metadata-driven button factory beyond what is needed for these requirements.

## Scope

Primary implementation lives in `src/atv_player/ui/player_window.py`.

Primary verification lives in `tests/test_player_window_ui.py`.

If the existing icon set does not already provide suitable seek and refresh icons, add narrowly scoped SVG files under `src/atv_player/icons/`.

## Design

### Button Identity

Keep episode navigation and seek navigation as separate concepts:

- `prev_button` and `next_button` continue to mean previous episode and next episode.
- `backward_button` and `forward_button` continue to mean relative seek backward and forward.

The seek buttons must use different icons from the episode navigation buttons so users can distinguish them at a glance. The exact icon artwork can be minimal, but it must be clearly different from the existing previous/next episode icons.

### Mute State Presentation

The mute button becomes a two-state icon button:

- When audio is not muted, show an "audio on" style icon.
- When audio is muted, show a muted icon.

The button icon should update immediately when the user toggles mute from the button or keyboard shortcut. The state tracked for the icon is local UI state in `PlayerWindow`; no controller changes are required.

### Refresh / Replay Action

Add a dedicated refresh icon button to the playback control group.

Behavior:

- Reload the current playlist item.
- Restart playback from the beginning of the current item.
- Keep the current playlist selection unchanged.
- Re-apply the current speed and current volume through the existing load path.
- Do not navigate to another item and do not close the player window.

This action is a replay/reload of the current entry, not a full session rebuild.

### Tooltips And Cursor

Every playback control button in the bottom playback area should expose a tooltip that includes the shortcut where one exists.

Examples of intent:

- Play/pause includes `Space`
- Mute includes `M`
- Fullscreen includes `Enter`
- Previous/next episode includes `PgUp` / `PgDn`
- Seek backward/forward includes `Left` / `Right`

Buttons without an assigned keyboard shortcut may keep the action-only label.

The playback control buttons should also use `Qt.PointingHandCursor` on hover so mouse interaction is visually obvious.

The scope of this cursor requirement is the playback controls in the player bottom area, including the new refresh button and existing sidebar toggle buttons only if they are created through the same helper path. It does not need to change unrelated widgets such as sliders or combo boxes.

### Playback Control Padding

Add a small amount of padding around the bottom playback control area so the controls do not sit flush against the container edges.

This should be done by adjusting layout contents margins on the bottom control container rather than changing the main window shell structure.

### Fullscreen Exit Restores Prior Window State

Track the window state immediately before entering fullscreen.

Required behavior:

- If the player enters fullscreen from a maximized window, exiting fullscreen restores the maximized window.
- If the player enters fullscreen from a normal window, exiting fullscreen restores the prior normal window size and position.

Fullscreen exit should not degrade a maximized window into a normal window.

This behavior should apply to both button-driven fullscreen toggles and Escape-driven fullscreen exit.

## Testing Strategy

Add or update focused UI tests in `tests/test_player_window_ui.py` to cover:

- seek buttons using different icons from previous/next episode buttons
- mute icon changing when mute is toggled
- refresh button existence and replay behavior
- control button tooltips containing shortcut hints
- playback control buttons using a pointing-hand cursor
- bottom control area margins including the new padding
- fullscreen exit restoring maximized state after leaving fullscreen

Each user-requested change is implemented as its own TDD cycle and committed separately in the requested order.

## Implementation Order

1. Distinguish seek icons from previous/next episode icons.
2. Distinguish muted and unmuted icons.
3. Add refresh button and replay behavior.
4. Add shortcut text to tooltips and pointing-hand cursors for playback buttons.
5. Add padding to the playback control area.
6. Preserve maximized window state when exiting fullscreen.
