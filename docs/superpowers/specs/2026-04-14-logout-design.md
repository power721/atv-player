# Logout Design

## Summary

Add a local-only logout flow to the desktop player so an authenticated user can explicitly leave the current session and return to the login window without clearing saved server and username defaults.

This design is intentionally narrow. It adds a logout entry point in the main window and reuses the existing application coordinator logout path.

## Goals

- Provide a visible logout action in the main window.
- Clear the stored `token` and `vod_token` when the user logs out.
- Return the user to the login window immediately after logout.
- Preserve non-auth configuration such as `base_url`, `username`, last path, and playback restore state.
- Cover the new behavior with focused UI and coordinator tests.

## Non-Goals

- Call the backend logout API.
- Add logout entry points to the player window.
- Change how unauthorized responses are handled.
- Clear saved browsing or playback state during logout.
- Introduce a new menu bar or global application shortcut for logout.

## Scope

Primary implementation lives in `src/atv_player/ui/main_window.py`.

Coordinator integration remains in `src/atv_player/app.py`.

Primary verification lives in `tests/test_app.py`.

## Design

### Main Window Logout Entry

Add a dedicated `退出登录` button to the main window chrome. The button should be explicit and always available while the main window is visible.

This change should avoid introducing a new menu structure. A direct button is the smallest UI addition that matches the current application structure and is easy to test.

When clicked, the button emits the existing `logout_requested` signal on `MainWindow`.

### Logout Flow Ownership

`MainWindow` remains responsible only for surfacing the user intent to log out.

`AppCoordinator` remains responsible for session teardown and view switching:

- call `repo.clear_token()`
- show a new login window
- close and discard the current main window

This keeps session lifecycle logic centralized in one place and avoids duplicating logout behavior in the UI layer.

### Persisted State Semantics

Logout clears only authentication state:

- `token`
- `vod_token`

Logout does not clear:

- `base_url`
- `username`
- `last_path`
- `last_active_window`
- playback restore fields
- saved window geometry

This preserves convenience defaults for the next login and stays consistent with the existing `SettingsRepository.clear_token()` behavior.

### Player Window Interaction

This change does not add a logout control to the player window.

If the user is currently in the player window, they can return to the main window through the existing flow and log out there. Extending logout into additional windows is intentionally deferred to keep this change minimal.

## Error Handling

This logout flow is local-only and does not depend on a network request.

No new error dialog is needed for the happy path. The only required behavior is deterministic local state clearing and navigation back to the login window.

Existing unauthorized handling remains unchanged and continues to use the same coordinator-owned logout path.

## Testing Strategy

Add or update focused tests in `tests/test_app.py` to cover:

- the main window exposing a `退出登录` button
- clicking the button emitting `logout_requested`
- the app coordinator handling logout by clearing stored auth tokens
- the app coordinator showing the login window after logout

The tests should stay narrow and avoid expanding into backend logout semantics because that is out of scope for this change.

## Implementation Order

1. Add a failing UI test for the main window logout button and signal emission.
2. Add a failing coordinator test for logout token clearing and login-window routing.
3. Implement the main window logout control.
4. Verify the existing coordinator logout handler satisfies the new tests, or make the smallest required adjustment.
