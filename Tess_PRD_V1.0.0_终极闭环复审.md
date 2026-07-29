# Tess PRD V1.0.0 — 终极闭环复审（R0 / N1 / N2 / N3 代码级找漏）

> 评审对象：用户给出的"代码级密闭闭环"重构（R0 后端 Gatekeeper 伪代码、N3 前端剥离渲染、N1 三态 UI 表、N2 Severity 落表）。
> 标尺（用户原话）：**Prompt 是建议，后端 Gatekeeper（校验网关）才是法典，前端 Template 才是渲染屏障。**
> 总体判断：分层哲学完全正确，已具备交付雏形。但**代码层有 1 个致命 bug（Gatekeeper 会反向熔断），且 R0 与 N1 自相矛盾**。补上这几点才是真正的密闭闭环。

---

## 一、🔴 P0 致命 bug：Gatekeeper 会"反向熔断"（每个正常诊断都被降级）

**问题代码：**
```python
data = jsonschema.validate(instance=llm_response_json, schema=TESS_OUTPUT_SCHEMA)
if "severity" in data:   # ← data 是 None，这里直接 TypeError
```

**根因**：在 `jsonschema` 库中，`validate()` 的成功返回值是 **`None`**（它只做原地校验，不返回解析后的 dict）。所以 `data = jsonschema.validate(...)` 会把 `data` 置为 `None`，紧接着 `"severity" in None` 立即抛 `TypeError` → 被外层 `except` 捕获 → **返回 INCONCLUSIVE(confidence 0.0) 兜底**。

**后果（灾难级）**：`validate` 成功时不抛异常，但 `data` 是 None，于是**任何输入（包括完全合法的 LLM 输出）都会落到 INCONCLUSIVE 兜底**。Gatekeeper 把自己熔断了——每个 Tess 调用都显示"系统熔断 / 转人工"，产品功能全废。

> 这恰是"Prompt 不是代码"的镜像：**代码写错，比 Prompt 约束失效更硬。** 你以为锁死了底线，其实锁死了功能。

**修复**：`validate()` 后**用原对象**，不要接返回值。正确写法见第四节。

---

## 二、🔴 P0 矛盾：R0 说 `<0.85 → INCONCLUSIVE`，N1 表说 `0.60–0.85 → SUSPECT`

两节对 `0.60–0.84` 的含义**直接冲突**：

- R0 伪代码：`if data["status"]=="DIAGNOSED" and confidence < 0.85: 降级为 INCONCLUSIVE`（即 <0.85 一律转人工）。
- N1 三态 UI 表：`0.60 ≤ C < 0.85` 是 **DIAGNOSED_SUSPECT**（黄色、保留诊断、强提醒核实，**不**转人工）；只有 `<0.60` 才 INCONCLUSIVE（转人工、隐藏处置）。

**正确语义应取三态表**（更合理，也符合你"绝不假装高置信"的初衷）：
- `≥0.85` → DIAGNOSED（绿，常规）
- `[0.60, 0.85)` → DIAGNOSED_SUSPECT（黄，保留诊断+警告，不转人工）
- `<0.60` → INCONCLUSIVE（灰，转人工、隐藏主处置）

**修复**：Gatekeeper 必须**把 status 归一化到三态枚举**（由 confidence + 显式 INCONCLUSIVE 推导），UI 直接读 `status`，而不是自己用 confidence 重算。当前伪代码既没产生 SUSPECT，又把 <0.85 误判成 INCONCLUSIVE。修正见第四节。

---

## 三、🟠 P1 缺口

1. **最终输出 §4.2 缺失（权威 schema 漂移）**：N3 的 LLM 输出片段删掉了 `impact_scope` 和 `recommended_actions`，但全文未给出合并后的最终 §4.2。三个版本的输出结构互相不一致。必须有一个"终稿 schema"。
2. **前端写死 `top_contributors[0]`**：`inputData.top_contributors[0].impact_share` 假设 primary 必是第 0 个。若 LLM 定的 `primary_contributor_id` 不是第 0 项，会展示**错维度的占比**。应 `find(c => c.dimension_value === primary_contributor_id)`。
3. **🛡️ 幻觉 ID 检测（强推荐，呼应你的"前端是渲染屏障"）**：`primary_contributor_id` 来自 LLM，必须由 Gatekeeper 对照 `input_data.top_contributors` 校验"是否存在"。返回不存在的 ID = 幻觉 → 降级。这把你"前端屏障"的思想延伸到"ID 也须经 Gatekeeper 对照 input 校验"。
4. **INCONCLUSIVE 时未清空因果链**：按上轮 R3，`causal_chain` 应为 `[]`、`primary_contributor_id` 应置 `null`。当前伪代码未做。
5. **Schema 强制项不全**：应强制 `confidence ∈ [0,1]` 数值、`status` 三态枚举、**输出不得含 `severity`**、`recommended_actions` 不来自 LLM（见下）。
6. **recommended_actions 去向不明**：N3 片段删了它，但 §3.2.D 的"一键处置路由"是核心功能。建议与数字同逻辑——**路由卡片由后端按 `primary_contributor_id` + 模块路由表生成，LLM 不持有任何可操作路径/模块 ID**。
7. **Severity `max` 聚合需文档化**：`max(Rule_Margin, Rule_Loss)` 取两规则较高者（安全方向，过估不低估，合理），但需明确"每行满足 A 或 B 即命中该等级"，并补"两规则均不命中 → LOW"的默认分支。
8. **熔断兜底 vs 真实 INCONCLUSIVE 文案混用**：`confidence: 0.0` 的兜底把"格式错乱"和"真数据不足"都叫 INCONCLUSIVE。建议兜底用独立标识（如 `status: CIRCUIT_BREAK`）或至少在 summary 区分，便于排障。

---

## 四、✅ 修正后的代码（直接替换你那版）

### 4.1 修正 Gatekeeper（修掉 None bug + 三态归一化 + 幻觉 ID 检测 + INCONCLUSIVE 清空）

```python
TESS_STATUSES = {"DIAGNOSED", "DIAGNOSED_SUSPECT", "INCONCLUSIVE"}
CONF_DIAG, CONF_SUSP = 0.85, 0.60

def validate_tess_output(llm_json, input_data):
    # 1) 结构与类型校验：validate 只做校验，成功后用原对象
    try:
        jsonschema.validate(instance=llm_json, schema=TESS_OUTPUT_SCHEMA)
    except jsonschema.ValidationError as e:
        return _circuit_break("Schema 校验失败", str(e))

    data = llm_json  # ← 关键修正：不要接 validate 的返回值

    # 2) 禁售字段：LLM 不得返回 severity
    if "severity" in data:
        return _circuit_break("LLM 越权返回 severity", "物理熔断")

    # 3) confidence 合法性
    c = data.get("confidence")
    if not isinstance(c, (int, float)) or not (0.0 <= c <= 1.0):
        return _circuit_break("confidence 非法", str(c))

    # 4) 三态归一化（尊重 LLM 显式 INCONCLUSIVE，否则按 confidence 推导）
    if data.get("status") == "INCONCLUSIVE":
        data["status"] = "INCONCLUSIVE"
        data["confidence"] = min(c, 0.59)
    elif c >= CONF_DIAG:
        data["status"] = "DIAGNOSED"
    elif c >= CONF_SUSP:
        data["status"] = "DIAGNOSED_SUSPECT"
    else:
        data["status"] = "INCONCLUSIVE"

    # 5) 幻觉 ID 检测：primary_contributor_id 必须存在于 input
    pid = data.get("primary_contributor_id")
    valid_ids = {c["dimension_value"] for c in input_data["top_contributors"]}
    if pid is not None and pid not in valid_ids:
        data["status"] = "DIAGNOSED_SUSPECT"   # 或 INCONCLUSIVE，按产品定
        data["confidence"] = min(c, 0.59)
        data["summary"] = f"[系统警示] 主因维度 {pid} 不在已知列表中，结论存疑，请人工核实。"

    # 6) INCONCLUSIVE 必须清空因果链与 ID
    if data["status"] == "INCONCLUSIVE":
        data["primary_contributor_id"] = None
        data["root_cause_analysis"]["primary_factor"] = "暂无法明确根因"
        data["root_cause_analysis"]["causal_chain"] = []

    return data

def _circuit_break(reason, detail):
    return {
        "status": "INCONCLUSIVE",
        "confidence": 0.0,
        "summary": f"Tess 输出未通过 Gatekeeper 校验（{reason}），已切入人工排查。",
        "root_cause_analysis": {
            "primary_factor": f"系统熔断：{reason}",
            "causal_chain": ["LLM 响应异常", "Gatekeeper 触发熔断", "转人工排查"]
        },
        "primary_contributor_id": None
    }
```

### 4.2 修正前端 lookup（按 ID 查，不写死 [0]）

```tsx
const contrib = inputData.top_contributors
  .find(c => c.dimension_value === llm.primary_contributor_id);

<p>
  核心受影响维度：<b>{llm.primary_contributor_id}</b>
  （贡献率：<mark>{contrib?.impact_share}%</mark>）
</p>
<p>
  预估损失速率：
  <span className="danger-text">
    ${inputData.anomaly_metadata.calculated_loss.loss_per_hour_usd} / 小时
  </span>
</p>
```

### 4.3 合并后的最终 §4.2（LLM 输出，权威终稿草案）

```json
{
  "status": "DIAGNOSED | DIAGNOSED_SUSPECT | INCONCLUSIVE",
  "confidence": 0.85,
  "summary": "定性结论（INCONCLUSIVE 时说明缺什么数据）",
  "primary_contributor_id": "Pub_Media_802",
  "root_cause_analysis": {
    "primary_factor": "定性根因（INCONCLUSIVE: '暂无法明确根因'）",
    "causal_chain": ["定性步骤1", "定性步骤2"]
  }
}
```
> 说明：`impact_scope`、`loss_statement`、`recommended_actions`、`severity` **均不在此**。数字与占比由前端用 `input_data` 渲染；处置路由卡片由后端按 `primary_contributor_id` + 模块路由表生成。LLM 不持有任何数字或可操作路径——这才是真正的"物理死锁"。

---

## 五、N2 Severity 表：基本自洽，1 处需产品拍板

- 矫正示例 `Margin 3.8% / Loss $350` → 命中 HIGH（两个条件都落 HIGH 带）→ **自洽了，R6 关闭** ✅。
- `max(Rule_Margin, Rule_Loss)` 取两规则较高者，安全方向正确（过估不低估）。
- **需拍板**：跨指标组合边界，例如 `Margin 9.9% + Loss $19` → 按表得 HIGH（仅因 Margin 接近阈值）。产品是否接受"接近阈值的毛利即 HIGH"？建议补充"两规则均不命中 → LOW"的默认分支，并明确"每行满足 A 或 B 即命中该等级"。

---

## 六、最终打卡表（修正版）

| 项 | 状态 | 说明 |
|---|---|---|
| **R0 物理保证** | 🟡 修正后成立 | 原伪代码有 None bug + 未归一化三态 + 无 ID 校验；按 §4.1 修正后成立 |
| **N1 置信度** | 🟡 修正后自洽 | 阈值 0.85 保留，但需三态归一化（SUSPECT 不再被误判 INCONCLUSIVE） |
| **N2 Severity** | ✅ 闭合 | 表落定、示例自洽；仅 `max` 边界需产品拍板 |
| **N3 数值安全** | ✅ 闭合 + 强化 | LLM 剥离数字；新增幻觉 ID 检测（§4.1.5）进一步锁死 |

---

## 给评审会的一句话
> 这套分层（Prompt 建议 / Gatekeeper 法典 / 前端屏障）的设计思想已经无懈可击；但**你贴的 Gatekeeper 伪代码有一个会让它自我熔断的 None bug，且 R0 与 N1 对 0.60–0.85 的定义互相打架**。把 §4.1 / §4.2 / §4.3 三处修正替换进去，才是真正"没有任何人能挑出漏洞"的版本。
