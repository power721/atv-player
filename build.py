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
