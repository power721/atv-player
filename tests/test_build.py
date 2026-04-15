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
