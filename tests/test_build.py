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


def test_prepare_runtime_version_file_writes_resolved_version(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(build, "BUILD_DIR", tmp_path / "build")

    version_file = build.prepare_runtime_version_file(" 0.3.2 ")

    assert version_file == tmp_path / "build" / "runtime" / build.RUNTIME_VERSION_FILENAME
    assert version_file.read_text(encoding="utf-8") == "0.3.2"


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


def test_find_libmpv_macos_uses_macports_prefix(monkeypatch) -> None:
    macports = Path("/opt/local/lib/libmpv.dylib")

    def fake_exists(self):
        return self == macports

    monkeypatch.setattr(Path, "exists", fake_exists)

    assert build.find_libmpv("macos") == [(macports, ".")]


def test_find_libmpv_macos_prefers_homebrew_over_macports_and_usr_local(monkeypatch) -> None:
    homebrew = Path("/opt/homebrew/lib/libmpv.dylib")
    existing = {homebrew, Path("/opt/local/lib/libmpv.dylib"), Path("/usr/local/lib/libmpv.dylib")}

    monkeypatch.setattr(Path, "exists", lambda self: self in existing)

    assert build.find_libmpv("macos") == [(homebrew, ".")]


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


def test_find_libmpv_linux_prefers_local_build_over_distro(monkeypatch) -> None:
    local_dir = "/usr/local/lib/x86_64-linux-gnu"
    distro_dir = "/usr/lib/x86_64-linux-gnu"
    versioned = Path(f"{local_dir}/libmpv.so.2")

    def fake_glob(self, pattern):
        full = str(self / pattern)
        if full.startswith(f"{local_dir}/libmpv.so"):
            # 目录里同时有无版本后缀的开发链接与带版本的真实库,应选后者
            return [Path(f"{local_dir}/libmpv.so"), versioned]
        if full.startswith(f"{distro_dir}/libmpv.so"):
            return [Path(f"{distro_dir}/libmpv.so.2")]
        return []

    monkeypatch.setattr(Path, "glob", fake_glob)

    assert build.find_libmpv("linux") == [(versioned, ".")]


def test_find_libmpv_linux_falls_back_to_distro_without_local(monkeypatch) -> None:
    distro = Path("/usr/lib/x86_64-linux-gnu/libmpv.so.2")

    def fake_glob(self, pattern):
        full = str(self / pattern)
        if full.startswith("/usr/lib/x86_64-linux-gnu/libmpv.so"):
            return [distro]
        return []

    monkeypatch.setattr(Path, "glob", fake_glob)

    assert build.find_libmpv("linux") == [(distro, ".")]


def test_build_pyinstaller_command_collects_icons_and_libmpv(monkeypatch, tmp_path) -> None:
    libmpv = tmp_path / "libmpv.so.2"
    libmpv.write_bytes(b"so")
    version_file = tmp_path / build.RUNTIME_VERSION_FILENAME
    version_file.write_text("0.3.2", encoding="utf-8")
    monkeypatch.setattr(build, "find_libmpv", lambda target_platform: [(libmpv, ".")])

    command = build.build_pyinstaller_command("linux", runtime_version_file=version_file)

    assert command[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "--noconfirm" in command
    assert "--clean" in command
    assert "--copy-metadata" in command
    assert command[command.index("--copy-metadata") + 1] == build.APP_NAME
    assert "--onedir" in command
    assert "--onefile" not in command
    assert "--paths" in command
    assert "src" in command
    assert "--windowed" not in command
    assert "--add-data" in command
    assert f"{build.ICONS_DIR}:atv_player/icons" in command
    assert f"{version_file}:atv_player/{build.RUNTIME_VERSION_FILENAME}" in command
    assert "--add-binary" in command
    assert f"{libmpv}:." in command
    assert "--icon" not in command
    assert command[-1] == str(build.ENTRYPOINT)


@pytest.mark.parametrize("target_platform,separator", [("linux", ":"), ("macos", ":"), ("windows", ";")])
def test_build_pyinstaller_command_collects_js_bridge_assets(
    monkeypatch, tmp_path, target_platform: str, separator: str
) -> None:
    runtime_path = tmp_path / "runtime-lib"
    runtime_path.write_bytes(b"lib")
    monkeypatch.setattr(build, "find_libmpv", lambda platform: [(runtime_path, ".")])

    command = build.build_pyinstaller_command(target_platform)

    add_data_values = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--add-data"
    ]
    assert f"{build.JS_BRIDGE_DIR}{separator}atv_player/plugins/js_bridge" in add_data_values


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


@pytest.mark.parametrize("target_platform", ["linux", "windows", "macos"])
def test_build_pyinstaller_command_collects_crypto_submodules_for_secspider_plugins(
    monkeypatch, tmp_path, target_platform: str
) -> None:
    runtime_path = tmp_path / "runtime-lib"
    runtime_path.write_bytes(b"lib")
    monkeypatch.setattr(build, "find_libmpv", lambda platform: [(runtime_path, ".")])

    command = build.build_pyinstaller_command(target_platform)

    assert "--collect-submodules" in command
    assert command[command.index("--collect-submodules") + 1] == "Crypto"


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
    def fake_build_pyinstaller_command(target_platform, runtime_version_file=None):
        run_calls["runtime_version_file"] = runtime_version_file
        return ["fake-pyinstaller", target_platform]

    monkeypatch.setattr(build, "build_pyinstaller_command", fake_build_pyinstaller_command)
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
    assert run_calls["runtime_version_file"] == project_root / "build" / "runtime" / build.RUNTIME_VERSION_FILENAME
    assert run_calls["runtime_version_file"].read_text(encoding="utf-8") == "0.3.2"
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


def test_github_workflow_builds_macos_intel_bundle_via_macports() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert "macos-15-intel" in workflow
    assert "port -N install mpv" in workflow
    # Intel 构建不得走 Homebrew(x86_64 已无 bottle,会整树源码编译)
    assert "if: matrix.macos_runtime == 'homebrew'" in workflow
    assert "if: matrix.macos_runtime == 'macports'" in workflow


def test_github_workflow_artifact_names_are_unique_per_job() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    # 两个 macOS job 共用 platform=macos,须用 os 区分 artifact 名
    assert "name: ${{ matrix.os }}-bundle" in workflow


def test_github_workflow_releases_only_for_version_tags() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert "tags:" in workflow
    assert "- 'v*'" in workflow
    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "softprops/action-gh-release@v2" in workflow


def test_github_workflow_includes_release_notes_in_telegram_notification() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert 'gh release view "$GITHUB_REF_NAME" --json body --jq .body' in workflow
    assert "body<<EOF" in workflow
    assert "更新内容:" in workflow
    assert "${{ steps.release_notes.outputs.body }}" in workflow
    assert workflow.index("Create GitHub Release") < workflow.index("Read release notes")
    assert workflow.index("Read release notes") < workflow.index("send telegram message")


def test_github_workflow_uses_repo_release_notes_file_for_release_body() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")
    release_job = workflow[workflow.index("  release:") :]

    assert "body_path: RELEASE_NOTES.md" in release_job
    assert "generate_release_notes: true" not in release_job
    assert release_job.index("Checkout code") < release_job.index("Create GitHub Release")


def test_github_workflow_checks_out_repo_before_release_notes_lookup() -> None:
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")
    release_job = workflow[workflow.index("  release:") :]

    assert "actions/checkout@v4" in release_job
    assert release_job.index("Checkout code") < release_job.index("Read release notes")


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
