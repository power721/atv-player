# 插件开发指南

本文面向 `atv-player` 的 Python Spider 插件开发者，说明宿主当前真正支持的接口、数据结构、播放器扩展能力，以及哪些传统 `TVBox`/`Atvp` 习惯写法在这里并不会生效。

如果你手上已经有旧爬虫，这篇文档可以帮助你判断：

- 哪些写法可以直接复用
- 哪些字段会被 `atv-player` 读取
- 哪些字段只是兼容保留、当前宿主并不接线

## 1. 先看结论

### 当前 `atv-player` 直接读取并使用的核心接口

- `init(self, extend="")`
- `getName(self)`
- `homeContent(self, filter)`
- `categoryContent(self, tid, pg, filter, extend)`
- `searchContent(self, key, quick, pg=1, category="")`
- `detailContent(self, ids)`
- `playerContent(self, flag, id, vipFlags)`
- `danmaku(self)`
- `getManagerActions(self)`
- `runManagerAction(self, action_id, context)`
- `runPlayerAction(self, action_id, context)`

### 当前宿主会读取的播放扩展字段

`playerContent(...)` 返回值里，`atv-player` 当前会使用这些字段：

- `url`
- `parse`
- `header`
- `cover`
- `qualities`
- `subt`
- `lyric`
- `danmaku`
- `actions`
- `ext`

### 兼容接口和宿主特定字段里，当前 `atv-player` 直加载模式不会读取或不会产生效果的部分

- `jx`
- `playUrl`
- `playerContent()["danmu"]`
- `localProxy(...)`
- `self.backend_parse = True`（这是 `alist-tvbox` 后端提供的 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 兼容运行层使用的特殊配置，`atv-player` 直加载模式不读取）
- `homeVideoContent(...)`
- `liveContent(...)`
- `isVideoFormat(...)`
- `manualVideoCheck(...)`
- `action(...)`

这几个点非常重要：

1. 旧式布尔开关 `playerContent()["danmu"]` 已经无效；聚合弹幕搜索只看 `danmaku()`，直供弹幕走新的 `danmaku` 字段 / `danmakuContent()` 方法（见 §9.5）
2. 对 Spider 插件来源，`danmaku()` 不仅控制弹幕能力，也控制自动元数据增强和播放器内刮削能力是否挂载
3. `localProxy(...)` 在 `atv-player` 当前实现里没有运行时入口，写了也不会被调用
4. `self.backend_parse = True` 不是通用传统字段，而是 `alist-tvbox` 后端提供的 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 兼容运行层读取的特殊配置；如果 Spider 返回的是网盘资源或磁力资源，就必须配置这个参数，`atv-player` 当前直加载模式不会读取它
5. 许多旧爬虫会返回 `jx` / `playUrl`，但 `atv-player` 当前只看 `parse`、`url`、`header`

## 2. 宿主如何调用你的插件

`atv-player` 的 Spider 插件加载流程是：

1. 读取插件源码并实例化 `Spider`
2. 调用 `init(extend)` 注入插件配置文本
3. 用 `homeContent(...)` 构建插件首页分类与筛选
4. 用 `categoryContent(...)` 和 `searchContent(...)` 拉列表
5. 用 `detailContent(...)` 构建详情页和播放列表
6. 用户点击某个播放项后，再延迟调用 `playerContent(flag, id, vipFlags)` 解析当前播放项

这意味着：

- `detailContent(...)` 负责“详情页和播放列表”
- `playerContent(...)` 负责“某一个播放项最终怎么播”
- 你的播放项 `id` 设计必须稳定，因为宿主会把它保存到 `PlayItem.vod_id`

## 3. 最小可运行骨架

下面是一个适合从零开始的最小骨架：

```python
from base.spider import Spider as BaseSpider


class Spider(BaseSpider):
    def __init__(self):
        self.name = "示例来源"
        self.host = "https://example.com"
        self.extend = ""

    def init(self, extend=""):
        self.extend = extend or ""
        return None

    def getName(self):
        return self.name

    def danmaku(self):
        return True

    def homeContent(self, filter):
        return {
            "class": [
                {"type_id": "movie", "type_name": "电影"},
                {"type_id": "tv", "type_name": "剧集"},
            ],
            "filters": {
                "movie": [
                    {
                        "key": "year",
                        "name": "年份",
                        "init": "",
                        "value": [
                            {"n": "全部", "v": ""},
                            {"n": "2026", "v": "2026"},
                        ],
                    }
                ]
            },
            "list": [],
        }

    def categoryContent(self, tid, pg, filter, extend):
        return {
            "page": int(pg),
            "limit": 1,
            "total": 1,
            "list": [
                {
                    "vod_id": "detail-1",
                    "vod_name": "示例影片",
                    "vod_pic": "https://img.example/poster.jpg",
                    "vod_remarks": "更新至第 1 集",
                }
            ],
        }

    def searchContent(self, key, quick, pg=1, category=""):
        return self.categoryContent(category or "movie", pg, False, {})

    def detailContent(self, ids):
        return {
            "list": [
                {
                    "vod_id": ids[0],
                    "vod_name": "示例影片",
                    "vod_pic": "https://img.example/poster.jpg",
                    "type_name": "剧情",
                    "vod_year": "2026",
                    "vod_area": "中国大陆",
                    "vod_actor": "演员A / 演员B",
                    "vod_content": "这里是简介",
                    "vod_play_from": "默认线路",
                    "vod_play_url": "第1集$play-1#第2集$play-2",
                }
            ]
        }

    def playerContent(self, flag, id, vipFlags):
        return {
            "parse": 0,
            "url": "https://media.example/video.m3u8",
            "header": {"Referer": self.host + "/"},
        }
```

## 4. 列表与详情数据格式

### 4.1 `homeContent(...)`

常用返回结构：

```python
{
    "class": [
        {"type_id": "movie", "type_name": "电影"},
        {"type_id": "tv", "type_name": "剧集"},
    ],
    "filters": {
        "movie": [
            {
                "key": "year",
                "name": "年份",
                "init": "",
                "value": [{"n": "全部", "v": ""}, {"n": "2026", "v": "2026"}],
            }
        ]
    },
    "list": [],
}
```

说明：

- `class` 用来生成一级分类
- `filters` 是可选项，键名通常对应 `type_id`
- `categoryContent(...)` 收到的 `extend` 就是当前筛选值

### 4.2 `categoryContent(...)`

常用字段：

- `page`
- `limit`
- `total`
- `list`

`list` 中每一项至少建议提供：

- `vod_id`
- `vod_name`
- `vod_pic`
- `vod_remarks`

### 4.3 `searchContent(...)`

`atv-player` 会优先尝试四参版本：

```python
searchContent(key, quick, pg, category)
```

如果你的插件只实现三参版本：

```python
searchContent(key, quick, pg)
```

宿主也会回退兼容。

如果你的来源天然有多种搜索域，例如按 `song`、`album`、`playlist`、`singer` 分流，建议直接使用第四个 `category` 参数。

### 4.4 `detailContent(...)`

宿主最关心的是：

- 详情元数据
- `vod_play_from`
- `vod_play_url`

典型结构：

```python
{
    "list": [
        {
            "vod_id": "detail-1",
            "vod_name": "示例影片",
            "vod_pic": "https://img.example/poster.jpg",
            "type_name": "剧情",
            "vod_year": "2026",
            "vod_area": "中国",
            "vod_director": "导演A",
            "vod_actor": "演员A / 演员B",
            "vod_content": "简介",
            "vod_play_from": "线路1$$$线路2",
            "vod_play_url": "第1集$play-1#第2集$play-2$$$备用1$play-1b#备用2$play-2b",
        }
    ]
}
```

规则：

- `$$$` 分隔不同播放源分组
- `#` 分隔同一分组下的多个播放项
- 每个播放项格式是 `标题$id`
- `playerContent(flag, id, vipFlags)` 里的 `flag` 对应当前分组名，`id` 对应这里的播放项值

## 5. 播放解析：`playerContent(...)` 应该怎么写

### 5.1 这是宿主真正依赖的字段

`atv-player` 直接读取：

```python
{
    "parse": 0 or 1,
    "url": "...",
    "header": {...},
}
```

### 5.2 直链播放

当你已经拿到最终媒体地址时，返回：

```python
return {
    "parse": 0,
    "url": "https://media.example/video.m3u8",
    "header": {
        "Referer": self.host + "/",
        "User-Agent": "Mozilla/5.0 ...",
    },
}
```

适合：

- 直出 `m3u8`
- 直出 `mp4`
- 站内接口已经解出真实地址

### 5.3 让宿主走内置解析器

如果你拿到的是播放页 URL、二次解析页 URL，或者必须交给宿主解析器处理的地址，返回：

```python
return {
    "parse": 1,
    "url": "https://example.com/play/123.html",
    "header": {"Referer": self.host + "/"},
}
```

说明：

- `parse=1` 会让 `atv-player` 的内置解析服务接管
- 这时当前播放项的“解析”下拉框会变为可用
- `url` 仍然必须是一个可交给解析器处理的字符串

这类站点型 Spider 的常见做法是：能直接解出媒体地址就返回 `parse=0`，不能稳定解出就降级为 `parse=1`

### 5.4 当前宿主忽略的传统字段

很多旧插件会返回：

```python
{
    "parse": 0,
    "jx": 0,
    "playUrl": "",
    "url": "...",
    "header": {},
}
```

在 `atv-player` 里：

- `parse` 有效
- `url` 有效
- `header` 有效
- `jx` 当前无效
- `playUrl` 当前无效

所以不要把关键逻辑放在 `jx` 或 `playUrl` 上。

### 5.5 错误返回

如果你当前无法解析出地址，优先返回空地址而不是伪造地址：

```python
return {"parse": 0, "url": "", "header": {}}
```

更推荐在必要时直接抛明确错误：

```python
raise ValueError("播放地址解析失败")
```

宿主会记录错误日志，并在播放器里显示失败信息。

## 6. 网盘链接、磁力链接和下载类链接

这是 `atv-player` 相对传统宿主最特殊、也最实用的一块。

### 6.1 你可以直接把分享链接放进播放列表

如果详情页本身就是网盘聚合或下载聚合，你可以直接把播放项值写成：

- 阿里云盘分享链接
- 夸克分享链接
- UC 分享链接
- 百度网盘分享链接
- `magnet:?`
- `ed2k://`

常见做法是：

- 详情页把网盘资源和磁力资源组织成普通 `vod_play_from` / `vod_play_url`
- 用户点击播放项时，宿主识别它是不是网盘分享链接或离线下载链接
- 如果是，宿主会走自己注入的后端解析流程，而不是把它当普通媒体 URL 直接播

### 6.2 网盘链接放在两处都可以

你可以让分享链接出现在：

1. `detailContent(...).vod_play_url` 的播放项值里
2. `playerContent(...).url` 的返回值里

宿主两边都会识别。

### 6.3 这类链接不要求你自己转成直链

如果宿主已经配置了网盘解析能力，插件只需要诚实返回原始链接：

```python
return {"parse": 0, "url": "https://pan.quark.cn/s/xxxx", "header": {}}
```

或在 `vod_play_url` 里直接写：

```text
夸克资源$https://pan.quark.cn/s/xxxx
```

### 6.4 什么时候自己解析，什么时候交给宿主

推荐：

- 普通站内点播：插件自己在 `playerContent(...)` 里解析
- 网盘分享链接：交给宿主
- 磁力/`ed2k`：交给宿主

不推荐：

- 为了统一逻辑，把所有网盘链接都在插件里强行解析成站外中转地址
- 把本应作为网盘分享链接保留的信息提前抹平

## 7. `self.backend_parse = True` 的真实情况

如果你的 Spider 需要通过 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 运行，可能会在 `__init__` 里写：

```python
self.backend_parse = True
```

这个字段不是传统 Spider 约定字段，也不是随手遗留的兼容字段。它是专门给 `alist-tvbox` 后端提供的 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 兼容运行层使用的特殊配置。

更准确地说，[Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 不是普通内层 Spider，而是 `alist-tvbox` 后端提供的外层转换器：

- 它对外实现的是传统 `TvBox` 风格 `Spider` 接口
- 它内部再去加载本项目 Spider
- 然后把本项目 Spider 的行为转换成传统 `TvBox` 兼容运行时能理解的形态

`backend_parse` 就是这个外层转换器使用的模式开关之一，而且它的核心使用场景就是网盘资源和磁力资源。

### [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 如何读取它

[Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 会先加载并实例化本项目 Spider，然后用下面的逻辑读取：

```python
def _category_mode_enabled(self):
    if self._inner is None:
        return False
    return bool(getattr(self._inner, "backend_parse", False))
```

也就是说，`backend_parse` 是本项目 Spider 向 `alist-tvbox` 后端提供的 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 兼容运行层声明的一个模式开关。

### [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 自身是怎么工作的

从代码看，[Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 的工作方式大致是：

1. 解析 `extend`
2. 读取 `source`，支持本地源码或远程源码
3. 如果是 `secspider` 包，就先验签、解密
4. 动态加载内部 `Spider` 类并实例化为 `self._inner`
5. 对外暴露传统 `TvBox` 风格的方法：
   - `homeContent(...)`
   - `categoryContent(...)`
   - `detailContent(...)`
   - `searchContent(...)`
   - `playerContent(...)`
   - `localProxy(...)`
6. 在这些对外方法里，决定是直接转发给 `self._inner`，还是先做一层 Atvp 自己的重写、拆分、后端解析和 filter 处理

所以你可以把 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 理解成：

- “由 `alist-tvbox` 后端提供、把本项目 Spider 包装成传统 TvBox 兼容代码再运行”的桥接层

而不是：

- 一个普通业务 Spider
- 只做一点点辅助函数封装的工具类

### 它控制的不是 `playerContent()`，而是 Atvp 面向网盘资源和磁力资源的分类模式

如果 Spider 返回的是网盘资源或磁力资源，那么在 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 链路下，`self.backend_parse = True` 不是“可选优化”，而是必需配置。

当 `backend_parse=False` 时：

- `categoryContent(...)` 直接走内层 Spider
- `searchContent(...)` 直接走内层 Spider
- 结果项保持原样

当 `backend_parse=True` 时：

- 分类和搜索结果会先被 Atvp 改写
- 每个结果项的 `vod_id` 会被包装成 `atvp_detail:<原始vod_id>`
- 每个结果项会被标记成 `folder`
- 用户点开后，Atvp 会先调用内层 `detailContent(...)`
- 然后把 `vod_play_from` / `vod_play_url` 拆成一个“二级目录列表”

这说明 `backend_parse` 的本质不是“视频解析开关”，而是：

- 是否让 Atvp 把 Spider 的分类/搜索结果当作“目录入口”
- 是否让 Atvp 在详情阶段接管一部分资源展开逻辑

这里的“资源展开逻辑”主要就是网盘资源、磁力资源、分享链接这类不能直接按普通视频详情处理的目标。

换句话说：

- 返回普通站内视频详情时，可以不依赖这个字段
- 返回网盘资源或磁力资源时，必须开启这个字段

### Atvp 后续会怎么接管

在 `backend_parse=True` 模式下，Atvp 会在后续链路里做两类事情：

1. 如果某个 `id` 看起来已经是 `http/https/magnet/ed2k`，`detailContent(...)` 会优先走 Atvp 的 `_parse(...)`
2. `_parse(...)` 会请求 Atvp 后端的 `parse` 接口，再把返回结果过一遍 filter、缓存 detail、缓存 play context

虽然 `_decode_parse(...)` 代码层面也识别 `http/https`、`magnet`、`ed2k`，但按你的设计意图，这个模式开关主要针对的是网盘资源和磁力资源。

所以这个字段更准确的定义是：

- `alist-tvbox` 后端提供的 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 兼容运行层面向网盘资源和磁力资源的“目录化后端解析模式开关”

而不是：

- 通用 Spider 规范字段
- `playerContent()` 的解析标记
- `atv-player` 直加载模式的功能开关

### 在 `atv-player` 直加载模式里

当前不会读取这个字段。

也就是说，仅仅设置：

```python
self.backend_parse = True
```

不会给 `atv-player` 带来任何额外行为。

文档建议：

- 如果你的插件需要兼容 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py)，并且返回的是网盘资源或磁力资源，就必须设置它
- 如果你的插件需要兼容 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py)，但返回的是普通站内直链/播放页，再根据链路决定是否需要它
- 但不要把 `atv-player` 直加载模式里的功能是否可用，建立在这个字段上

## 8. `localProxy(...)` 的真实情况

### 结论

`localProxy(...)` 在 `atv-player` 当前实现里没有效果。

根因不是你的爬虫写法，而是宿主没有把这个接口接到运行时 HTTP 入口：

- 插件控制器不会调用 `localProxy(...)`
- 本地 HLS 代理也不会转发到插件的 `localProxy(...)`

因此像下面这种传统写法：

```python
def localProxy(self, params):
    ...
    return [200, "application/vnd.apple.mpegurl", rewritten.encode("utf-8")]
```

在当前 `atv-player` 中不会生效。

### 文档应该怎么理解这个能力

- 对 `TVBox`/`Atvp` 风格宿主：这是可用接口
- 对当前 `atv-player`：这是未接线的兼容接口

### 实际建议

如果你的目标宿主是当前 `atv-player`，优先选择：

1. 在 `playerContent(...)` 中直接返回最终媒体 URL
2. 必要时返回 `parse=1` 交给宿主解析器
3. 把网盘/磁力链接原样返回给宿主做后续处理

不要依赖 `localProxy(...)` 做：

- `m3u8` 重写
- 分片代理
- 防盗链中转
- 动态签名转发

除非未来宿主明确把这条链路接上。

## 9. 弹幕支持

### 9.1 开关只看 `danmaku()`

要让宿主把这个插件视为“支持弹幕来源推断”，实现：

```python
def danmaku(self):
    return True
```

当前宿主不会再读取旧式的：

```python
{"danmu": True}
```

对 Spider 插件来源来说，这个开关当前还有额外含义：

- `danmaku() == True` 时，插件请求会挂载自动元数据增强能力
- `danmaku() == True` 时，播放器里的元数据刮削服务也会挂载
- `danmaku() == False` 时，这两条能力都不会为该插件来源启用

所以如果你希望这个插件来源支持：

- 自动元数据增强
- 弹幕自动解析

那就必须返回：

```python
def danmaku(self):
    return True
```

### 9.2 弹幕是如何触发的

只要：

- `danmaku()` 返回 `True`
- 当前播放项最终解析出了有效播放地址

宿主就会尝试根据当前内容推断弹幕搜索标题、集数和候选页面 URL。

### 9.3 对插件作者最有帮助的做法

建议尽量提供：

- 稳定的 `vod_name`
- 清晰的剧集标题
- 尽量准确的 `vod_year`
- 当播放项本身就是站内详情页 URL 或视频页 URL 时，直接把它保留在播放项 `id` 或解析结果里

例如：

- `腾讯视频` / `v.qq.com` 页面 URL
- 站内单集详情页 URL

宿主在很多情况下会拿这些信息去辅助弹幕匹配。

### 9.4 什么时候不该指望弹幕自动对上

下面这些情况宿主很难自动推断：

- 纯资源聚合站，没有稳定的单集页面
- 你的播放项 `id` 只是内部随机串，没有剧集信息
- 详情页标题和真实片名相差很大

这时更应该优先保证：

- 标题干净
- 集数标注明确
- 年份尽量准确

### 9.5 弹幕直供（`danmaku` 字段与 `danmakuContent()` 方法）

如果插件自己就知道弹幕数据在哪（例如站内评论接口需要插件签名才能调用），可以让宿主跳过聚合搜索，直接使用插件提供的弹幕。这是和 `danmaku()` 聚合搜索完全独立的一条通道：

**方式一：`playerContent` 返回 `danmaku` 字段（适合已有现成 XML 直链或小体量数据）**

```python
def playerContent(self, flag, id, vipFlags):
    return {
        "parse": 0,
        "url": play_url,
        "danmaku": "https://cdn.example/comments/episode-1.xml",  # B站式弹幕 XML 直链
    }
```

支持三种写法：

- URL 字符串：`"danmaku": "https://.../xxx.xml"`
- 内联 XML：`"danmaku": {"xml": "<i><d p=\"1.0,1,25,16777215\">文本</d>...</i>"}` 或裸 XML 字符串
- 带请求头：`"danmaku": {"url": "https://...", "header": {"Referer": "..."}}`

XML 需要是 B站兼容格式：`<d p="秒,模式,字号,颜色,时间戳,池,用户,行ID">文本</d>`。下载在起播后的后台线程进行，不会阻塞起播；默认复用 `header` 里的 referer/user-agent/cookie（dict 形式的 `header` 会整体覆盖）。

**方式二：实现可选方法 `danmakuContent`（适合需要插件签名/翻页才能拿到数据的场景）**

```python
def danmakuContent(self, flag, id):
    # 用插件自己的签名请求拉齐弹幕，转成 B站式 XML 字符串
    return '<?xml version="1.0" encoding="UTF-8"?><i><d p="1.0,1,25,16777215">你好</d></i>'
```

宿主在起播后的弹幕后台线程里调用它（不在 `playerContent` 里同步调用，所以可以做耗时 的签名请求与翻页）。返回 `None`、空串或抛异常都视为本集无直供弹幕。JS 插件同样支持：导出 `danmakuContent(flag, id)` 即可。

**语义边界：**

- 直供与聚合互不隐含：`danmaku()` 只控制聚合搜索；`danmaku` 字段 / `danmakuContent` 只控制直供。需要签名的源建议把 `danmaku()` 保持 `False`（聚合源大概率不收录，白扫），只用直供。
- 优先级：payload `danmaku` 字段 > `danmakuContent` 方法 > 聚合搜索。直供失败（网络错误、方法抛异常）时，若 `danmaku()` 为 `True` 才回退聚合搜索。
- 直供不受短剧排除影响（聚合搜索会跳过短剧条目，直供不会——短剧恰恰是直供的主要场景）。
- 直供结果按 `插件名 + 播放项 id` 缓存 3 天，重播不重新请求；插件侧也可自行用 `getCache/setCache` 再做一层。
- 实现了 `danmakuContent` 的插件即使 `danmaku()` 为 `False`、条目是短剧，也会正常挂载播放器弹幕控制与下集弹幕预取。

## 10. 外挂字幕与卡拉 OK 歌词

### 10.1 `subt`

`playerContent(...)` 可以返回外挂字幕：

```python
return {
    "parse": 0,
    "url": "https://media.example/video.mp4",
    "subt": "https://cdn.example/subtitles/episode-1.srt",
}
```

宿主支持这些形式：

- 绝对 URL
- 以 `/` 开头的相对路径
- 本地绝对路径
- 内联文本负载

宿主会把它们归一化进 `PlayItem.external_subtitles`。

### 10.2 `lyric`

`playerContent(...)` 还可以返回原始歌词负载，宿主会把它转成可选字幕：

```python
return {
    "parse": 0,
    "url": play_url,
    "lyric": {
        "format": "qqmusic-qrc",
        "text": "...",
        "translation": "...",
    },
}
```

当前宿主支持的典型歌词格式包括：

- `qqmusic-qrc`
- `kugou-krc`
- `netease-yrc`

如果 `lyric` 成功转成逐字歌词，宿主会优先使用它；只有歌词无效时，才回退到 `subt`。

这一块的典型场景是音乐类或歌词类 Spider。

## 11. 播放质量、播放封面和其他播放扩展

### 11.1 `qualities`

如果一个播放项存在多个清晰度，可以返回：

```python
{
    "parse": 0,
    "url": "https://media.example/default.m3u8",
    "qualities": [
        {"id": "1080p", "name": "1080P", "url": "https://media.example/1080.m3u8"},
        {"id": "720p", "name": "720P", "url": "https://media.example/720.m3u8"},
    ],
}
```

宿主会把它映射到播放器的清晰度切换控件。

### 11.2 `cover`

如果你希望播放开始后，用新的播放封面覆盖播放器里的视频占位图，可以返回：

```python
{
    "parse": 0,
    "url": "https://media.example/video.mp4",
    "cover": "https://img.example/resolved-cover.jpg",
}
```

它只影响播放器里的视频封面，不会回写详情页海报。

## 12. 播放器自定义动作

这是当前宿主对插件扩展支持最完整的一部分。

### 12.1 动作模型

每个动作都会被归一化成：

- `id: str`
- `label: str`
- `active: bool = False`
- `enabled: bool = True`
- `visible: bool = True`
- `tooltip: str = ""`

规则：

- `id` 和 `label` 必填
- `visible=False` 的动作会被丢弃
- 非法动作会被忽略
- 原始顺序会被保留

### 12.2 两个动作来源

播放器详情动作可以来自两处：

1. `detailContent(...).list[0].actions`
2. `playerContent(...).actions`

### 12.3 `detailContent.actions`

适合容器级状态：

- `收藏歌单`
- `收藏专辑`
- `关注歌手`

原因：

- `detailContent(...)` 拿得到完整容器上下文
- `playerContent(...)` 只有 `flag` 和播放项 `id`

示例：

```python
"actions": [
    {
        "id": "favorite_playlist",
        "label": "收藏歌单",
        "active": False,
        "tooltip": "",
    }
]
```

### 12.4 `playerContent.actions`

适合当前播放项状态：

- `收藏歌曲`
- `点赞`
- `不喜欢`

示例：

```python
return {
    "parse": 0,
    "url": self.get_play_url(id),
    "actions": [
        {"id": "favorite_track", "label": "收藏歌曲", "active": False},
        {"id": "like_track", "label": "点赞", "enabled": True},
    ],
}
```

行为：

- 当前播放项动作会合并到已有动作列表
- 如果 `id` 相同，`playerContent(...)` 的动作会覆盖 `detailContent(...)` 的同名动作

### 12.5 让按钮真的能点：`runPlayerAction(...)`

实现：

```python
def runPlayerAction(self, action_id, context):
    ...
```

宿主会传入：

- `context["action_id"]`
- `context["vod"]`
- `context["play_item"]`
- `context["playlist"]`
- `context["playlist_index"]`
- `context["play_index"]`
- `context["log"]`

推荐：

- 用 `vod` 处理专辑/歌单/歌手级动作
- 用 `play_item` 处理单曲/单集级动作

### 12.6 返回值契约

动作执行后，返回刷新后的完整动作列表。

支持两种形式：

```python
return {
    "actions": [
        {"id": "favorite_album", "label": "已收藏专辑", "active": True},
        {"id": "favorite_track", "label": "已收藏歌曲", "active": True},
    ]
}
```

或：

```python
return [
    {"id": "favorite_album", "label": "已收藏专辑", "active": True},
    {"id": "favorite_track", "label": "已收藏歌曲", "active": True},
]
```

推荐不要只返回被点击的那一个按钮，因为宿主会把返回结果当成当前播放项动作区的新状态。

### 12.7 完整动作示例

这类模式常见于带收藏、关注、点赞能力的来源：

```python
def runPlayerAction(self, action_id, context):
    vod = context.get("vod") or {}
    play_item = context.get("play_item") or {}

    if action_id == "favorite_playlist":
        self.favorite_playlist(vod.get("vod_id", ""))
    elif action_id == "favorite_track":
        self.favorite_track(play_item.get("vod_id", ""))
    else:
        raise ValueError(f"unsupported action: {action_id}")

    return {"actions": self._build_actions_for_context(context)}
```

## 13. 播放器自定义详情字段

### 13.1 基本只读字段

你可以给播放器详情侧栏补充只读信息：

```python
"ext": [
    {"label": "播放", "value": "12万"},
    {"label": "更新", "value": "2026-05-08"},
]
```

规则：

- `detailContent(...).list[0].ext` 是整部作品级字段
- `playerContent(...).ext` 是当前播放项级字段
- 如果当前播放项有有效 `ext`，它会覆盖整部作品级字段显示
- 每一行必须同时有非空 `label` 和 `value`

### 13.2 可点击详情字段

`value` 不仅能是字符串，也能是数组或带动作对象：

```python
{"label": "演员", "value": "演员1"}
{"label": "演员", "value": ["演员1", "演员2"]}
{
    "label": "演员",
    "value": [
        {"label": "演员1", "action": {"type": "search", "value": "演员1"}},
        {"label": "演员2", "action": {"type": "detail", "value": "actor-2"}},
    ],
}
```

支持的动作类型：

- `category`
- `search`
- `detail`
- `link`

行为：

- `category`：切回插件标签页并加载 `categoryContent(...)`
- `search`：切回插件标签页并加载 `searchContent(...)`
- `detail`：通过当前插件打开新的详情请求
- `link`：在系统浏览器打开 URL

### 13.3 行内 CR 链接

如果你直接往 `vod_actor`、`vod_director`、`vod_content` 这类纯字符串字段里塞文本，也可以嵌入点击段：

```text
[a=cr:{"type":"search","value":"周杰伦"}/]周杰伦[/a]
```

也支持指定：

```text
[a=cr:{"target":"bilibili","type":"category","value":"up:378885845"}/]Harold[/a]
```

规则：

- 可见文本是 `[a=cr:...]` 和 `[/a]` 中间的部分
- `type` 和 `value` 必填
- `target` 可选
- `target="bilibili"` 会路由到内置 Bilibili 标签

### 13.4 相关推荐

在 `detailContent(...).list[0]` 里返回 `vod_related`，播放器详情侧栏会显示一排"相关推荐"胶囊按钮，点击即用当前来源打开该条目详情并播放。适合映射源站的"同系列""相关推荐""猜你喜欢"等区块——同一作品的其他季/部和其他相似作品可以放进同一个列表：

```python
def detailContent(self, ids):
    vod = {
        "vod_id": "book-1001",
        "vod_name": "闪婚老伴第一季",
        # ...
        "vod_related": [
            {"vod_id": "book-1001", "vod_name": "闪婚老伴第一季", "vod_remarks": "全62集"},
            {"vod_id": "book-2042", "vod_name": "闪婚老伴第二季", "vod_remarks": "更新至30集",
             "vod_pic": "https://cdn.example.com/2042.jpg", "vod_year": "2025"},
            {"vod_id": "book-3190", "vod_name": "黄昏恋歌", "vod_remarks": "都市短剧",
             "vod_pic": "https://cdn.example.com/3190.jpg", "vod_year": "2026"},
        ],
        "vod_related_label": "同系列",
    }
    return {"list": [vod]}
```

规则：

- 每个条目需要 `vod_id` 和 `vod_name`（键名也接受 `id`/`name`/`title`/`label`/`value` 别名），缺一即丢弃该条目
- 可选 `vod_remarks`（也接受 `remarks` 别名）作为条目备注（集数/状态等），显示在胶囊的 tooltip 第二行
- 可选 `vod_pic`（也接受 `pic` 别名）海报地址、`vod_year`（也接受 `year` 别名）年份：整个播放列表播完后,播放器会在视频区弹出全屏网格的相关推荐媒体卡片(海报 + 标题 + 年份/备注,点击即播),带海报的卡片效果最好
- `vod_id` 必须是当前插件 `detailContent(...)` 能直接打开的 id，并尽量与列表/搜索返回的 `vod_id` 保持同一形态——播放记录按 id 存续播进度，形态漂移会先按同插件同名条目兜底续播
- 列表包含当前作品本身时，正在播放的条目会高亮且不可点击；只放相似作品时可以不含当前作品
- 可选 `vod_related_label` 自定义区块标题（默认"相关推荐"，超过 12 字符会截断），例如纯同系列列表可设为"同系列"、站方推荐位可设为"猜你喜欢"
- 建议按相关度/季部顺序排列，最多取前 100 条（播完卡片最多显示前 24 条）
- 跳转走的是普通详情请求，历史记录、断点续播照常生效
- 条目多于 8 个时播放器默认收起该区块（标题显示数量），用户可点标题展开/收起并持久化偏好

## 14. 插件管理动作

除了播放器里的详情动作，你还可以给“插件管理”对话框提供自定义按钮。

### 14.1 声明动作

```python
def getManagerActions(self):
    return [
        {"id": "qr_login", "label": "扫码登录"},
        {
            "id": "clear_login",
            "label": "清除登录",
            "enabled": True,
            "tooltip": "",
        },
    ]
```

### 14.2 执行动作

```python
def runManagerAction(self, action_id, context):
    if action_id == "clear_login":
        context.set_config_text("")
        context.log("info", "已清除登录信息")
        context.refresh_plugin()
        return
    raise ValueError(f"unsupported action: {action_id}")
```

当前上下文常用字段：

- `context.parent`
- `context.plugin_id`
- `context.plugin_name`
- `context.config_text`
- `context.set_config_text(text)`
- `context.refresh_plugin()`
- `context.log(level, message)`

最实用的场景通常是：

- 扫码登录
- 清除登录态
- 写回 cookie/token 到配置文本

## 15. 配置文本与 `init(extend)`

宿主会把插件配置文本原样传给 `init(extend)`。

建议：

- 简单场景直接把 `extend` 当 cookie 文本
- 复杂场景优先支持 JSON

常见做法是：

- 如果 `extend` 是 JSON，就解析成配置对象
- 如果 `extend` 是普通文本且包含 `=`，就当作 cookie

推荐写法：

```python
def init(self, extend=""):
    self.extend = extend or ""
    try:
        data = json.loads(self.extend) if self.extend.strip().startswith("{") else {}
    except Exception:
        data = {}
    cookie = data.get("cookie", "") if isinstance(data, dict) else ""
    if cookie:
        self.headers["Cookie"] = cookie
```

## 16. 当前宿主下的最佳实践

推荐：

- 把容器级动作放在 `detailContent.actions`
- 把当前播放项动作放在 `playerContent.actions`
- 把当前播放项详情补充字段放在 `playerContent.ext`
- 直接返回最终媒体 URL 时使用 `parse=0`
- 需要交给宿主解析器时使用 `parse=1`
- 网盘分享链接、磁力链接原样返回给宿主
- `danmaku()` 单独返回 `True`，不要再依赖 `playerContent()["danmu"]`
- 保持 `vod_id`、播放项 `id`、动作 `id` 稳定

不推荐：

- 依赖 `jx`
- 依赖 `playUrl`
- 依赖 `localProxy(...)`
- 把 `self.backend_parse = True` 当成 `atv-player` 直加载模式的功能开关
- 只返回局部动作更新
- 把站点类型、专辑类型等状态硬编码进前端假设

## 17. 排障清单

### 点击播放后提示“插件未返回可播放地址”

先检查：

1. `playerContent(...).url` 是否为空
2. `parse=0` 时，`url` 是否真的是媒体 URL、网盘分享链接或磁力链接
3. 你是不是只返回了 `playUrl`，却没有返回 `url`

### 自定义动作能显示但点了没反应

先检查：

1. 是否实现了 `runPlayerAction(...)`
2. 动作 `id` 是否和声明一致
3. 是否抛出了被宿主记录的异常

### 弹幕按钮可见但总是搜不到

先检查：

1. `danmaku()` 是否返回 `True`
2. 标题是否过脏
3. 集数是否可推断
4. 播放项是否保留了有意义的页面 URL 或单集信息

### `localProxy(...)` 完全没效果

这不是你一个插件的问题。当前宿主没有接这条链路。

### `self.backend_parse = True` 设置了也没区别

如果你是在 `atv-player` 直加载模式下观察到这一点，这是当前宿主的预期行为，因为它不会直接读取这个字段。

如果你走的是 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 适配链路，则要按“分类模式/后端解析适配开关”来理解它，而不是按播放器直加载模式理解。

### 返回网盘资源或磁力资源却没有设置 `self.backend_parse = True`

如果你在 [Atvp.py](https://github.com/power721/alist-tvbox/blob/master/src/main/resources/static/Atvp.py) 链路下返回的是网盘资源或磁力资源，但没有设置这个参数，Atvp 就不会切到这套目录化后端解析模式。

结果通常会是：

- 分类/搜索结果不会被包装成 `atvp_detail:...` 目录入口
- Atvp 不会按网盘资源/磁力资源的方式拆二级目录
- 后续详情和播放阶段不会按你为网盘资源设计的链路接管

所以对网盘资源和磁力资源来说，这个参数是必填的。
