# Windows Onefile Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change Windows packaging to publish a single `atv-player.exe` while keeping Linux and macOS packaging unchanged.

**Architecture:** Keep `build.py` as the single source of truth for platform-specific PyInstaller behavior. Encode the Windows onefile mode in `BuildTarget`, update the workflow to upload a `.exe` directly instead of zipping a Windows bundle, and lock the contract in `tests/test_build.py`.

**Tech Stack:** Python 3.12, PyInstaller, pytest, GitHub Actions, uv

---

### Task 1: Teach `build.py` that Windows is a onefile target

**Files:**
- Modify: `tests/test_build.py`
- Modify: `build.py`

- [ ] **Step 1: Write the failing tests**

Update `tests/test_build.py` with these assertions:

```python
def test_build_archive_name_uses_normalized_architecture() -> None:
    assert build.build_archive_name("linux", "x86_64") == "atv-player-linux-x64.tar.gz"
    assert build.build_archive_name("Darwin", "aarch64") == "atv-player-macos-arm64.zip"
    assert build.build_archive_name("windows", "AMD64") == "atv-player-windows-x64.exe"


def test_build_target_windows_uses_onefile_mode() -> None:
    target = build.build_target("windows")

    assert target.platform_id == "windows"
    assert target.archive_ext == "exe"
    assert target.data_separator == ";"
    assert target.windowed is True
    assert target.onefile is True


def test_build_pyinstaller_command_collects_icons_and_libmpv(monkeypatch, tmp_path) -> None:
    libmpv = tmp_path / "libmpv.so.2"
    libmpv.write_bytes(b"so")
    monkeypatch.setattr(build, "find_libmpv", lambda target_platform: [(libmpv, ".")])

    command = build.build_pyinstaller_command("linux")

    assert command[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "--noconfirm" in command
    assert "--clean" in command
    assert "--onedir" in command
    assert "--onefile" not in command
    assert "--paths" in command
    assert "src" in command
    assert "--windowed" not in command
    assert "--add-data" in command
    assert f"{build.ICONS_DIR}:atv_player/icons" in command
    assert "--add-binary" in command
    assert f"{libmpv}:." in command
    assert command[-1] == str(build.ENTRYPOINT)


def test_build_pyinstaller_command_windows_uses_onefile_mode(monkeypatch, tmp_path) -> None:
    dll_path = tmp_path / "libmpv-2.dll"
    dll_path.write_bytes(b"dll")
    monkeypatch.setattr(build, "find_libmpv", lambda target_platform: [(dll_path, ".")])

    command = build.build_pyinstaller_command("windows")

    assert "--windowed" in command
    assert "--onefile" in command
    assert "--onedir" not in command
    assert f"{build.ICONS_DIR};atv_player/icons" in command


def test_bundle_path_for_target_matches_platform_output() -> None:
    assert build.bundle_path_for_target("linux") == build.DIST_DIR / "atv-player"
    assert build.bundle_path_for_target("macos") == build.DIST_DIR / "atv-player.app"
    assert build.bundle_path_for_target("windows") == build.DIST_DIR / "atv-player.exe"
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `uv run pytest tests/test_build.py -q`

Expected: FAIL because the current Windows target still returns `.zip`, uses `--onedir`, and resolves the Windows bundle path to `dist/atv-player`.

- [ ] **Step 3: Implement the minimal Windows onefile behavior in `build.py`**

Update `build.py` so `BuildTarget` carries the bundle mode and Windows becomes onefile-only:

```python
@dataclass(frozen=True, slots=True)
class BuildTarget:
    platform_id: str
    archive_ext: str
    data_separator: str
    windowed: bool
    onefile: bool


def build_target(value: str | None) -> BuildTarget:
    platform_id = normalize_target_platform(value)
    if platform_id == "linux":
        return BuildTarget(
            platform_id="linux",
            archive_ext="tar.gz",
            data_separator=":",
            windowed=False,
            onefile=False,
        )
    if platform_id == "macos":
        return BuildTarget(
            platform_id="macos",
            archive_ext="zip",
            data_separator=":",
            windowed=True,
            onefile=False,
        )
    return BuildTarget(
        platform_id="windows",
        archive_ext="exe",
        data_separator=";",
        windowed=True,
        onefile=True,
    )


def build_pyinstaller_command(target_platform: str) -> list[str]:
    target = build_target(target_platform)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile" if target.onefile else "--onedir",
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
    if target.platform_id == "windows":
        return DIST_DIR / f"{APP_NAME}.exe"
    return DIST_DIR / APP_NAME
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `uv run pytest tests/test_build.py -q`

Expected: PASS for the updated Windows naming, command-construction, and output-path assertions.

- [ ] **Step 5: Commit the build-script change**

Run:

```bash
git add build.py tests/test_build.py
git commit -m "feat: package windows as onefile exe"
```

Expected: a commit containing only the Windows onefile build-script and test changes.

### Task 2: Update GitHub Actions to upload the `.exe` directly

**Files:**
- Modify: `tests/test_build.py`
- Modify: `.github/workflows/build.yml`

- [ ] **Step 1: Write the failing workflow assertions**

Append these assertions to `tests/test_build.py`:

```python
def test_github_workflow_uploads_windows_exe_and_releases_it() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert "atv-player-windows-x64.exe" not in workflow
    assert "Copy-Item $env:BUNDLE_PATH \"dist\\$env:ARCHIVE_NAME\" -Force" in workflow
    assert "path: dist/${{ env.ARCHIVE_NAME }}" in workflow
    assert '-name "*.exe"' in workflow
    assert 'Compress-Archive -Path "$env:BUNDLE_PATH\\*"' not in workflow
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `uv run pytest tests/test_build.py -q`

Expected: FAIL because the current workflow still compresses the Windows bundle directory and the release asset collection does not include `.exe`.

- [ ] **Step 3: Implement the minimal workflow change**

Update `.github/workflows/build.yml` so the Windows build copies the generated executable to the normalized artifact name and the release job collects `.exe` files:

```yaml
      - name: Archive Windows bundle
        if: matrix.platform == 'windows'
        shell: pwsh
        run: |
          if (!(Test-Path $env:BUNDLE_PATH)) {
            throw "Missing bundle path: $env:BUNDLE_PATH"
          }
          Copy-Item $env:BUNDLE_PATH "dist\$env:ARCHIVE_NAME" -Force
```

Replace the release asset collection with:

```yaml
      - name: Prepare release assets
        run: |
          mkdir -p release-assets
          find artifacts -type f \( -name "*.tar.gz" -o -name "*.zip" -o -name "*.exe" \) -exec cp {} release-assets/ \;
          ls -la release-assets
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `uv run pytest tests/test_build.py -q`

Expected: PASS with the workflow assertions updated for direct `.exe` publishing.

- [ ] **Step 5: Commit the workflow change**

Run:

```bash
git add .github/workflows/build.yml tests/test_build.py
git commit -m "ci: publish windows exe artifact"
```

Expected: a commit containing the direct Windows `.exe` upload and release-asset collection changes.

### Task 3: Run the full packaging regression checks

**Files:**
- Modify: none
- Test: `tests/test_build.py`

- [ ] **Step 1: Run the full build packaging test module**

Run: `uv run pytest tests/test_build.py -v`

Expected: PASS for all build-script and workflow packaging assertions, including the new Windows onefile coverage.

- [ ] **Step 2: Inspect the diff for scope control**

Run: `git diff -- build.py tests/test_build.py .github/workflows/build.yml`

Expected: only the Windows onefile packaging changes are present; Linux and macOS behavior should remain unchanged except where shared assertions intentionally mention `.exe` support in release collection.

- [ ] **Step 3: Create the final combined commit**

Run:

```bash
git add build.py tests/test_build.py .github/workflows/build.yml docs/superpowers/specs/2026-04-15-windows-onefile-packaging-design.md docs/superpowers/plans/2026-04-15-windows-onefile-packaging.md
git commit -m "feat: publish windows onefile executable"
```

Expected: a single final commit containing the approved design doc, implementation plan, tested build-script updates, and workflow changes.
