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
TESS_REALTIME_DROP_THRESHOLD=0.3    # 实时 KPI 同比跌幅阈值
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
      }
    }
  ]
}
```

无数据时：`{ "as_of": null, "count": 0, "items": [] }` —— 正常，按"无异常"处理即可。

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

> 注：以上为 Gatekeeper 归一化后的**稳定契约字段**。原始 `current_value/benchmark_value/severity`（实时 KPI 掉零/暴跌的具体数值）目前不在诊断输出里，需要的话可让 Tess 把原始 `anomaly_metadata` 一并透传（可选增强，见第 7 节）。

---

## 6. 错误处理与节奏建议

- **401**：`X-API-Key` 与 Tess 的 `TESS_API_KEY` 不一致 → 找 Tess 运维对齐密钥。
- **网络/超时**：指数退避重试，不要因一次失败丢批；Tess 下一小时会再产一批。
- **空 `items`**：无异常，正常。
- **去重**：务必用 `as_of` 判等，避免同一批重复告警（Teensing 比 Tess 跑得勤时会重复拿到同批）。
- **节奏**：Teensing 轮询间隔 ≤ Tess 产出间隔（默认都 1 小时）即可；跑更勤也只是重复拿同批，靠 `as_of` 去重。

---

## 7. 可选增强（按需让 Tess 加）

1. **透传原始数值**：让 Tess 在每条 alert 里附带 `anomaly_metadata`（current/benchmark/severity），Teensing 可直接展示"昨日基准 ¥X，今日 ¥0"。
2. **按 severity 过滤**：接口加 `?min_severity=HIGH`，只拉高危。
3. **增量游标**：接口加 `?since_as_of=...`，只拉比某批次更新的结果，省流量。

需要哪一项，让 Tess 侧补一个参数即可。
