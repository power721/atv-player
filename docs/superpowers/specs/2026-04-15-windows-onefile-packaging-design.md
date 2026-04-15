# Windows Onefile Packaging Design

## Context

The repository already ships cross-platform packaging through `build.py` and `.github/workflows/build.yml`.
Linux and macOS currently produce archived application bundles, and Windows currently produces a bundled directory that is zipped for distribution.

The new requirement is narrower than the original packaging work: Windows builds should publish a single `atv-player.exe` file instead of a zipped directory bundle.

## Goal

Change only the Windows packaging path so that GitHub Actions and local packaging produce a single distributable `atv-player.exe`.

## Non-Goals

- Changing Linux packaging format
- Changing macOS packaging format
- Adding an installer such as MSI or Inno Setup
- Eliminating PyInstaller's temporary extraction behavior at runtime
- Adding code signing or release metadata changes unrelated to artifact format

## Proposed Approach

### Packaging Mode

Keep Linux and macOS on `PyInstaller` `onedir`.
Switch Windows to `PyInstaller` `onefile`.

This is the smallest viable change because it keeps the existing packaging entrypoint, dependency installation, and artifact naming flow. It also keeps `libmpv` collection inside `build.py`, so the workflow still only needs to download the Windows runtime into the repository-local `mpv/` directory.

The resulting Windows binary will be a single `dist/atv-player.exe` file. At runtime, PyInstaller will still unpack embedded resources into a temporary directory before launching the application. That behavior is acceptable for this requirement.

### Build Script Changes

`build.py` should make Windows a first-class onefile target by encoding the bundle mode in `BuildTarget`.

Required behavior:

- Linux target keeps `archive_ext="tar.gz"` and directory output
- macOS target keeps `archive_ext="zip"` and `.app` output
- Windows target switches to `archive_ext="exe"` and file output
- `build_pyinstaller_command()` uses `--onefile` for Windows and `--onedir` elsewhere
- `bundle_path_for_target()` returns `dist/atv-player.exe` for Windows

The Windows target must continue to include the `libmpv` DLL with `--add-binary`, so the executable remains self-contained from the user's perspective.

### Workflow Changes

The workflow should stop zipping the Windows bundle.

Required behavior:

- keep the current matrix build structure
- keep the existing Windows `libmpv-2.dll` download step
- keep `uv run python build.py windows` as the build command
- resolve `ARCHIVE_NAME` as `atv-player-windows-<arch>.exe`
- verify that the built Windows file exists
- copy or rename `dist/atv-player.exe` to `dist/$ARCHIVE_NAME`
- upload that `.exe` directly as the Windows artifact

The release job must also collect `.exe` files in addition to `.tar.gz` and `.zip` files so tag builds publish the Windows executable as a release asset.

### Testing

Add focused tests around the packaging contract instead of trying to run PyInstaller in unit tests.

Tests should cover:

- Windows target metadata encodes onefile output and `.exe` artifact naming
- Windows `build_pyinstaller_command()` uses `--onefile` and does not use `--onedir`
- Windows bundle path resolves to `dist/atv-player.exe`
- workflow assertions reflect direct Windows `.exe` upload and release collection

## Risks and Mitigations

- Risk: Windows startup becomes slower because onefile extraction happens on launch.
  Mitigation: accept this as a trade-off for single-file distribution and keep Linux/macOS unchanged.
- Risk: workflow release logic still filters only archive formats and misses the `.exe`.
  Mitigation: add a test that asserts `.exe` release asset collection.
- Risk: Windows build logic accidentally changes other platforms.
  Mitigation: keep target-specific behavior centralized in `BuildTarget` and preserve existing Linux/macOS tests.
