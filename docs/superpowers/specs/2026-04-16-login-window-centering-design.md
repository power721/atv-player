# Login Window Centering Design

## Summary

Adjust the login window so it opens at a larger default size and keeps the login form centered both horizontally and vertically.

This change only affects the login window layout and initial sizing. It does not change login behavior, field order, validation, or post-login flow.

## Scope

### Login Window Layout

- Keep the existing field order:
  - backend URL
  - username
  - password
  - login button
- Wrap the current form and button in an inner `content_container` widget.
- Center that container inside the window with stretch on all four sides so the login area stays visually centered on larger windows.
- Give the container a bounded target width so input fields have more room without becoming excessively wide.

### Window Sizing

- Increase the login window default size from content-driven sizing to an explicit larger initial size.
- Keep the window resizable.
- Avoid enforcing a fullscreen-style layout; this should still behave like a compact standalone login window.

## Architecture

Follow the same page-boundary centering pattern already used elsewhere in the UI:

- create a dedicated `content_container` on `LoginWindow`
- move the existing form layout and login button into that container
- use an outer layout with horizontal and vertical stretch to center the container
- set an explicit initial window size in `LoginWindow`

This keeps the change local to the login window and avoids touching controller logic.

## Testing

Add UI coverage that verifies:

- `LoginWindow` exposes a centered `content_container`
- the default window size is larger than the previous content-only layout
- on a wide and tall window, the container center aligns with the window center within a small tolerance
- existing login submission behavior remains unchanged
