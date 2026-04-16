# Help Dialog Shortcuts Design

## Summary

Add a keyboard help dialog that opens with `F1` in both the main window and the player window. The dialog should show the shortcut list relevant to the current window, reuse a shared UI module, and avoid opening duplicate dialog instances when `F1` is pressed repeatedly.

## Goals

- Open a help dialog from `F1` in `MainWindow`.
- Open the same style help dialog from `F1` in `PlayerWindow`.
- Show only the shortcut list relevant to the active window.
- Keep shortcut metadata in one shared place so the help content and bindings stay aligned.
- Reuse an existing dialog instance per window instead of stacking multiple copies.

## Non-Goals

- Adding menu-bar help actions.
- Introducing network-backed help content.
- Building a global shortcut registry across the entire application.
- Changing the behavior of existing shortcuts beyond adding `F1`.

## Scope

Primary implementation lives in:

- `src/atv_player/ui/help_dialog.py`
- `src/atv_player/ui/main_window.py`
- `src/atv_player/ui/player_window.py`

Primary verification lives in:

- `tests/test_app.py`
- `tests/test_player_window_ui.py`

## Design

### User Interaction

Pressing `F1` inside the main window opens a modal help dialog for the main window. Pressing `F1` inside the player window opens a modal help dialog for the player window.

The dialog title should use clear wording such as `快捷键帮助`. The body should render a readable list of shortcut keys and their descriptions for the current window only. Main-window shortcuts and player shortcuts should not be merged into one list because that would make the dialog noisier and less accurate for the current context.

If the dialog is already open for a window, pressing `F1` again should raise and focus the existing dialog instead of creating a second instance.

### Shared Help Module

Add a dedicated UI module, `src/atv_player/ui/help_dialog.py`, with two responsibilities:

- define shortcut entry data for each help context
- build and show the dialog from that data

The shared module should expose a simple interface that lets callers request help for a specific context such as `main_window` or `player_window`. The window classes should not be responsible for laying out the dialog contents directly.

### Shortcut Data Ownership

Shortcut entries should be declared statically in the shared help module. Each entry should include:

- displayed key text, such as `F1` or `Ctrl+P`
- a short Chinese description of the action

This keeps the displayed shortcut list local to the help UI and reduces duplication between `MainWindow` and `PlayerWindow`.

The initial main-window list should include the currently supported window-level shortcuts:

- `F1`
- `Ctrl+P`
- `Esc`
- the standard quit shortcut shown by Qt on the current platform

The initial player-window list should include:

- `F1`
- `Ctrl+P`
- `Esc`
- playback toggle
- previous and next item
- seek backward and forward
- mute
- fullscreen
- the standard quit shortcut shown by Qt on the current platform

The implementation should use descriptions that match the current behavior already present in each window.

### Window Integration

`MainWindow` should register an `F1` shortcut and route it to the shared help dialog entry point.

`PlayerWindow` should register an `F1` shortcut using the same shortcut context style already used for the other player-wide shortcuts. The handler should open the shared help dialog for the player context and must not interfere with existing playback shortcuts.

Each window should keep a reference to its current help dialog instance so repeated activation can reuse it.

### State and Error Handling

The help dialog must be pure local UI. It should not depend on any controller, playback session, or network state. Opening the dialog should work regardless of whether content has loaded or a video session is active.

If a dialog instance was previously created and later closed, the next `F1` press should create a fresh instance and replace the stale reference.

Because shortcut descriptions are static, there is no runtime fallback or partial-loading state to manage.

## Testing Strategy

Follow TDD for implementation.

Add focused tests in `tests/test_app.py` for:

- pressing `F1` in `MainWindow` opens a help dialog
- the main-window help dialog shows main-window shortcut entries such as `F1` and `Ctrl+P`
- repeated `F1` activation in `MainWindow` does not create duplicate dialog instances

Add focused tests in `tests/test_player_window_ui.py` for:

- pressing `F1` in `PlayerWindow` opens a help dialog
- the player help dialog shows player shortcut entries such as `F1`, seek keys, and playback keys
- repeated `F1` activation in `PlayerWindow` does not create duplicate dialog instances

These tests should verify visible help content, not only that a handler was called, so regressions in dialog wiring or rendered shortcut text are caught.

## Implementation Order

1. Add failing UI tests for `F1` in the main window.
2. Add failing UI tests for `F1` in the player window.
3. Implement the shared help dialog module and shortcut data.
4. Wire `F1` into `MainWindow` and `PlayerWindow`.
5. Run focused verification for the new tests and then the relevant broader suites.
