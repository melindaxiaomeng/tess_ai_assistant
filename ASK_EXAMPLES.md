# Tess AI Assistant — 自然语言提问示例（五维实体下钻）

> 适用接口：`POST /tess/ask`（自由提问）或 `POST /tess/analytics`（前端胶囊显式 `analysis_type`）。
> 所有示例均经过「五维实体正则 `extract_entities` + 名称/代号解析 `resolve_entities`」路由验证。
> 优先级：① 显式 `analysis_type` > ② 五维实体识别（`route_source="entity"`）> ③ 关键词推断 > ④ 浅层兜底。

---

## 1. Campaign（广告活动）维度

| 提问示例 | 触发类型 | 抽取实体 |
|---|---|---|
| `帮我查下 5845554camp 的 ctit` | `campaign_detail` | `campaign_id=5845554` |
| `campaign id5845554 的转化漏斗` | `campaign_detail` | `campaign_id=5845554` |
| `这个 5845554 的 etit 分布` | `campaign_detail` | `campaign_id=5845554` |
| `哪个 Campaign 利润环比下滑最快` | `campaign_ranking`（关键词推断） | — |

---

## 2. Advertiser（广告主）维度

| 提问示例 | 触发类型 | 抽取实体 |
|---|---|---|
| `oppo-mmp-Betty 这个广告主最近跑得怎么样` | `advertiser_deepdive` | `advertiser_name=oppo-mmp-Betty` → 解析 `advertiser_id=1000839` |
| `广告主 1000839 的日 KPI` | `advertiser_deepdive` | `advertiser_id=1000839` |
| `1000839adv 旗下 Campaign` | `advertiser_deepdive` | `advertiser_id=1000839` |

> 名称写法支持「数字+关键字」「关键字+数字」两种语序；广告主名（如 `oppo-mmp-Betty`）经 `/advertisers` 列表扫描按子串解析为 id。

---

## 3. Publisher（渠道 / 媒体）维度

| 提问示例 | 触发类型 | 抽取实体 |
|---|---|---|
| `Pub_1000684 这个渠道质量如何` | `publisher_deepdive` | `publisher_id=1000684` |
| `1000571pub 的替换与屏蔽规则` | `traffic_policy_check` | `publisher_id=1000571` |
| `1000571渠道 的扣量率` | `publisher_deepdive` | `publisher_id=1000571` |
| `sub_mkt_9 这个渠道质量如何` | （经 `channel` 解析，若命中真实 publisher 则 `publisher_deepdive`；否则退回关键词/兜底） | `channel=sub_mkt_9` |

> 渠道代号（如 `sub_mkt_9`）经 `/mapping-publisher-channels?channel=` 反解 publisher_id；数字 id 或 `pub_` 前缀直接识别。

---

## 4. Package Name（包名 / 应用）维度

| 提问示例 | 触发类型 | 抽取实体 |
|---|---|---|
| `link.merge.puzzle.onnect.number 这个包近 7 日的营收和利润怎么样` | `pkg_deepdive` | `package_name=link.merge.puzzle.onnect.number` |
| `com.melodong.game 这个包的 CVR 和 Postback 正常吗` | `pkg_deepdive` | `package_name=com.melodong.game` |
| `这个 com.xxx.yyy 包跑了哪些广告主` | `pkg_deepdive` | `package_name=com.xxx.yyy` |

> 包名经 `/advertiser-publisher-pkg-maps?packagename=` 归因出归属广告主/渠道集合，再聚合 `/report` 近 7 日营收/利润/Margin。
> 注意：`packagename=` 为**模糊匹配**（一个包名可能命中多条跨广告主映射）；Teensing 接口**未提供** `/report?package_name=` 直接过滤，包名维度必须经由 pkg-maps 归因。

---

## 5. Owner（AM / BD 负责人）维度

| 提问示例 | 触发类型 | 抽取实体 |
|---|---|---|
| `AM 118 名下客户近 7 日总营收是多少` | `owner_performance` | `owner_name=118` → 解析 `owner_user_id=118`（或显式传 `owner_user_id`） |
| `Betty 手上的客户今天消耗掉了多少？` | `owner_performance` | `owner_name=Betty` → 经 `/users/options` 解析 |
| `BD 35 负责渠道的消耗` | `owner_performance` | `owner_name=35`, `owner_role=bd` |

> 负责人姓名 → user id 经 `/users/options` 扁平目录解析。**该目录覆盖有限**（实测仅返回少量用户），若姓名不在目录内会解析失败、退回兜底（建议在 `params` 中显式透传 `owner_user_id` + `owner_role`）。
> AM/BD 名下广告主经 `/advertisers?am=<id>` / `?bd=<id>` 字段过滤解析（Teensing **无** `/advertisers/am/{id}` 嵌套路由，实测 404）。

---

## 前端胶囊（显式透传）对照

若前端用胶囊直接透传 `analysis_type`，可对应提问意图：

| 维度 | analysis_type | 必填 params |
|---|---|---|
| Campaign | `campaign_detail` | `campaign_id` |
| Advertiser | `advertiser_deepdive` | `advertiser_id` |
| Publisher | `publisher_deepdive` | `publisher_id` |
| Package | `pkg_deepdive` | `package_name` |
| Owner | `owner_performance` | `owner_user_id` + `owner_role` |
| 流量策略 | `traffic_policy_check` | `campaign_id` 或 `publisher_id` |
| KPI 对比 | `kpi_compare` | `campaign_id` |
| 排名诊断 | `campaign_ranking` | — |
| 账户全景 | `account_overview` | — |
| 渠道质量 | `publisher_deepdive` | `publisher_id`（可选） |
| 放量容量 | `scaling_capacity` | — |
| 日报 | `daily_summary` | — |
| 财务对账 | `finance_check` | `params.report_month` |
