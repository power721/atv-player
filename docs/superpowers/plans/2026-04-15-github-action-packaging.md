# GitHub Action Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a repository-owned cross-platform packaging flow that builds Linux, macOS, and Windows bundles in GitHub Actions and publishes them to GitHub Releases on version tags.

**Architecture:** Keep packaging logic in a root-level `build.py` so local builds and CI use the same command builder, target normalization, asset inclusion, and `libmpv` collection rules. Keep `.github/workflows/build.yml` thin: install platform prerequisites, run `build.py`, archive the generated bundle, upload artifacts on every build, and publish them only when a `v*` tag is pushed.

**Tech Stack:** Python 3.12, uv, PyInstaller, PySide6, python-mpv, pytest, GitHub Actions

---

## File Structure

- `build.py`
  - Central build entrypoint for target normalization, architecture naming, PyInstaller command construction, `libmpv` lookup, and bundle-path helpers reused by CI.
- `tests/test_build.py`
  - Pure-function coverage for `build.py` plus workflow-shape assertions so CI behavior is locked in by tests.
- `pyproject.toml`
  - Packaging-only dependency group for `pyinstaller`.
- `.github/workflows/build.yml`
  - Cross-platform build matrix and tag-only GitHub Release publishing.
- `README.md`
  - Local packaging commands and release behavior summary.

### Task 1: Add A Tested Cross-Platform `build.py`

**Files:**
- Create: `tests/test_build.py`
- Create: `build.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/test_build.py`

- [ ] **Step 1: Write the failing build-script tests**

Create `tests/test_build.py` with this content:

```python
from pathlib import Path
import sys

import pytest

import build


def test_normalize_target_platform_maps_runtime_names(monkeypatch) -> None:
    monkeypatch.setattr(build.platform, "system", lambda: "Darwin")

    assert build.normalize_target_platform("current") == "macos"
    assert build.normalize_target_platform("linux") == "linux"
    assert build.normalize_target_platform("win32") == "windows"


def test_build_archive_name_uses_normalized_architecture() -> None:
    assert build.build_archive_name("linux", "x86_64") == "atv-player-linux-x64.tar.gz"
    assert build.build_archive_name("Darwin", "aarch64") == "atv-player-macos-arm64.zip"
    assert build.build_archive_name("windows", "AMD64") == "atv-player-windows-x64.zip"


def test_data_mapping_uses_platform_specific_separator() -> None:
    source = Path("/tmp/icons")

    assert build.data_mapping(source, "atv_player/icons", "linux") == "/tmp/icons:atv_player/icons"
    assert build.data_mapping(source, "atv_player/icons", "windows") == r"/tmp/icons;atv_player/icons"


def test_find_libmpv_uses_explicit_windows_runtime_dir(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "mpv"
    runtime_dir.mkdir()
    dll_path = runtime_dir / "libmpv-2.dll"
    dll_path.write_bytes(b"dll")
    monkeypatch.setenv("ATV_MPV_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("PATH", "")

    assert build.find_libmpv("windows") == [(dll_path, ".")]


def test_build_pyinstaller_command_collects_icons_and_libmpv(monkeypatch, tmp_path) -> None:
    libmpv = tmp_path / "libmpv.so.2"
    libmpv.write_bytes(b"so")
    monkeypatch.setattr(build, "find_libmpv", lambda target_platform: [(libmpv, ".")])

    command = build.build_pyinstaller_command("linux")

    assert command[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "--noconfirm" in command
    assert "--clean" in command
    assert "--onedir" in command
    assert "--paths" in command
    assert "src" in command
    assert "--windowed" not in command
    assert "--add-data" in command
    assert f"{build.ICONS_DIR}:atv_player/icons" in command
    assert "--add-binary" in command
    assert f"{libmpv}:." in command
    assert command[-1] == str(build.ENTRYPOINT)


def test_build_pyinstaller_command_windows_uses_windowed_mode(monkeypatch, tmp_path) -> None:
    dll_path = tmp_path / "libmpv-2.dll"
    dll_path.write_bytes(b"dll")
    monkeypatch.setattr(build, "find_libmpv", lambda target_platform: [(dll_path, ".")])

    command = build.build_pyinstaller_command("windows")

    assert "--windowed" in command
    assert f"{build.ICONS_DIR};atv_player/icons" in command


def test_bundle_path_for_target_matches_platform_output() -> None:
    assert build.bundle_path_for_target("linux") == build.DIST_DIR / "atv-player"
    assert build.bundle_path_for_target("macos") == build.DIST_DIR / "atv-player.app"
    assert build.bundle_path_for_target("windows") == build.DIST_DIR / "atv-player"


def test_unknown_platform_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported target platform"):
        build.normalize_target_platform("plan9")
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
uv run pytest tests/test_build.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'build'` because the build entrypoint does not exist yet.

- [ ] **Step 3: Implement the minimal tested build entrypoint**

Add the packaging dependency group to `pyproject.toml`:

```toml
[dependency-groups]
dev = [
  "pytest>=8.3",
  "pytest-qt>=4.4",
]
package = [
  "pyinstaller>=6.16",
]
```

Refresh the lockfile so GitHub Actions can continue to use `uv sync --frozen`:

```bash
uv lock
```

Create `build.py` with this content:

```python
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "atv-player"
PROJECT_ROOT = Path(__file__).resolve().parent
ENTRYPOINT = PROJECT_ROOT / "src" / "atv_player" / "main.py"
ICONS_DIR = PROJECT_ROOT / "src" / "atv_player" / "icons"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"


@dataclass(frozen=True, slots=True)
class BuildTarget:
    platform_id: str
    archive_ext: str
    data_separator: str
    windowed: bool


def normalize_target_platform(value: str | None) -> str:
    normalized = (value or "current").strip().lower()
    if normalized == "current":
        normalized = platform.system().lower()
    mapping = {
        "linux": "linux",
        "darwin": "macos",
        "macos": "macos",
        "windows": "windows",
        "win32": "windows",
    }
    if normalized not in mapping:
        raise ValueError(f"Unsupported target platform: {value}")
    return mapping[normalized]


def normalize_arch(value: str | None = None) -> str:
    normalized = (value or platform.machine()).strip().lower()
    return {
        "x86_64": "x64",
        "amd64": "x64",
        "x64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(normalized, normalized)


def build_target(value: str | None) -> BuildTarget:
    platform_id = normalize_target_platform(value)
    if platform_id == "linux":
        return BuildTarget(platform_id="linux", archive_ext="tar.gz", data_separator=":", windowed=False)
    if platform_id == "macos":
        return BuildTarget(platform_id="macos", archive_ext="zip", data_separator=":", windowed=True)
    return BuildTarget(platform_id="windows", archive_ext="zip", data_separator=";", windowed=True)


def build_archive_name(target_platform: str, arch: str | None = None) -> str:
    target = build_target(target_platform)
    return f"{APP_NAME}-{target.platform_id}-{normalize_arch(arch)}.{target.archive_ext}"


def data_mapping(source: Path, destination: str, target_platform: str) -> str:
    return f"{source}{build_target(target_platform).data_separator}{destination}"


def find_libmpv(target_platform: str) -> list[tuple[Path, str]]:
    platform_id = normalize_target_platform(target_platform)

    if platform_id == "linux":
        search_patterns = [
            "/usr/lib/x86_64-linux-gnu/libmpv.so*",
            "/usr/lib64/libmpv.so*",
            "/usr/lib/libmpv.so*",
        ]
        for pattern in search_patterns:
            matches = sorted(Path("/").glob(pattern.lstrip("/")))
            if matches:
                return [(matches[0], ".")]
        raise FileNotFoundError("libmpv was not found on Linux")

    if platform_id == "macos":
        for candidate in (
            Path("/opt/homebrew/lib/libmpv.dylib"),
            Path("/usr/local/lib/libmpv.dylib"),
            Path("/opt/homebrew/lib/libmpv.2.dylib"),
            Path("/usr/local/lib/libmpv.2.dylib"),
        ):
            if candidate.exists():
                return [(candidate, ".")]
        raise FileNotFoundError("libmpv was not found on macOS")

    search_dirs: list[Path] = []
    runtime_dir = os.environ.get("ATV_MPV_RUNTIME_DIR")
    if runtime_dir:
        search_dirs.append(Path(runtime_dir))
    search_dirs.extend(Path(part) for part in os.environ.get("PATH", "").split(os.pathsep) if part)

    for directory in search_dirs:
        for dll_name in ("libmpv-2.dll", "mpv-2.dll", "mpv.dll"):
            dll_path = directory / dll_name
            if dll_path.exists():
                return [(dll_path, ".")]
    raise FileNotFoundError("libmpv was not found on Windows")


def build_pyinstaller_command(target_platform: str) -> list[str]:
    target = build_target(target_platform)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        APP_NAME,
        "--paths",
        "src",
        "--add-data",
        data_mapping(ICONS_DIR, "atv_player/icons", target.platform_id),
        "--add-binary",
        data_mapping(find_libmpv(target.platform_id)[0][0], ".", target.platform_id),
    ]
    if target.windowed:
        command.append("--windowed")
    command.append(str(ENTRYPOINT))
    return command


def bundle_path_for_target(target_platform: str) -> Path:
    target = build_target(target_platform)
    if target.platform_id == "macos":
        return DIST_DIR / f"{APP_NAME}.app"
    return DIST_DIR / APP_NAME


def build(target_platform: str) -> Path:
    if not ENTRYPOINT.exists():
        raise FileNotFoundError(f"Missing entrypoint: {ENTRYPOINT}")
    if not ICONS_DIR.exists():
        raise FileNotFoundError(f"Missing icons directory: {ICONS_DIR}")

    command = build_pyinstaller_command(target_platform)
    subprocess.run(command, check=True, cwd=PROJECT_ROOT)

    bundle_path = bundle_path_for_target(target_platform)
    if not bundle_path.exists():
        raise FileNotFoundError(f"Missing build output: {bundle_path}")
    return bundle_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("platform", nargs="?", default="current")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle_path = build(args.platform)
    print(bundle_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run:

```bash
uv sync --group dev --group package
uv run pytest tests/test_build.py -q
```

Expected: PASS for all `tests/test_build.py` cases.

- [ ] **Step 5: Run a local build smoke test**

Run:

```bash
uv run python build.py current
```

Expected:

- Linux: prints a path ending in `dist/atv-player`
- macOS: prints a path ending in `dist/atv-player.app`
- Windows: prints a path ending in `dist/atv-player`

- [ ] **Step 6: Commit the build entrypoint**

Run:

```bash
git add pyproject.toml uv.lock build.py tests/test_build.py
git commit -m "feat: add packaging build script"
```

Expected: a commit containing the new `build.py`, the packaging dependency group, the refreshed lockfile, and focused build-script tests.

### Task 2: Add The GitHub Actions Matrix Build And Release Workflow

**Files:**
- Modify: `tests/test_build.py`
- Create: `.github/workflows/build.yml`
- Test: `tests/test_build.py`

- [ ] **Step 1: Extend tests with workflow expectations**

Append these workflow assertions to `tests/test_build.py`:

```python
def test_github_workflow_builds_all_target_platforms() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert "ubuntu-latest" in workflow
    assert "macos-latest" in workflow
    assert "windows-latest" in workflow
    assert "uv run python build.py ${{ matrix.platform }}" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_github_workflow_releases_only_for_version_tags() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert "tags:" in workflow
    assert "- 'v*'" in workflow
    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "softprops/action-gh-release@v2" in workflow
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run pytest tests/test_build.py -q
```

Expected: FAIL with `FileNotFoundError` because `.github/workflows/build.yml` does not exist yet.

- [ ] **Step 3: Create the workflow**

Create `.github/workflows/build.yml` with this content:

```yaml
name: Build Packages

on:
  push:
    tags:
      - 'v*'
  pull_request:
    branches: [main, master]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  build:
    name: Build ${{ matrix.platform }}
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        include:
          - os: ubuntu-latest
            platform: linux
          - os: macos-latest
            platform: macos
          - os: windows-latest
            platform: windows

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install Linux system dependencies
        if: matrix.platform == 'linux'
        run: |
          sudo apt-get update
          sudo apt-get install -y libmpv-dev libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0

      - name: Install macOS system dependencies
        if: matrix.platform == 'macos'
        run: brew install mpv

      - name: Install Windows system dependencies
        if: matrix.platform == 'windows'
        run: choco install mpv -y

      - name: Sync dependencies
        run: uv sync --frozen --group dev --group package

      - name: Build application
        run: uv run python build.py ${{ matrix.platform }}

      - name: Resolve archive name (POSIX)
        if: matrix.platform != 'windows'
        run: |
          ARCHIVE_NAME=$(uv run python -c "import build; print(build.build_archive_name('${{ matrix.platform }}'))")
          BUNDLE_PATH=$(uv run python -c "import build; print(build.bundle_path_for_target('${{ matrix.platform }}'))")
          echo "ARCHIVE_NAME=$ARCHIVE_NAME" >> "$GITHUB_ENV"
          echo "BUNDLE_PATH=$BUNDLE_PATH" >> "$GITHUB_ENV"

      - name: Resolve archive name (Windows)
        if: matrix.platform == 'windows'
        shell: pwsh
        run: |
          $archiveName = uv run python -c "import build; print(build.build_archive_name('${{ matrix.platform }}'))"
          $bundlePath = uv run python -c "import build; print(build.bundle_path_for_target('${{ matrix.platform }}'))"
          "ARCHIVE_NAME=$archiveName" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
          "BUNDLE_PATH=$bundlePath" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append

      - name: Archive Linux bundle
        if: matrix.platform == 'linux'
        run: |
          test -d "$BUNDLE_PATH"
          tar -C dist -czf "dist/$ARCHIVE_NAME" atv-player

      - name: Archive macOS bundle
        if: matrix.platform == 'macos'
        run: |
          test -d "$BUNDLE_PATH"
          ditto -c -k --sequesterRsrc --keepParent "$BUNDLE_PATH" "dist/$ARCHIVE_NAME"

      - name: Archive Windows bundle
        if: matrix.platform == 'windows'
        shell: pwsh
        run: |
          if (!(Test-Path $env:BUNDLE_PATH)) {
            throw "Missing bundle path: $env:BUNDLE_PATH"
          }
          Compress-Archive -Path "$env:BUNDLE_PATH\*" -DestinationPath "dist\$env:ARCHIVE_NAME" -Force

      - name: Upload build artifact
        uses: actions/upload-artifact@v4
        with:
          name: ${{ matrix.platform }}-bundle
          path: dist/${{ env.ARCHIVE_NAME }}
          retention-days: 7

  release:
    name: Publish Release
    runs-on: ubuntu-latest
    needs: build
    if: startsWith(github.ref, 'refs/tags/v')
    permissions:
      contents: write

    steps:
      - name: Download build artifacts
        uses: actions/download-artifact@v4
        with:
          path: artifacts

      - name: Prepare release assets
        run: |
          mkdir -p release-assets
          find artifacts -type f \( -name "*.tar.gz" -o -name "*.zip" \) -exec cp {} release-assets/ \;
          ls -la release-assets

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          files: release-assets/*
          generate_release_notes: true
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

- [ ] **Step 4: Run the workflow tests to verify they pass**

Run:

```bash
uv run pytest tests/test_build.py -q
```

Expected: PASS, including the workflow assertions for platform coverage and tag-only release publishing.

- [ ] **Step 5: Commit the workflow**

Run:

```bash
git add .github/workflows/build.yml tests/test_build.py
git commit -m "ci: add packaging workflow"
```

Expected: a commit containing the matrix build workflow and its lock-in tests.

### Task 3: Document Local Packaging And CI Release Behavior

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a packaging section to the README**

Append this section to `README.md`:

````markdown
## Packaging

Local packaging uses the same `build.py` entrypoint as GitHub Actions.

Install the packaging dependencies:

```bash
uv sync --group dev --group package
```

Build the current platform bundle:

```bash
uv run python build.py current
```

GitHub Actions builds Linux, macOS, and Windows artifacts for pull requests and manual runs. Pushing a tag that starts with `v` also creates a GitHub Release and uploads the generated archives.
````

- [ ] **Step 2: Verify the README mentions the packaging entrypoint**

Run:

```bash
rg -n "^## Packaging|uv run python build.py current|GitHub Release" README.md
```

Expected:

```text
README.md:3:## Packaging
README.md:11:uv run python build.py current
README.md:15:GitHub Actions builds Linux, macOS, and Windows artifacts for pull requests and manual runs. Pushing a tag that starts with `v` also creates a GitHub Release and uploads the generated archives.
```

- [ ] **Step 3: Run the focused verification set**

Run:

```bash
uv run pytest tests/test_build.py -q
```

Expected: PASS

- [ ] **Step 4: Commit the documentation update**

Run:

```bash
git add README.md
git commit -m "docs: add packaging instructions"
```

Expected: a commit containing the local packaging and release usage notes.
