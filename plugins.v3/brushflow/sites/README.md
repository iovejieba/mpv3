# 站点策略层（sites/）开发指南

本包是刷流插件的**站点策略适配层**：把"全站 H&R"这类站点级规则的权威语义，
以纯逻辑模块的形式接入 9.x 的删种硬保护链与选种链。

设计依据与分工见
`MoviePilot-Resources-main/exoticaz-python-reference/MPV3-适配设计与交叉验证说明.md`：

- **数据层**（站点定义、解析引擎）在 MoviePilot 侧的 `user.sites.v3.bin`（自维护工具链见
  `MoviePilot-Resources-main/resources.v3/MAINTENANCE.md`），插件不重复做站点解析；
- **策略层**（H&R 计时、豁免判定、删种决策）在本包，插件独有；
- **合同**：规则语义以参考实现 `exoticaz_client.py` + 31 条规范向量为权威，任何一侧偏离先对拍。

## 目录结构

```
sites/
├── __init__.py      # SiteHRRule（策略对象）+ REGISTRY（domain → 规则）+ resolve_rule()
├── exoticaz.py      # ExoticaZ 适配：官方公式、豁免判定、纯解析助手
└── README.md        # 本文件
```

## 核心语义（与参考实现逐行对齐）

**H&R 公式**（x = 体积 GiB，站点页面 GB 即 GiB，1024 进制）：

```
f(x) = 72 + 2x               , x < 50      # 分支条件必须 x < 50，右支含 50
f(x) = 100·ln(x) − 219.2023  , x ≥ 50      # x=50 → 172.00000054…，ceil 后 173
返回 ceil(max(f(x), 72))；x ≤ 0 → 72 兜底
```

**豁免判定**（OR 语义，两边都是 `>=`，比较用 ceil 后整数）：

```
cleared = seeded_hours ≥ f(size)  OR  (clear_ratio > 0 AND ratio ≥ clear_ratio)
```

**豁免线配置**（站点级，任务不可设）：

| 覆盖值 | 行为 |
| --- | --- |
| 留空 | 用内置默认 1.0（站点官方 0.9 + qB 本地统计与站点记账的上报偏差缓冲） |
| 0 | 关闭分享率通道，只走做种时长路径（最保守） |
| (0, 0.9) | 比站点规则更激进，拒绝——按 0.9 兜底 |
| ≥ 0.9 | 原样生效 |

覆盖值持久化在插件 KV `site_hr_policies`（`{domain: {"clear_ratio": ...}}`），
由 `__init__.py` 的 `_site_hr_policy_for_task()` 读取合并。

## 接线点（改动地图）

| 位置 | 作用 |
| --- | --- |
| `deletion_service.py` `observations()` → `_apply_site_hr_policy()` | 单点收口：已豁免的种子在观测层解除 `hit_and_run` 标记；三份硬安全线（`hard_safety_reasons` / `decision.evaluate_candidate` / `_smart_runtime_safety_reasons`）代码零改动 |
| `deletion_service.py` `sample_and_plan()` | evaluated 行携带 `hr` 明细（domain/required_hours/seeded_hours/ratio/clear_ratio/cleared） |
| `__init__.py` `_deletion_service()` | 全仓唯一 DeletionService 构造点，按任务站点 domain 注入规则与豁免线覆盖 |
| `__init__.py` `__brush_site_torrents()` / `__evaluate_conditions_for_brush()` | 站点存在 HR 策略时跳过 `task.hr` 选种一票否决（否则全站 HR 站永远无候选） |
| `__init__.py` 策略状态聚合 | `strategy.hr_rows` → 工作台"策略详情"展示每个种子的 H&R 状态 |

**fail-closed 原则**：size/seeding_time/uploaded 缺失或非法 → 一律视为未豁免，
维持 9.1 的 H&R 永久硬保护。策略层永不"早放"。

## 接入一个新站点（全站 H&R 类）

1. 在本包新增 `mysite.py`：
   - 常量：`SITE_DOMAIN`（与 MP 站点定义的 domain 一致，忽略大小写与 www 前缀）、
     `SITE_MIN_CLEAR_RATIO`（站点官方豁免线）、`SITE_DEFAULT_CLEAR_RATIO`（内置默认）；
   - `def hr_seed_hours(size_gib) -> int`：站点官方公式（无公式则用常数下限函数）；
   - `register_rule(SiteHRRule(domain=..., required_hours=hr_seed_hours, ...))`；
2. 在 `__init__.py` 追加 `from .sites import mysite as _site_mysite  # noqa: F401`（导入即注册）；
3. 确认 MP 侧站点定义已收录该站点（或按 resources.v3 工具链自定义）；
4. 按"对拍协议"补向量测试（见下）；
5. 跑全套离线测试 + 真机小种端到端验证（公式时长内始终拒绝删除 → 到期或 ratio 达标放行）。

非全站 H&R 的普通站点**无需**接入本包——未注册 domain 的站点维持 9.1 原行为
（HR 标记 → 永久保护），选种侧 `task.hr` 过滤照常生效。

## 测试与对拍协议

```bash
# 全套离线测试（便携解释器路径按实际环境调整）
python -m pytest tests -q

# 只跑规范向量对拍（31 条，来自参考包 hr_conformance_vectors.json）
python -m pytest tests/v3/brushflow/sites/test_hr_conformance.py -v
```

- 向量文件 `tests/v3/brushflow/sites/hr_conformance_vectors.json` 从参考包复制，
  `test_vectors_file_version` 用 SHA256 前缀（当前 `56ADE6A0`）钉住版本；
  **资产变更时先同步两侧、更新前缀断言，再谈结果**（§6 版本三元组纪律）；
- 站点改版时：重新抓样本 → `resources.v3/validate_overlay.py` 校验定义 →
  参考包/向量同步更新 → 本包重跑对拍。

## 站点用户统计探活（分享率显示与控制兜底）

MP 站点定义层不含用户数据（官方 Unit3d 站点同样如此），任务开启"站点分享率控制"后，
MP 无数据的站点会一直显示"暂无站点分享率统计"并拦截新增。策略层提供探活兜底：

- `sites/__init__.py` 的 `register_stats_parser(domain, parser)` / `resolve_stats_parser(domain)`：
  按 domain 注册"登录页 → 用户统计 dict"的纯解析器（`parse_ratio_bar`，bs4 实现，
  未登录返回 None 交由调用方区分 Cookie 失效与被盾）；
- `__init__.py` 的 `_ensure_site_user_stats(task, site)`：brush 流程按需探活，TTL 30 分钟
  （含失败负缓存），MP 已有数据时不发请求；HTTP 401/403 提示人工过盾；
- `_build_site_ratio_status`：MP 无数据时读取探活缓存，`source=plugin_probe` 标记来源；
- 工作台"策略详情"展示当前分享率/目标（含"无限"与"等待数据更新"状态）。

依赖：`pyproject.toml` 声明 `beautifulsoup4>=4.12`（宿主共享环境安装）。
探活纪律沿用请求纪律：仅 1 个 GET、带站点 Cookie/UA、失败静默降级不影响刷流。

## 站点 H&R 账本镜像层（服务器权威判定）

ExoticaZ 的 H&R 有客户端无法消除的口径差（合同见参考包《刷流插件对接说明.md》§2.3）：

- 做种要求须在**下载完成后 96 小时内**达成（锚点=完成时刻，qB `completion_on` 现成；
  离线消耗窗口但不计做种时长；窗口到期即记账，做种中也不例外——站点二开差异点）；
- 服务器做种时长只在 announce 入账（announce 之间倒计时冻结，实测 ~25-30 分钟周期）；
- 实测漂移 ≈6h（离线 ~5h + announce 结算滞后 ~1h）→ 客户端时长达标 ≠ 服务器豁免。

三层结构（`deletion_service._apply_site_hr_policy`）：

| 层 | 实现 | 语义 |
| --- | --- | --- |
| 基线闸门 | 公式 + 客户端做种时长 + `hr_buffer_hours` 缓冲 | `hours ≥ required + buffer` 或 `ratio ≥ clear_ratio` 才可能豁免 |
| 账本镜像 | `hr_ledger_lookup(torrent_id)` 查观察名单 | 名单内（`watch`/`counted`）一票否决豁免；**缺席不作为豁免证据**（新种未入账），只多保不早放 |
| 标定 | `_calibrate_hr_buffer()`：账本 `seed_hours`（要求−剩余，分钟精度）vs 客户端统计 | 漂移样本 [0,48]h，EWMA 0.2、≥3 样本后收敛，缓冲落任务数据 `site_hr_calibration` |

同步与纪律：

- `__init__.py` `_sync_site_hr_ledger()`：`history?hnr=1` 观察名单（watch+counted），
  1~3 请求/轮、`_SITE_LEDGER_TTL` 30 分钟、与探活共用 `RequestThrottle`（全站 1rps）；
- 新记账命中 → `__send_message` 提醒（自愈语义 + BP 定价 `round(10×剩余小时)` + 96h 窗口纪律）；
- 下载器快照已带 `completion_on`（qB/Tr 双栈），窗口余量计算可用；
- 同步失败静默降级，基线闸门继续兜底。

解析器：`sites/exoticaz_ledger.py`（收编 MP 侧 userdata_parser，lxml 实现，
`parse_history_ledger` 11 列、`parse_ratio_bar`、`collect_user_data` 编排）。

## 请求与日志纪律（写代码前先读）

- 站点请求 ≤ 1 rps；RSS 拉取尊重 `ttl`；
- 下载链接/RSS URL 含每用户令牌（`{rid}`/`rsskey`），日志永不打印完整 URL；
- `.torrent` 载荷校验：首字节必须是 `d`（`validate_torrent_payload`）；
- 文本匹配前先 `norm_space()`；体积解析锚定行首单位（内嵌 MB 徽标坑）；
- 登录态判定看 `.ratio-bar` 存在性，不看 Cookie 字面过期时间；
- 403 + "just a moment" = Cloudflare 挑战 → 通知人工过盾，不要当 Cookie 失效重试；
- 未完成/卡种永不自动删（HR 时钟不走，公式永不满足——9.1 下载健康"修复一次→暂停"接管）。

## 当前边界与待办（M2）

- 列表页无促销到期时间（`freedate` 缺口坐实）→ 详情页富化待做；
- 站点用户统计（ratio-bar 三步）不在 MP 定义内 → 分享率显示/控制插件探活待做；
- MP 引擎对 `date_en_elapsed_parse` 的行为是唯一离线验不到的点 → 首次部署核对；
- `presentation.py` 白话结论可进一步吸收 `hr_rows` 聚合（如"3 个种子处于 H&R 保护"）。
