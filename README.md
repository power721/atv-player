# atv-player

基于 `PySide6` 和 `mpv` 的 `alist-tvbox` 桌面播放器，当前以 Linux 为优先目标平台，同时保留 macOS 和 Windows 打包支持。

应用默认连接 `http://127.0.0.1:4567`，提供登录、资源浏览、豆瓣电影、电报影视、网络直播、Emby、Jellyfin、播放记录和独立播放器窗口。

## 功能概览

- 登录 `alist-tvbox` 后端并持久化保存基础配置与令牌
- 使用独立播放器窗口播放视频，而不是把播放器嵌入主页面
- 支持豆瓣电影、电报影视、网络直播、文件浏览、播放记录
- 根据后端能力自动显示 `Emby` 和 `Jellyfin` 标签页
- 支持播放列表、上一集/下一集、拖动进度、音量、倍速、字幕轨、音轨
- 支持恢复历史播放进度，并定时上报播放进度到后端
- 本地缓存海报图片，减少重复请求

## 环境要求

- Python `3.12+`
- `uv`
- 可用的 `libmpv`
- 一个可访问的 `alist-tvbox` 后端

Linux 上如果系统里没有 `libmpv`，打包和运行都会失败。`build.py` 会在常见系统目录查找它。

## 快速开始

安装开发依赖：

```bash
uv sync --group dev
```

启动应用：

```bash
./start.sh
```

`start.sh` 实际执行的是：

```bash
uv run src/atv_player/main.py
```

首次启动后，本地配置数据库会写到：

```text
~/.local/share/atv-player/app.db
```

海报缓存目录位于：

```text
~/.cache/atv-player/posters
```

## 开发

运行测试：

```bash
uv run pytest
```

运行 `ruff`：

```bash
uv run ruff check .
```

项目采用 `src` 布局，主要目录如下：

- `src/atv_player`: 应用代码
- `tests`: 单元测试和 UI 测试
- `packaging/linux`: Linux 桌面打包资源
- `docs/superpowers`: 设计说明与实现计划

## 打包

本地打包和 GitHub Actions 共用同一个入口：

```bash
uv sync --group dev --group package
uv run python build.py current
```

也可以显式指定目标平台：

```bash
uv run python build.py linux
uv run python build.py macos
uv run python build.py windows
```

各平台输出规则：

- Linux: 先生成 `PyInstaller` 目录包，再封装成 `AppImage`
- macOS: 生成 `.app`
- Windows: 生成单文件 `.exe`

Linux 打包额外要求：

- 系统里需要 `appimagetool`
- 系统里需要可用的 `libmpv`

Windows 打包时，`build.py` 会优先从这些位置查找 `mpv` 运行库：

- 环境变量 `ATV_MPV_RUNTIME_DIR`
- 仓库根目录下的 `mpv/`
- 当前 `PATH`

GitHub Actions 会为 Pull Request 和手动触发构建 Linux、macOS、Windows 制品；推送以 `v` 开头的标签时，还会创建 GitHub Release 并上传产物。

## 数据与状态

应用通过本地 `sqlite` 保存以下状态：

- 后端地址
- 用户名
- 登录令牌和 `vod token`
- 最近浏览路径
- 上次活跃窗口
- 播放器音量
- 主窗口和播放器窗口布局状态

应用不会保存密码。
