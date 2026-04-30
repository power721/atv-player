from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build


def test_normalize_target_platform_maps_runtime_names(monkeypatch) -> None:
    monkeypatch.setattr(build.platform, "system", lambda: "Darwin")

    assert build.normalize_target_platform("current") == "macos"
    assert build.normalize_target_platform("linux") == "linux"
    assert build.normalize_target_platform("win32") == "windows"


def test_resolve_artifact_version_prefers_explicit_value() -> None:
    assert build.resolve_artifact_version("1.2.3") == "1.2.3"
    assert build.resolve_artifact_version(" 2.0.0 ") == "2.0.0"


def test_resolve_artifact_version_falls_back_to_default() -> None:
    assert build.resolve_artifact_version(None) == "dev"
    assert build.resolve_artifact_version("") == "dev"
    assert build.resolve_artifact_version("   ") == "dev"


def test_build_archive_name_includes_explicit_version() -> None:
    assert build.build_archive_name("linux", "x86_64", "1.2.3") == "atv-player-1.2.3-linux-x64.AppImage"
    assert build.build_archive_name("Darwin", "aarch64", "2.0.0") == "atv-player-2.0.0-macos-arm64.zip"
    assert build.build_archive_name("windows", "AMD64", "3.4.5") == "atv-player-3.4.5-windows-x64.exe"


def test_build_archive_name_uses_default_version_when_missing() -> None:
    assert build.build_archive_name("linux", "x86_64") == "atv-player-dev-linux-x64.AppImage"
    assert build.build_archive_name("windows", "AMD64", "   ") == "atv-player-dev-windows-x64.exe"


def test_build_target_linux_uses_appimage_release_format() -> None:
    target = build.build_target("linux")

    assert target.platform_id == "linux"
    assert target.archive_ext == "AppImage"
    assert target.data_separator == ":"
    assert target.windowed is False
    assert target.onefile is False


def test_build_target_windows_uses_onefile_mode() -> None:
    target = build.build_target("windows")

    assert target.platform_id == "windows"
    assert target.archive_ext == "exe"
    assert target.data_separator == ";"
    assert target.windowed is True
    assert target.onefile is True


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


def test_find_libmpv_uses_repo_local_windows_runtime_dir(monkeypatch, tmp_path) -> None:
    runtime_dir = tmp_path / "mpv"
    runtime_dir.mkdir()
    dll_path = runtime_dir / "libmpv-2.dll"
    dll_path.write_bytes(b"dll")
    monkeypatch.setattr(build, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("ATV_MPV_RUNTIME_DIR", raising=False)
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
    assert "--onefile" not in command
    assert "--paths" in command
    assert "src" in command
    assert "--windowed" not in command
    assert "--add-data" in command
    assert f"{build.ICONS_DIR}:atv_player/icons" in command
    assert "--add-binary" in command
    assert f"{libmpv}:." in command
    assert "--icon" not in command
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
    assert "--icon" in command
    assert command[command.index("--icon") + 1] == str(build.PROJECT_ROOT / "packaging" / "icons" / "app.ico")


@pytest.mark.parametrize("target_platform", ["linux", "windows", "macos"])
def test_build_pyinstaller_command_includes_spider_plugin_runtime_deps(
    monkeypatch, tmp_path, target_platform: str
) -> None:
    runtime_path = tmp_path / "runtime-lib"
    runtime_path.write_bytes(b"lib")
    monkeypatch.setattr(build, "find_libmpv", lambda platform: [(runtime_path, ".")])

    command = build.build_pyinstaller_command(target_platform)

    hidden_imports = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--hidden-import"
    ]

    assert "pyquery" in hidden_imports
    assert "bs4" in hidden_imports


def test_build_pyinstaller_command_macos_uses_native_app_icon(monkeypatch, tmp_path) -> None:
    dylib_path = tmp_path / "libmpv.dylib"
    dylib_path.write_bytes(b"dylib")
    monkeypatch.setattr(build, "find_libmpv", lambda target_platform: [(dylib_path, ".")])

    command = build.build_pyinstaller_command("macos")

    assert "--windowed" in command
    assert "--onedir" in command
    assert "--onefile" not in command
    assert "--icon" in command
    assert command[command.index("--icon") + 1] == str(build.PROJECT_ROOT / "packaging" / "icons" / "app.icns")


def test_bundle_path_for_target_matches_platform_output() -> None:
    assert build.bundle_path_for_target("linux") == build.DIST_DIR / "atv-player"
    assert build.bundle_path_for_target("macos") == build.DIST_DIR / "atv-player.app"
    assert build.bundle_path_for_target("windows") == build.DIST_DIR / "atv-player.exe"


def test_release_artifact_path_for_target_includes_version() -> None:
    assert build.release_artifact_path_for_target("linux", "x86_64", "1.2.3") == (
        build.DIST_DIR / "atv-player-1.2.3-linux-x64.AppImage"
    )
    assert build.release_artifact_path_for_target("macos", "arm64") == build.DIST_DIR / "atv-player-dev-macos-arm64.zip"
    assert build.release_artifact_path_for_target("windows", "AMD64", "9.9.9") == (
        build.DIST_DIR / "atv-player-9.9.9-windows-x64.exe"
    )


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
    (packaging_dir / "AppRun").write_text(
        "#!/bin/sh\nexec \"$APPDIR/usr/lib/atv-player/atv-player\" \"$@\"\n",
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

    assert appdir_path == project_root / "build" / "appimage" / "atv-player-x64.AppDir"
    assert (appdir_path / "AppRun").exists()
    assert (appdir_path / "atv-player.desktop").exists()
    assert (appdir_path / ".DirIcon").exists()
    assert (appdir_path / "atv-player.svg").exists()
    assert (appdir_path / "usr" / "lib" / "atv-player" / "atv-player").exists()


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
    repo_root = Path(__file__).resolve().parents[1]
    (packaging_dir / "AppRun").write_text((repo_root / "packaging" / "linux" / "AppRun").read_text(encoding="utf-8"))
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


def test_build_linux_uses_artifact_version_for_appimage_output(monkeypatch, tmp_path) -> None:
    project_root = tmp_path / "project"
    dist_dir = project_root / "dist"
    bundle_dir = dist_dir / "atv-player"
    bundle_dir.mkdir(parents=True)
    entrypoint = project_root / "src" / "atv_player" / "main.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("print('ok')\n", encoding="utf-8")
    icons_dir = project_root / "src" / "atv_player" / "icons"
    icons_dir.mkdir(parents=True)
    (icons_dir / "app.svg").write_text("<svg />", encoding="utf-8")

    run_calls: dict[str, object] = {}

    monkeypatch.setattr(build, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(build, "DIST_DIR", dist_dir)
    monkeypatch.setattr(build, "BUILD_DIR", project_root / "build")
    monkeypatch.setattr(build, "ENTRYPOINT", entrypoint)
    monkeypatch.setattr(build, "ICONS_DIR", icons_dir)
    monkeypatch.setattr(build, "build_pyinstaller_command", lambda target_platform: ["fake-pyinstaller", target_platform])
    monkeypatch.setattr(build.subprocess, "run", lambda command, check, cwd: run_calls.update(command=command, check=check, cwd=cwd))
    monkeypatch.setattr(build, "prepare_linux_appdir", lambda bundle_path: project_root / "build" / "appdir")

    def fake_build_linux_appimage(appdir_path, output_path, arch=None):
        run_calls["appdir_path"] = appdir_path
        run_calls["output_path"] = output_path
        run_calls["arch"] = arch
        return output_path

    monkeypatch.setattr(build, "build_linux_appimage", fake_build_linux_appimage)
    monkeypatch.setenv("ARTIFACT_VERSION", "0.3.2")

    result = build.build("linux")

    assert run_calls["command"] == ["fake-pyinstaller", "linux"]
    assert run_calls["check"] is True
    assert run_calls["cwd"] == project_root
    assert run_calls["appdir_path"] == project_root / "build" / "appdir"
    assert run_calls["output_path"] == dist_dir / "atv-player-0.3.2-linux-x64.AppImage"
    assert run_calls["arch"] is None
    assert result == dist_dir / "atv-player-0.3.2-linux-x64.AppImage"


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
        output_path.write_bytes(b"appimage")

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


def test_unknown_platform_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported target platform"):
        build.normalize_target_platform("plan9")


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


def test_github_workflow_uploads_windows_exe_and_releases_it() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert "Copy-Item $env:BUNDLE_PATH \"dist\\$env:ARCHIVE_NAME\" -Force" in workflow
    assert "path: dist/${{ env.ARCHIVE_NAME }}" in workflow
    assert '-name "*.exe"' in workflow
    assert 'Compress-Archive -Path "$env:BUNDLE_PATH\\*"' not in workflow


def test_github_workflow_uploads_linux_appimage_and_releases_it() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert "appimagetool-x86_64.AppImage" in workflow
    assert "APPIMAGE_EXTRACT_AND_RUN=1" in workflow
    assert "release_artifact_path_for_target" in workflow
    assert '-name "*.AppImage"' in workflow
    assert 'tar -C dist -czf "dist/$ARCHIVE_NAME" atv-player' not in workflow


def test_github_workflow_resolves_versioned_artifact_names() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert 'if [[ "${GITHUB_REF}" == refs/tags/v* ]]; then' in workflow
    assert 'ARTIFACT_VERSION="${GITHUB_REF_NAME#v}"' in workflow
    assert "ARTIFACT_VERSION=dev" in workflow
    assert "build.build_archive_name('${{ matrix.platform }}', version=os.environ['ARTIFACT_VERSION'])" in workflow
    assert "build.release_artifact_path_for_target('${{ matrix.platform }}', version=os.environ['ARTIFACT_VERSION'])" in workflow
    assert workflow.index("Resolve artifact version (POSIX)") < workflow.index("Build application")
    assert workflow.index("Resolve artifact version (Windows)") < workflow.index("Build application")
