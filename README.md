# atv-player

基于 `PySide6` 和 `mpv` 的 `alist-tvbox` 桌面播放器，当前以 Linux 为优先目标平台，同时保留 macOS 和 Windows 打包支持。

应用默认连接 `http://127.0.0.1:4567`，围绕 `alist-tvbox` 后端提供登录、媒体浏览、播放记录、网络直播、插件扩展和独立播放器窗口。

## 文档导航

- [详细帮助文档](docs/help.md)
- [Python Spider 插件开发指南](docs/python-spider-plugin-development-guide.md)

## 核心能力

### 内容入口

- 根据后端能力自动显示豆瓣电影、电报影视、B站、YouTube、网络直播、Emby、Jellyfin、飞牛影视、盘搜、文件浏览、播放记录等标签页
- 为每个启用的 `TvBox Python` 爬虫插件生成独立标签页，插件过多时自动收入“更多”抽屉并支持搜索
- 顶部全局搜索会并发搜索所有已启用来源，并在标签标题中显示结果数量
- 全局搜索支持直接打开几类地址：
  - `magnet:?` / `ed2k://`：走离线下载流程
  - 常见网盘分享链接：进入网盘详情/解析流程
  - `YouTube` 页面链接或 11 位视频 id：通过系统 `yt-dlp` 解析后拉起播放器
  - 普通 `HTTP/HTTPS` 链接：走内置全局解析并直接拉起播放器
- 内置 YouTube 标签页会在检测到系统 `yt-dlp` 可用时显示，支持内置分类、搜索、频道、播放列表、账号相关列表和自定义分类配置

### 浏览与历史

- 豆瓣电影、电报影视、飞牛影视采用海报网格浏览，支持分类、筛选、分页
- YouTube 支持首页推荐、热门、最近更新、直播、影视、音乐、频道、播放列表等入口；配置浏览器 Cookie 后还会显示订阅、历史和稍后再看
- B站、Emby、Jellyfin 支持目录层级导航与播放恢复
- 文件浏览页支持面包屑、排序、分页和按网盘类型筛选
- 播放记录支持分页、删除、清空和双击恢复，YouTube 也会按视频、频道或播放列表保存恢复信息

### 播放器

- 独立播放器窗口，支持播放/暂停、快进快退、音量、倍速、上一集/下一集、全屏和宽屏模式
- 支持多来源分组、多线路播放列表、自动连播和播放恢复
- 播放列表根据可用字段支持原始顺序、名称、大小、评分和时间排序，并在排序后保持当前播放项
- 支持主字幕、次字幕、外挂字幕、音轨选择、DASH 清晰度切换
- 支持从外部字幕站搜索并加载字幕：内置 `SubDL`、`SubHD`、`字幕库`、`射手网(伪)`、`SubSource`、`OpenSubtitles` 六个来源，按发布文件名解析出片名/季集/画质/片源/编码后并发搜索，并按匹配度排序（简英双语优先）
- YouTube / `yt-dlp` 播放支持默认画质上限、清晰度切换、多音轨切换、外部字幕加载、详情字段回填和短时解析缓存
- 支持弹幕搜索、弹幕来源切换、弹幕渲染设置和缓存
- 支持媒体刮削，可手动搜索并补充影片元数据（海报、简介、评分、演员等），也可自动增强
- 详情侧栏可显示海报、元数据、插件自定义动作和播放日志
- 解析型播放项会启用”解析”下拉框，可切换并记住偏好的内置解析器

### 网络直播

- 内置默认远程直播源
- 支持远程 M3U、本地 M3U/`txt`、手动维护三类直播源
- 支持多行 EPG URL 配置与手动立即更新
- 解析 `group-title`、`tvg-logo`、`http-user-agent`、`http-header`
- 会合并同组下同名频道，将多条线路组织成一个播放列表

### 插件

- 支持本地和远程 `TvBox Python` 爬虫插件
- 支持添加、启用/禁用、重命名、编辑配置、刷新、删除、查看日志
- 支持 GitHub 仓库和 `spiders_v2.json` 清单 URL 导入，显示新增/更新/跳过摘要
- 支持上移/下移与排序对话框，排序对话框支持置顶/置底和拖拽移动
- 插件可提供自定义管理动作与播放器详情动作

> 远程插件会执行本地 Python 代码，只应加载受信任来源。

### 本地能力

- 内置本地 HLS 代理（默认 `127.0.0.1:2323`），用于 M3U8 重写、广告片段过滤、TS 分片缓存和 DASH 处理
- 支持远程蓝光 ISO 的 HTTP range 读取、UDF 解析、播放列表检测和流式播放
- 支持 QQ 音乐 `QRC`、酷狗 `KRC`、网易 `YRC` 卡拉 OK 歌词解析与 ASS 渲染

## 环境要求

- Python `3.12+`
- `uv`
- 可用的 `libmpv`
- 一个可访问的 `alist-tvbox` 后端
- 可选：系统中单独安装 `yt-dlp`，用于显示 YouTube 标签页和直接播放 YouTube 等页面链接

Linux 上如果系统里没有 `libmpv`，运行和打包都会失败。`build.py` 会在常见系统目录查找它。

`yt-dlp` 不随发行包打包。安装后可用下面命令确认：

```bash
yt-dlp --version
```

如果你需要单独构建较新的 `mpv/libmpv` 运行时，可使用仓库内的辅助脚本：

```bash
scripts/build_mpv.sh
```

常见用法：

```bash
scripts/build_mpv.sh --disable-x86asm
scripts/build_mpv.sh --disable-x86asm --no-install --dry-run
scripts/build_mpv.sh --master
```

说明：

- 默认使用 `mpv-build` 的 release 轨道构建 `mpv/libmpv`
- 默认执行 `sudo ./install`
- 默认启用 FFmpeg 的 `libxml2`，确保 DASH/MPD 解复用可用；缺少开发包时会自动安装 `libxml2-dev`
- 如果缺少 Lua 开发包，脚本会在 `apt-get` 可用时自动执行 `sudo apt-get install -y liblua5.2-dev`
- 如果缺少硬件解码相关开发包，脚本会在 `apt-get` 可用时自动执行 `sudo apt-get install -y libva-dev libvdpau-dev`
- 如果缺少 NVIDIA codec headers，脚本会在 `apt-get` 可用时自动执行 `sudo apt-get install -y libffmpeg-nvenc-dev`
- 如果当前桌面会话是 `X11` 且缺少 `xpresent` 开发包，脚本会自动执行 `sudo apt-get install -y libxpresent-dev`
- 安装完成后建议先执行 `hash -r`，再用 `/usr/local/bin/mpv --version` 和 `ldconfig -p | grep libmpv` 确认新运行时已生效
- 如果遇到 `nasm not found or too old`，可先使用 `--disable-x86asm`
- `--dry-run` 只打印构建命令，便于先检查流程

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
uv run atv-player
```

## 首次使用

1. 启动 `alist-tvbox` 后端。
2. 打开播放器，在登录页输入后端地址、用户名和密码。
3. 登录成功后进入主窗口；应用会记住后端地址、用户名、令牌和上次打开状态，但不会保存密码。
4. 通过顶部标签页或全局搜索进入内容详情，双击或点击播放项拉起独立播放器。
5. 在主窗口或播放器窗口按 `F1` 可以随时打开快捷键帮助。

更完整的界面说明、操作流程、直播源格式示例和排障信息见 [详细帮助文档](docs/help.md)。

## 快捷键速览

### 主窗口

| 快捷键 | 说明 |
|--------|------|
| `F1` | 打开快捷键帮助 |
| `Ctrl+P` | 显示或返回播放器 |
| `Esc` | 显示或返回播放器 |
| `Ctrl+Q` | 退出应用 |

### 播放器窗口

| 快捷键 | 说明 |
|--------|------|
| `F1` | 打开快捷键帮助 |
| `Space` | 播放/暂停 |
| `Enter` | 切换全屏 |
| `W` | 切换宽屏 |
| `D` | 打开弹幕源 |
| `S` | 打开刮削 |
| `C` | 搜索外部字幕 |
| `Ctrl+D` | 打开弹幕设置 |
| `I` | 显示视频信息 |
| `Ctrl+P` | 返回主窗口 |
| `Esc` | 退出全屏或返回主窗口 |
| `PgUp` | 播放上一集 |
| `PgDn` | 播放下一集 |
| `Left` | 后退 15 秒 |
| `Right` | 前进 15 秒 |
| `Ctrl+Left` | 后退 60 秒 |
| `Ctrl+Right` | 前进 60 秒 |
| `Up` | 音量增加 |
| `Down` | 音量减小 |
| `M` | 静音 |
| `-` | 降低倍速 |
| `+` | 提高倍速 |
| `=` | 恢复 `1.0x` |
| `Ctrl+Q` | 退出应用 |

## 本地数据

应用使用 `Qt` 的标准数据目录和缓存目录。Linux 上通常分别是：

```text
~/.local/share/atv-player
~/.cache/atv-player
```

常见文件和目录：

- 配置数据库：`~/.local/share/atv-player/app.db`
- 应用日志：`~/.local/share/atv-player/logs/application.jsonl`
- 插件缓存：`~/.cache/atv-player/plugins`
- 海报缓存：`~/.cache/atv-player/posters`
- 弹幕搜索缓存：`~/.local/share/atv-player/danmaku-search-cache.json`
- 外部字幕缓存：`~/.cache/atv-player/subtitles`
- 弹幕系列偏好：`~/.local/share/atv-player/danmaku-series-preferences.json`
- 元数据缓存：`~/.cache/atv-player/metadata`
- 元数据手动绑定：保存在 `~/.local/share/atv-player/app.db` 的 `metadata_bindings` 表中

应用会持久化以下状态：

- 后端地址、用户名、登录令牌和 `vod token`
- 上次活跃窗口、上次活跃标签页、分类和浏览路径
- 上次播放来源、恢复信息、播放器布局、宽屏模式和日志可见性
- 播放器音量、静音状态、偏好解析器、MPV 渲染模式、缓存、网络超时、普通流预读和额外 MPV 选项
- 直播源、手动频道、EPG 配置
- 插件配置、缓存路径和加载日志
- 弹幕偏好（启用、行数、显示模式、颜色、位置、速率、字号）
- 元数据增强配置（启用状态、TMDB API Key、TMDB 代理地址、Bangumi Token、豆瓣 Cookie、剧集标题增强；代理已隐藏 Key 时可不填 TMDB API Key）
- 字幕站配置（启用的字幕站、SubDL API Key、射手网 Token、OpenSubtitles API Key）
- 元数据手动绑定记录
- YouTube 偏好（Cookie 浏览器、默认画质、默认字幕、默认音轨、元数据语言、地区、分类配置源和分类缓存）

高级设置的 TMDB 接入可选择官方 API、内置 Cloudflare Worker 代理或自定义代理地址，并可对内置接入执行测速。

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

- `src/atv_player`：应用代码
- `tests`：单元测试和 UI 测试
- `packaging`：各平台图标和打包资源
- `docs`：用户文档、设计说明与实现计划

## 打包

本地打包和 GitHub Actions 共用同一个入口：

```bash
uv sync --group dev --group build
uv run python build.py current
```

也可以显式指定目标平台：

```bash
uv run python build.py linux
uv run python build.py macos
uv run python build.py windows
```

各平台输出规则：

- Linux：先生成 `PyInstaller` 目录包，再封装成 `AppImage`
- macOS：生成 `.app`
- Windows：生成单文件 `.exe`

Linux 打包额外要求：

- 系统里需要 `appimagetool`
- 系统里需要可用的 `libmpv`

Windows 打包时，`build.py` 会优先从这些位置查找 `mpv` 运行库：

- 环境变量 `ATV_MPV_RUNTIME_DIR`
- 仓库根目录下的 `mpv/`
- 当前 `PATH`

GitHub Actions 会为 Pull Request 和手动触发构建 Linux、macOS（Apple Silicon `arm64` 与 Intel `x64` 双架构）、Windows 制品；推送以 `v` 开头的标签时，还会创建 GitHub Release 并上传产物。

macOS 双架构说明：

- Apple Silicon 构建通过 Homebrew 安装 `mpv`（产物要求 macOS 26+，随 Homebrew bottle 的最低系统版本浮动）
- Intel 构建跑在 `macos-15-intel` runner 上，`libmpv` 来自 MacPorts 官方预编译归档（产物要求 macOS 15+），整个 `/opt/local` 由 CI 缓存
- macOS 侧 `build.py` 按 `/opt/homebrew` → `/opt/local` → `/usr/local` 的顺序探测 `libmpv`

## 环境变量

| 变量 | 说明 |
|------|------|
| `ATV_MPV_DEBUG` | 启用 mpv 调试日志输出 |
| `ATV_GPU_VENDOR` | 覆盖自动渲染模式的 GPU 厂商探测，可选 `nvidia`、`amd`、`intel`、`unknown` |
| `ATV_MPV_RUNTIME_DIR` | Windows 打包时指定 mpv 运行库目录 |
| `ATV_YTDLP_PATH` | 指定系统 `yt-dlp` 可执行文件路径；留空时从 PATH 查找 |
| `ATV_YTDLP_COOKIES_FROM_BROWSER` | 为 `yt-dlp` 指定浏览器 Cookie 来源，例如 `chrome`、`edge`、`firefox`；高级设置中的 YouTube Cookie 会优先用于应用内解析 |

## 自定义 libmpv

应用内置的 `libmpv` 在个别机器上可能无法加载（例如老 CPU 不满足新版构建的指令集要求）。此时把一份可用的 libmpv 放到以下任一目录（按优先级），应用启动时会优先加载它，无需替换应用目录里的内置文件：

1. 用户目录下的 `mpv` 目录，例如 `~/mpv/libmpv-2.dll`
2. 应用目录下的 `lib` 子目录，例如 `<应用目录>/lib/libmpv-2.dll`
3. 应用目录本身，例如 `<应用目录>/libmpv-2.dll`

目录内按 `libmpv-2.dll`、`mpv-2.dll`、`mpv.dll` 的顺序查找（Linux/macOS 对应 `libmpv.so`、`libmpv.so.2`、`libmpv.dylib`）。候选按优先级逐个尝试；某个文件加载失败会在日志中记录原因并继续尝试下一个，全部失败才回退到内置 libmpv。
