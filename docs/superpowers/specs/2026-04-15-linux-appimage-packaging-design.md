# Linux AppImage Packaging Design

## Context

The repository already has a cross-platform packaging flow driven by `build.py` and `.github/workflows/build.yml`.
Windows now publishes a single `.exe`, macOS publishes a `.zip`, and Linux still publishes a `tar.gz` archive built from a `PyInstaller` `onedir` bundle.

The new requirement is to replace the Linux `tar.gz` artifact with an AppImage while keeping the existing Windows and macOS packaging behavior unchanged.

## Goal

Publish Linux builds as a single `.AppImage` file instead of a `tar.gz` archive, while keeping local and CI packaging driven by the same `build.py` entrypoint.

## Non-Goals

- Changing the Windows `.exe` packaging flow
- Changing the macOS `.zip` packaging flow
- Replacing `PyInstaller` as the current packaging backend
- Producing multiple Linux artifact formats in the same build
- Adding code signing, desktop store metadata, or installer-specific behavior outside the AppImage requirements

## Proposed Approach

### Packaging Architecture

Keep the current Linux `PyInstaller --onedir` bundle as an internal build step.
Linux packaging should still first create `dist/atv-player/`, because that preserves the current resource collection, runtime layout, and `libmpv` bundling logic already implemented in `build.py`.

After the `onedir` bundle is built, `build.py` should run a Linux-specific post-processing step that assembles an `AppDir` and turns it into a final AppImage.

This keeps responsibilities separated:

- `PyInstaller` remains responsible for producing the runnable Linux application tree
- the AppImage step remains responsible for Linux distribution packaging
- the workflow remains responsible only for orchestration and artifact upload

### Artifact Model

Linux should stop treating the `onedir` bundle as the final distributable artifact.

Required behavior:

- `dist/atv-player/` remains the Linux intermediate bundle path
- the final Linux release artifact becomes `atv-player-linux-<arch>.AppImage`
- `build.py` should expose a distinct final artifact path for Linux so the workflow uploads the AppImage directly instead of creating a `tar.gz`

Windows and macOS keep their current artifact model:

- Windows publishes `atv-player-windows-<arch>.exe`
- macOS publishes `atv-player-macos-<arch>.zip`

### Linux AppImage Assembly

The Linux packaging flow should build an `AppDir` from the existing `dist/atv-player/` output and then invoke an AppImage packaging tool to create the final `.AppImage`.

The AppDir assembly should create the standard files needed for an AppImage:

- `AppRun`
- a `.desktop` launcher file
- an application icon
- the packaged application payload under the AppDir layout

The Linux runner in GitHub Actions should install the required AppImage packaging tool and use the same `uv run python build.py linux` command used for local packaging. AppImage-specific logic should not live only in the workflow.

### Workflow Changes

The GitHub Actions Linux leg should stop creating `tar.gz`.

Required behavior:

- keep `ubuntu-latest` as the Linux runner
- keep the existing Linux system package installation needed by the app runtime
- add installation for the AppImage packaging tool
- keep `uv run python build.py linux` as the Linux build entrypoint
- upload the generated `.AppImage` directly as the Linux artifact

The release job should also stop collecting Linux `*.tar.gz` assets and instead collect Linux `*.AppImage` assets. Windows `.exe` and macOS `.zip` collection should remain intact.

## Error Handling

Linux packaging should fail fast with explicit errors when:

- the AppImage packaging tool is unavailable
- the `PyInstaller` Linux bundle directory was not created
- required AppDir files such as `AppRun`, `.desktop`, or icon assets are missing
- the final `.AppImage` file was not produced

These failures should come from `build.py`, not from ambiguous workflow shell errors, so local and CI packaging produce the same diagnostics.

## Testing

Unit tests should stay focused on packaging behavior and command construction rather than trying to run a full AppImage build inside pytest.

Tests should cover:

- Linux target metadata now uses the `AppImage` extension
- Linux final artifact path resolves to `.AppImage`
- Linux `PyInstaller` command remains `--onedir`
- Linux AppImage helper functions compute the expected paths and commands
- workflow assertions reflect Linux direct AppImage upload and release collection of `*.AppImage`

The GitHub Actions Linux build remains the integration test for the actual AppImage generation path.

## Risks and Mitigations

- Risk: Linux packaging logic becomes split between `build.py` and workflow shell steps.
  Mitigation: keep AppImage generation inside `build.py` and let the workflow only install prerequisites and upload the final file.
- Risk: AppImage-specific files drift from the packaged app layout.
  Mitigation: generate the AppDir from the existing `dist/atv-player/` bundle and add focused tests for the AppDir assembly helpers.
- Risk: Linux release artifacts become inconsistent with the other platforms.
  Mitigation: keep the normalized `<app>-<platform>-<arch>.<ext>` naming contract and update tests to enforce Linux `.AppImage` output.
