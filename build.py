from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "atv-player"
DEFAULT_ARTIFACT_VERSION = "dev"
PROJECT_ROOT = Path(__file__).resolve().parent
ENTRYPOINT = PROJECT_ROOT / "src" / "atv_player" / "main.py"
ICONS_DIR = PROJECT_ROOT / "src" / "atv_player" / "icons"
PACKAGING_ICONS_DIR = PROJECT_ROOT / "packaging" / "icons"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"


@dataclass(frozen=True, slots=True)
class BuildTarget:
    platform_id: str
    archive_ext: str
    data_separator: str
    windowed: bool
    onefile: bool


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


def resolve_artifact_version(value: str | None = None) -> str:
    normalized = (value or "").strip()
    return normalized or DEFAULT_ARTIFACT_VERSION


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


def build_archive_name(target_platform: str, arch: str | None = None, version: str | None = None) -> str:
    target = build_target(target_platform)
    return (
        f"{APP_NAME}-"
        f"{resolve_artifact_version(version)}-"
        f"{target.platform_id}-"
        f"{normalize_arch(arch)}."
        f"{target.archive_ext}"
    )


def release_artifact_path_for_target(
    target_platform: str,
    arch: str | None = None,
    version: str | None = None,
) -> Path:
    return DIST_DIR / build_archive_name(target_platform, arch, version)


def data_mapping(source: Path, destination: str, target_platform: str) -> str:
    return f"{source}{build_target(target_platform).data_separator}{destination}"


def pyinstaller_icon_path(target_platform: str) -> Path | None:
    platform_id = normalize_target_platform(target_platform)
    if platform_id == "windows":
        return PACKAGING_ICONS_DIR / "app.ico"
    if platform_id == "macos":
        return PACKAGING_ICONS_DIR / "app.icns"
    return None


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
    search_dirs.append(PROJECT_ROOT / "mpv")
    search_dirs.extend(Path(part) for part in os.environ.get("PATH", "").split(os.pathsep) if part)

    for directory in search_dirs:
        for dll_name in ("libmpv-2.dll", "mpv-2.dll", "mpv.dll"):
            dll_path = directory / dll_name
            if dll_path.exists():
                return [(dll_path, ".")]
    raise FileNotFoundError("libmpv was not found on Windows")


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

    app_run_target = appdir_path / "AppRun"
    shutil.copy2(app_run_source, app_run_target)
    app_run_target.chmod(app_run_target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
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
    icon_path = pyinstaller_icon_path(target.platform_id)
    if icon_path is not None:
        command.extend(["--icon", str(icon_path)])
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


def build(target_platform: str) -> Path:
    if not ENTRYPOINT.exists():
        raise FileNotFoundError(f"Missing entrypoint: {ENTRYPOINT}")
    if not ICONS_DIR.exists():
        raise FileNotFoundError(f"Missing icons directory: {ICONS_DIR}")
    icon_path = pyinstaller_icon_path(target_platform)
    if icon_path is not None and not icon_path.exists():
        raise FileNotFoundError(f"Missing PyInstaller icon: {icon_path}")

    command = build_pyinstaller_command(target_platform)
    subprocess.run(command, check=True, cwd=PROJECT_ROOT)

    bundle_path = bundle_path_for_target(target_platform)
    if not bundle_path.exists():
        raise FileNotFoundError(f"Missing build output: {bundle_path}")
    if normalize_target_platform(target_platform) == "linux":
        appdir_path = prepare_linux_appdir(bundle_path)
        return build_linux_appimage(appdir_path, release_artifact_path_for_target(target_platform))
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
