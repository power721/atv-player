#!/bin/bash
# Build fcitx5-qt6 plugin for PySide6 Qt version
# This script uses aqtinstall to install matching Qt SDK and compiles fcitx5 plugin

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Building fcitx5-qt6 plugin for PySide6 ==="

cd "$PROJECT_DIR"

# Get PySide6 Qt version using project venv
QT_VERSION=$(uv run python -c "from PySide6.QtCore import QLibraryInfo; print(QLibraryInfo.version().toString())" 2>/dev/null)
QT_MAJOR=$(echo $QT_VERSION | cut -d. -f1)
QT_MINOR=$(echo $QT_VERSION | cut -d. -f2)
QT_PATCH=$(echo $QT_VERSION | cut -d. -f3)
QT_VERSION_SHORT="${QT_MAJOR}.${QT_MINOR}"

echo "PySide6 Qt version: $QT_VERSION"

# Get PySide6 plugin path
PYSIDE_PLUGIN_PATH=$(uv run python -c "from PySide6.QtCore import QLibraryInfo; print(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))" 2>/dev/null)
echo "PySide6 plugin path: $PYSIDE_PLUGIN_PATH"

# Install build dependencies
echo ""
echo "=== Installing build dependencies ==="
echo "Please run: sudo apt install build-essential cmake extra-cmake-modules libfcitx5core-dev libfcitx5utils-dev libgl1-mesa-dev libxkbcommon-dev libwayland-dev wayland-protocols"

# Install aqtinstall to project venv
echo ""
echo "=== Installing aqtinstall ==="
uv pip install aqtinstall

# Install Qt SDK matching PySide6 version
QT_INSTALL_DIR="$HOME/Qt${QT_VERSION}"
echo ""
echo "=== Installing Qt ${QT_VERSION} SDK to ${QT_INSTALL_DIR} ==="
uv run aqt install-qt linux desktop ${QT_VERSION} linux_gcc_64 \
    -O "$QT_INSTALL_DIR"

QT_SDK_PATH="${QT_INSTALL_DIR}/${QT_VERSION}/gcc_64"
echo "Qt SDK installed at: $QT_SDK_PATH"

# Clone fcitx5-qt repository
BUILD_DIR=$(mktemp -d)
echo ""
echo "=== Building in $BUILD_DIR ==="
cd "$BUILD_DIR"

git clone --depth 1 https://github.com/fcitx/fcitx5-qt.git
cd fcitx5-qt

# Build only Qt6 plugin
mkdir -p build
cd build

# Configure with Qt6 from aqtinstall
# BUILD_ONLY_PLUGIN skips DBus addons and widgets that need fcitx5 dev libraries
cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_QT4=OFF \
    -DENABLE_QT5=OFF \
    -DENABLE_QT6=ON \
    -DBUILD_ONLY_PLUGIN=ON \
    -DCMAKE_PREFIX_PATH="$QT_SDK_PATH" \
    -DCMAKE_INSTALL_PREFIX=/usr

make -j$(nproc)

# Copy plugin to PySide6
echo ""
echo "=== Installing plugin to PySide6 ==="
PLUGIN_SRC="qt6/platforminputcontext/libfcitx5platforminputcontextplugin.so"
PLUGIN_DEST="$PYSIDE_PLUGIN_PATH/platforminputcontexts/"

if [ -f "$PLUGIN_SRC" ]; then
    mkdir -p "$PLUGIN_DEST"
    cp "$PLUGIN_SRC" "$PLUGIN_DEST/"
    echo "Plugin installed to: $PLUGIN_DEST"
else
    echo "ERROR: Plugin not found at $PLUGIN_SRC"
    exit 1
fi

# Cleanup
cd /
rm -rf "$BUILD_DIR"

echo ""
echo "=== Done! ==="
echo "Please restart the application to use fcitx5 input method."
