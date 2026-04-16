# CI Artifact Version Naming Design

## Context

The repository already centralizes release artifact naming in `build.py`, and `.github/workflows/build.yml` asks that script for the filenames it should upload and publish.

Current artifact names include only application name, target platform, and architecture. The new requirement is to include a version token in the packaged filename, with the version coming from the Git tag in release builds and from a placeholder value in non-tag CI runs.

## Goal

Make CI-generated artifact filenames include a version segment while keeping naming rules centralized in `build.py`.

## Non-Goals

- Changing the application's runtime version display or metadata
- Deriving version numbers from `pyproject.toml` for CI artifact naming
- Changing bundle formats, release triggers, or platform build behavior
- Requiring local `build.py` users to have Git metadata available

## Proposed Approach

### Version Source

Artifact naming should accept an explicit release-version input.

Required precedence:

- Use an explicitly provided version string when one is passed to `build.py` naming helpers
- In GitHub Actions tag builds, pass the pushed Git tag name without the leading `v`
- In non-tag CI runs such as `pull_request` and `workflow_dispatch`, pass a fixed placeholder version string

The placeholder version should be `dev`.

This keeps version resolution outside of `build.py`'s local environment concerns while still making `build.py` the single source of truth for the final filename shape.

### Filename Format

Release artifact names should change from:

- `atv-player-linux-x64.AppImage`
- `atv-player-macos-arm64.zip`
- `atv-player-windows-x64.exe`

to:

- `atv-player-<version>-linux-x64.AppImage`
- `atv-player-<version>-macos-arm64.zip`
- `atv-player-<version>-windows-x64.exe`

The version token should appear immediately after the app name so all platforms share one predictable pattern.

### Build Script Changes

`build.py` should expose a small helper for resolving the effective artifact version from an explicit input.

Required behavior:

- add a `DEFAULT_ARTIFACT_VERSION = "dev"` constant
- add a helper that returns the provided version when it is non-empty after trimming, otherwise returns `dev`
- update `build_archive_name()` to accept an optional version argument and include it in the filename
- update `release_artifact_path_for_target()` to accept the same version argument and delegate to `build_archive_name()`

No other build behavior should change. Bundle output paths such as `dist/atv-player.exe` and `dist/atv-player.app` should remain unchanged; only the archived artifact names used by CI and release publishing should gain the version segment.

### Workflow Changes

The workflow should resolve one environment variable for artifact naming before calling the Python helpers that emit archive names.

Required behavior:

- on tag pushes like `refs/tags/v1.2.3`, use `1.2.3`
- on non-tag runs, use `dev`
- pass that value into every `build.build_archive_name(...)` and `build.release_artifact_path_for_target(...)` call used by the workflow

The workflow should continue to upload and release the same files as before, just under the new versioned filenames.

### Testing

Add focused unit tests around the naming contract instead of trying to test GitHub context parsing end to end.

Tests should cover:

- explicit version values are preserved in archive names
- missing or blank version values fall back to `dev`
- release artifact paths include the same versioned archive names
- workflow assertions verify tag builds strip the leading `v` and non-tag runs use `dev`

## Risks and Mitigations

- Risk: `build.py` and the workflow resolve version strings differently.
  Mitigation: keep string normalization in a single `build.py` helper and have the workflow pass only raw CI context values.
- Risk: tag names accidentally retain the leading `v` in filenames.
  Mitigation: add a workflow test that looks for the exact `${GITHUB_REF_NAME#v}` trimming behavior.
- Risk: local callers get unexpected filenames without passing a version.
  Mitigation: make the fallback explicit and stable as `dev`.
