# Teensing 对接指南（拉取 Tess 实时 KPI 异常结果）

> 适用对象：Teensing 后端/SaaS 工程同学
> 前置：Tess 已部署并启用定时预警（见第 1 节），对外暴露 `GET /tess/realtime-kpi/alerts`

---

## 0. 一句话架构

```
Teensing 真实数据 ──(Tess 每小时主动拉取 /overview/realtime-kpi)──▶ Tess
                                                                    │
                                          Tess 诊断 + 落预警库 ──────┤
                                                                    ▼
                                       Teensing 后端 ──(定时轮询 GET /tess/realtime-kpi/alerts)──▶ 展示/告警
```

Tess 不主动推，Teensing **自己定时来拉**。本指南只讲 Teensing 侧如何拉，以及 Tess 侧要提前开好的开关。

---

## 0.1 Source 模型（重要：anomaly-warning 与 fluctuation 不是并列的两种告警）

`GET /tess/alerts` 按 `source` 过滤，落库后**只有 2 种 source**，不要和「上游接口」混淆：

| 落库 source | 实际包含的上游数据 | Tess 拉取的接口 |
|---|---|---|
| `anomaly-warning` | 被预警实体 **+** 涨跌榜（fluctuation）按 `campaign_id` 合并后的结果 | `GET /overview/ranking/anomaly-warning` **与** `GET /overview/ranking/fluctuation`（两接口一起拉、合并） |
| `realtime-kpi` | 实时大盘小时级骤降 | `GET /overview/realtime-kpi` |

关键澄清：

- **fluctuation 不是独立的 source。** 在 `TeensingDataConnector.fetch_recent_anomalies()` 中，代码先拉 `anomaly-warning` 拿到被预警实体，再拉 `fluctuation` 拿到 rising/falling 榜（含 `revenue_change`），然后用 `setdefault` 把 fluctuation 的环比数据补到同名 campaign 上，合并成**一条**记录，统一打 `source="anomaly-warning"` 存库。若 `anomaly-warning` 接口返回空，代码会退化为直接用整张 `fluctuation` 榜当异常源——结果同样存为 `source="anomaly-warning"`。
- 真正与 `anomaly-warning` **并列**的第二种 source 是 **`realtime-kpi`**（实时大盘维度，按小时曲线检测骤降），走 `GET /overview/realtime-kpi` → `extract_realtime_anomalies()` 独立分支。
- 因此 Teensing 拉数据时：要 campaign 级异常用 `GET /tess/alerts?source=anomaly-warning`（里面已含 fluctuation 合并结果）；要实时大盘异常用 `GET /tess/realtime-kpi/alerts`。**不存在** `source=fluctuation` 这种端点，也无需为 fluctuation 单独拉取。

---

## 1. Tess 侧前置（需 Tess 运维确认已开启）

确保 Tess 服务 `docker-compose` 环境变量包含：

```bash
TESS_SCHEDULE_ENABLED=true          # 开启每小时自动诊断
TESS_SCHEDULE_INTERVAL=3600         # 间隔秒，默认 1 小时
TESS_SCHEDULE_LIMIT=20
TESS_SYSTEM_TOKEN=<共享服务 token>  # Tess 拉 Teensing 数据时用的 Bearer；留空回退 TESS_DATA_API_KEY
TESS_DATA_CONNECTOR=teensing        # 接真实数据（非 mock）
TESS_DATA_API_BASE_URL=https://<saas-host>/api/v1
TESS_REALTIME_DROP_THRESHOLD=0.3    # 实时 KPI 同比最低跌幅门槛（0.3=跌幅超 30% 才报；0.0=任何下跌都报）
TESS_API_KEY=<强随机值>             # 开启拉取接口鉴权（Teensing 需带 X-API-Key）
```

开启后 Tess 每小时会把 realtime-kpi 异常诊断写入预警库，Teensing 即可拉取。
（预警库后端由 `TESS_DATABASE_URL` 配置：默认本地 SQLite，生产建议 PostgreSQL；详见 DEPLOY.md §12.0。）
手动立即产出一轮（联调用）：`POST /tess/cron/run {"limit":20}`。

---

## 2. Teensing 侧对接：三件事

1. **知道端点与密钥**：`GET https://<tess-host>:8080/tess/realtime-kpi/alerts`，请求头 `X-API-Key: <TESS_API_KEY>`。
2. **定时轮询**：用定时任务（cron / 云函数 / 消息队列延迟消息）每小时拉一次。
3. **去重 + 展示**：用响应里的 `as_of` 去重，按 `status` 过滤，把 `diagnosis` 字段映射成页面/告警。

---

## 3. 响应结构（Teensing 按它解析）

```json
{
  "as_of": "2026-07-30 13:00:00",        // 批次时间，相同 as_of = 同一批，用于去重
  "generated_at": "2026-07-30 13:05:12", // 响应生成时间
  "count": 1,
  "items": [
    {
      "id": 42,
      "run_time": "2026-07-30 13:00:00",
      "event_id": "REALTIME-GAP-09-17",  // 异常标识：REALTIME-GAP-起止小时 / REALTIME-DROP-小时
      "status": "DIAGNOSED",             // DIAGNOSED / DIAGNOSED_SUSPECT / INCONCLUSIVE
      "confidence": 0.91,
      "source": "realtime-kpi",
      "diagnosis": {                      // Gatekeeper 归一化诊断（稳定字段，见第 5 节）
        "status": "DIAGNOSED",
        "confidence": 0.91,
        "summary": "数据中断：09–17 时段实时收益掉零",
        "primary_contributor_id": "realtime-kpi:revenue",
        "root_cause_analysis": {
          "primary_factor": "实时采集链路中断或接口滞后",
          "causal_chain": ["采集掉零", "同比缺失", "收益异常"]
        }
      },
      "anomaly_metadata": {               // 原始异常数值（Tess 透传，供直接展示）
        "event_id": "REALTIME-GAP-09-17",
        "trigger_time": "hour 09-17",
        "target_metric": "Revenue",
        "current_value": 0.0,             // 今日（异常时段）收益
        "benchmark_value": 12345.6,       // 昨日同期基准收益
        "severity": "HIGH",
        "calculated_loss": 12345.6        // 估算损失（= 昨日基准合计）
      },
      "acked_at": null,                  // 运营确认时间；null=尚未确认（默认拉取会出现）
      "resolution": null,                // 运营处理结论：acknowledged / resolved / false_positive
      "acked_by": null,                  // 处理人（运营身份）
      "ack_note": null                   // 处理备注
    }
  ]
}
```

### 3.1 顶层 `campaign_id` / `publisher_id`（定位字段，必读）

为方便 Teensing **直接按 campaign 维度聚合/跳转/去重**，每条 `item` 顶层都新增了这两个定位字段（不再需要到 `anomaly_metadata` 或 `event_id` 里抠）：

```json
{
  "id": 2635,
  "event_id": "AW-CAMP-6844120",       // anomaly-warning 源：本就是 campaign_id 加前缀
  "source": "anomaly-warning",
  "campaign_id": "6844120",            // 顶层：本条告警归属的 campaign（纯数字字符串）
  "publisher_id": "1000233",           // 顶层：publisher 维度；可能为空（见下表说明）
  "diagnosis": { ... },
  "anomaly_metadata": { ... }
}
```

| 字段 | 含义 | 取值规则 | 何时为 null |
|---|---|---|---|
| `item.campaign_id` | 告警归属的 campaign | 优先取 `anomaly_metadata.campaign_id` → 否则取**纯数字的 `event_id`**（anomaly-warning 真实记录的 event_id 即 campaign_id）→ 否则 null | `realtime-kpi` 源（event_id 形如 `RT-HOUR-13`）、或异常无 campaign 维度时 |
| `item.publisher_id` | 告警归属的 publisher（版位/媒体） | 取 `anomaly_metadata.publisher_id` | ① `realtime-kpi` 源；② anomaly-warning 的**历史记录**（旧代码落库时未存 publisher_id，故为空）；③ 之后的新记录会带真实值 |

**使用要点（避免误用）：**
- `diagnosis.primary_contributor_id` **不是** campaign_id —— 对大多数 anomaly-warning 它其实是「主因维度/版位名」（如 `recl-domino-ID(...)`、`ru.yandex.music_RU`），拿它当 campaign_id 会错。请一律用顶层 `campaign_id`。
- `realtime-kpi` 源的告警没有单一 campaign 维度，两字段均为 `null`，按"大盘级"处理即可，不要拿 `RT-HOUR-13` 这类 event_id 当 campaign_id。
- 这两条字段由**读侧自动抽取 + 写侧落库**共同保证（详见 §7「已实现」的最新条目）：历史的裸数字 event_id 记录部署后立即自动补出 `campaign_id`；`publisher_id` 仅演示数据与新记录带值，历史真实记录留空。

无数据时：`{ "as_of": null, "count": 0, "items": [] }` —— 正常，按"无异常"处理即可。

> `anomaly_metadata` 与 `diagnosis` 是**两套独立信息**：`diagnosis` 是 LLM 的文字归因结论，`anomaly_metadata` 是 Tess 检测出的**原始数值事实**（今日值 / 昨日基准 / 严重度 / 估算损失）。展示告警卡片时建议两者都用地上——数值用 `anomaly_metadata`，结论文字用 `diagnosis.summary`。


---

## 4. 轮询客户端代码（直接可用）

### 4.1 Python（requests）

```python
import os, time, requests

TESS_HOST = os.getenv("TESS_HOST", "https://<tess-host>:8080")
TESS_API_KEY = os.getenv("TESS_API_KEY", "<TESS_API_KEY>")
POLL_URL = f"{TESS_HOST}/tess/realtime-kpi/alerts"

# 记住上一次处理过的批次，避免重复告警
_last_as_of = None

def pull_once():
    global _last_as_of
    try:
        resp = requests.get(POLL_URL, headers={"X-API-Key": TESS_API_KEY}, timeout=10)
    except requests.RequestException as e:
        print(f"[tess] 拉取失败（网络）: {e}")
        return
    if resp.status_code == 401:
        print("[tess] 401 鉴权失败：检查 X-API-Key / TESS_API_KEY")
        return
    if resp.status_code != 200:
        print(f"[tess] 非预期状态码 {resp.status_code}")
        return

    data = resp.json()
    as_of = data.get("as_of")
    if as_of == _last_as_of:
        return  # 同一批次，已处理过，跳过
    _last_as_of = as_of

    for item in data.get("items", []):
        handle_alert(item)

def handle_alert(item):
    diag = item.get("diagnosis", {})
    status = diag.get("status")
    if status not in ("DIAGNOSED", "DIAGNOSED_SUSPECT"):
        return  # 只处理已确诊/疑似，INCONCLUSIVE 转人工不自动告警
    print(f"[告警] {item['event_id']} | {diag.get('summary')} "
          f"| 主因={diag.get('primary_contributor_id')} | 置信度={diag.get('confidence')}")
    # TODO: 这里把告警推给运营。运营在 Teensing 侧「查看/解决/确认正常波动」后，
    #       调 ack_alert(item["id"], "resolved" | "acknowledged" | "false_positive") 回写 Tess，
    #       之后该告警默认不再出现在拉取结果里（见下方 ack_alert）。

def ack_alert(alert_id: int, resolution: str, acked_by: str = None, note: str = None):
    """运营确认/处理回写：标记某告警已处理，Tess 落库后默认拉取不再返回它。

    resolution 三选一：
      "acknowledged"  - 运营已查看/知晓
      "resolved"      - 运营已解决线上问题
      "false_positive"- 运营确认是正常流量波动/误报
    """
    payload = {"resolution": resolution}
    if acked_by: payload["acked_by"] = acked_by
    if note: payload["note"] = note
    try:
        r = requests.post(
            f"{POLL_URL.rsplit('/', 1)[0]}/tess/alerts/{alert_id}/ack",
            json=payload, headers={"X-API-Key": TESS_API_KEY}, timeout=10,
        )
        if r.status_code == 200:
            print(f"[ack] 告警 {alert_id} 已标记 {resolution}")
        elif r.status_code == 404:
            print(f"[ack] 告警 {alert_id} 不存在（可能已过期）")
        else:
            print(f"[ack] 异常状态码 {r.status_code}")
    except requests.RequestException as e:
        print(f"[ack] 回写失败（网络）: {e}")  # 失败不影响主流程，下轮可重试

# 放到 cron / 定时循环里，每小时一次即可（Tess 也是每小时产一批）
if __name__ == "__main__":
    while True:
        pull_once()
        time.sleep(3600)
```

### 4.2 Node / TypeScript（fetch，后端用）

```typescript
const TESS_HOST = process.env.TESS_HOST ?? "https://<tess-host>:8080";
const TESS_API_KEY = process.env.TESS_API_KEY ?? "<TESS_API_KEY>";
const POLL_URL = `${TESS_HOST}/tess/realtime-kpi/alerts`;

let lastAsOf: string | null = null;

async function pullOnce() {
  try {
    const resp = await fetch(POLL_URL, { headers: { "X-API-Key": TESS_API_KEY } });
    if (resp.status === 401) { console.error("[tess] 401 鉴权失败"); return; }
    if (!resp.ok) { console.error(`[tess] 状态码 ${resp.status}`); return; }
    const data = await resp.json();
    if (data.as_of === lastAsOf) return;   // 同批去重
    lastAsOf = data.as_of;
    for (const item of data.items ?? []) handleAlert(item);
  } catch (e) {
    console.error("[tess] 拉取失败:", e);
  }
}

function handleAlert(item: any) {
  const diag = item.diagnosis ?? {};
  if (!["DIAGNOSED", "DIAGNOSED_SUSPECT"].includes(diag.status)) return;
  console.log(`[告警] ${item.event_id} | ${diag.summary} | 主因=${diag.primary_contributor_id}`);
}

// 每小时轮询一次
setInterval(pullOnce, 3600_000);
```

### 4.3 最小验证（curl）

```bash
curl "https://<tess-host>:8080/tess/realtime-kpi/alerts?limit=50" \
  -H "X-API-Key: <TESS_API_KEY>"
```

---

## 5. diagnosis 字段映射（稳定，可直接展示）

| 字段 | 含义 | Teensing 用法 |
|------|------|---------------|
| `diagnosis.status` | `DIAGNOSED` / `DIAGNOSED_SUSPECT` / `INCONCLUSIVE` | 仅前两者自动告警；INCONCLUSIVE 转人工 |
| `diagnosis.confidence` | 0~1 置信度 | 排序/阈值展示 |
| `diagnosis.summary` | 一句话归因结论 | 直接展示为告警标题/描述 |
| `diagnosis.primary_contributor_id` | 主因维度/实体 ID | 展示"主因对象" |
| `diagnosis.root_cause_analysis.primary_factor` | 主因文字 | 详情展开 |
| `diagnosis.root_cause_analysis.causal_chain` | 因果链数组 | 详情时间线 |

`anomaly_metadata`（Tess 透传的原始数值，建议直接展示）：

| 字段 | 含义 | Teensing 用法 |
|------|------|---------------|
| `current_value` | 异常时段今日值（如收益） | 展示"今日" |
| `benchmark_value` | 昨日同期基准值 | 展示"昨日基准"、算跌幅 |
| `severity` | `HIGH` / `MEDIUM` / `LOW` | 告警级别配色/过滤 |
| `calculated_loss` | 估算损失（≈昨日基准合计） | 展示影响面 |
| `trigger_time` / `event_id` | 时段 / 异常标识 | 定位 |

运营确认字段（Teensing 回写后由 Tess 原样返回，详见第 4 节）：

| 字段 | 含义 | Teensing 用法 |
|------|------|---------------|
| `acked_at` | 运营确认时间（ISO，null=未确认） | 判断是否已处理 |
| `resolution` | `acknowledged` / `resolved` / `false_positive` | 区分"已读/已解决/误报正常波动" |
| `acked_by` | 处理人（运营身份） | 审计/追责 |
| `ack_note` | 处理备注 | 展示处理说明 |

> ⚠️ 注：上面两段（`diagnosis` 与 `anomaly_metadata`）一起构成告警完整信息。第 7 节列的"透传原始数值"增强**已完成**，现已默认返回，无需额外开启。


---

## 6. 错误处理与节奏建议

- **401**：`X-API-Key` 与 Tess 的 `TESS_API_KEY` 不一致 → 找 Tess 运维对齐密钥。
- **网络/超时**：指数退避重试，不要因一次失败丢批；Tess 下一小时会再产一批。
- **空 `items`**：无异常，正常。
- **去重**：务必用 `as_of` 判等，避免同一批重复告警（Teensing 比 Tess 跑得勤时会重复拿到同批）。
- **节奏**：Teensing 轮询间隔 ≤ Tess 产出间隔（默认都 1 小时）即可；跑更勤也只是重复拿同批，靠 `as_of` 去重。

---

## 7. 增强能力清单

### 已实现（开箱即用）
1. **按 severity 过滤**：`?min_severity=MEDIUM`（或 `HIGH`）只拉 >= 指定级别的告警，避免 `LOW` 微跌刷屏。例：`GET /tess/realtime-kpi/alerts?min_severity=MEDIUM`。
2. **增量游标（since_as_of）**：`?since_as_of=2026-07-30 13:00:00` 只返回比该批次更新的告警（可能跨多批）。Teensing 用上次拿到的 `as_of` 传入即可只拿新增，省流量且无需客户端再比对去重。例：`GET /tess/realtime-kpi/alerts?since_as_of=2026-07-30 13:00:00`。
3. **运营确认回写**：`POST /tess/alerts/{id}/ack`（body `{"resolution":"acknowledged|resolved|false_positive","acked_by":"alice","note":"..."}`）标记告警已被运营处理。标记后**默认拉取（`include_acked=false`）不再返回该告警**，避免已处理项刷屏；做历史/审计视图时传 `?include_acked=true` 仍可查回。Teensing 客户端示例见第 4 节 `ack_alert()`。
4. **顶层定位字段 `campaign_id` / `publisher_id`**：每条 `item` 顶层直接带 `campaign_id`（anomaly-warning 源必填）与 `publisher_id`，Teensing 无需从 `event_id`/`anomaly_metadata` 里抠。字段契约与取值规则见 [§3.1](#31-顶层-campaign_id--publisher_id定位字段必读)。注意 `diagnosis.primary_contributor_id` **不是** campaign_id（它多半是版位/主因名），一律用顶层 `campaign_id`；`realtime-kpi` 源两字段均为 `null`。

### 如何判断"运营是否已知晓/处理"？
- Tess **不知道**运营在 Teensing 侧做了什么，需由 Teensing 在处理动作（查看/解决/确认正常波动）后**反向调用 ack 接口回写**。
- 回写后该告警 `acked_at` 非空、`resolution` 记录处理结论；Tess 默认拉取自动过滤掉，等于"已闭环"。
- 三种 resolution 语义：`acknowledged`=已读知晓、`resolved`=已解决、`false_positive`=误报/正常流量波动（运营确认无异常）。

> 注：「透传原始数值」也已实现（见第 3/5 节 `anomaly_metadata`），现已默认返回，无需额外开启。

### 可选增强（如需再加）
- 批量确认接口（一次 ack 多条）；确认状态变更事件推送（webhook）；回写失败重试策略等。

---

## 8. 附录：Tess 如何判定「异常」（Teensing 同学了解即可）

数据来源：Tess 每小时拉 `GET /overview/realtime-kpi`，返回**逐小时** `today_*` / `yesterday_*` 同比
（hour 00–23，字段如 `today_revenue` / `yesterday_revenue`）。

判定分三步，**核心是先锚定"数据更新到哪小时"，再只判已完整过去的时段，避免误报**：

### 8.1 锚定更新小时（as_of_hour）
- 数据每小时滚动更新：接口在某时刻返回的是"截至当前已更新的小时快照"，**尾部 hour 的 `today=0` 通常只是"还没产生/接口滞后"，不是真掉零**。
- Tess 默认从数据自身推断 `as_of_hour` = 最后一个 `today_revenue > 0` 的小时。
- 例：快照里 00–08 有值、09–23 为 0 → `as_of_hour = 08`，09–23 视为"未来/未就绪"，**不参与掉零判定**。

### 8.2 延迟容忍窗口（grace_hours，默认 1）
- 即便"已发生"的小时，刚过去的 15–30 分钟内 `today` 也可能还是 0（接口滞后）。
- `grace_hours` 内（当前小时及前 1 小时）的 `today=0` 视为"数据未就绪"，**不报**。
- 仅对 `h <= as_of_hour - grace_hours` 的"已完整过去"小时做判定。

### 8.3 异常规则（任何下跌都算异常，按跌幅分档）

1. **数据掉零（数据中断）**：`yesterday_revenue > 0` 且 `today_revenue <= 0` → 跌幅 100% → `HIGH`。
   - **连续掉零小时聚合成一条告警** `REALTIME-GAP-{起}-{止}`（如 `REALTIME-GAP-09-17`），不逐小时刷屏。
2. **同比下跌（任何跌幅都判异常）**：`today_revenue > 0` 且 `today < yesterday`（即 `drop = (yesterday - today)/yesterday > 0`）即判异常，按跌幅分档严重度：
   - `drop <= 30%` → **LOW**
   - `30% < drop < 50%` → **MEDIUM**
   - `drop >= 50%` → **HIGH**
   - 单小时一条 `REALTIME-DROP-{小时}`。
   - `TESS_REALTIME_DROP_THRESHOLD`（默认 `0.3`）作为「最低跌幅门槛」：`drop > 阈值` 才上报，默认 `0.3` 表示跌幅超 30% 才报；设为 `0.0` 则任何下跌都报（噪音大）。

> 说明：之前"跌满 30% 才报"的口径已改为"任何下跌都报"，严重度只是分档（LOW/MEDIUM/HIGH），不再作为是否上报的门槛。

### 8.4 判定后
- 每条命中的 Context 送入 LLM 诊断（Gatekeeper 归一化），结论进 `diagnosis`；原始数值进 `anomaly_metadata`（含 `severity`/`current_value`/`benchmark_value`）；两者一起落预警库，供 Teensing 拉取。
- **若所有 `today_revenue` 全为 0**（无法锚定）→ 直接跳过、不报，避免整表空时误报。

### 8.5 可调参数
| 参数 | 默认 | 作用 |
|------|------|------|
| `TESS_REALTIME_DROP_THRESHOLD` | `0.3` | 最低跌幅门槛（默认 0.3 = 跌幅超 30% 才报；0.0 = 任何下跌都报） |
| `TESS_REALTIME_GRACE_HOURS` | 1 | 延迟容忍窗口（小时） |
| `TESS_SCHEDULE_INTERVAL` | 3600 | 检测频率（秒） |

---

## 9. 生产部署：Nginx 转发 `/tess`（注入 X-API-Key）

dev 环境 Teensing 用 Vite 代理（`/tess` → `Tess host:8080`）即可。生产部署时，**不要**把 `TESS_API_KEY` 暴露给前端，而是在 Teensing 站点的 Nginx 上加一段：把 `/tess/*` 转发到 Tess 并**由 Nginx 注入 `X-API-Key`**。前端只调自己域名的 `/tess/*`，密钥在网关侧注入。

完整片段见仓库 `deploy/nginx-tess-proxy.conf`，核心如下（放入 Teensing 站点的 `server {}` 内）：

```nginx
location /tess/ {
    # 仅允许 Teensing 后端/内网访问（接口含写入，勿裸露公网）
    # allow 10.0.0.0/8; deny all;

    # 注入共享密钥（与 Tess 启动的 TESS_API_KEY 一致）
    proxy_set_header X-API-Key "<TESS_API_KEY>";
    # 转发到 Tess（保留 /tess/ 前缀）
    proxy_pass http://<TESS_HOST>:8080/tess/;

    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location = /tess { return 301 /tess/; }
```

替换占位符：

- `<TESS_HOST>`：Tess 服务可达地址。若 Nginx 与 Tess 同机，`127.0.0.1` 或 docker 网关 `172.17.0.1`；跨机用内网 IP（**不要**用公网 IP 裸跑）。
- `<TESS_API_KEY>`：与 Tess 容器 `TESS_API_KEY` 环境变量**完全一致**的值。

### 9.1 为什么这样更安全
- 前端（浏览器）调用的是 Teensing 自己域名下的 `/tess/*`，同源、无 CORS 问题，且**永远拿不到密钥**。
- 密钥只在 Teensing 服务端（Nginx）与 Tess 之间传递。
- `POST /tess/alerts/{id}/ack` 是写操作，建议用 `allow/deny` 限制在 Teensing 内网/后端 IP，避免被公网任意 ack。

### 9.2 对应的 Tess 侧配置
Nginx 注入的 `<TESS_API_KEY>` 必须与 Tess 容器启动时的 `TESS_API_KEY` 相同（见 `.env` 与 `docker-compose.yml` 的 `TESS_API_KEY: "${TESS_API_KEY}"`）。Tess 端设了该值后，所有 `/tess/*` 接口强制校验 `X-API-Key`，否则 401。

---

## 10. 数据分析（主动式 BI 助手）

除了「被动告警排查」，Tess 还提供**主动式商业智能**能力：运营在输入框提问，Tess 主动拉取 Teensing 业务数据并由 LLM 生成结构化简报。

### 10.1 端点

```
POST /tess/analytics
Content-Type: application/json
X-API-Key: <TESS_API_KEY>            # Tess<->Teensing 共享密钥（网关注入）
X-Teensing-Token: <终端用户 SaaS access_token>   # 可选：按「该用户」权限取数；缺省回退系统 token
X-Operator-Id: <运营ID>             # 可选：仅审计归因

{
  "analysis_type": "daily_summary" | "scaling_opportunity" | "finance_check"
                | "account_overview" | "publisher_deepdive" | "scaling_capacity"
                | "campaign_detail" | "advertiser_deepdive" | "traffic_policy_check" | "kpi_compare",
  // 实体下钻类型（campaign_detail / advertiser_deepdive / traffic_policy_check / kpi_compare）
  // 需要对应实体 id，可放在顶层或 params 内：
  "campaign_id": 5845554,        // campaign_detail / kpi_compare 必填
  "advertiser_id": 1000734,      // advertiser_deepdive 必填
  "publisher_id": 1000571,       // traffic_policy_check 选填（也可只传 campaign_id 自动解析 publisher）
  "params": { "report_month": "2026-08" }   // 仅 finance_check 可选
}
```

**按用户权限取数（重要）**：`X-Teensing-Token` 是**终端运营/用户的 SaaS access_token**，Tess 原样透传给 Teensing 作为其取数凭据；Teensing 按该用户的 RBAC/数据权限返回数据 —— **用户看不到其无权访问的 Campaign / 广告主 / 营收**。缺失时回退到 Tess 的 `TESS_SYSTEM_TOKEN`（系统级、无按人过滤，仅限内部 / 定时任务等无终端用户场景）。生产（Teensing 真实连接器）下若两者皆无，接口返回 `400`。

> ⚠️ **两个密钥切勿混淆**：`X-API-Key` 是 **Tess↔Teensing 的共享密钥**，由 Teensing 的 Nginx/网关层统一注入（前端永不接触）；`X-Teensing-Token` 是**每个登录用户自己的 access_token**，必须由 Teensing 前端从当前用户会话里取出后**逐请求带上**。**不要把 `X-Teensing-Token` 写进 Nginx 注入**——否则所有请求共用同一个 token，按用户隔离就失效了。

返回：

```json
{
  "analysis_type": "daily_summary",
  "report": "📊 **数据复盘 / 洞察摘要**\n- ...\n💡 **潜能点 / 风险点**\n- ...\n🚀 **推荐执行动作**\n1. ...",
  "context_summary": {
    "analysis_type": "daily_summary",
    "date_or_month": "2026-08-03",
    "errors": [],
    "operator_id": "anonymous",     // 来自 X-Operator-Id，审计用
    "token_mode": "user"            // "user"=按调用方 X-Teensing-Token 权限取数；"system"=系统 token
  }
}
```

`report` 为 Markdown 简报（含 📊/💡/🚀 三段），前端可直接渲染；`errors` 列表非空表示部分数据源拉取失败（接口已做单源容错，不会整轮崩）；`token_mode` 让前端 / 审计方一眼看出本次结果是否按用户权限隔离。

### 10.2 真实可用 API 完整目录（已用生产 token 探测确认）

> 下列端点均已用 `TESS_SYSTEM_TOKEN` 对 `TESS_DATA_API_BASE_URL` 实测存在并返回数据；BI 助手的数据加载器 `fetch_bi_analysis_context()` 即从其中按需编排。分五组：**A 大盘与绩效 / B 报表与聚合 / C 质量与扣量 / D 主数据目录 / E 实体下钻与流量策略（随完全体路由网关新增）**。

#### A. 大盘与绩效（Overview & Performance）

| 端点 | 返回形状 | 关键字段 | 已用于场景 |
|---|---|---|---|
| `GET /overview/daily-kpi` | `list[7]` | `date, clicks, conversions, revenue, payout, profit` | `daily_summary` |
| `GET /overview/ranking` | `list[10]` | `rank, advertiser_id, advertiser_name, clicks, conversions, cvr, revenue, payout, margin, revenue_change, revenue_change_status` | `daily_summary` / 账户全景（规划） |
| `GET /overview/ranking/fluctuation` | `{rising[], falling[]}` | `campaign_id, name, revenue_change, …` | `daily_summary` |
| `GET /overview/ranking/anomaly-warning` | `{total, page, page_size, items[]}` | 被预警实体（campaign 级） | 异常复盘（规划） |
| `GET /overview/realtime-kpi` | `{items[24 小时]}` | `hour, today_revenue, today_clicks, today_conversions, yesterday_*` | 实时大盘快照（规划） |

#### B. 报表与聚合（Reports & Aggregation）

| 端点 | 返回形状 | 关键字段 | 已用于场景 |
|---|---|---|---|
| `GET /report` | `{total, page, page_size, items[]}` | `dimensions=campaign,publisher` / `date,hour,campaign`；`revenue, payout, profit, margin, cr` | `scaling_opportunity` / 多维透视（规划） |
| `GET /report/month` | `{total, page, page_size, items[], total_data{…}}` | `total_data: revenue, scrub_revenue, calc_revenue, payout, scrub_payout, calc_payout` | `finance_check` |

#### C. 质量与扣量（Quality & Scrub）

| 端点 | 返回形状 | 关键字段 | 已用于场景 |
|---|---|---|---|
| `GET /campaign-quality/publisher` | `{items[]}` | `publisher_id, publisher_name, publisher_status, total{…}, <date>:{conversions, postback_conversions, clicks, q1_*}` | `scaling_opportunity` / 渠道质量（规划） |

#### D. 主数据目录（Master Data & Catalogs）—— 新发现，可扩展

| 端点 | 返回形状 | 关键字段 | 可支撑的扩展场景 |
|---|---|---|---|
| `GET /campaigns` | `{total, page, page_size, items[]}` | `id, name, advertiser_id, package_name, country, cap, click_cap, payout_event, status, weget, kpi, …` | 账户全景 / 放量容量评估（已实现） |
| `GET /publishers` | `{total, page, page_size, items[]}` | `id, name, margin, payment_terms, click_caps, conversion_caps, postback_url, …` | 渠道横向对比（已实现） |
| `GET /advertisers` | `{total, page, page_size, items[]}` | `id, name, bd, am, margin, contract_valid_to, …` | 广告主结构复盘（已实现） |

#### E. 实体下钻与流量策略（新增，已探测确认）

随「完全体路由网关」扩张接入，支撑单 Campaign / 广告主 / 流量策略 / KPI 对比四类深度下钻。

| 端点 | 入参（实测正确名） | 关键字段 | 已用于场景 |
|---|---|---|---|
| `GET /campaign-detail` | `campaign_ids`（复数） | `t_campaign_id, campaigns, events, advertisers` | `campaign_detail` |
| `GET /campaign-quality` | `campaign_ids`（复数） | 按 `time_label` 的质量时序：`conversions, postback_conversions, q1_rate, q2_rate, reject_rate` | `campaign_detail` |
| `GET /campaign-kpi-trend` | `campaign_ids`（复数） | 按 `time_label` 的指标趋势：`revenue, clicks, cvr, margin_rate, payout` | `campaign_detail` / `kpi_compare` |
| `GET /campaign-ctit-etit` | **`campaign_id`（单数！）** | `ctit, etit`（转化/事件时间分布） | `campaign_detail` |
| `GET /advertisers/{id}` | 路径参数 `id` | `name, user_name, bd, am, status, jointime` | `advertiser_deepdive` |
| `GET /advertisers/campaign-daily-kpi` | `advertiser_id` | `total, campaigns[]`（日 KPI） | `advertiser_deepdive` |
| `GET /mapping-publisher-channels` | `publisher_id` | `items[]`（渠道映射） | `traffic_policy_check` |
| `GET /replace-channels` | `publisher_id` | `items[]`（替换渠道规则） | `traffic_policy_check` |
| `GET /publisher-campaign-blocks` | `publisher_id` 或 `campaign_id` | `items[]`（屏蔽规则） | `traffic_policy_check` |

> ⚠️ **参数易错点**：`/campaign-ctit-etit` 真实入参是**单数 `campaign_id`**（其余 Campaign 级接口多是复数 `campaign_ids`）；`traffic_policy_check` 若只给 `campaign_id`，代码会先调 `/campaigns?campaign_ids=...` 反解出 `publisher_id` 再去拉映射/替换/屏蔽规则。

#### 已确认不可用（HTTP 404，勿依赖）

`/external/invoices`、`/overview/summary|performance|kpi|quality|conversion|advertiser`、`/report/day|daily|campaign|publisher`、`/campaign-quality/campaign|advertiser`、`/campaign/list`、`/statistics/overview`、`/dashboard`、`/metrics`、`/kpi`。

> 注：`/report/export` 存在但返回文件流（CSV / 带 UTF-8 BOM），非 JSON，不适合直接喂给 LLM 简报，故未列入。

#### 字段与口径注意

- `/report` 维度下**转化率字段名为 `cr`（非 `cvr`）**；`margin` 为百分比数值（如 `41.4` = 41.4%）。
- `/overview/ranking` 返回的是**广告主（advertiser）维度**排名（含 `advertiser_id` / `advertiser_name`），并非 campaign 级；其 `revenue_change` 为环比金额。
- `/campaign-quality/publisher` 的扣量/质量信号在 `total` 与按日期键的对象里（如 `q1_*` 疑似质量/扣量指标），具体口径待平台侧补充。
- `/campaigns`、`/publishers`、`/advertisers` 为**主数据目录**，本身**不含营收时序**；产出带金额的洞察时需与 `/report`、`/overview/*` 联动。

### 10.3 代码位置
- 数据拼装：`tess_backend/analytics.py` → `fetch_bi_analysis_context()`（按类型编排上述接口，单源容错）
- LLM 编排：`process_data_analysis_query()`（复用 `HttpLLMClient`，`json_mode=False` 返回 Markdown）
- 路由：`app.py` → `POST /tess/analytics`
- 提示词：同文件 `BI_SYSTEM_PROMPT`（观点先行 / 数据支撑 / 动作导向）
- 验证脚本：`verify_analytics.py`（仓库根）—— 支持模块直跑（`--no-llm` 仅校验数据）、带真实 LLM 直跑、以及部署后 HTTP 模式（`--http <url> --api-key <key>`）三种方式验证六类场景。

### 10.4 已知缺口
- **`/report/month` 参数名易错（已修正）**：Teensing 该接口真实参数名为 `start_date`/`end_date`，且接受 `YYYYMM`（无横杠）格式；早期代码误传 `report_month=2026-06`（带横杠）会被接口忽略 → 返回全 0 的"本月暂无数据"。现 `finance_check` 已改为内部把调用方契约 `report_month=2026-06` 转换成 `start_date=202606&end_date=202606` 透传，实测 6 月可返回真实数据（revenue≈717,744、calc_revenue≈714,462、payout≈403,454 等）。调用方仍按 `report_month` 传参即可，无需改前端。
- 该能力依赖 `TESS_DATA_CONNECTOR=teensing` 的真实连接器（mock 模式不支持）。

### 10.5 已实现的扩展场景（基于 §10.2 D/E 组接口）

下列 7 个场景已接入 `fetch_bi_analysis_context()` 与 `POST /tess/analytics`（作为给 Teensing 系统调用的后端集成面，由 Teensing 自身 UI 触发，无独立前端）：前 3 个为「账户 / 渠道 / 放量」全局研判，后 4 个为「单 Campaign / 广告主 / 流量策略 / KPI 对比」实体下钻。

| analysis_type | 业务问题 | 编排的真实接口 | 产出 |
|---|---|---|---|
| `account_overview` | 账户下广告主 / Campaign 结构、健康度 | `/campaigns` + `/advertisers` + `/overview/ranking` | 全局 campaign/advertiser 总量、首页 100 条抽样的活跃/缺失 Cap 统计、Top 广告主营收贡献 |
| `publisher_deepdive` | 各渠道（Publisher）质量与扣量横向对比 | `/publishers` + `/campaign-quality/publisher`（可选 + `/campaign-quality/publisher/channels`） | 各渠道 clicks/conversions、postback 回传缺口、q1/q2/reject 扣量率，自动标记异常渠道 |
| `scaling_capacity` | 哪些 Campaign 还有放量容量且回报好 | `/report`（按 `profit` 降序）聚合 → 反查 `/campaigns?campaign_ids=...` 取 `cap`（可选 `/global-settings` 取全局上限） | 近 7 日按 Campaign 聚合的利润/Margin，`scaling_room`（盈利高 Margin 且 Cap 偏低）/ `over_cap_waste`（亏损仍挂 Cap）清单，`global_cap`（全局放量上限） |
| `campaign_detail` | 单个 Campaign 的微观下钻：配置 / 质量时序 / CTIT-ETIT / 指标趋势 | `/campaigns`(campaign_ids) + `/campaign-detail`(campaign_ids) + `/campaign-quality`(campaign_ids) + `/campaign-kpi-trend`(campaign_ids) + `/campaign-ctit-etit`(**campaign_id 单数**) | `campaign_config`（cap/status/kpi）、`detail`（事件结构）、`quality_timeseries`（扣量率时序）、`kpi_trend`（营收/cvr/margin 时序）、`ctit_etit` 分布 |
| `advertiser_deepdive` | 单个广告主维度：档案 + 日 KPI + 旗下 Campaign | `/advertisers/{id}` + `/advertisers/campaign-daily-kpi`(advertiser_id) | `profile`（bd/am/status）、`daily_kpi`（total + 旗下 campaign 日 KPI） |
| `traffic_policy_check` | 流量策略核查：渠道映射 / 替换渠道 / 屏蔽规则 | `/mapping-publisher-channels` + `/replace-channels` + `/publisher-campaign-blocks`（仅给 campaign_id 时先 `/campaigns` 反解 publisher_id） | `mapping_publisher_channels`（映射）、`replace_channels`（替换规则）、`blocks`（屏蔽规则） |
| `kpi_compare` | 单 Campaign 指标趋势与同期对比 | `/campaign-kpi-trend`(campaign_ids) + `/campaign-compare`(campaign_ids + date_start/date_end) | `kpi_trend`（近期时序）、`compare`（区间环比对照，默认最近 7 日→今日） |

> 前三个场景（`account_overview` / `publisher_deepdive` / `scaling_capacity`）支持「全局/账户级」自动研判；后四个（`campaign_detail` / `advertiser_deepdive` / `traffic_policy_check` / `kpi_compare`）为**实体下钻类型**，必须由请求显式传入实体 id（顶层或 `params` 内），或在 `/tess/ask` 里由问题正则自动抽取（见 §10.6 ②）。

**实现要点（实测验证）：**
- `/campaigns` 支持 `campaign_ids=逗号列表` 精确过滤（上限 `page_size=100`）；`scaling_capacity` 先聚合 `/report` 拿到相关 campaign_id，再分批（每批≤100）回查 cap，避免 3M 全量拉取与 422 报错。
- `/campaign-quality/publisher` 的扣量/质量信号在 `total` 聚合对象里：`q1_rate` / `q2_rate` / `reject_rate` 为扣量率，`postback_conversions` 与 `conversions` 之差为回传缺口。
- `account_overview` 的 active/inactive/missing_cap 仅来自首页 100 条**抽样**，文案已明确不可当作全局占比；全局规模以 `campaign_total` / `advertiser_total` 为准。
- 主数据接口**不含营收时序**，放量/容量类指标从 `/report` 取数；单 Campaign 时序从 `/campaign-kpi-trend` / `/campaign-quality` 取数。
- `campaign_detail` 对同一个 `campaign_id` 并发调用 5 个端点（config/detail/quality/trend/ctit），单源容错（任一个失败只在 `errors` 里体现，不影响其余）。
- `kpi_compare` 的对比区间 `date_start`/`date_end` 缺省为「最近 7 日 → 今日」；`/campaign-compare` 真实入参即这两个日期字段（非 `report_month`）。

### 10.6 自然语言问答端点 `POST /tess/ask`（Tess AI Assistant 对话接口）

面向 Teensing 前端「问一句、答一段」的对话场景。**字段契约即对接方当前约定**：请求体发 `question`，返回体取 `answer`（兼容 `.answer` / `.result` / `.data` 任一同义字段）。

**深度下钻（可选）**：`/tess/ask` 不只能答浅层全局问题，还复用 `/tess/analytics` 同一套深度取数层（`fetch_bi_analysis_context`）。上下文选择优先级（①②③ 走深度上下文，④ 走浅层兜底）：
1. **显式透传**：请求体带 `analysis_type`（前端胶囊直接透传，确定性最强）→ `route_source="explicit"`；
2. **实体正则识别（新增）**：不带 `analysis_type`、但问题里含实体 id 时，用 `extract_entity_id()` 正则抽取并映射到对应深度类型。**支持两种语序**（数字+关键字 或 关键字+数字，中间可夹 `id`/分隔符）——如 `5845554camp` / `campaign id5845554` / `campaign 5845554` / 含 `ctit|etit`（并可从 `id5845554` 紧贴写法补抽）→ `campaign_detail`（自动取 campaign_id）；`广告主 1000734` / `1000734adv` → `advertiser_deepdive`；`1000571pub` / `1000571渠道` → `publisher_deepdive`；命中即下钻，`route_source="entity"`；
3. **后端关键词推断**：都不命中时，用轻量关键词把问题映射到深度类型（如「放量空间」→ `scaling_capacity`、「对账」→ `finance_check`、`ctit`/`漏斗` → `campaign_detail`、`对比`/`趋势` → `kpi_compare`），命中即下钻，`route_source="inferred"`；
4. **浅层兜底**：①②③ 皆未命中 → 退回原「全局态势」上下文（`fetch_qa_context`），保证仍能量身答（无 `route_source` 字段）。

> 注意优先级：② 实体正则高于 ③ 关键词表，因此「帮我分析一下这个 5845554camp 的 ctit」或「campaign id5845554 的 ctit」都会精确落到 `campaign_detail`（而非被笼统的关键词命中），不再出现"数据不足"。顶层或 `params` 内显式传入的 `campaign_id` / `advertiser_id` / `publisher_id` 会直接覆盖正则推断。

```
POST /tess/ask
Content-Type: application/json
X-API-Key: <TESS_API_KEY>            # 同 §10.1，网关注入
X-Teensing-Token: <终端用户 access_token>   # 同 §10.1，按用户权限取数；缺省回退系统 token
X-Operator-Id: <运营ID>             # 可选，审计

# 方式 A：纯自由提问（后端自动判断是否下钻）
{ "question": "昨天整体营收表现如何？有没有需要重点关注的异常？" }

# 方式 B：前端胶囊显式下钻（analysis_type 与 /tess/analytics 同枚举，共 10 种）
{ "question": "各 Campaign 还有多少放量空间？", "analysis_type": "scaling_capacity", "params": {} }
# 财务对账可带月份：{ "question": "本月对账", "analysis_type": "finance_check", "params": { "report_month": "2026-08" } }
# 实体下钻：显式传 id（顶层或 params 内皆可）
{ "question": "分析 5845554 的 ctit", "campaign_id": 5845554 }
{ "question": "广告主 1000734 最近表现", "advertiser_id": 1000734 }
{ "question": "渠道 1000571 的替换与屏蔽规则", "publisher_id": 1000571 }
# 或不传 id、让问题正则自动抽取：
{ "question": "帮我分析一下这个 5845554camp 的 ctit" }   # -> 自动识别 campaign_detail + campaign_id=5845554
```

返回（深度下钻时 `context_summary` 额外回显 `analysis_type` / `route_source` / `date_or_month`）：

```json
{
  "answer": "📊 **昨日整体营收…**\n…Markdown 回答…",
  "result": "<同 answer>",
  "data":   "<同 answer>",
  "context_summary": {
    "endpoint": "/tess/ask",
    "errors": [],
    "operator_id": "op-1",
    "token_mode": "user",
    "analysis_type": "campaign_detail",       // 仅深度下钻时存在
    "route_source": "entity",                 // 仅深度下钻时存在：explicit(前端透传) | entity(问题正则识别 id) | inferred(后端关键词)
    "date_or_month": "2026-08-04"             // 仅深度下钻时存在
  }
}
```

**实现说明**：`process_question()` 先按上文优先级选定上下文——深度上下文走 `fetch_bi_analysis_context(analysis_type)`（与 `/tess/analytics` 完全同一套取数），浅层走 `fetch_qa_context()`（`/overview/daily-kpi` + `/overview/ranking` + `/overview/ranking/anomaly-warning` + `/campaign-quality/publisher`，单源容错）；两者均连同 `question` 交给 LLM（`ASK_SYSTEM_PROMPT`，`json_mode=False` 返回 Markdown），并写审计（`AUDIT.log_query`，meta 含 `analysis_type` / `route_source`）。`answer`/`result`/`data` 三者同值，对接方用哪一个都能取到回答。
- 验证：`python verify_analytics.py --ask "你的问题"`（模块直跑，需真实 LLM）或 `--http <url> --api-key <key> --ask "你的问题"`（线上端点）；加 `--analysis-type scaling_capacity` 可强制走深度下钻验证。

