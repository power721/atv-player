# Emby Playback Design

## Goal

让 `Emby` 播放改走 `/emby-play/{token}` 协议：

- `t=0` 获取真实播放 URL 和请求头
- `t=<毫秒>` 上报播放进度
- `t=-1` 停止播放

并且 `Emby` 不再读写本地历史记录。

## Existing Context

- 当前 `Emby` 详情接口 `/emby/{token}?ids=...` 只用于拿详情和播放列表。
- 当前播放器统一通过 `PlayerController` 读写本地 `history`，没有来源级别的播放钩子。
- `MpvWidget.load()` 也还不能接收来源返回的 HTTP 请求头。

## Design

### Session-level playback hooks

给 `OpenPlayerRequest` / `PlayerSession` 增加可选播放钩子与策略字段：

- 是否启用本地历史
- 播放前加载器：按 `PlayItem.vod_id` 获取真实 URL 和 headers
- 进度上报器
- 停止上报器

这样 `PlayerWindow` 只消费会话能力，不需要硬编码来源名称。

### Emby controller responsibilities

`EmbyController` 继续负责：

- `/emby/{token}` 分类、目录、搜索、详情
- 详情页 `vod_play_url` 解析出播放列表项 `vod_id`

同时新增 `Emby-play` 适配职责：

- 根据 `PlayItem.vod_id` 调 `/emby-play/{token}?t=0&id=...` 获取真实 URL 和 header
- 根据当前播放位置调用 `/emby-play/{token}?t=<ms>&id=...`
- 在切集、关闭、返回主界面、自然播放结束时调用 `/emby-play/{token}?t=-1&id=...`

### Player responsibilities

- 打开条目前，先通过会话的播放前加载器把 `PlayItem.url` 与 headers 填好，再交给 mpv
- 若会话禁用本地历史，则 `PlayerController` 不再调用 `get_history()/save_history()`
- 在切换条目前，先对旧条目做最后一次进度上报，再发送停止

### mpv integration

`MpvWidget.load()` 新增可选 `headers` 参数，并把它们映射到 mpv 的 `http-header-fields` 选项。

当前 `/emby-play` 返回的 `subs` 暂不接入播放器字幕逻辑，先忽略。

## Testing

- API：验证 `/emby-play` 的 `t=0` / `t=<ms>` / `t=-1` 查询参数
- 控制器：验证 `Emby` 请求会禁用本地历史并提供播放钩子
- PlayerController：验证禁用本地历史时不会访问 `history`，但仍会调用来源钩子
- PlayerWindow：验证打开 `Emby` 条目时会先取真实 URL 和 headers，再加载到视频控件；切集/关闭时会发送停止
