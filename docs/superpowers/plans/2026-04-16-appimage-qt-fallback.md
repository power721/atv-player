# AppImage Qt Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Linux AppImage prefer a stable Qt/X11 startup path instead of the failing xcb EGL path.

**Architecture:** Keep the change in `packaging/linux/AppRun` so only the Linux AppImage runtime is affected. Lock the behavior with a packaging regression test in `tests/test_build.py` that inspects the copied launcher content after AppDir assembly.

**Tech Stack:** Python 3.14, pytest, shell launcher script, AppImage packaging helpers

---

### File Map

**Files:**
- Modify: `packaging/linux/AppRun`
- Modify: `tests/test_build.py`
- Reference: `build.py`

### Task 1: Lock the launcher behavior in tests

**Files:**
- Modify: `tests/test_build.py`
- Reference: `build.py`

- [ ] **Step 1: Write the failing test**

```python
def test_prepare_linux_appdir_preserves_qt_fallback_launcher(monkeypatch, tmp_path) -> None:
    project_root = tmp_path / "project"
    bundle_dir = project_root / "dist" / "atv-player"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "atv-player").write_text("binary", encoding="utf-8")
    icons_dir = project_root / "src" / "atv_player" / "icons"
    icons_dir.mkdir(parents=True)
    (icons_dir / "app.svg").write_text("<svg />", encoding="utf-8")
    packaging_dir = project_root / "packaging" / "linux"
    packaging_dir.mkdir(parents=True)
    (packaging_dir / "AppRun").write_text(
        "#!/bin/sh\n"
        "HERE=\"$(dirname \"$(readlink -f \"$0\")\")\"\n"
        "export QT_QPA_PLATFORM_PLUGIN_PATH=\"$HERE/usr/lib/atv-player/_internal/PySide6/Qt/plugins/platforms\"\n"
        ": \"${QT_QPA_PLATFORM:=xcb}\"\n"
        "export QT_QPA_PLATFORM\n"
        ": \"${QT_OPENGL:=software}\"\n"
        "export QT_OPENGL\n"
        ": \"${QT_XCB_GL_INTEGRATION:=none}\"\n"
        "export QT_XCB_GL_INTEGRATION\n"
        "exec \"$HERE/usr/lib/atv-player/atv-player\" \"$@\"\n",
        encoding="utf-8",
    )
    (packaging_dir / "atv-player.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=atv-player\n"
        "Exec=atv-player\n"
        "Icon=atv-player\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Player;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(build, "BUILD_DIR", project_root / "build")
    monkeypatch.setattr(build, "DIST_DIR", project_root / "dist")
    monkeypatch.setattr(build, "ICONS_DIR", icons_dir)

    appdir_path = build.prepare_linux_appdir(bundle_dir, "x86_64")
    launcher = (appdir_path / "AppRun").read_text(encoding="utf-8")

    assert 'QT_QPA_PLATFORM_PLUGIN_PATH="$HERE/usr/lib/atv-player/_internal/PySide6/Qt/plugins/platforms"' in launcher
    assert ': "${QT_QPA_PLATFORM:=xcb}"' in launcher
    assert ': "${QT_OPENGL:=software}"' in launcher
    assert ': "${QT_XCB_GL_INTEGRATION:=none}"' in launcher
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_build.py::test_prepare_linux_appdir_preserves_qt_fallback_launcher -v`
Expected: FAIL because the current launcher does not export the Qt fallback variables.

### Task 2: Add the minimal AppImage launcher fix

**Files:**
- Modify: `packaging/linux/AppRun`
- Test: `tests/test_build.py::test_prepare_linux_appdir_preserves_qt_fallback_launcher`

- [ ] **Step 1: Write minimal implementation**

```sh
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export QT_QPA_PLATFORM_PLUGIN_PATH="$HERE/usr/lib/atv-player/_internal/PySide6/Qt/plugins/platforms"
: "${QT_QPA_PLATFORM:=xcb}"
export QT_QPA_PLATFORM
: "${QT_OPENGL:=software}"
export QT_OPENGL
: "${QT_XCB_GL_INTEGRATION:=none}"
export QT_XCB_GL_INTEGRATION
exec "$HERE/usr/lib/atv-player/atv-player" "$@"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_build.py::test_prepare_linux_appdir_preserves_qt_fallback_launcher -v`
Expected: PASS

- [ ] **Step 3: Run the focused packaging suite**

Run: `uv run pytest tests/test_build.py -v`
Expected: PASS
