# Tess PRD V1.0.0 — 终审闭环（第 5 轮）

> 评审对象：用户给出的"代码级完全补全"最终版（Schema 三态 + `additionalProperties: false` + Gatekeeper + 前端全渲染）。
> 总体判断：**此前所有 P0 / P1 / P2 均已真正补齐，架构已站立。** 仅余 **1 个 P1 一致性裂缝**（INCONCLUSIVE 三分支对 `root_cause` 的处理不一致，导致"无法确定"仍可能渲染出因果链），补上这一处即名副其实的四层密闭。

---

## 一、已确认闭环（此版做对了）

| 之前的问题 | 此版状态 | 说明 |
|---|---|---|
| Schema 枚举漏 `DIAGNOSED_SUSPECT` | ✅ 已补 | `enum` 现含三态；下游用同 schema 验返回体不再崩 |
| Schema 未锁禁售字段 | ✅ 已补 | `additionalProperties: false` → LLM 敢塞 `severity` 直接 ValidationError → 熔断。比手写 `if "severity" in data` 更硬 |
| `jsonschema.validate` 返回 None 的灾难 bug | ✅ 已修 | `data = llm_response_json` |
| 三态归一化与 UI 表打架 | ✅ 已自洽 | `≥0.85→DIAGNOSED`、`[0.60,0.85)→SUSPECT`、`<0.60→INCONCLUSIVE` |
| 幻觉 ID 检测 | ✅ 已补且一致 | 对照 `input.top_contributors`；分支清空 `root_cause` |
| 前端吞掉诊断叙事 / 置信度 / Severity | ✅ 已补 | 叙事块渲染 `summary`+`causal_chain`；Header 渲染置信度% 与 `[severity]` 标签 |
| 路由大小写耦合 | ✅ 已解耦 | `dimension_type.toLowerCase()` + 小写 Map 键 |
| `calculated_loss` 空值守卫 | ✅ 已补 | `?.loss_per_hour_usd ?? 0` |
| `returned_id` 拼写 | ✅ 已正 | 现为 `returned_id` |

> 这一版真正实现了你说的"数字走算法，叙事走 AI"：所有数字/占比/损失/路由来自 `input_data` 与 `MODULE_ROUTE_MAP`，LLM 只产出定性 `summary` + `causal_chain` + 一个需经 Gatekeeper 校验的 `primary_contributor_id`。

---

## 二、🔴 P1：INCONCLUSIVE 三分支对 `root_cause` 处理不一致

**现象**：后端有三个会产生 `INCONCLUSIVE` 的路径，但只有两个清空了 `root_cause_analysis`：

| INCONCLUSIVE 触发路径 | 是否清空 root_cause | 结果 |
|---|---|---|
| ① 幻觉 ID（`returned_id not in valid_ids`） | ✅ 清空为系统消息 | 一致 |
| ② 极端异常（`except` 兜底熔断） | ✅ 清空为系统消息 | 一致 |
| ③ **LLM 主动认输 / `confidence < 0.60` 早返回** | ❌ **不清空，原样返回 LLM 的 causal_chain** | **不一致** |

③ 的代码：
```python
if data["status"] == "INCONCLUSIVE" or data["confidence"] < 0.60:
    data["status"] = "INCONCLUSIVE"
    data["confidence"] = min(data["confidence"], 0.59)
    return data   # ← root_cause_analysis 仍是 LLM 原内容，未清空
```

**后果**：前端叙事块对 `causal_chain` 的渲染是**无条件**的（`{root_cause_analysis?.causal_chain?.length > 0 && ...}`）。于是路径③下，UI 顶部灰色 banner 写着"归因无法确定，已转人工"，**下方却仍展示 LLM 推测出的因果链条**——banner 与内容自相矛盾，且把"未能确定"时 LLM 的猜测当成了可展示结论。这恰好违反你"密闭"的初衷。

**修复（后端死锁，与 ①② 对齐）**：
```python
if data["status"] == "INCONCLUSIVE" or data["confidence"] < 0.60:
    data["status"] = "INCONCLUSIVE"
    data["confidence"] = min(data["confidence"], 0.59)
    # 与 ①② 分支保持一致：清空因果链，避免"无法确定"却展示推测链条
    data["root_cause_analysis"] = {
        "primary_factor": "暂无法明确根因",
        "causal_chain": []
    }
    return data
```
> 前端已有 `length > 0` 守卫，清空为 `[]` 后自动不渲染链条，无需改前端。也可作为防御纵深，在前端把因果链渲染再加 `!isInconclusive` 守卫——但后端清空是正解。

---

## 三、🟡 P2：可选的精炼项（不影响密闭，建议收尾）

1. **`primary_contributor_id` 类型收紧**：当前 schema 为 `"type": "string"`。若 LLM 合法地返回 `null`（确实无法确定主因维度），会因类型不符触发**整个响应熔断**。建议改为 `"type": ["string", "null"]`，让 null 优雅通过；前端 `matchedContributor` 已有 `|| top_contributors[0]` 兜底。
2. **`data = llm_response_json` 原地改写**：Gatekeeper 直接 mutate 了调用方的原 dict。无害，但建议 `data = dict(llm_response_json)` 做浅拷贝，避免副作用。
3. **SUSPECT banner 文案硬编码**：写死"缺少第三方 API 报错日志直接佐证"。若低置信是因"日志相互冲突"而非"缺失"，文案不精准。可改为更中性的"维度高度集中但缺乏直接技术佐证，请核实"。
4. **`severity` 缺省类**：`anomaly_metadata.severity || 'UNKNOWN'` 生成的 `severity-badge unknown` 无样式。建议补一个 UNKNOWN 样式或默认走 LOW。
5. **`calculated_loss.loss_per_hour_usd` 假设为 number**：前端直接 `.toFixed(2)`。依赖算法层 schema 保证其为数值；若要更稳可在前端做 `Number(...)||0` 兜底。

---

## 四、终审打卡表（补上 P1 后）

| 层 | 机制 | 状态 |
|---|---|---|
| 法典 Schema | 三态枚举 + `additionalProperties: false` 锁死 `severity` | ✅ 成立 |
| 后端 Gatekeeper | None 修复 / 三态归一 / 幻觉 ID / **INCONCLUSIVE 三分支一致清空** | ✅ 成立（补 P1 后） |
| 数据计算权 | `severity` 与 `loss` 由规则引擎算，不交 LLM | ✅ 成立 |
| 前端屏障与渲染 | 数字/路由走 input+Map；叙事/置信度/Severity 全渲染；**INCONCLUSIVE 不展示推测链** | ✅ 成立（补 P1 后） |

---

## 给评审会的一句话
> 这一版把前四轮的问题全部真闭环了，Schema 用 `additionalProperties: false` 把 `severity` 在协议层物理锁死，前端也把诊断叙事补全。唯一剩下的裂缝是：INCONCLUSIVE 的"LLM 认输/低置信"分支没像另两个分支那样清空 `root_cause`，导致"无法确定"的卡片仍能渲染出推测因果链。在早返回分支补三行清空代码，就是真正四层密闭、没有人能挑出漏洞的版本。
