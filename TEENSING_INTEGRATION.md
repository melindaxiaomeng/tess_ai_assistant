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

## 1. Tess 侧前置（需 Tess 运维确认已开启）

确保 Tess 服务 `docker-compose` 环境变量包含：

```bash
TESS_SCHEDULE_ENABLED=true          # 开启每小时自动诊断
TESS_SCHEDULE_INTERVAL=3600         # 间隔秒，默认 1 小时
TESS_SCHEDULE_LIMIT=20
TESS_SYSTEM_TOKEN=<共享服务 token>  # Tess 拉 Teensing 数据时用的 Bearer；留空回退 TESS_DATA_API_KEY
TESS_DATA_CONNECTOR=teensing        # 接真实数据（非 mock）
TESS_DATA_API_BASE_URL=https://<saas-host>/api/v1
TESS_REALTIME_DROP_THRESHOLD=0.0    # 实时 KPI 同比最低跌幅门槛（0.0=任何下跌都报；调高可忽略微跌）
TESS_API_KEY=<强随机值>             # 开启拉取接口鉴权（Teensing 需带 X-API-Key）
```

开启后 Tess 每小时会把 realtime-kpi 异常诊断写入预警库，Teensing 即可拉取。
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
   - `TESS_REALTIME_DROP_THRESHOLD`（默认 `0.0`）作为「最低跌幅门槛」：`drop > 阈值` 才上报，默认 `0.0` 表示任何下跌都报；调高（如 `0.05`）可忽略 <5% 的微跌噪声。

> 说明：之前"跌满 30% 才报"的口径已改为"任何下跌都报"，严重度只是分档（LOW/MEDIUM/HIGH），不再作为是否上报的门槛。

### 8.4 判定后
- 每条命中的 Context 送入 LLM 诊断（Gatekeeper 归一化），结论进 `diagnosis`；原始数值进 `anomaly_metadata`（含 `severity`/`current_value`/`benchmark_value`）；两者一起落预警库，供 Teensing 拉取。
- **若所有 `today_revenue` 全为 0**（无法锚定）→ 直接跳过、不报，避免整表空时误报。

### 8.5 可调参数
| 参数 | 默认 | 作用 |
|------|------|------|
| `TESS_REALTIME_DROP_THRESHOLD` | `0.0` | 最低跌幅门槛（默认 0.0 = 任何下跌都报；调高可忽略微跌） |
| `TESS_REALTIME_GRACE_HOURS` | 1 | 延迟容忍窗口（小时） |
| `TESS_SCHEDULE_INTERVAL` | 3600 | 检测频率（秒） |

