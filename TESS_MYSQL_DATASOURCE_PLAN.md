# Tess 直连 MySQL 取数改造 — 实施计划（草案）

> 状态：代码已实现并本地单测全绿（8/8），待「真机对拍 0 差异」验证后铺开其余 17 种。
> 日期：2026-10-16（第七节 6 项待核实已闭环）；2026-10-16 下午实施推进中。

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
- `report_day` —— 按 `(advertiser × publisher × date)` 聚合，**无 campaign 维度**
- `report_campaign` —— 按 `(campaign × advertiser × publisher × date)` 聚合，**带 campaign_id**
- `report_hour_new` —— 小时级（列待确认）
- `report_month` —— 月级（列待确认）

`report_day` 已确认列：`conversions, payout, revenue, advertiser_id, postback_conversions, date(varchar16), publisher_id, clicks, impression`
`report_campaign` 已确认列：`date, campaign_id, advertiser_id, publisher_id, conversions, payout, revenue, postback_conversions, clicks`

**关键口径点**：两张表都**没有 `profit` 列**，只有 `revenue` 和 `payout`。`profit` 应为 `revenue - payout` 计算（对拍时重点验证是否与 `/report` 一致）。

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
| `/report?dimensions=campaign,...` | `report_campaign` | 按 campaign 维度聚合表 |
| `/report?dimensions=date/publisher` | `report_day` | 按 advertiser×publisher×date |
| `/report?dimensions=hour` | `report_hour_new` | 小时级（列待确认） |
| `/report?dimensions=month` | `report_month` | 月级；`date`=int(YYYYMM)、`campaign_id`=int（需类型强转）；含 `scrub_*` 疑似扣量值，对拍时确认用原始还是 scrub |
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
- `tess_backend/mysql_connector.py`：新增 `MysqlDataConnector`，实现与 `TeensingDataConnector` 同签名 `api_get`，按 path 路由到 `report_campaign`/`campaign`/`advertiser` 等表；`profit=revenue-payout`；`status` 数字→名称（`_camp_status_label`）；平台级选库 + 运营级 `am` 过滤。
- 新增 `self_check()`（只读自检：连通性 + 每库该广告主行数 + `date` 列格式）与 `_query_across()`（**试点期跨库回退**：默认库查不到时自动试 `TESS_MYSQL_DATABASES` 其它库，避免跨平台串档误判「无数据」；上线前建议 `TESS_MYSQL_CROSS_DB=0` 关掉）。
- `tess_backend/data_connector.py`：`get_data_connector` 增加 `operator_id` 参数 + `mysql` 分支。
- `tess_backend/app.py` / `tools_adapter.py`：`operator_id` 在实例化 connector 前取出并透传。
- `requirements.txt`：加 `pymysql>=1.1`（**务必同步到部署机**，否则容器重建后缺 pymysql）。
- `docker-compose.yml`：补 `TESS_MYSQL_*` 环境变量块（密码走宿主 env / `.env`，不写死镜像）。
- `.env`：补休眠态 `TESS_MYSQL_*`（默认 `api` 不生效；密码 `melo202003.`）。
- 单测 `tests/test_mysql_connector.py`：8 passed（Fake 注入假数据，不依赖真库）。
- 对拍脚本 `verify_mysql.py`（跑同一份 analytics 代码两路 diff）+ 独立最小自检 `verify_mysql_conn.py`（纯 pymysql，不依赖 tess_backend 导入）。

### 9.2 当前阻塞点（真机对拍）
- **现象**：`verify_mysql.py` 跑 `advertiser_id=1000853 / 2026-07-01~2026-10-31`，API 路 30 campaign（0 null、Active20/Stop8/Paused2），**MySQL 路 0 campaign、且无报错**（说明连接成功、查询跑了，但返回空）。
- **三选一根因（待真机确认）**：
  1. 数据在 `seanear` 库不在 `melocpi`（广告主 1000853 实际属 seanear 平台，而试点设 `platform_id=Melodong` → 默认库 melocpi 无数据）；
  2. `report_campaign.date` 不是 `YYYY-MM-DD`（如 `YYYYMMDD`），导致 `BETWEEN '2026-07-01' AND '2026-10-31'` 匹配不到；
  3. `advertiser_id` 在表中以其它形式存储。
- **诊断命令（在 Tess 部署机上项目目录执行，无需重建容器）**：
  ```bash
  pip install pymysql -q 2>/dev/null
  TESS_MYSQL_PASSWORD=melo202003. python3 verify_mysql_conn.py
  ```
  输出会直接显示 melocpi / seanear 各自的总条数、窗口内条数、`date` 列样本 → 一眼定位根因。
- **部署目标已厘清**：Tess 后端在 `tess.teensing.com` 对应主机（Cloudflare 反代，本机无法直连 SSH），**不在** `47.76.101.39`（该机已无 `/opt/tess`、无 tess 容器，已实测）。容器需 `docker network connect bridge tess-diagnose` 才连得到 `172.22.0.6`。

### 9.3 下一步
1. 用户在部署机跑 `verify_mysql_conn.py` → 贴回输出，定位根因。
2. 若是根因①（库选错）：修正 platform→库路由（或确认 1000853 平台归属），重跑 `verify_mysql.py` 做 0 差异对拍。
3. 若是根因②（date 格式）：在 `mysql_connector.py` 的日期过滤加格式兼容（如 `REPLACE(date,'-','')`）。
4. 0 差异通过 → 把 `TESS_DATA_CONNECTOR=mysql` 作为可选试点开关验证整链路；随后铺开其余 17 种 analysis_type 的 MySQL 映射（本期仅 `advertiser_deepdive` 已映射）。
