# GitHub Action Packaging Design

## Context

The repository is currently a Linux-first `PySide6` desktop application without an automated packaging pipeline. The reference `music-player` repository already uses a repository-owned build entrypoint plus a GitHub Actions workflow to produce desktop bundles, but this repository does not yet have a matching build script, workflow, or release automation.

The requested scope is broader than the current runtime focus: GitHub Actions should build distributable artifacts for Linux, macOS, and Windows, and tag builds should automatically create a GitHub Release with those artifacts attached.

## Goal

Add a maintainable cross-platform packaging flow that:

- builds Linux, macOS, and Windows application bundles in GitHub Actions
- uploads build artifacts for pull requests and manual runs
- automatically creates a GitHub Release and uploads the built assets when a version tag is pushed
- keeps the packaging logic reproducible locally instead of embedding all platform logic directly inside the workflow

## Non-Goals

- Producing native installer formats in the first iteration, such as AppImage, `.dmg`, or Windows installers
- Cross-compiling one target platform from another runner
- Code signing, notarization, or store-distribution metadata
- Reworking runtime media dependencies beyond what is needed to produce packaged `PyInstaller` bundles

## Proposed Approach

### Packaging Architecture

Add a repository-level `build.py` as the single packaging entrypoint.

`build.py` will own:

- application metadata such as package name, entrypoint, and version
- platform-specific `PyInstaller` arguments
- packaging of runtime assets such as icons
- normalized artifact naming for each operating system

The GitHub Actions workflow will stay thin. It should install dependencies, invoke `uv run python build.py current`, compress the platform output into a release artifact, and publish the result.

This mirrors the reference repository's overall structure while staying narrower than its Linux-specific AppImage flow.

### Build Format

Use `PyInstaller` `onedir` bundles for all three platforms in the first version.

Expected packaged outputs should follow a normalized `<app>-<platform>-<arch>.<archive>` format, for example:

- `atv-player-linux-x64.tar.gz`
- `atv-player-macos-arm64.tar.gz`
- `atv-player-windows-x64.zip`

`build.py` should derive the architecture token from the runner platform and normalize it to values such as `x64` or `arm64`. The exact macOS archive format can be chosen based on simpler CI implementation, but the archive name should remain explicit about platform and architecture.

Using `onedir` everywhere keeps the first implementation consistent and reduces platform-specific packaging risk. Native installer formats can be added later without replacing the GitHub Actions structure.

### Dependency Strategy

Add a packaging dependency group in `pyproject.toml` for tools required only during packaging, primarily `pyinstaller`.

The workflow should continue to use `uv` so local builds and CI resolve dependencies in the same way. Each platform will build on its native GitHub runner:

- `ubuntu-latest`
- `macos-latest`
- `windows-latest`

The packaging logic should explicitly include package-local runtime assets needed by the app, especially `src/atv_player/icons/`, so a packaged bundle does not depend on source checkout layout.

### Workflow Design

Create `.github/workflows/build.yml` with these triggers:

- `pull_request`: run cross-platform build validation and upload artifacts only
- `workflow_dispatch`: allow manual build runs and upload artifacts only
- `push` on version tags matching `v*`: run cross-platform builds, upload artifacts, then publish a GitHub Release

The workflow should use a matrix `build` job across the three operating systems. Each matrix leg should:

- check out the repository
- install Python and `uv`
- sync dependencies including the packaging group
- invoke `build.py`
- compress the platform bundle into a single archive
- upload that archive as a workflow artifact

Add a separate `release` job that depends on `build` and runs only for tag pushes. That job should download all uploaded artifacts, create the GitHub Release, and upload each archive as a release asset.

### Release Behavior

Tag builds should be all-or-nothing.

If any platform build fails, the release job should not publish a partial release. This keeps tags from producing incomplete public artifacts and surfaces platform regressions before a release is published.

The release should derive its version from the pushed Git tag so the workflow and `build.py` share the same version string for artifact naming.

### Error Handling

Packaging logic should fail fast on missing inputs:

- unsupported platform argument
- missing entrypoint
- missing packaged assets
- missing generated archive after build

The workflow should also verify that the expected archive exists before artifact upload. That gives a clear failure mode when `PyInstaller` succeeds partially but the packaging step does not produce the expected bundle name.

### Testing

Add focused automated tests for `build.py` logic rather than trying to unit-test the full `PyInstaller` process.

Tests should cover:

- target platform normalization
- artifact naming per platform
- selection of `PyInstaller` arguments and output extension
- resource/data inclusion paths used by the packaging command

CI packaging itself is the integration test. The workflow build matrix should be the source of truth for validating that the application can actually be packaged on each runner.

## Risks and Mitigations

- Risk: platform-specific `PyInstaller` arguments drift or break silently.
  Mitigation: keep all packaging rules in `build.py` and add unit tests for command construction.
- Risk: packaged app misses runtime assets such as icons.
  Mitigation: make asset inclusion explicit in `build.py` and verify archive creation in CI.
- Risk: macOS or Windows output needs runner-specific archive handling.
  Mitigation: keep archive creation in workflow steps that are allowed to vary by platform while leaving packaging logic centralized.
- Risk: a future move to native installers causes workflow churn.
  Mitigation: keep the workflow responsible only for orchestration so a later packaging backend change is isolated mostly to `build.py` or helper scripts.
