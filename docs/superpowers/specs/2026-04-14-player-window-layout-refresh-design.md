# Player Window Layout Refresh Design

## Summary

Refresh the player window control layout to improve readability in windowed modes and reduce visual noise in fullscreen mode.

The update applies to all non-fullscreen states:

- Move playback controls into a centered control group.
- Move volume controls into a right-aligned group.
- Add a volume icon before the volume slider.
- Limit the visible width of the volume slider.
- Show the current playback time on the left side of the progress row.
- Show the media duration on the right side of the progress row.

In fullscreen mode:

- Hide the entire bottom area, including the progress row, time labels, and control buttons.
- Hide the playlist panel.
- Hide the details panel.

## Goals

- Make the playback controls visually centered instead of spreading every button across one row.
- Make the volume controls easier to identify and less space-hungry.
- Expose elapsed time and total duration directly around the progress bar.
- Ensure fullscreen mode focuses entirely on video playback.
- Preserve existing playback behavior, shortcuts, and sidebar toggle state outside fullscreen.

## Non-Goals

- Add overlay controls in fullscreen mode.
- Change playback shortcuts or playback semantics.
- Redesign the right sidebar beyond fullscreen hiding behavior.
- Introduce custom styling or theming outside layout-level adjustments.

## Layout Design

### Bottom Area In Non-Fullscreen Modes

The bottom area remains visible in normal and wide modes, but is split into two rows.

#### Progress Row

- Left: current playback time label.
- Center: horizontal progress slider.
- Right: total duration label.

The labels show `00:00` when timing data is unavailable. If the media duration becomes available later, the right label updates when progress sync runs.

#### Control Row

The control row is split into three regions:

- Left spacer.
- Centered playback control group.
- Right-aligned auxiliary group.

The centered playback control group contains:

- previous
- play/pause
- next
- backward seek
- forward seek
- wide toggle
- fullscreen toggle
- speed selector

The right-aligned auxiliary group contains:

- mute button shown as the volume icon
- horizontal volume slider with a fixed maximum width

The playlist toggle and details toggle remain in the sidebar action row, unchanged in non-fullscreen modes.

### Fullscreen Behavior

When the window enters fullscreen mode:

- hide the bottom area widget
- hide the playlist widget
- hide the details widget
- hide the sidebar action row
- keep the video area visible

When the window exits fullscreen mode:

- restore the bottom area widget
- restore sidebar visibility based on the existing toggle button checked state
- preserve wide-mode behavior and previously checked sidebar toggles

Fullscreen mode should not mutate the user's toggle choices. It only temporarily overrides visibility.

## State Management

Add a dedicated visibility refresh path so the window can derive actual visibility from:

- fullscreen state
- wide mode state
- playlist toggle checked state
- details toggle checked state

This avoids scattering `show()` and `hide()` decisions across unrelated handlers.

## Testing

Add or update UI tests to cover:

- progress row exposes current-time and duration labels
- volume controls include the mute icon button before the slider
- volume slider width is capped
- fullscreen hides the bottom area, playlist, and details
- exiting fullscreen restores bottom area and sidebar contents according to toggle state

## Implementation Notes

- Keep the work localized to `PlayerWindow` and its UI tests.
- Use a small formatting helper to render seconds as `MM:SS` or `HH:MM:SS` when needed.
- Continue syncing the progress slider and new time labels from the existing timer.
