# atv-player

基于 `PySide6` 和 `mpv` 的 `alist-tvbox` 桌面播放器，当前以 Linux 为优先目标平台，同时保留 macOS 和 Windows 打包支持。

应用默认连接 `http://127.0.0.1:4567`，围绕 `alist-tvbox` 后端提供登录、媒体浏览、豆瓣电影、电报影视、网络直播、Emby、Jellyfin、播放记录，以及独立播放器窗口。

## 功能概览

### 主界面

- 登录后自动保存后端地址、用户名、令牌和 `vod token`
- 主界面包含 `豆瓣电影`、`电报影视`、`网络直播`、`文件浏览`、`播放记录`
- 根据后端能力自动显示 `Emby` 和 `Jellyfin` 标签页
- 支持加载 `TvBox Python` 爬虫插件，并为每个启用插件生成独立标签页
- 浏览页会记住最近路径，支持本地排序和搜索跳转

### 播放器

- 使用独立播放器窗口播放视频，而不是把播放器嵌入主页面
- 支持播放列表、上一集/下一集、拖动进度、悬浮时间提示、音量、静音、倍速
- 支持片头/片尾跳过时长设置
- 支持字幕轨、次字幕、音轨选择
- 支持播放器右键菜单调整主字幕/次字幕的位置与大小
- 支持恢复上次播放状态，并定时向后端上报进度
- 支持播放详情、海报、播放日志和自动隐藏鼠标光标

### 网络直播

- 内置一个默认远程直播源
- 支持添加远程 `M3U`、本地 `M3U`、手动维护三类直播源
- 支持直播源启用/禁用、重命名、删除、刷新
- 手动直播源支持频道分组、`Logo URL`、顺序调整
- 会合并同组下同名频道，并把多线路组织成一个播放列表
- 解析 `tvg-logo`、`group-title` 和 `http-header` / `http-user-agent` 等 `M3U` 属性

### 插件

- 支持本地和远程 `TvBox Python` 爬虫插件
- 支持插件启用/禁用、重命名、上移/下移、刷新、删除、查看加载日志
- 远程插件会缓存到本地后再加载

远程插件会执行本地 Python 代码，只应加载受信任来源。

## 环境要求

- Python `3.12+`
- `uv`
- 可用的 `libmpv`
- 一个可访问的 `alist-tvbox` 后端

Linux 上如果系统里没有 `libmpv`，运行和打包都会失败。`build.py` 会在常见系统目录查找它。

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

## 快捷键

主窗口：

- `F1`: 打开快捷键帮助
- `Ctrl+P` / `Esc`: 显示或返回播放器
- `Ctrl+Q` 或系统对应退出快捷键: 退出应用

播放器窗口：

- `Space`: 播放/暂停
- `Enter`: 切换全屏
- `Ctrl+P`: 返回主窗口
- `Esc`: 退出全屏或返回主窗口
- `PgUp` / `PgDn`: 上一集 / 下一集
- `Left` / `Right`: 后退 / 前进 15 秒
- `Ctrl+Left` / `Ctrl+Right`: 后退 / 前进 60 秒
- `Up` / `Down`: 调整音量
- `M`: 静音
- `-` / `+` / `=`: 降低倍速 / 提高倍速 / 恢复 `1.0x`

## 本地数据

应用使用 `Qt` 的标准数据目录和缓存目录。Linux 上通常分别是：

```text
~/.local/share/atv-player
~/.cache/atv-player
```

主要文件和目录：

- 配置数据库：`~/.local/share/atv-player/app.db`
- 插件缓存：`~/.local/share/atv-player/plugins/cache`
- 海报缓存：`~/.cache/atv-player/posters`

应用通过本地 `sqlite` 保存以下状态：

- 后端地址
- 用户名
- 登录令牌和 `vod token`
- 最近浏览路径
- 上次活跃窗口
- 上次播放来源和恢复信息
- 播放器音量、静音状态
- 主窗口、浏览页、播放器窗口布局状态
- 直播源、手动频道和插件配置
- 插件加载日志

应用不会保存密码。

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
- `packaging`: 各平台图标和打包资源
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
