# danmu_api 可借鉴特性调查

> 调查对象：`/home/harold/workspace/danmu_api`（Node.js 弹幕聚合服务，huangxd-/danmu_api）
> 调查目的：找出可移植到 atv-player（PySide6 + mpv 桌面播放器）的算法/技术，**保持"原生直连、单源加载、运行时不依赖聚合服务"的既有哲学**。
> 日期：2026-08-06

## 关系定位

atv-player 的设计文档（`docs/superpowers/specs/2026-07-25-danmaku-three-tier-acquisition-design.md` 等）已明确这条线：

> 参考本地 `/home/harold/workspace/danmu_api` 的 source 经验 … 全程保持 atv-player 原生直连：运行时不依赖用户部署 danmu_api，也不调用其聚合接口，仅移植其匹配与发现经验。

筛选标准：**纯算法/技术可移植，不破坏单源加载 + 原生直连**。

## atv-player 既有能力（无需替换，部分更强）

10 个 provider（tencent/youku/bilibili/iqiyi/mgtv/sohu/renren/dandan/bahamut/animeko）+ 豆瓣发现 + 三级回退（直连 → 豆瓣 → other/dmku.hls.one）；有界并发 + 单源错误隔离（`iter_bounded_settled`）；**结构化置信度匹配引擎**（`DanmakuMatchContext` + 硬约束 + 证据评分，架构比 danmu_api 的启发式更清晰）；每集每源 offset 持久化；完整 ASS 渲染器（static/scroll/mixed + 着色优先车道）；文件缓存 3 天 + 版本失效；dandan 网关探活。

danmu_api 的匹配（`merge-util.js` 3442 行）是 CN 特化启发式、高度耦合 —— **只摘算法，不搬整块**；硬编码密钥/签名是脆弱点，需隔离。

---

## S 级 — 直接填补已记录的非目标缺口

### 1. Bangumi-data 动漫季号权威消歧 ⭐最高价值
- **danmu_api**：`utils/bangumi-data-util.js`。裁剪后的 bangumi-data 数据集，每条 `{title, sites:[{site, id, season_id?, video_sn?}], titleTranslate, begin}`，倒排索引检索（L491-734）。`season_id`（bilibili）/ `video_sn`（gamer/巴哈）能**直接拼成 provider 的分集请求**，绕过脆弱的标题匹配。
- **atv-player 现状**：季号处理"仅拒绝"（`_has_sequel_number_mismatch`），无权威动漫季源。会出现"高达00 误判 0079"（danmu_api commit `9d31e85` 专修）。atv-player 追更系统已接 Bangumi，数据管线部分已存在。
- **移植要点**：bangumi-data 数据集（JSON，本地缓存 7 天）作为动漫 provider 的季号/站点 ID 解析层。命中后 bilibili 用 `season_id`、bahamut 用 `video_sn` 直接请求分集，不靠标题猜季。纯增量补强。

### 2. 跨季集数对齐引擎 `findBestAlignmentOffset`
- **danmu_api**：`utils/merge-util.js:1299-1464`。LCS 去冗余标题 → 季差估计 → 对称搜索窗 → 评分（季差精确 +15、零差奖励 `+100 base + 5/hit` L1454）→ 模式一致性收尾。配套 `extractSeasonMarkers`（L621-683，返回 `{'S2','P1','OVA'}`）+ `SpecialSeriesRegistry`/`SUFFIX_SPECIFIC_MAP`（A's→S2、StrikerS→S3；美战 R→第二季）+ 小数集 sinking（12.5 先移出再对齐）。
- **atv-player 现状**：设计文档明确 *"不实现跨季连续集号顺延 `findCrossSeasonEpisodeMap` — 暂不做"*。季=拒绝，不能选第2季目录。
- **移植要点**：被主动延后的能力，算法现成。只摘对齐内核 + season marker 集 + 特例表，接进 `DanmakuMatchContext`。

### 3. 咪咕 AES 重新评估（可能免 WASM 恢复已删源）⭐
- **danmu_api 现状**：`migu-util.js` 已是纯 AES-ECB + 16 元素 nibble 置换表（`KEY_NIBBLE_SUBSTITUTION=[3,5,7,0,15,10,13,1,11,14,4,6,9,12,8,2]`），密钥 `DEFAULT_GATEWAY_KEY="vwwLu7e6ug4HAQMAug8CsA8HD7oHDwuxAg4HAQG6DLA="`。**无 WASM**。30s 分段 `webapi.miguvideo.com/.../barrage/v2/list`。
- **atv-player 现状**：源已删（`3b3ff6e6`），旧解密用密钥 `ALsP...` + 无 nibble 置换 → 不可靠。**nibble 置换是当年缺的那一步**。
- **移植要点**：纯 Python `cryptography` AES-ECB + 16 元素查表，无原生依赖。需 live 验证。详见 [[migu-danmaku-decryption]] 记忆。

---

## A 级 — 现有管线 drop-in 增强

### 4. Stream sniff-and-abort（早判风控）
`http-util.js:677`：32KB 嗅探窗，下载完整 protobuf 前检测风控/HTML 响应并中止。bilibili/iqiyi protobuf 分段常被塞 HTML 页。移植到 atv-player HTTP 客户端，bilibili 收益最大。低成本。

### 5. 入站繁→简转换（巴哈姆特弹幕）
atv-player 只做出站 s2twp（`bahamut.py` 查询前转繁），巴哈返回繁体弹幕**不回转简体**，用户见繁简混排。danmu_api 在 `zh-util.js` 集中两向 converter，并对弹幕文本入站转简（`danmu-util.js:296-309`）。建议巴哈 resolve 时 OpenCC `t2s` 转简，或可配置。改动极小。

### 6. 每源反向代理路由（makeProxyUrl 4 类）
`globals.js:78-147`：按 hostname 把 `bahamut@url`/`tmdb@url`/`@万能反代`/`http正向代理`/直连分类。atv-player 只有 dandan 网关 + 全局代理。对巴哈(TW)/TMDB，**按 provider 走反代**更精细。可加进高级设置"每源网络"区。

### 7. 差异化退避重试 + validStatusCodes
`http-util.js:55-75`：物理网络错误（ETIMEDOUT/ECONNRESET）100ms 快重试，5xx/429 指数退避；状态码白名单。atv-player `iter_bounded_settled` 隔离了错误但 HTTP 层重试偏粗。

---

## B 级 — 能力扩展（新源，权衡维护成本）

danmu_api 23 源，atv-player 10 源。缺失且有意义：

| 源 | 价值 | 维护成本 |
|---|---|---|
| **红果短剧** Hongguo | 短剧赛道 | **高** — 字节级签名 X-Argus/Gorgon/Ladon（SimON+sm3+自研 Feistel+RC4 变种），频繁失效 |
| **韩剧TV** Hanjutv | 韩剧 | 中 — 双 host 容错 + 加密 payload |
| **VOD 聚合** vod.js | 通用 CatVod 资源站（金蝉/789/听风）| 低-中 — `{server}/api.php/provide/vod/?ac=detail`，fastest/all |
| 埋堆堆 / 爱壹帆 | 港澳 / 华语 | 中 |

建议：**VOD 聚合**性价比最高；红果除非短剧是硬需求否则别碰签名地狱。

---

## C 级 — 战略级（与单源加载哲学冲突，列为选项）

### 8. 跨来源弹幕合并 + 时间轴对齐 + 本地去噪
danmu_api 相对 atv-player **最大能力落差**，但 atv-player 设计列为非目标（单源加载）。若将来做多源合并，值得摘：
- **dandan 锚定时间轴对齐** `alignSourceTimelines`（`merge-util.js:3364-3442`）：弹弹play 为时间基准，逐源投票时间差，需 `matchCount≥0.8 / effectiveRatio≥0.05 / consensusRatio≥0.15` 三重共识才平移。
- **本地精确去噪** `groupDanmusByMinute`（`danmu-util.js:18-108`）：重复计数 ÷ **不同平台数**（非总源数），跨源重复抵消、同源真实重复保留。`x N` 后缀可回放。
- **隔源独立检索**：合并时每个副源独立对齐主源，避免噪声副源污染。

哲学切换，非 quick win —— 先不动，记进知识库。

---

## D 级 — 架构/配置模式（可选润色）

- **Envs 元数据驱动配置**（`envs.js`）：每项带 `category/type/options/min/max/description`，自动生成设置 UI。atv-player 设置项日增，比手写对话框更可维护。
- **Hash-gated 缓存写入**（`codec-util.js:9 simpleHash` + `globals.lastHashes`）：内容 hash 未变跳过落盘，稳态省 95%+ 写入。
- **多格式输出**（`@dan-uni/dan-any`）：一份数据 → 8 格式（Artplayer/Dplayer/Dandan XML/proto-bin…）。atv-player 自渲染 ASS，相关性低。

---

## 明确不要照搬

- 整块 `merge-util.js`（3442 行）—— 只摘算法。
- 硬编码密钥/签名 —— 隔离成配置/可更新。
- atv-player 已更强的不必替换（尤其置信度结构化匹配引擎）。

## 实施优先级（本轮）

1. **Spike + 恢复咪咕 AES**（第 3 条）— 验证 live；通则免 WASM 恢复已删源。
2. **接 Bangumi-data**（第 1 条）— 动漫季号消歧，bilibili/bahamut 受益。
3. 后续：入站繁简（第 5）、stream sniff-and-abort（第 4）、跨季对齐（第 2）。

---

## 2026-10-18 对齐落地记录（danmu_api ea88a15..5ef63a0，33 提交）

**已对齐：**
- **标题比对统一繁转简**（280b232/#484）：utils `_match_text` / `_simplify_name` / service `_compact_title` 接 OpenCC t2s——繁体候选（巴哈/bangumi 官方名）不再被 `should_filter_name` 相似度整批误杀；"第3話"类繁体集标也可提取。注意与本文第 5 条（弹幕**文本**入站转简）是两回事，那条仍未做。
- **罗马数字季号 Ⅰ-Ⅻ**（280b232）：`_simplify_name` 折算为阿拉伯数、`strip_season_suffix` 认尾部罗马数字、bangumi_data `_extract_season` 同步——"无职转生Ⅲ"与"第3季/第二季"跨形互匹配，季错配仍拒。附带把"第N季"归一为裸数字基名（三种书写折到同一基名）。
- **综艺分部尾括号形式**（b4901e4/#494）：`_VARIETY_PART_PATTERN` 增"（上/下）"结尾分支——"第2期：标题（下）"不再与（上）折叠成同一 issue key。
- **dandan 外文检索词两级放行**（3aba4ad/#493）：无中文字符的检索词，标题直击存在时只留直击条目，全部无直击则跳过标题过滤全量放行。
- 另：animeko 搜索镜像补 danmu_api f56b34f 迁移后的 `api.bangumi.vip` 兜底节点。

**逐项核对免对齐**：8152715 弹幕排序（processing/subtitle 已按时间排）、78f7377 腾讯集标题/时长（已用 play_title/video_subtitle/timeLong）、3cbf474 爱奇艺集数补齐（mesh `itemNumber` 独立取数不靠标题）、e7fb658 B站 proto 标签 varint（atv-player 解析器本就 varint + 按 wire type 跳过）、1c9340f 模型 null 缺省（Python `get`/isinstance 无 JS 解构默认值坑）、249cbe5 TMDB 检索词（danmaku 匹配不用 TMDB）。

**架构不同/无此功能判无关**：aiyifan×2、hanjutv（无此源）、merge×3（无跨源合并功能）、NipaPlay 中转×3（需登录的自建机制）、local-danmu×4（无本地弹幕管理）、cache/forward/widget/ui/ci/docs。
