# 插件级弹幕能力切换设计

## 目标

将弹幕能力开关从 `playerContent()` 返回值中的 `"danmu": True` 移动到插件级 `Spider.danmaku()`。

最终行为：

- 只有 `Spider.danmaku()` 返回 `True` 的插件会触发弹幕搜索与解析。
- `playerContent()` 返回内容中的 `"danmu"` 字段被完全忽略。
- 未实现 `danmaku()` 的旧插件继续按基类默认值 `False` 处理，不启用弹幕。

## 设计

### 能力来源

`SpiderPluginController` 在初始化时读取一次插件实例的 `danmaku()` 返回值，并保存为控制器级私有布尔标记。

实现要求：

- 使用 `getattr(spider, "danmaku", lambda: False)()` 获取能力。
- 使用 `bool(...)` 归一化结果。
- 不在每次播放解析时重复调用。

### 触发条件

`_maybe_resolve_danmaku()` 的入口条件改为：

- 控制器级弹幕能力标记为 `True`
- `danmaku_service` 已注入
- 当前条目可以构造搜索名

不再读取：

- `payload.get("danmu")`

### 播放链路影响

`playerContent()` 仍只负责返回播放地址、解析标记和请求头等播放信息。

弹幕是否启用不再由单集返回值控制，因此同一插件下：

- 开启弹幕能力后，所有播放项都按现有搜索策略尝试解析弹幕
- 弹幕搜索/解析失败仍只记日志，不影响正常播放

## 兼容性

这是一次明确的行为切换，不保留旧的 `"danmu"` 兼容路径。

影响：

- 旧插件即使继续返回 `"danmu": True`，也不会再触发弹幕
- 插件作者需要改为覆盖 `Spider.danmaku()` 并返回 `True`

## 测试

需要更新和补充以下测试：

- 控制器在 `Spider.danmaku() == True` 时触发弹幕解析
- 控制器在 `Spider.danmaku() == False` 时，即使 `playerContent()` 返回 `"danmu": True` 也不触发
- 弹幕解析失败不影响播放的现有回归测试继续保留

## 范围控制

本次仅调整插件级弹幕开关来源，不改：

- 弹幕搜索算法
- 弹幕渲染逻辑
- 插件数据库结构
- 插件管理 UI
