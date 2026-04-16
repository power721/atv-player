# AppImage Qt Fallback Design

## Goal

Make the Linux AppImage start reliably on systems where the packaged Qt runtime picks the X11 EGL integration path and crashes during window creation.

## Current Behavior

The AppImage launcher in `packaging/linux/AppRun` immediately executes the packaged binary with no runtime guards. The packaged Qt runtime includes both `libqxcb-egl-integration.so` and `libqxcb-glx-integration.so`. On some Linux systems, Qt selects the EGL-backed xcb integration and fails during Mesa device setup, producing `libEGL`, `MESA`, `ZINK`, and `BadWindow` errors before the UI can settle.

## Requirements

- Keep the fix scoped to the Linux AppImage runtime.
- Do not change normal `uv run` development startup behavior.
- Do not change Windows or macOS packaging behavior.
- Prefer startup reliability over accelerated rendering for the AppImage when the system graphics stack is fragile.

## Proposed Design

Update `packaging/linux/AppRun` so the AppImage exports conservative Qt/X11 environment variables before launching the packaged binary. The launcher should:

- resolve the AppImage directory as it does today
- set the Qt platform plugin path from the packaged runtime
- keep the platform on `xcb` unless the user has already overridden it
- force Qt to use the software OpenGL backend unless the user has already overridden it
- force the xcb GL integration to `none` unless the user has already overridden it

Using `QT_XCB_GL_INTEGRATION=none` prevents Qt from selecting the failing xcb EGL integration path. Using `QT_OPENGL=software` gives Qt a stable software-rendered fallback for widget rendering inside the AppImage. Keeping these settings in `AppRun` confines the behavior change to the packaged Linux artifact.

## Testing

Add a packaging regression test in `tests/test_build.py` that verifies the Linux AppDir assembly preserves the launcher contents needed for the fallback behavior. This keeps the test focused on repository-owned packaging assets rather than trying to boot a full GUI inside pytest.

## Risks

- Some AppImage users may lose GPU-accelerated Qt rendering.
- This does not change mpv's own rendering backend directly; it only stabilizes Qt/X11 startup.

## Non-Goals

- Changing runtime behavior for non-AppImage launches
- Reworking mpv embedding
- Adding dynamic graphics backend detection logic
