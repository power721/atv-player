# App Icon Design

## Context

The repository already contains a root-level `app.svg`, but the Qt application does not currently set an application-wide icon. Existing playback control icons live under `src/atv_player/icons/`, so the app icon should follow the same package-local resource layout instead of staying at the repository root.

## Goal

When the app is started from source, the Qt application should use a packaged `app.svg` so top-level windows inherit the same icon in the title bar and taskbar.

## Non-Goals

- Packaging desktop launcher metadata or installer assets
- Setting window icons separately on each top-level widget
- Redesigning the existing SVG artwork

## Proposed Approach

### Resource Layout

Move `app.svg` from the repository root to `src/atv_player/icons/app.svg`.

This keeps all SVG assets in one package-owned directory and avoids runtime dependence on the current working directory or repository layout.

### Application Initialization

In `src/atv_player/app.py`, add a small helper that resolves the package-local app icon path from the module location.

`build_application()` should continue to construct `QApplication`, set the application name, and additionally call `app.setWindowIcon(QIcon(str(app_icon_path)))` before any windows are shown.

The icon is set at the application level so `LoginWindow`, `MainWindow`, and `PlayerWindow` inherit the same icon automatically without duplicating logic in each window class.

### Packaging

Update the build configuration so SVG files under `src/atv_player/icons/` are included in the built wheel.

This prevents a working source checkout from masking a missing runtime asset after installation.

### Error Handling

Do not add fallback search paths or silent recovery logic.

The icon path is deterministic, and tests should guard the expected resource location. If the asset is missing during development, a straightforward missing icon is preferable to hidden path heuristics.

## Testing

Add a test in `tests/test_app.py` covering `build_application()`.

The test should assert:

- the returned `QApplication` has a non-null window icon
- the returned repository instance is still created as before

This keeps the behavior check focused on the new application-level responsibility without coupling the test to platform-specific icon rendering details.

## Risks and Mitigations

- Risk: the SVG is not packaged into wheel builds.
  Mitigation: explicitly include the icon directory in build configuration and keep a test around `build_application()` so resource wiring stays visible.
- Risk: future windows bypass the application icon by setting their own icon.
  Mitigation: keep icon ownership centralized in `build_application()` and avoid window-level icon setup unless a future requirement explicitly needs a different icon.
