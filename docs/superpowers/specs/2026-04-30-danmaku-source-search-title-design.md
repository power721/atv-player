# 弹幕源对话框搜索标题拆分与持久化设计

## 概要

将播放器中的弹幕源对话框从“单个搜索词输入框”调整为“媒体标题 + 集数”两个独立输入项。搜索时仍然拼接为现有弹幕搜索服务可接受的单个查询串，但系列级持久化只保存用户确认成功的“媒体标题”修改，`集数` 继续按当前播放项单独维护，不跨集、不跨会话持久化。

## 目标

- 在弹幕源对话框中分开展示媒体标题和集数。
- 允许用户分别修改媒体标题和集数后重新搜索弹幕源。
- 仅在成功路径上按系列保存用户修改后的媒体标题。
- 后续同系列剧集再次打开弹幕源时，自动复用已保存的媒体标题。
- 保持当前集数默认推断与弹幕搜索排序逻辑不变。

## 非目标

- 不修改弹幕 provider 的搜索与解析算法。
- 不保存用户手动修改的集数。
- 不把媒体标题保存到 `AppConfig`。
- 不新增跨系列或全局搜索词管理界面。

## 状态模型

### PlayItem

为当前播放项增加两个显式字段：

- `danmaku_search_title: str`
- `danmaku_search_episode: str`

现有字段保留：

- `danmaku_search_query`
- `danmaku_search_query_overridden`

其中：

- `danmaku_search_title` 表示当前 UI 中正在编辑或使用的媒体标题。
- `danmaku_search_episode` 表示当前 UI 中正在编辑或使用的集数字符串。
- `danmaku_search_query` 改为派生值，每次执行搜索前由 `标题 + 集数` 拼接得到。
- `danmaku_search_query_overridden` 继续表示当前播放项是否使用了用户手动修改过的搜索输入。

### 系列偏好

扩展 `DanmakuSeriesPreference` 与对应存储内容，新增：

- `search_title: str = ""`

持久化结构变为：

```json
{
  "series_key": {
    "provider": "tencent",
    "page_url": "https://v.qq.com/...",
    "title": "剑来 第12集",
    "search_title": "剑来",
    "updated_at": 1770000000
  }
}
```

语义：

- `provider` / `page_url` / `title` 继续用于记忆成功弹幕源。
- `search_title` 用于同系列后续剧集的默认媒体标题输入值。

## 默认值规则

### 媒体标题

默认媒体标题按以下优先级确定：

1. 当前系列已保存的 `search_title`
2. 当前 `PlayItem.media_title`
3. 现有回退逻辑生成的标题部分

### 集数

默认集数始终来自当前播放项的自动推断结果：

- 使用现有 `_extract_episode_label(...)` 逻辑
- 若当前会话内用户已手动修改当前 `PlayItem.danmaku_search_episode`，则对话框重新打开时显示该值
- 不写入系列偏好

## UI 设计

弹幕源对话框顶部区域由一个输入框改为两个输入框：

- `媒体标题` 输入框
- `集数` 输入框

交互保持现有按钮结构：

- `重新搜索`
- `恢复默认搜索词`
- `切换并加载`

行为：

- `重新搜索` 使用两个输入框的值拼成查询串后重新搜索。
- `恢复默认搜索词` 将媒体标题恢复为“已保存系列标题，否则当前媒体标题”，并将集数恢复为当前集自动推断值。
- `切换并加载` 仅切换当前选中的弹幕源，但如果这次切换成功，也要把当前媒体标题写入系列偏好。

## 查询拼接规则

对外搜索仍保持单字符串查询：

- 先去除标题和集数两端空白
- 过滤空字符串
- 按空格拼接

示例：

- `标题="剑来"`，`集数="12集"` -> `剑来 12集`
- `标题="新闻联播"`，`集数=""` -> `新闻联播`

## 控制器流程

### 默认搜索词初始化

控制器新增或调整内部辅助逻辑，分别计算：

- 默认媒体标题
- 默认集数
- 完整查询串

当播放项首次触发弹幕搜索或打开弹幕源对话框时：

1. 计算 `series_key`
2. 读取系列偏好
3. 用偏好中的 `search_title` 回填 `PlayItem.danmaku_search_title`
4. 用当前播放项推断结果回填 `PlayItem.danmaku_search_episode`
5. 拼接得到 `PlayItem.danmaku_search_query`

### 重新搜索

`refresh_danmaku_sources(...)` 接口改为接收拆分后的 override 输入，或新增内部辅助参数：

- `search_title_override`
- `search_episode_override`

内部流程：

1. 确定当前使用的标题与集数
2. 回写到 `PlayItem`
3. 拼接 `query_name`
4. 调用现有搜索逻辑
5. 如果搜索成功并产生候选结果，则将当前标题写入系列偏好
6. 如果搜索失败或抛错，不写系列偏好

这里的“成功”定义为：

- 调用未抛出异常
- 获得了可应用到 `PlayItem` 的搜索结果

### 切换弹幕源

`switch_danmaku_source(...)` 成功解析后，继续保存原有系列偏好字段，并额外写入当前 `PlayItem.danmaku_search_title`。

失败时：

- 保持当前对话框输入值不变
- 不更新系列偏好

## 恢复默认搜索词

恢复默认时：

- `PlayItem.danmaku_search_title` 恢复为“系列已保存标题，否则 `item.media_title`”
- `PlayItem.danmaku_search_episode` 恢复为当前播放项自动推断结果
- `PlayItem.danmaku_search_query` 更新为拼接后的默认值
- 重新执行搜索

这一步本身不直接写系列偏好，仍以搜索成功或切换成功为落盘条件。

## 兼容性

- 旧的 `danmaku-series-preferences.json` 可能没有 `search_title` 字段，读取时需要兼容缺失字段并回退为空字符串。
- 旧测试和调用路径若仍只依赖 `danmaku_search_query`，应保持其可用，只是其来源改为显式拼接。

## 验证

需要覆盖的回归测试：

- 对话框显示“媒体标题”和“集数”两个输入框，默认值分别正确。
- 修改媒体标题和集数后点击 `重新搜索`，控制器收到拼接后的查询串。
- `重新搜索` 成功时保存 `search_title`；失败时不保存。
- `切换并加载` 成功时保存 `search_title`。
- 下次打开同系列其他剧集时，媒体标题回填为已保存值，而集数根据当前剧集重新推断。

## 涉及文件

- `src/atv_player/models.py`
- `src/atv_player/danmaku/models.py`
- `src/atv_player/danmaku/preferences.py`
- `src/atv_player/plugins/controller.py`
- `src/atv_player/ui/player_window.py`
- `tests/test_player_window_ui.py`
- 相关偏好存储测试文件（如当前缺失则新增）
