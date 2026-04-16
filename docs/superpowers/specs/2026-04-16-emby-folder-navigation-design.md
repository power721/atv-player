# Emby Folder Navigation Design

## Goal

让 `Emby` 首页列表支持两种点击行为：

- `vod_tag=folder`：保留在 `Emby` tab 内进入下一层目录
- 其他项：按现有详情接口直接构建播放请求并打开播放器

## Existing Context

- `Emby` 目前复用 [`src/atv_player/ui/douban_page.py`](/home/harold/workspace/atv-player/src/atv_player/ui/douban_page.py) 的海报墙 UI。
- 当前卡片点击只会通过 `open_requested(vod_id)` 向外发送 `vod_id`，不足以让外层根据 `vod_tag` 区分“文件夹”和“文件”。
- `MainWindow` 当前把所有 `Emby` 点击都交给 `EmbyController.build_request()`，因此文件夹也会被错误地当成可播放详情处理。

## Design

### UI boundary

`DoubanPage` 增加 item 级点击信号，保持现有 `open_requested(str)` 兼容不变。

- `Telegram` 继续使用 `open_requested(vod_id)`。
- `Emby` 改为监听新的 item 级信号，从完整 `VodItem` 上读取 `vod_tag` 决定下一步动作。

### Emby interaction flow

`MainWindow` 只对 `Emby` 增加点击分流：

- `vod_tag == "folder"`：调用 `EmbyController` 进入该文件夹，并让当前 `Emby` 页加载该目录内容
- 否则：沿用当前 `build_request(vod_id)` 直接打开播放器

该行为同时作用于分类列表和搜索结果。

### Emby controller/API

`EmbyController` 增加“按目录 id 加载目录内容”的能力，复用 `/emby/{token}?ids=<vod_id>` 接口。

- 当接口返回目录列表时，映射为 `VodItem` 列表并在当前页面展示
- 当接口返回文件详情时，仍由 `build_request(vod_id)` 负责解析播放列表并进入播放器

## Testing

- 控制器测试：验证目录加载会把 `/emby/{token}?ids=<vod_id>` 返回的 `list` 映射为页面项
- 页面/主窗口测试：验证 `Emby` 点击 `folder` 时不会直接播放，而是调用当前页进入目录；点击 `file` 时仍会打开播放器
- 回归：`Telegram` 与普通 `DoubanPage open_requested` 行为保持不变
