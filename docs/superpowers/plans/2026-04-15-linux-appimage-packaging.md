# Linux AppImage Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Linux `tar.gz` release artifact with a single `.AppImage` while keeping Windows `.exe` and macOS `.zip` packaging unchanged.

**Architecture:** Keep the current Linux `PyInstaller --onedir` bundle as an intermediate build output in `dist/atv-player/`, then add Linux-only AppDir assembly and AppImage generation inside `build.py`. Add repository-owned Linux launcher assets under `packaging/linux/`, expose a final release-artifact path helper for all platforms, and update GitHub Actions to upload the Linux `.AppImage` directly.

**Tech Stack:** Python 3.12, PyInstaller, pytest, GitHub Actions, shell launcher assets, AppImage tool

---

### Task 1: Lock the Linux `.AppImage` contract in tests and `build.py`

**Files:**
- Modify: `tests/test_build.py`
- Modify: `build.py`

- [ ] **Step 1: Write the failing tests**

Update `tests/test_build.py` so Linux no longer expects `tar.gz` and so the final release-artifact path is explicit:

```python
def test_build_archive_name_uses_normalized_architecture() -> None:
    assert build.build_archive_name("linux", "x86_64") == "atv-player-linux-x64.AppImage"
    assert build.build_archive_name("Darwin", "aarch64") == "atv-player-macos-arm64.zip"
    assert build.build_archive_name("windows", "AMD64") == "atv-player-windows-x64.exe"


def test_build_target_linux_uses_appimage_release_format() -> None:
    target = build.build_target("linux")

    assert target.platform_id == "linux"
    assert target.archive_ext == "AppImage"
    assert target.data_separator == ":"
    assert target.windowed is False
    assert target.onefile is False


def test_bundle_path_for_target_matches_platform_output() -> None:
    assert build.bundle_path_for_target("linux") == build.DIST_DIR / "atv-player"
    assert build.bundle_path_for_target("macos") == build.DIST_DIR / "atv-player.app"
    assert build.bundle_path_for_target("windows") == build.DIST_DIR / "atv-player.exe"


def test_release_artifact_path_for_target_matches_platform_output() -> None:
    assert build.release_artifact_path_for_target("linux", "x86_64") == build.DIST_DIR / "atv-player-linux-x64.AppImage"
    assert build.release_artifact_path_for_target("macos", "arm64") == build.DIST_DIR / "atv-player-macos-arm64.zip"
    assert build.release_artifact_path_for_target("windows", "AMD64") == build.DIST_DIR / "atv-player-windows-x64.exe"
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `uv run pytest tests/test_build.py -q`

Expected: FAIL because Linux still reports `tar.gz` and `build.py` does not define `release_artifact_path_for_target()` yet.

- [ ] **Step 3: Implement the minimal Linux artifact contract**

Update `build.py` so Linux target metadata switches to `.AppImage`, while `bundle_path_for_target()` keeps returning the intermediate `dist/atv-player/` directory and the new helper exposes the final release artifact:

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
            archive_ext="AppImage",
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


def release_artifact_path_for_target(target_platform: str, arch: str | None = None) -> Path:
    return DIST_DIR / build_archive_name(target_platform, arch)
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `uv run pytest tests/test_build.py -q`

Expected: PASS for the Linux target metadata and final artifact path assertions, while Linux `build_pyinstaller_command()` still uses `--onedir`.

- [ ] **Step 5: Commit the Linux artifact-contract change**

Run:

```bash
git add build.py tests/test_build.py
git commit -m "feat: define linux appimage artifact contract"
```

Expected: a commit containing only Linux artifact naming/path contract changes.

### Task 2: Add Linux AppDir assembly and AppImage generation

**Files:**
- Create: `packaging/linux/AppRun`
- Create: `packaging/linux/atv-player.desktop`
- Modify: `tests/test_build.py`
- Modify: `build.py`

- [ ] **Step 1: Write the failing tests**

Append these tests to `tests/test_build.py`:

```python
def test_prepare_linux_appdir_copies_bundle_and_launcher_assets(monkeypatch, tmp_path) -> None:
    project_root = tmp_path / "project"
    bundle_dir = project_root / "dist" / "atv-player"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "atv-player").write_text("binary", encoding="utf-8")
    icons_dir = project_root / "src" / "atv_player" / "icons"
    icons_dir.mkdir(parents=True)
    (icons_dir / "app.svg").write_text("<svg />", encoding="utf-8")
    packaging_dir = project_root / "packaging" / "linux"
    packaging_dir.mkdir(parents=True)
    (packaging_dir / "AppRun").write_text("#!/bin/sh\nexec \"$APPDIR/usr/lib/atv-player/atv-player\" \"$@\"\n", encoding="utf-8")
    (packaging_dir / "atv-player.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=atv-player\nExec=atv-player\nIcon=atv-player\nTerminal=false\nCategories=AudioVideo;Player;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(build, "BUILD_DIR", project_root / "build")
    monkeypatch.setattr(build, "DIST_DIR", project_root / "dist")
    monkeypatch.setattr(build, "ICONS_DIR", icons_dir)

    appdir_path = build.prepare_linux_appdir(bundle_dir, "x86_64")

    assert appdir_path == project_root / "build" / "appimage" / "atv-player-x64.AppDir"
    assert (appdir_path / "AppRun").exists()
    assert (appdir_path / "atv-player.desktop").exists()
    assert (appdir_path / ".DirIcon").exists()
    assert (appdir_path / "atv-player.svg").exists()
    assert (appdir_path / "usr" / "lib" / "atv-player" / "atv-player").exists()


def test_build_linux_appimage_uses_appimagetool_with_normalized_arch(monkeypatch, tmp_path) -> None:
    appdir_path = tmp_path / "atv-player-x64.AppDir"
    appdir_path.mkdir()
    output_path = tmp_path / "atv-player-linux-x64.AppImage"
    tool_path = tmp_path / "appimagetool"
    tool_path.write_text("", encoding="utf-8")
    run_calls: dict[str, object] = {}

    monkeypatch.setattr(build, "appimage_tool_path", lambda: tool_path)

    def fake_run(command, check, cwd, env):
        run_calls["command"] = command
        run_calls["check"] = check
        run_calls["cwd"] = cwd
        run_calls["env"] = env

    monkeypatch.setattr(build.subprocess, "run", fake_run)

    build.build_linux_appimage(appdir_path, output_path, "x86_64")

    assert run_calls["command"] == [str(tool_path), str(appdir_path), str(output_path)]
    assert run_calls["check"] is True
    assert run_calls["cwd"] == build.PROJECT_ROOT
    assert run_calls["env"]["ARCH"] == "x86_64"


def test_appimage_tool_path_prefers_environment_variable(monkeypatch, tmp_path) -> None:
    tool_path = tmp_path / "appimagetool"
    tool_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("APPIMAGE_TOOL", str(tool_path))

    assert build.appimage_tool_path() == tool_path
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `uv run pytest tests/test_build.py -q`

Expected: FAIL because `prepare_linux_appdir()`, `build_linux_appimage()`, and `appimage_tool_path()` do not exist yet.

- [ ] **Step 3: Create the repository-owned Linux launcher assets**

Add `packaging/linux/AppRun`:

```sh
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/atv-player/atv-player" "$@"
```

Add `packaging/linux/atv-player.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=atv-player
Exec=atv-player
Icon=atv-player
Terminal=false
Categories=AudioVideo;Player;
```

- [ ] **Step 4: Implement the minimal Linux AppImage helpers in `build.py`**

Add the needed imports at the top:

```python
import shutil
import stat
```

Then add these helpers below `find_libmpv()`:

```python
def linux_appdir_path(arch: str | None = None) -> Path:
    return BUILD_DIR / "appimage" / f"{APP_NAME}-{normalize_arch(arch)}.AppDir"


def linux_appimage_arch(arch: str | None = None) -> str:
    normalized = normalize_arch(arch)
    return {"x64": "x86_64", "arm64": "aarch64"}.get(normalized, normalized)


def appimage_tool_path() -> Path:
    explicit = os.environ.get("APPIMAGE_TOOL")
    if explicit:
        path = Path(explicit)
        if path.exists():
            return path
        raise FileNotFoundError(f"AppImage tool was not found: {path}")
    tool = shutil.which("appimagetool")
    if tool:
        return Path(tool)
    raise FileNotFoundError("appimagetool was not found. Set APPIMAGE_TOOL or install appimagetool.")


def prepare_linux_appdir(bundle_path: Path, arch: str | None = None) -> Path:
    if not bundle_path.exists():
        raise FileNotFoundError(f"Missing Linux bundle directory: {bundle_path}")

    appdir_path = linux_appdir_path(arch)
    if appdir_path.exists():
        shutil.rmtree(appdir_path)
    appdir_path.mkdir(parents=True)

    packaging_dir = PROJECT_ROOT / "packaging" / "linux"
    app_run_source = packaging_dir / "AppRun"
    desktop_source = packaging_dir / f"{APP_NAME}.desktop"
    icon_source = ICONS_DIR / "app.svg"
    for required_path in (app_run_source, desktop_source, icon_source):
        if not required_path.exists():
            raise FileNotFoundError(f"Missing Linux packaging asset: {required_path}")

    shutil.copy2(app_run_source, appdir_path / "AppRun")
    (appdir_path / "AppRun").chmod((appdir_path / "AppRun").stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    shutil.copy2(desktop_source, appdir_path / f"{APP_NAME}.desktop")
    shutil.copy2(icon_source, appdir_path / f"{APP_NAME}.svg")
    shutil.copy2(icon_source, appdir_path / ".DirIcon")

    payload_dir = appdir_path / "usr" / "lib" / APP_NAME
    payload_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle_path, payload_dir)
    return appdir_path


def build_linux_appimage(appdir_path: Path, output_path: Path, arch: str | None = None) -> Path:
    tool_path = appimage_tool_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["ARCH"] = linux_appimage_arch(arch)
    subprocess.run([str(tool_path), str(appdir_path), str(output_path)], check=True, cwd=PROJECT_ROOT, env=env)
    if not output_path.exists():
        raise FileNotFoundError(f"Missing AppImage output: {output_path}")
    return output_path
```

Finally, update `build()` so Linux builds the intermediate `onedir` bundle and then produces the final AppImage:

```python
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

    if normalize_target_platform(target_platform) == "linux":
        appdir_path = prepare_linux_appdir(bundle_path)
        return build_linux_appimage(appdir_path, release_artifact_path_for_target(target_platform))
    return bundle_path
```

- [ ] **Step 5: Run the targeted tests to verify they pass**

Run: `uv run pytest tests/test_build.py -q`

Expected: PASS for the Linux AppDir assembly, AppImage command, and tool-discovery coverage.

- [ ] **Step 6: Commit the Linux AppImage build helpers**

Run:

```bash
git add build.py tests/test_build.py packaging/linux/AppRun packaging/linux/atv-player.desktop
git commit -m "feat: build linux appimage artifact"
```

Expected: a commit containing the new Linux packaging assets and AppImage helper logic.

### Task 3: Update GitHub Actions to publish Linux `.AppImage` files

**Files:**
- Modify: `tests/test_build.py`
- Modify: `.github/workflows/build.yml`

- [ ] **Step 1: Write the failing workflow assertions**

Append these assertions to `tests/test_build.py`:

```python
def test_github_workflow_uploads_linux_appimage_and_releases_it() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert "appimagetool-x86_64.AppImage" in workflow
    assert 'APPIMAGETOOL_APPIMAGE_EXTRACT_AND_RUN=1' in workflow
    assert "release_artifact_path_for_target" in workflow
    assert '-name "*.AppImage"' in workflow
    assert 'tar -C dist -czf "dist/$ARCHIVE_NAME" atv-player' not in workflow
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `uv run pytest tests/test_build.py -q`

Expected: FAIL because the current workflow still archives Linux as `tar.gz` and does not install or reference an AppImage tool.

- [ ] **Step 3: Implement the minimal workflow change**

Update the Linux dependency step in `.github/workflows/build.yml` to install `curl` and keep the current runtime packages:

```yaml
      - name: Install Linux system dependencies
        if: matrix.platform == 'linux'
        run: |
          sudo apt-get update
          sudo apt-get install -y curl libmpv-dev libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0
```

Add a Linux-only AppImage tool step after dependency installation:

```yaml
      - name: Install Linux AppImage tool
        if: matrix.platform == 'linux'
        run: |
          curl -fsSL -o "$RUNNER_TEMP/appimagetool-x86_64.AppImage" \
            "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
          chmod +x "$RUNNER_TEMP/appimagetool-x86_64.AppImage"
          echo "APPIMAGE_TOOL=$RUNNER_TEMP/appimagetool-x86_64.AppImage" >> "$GITHUB_ENV"
          echo "APPIMAGETOOL_APPIMAGE_EXTRACT_AND_RUN=1" >> "$GITHUB_ENV"
```

Replace the POSIX archive-resolution step with:

```yaml
      - name: Resolve artifact paths (POSIX)
        if: matrix.platform != 'windows'
        run: |
          ARCHIVE_NAME=$(uv run python -c "import build; print(build.build_archive_name('${{ matrix.platform }}'))")
          BUNDLE_PATH=$(uv run python -c "import build; print(build.bundle_path_for_target('${{ matrix.platform }}'))")
          RELEASE_ASSET_PATH=$(uv run python -c "import build; print(build.release_artifact_path_for_target('${{ matrix.platform }}'))")
          echo "ARCHIVE_NAME=$ARCHIVE_NAME" >> "$GITHUB_ENV"
          echo "BUNDLE_PATH=$BUNDLE_PATH" >> "$GITHUB_ENV"
          echo "RELEASE_ASSET_PATH=$RELEASE_ASSET_PATH" >> "$GITHUB_ENV"
```

Replace the Linux archive step with a direct file check:

```yaml
      - name: Archive Linux bundle
        if: matrix.platform == 'linux'
        run: |
          test -f "$RELEASE_ASSET_PATH"
```

Keep the upload step targeting the normalized archive name in `dist/`:

```yaml
      - name: Upload build artifact
        uses: actions/upload-artifact@v4
        with:
          name: ${{ matrix.platform }}-bundle
          path: dist/${{ env.ARCHIVE_NAME }}
          retention-days: 7
```

Update the release asset collection to:

```yaml
      - name: Prepare release assets
        run: |
          mkdir -p release-assets
          find artifacts -type f \( -name "*.AppImage" -o -name "*.zip" -o -name "*.exe" \) -exec cp {} release-assets/ \;
          ls -la release-assets
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `uv run pytest tests/test_build.py -q`

Expected: PASS with Linux workflow assertions updated for direct `.AppImage` publishing.

- [ ] **Step 5: Commit the workflow change**

Run:

```bash
git add .github/workflows/build.yml tests/test_build.py
git commit -m "ci: publish linux appimage artifact"
```

Expected: a commit containing only the Linux AppImage workflow changes and assertions.

### Task 4: Run the full packaging verification

**Files:**
- Modify: none
- Test: `tests/test_build.py`

- [ ] **Step 1: Run the packaging test module**

Run: `uv run pytest tests/test_build.py -v`

Expected: PASS for Linux `.AppImage`, macOS `.zip`, and Windows `.exe` packaging assertions.

- [ ] **Step 2: Inspect the packaging diff**

Run: `git diff -- build.py tests/test_build.py .github/workflows/build.yml packaging/linux/AppRun packaging/linux/atv-player.desktop docs/superpowers/specs/2026-04-15-linux-appimage-packaging-design.md docs/superpowers/plans/2026-04-15-linux-appimage-packaging.md`

Expected: only Linux AppImage packaging changes plus the spec/plan documents are present; Windows `.exe` and macOS `.zip` logic remain intact.

- [ ] **Step 3: Create the final combined commit**

Run:

```bash
git add build.py tests/test_build.py .github/workflows/build.yml packaging/linux/AppRun packaging/linux/atv-player.desktop docs/superpowers/specs/2026-04-15-linux-appimage-packaging-design.md docs/superpowers/plans/2026-04-15-linux-appimage-packaging.md
git commit -m "feat: publish linux appimage package"
```

Expected: a final commit containing the approved design doc, implementation plan, Linux AppImage packaging code, workflow updates, and test coverage.
