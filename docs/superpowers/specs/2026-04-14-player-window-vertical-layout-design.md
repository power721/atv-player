# Player Window Vertical Layout Design

## Summary

Adjust the player window from a side-by-side shell to a vertical shell:

- The top region becomes the main content area.
- The bottom region becomes a dedicated playback control area.
- Inside the top region, keep the existing left-right split:
  - left: video player
  - right: sidebar
- Inside the sidebar, keep the existing vertical split:
  - top: playlist
  - bottom: details

This change makes the playback controls span the full window width while preserving the current playlist and details workflow.

## Goals

- Move the playback controls into a dedicated bottom area that sits below the main content.
- Preserve the current player, playlist, and details arrangement within the upper content region.
- Keep playlist and details toggle behavior unchanged outside fullscreen.
- Keep fullscreen behavior focused on video-only playback.
- Avoid broken layouts when users upgrade from the previous splitter structure.

## Non-Goals

- Redesign playback controls, icons, or shortcut semantics.
- Change playlist or details content.
- Introduce new persisted layout controls beyond what is required for a safe migration.
- Add fullscreen overlay controls.

## Layout Design

### Outer Structure

Replace the current top-level horizontal splitter with a vertical shell:

- top: main content container
- bottom: playback controls container

The playback controls container spans the full window width and is no longer nested only under the video pane.

### Top Region

The top region contains the current main content split:

- left pane: `MpvWidget`
- right pane: sidebar container

The left-right relationship remains resizable.

### Sidebar

The sidebar structure stays intact:

- top action row with the playlist and details toggle buttons
- vertical splitter containing:
  - playlist list widget
  - details text view

This keeps the current sidebar interaction model stable while only changing the outer shell.

### Bottom Region

The bottom region contains the existing playback controls:

- progress row with current time, progress slider, and duration
- control row with playback controls, view toggles, speed selector, mute, and volume slider

The bottom region remains hidden in fullscreen mode.

## Visibility And Interaction

Outside fullscreen:

- the bottom controls are visible
- the sidebar action row is visible
- playlist visibility follows the playlist toggle button
- details visibility follows the details toggle button

In fullscreen:

- hide the bottom controls
- hide the sidebar action row
- hide the playlist
- hide the details
- keep the video area visible

Exiting fullscreen restores the non-fullscreen visibility derived from the toggle buttons. Fullscreen does not mutate the stored toggle intent.

## State Persistence And Migration

The current configuration stores `player_main_splitter_state` for the old top-level horizontal splitter.

Because the new outer structure changes the splitter hierarchy, the old saved state should be treated as incompatible for the new outer layout. The implementation should:

- preserve use of the right sidebar vertical splitter
- avoid restoring stale top-level splitter bytes into the new structure
- fall back to sane default sizes when the saved state does not match the new layout

This prevents upgraded users from seeing collapsed or malformed panes after the layout change.

## Implementation Notes

- Keep the change localized to `PlayerWindow` and its focused UI tests.
- Prefer a small helper to build the top region container so the new outer structure is obvious in code.
- Keep the existing visibility refresh path centralized so fullscreen and sidebar toggles still derive visibility from one place.
- Save splitter state only for layouts that remain structurally compatible with the stored bytes.

## Testing

Update focused UI tests to cover:

- the outer player layout uses a vertical structure
- the upper content region still uses a horizontal split
- the sidebar still uses a vertical split
- the playback controls remain present and sized as before
- fullscreen still hides the bottom controls and sidebar content
- restoring from configuration does not break when prior splitter state exists
