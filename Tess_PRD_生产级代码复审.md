# Tess PRD — 生产级代码复审（终验）

> 评审对象：用户给出的"生产级无错版本"——后端 Gatekeeper + 前端 React 安全渲染。
> 先确认**已修复（上一轮 P0 已闭环）**：
> - ✅ `jsonschema.validate` 的 None bug 已修（`data = llm_response_json`）。
> - ✅ 三态归一化与 UI 表自洽：`0.60≤C<0.85 → SUSPECT`、`C≥0.85 → DIAGNOSED`、`C<0.60 → INCONCLUSIVE`；LLM 显式 INCONCLUSIVE 被尊重。
> - ✅ 幻觉 ID 检测（对照 input.top_contributors）。
> - ✅ 前端 `find()` 替代 `[0]` 位错位。
> - ✅ 路由走 `MODULE_ROUTE_MAP`（LLM 不参与 URL 拼接）。
> - ✅ 数字全部由 `input_data` 渲染。
>
> 但"四层绝对密闭"仍有 **1 个 P0（法典自身矛盾）+ 1 个 P1（核心输出未渲染）**，补上才是真密闭。

---

## 一、🔴 P0：Schema（法典）自身与三态输出自相矛盾

**问题**：`TESS_OUTPUT_SCHEMA` 的 `status` 枚举是 `["DIAGNOSED", "INCONCLUSIVE"]`，**漏了 `DIAGNOSED_SUSPECT`**。

```python
"status": {"type": "string", "enum": ["DIAGNOSED", "INCONCLUSIVE"]},  # ← 缺 DIAGNOSED_SUSPECT
```

但函数内部会 `data["status"] = "DIAGNOSED_SUSPECT"`。于是：
- 函数在**入口**用这个 schema 校验 LLM 原始输出（此时 LLM 不会吐 SUSPECT，所以侥幸通过）；
- 函数**出口**把 status 改成 SUSPECT 后返回；
- **任何下游若用同一份 `TESS_OUTPUT_SCHEMA` 再校验 Gatekeeper 的输出（这是"绝对密闭"最自然会做的事），SUSPECT 直接校验失败**。

> 这和你上一版 R0↔N1 的矛盾是同一类病：**"法典"自己都没把三态写全**。评审者只要拿 schema 去 validate 一下你的返回体，立刻破功。

**修复**：把 SUSPECT 加进枚举；并顺手加 `additionalProperties: false` 让"禁售字段"由 schema 第一道锁住（比手动 `if "severity" in data` 更硬）。

```python
TESS_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,   # 法典第一道锁：LLM 多塞任何字段（含 severity）直接 ValidationError → 熔断
    "properties": {
        "status": {"type": "string",
                    "enum": ["DIAGNOSED", "DIAGNOSED_SUSPECT", "INCONCLUSIVE"]},  # ← 补齐三态
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string"},
        "primary_contributor_id": {"type": ["string", "null"]},
        "root_cause_analysis": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "primary_factor": {"type": "string"},
                "causal_chain": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["primary_factor", "causal_chain"]
        }
    },
    "required": ["status", "confidence", "summary", "root_cause_analysis"]
}
```

> 注：`primary_contributor_id` 改为 `["string", "null"]`，以容纳 INCONCLUSIVE 时置 null 的规范（见下 P2-4）。

---

## 二、🟠 P1：前端把 Tess 的核心诊断文本"渲染没了"

**问题**：你这套前端只渲染了 `input_data` 里的**数字**（维度、占比、损失），但**从未渲染 LLM 的 `summary` 和 `causal_chain`**——也就是 Tess 真正的诊断结论和因果链。翻一遍 JSX：

- `matchedContributor.dimension_value / dimension_type / impact_share` ✅ 来自 input
- `calculated_loss.loss_per_hour_usd` ✅ 来自 input
- `summary`、`root_cause_analysis.primary_factor`、`causal_chain` ❌ **完全没被 render**

后果：**用户打开抽屉，只看到一串数字和一个"前往配置"按钮，看不到 Tess 到底诊断出了什么、因果链是什么。** LLM 千辛万苦生成的归因文本被前端丢掉了。这是功能级缺口，不是打磨。

**附带**：`const { status, confidence, primary_contributor_id } = llmOutput;` 里 **`confidence` 也从未被使用**——置信度算出来、用来 gating 了，却不展示给用户（用户无法判断该多信这个结论）。

**修复**：补上诊断叙事渲染块 + 置信度展示。下面这段直接插进 `TessDiagnosticDrawer` 的 return 里即可：

```tsx
{/* 诊断叙事：LLM 定性输出（仅文本，不含任何数字/路由） */}
{!isInconclusive && llmOutput.root_cause_analysis && (
  <div className="diagnosis-narrative">
    <p className="summary">{llmOutput.summary}</p>
    <ol className="causal-chain">
      {llmOutput.root_cause_analysis.causal_chain.map((step, i) => (
        <li key={i}>{step}</li>
      ))}
    </ol>
    <span className="confidence-score">
      置信度 {(confidence * 100).toFixed(0)}%
    </span>
  </div>
)}

{/* INCONCLUSIVE 时也要把系统给的说明文案露出来（含幻觉 ID 的降级提示） */}
{isInconclusive && llmOutput.summary && (
  <p className="inconclusive-note">{llmOutput.summary}</p>
)}
```

> 原组件在 INCONCLUSIVE 时只显示 banner + "呼叫值班运维"按钮，summary 被吞了——上面第二段补回来。

---

## 三、🟡 P2：密闭性打磨项

1. **系统 Severity 标识未渲染**：`input_data.anomaly_metadata.severity`（CRITICAL/HIGH…）是 P0-2 的关键交付，但本组件只渲染了 Tess 的 status，**没渲染规则引擎算出的 Severity 标签**。用户应同时看到"业务严重度（红/橙…）"和"Tess 置信状态（绿/黄/灰）"两层。建议加：
   ```tsx
   {inputData.anomaly_metadata.severity && (
     <span className={`severity-tag ${inputData.anomaly_metadata.severity.toLowerCase()}`}>
       {inputData.anomaly_metadata.severity}
     </span>
   )}
   ```
2. **幻觉 ID 分支未清空 `root_cause_analysis`**：该分支置了 `status=INCONCLUSIVE, confidence=0.0`，但**没把 `primary_contributor_id` 置 null、也没把 `causal_chain` 清空**。与电路熔断兜底分支（它清空了）行为不一致，且返回体仍带幻觉内容。建议在该分支补：
   ```python
   data["primary_contributor_id"] = None
   data["root_cause_analysis"] = {
       "primary_factor": f"系统降级：匹配到不存在的维度 ({returned_id})",
       "causal_chain": ["归因维度不存在", "Gatekeeper 触发降级", "转人工排查"]
   }
   ```
3. **路由 Map 键与 `dimension_type` 大小写耦合**：`MODULE_ROUTE_MAP` 键是 `Publisher/Advertiser/Campaign`，靠 `matchedContributor.dimension_type` 精确匹配。若算法层吐 `publisher`（小写）就 fallback 到 `/overview`。建议路由键与 input 的 `dimension_type` 枚举来自**同一份常量定义**，避免两端漂移。
4. **`returned_id` 拼写**：应为 `returned_id`（returned）。当前块内自洽能跑，但是英文单词拼错，建议改正。
5. **`inputData.anomaly_metadata.calculated_loss` 未做空值守卫**：若算法层没给 `calculated_loss`，`loss_per_hour_usd` 取值会抛错。建议 `inputData.anomaly_metadata?.calculated_loss?.loss_per_hour_usd ?? 0`。

---

## 四、终验打卡表（这版 + 上述修正后）

| 层 | 项 | 状态 |
|---|---|---|
| Schema 法典 | 三态枚举齐全 + `additionalProperties:false` 锁禁售字段 | 🟡 修正后成立（原漏 SUSPECT） |
| 后端 Gatekeeper | None bug 修、三态归一化、幻觉 ID、INCONCLUSIVE 清空 | ✅ 已成立（补 P2-2 更纯） |
| 前端渲染屏障 | 数字/占比/路由走 input+Map | ✅ 已成立 |
| 前端叙事 | summary / causal_chain / confidence / severity 标签 | 🟡 修正后成立（原吞掉诊断文本） |

---

## 给评审会的一句话
> 你这版真正修掉了上一轮两个 P0，架构已经站得住。但"法典"自己漏写了 `DIAGNOSED_SUSPECT` 枚举（拿 schema 一验就破），且前端把 LLM 的诊断文本和置信度全渲染没了（用户只看到数字和按钮）。把 **Schema 补三态 + 加 `additionalProperties:false`**，再**补上那段诊断叙事 JSX**，才是名副其实的四层密闭。
