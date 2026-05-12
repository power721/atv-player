# GitHub Plugin Import Cancel Design

## Summary

当前插件管理里的“从 GitHub 导入”会在 UI 线程里同步执行整个仓库导入流程。虽然已有进度对话框，但它被配置为无取消按钮，用户一旦开始导入，只能等待全部仓库条目处理完成。

本次改动目标是在不回滚已完成结果的前提下，为 GitHub 仓库导入增加可见、可预测的取消能力。取消后不再继续处理后续插件，但已经完成的新增、更新和刷新结果保留，并立即反映到插件列表中。

## Goals

- 为“从 GitHub 导入”进度对话框提供取消入口。
- 取消后停止后续导入，不回滚已完成的新增或更新。
- 区分“取消”和“失败”两种结束状态。
- 保持现有导入摘要能力，并在取消时展示部分结果摘要。
- 保持改动局限在现有同步导入架构内，不引入后台线程。

## Non-Goals

- 不中断单个已经发出的 HTTP 请求。
- 不回滚导入开始前已经完成的数据库写入或插件刷新。
- 不把 GitHub 导入整体改造成后台任务。
- 不改变原有 GitHub manifest 解析、版本比较或去重规则。

## Current Flow

当前 UI 位于 [plugin_manager_dialog.py](/home/harold/workspace/atv-player/src/atv_player/ui/plugin_manager_dialog.py:406)：

1. 用户输入 GitHub 仓库 URL。
2. UI 创建 `QProgressDialog`，但通过 `setCancelButton(None)` 关闭取消按钮。
3. UI 同步调用 `SpiderPluginManager.import_github_repository(...)`。
4. manager 依次解析默认分支、下载 `spiders_v2.json`、逐项抓取源码、写库并刷新插件。
5. UI 最终展示“导入完成”或“导入失败”。

因此当前不存在“用户请求停止后续导入”的控制点。

## Proposed Behavior

### Cancellation Semantics

取消定义为“尽力停止后续处理”，不是事务回滚：

- 已经完成的新增插件记录保留。
- 已经完成的版本更新保留。
- 已经完成的 `refresh_plugin(...)` 副作用保留。
- 取消触发后，不再开始处理新的 manifest 条目。

这意味着取消结果可能是部分成功，符合用户对“停止后续导入，但已完成的导入/更新保留”的要求。

### UI Behavior

GitHub 导入进度对话框改为显示标准取消按钮，按钮文案使用 Qt 默认值即可。用户点击取消后：

1. `QProgressDialog.wasCanceled()` 变为 `True`。
2. UI 通过一个纯 Python 取消检查函数传给 manager。
3. manager 在下一个检查点发现取消请求后终止流程。
4. UI 关闭进度对话框，刷新插件列表，并展示“已取消”摘要。

取消不是错误，因此不弹“导入失败”警告框。

### Manager Contract

`SpiderPluginManager.import_github_repository(...)` 新增可选取消检查参数，例如：

- `cancel_callback: Callable[[], bool] | None = None`

manager 不依赖 Qt 类型，只在自己的检查点调用该回调；若回调返回 `True`，则抛出专用取消异常，例如：

- `SpiderPluginImportCancelled`

该异常实例需要携带当前累计结果，至少包含：

- `imported_count`
- `updated_count`
- `skipped_count`

这样 UI 可以复用已有摘要逻辑，而不必自己猜测处理进度。

## Cancellation Checkpoints

取消不会打断正在执行的单次网络请求，因此需要在明确边界上检查：

1. 解析默认分支前
2. 下载 `spiders_v2.json` 前
3. 每个 manifest 条目开始前
4. 每个条目源码下载前
5. 每个条目写库和刷新前

推荐最小实现至少覆盖：

- 整体流程启动前
- 每个条目循环开始处
- 刷新前

这样用户点击取消后，最晚会在当前条目完成后停止，不会继续进入下一个条目。

## Result Reporting

### Completed

无取消、无整体错误时，仍保持现有提示：

- `导入完成：新增 N 个，更新 M 个，跳过 K 个。`

### Cancelled

取消时改为信息提示而不是警告提示：

- `已取消：新增 N 个，更新 M 个，跳过 K 个。`

该提示表示：

- 用户主动结束流程
- 当前摘要只覆盖取消前已经处理完成的条目

### Failed

整体错误仍维持现有行为：

- 非法仓库 URL
- 默认分支解析失败
- manifest 下载或解析失败

这些情况继续走“导入失败”警告框，不混淆为取消。

## Architecture

### Models

需要新增一个专用异常类型，建议放在 [models.py](/home/harold/workspace/atv-player/src/atv_player/models.py) 与现有导入结果类型相邻，保持导入流程相关的数据结构集中。

异常至少包含：

- `result: SpiderPluginImportResult`

异常消息可以固定为 `已取消导入`，避免 UI 自行拼接错误文案。

### Manager

[plugins/__init__.py](/home/harold/workspace/atv-player/src/atv_player/plugins/__init__.py) 负责：

- 接收 `cancel_callback`
- 在检查点调用取消检查
- 取消时抛出 `SpiderPluginImportCancelled(result)`

取消不应被当作单条插件失败计入 `skipped_count`。`skipped_count` 仍然只表示按现有规则跳过的条目，例如非法路径、同地址同版本等。

### Dialog

[plugin_manager_dialog.py](/home/harold/workspace/atv-player/src/atv_player/ui/plugin_manager_dialog.py) 负责：

- 恢复 `QProgressDialog` 的取消按钮
- 传入 `cancel_callback=lambda: progress.wasCanceled()`
- 捕获取消异常并显示取消摘要
- 不论完成或取消，都执行一次 `reload_plugins()`

`reload_plugins()` 不能只在完全成功时调用，因为取消后用户仍需要立刻看到已完成的部分导入结果。

## Testing

需要覆盖：

- manager 在取消回调触发后停止后续条目处理。
- manager 取消时抛出专用异常，并携带当时的累计结果。
- 取消不会把未开始处理的条目计入 `skipped_count`。
- 对话框进度窗口存在取消按钮，不再调用 `setCancelButton(None)`。
- 对话框取消后展示“已取消”摘要，而不是“导入失败”。
- 对话框取消后仍然刷新插件列表。

## Risks

- 如果取消被计入 `skipped_count`，摘要语义会混乱。
- 如果 UI 只在成功时 `reload_plugins()`，取消后的部分结果会对用户不可见。
- 如果 manager 在过少的检查点才轮询取消，用户会误以为取消失效。

## Recommendation

采用“UI 取消按钮 + manager 轮询取消回调 + 专用取消异常”的方案。

这是当前同步导入架构下最小且行为最清晰的实现：

- 不引入线程复杂度
- 不污染 manager 与 Qt 的边界
- 能准确表达“停止后续导入，但保留已完成结果”
