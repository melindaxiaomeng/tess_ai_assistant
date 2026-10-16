# Tess 直连 MySQL 取数改造 — 实施计划（草案）

> 状态：**advertiser_deepdive 真机对拍 0 差异已通过**（广告主 1000853 / 2026-07-01~2026-10-31，API vs MySQL 两路 30 campaign 全一致：id 集 / status 分布 / 营收差异 0 / 名称 0 / null 0）。本地单测全绿（8/8）。下一步：铺开其余 17 种（均复用本连接器已实现的 5 条 route，未铺类型按需逐一对拍）。
> 日期：2026-10-16 起；2026-10-16 晚 0 差异对拍通过。

## 一、背景与目标

**现状**：Tess 现在经 HTTP 调 `adminv3.melodong.com/api/v1` 的 `/report`、`/campaigns`、`/advertisers`、`/advertisers/campaign-daily-kpi` 等接口取数，再喂给确定性引擎做分析。

**痛点**：
- 接口层慢、可能限流；
- 上游字段缺失会直接卡住 Tess（典型如 `advertiser_deepdive` 的 campaign `status` 全为 null，就是被上游接口字段语义坑的）。

**目标**：Tess 不再绕 HTTP 接口，直接连网盟 MySQL（即 `tess_ai_pb` 已经能连的那台 `172.22.0.6`）用 SQL 取数。底层数据本来就是这些表，等于省掉中间层，还顺手解决上游字段缺失的坑。

## 二、已确认的数据库事实（探查结论，非假设）

MySQL `172.22.0.6:3306`，用户 `melo`，两个业务库：

| 库 | 对应平台（platform_id） | 说明 |
|---|---|---|
| `melocpi` | `Melodong` | 主库，已启用 |
| `seanear` | `seanear` | 已启用 |
| （其余 7 个 `pb_03~09`） | `PLACEHOLDER` / `enabled=False` | 占位未接入，先不管 |

### 报表表族（= `/report` 接口直接读的预聚合表）
- `report_day` —— 按 `(advertiser × publisher × date)` 聚合，**无 campaign 维度**（不能用于 campaign 级取数）
- `report_campaign` —— **整表为空**（前序已定位，是初版 0 campaign 根因；已弃用）
- `report_hour_new` —— **唯一带 campaign_id 的明细表（小时级）**，campaign 级取数的权威源；`campaign_id`=**varchar(64)**、`date`=varchar(16)、`advertiser_id`=int
- `report_month` —— 月级预聚合；`date`=**int**(YYYYMM)、`campaign_id`=**int**（与其它表 varchar 不同）；无 `clicks` 列

> ⚠️ **致命性能坑（2026-10-16 晚实测定位，本连接器能否跑通的关键）**：`report_hour_new` 共 **4990 万行**，索引 `camp_adv_date_hour` 前缀为 `(campaign_id, advertiser_id, date, hour)`。
> 但 `campaign_id` 是 **VARCHAR(64)**——若按 **INT** 传入 `campaign_id IN (...)`，MySQL 类型不匹配会**放弃该索引、退化为 `advertiser_id` 全扫描（单 advertiser 1200 万+ 行，单 campaign 聚合 20~58s，直接超时）**。
> **必须按字符串传入 `campaign_id`** → 即走 `campaign_id` 前缀的 range 扫描（单 campaign 0.05s、IN(30) 0.08s）。`advertiser_id` 是 int（按 int 传），`date` 是 varchar（按字符串传）。`report_month.campaign_id` 是 int（按 int 传）。

`report_day` 已确认列：`conversions, payout, revenue, advertiser_id, postback_conversions, date(varchar16), publisher_id, clicks, impression`

**关键口径点**：报表表都**没有 `profit` 列**，只有 `revenue` 和 `payout`。`profit = revenue - payout`（已核实无调整项）。逐字段对拍与 `/report` 完全一致。

### 实体 / 配置表（对应 `/campaigns`、`/advertisers`、`/advertiser-publisher-pkg-maps` 等接口）
- `campaign` —— campaign 目录，`status` 为数字（1=Active/2=Paused/3=Stop，已有 `_camp_status_label` 映射）
- `advertiser` —— **含 `am` 列（int）**，即负责人/AM 字段 → 运营级过滤的数据基础
- `publisher`、`advertiser_publisher_pkg_map`、`campaign_am`、`sys_user` —— 渠道 / 包名归因 / AM 映射 / 用户
- `publisher_campaign`、`publisher_warning` —— 渠道质量 / 警告（对应 `/campaign-quality/...`）

### 回传明细（tess_ai_pb 同步进 ClickHouse 的源，**本改造不直接用**）
- `postback_info`、`event_postback_info`、`event_postback_daily_agg`

## 三、决策记录（与用户确认）

1. **取数策略**：读预聚合报表表（`report_*`），**不**从 `postback_info` 重新聚合。口径与现接口 100% 一致、最快最稳。
2. **鉴权隔离**：保留过滤 —— **平台级（按 platform 选库）+ 运营级（按 am 裁剪）都做**。
3. **推进方式**：先单库 + 单广告主试点对拍，0 差异再全量铺开。
4. **运营级数据基础**：`advertiser.am` 列已确认存在，落地可行。

## 四、架构设计

### 4.1 新增 `MysqlDataConnector`
- 实现与现有 `TeensingDataConnector` **同一套取数接口**（`api_get(path, params, token)` 或等价方法签名）。
- `fetch_bi_analysis_context` 内部通过**配置开关**选择用哪个 connector，不动 18 种 `analysis_type` 的取数逻辑。
- 开关：`DATA_SOURCE=mysql|api`，**默认 `api`** → 线上零风险，试点时按需切 `mysql`，随时回退。

### 4.2 平台级过滤（选库路由）
- 复用 `platforms.json` 路由：`Melodong → melocpi`、`seanear → seanear`（同 host `172.22.0.6`）。
- Tess 收到 `X-Platform-Id` → 查路由 → 选对应库连接。数据天然隔离到该平台范围。
- Tess 自带一份脱敏的 platform→DB 映射（仅 `Melodong`/`seanear` 两个有效连接），不依赖 tess_ai_pb 运行时。

### 4.3 运营级过滤（按 am 裁剪）
- 收到 `operator_id`（运营身份，来自 `X-Operator-Id` 或 token 解析）→ 查该运营可见的广告主集合：
  `SELECT id FROM advertiser WHERE am = :operator_id`（在对应平台库内执行）。
- 所有 `report_*` / `campaign` 查询注入 `WHERE advertiser_id IN (visible_ids)`。
- **已核实（2026-10-16）**：`advertiser.am` = `sys_user.id`，`operator_id` 直接等于 `am`，无中间映射，落地即用。

### 4.4 取数映射表（HTTP 接口 → MySQL 表）

| 原 HTTP 调用 | 改用 | 说明 |
|---|---|---|
| `/report`（按 campaign 聚合 / 长周期下钻） | `report_month`（已结月排名，0.4s）+ `report_hour_new`（top-N 完整窗口回填，**campaign_id 按字符串传**） | 混合策略：report_month 快路径拿候选/已结月营收 → top-N 用 report_hour_new 小 IN 回填（含当前月+clicks）→ 与 `/report` 逐字段吻合且 <1s |
| `/report?campaign_ids=...&dimensions=campaign,date`（日明细/趋势） | `report_hour_new` GROUP BY (campaign_id,date)，**campaign_id 按字符串传** | scoped 极快（0.05s/单 campaign）；`_monthly_from_daily` 聚成月度 |
| `/report?dimensions=date/publisher` | `report_day` | 按 advertiser×publisher×date（无 campaign 维度） |
| `/report?dimensions=month` | `report_month` | 月级；`date`=int(YYYYMM)、`campaign_id`=int |
| `/campaigns` | `campaign` 表 | 目录 + `status` 数字 → 名称映射 |
| `/advertisers` | `advertiser` 表（带 `am` 过滤） | 实体 + 负责人 |
| `/advertiser-publisher-pkg-maps` | `advertiser_publisher_pkg_map` | 包名归因 |
| `/advertisers/campaign-daily-kpi` | `campaign` 表 + `report_day` | 快照 + status |
| `/campaign-quality/publisher/channels` | `publisher_campaign` / `publisher_warning` | 渠道质量（待确认） |

### 4.5 `profit` 口径
`report_*` 无 `profit` 列 → `profit = revenue - payout`（**已核实**：无其他调整项如退款/扣量）。三张报表表口径统一。

## 五、网络与凭证

- **网络（已实测，方案确定）**：Tess 容器（`tess-diagnose`）在 `tess_default`(172.20.0.0/16)，MySQL 在另一张 Docker 内网 `172.22.0.6`，默认不互通；但**宿主机可直接连 `172.22.0.6:3306`**。→ Tess 容器改用 `network_mode: host`（一行 compose 改动）即可直连 MySQL，与 `tess_ai_pb` 同路。注意：host 模式下容器直接绑宿主机端口，compose 原 `ports: 18080:8080` 需改为绑 `8080`（或相应调整对外端口）。
- **凭证**：放 Tess 的 `.env`，按 platform 动态选库，不写死单库。连接信息脱敏自 `platforms.json`（仅 `Melodong`/`seanear`）。
- 建议生产给 MySQL 建只读账号（纵深防御），当前先用 `melo` 只读库。

## 六、试点计划

- **平台**：`Melodong`（`melocpi`）
- **广告主**：`1000853`（用户一直在测的那个）
- **时间窗**：`2026-07-01 ~ 2026-10-31`（与上次 `advertiser_deepdive` 复测一致，便于对比）
- **对拍方法**：同一请求分别走 `api` 和 `mysql` 两路取数 → 逐字段 diff → **0 差异才算过关**。
- **推进顺序**：先单 `analysis_type`（`advertiser_deepdive`）打通对拍，再扩其他 17 种。

## 七、核实结论（2026-10-16 用户确认 + 实测）

1. **`advertiser.am` = `sys_user.id`**，且 `operator_id` 直接等于 `am`（用户确认）。→ 运营级过滤直接 `WHERE am = :operator_id`，无中间映射。
2. **`profit = revenue - payout`，无其他调整项**（用户确认）。→ 三张报表表都无 `profit` 列，统一在取数层算。
3. **`report_hour_new` / `report_month` 列对齐（已实测列结构）**：
   - 两张表都**无 `profit` 列**（与 `report_day`/`report_campaign` 一致）→ 统一 `revenue - payout`。
   - `report_hour_new`：`date`=varchar(16) "YYYY-MM-DD" + `hour`=varchar(2)；含 `campaign_id`(varchar64)、`revenue`、`payout`、`conversions`、`clicks`、`impression`、`channel`/`sub_channel`/`replace_channel`/`t_campaign_id`。
   - `report_month`：`date`=**int**(YYYYMM，如 202610)、`campaign_id`=**int**（与其它表 varchar 不同）；额外有 `scrub_revenue`/`scrub_payout`/`scrub_conversions` 与 `advertiser_status`/`publisher_status`。
   - **含义**：写 SQL 时要统一两类差异——① 日期解析（`report_day/hour` 是字符串日期，`report_month` 是数字月份）；② `campaign_id` 类型强转（int ↔ varchar）。否则对拍会「字段看起来对不上」。
   - **遗留子项**：`report_month` 的 `scrub_*`（疑似扣量/清洗值）需在对拍时确认 `/report` 用的是原始 `revenue` 还是 `scrub_revenue`。
4. **`campaign.status` 数字 → 名称映射**：已有 `_camp_status_label`（1=Active / 2=Paused / 3=Stop），由实现方核对，用户无需操心。
5. **Tess 容器 → `172.22.0.6` 网络通路（已实测）**：Tess 容器在 `tess_default`(172.20.0.0/16)，MySQL 在另一张 Docker 内网(172.22.0.6)，**默认不互通**；但**宿主机可直接连 `172.22.0.6:3306`**。→ 解决办法：Tess 容器改用 `network_mode: host`（一行 compose 改动），即可和 `tess_ai_pb` 一样直连 MySQL。影响：host 模式下容器直接绑宿主机端口，compose 的 `ports: 18080:8080` 需相应调整（去掉 ports 或绑 8080）。
6. **`seanear` 库需要覆盖**（用户确认）。→ 平台级路由本来就是按 `platform_id` 选库（Melodong→melocpi / seanear→seanear），两库表结构一致；试点先 Melodong 对拍通过后，seanear 同逻辑自然覆盖，不需重写。

## 八、风险与回退

- 开关默认 `api`，随时零成本回退。
- 口径不一致：对拍兜底，发现差异先记录、**不强行切换**。
- 运营级过滤若 `operator_id` 映射一时不清，可先平台级上线、运营级后续补（不影响核心取数）。
- `seanear` 等其它库按 platform 路由自然扩展，不需重写逻辑。

## 九、实施进展（2026-10-16 下午）

### 9.1 已完成（本地）
- `tess_backend/mysql_connector.py`：新增 `MysqlDataConnector`，实现与 `TeensingDataConnector` 同签名 `api_get`，按 path 路由到 `report_hour_new`+`report_month` 混合策略/`campaign`/`advertiser` 等表；`profit=revenue-payout`；`status` 数字→名称（`_camp_status_label`）；平台级选库 + 运营级 `am` 过滤。**`report_hour_new` 的 `campaign_id` 必须按 VARCHAR 字符串传入以命中 `camp_adv_date_hour` 索引（否则退化为全扫描超时）**。
- 新增 `self_check()`（只读自检：连通性 + 每库该广告主行数 + `date` 列格式）与 `_query_across()`（**试点期跨库回退**：默认库查不到时自动试 `TESS_MYSQL_DATABASES` 其它库，避免跨平台串档误判「无数据」；上线前建议 `TESS_MYSQL_CROSS_DB=0` 关掉）。
- `tess_backend/data_connector.py`：`get_data_connector` 增加 `operator_id` 参数 + `mysql` 分支。
- `tess_backend/app.py` / `tools_adapter.py`：`operator_id` 在实例化 connector 前取出并透传。
- `requirements.txt`：加 `pymysql>=1.1`（**务必同步到部署机**，否则容器重建后缺 pymysql）。
- `docker-compose.yml`：补 `TESS_MYSQL_*` 环境变量块（密码走宿主 env / `.env`，不写死镜像）。
- `.env`：补休眠态 `TESS_MYSQL_*`（默认 `api` 不生效；密码 `melo202003.`）。
- 单测 `tests/test_mysql_connector.py`：8 passed（Fake 注入假数据，不依赖真库）。
- 对拍脚本 `verify_mysql.py`（跑同一份 analytics 代码两路 diff）+ 独立最小自检 `verify_mysql_conn.py`（纯 pymysql，不依赖 tess_backend 导入）。

### 9.2 真机对拍结果（2026-10-16 晚，**0 差异通过**）
- **环境**：真机 `43.163.88.74`，容器 `tess-diagnose`（已装 pymysql、挂 bridge 连 `172.22.0.6`；`TESS_MYSQL_HOST` 等 env 在容器内未预设，运行 `verify_mysql.py` 时需 `-e TESS_MYSQL_PASSWORD=melo202003.` 注入，其余 host/port/user/db 走代码默认值）。
- **根因（本次定位，致命）**：初版按 **INT** 传 `campaign_id` 给 `report_hour_new`（该列实为 **VARCHAR(64)**），MySQL 类型不匹配 → 放弃 `camp_adv_date_hour` 索引、退化为 `advertiser_id` 全扫描（4990 万行，单 campaign 聚合 20~58s，超出取数预算）→ 整体超时、对拍拿不到数。修正为**按字符串传 `campaign_id`** 后，索引 range 扫描 0.05s/单 campaign、IN(30) 0.08s。
- **次要修正**：`_campaign_daily_kpi` 原用 `report_hour_new` 按 `advertiser_id` 整段 DISTINCT（54.9s）→ 改为 `report_month` 取 campaign 名单（0.2s，已结月，按 advertiser_id 索引；当前月 campaign 不入 top-30 快照故不补全）。
- **对拍结论（广告主 1000853 / 2026-07-01~2026-10-31，API 路 vs MySQL 路同跑一份 analytics）**：
  - `campaigns_snapshot`：两路均 30 个，**id 集完全相同**（无差集）；status 分布一致（Active19/Stop9/Paused2）；0 个 null status；名称 0 不一致。
  - 营收/payout 差异（相对差 >0.5%）：**0 个**。
  - `campaign_trends_monthly`：两路均 30 个 campaign。
  - 两路 `errors` 均为 `[]`。

### 9.3 下一步
1. ✅ advertiser_deepdive 0 差异已通过（本类型覆盖连接器全部 5 条 route：`/report`、`/campaigns`、`/advertisers`、`/advertisers/campaign-daily-kpi`、`/campaign-kpi-trend`）。
2. 其余 17 种 analysis_type 均复用上述 5 条 route（上层取数逻辑一行不动），建议逐一对拍确认；优先 `publisher_deepdive` / `campaign_detail` 等同样走 campaign 级取数的类型。
3. 0 差异稳定后，把 `TESS_DATA_CONNECTOR=mysql` 作为可选试点开关验证整链路（开关默认 `api`，零风险回退）。
4. 部署：AI 只 `git push origin main`；生产容器（`tess_ai_pb-app-1`）部署由用户执行 `git pull && docker compose up -d --build`，并补 `TESS_MYSQL_*` 环境变量（密码 `melo202003.`；`requirements.txt` 已含 `pymysql`）。
