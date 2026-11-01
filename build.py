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
RUNTIME_VERSION_FILENAME = "_build_version.txt"
PROJECT_ROOT = Path(__file__).resolve().parent
ENTRYPOINT = PROJECT_ROOT / "src" / "atv_player" / "main.py"
ICONS_DIR = PROJECT_ROOT / "src" / "atv_player" / "icons"
JS_BRIDGE_DIR = PROJECT_ROOT / "src" / "atv_player" / "plugins" / "js_bridge"
PACKAGING_ICONS_DIR = PROJECT_ROOT / "packaging" / "icons"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
SPIDER_PLUGIN_RUNTIME_HIDDEN_IMPORTS = (
    "bs4",
    "pyquery",
)
SPIDER_PLUGIN_RUNTIME_COLLECT_SUBMODULES = (
    "Crypto",
)


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


def prepare_runtime_version_file(version: str | None = None) -> Path:
    path = BUILD_DIR / "runtime" / RUNTIME_VERSION_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(resolve_artifact_version(version), encoding="utf-8")
    return path


def prepare_runtime_version_hook(version: str | None = None) -> Path:
    path = BUILD_DIR / "runtime" / "_version_hook.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'import os\nos.environ["ATV_BUILD_VERSION"] = {resolve_artifact_version(version)!r}\n',
        encoding="utf-8",
    )
    return path


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
            # 优先本地构建的新版(scripts/build_mpv.sh 安装到 /usr/local),
            # 发行版 /usr/lib 里的版本往往过旧(如 noble 的 0.37 不支持
            # deinterlace=auto)。
            "/usr/local/lib/x86_64-linux-gnu/libmpv.so*",
            "/usr/local/lib64/libmpv.so*",
            "/usr/local/lib/libmpv.so*",
            "/usr/lib/x86_64-linux-gnu/libmpv.so*",
            "/usr/lib64/libmpv.so*",
            "/usr/lib/libmpv.so*",
        ]
        for pattern in search_patterns:
            matches = sorted(Path("/").glob(pattern.lstrip("/")))
            if matches:
                versioned = [m for m in matches if m.name == "libmpv.so.2"]
                return [(versioned[0] if versioned else matches[0], ".")]
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


def render_linux_appimage_icon(svg_path: Path, png_path: Path) -> None:
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(svg_path.read_bytes())
    if not renderer.isValid():
        raise ValueError(f"Invalid Linux application icon: {svg_path}")

    image = QImage(QSize(256, 256), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    if not image.save(str(png_path), "PNG"):
        raise OSError(f"Could not write Linux application icon: {png_path}")


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
    icon_png_path = appdir_path / f"{APP_NAME}.png"
    render_linux_appimage_icon(icon_source, icon_png_path)
    shutil.copy2(icon_png_path, appdir_path / ".DirIcon")

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


def build_pyinstaller_command(
    target_platform: str,
    runtime_version_file: Path | None = None,
    runtime_version_hook: Path | None = None,
) -> list[str]:
    target = build_target(target_platform)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--copy-metadata",
        APP_NAME,
        "--onefile" if target.onefile else "--onedir",
        "--name",
        APP_NAME,
        "--paths",
        "src",
    ]
    for module_name in SPIDER_PLUGIN_RUNTIME_HIDDEN_IMPORTS:
        command.extend(["--hidden-import", module_name])
    for package_name in SPIDER_PLUGIN_RUNTIME_COLLECT_SUBMODULES:
        command.extend(["--collect-submodules", package_name])
    command.extend(
        [
            "--add-data",
            data_mapping(ICONS_DIR, "atv_player/icons", target.platform_id),
            "--add-data",
            data_mapping(JS_BRIDGE_DIR, "atv_player/plugins/js_bridge", target.platform_id),
            "--add-binary",
            data_mapping(find_libmpv(target.platform_id)[0][0], ".", target.platform_id),
        ]
    )
    if runtime_version_file is not None:
        command.extend(
            [
                "--add-data",
                data_mapping(runtime_version_file, f"atv_player/{RUNTIME_VERSION_FILENAME}", target.platform_id),
            ]
        )
    if runtime_version_hook is not None:
        command.extend(["--runtime-hook", str(runtime_version_hook)])
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
    artifact_version = resolve_artifact_version(os.environ.get("ARTIFACT_VERSION"))
    icon_path = pyinstaller_icon_path(target_platform)
    if icon_path is not None and not icon_path.exists():
        raise FileNotFoundError(f"Missing PyInstaller icon: {icon_path}")

    runtime_version_file = prepare_runtime_version_file(artifact_version)
    runtime_version_hook = prepare_runtime_version_hook(artifact_version)
    try:
        command = build_pyinstaller_command(
            target_platform,
            runtime_version_file=runtime_version_file,
            runtime_version_hook=runtime_version_hook,
        )
    except TypeError as exc:
        if "runtime_version_hook" not in str(exc):
            raise
        command = build_pyinstaller_command(
            target_platform,
            runtime_version_file=runtime_version_file,
        )
    subprocess.run(command, check=True, cwd=PROJECT_ROOT)

    bundle_path = bundle_path_for_target(target_platform)
    if not bundle_path.exists():
        raise FileNotFoundError(f"Missing build output: {bundle_path}")
    if normalize_target_platform(target_platform) == "linux":
        appdir_path = prepare_linux_appdir(bundle_path)
        return build_linux_appimage(
            appdir_path,
            release_artifact_path_for_target(target_platform, version=artifact_version),
        )
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
