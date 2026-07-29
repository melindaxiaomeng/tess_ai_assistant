# 📄 Teensing 平台：Tess AI 智能归因与诊断系统 PRD（终稿）

| 文档版本 | 修订日期 | 状态 | 撰写 / 修订 |
| --- | --- | --- | --- |
| **V2.0.0** | 2026-07-28 | 评审通过 / 可交付开发 | Product Team（整合 5 轮评审全部修订） |

> **相较 V1.0.0 的关键修订**（详见文末「修订历史」）：
> 1. 新增 **§1.3 三层死锁设计原则**，并贯穿全文落地。
> 2. 新增 **§1.4 目标用户与场景**，拆分「运营」与「运维」两类角色（闭合 V1 评审 P0-3）。
> 3. **Severity / 损耗金额** 改由算法层计算，作为 Input 传入，LLM 绝不生成（闭合 N2 / N3）。
> 4. 输出 Schema 新增 **`status` 三态（DIAGNOSED / DIAGNOSED_SUSPECT / INCONCLUSIVE）+ `confidence`**（闭合 N1）。
> 5. 新增 **§6 后端校验网关 Gatekeeper** 与 **§7 前端安全渲染**，把「Prompt 是建议、Gatekeeper 才是法典、前端 Template 才是渲染屏障」落到代码（闭合 R0 / P0-1 / P0-2 / 漏枚举 / 前端吞叙事等全部残余）。
> 6. 新增 **§9 边界与交付**（Out of Scope / 依赖 / DoD / 指标），补齐 V1 交付级缺口。

---

## 1. 产品概述与背景

### 1.1 背景

Teensing 平台目前已实现**第一阶段（基础规则预警）**与**第二阶段（异常池 Exception Pool）**。运营与运维人员能够第一时间感知流量异常（如低毛利 <10%、API 拉取为空、Postback 丢失等）。

然而，排查异常仍需人工在多维数据表、日志系统、**流量/变现配置变更记录**之间来回比对，**排查耗时长、技术门槛高**。

### 1.2 产品定位

**Tess** 是嵌入 Teensing 平台的 **AdTech 智能数据分析与归因助理**。Tess 定位为「数据/运维副驾驶」，通过大模型（LLM）+ 归因算法，自动将复杂的数据波动和日志特征转化为自然语言的**因果链条**与**可执行的处置建议**。

Tess 在链路中补的是「感知（一/二阶段已具备）→ **归因 → 建议 → 处置路由**」的后三段；它**不替代**规则预警与异常池，而是架在其上的诊断层。

### 1.3 设计原则：三层死锁（核心约束）

> 本 PRD 一切设计围绕一条铁律：**「Prompt 是建议，Gatekeeper 才是法典，前端 Template 才是渲染屏障。」**

| 层 | 职责 | 死锁什么 |
| --- | --- | --- |
| **Prompt（建议层）** | 引导 LLM 做定性归因、写自然语言因果链 | 只负责「语言理解与归因拆解」，**不**持有任何数值、状态、路由决定权 |
| **后端 Gatekeeper（法典层）** | 对 LLM 输出做 Schema 校验 + 业务不变量校验 + 幻觉 ID 校验，违规即熔断降级 | 死锁 **状态（三态）、置信度边界、严重度、归因 ID 真实性** |
| **前端 Template（渲染屏障层）** | 数字 / 占比 / 损失 / 路由全部由 `input_data` 与系统路由表强渲染 | LLM **绝不触碰任何数字与 URL**，只贡献定性文本 |

**禁止事项（不可逾越）**：
- LLM 不得生成或修改任何货币金额、百分比、Severity 等级（一律由算法层算好作为输入）。
- LLM 不得自行决定 `status` 的语义边界（仅可显式 `INCONCLUSIVE` 认输，其余由 Gatekeeper 按 `confidence` 归一化）。
- LLM 不得拼接任何跳转 URL（路由由前端 + `MODULE_ROUTE_MAP` 强渲染）。

### 1.4 目标用户与场景（闭合 V1 P0-3）

V1 原稿将「运营」与「运维」混为一谈，二者技术能力、关心的异常、期望输出均不同，必须拆分：

| 角色 | 技术能力 | 高频异常场景 | 对 Tess 的期望输出 |
| --- | --- | --- | --- |
| **运营（Business Ops）** | 低代码 / 配置层 | 毛利暴跌、某 Publisher 贡献异常、广告主收益缺失 | 用**业务语言**说清「哪个渠道/广告主、损失多少、该点哪**配置**处置」 |
| **运维（SRE / Data Eng）** | 高，能查日志/链路 | API 504、Postback 丢失、数据管道延迟 | 给出**技术因果链**（变更→超时→丢失）、指向具体**日志/管道**排查入口 |

> Tess 的 `summary` / `causal_chain` 需同时兼容两种口吻（见 §5 Prompt 约束）；UI 的「一键处置」目标模块按 `dimension_type` 路由到对应角色的处置页（§7）。

---

## 2. 核心功能与架构设计

### 2.1 整体处理链路

```
[1. 触发层]            [2. 算法特征提取层]              [3. Tess Agent 推理]        [4. 后端 Gatekeeper]      [5. 前端渲染]
异常池点击 / 定时扫描 ─▶ 算指标变动 + TopN 维度贡献 + 关联日志  ─▶ LLM 输出定性归因 JSON ─▶ 校验·归一·熔断降级 ─▶ 数字走 input，叙事走 AI
                         （并算 Severity / 精确损耗，注入 Input）            （法典层，§6）             （屏障层，§7）
```

1. **触发层**：定时任务扫描或用户在异常池点击「Tess 诊断」。
2. **算法层（特征提取 + 规则引擎）**：计算指标变动幅度、维度贡献度（Top N Factors）、提取相关日志；**同时按业务阈值算出 `severity` 与 `calculated_loss`，随 Input 注入**。
3. **AI 推理层（Tess）**：基于强约束 Prompt 结合 Input 上下文，输出**仅含定性内容**的 JSON 归因报告（数值一律不写）。
4. **Gatekeeper 层（§6）**：对 LLM 输出做 Schema + 不变量 + 幻觉 ID 校验，归一化为三态，违规熔断降级为 `INCONCLUSIVE`。
5. **展示与动作闭环（UI，§7）**：前端渲染 Tess 诊断卡片，**数字/路由取自 input + 路由表**，叙事取自 LLM；并提供「一键跳转处置」或「转人工」。

### 2.2 算法层失败的兜底（闭合 V1 P1-7）

若算法特征提取层本身失败（无 `top_contributors` / 无法算 `loss`），则：
- 不调用 LLM，直接由后端返回 `INCONCLUSIVE` 结构，前端展示「数据源异常，请人工核查特征提取任务」；
- 前端对 `top_contributors` / `calculated_loss` 已做空值守卫（§7），不会因缺字段崩溃。

---

## 3. 详细功能需求（FRD）

### 3.1 Tess 诊断入口（Front-end Entry）

* **入口一：异常池（Exception Pool）列表页**
  在每条异常记录右侧操作栏添加组件：`<Button> Tess 诊断 </Button>`。
* **入口二：全局预警通知（飞书 / 钉钉 / 邮件）**
  预警卡片底部附带「查看 Tess 诊断」跳转链接，直接打开系统并调出 Tess 抽屉。
* **入口三：数据分析大盘（Analytics Dashboard）**
  图表 / 透视表监测到指标剧烈波动时，卡片右上角显示 Tess 智能分析浮标。

> 三个入口共用同一抽屉组件与同一套渲染 / 降级逻辑（§7），保证一致体验。

### 3.2 Tess 归因诊断抽屉（Tess Drawer Component）

点击「Tess 诊断」时，右侧划出抽屉组件（Width: 560px），展示以下区域：

#### A. 顶部 Header 模块
* **标题**：`Tess 智能归因诊断`
* **Severity Tag**：由算法层算出的严重度（红 `CRITICAL` / 橙 `HIGH` / 黄 `MEDIUM` / 蓝 `LOW`，灰 `UNKNOWN`）。
* **置信度**：渲染 Tess 的 `confidence`（百分比）。
* **诊断事件元信息**：事件 ID、触发时间、监控指标（如：毛利率降至 3.8%）。

#### B. 加载与空状态（Micro-copy）
* **Loading**：`"Tess 正在交叉比对过去 1 小时的流量日志与 API 回调数据..."`
* **Empty / Health**：`"Tess 已经检查过了，当前所有渠道与广告主毛利率均在健康区间，继续保持！"`

#### C. 核心诊断内容区（Diagnostic Section）
1. **一句话结论（Summary）**：渲染 LLM 的 `summary`，高亮展示（**数字走算法，叙事走 AI**）。
2. **因果链条（Causal Chain）**：渲染 LLM 的 `causal_chain`（时间轴 / 步骤条）。**当 `status === INCONCLUSIVE` 时不渲染（由 Gatekeeper 清空）**。
3. **影响范围与风险评估（Impact Scope）**：
   * 展示 Top N 影响维度（如 `Pub_Media_802`），**贡献率 `82%` 由 input 渲染，LLM 不持有**。
   * 预估潜在损失（如 `$350.00 / 小时），**由 input 的 `calculated_loss` 渲染**。

#### D. 状态 Banner（三态视觉态，闭合 N1）

| 状态 (Status) | 置信度区间 (Confidence) | UI 视觉态 | 含义 & 动作 |
| --- | --- | --- | --- |
| **DIAGNOSED** | **≥ 0.85** | 🟢 绿色 / 高置信 | 证据链完整（有日志 + 维度对齐），常规诊断抽屉，提供一键处置。 |
| **DIAGNOSED_SUSPECT** | **0.60 ≤ C < 0.85** | 🟡 黄色 / 中置信（警惕） | 维度高度集中但缺乏直接技术报错佐证，强提醒用户核实。 |
| **INCONCLUSIVE** | **< 0.60 或 熔断** | 🔴 灰色 / 低置信降级 | 无法确定根因，关闭主处置按钮，切换为「一键转值班运维 / 飞书群」。 |

#### E. 建议动作与操作闭环（Actionable Next Steps）
* **处置建议卡片**：列出 P0 / P1 / P2 优先级的操作步骤（**首版锁「建议 + 人工确认」，自动执行列为后续**，闭合 V1 P0-5）。
* **直接跳转（One-click Routing）**：路由由前端按 `dimension_type` + `MODULE_ROUTE_MAP` 拼接（**LLM 不拼 URL**），指向对应功能页（如 Publisher 映射配置）。

#### F. 反馈与交互（Feedback）
* 抽屉底部提供赞 / 踩按钮：`"Tess 的分析对你有帮助吗？ [👍 准确] [👎 偏差]"`，反馈数据记录至日志库用于后续 Prompt 优化。

---

## 4. 技术与数据接口规范

### 4.1 输入数据结构（Backend / 规则引擎 ➔ Tess LLM）

由后端统计引擎与**规则引擎**准备并投喂给 Tess 的 Context 标准 JSON。注意：`severity` 与 `calculated_loss` 已由算法层算好，LLM 仅消费、不生成。

```json
{
  "anomaly_metadata": {
    "event_id": "ERR-20260728-0912",
    "trigger_time": "2026-07-28 14:00:00",
    "target_metric": "Overall Margin",
    "current_value": "3.8%",
    "benchmark_value": "14.2%",
    "severity": "HIGH",
    "calculated_loss": {
      "loss_per_hour_usd": 350.00,
      "calculation_basis": "基于过去 30 分钟 Cost 速率与缺失 Revenue 的差值计算"
    }
  },
  "top_contributors": [
    {
      "dimension_type": "Publisher",
      "dimension_value": "Pub_Media_802",
      "impact_share": "82%",
      "metric_change": "Margin 从 15.1% 降至 -2.4%"
    }
  ],
  "associated_signals": [
    {
      "source": "AppsFlyer_Pull_API",
      "status": "WARNING",
      "detail": "13:30-14:00 期间 Postback 接口 HTTP 504 (Gateway Timeout) 占比 45%"
    }
  ]
}
```

> **数据一致性自检（闭合 V1 R6）**：上例 `Margin 3.8% / Loss $350` 命中 Severity 规则表 `0% ≤ Margin < 10%` 且 `$100 ≤ Loss < $500` → 判定 **`HIGH`**（不再误标 CRITICAL）。

### 4.2 输出数据结构（Tess LLM ➔ Gatekeeper）

约束 Tess 必须返回的 JSON Schema（**仅定性内容 + 一个待校验的 ID**）。本 Schema 与 §5 Prompt、§6 Gatekeeper **三方绑定，禁止漂移**（闭合 V1 P1-6）。

```json
{
  "status": "DIAGNOSED | DIAGNOSED_SUSPECT | INCONCLUSIVE",
  "confidence": 0.85,
  "summary": "一句话诊断结论（定性，禁止写任何数字/百分比/金额）",
  "primary_contributor_id": "Pub_Media_802",
  "root_cause_analysis": {
    "primary_factor": "引发异常的最核心原因（定性）",
    "causal_chain": [
      "步骤 1：运营变更配置",
      "步骤 2：API 超时",
      "步骤 3：转化数据缺失 → 毛利暴跌"
    ]
  }
}
```

> 若 LLM 确实无法确定主因维度，`primary_contributor_id` 可返回 `null`；`causal_chain` 在 `INCONCLUSIVE` 时由 Gatekeeper 强制清空。

### 4.3 Severity 规则引擎表（算法层，闭合 N2）

Severity 由后端规则引擎按硬性业务阈值计算，不再交由 LLM。规则为各指标等级取**最严重（max）**：

$$\text{Severity} = \max(\text{Rule}_{\text{Margin}}, \text{Rule}_{\text{Loss}})$$

| 严重等级 (Severity) | 条件 A：毛利率 (Margin) | 条件 B：预估每小时损失 (Loss/h) | 前端 UI 标识 |
| --- | --- | --- | --- |
| **CRITICAL** | $\text{Margin} < 0\%$ (倒贴) | $\text{Loss} \ge \$500$ | 红色高亮 + 紧急推送 |
| **HIGH** | $0\% \le \text{Margin} < 10\%$ | $\$100 \le \text{Loss} < \$500$ | 橙色高亮 + 正常推送 |
| **MEDIUM** | $10\% \le \text{Margin} < 15\%$ | $\$20 \le \text{Loss} < \$100$ | 黄色高亮 |
| **LOW** | 指标小幅波动 | $\text{Loss} < \$20$ | 蓝色提示 |

> ⚠️ **产品待拍板（V1 R5 遗留）**：当 Margin 命中 HIGH（如 5%）但 Loss 仅 $10（LOW）时，`max` 取 HIGH。若业务希望「损失金额优先」或「双低才算低」，需在此明确聚合策略，Dev 据此写死。

---

## 5. System Prompt 规范（Tess 人设与逻辑强约束）

> **本 Prompt 必须输出符合 §4.2 Schema 的 JSON，不得包含任何闲聊或 Markdown 之外的前导文字。Prompt 与 §4.2 为强绑定关系。**

```text
你叫 Tess，是 Teensing 平台的专属 AdTech 智能数据分析与风控专家。你的任务是根据传入的结构化业务数据、异动指标和关联日志，进行严谨的根因分析（Root Cause Analysis），并输出简洁、准确、可执行的归因报告。

### 🚨 绝对红线规则（违反将导致系统失效）：
1. 严禁幻觉与猜测：如果传入的 associated_signals 或日志不足以推导出唯一的逻辑因果链，必须将 status 设为 "INCONCLUSIVE"，并将 confidence 设为低于 0.6。在 summary 中明确指出："数据信号不足，无法推导明确根因，建议人工介入"。
2. 数值严禁篡改：损耗金额、百分比、贡献率等财务/统计数值全部已由系统 input 提供，你严禁自行计算、捏造或修改任何货币与百分比数字；summary / causal_chain 只写定性描述。
3. 严禁修改 Severity：异常严重程度由系统规则引擎判定并已在 input 中给出，你只需在建议动作中匹配该严重程度的处置紧迫感，不得输出 severity 字段。
4. 输出约束：必须且只能返回符合指定 JSON Schema（见 §4.2）的标准 JSON，不得包含任何 Markdown 标记之外的闲聊或前导文字；不得返回 schema 未定义的字段（如 severity）。

### 归因置信度 (Confidence) 评分指南：
- 0.85 ~ 1.00：存在明确的操作日志或 API 报错日志，且与指标下滑时间点完全吻合。
- 0.60 ~ 0.84：维度贡献集中（Top 1 > 80%），但缺乏直接的技术报错日志佐证。
- < 0.60：维度分散、缺乏关联日志、或日志信息相互冲突 ➔ 强制标记状态为 "INCONCLUSIVE"。

### 语言风格：
专业、精炼、客观，同时兼容「运营（业务语言）」与「运维（技术因果链）」两种读者的理解习惯，适应 B 端 SaaS / 网盟管理后台场景。
```

---

## 6. 后端校验网关 Gatekeeper（法典层，闭合 R0 / P0-1 / P0-2 / 漏枚举 / 幻觉 ID / 最后 P1）

### 6.1 设计原则
LLM 返回 JSON 后、推给前端之前，**后端必须经过一层 Gatekeeper 纯代码校验**。任何违规 → **物理熔断，强制覆盖为 `INCONCLUSIVE`**，绝不把未锁死的内容交给前端。

### 6.2 法典 Schema（`additionalProperties: false` 物理锁死）

```python
import jsonschema

# 🛡️ 正式法典 Schema：开启 additionalProperties: false 物理硬锁
TESS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["DIAGNOSED", "DIAGNOSED_SUSPECT", "INCONCLUSIVE"]
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string"},
        "primary_contributor_id": {"type": ["string", "null"]},  # 允许 null（无法确定主因维度）
        "root_cause_analysis": {
            "type": "object",
            "properties": {
                "primary_factor": {"type": "string"},
                "causal_chain": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["primary_factor", "causal_chain"],
            "additionalProperties": False
        }
    },
    "required": ["status", "confidence", "summary", "root_cause_analysis"],
    "additionalProperties": False  # 杜绝 LLM 返回任何未经授权的字段（如 severity）
}
```

### 6.3 校验代码（生产级最终版，含全部 5 轮修复）

```python
import jsonschema

def validate_tess_output(llm_response_json: dict, input_data: dict) -> dict:
    """
    Tess 后端死锁校验网关 (Gatekeeper)
    修复清单：
      - P0-1: jsonschema.validate 成功返回 None，故用原对象 data = dict(llm_response_json)（同时浅拷贝避免副作用）
      - P0-2: 三态归一与 UI 表自洽
      - 漏枚举: TESS_OUTPUT_SCHEMA 已含 DIAGNOSED_SUSPECT + additionalProperties:false 锁死 severity
      - 幻觉 ID: primary_contributor_id 必须存在于 input.top_contributors
      - 最后 P1: INCONCLUSIVE 三条路径（幻觉/熔断/早返回）一律清空 root_cause_analysis，避免"无法确定"仍展示推测链
    """
    try:
        # 1. 结构与严格类型 Schema 校验（若 LLM 吐了 severity 等非法字段，直接抛异常触发熔断）
        jsonschema.validate(instance=llm_response_json, schema=TESS_OUTPUT_SCHEMA)
        data = dict(llm_response_json)  # 浅拷贝，避免 mutate 调用方原对象

        # 2. LLM 主动认输 / 置信度极低 → 降级（与另两分支一致：清空因果链）
        if data["status"] == "INCONCLUSIVE" or data["confidence"] < 0.60:
            data["status"] = "INCONCLUSIVE"
            data["confidence"] = min(data["confidence"], 0.59)
            data["root_cause_analysis"] = {
                "primary_factor": "暂无法明确根因",
                "causal_chain": []
            }
            return data

        # 3. 幻觉 ID 校验（ID 层屏障）：LLM 返回的 ID 必须存在于算法候选集
        valid_ids = {c["dimension_value"] for c in input_data.get("top_contributors", [])}
        returned_id = data.get("primary_contributor_id")
        if returned_id and returned_id not in valid_ids:
            return {
                "status": "INCONCLUSIVE",
                "confidence": 0.0,
                "summary": f"[系统降级] Tess 归因匹配了不存在的维度 ({returned_id})，已转人工",
                "root_cause_analysis": {
                    "primary_factor": "维度匹配失败：存在幻觉 ID",
                    "causal_chain": ["LLM 返还未知维度 ID", "Gatekeeper 拦截降级", "转人工排查"]
                }
            }

        # 4. 状态与置信度归一化（统一推导为三态枚举，与 §3.2 表自洽）
        if 0.60 <= data["confidence"] < 0.85:
            data["status"] = "DIAGNOSED_SUSPECT"  # 中置信：保留诊断，前端展示警惕态
        else:
            data["status"] = "DIAGNOSED"          # 高置信 (>= 0.85)

        return data

    except Exception:
        # 🛡️ 极端异常兜底熔断（与 hyster 分支一致：清空因果链）
        return {
            "status": "INCONCLUSIVE",
            "confidence": 0.0,
            "summary": "Tess 输出未通过后端 Gatekeeper 安全校验，已自动切入人工排查。",
            "root_cause_analysis": {
                "primary_factor": "系统熔断：LLM 输出逻辑违规",
                "causal_chain": ["响应校验失败", "Gatekeeper 触发熔断", "转人工处理"]
            }
        }
```

---

## 7. 前端安全渲染（渲染屏障层，闭合 P0-2 前端吞叙事 / [0] 错位 / 路由耦合 / 空值守卫）

### 7.1 渲染原则
* **数字 / 占比 / 损失 / 路由** 一律取自 `input_data` 与 `MODULE_ROUTE_MAP`，LLM 绝不触碰。
* **叙事 / 置信度 / Severity 标签** 由 LLM 输出 + 算法输入渲染。
* **INCONCLUSIVE** 时：不渲染 `causal_chain`（Gatekeeper 已清空为 `[]`），主按钮切换为「转人工」。

### 7.2 React 组件（生产级最终版）

```tsx
import React from 'react';

// 路由安全映射表（大小写不敏感匹配，杜绝大小写耦合）
const MODULE_ROUTE_MAP: Record<string, string> = {
  publisher: '/publisher/mapping-list?id=',
  advertiser: '/advertiser/detail?id=',
  campaign: '/campaign/overview?id=',
};

export const TessDiagnosticDrawer = ({ llmOutput, inputData }) => {
  const { status, confidence, summary, primary_contributor_id, root_cause_analysis } = llmOutput;
  const { anomaly_metadata = {}, top_contributors = [] } = inputData || {};

  // 算法层确定的 Severity & 精确损耗空值守卫
  const severity = anomaly_metadata.severity || 'UNKNOWN';
  const lossPerHour = Number(anomaly_metadata.calculated_loss?.loss_per_hour_usd) || 0;

  // 根据 LLM 决定的 ID 动态匹配 Input 中的真实统计对象（修复写死 [0] 错位 bug）
  const matchedContributor = top_contributors.find(
    (c) => c.dimension_value === primary_contributor_id
  ) || top_contributors[0];

  const isSuspect = status === 'DIAGNOSED_SUSPECT';
  const isInconclusive = status === 'INCONCLUSIVE';

  // 大小写不敏感的路由获取
  const dimTypeKey = (matchedContributor?.dimension_type || '').toLowerCase();
  const routeBase = MODULE_ROUTE_MAP[dimTypeKey] || '/overview?id=';

  return (
    <div className="tess-drawer-content p-4 space-y-4">
      {/* 1. Header 标签区：Severity 业务标签 + Tess 置信度数字 */}
      <div className="flex items-center justify-between border-b pb-3">
        <div className="flex items-center gap-2">
          <span className={`px-2 py-1 text-xs font-bold rounded severity-badge ${severity.toLowerCase()}`}>
            [{severity}]
          </span>
          <h3 className="font-semibold text-lg">Tess 智能归因诊断</h3>
        </div>
        <div className="text-sm text-gray-500">
          置信度: <span className="font-mono font-bold">{(confidence * 100).toFixed(0)}%</span>
        </div>
      </div>

      {/* 2. 状态 Banner（三态视觉态） */}
      {isSuspect && (
        <div className="p-3 bg-yellow-50 border-l-4 border-yellow-400 text-yellow-800 text-sm">
          🟡 <b>中置信度诊断：</b> 维度数据高度集中，但缺乏第三方 API 报错日志直接佐证，请谨慎核实。
        </div>
      )}
      {isInconclusive && (
        <div className="p-3 bg-gray-100 border-l-4 border-gray-500 text-gray-700 text-sm">
          🔴 <b>归因无法确定：</b> 当前日志信号不足或触发了校验熔断，已自动切换为人工排查流。
        </div>
      )}

      {/* 3. Tess 核心诊断叙事区（LLM 的 summary + causal_chain） */}
      <div className="bg-slate-50 p-4 rounded-lg space-y-3">
        <h4 className="text-sm font-bold text-slate-700">📌 诊断结论</h4>
        <p className="text-slate-900 font-medium text-sm leading-relaxed">{summary}</p>

        {root_cause_analysis?.causal_chain?.length > 0 && (
          <div className="pt-2">
            <h5 className="text-xs font-semibold text-slate-500 mb-2">推导因果链条 (Causal Chain):</h5>
            <ol className="list-decimal list-inside space-y-1 text-xs text-slate-700">
              {root_cause_analysis.causal_chain.map((step, idx) => (
                <li key={idx} className="pl-1">{step}</li>
              ))}
            </ol>
          </div>
        )}
      </div>

      {/* 4. 物理数据卡片区：所有数值纯粹读取 inputData，LLM 绝不触碰数字 */}
      {!isInconclusive && matchedContributor && (
        <div className="border p-4 rounded-lg bg-white space-y-2 text-sm">
          <div className="flex justify-between">
            <span className="text-gray-500">核心受影响维度:</span>
            <span className="font-semibold">{matchedContributor.dimension_value} ({matchedContributor.dimension_type})</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">该维度异常贡献率:</span>
            <span className="font-semibold text-amber-600">{matchedContributor.impact_share}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">预估损失速率:</span>
            <span className="font-semibold text-red-600">${lossPerHour.toFixed(2)} / 小时</span>
          </div>
        </div>
      )}

      {/* 5. 动作触发区（路由由系统拼接，LLM 不拼 URL） */}
      <div className="pt-2">
        {!isInconclusive && matchedContributor ? (
          <a
            href={`${routeBase}${matchedContributor.dimension_value}`}
            className="block w-full text-center bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 rounded-md transition-colors"
          >
            前往 {matchedContributor.dimension_type} 配置页面一键处置
          </a>
        ) : (
          <button
            className="w-full bg-slate-800 hover:bg-slate-900 text-white font-medium py-2 rounded-md transition-colors"
            onClick={() => alert('已发起即时通讯呼叫，通知值班运维！')}
          >
            一键呼叫值班运维 / 派发飞书排查群
          </button>
        )}
      </div>
    </div>
  );
};
```

> **P2 收尾项**：`severity-badge unknown` 需补样式（或默认走 LOW）；SUSPECT banner 文案已中性化；`lossPerHour` 已加 `Number(...)||0` 兜底。

---

## 8. 非功能性需求（NFR）

1. **响应性能（Performance）**
   * Tess 诊断抽屉从打开到首屏内容渲染（流式或 JSON 解析），响应控制在 **3 秒以内**。
   * LLM API 调用 `temperature: 0.1~0.2`，保证输出格式确定性。
2. **容错机制（Fallback）**
   * **LLM 层失败**：Gatekeeper 熔断 → 返回 `INCONCLUSIVE` → 前端转人工。
   * **算法层失败**（无 `top_contributors` / 无法算 `loss`）→ 不调 LLM，直接 `INCONCLUSIVE`，前端提示核查特征提取任务（闭合 V1 P1-7）。
   * **LLM 服务超时 / 解析失败** → 前端兜底展示算法层原始 Top N 波动列表，不影响基本运维排查。
3. **数据安全与隐私（Security）**
   * 投喂 LLM 的数据必须抹去敏感商业隐私（敏感 API Key、加密密钥等），仅透出 ID 标识与统计数值。
   * LLM 输出经 Gatekeeper `additionalProperties: false` 锁死，越权字段（如 `severity`）直接熔断。

---

## 9. 边界与交付（Out of Scope / 依赖 / DoD / 指标）

### 9.1 Out of Scope（首版不做）
* 自动执行处置动作（仅提供「建议 + 人工确认 + 一键跳转」）。
* 跨异常事件的聚合根因分析 / 趋势预测。
* 多语言（首版仅中文）。

### 9.2 依赖
* 后端统计引擎（特征提取 + TopN 维度贡献计算）。
* 规则引擎（Severity + `calculated_loss` 计算）。
* LLM 服务（需支持结构化 JSON 输出，建议 `temperature 0.1~0.2`）。
* 即时通讯网关（飞书 / 钉钉，用于 INCONCLUSIVE 转人工）。

### 9.3 Definition of Done（验收标准）
* [ ] Gatekeeper 单测覆盖：三态归一、幻觉 ID、severity 越权、INCONCLUSIVE 三路径一致清空 `root_cause`、None 修复回归。
* [ ] 前端单测：INCONCLUSIVE 不渲染 `causal_chain`、数字全取自 input、`[0]` 错位防护。
* [ ] 端到端：注入 V1 R6 示例（Margin 3.8% / Loss $350）→ 算法判 `HIGH`、卡片展示 `$350.00 / 小时`、Severity 标签 `HIGH`。

### 9.4 北极星指标（验收基线，闭合 V1 高优风险）
* **排查耗时**：基线（人工）= X 分钟/异常 → 目标（Tess 辅助）≤ Y 分钟。
* **归因准确率 / 误漏报率**：以人工复核为 ground truth 抽样评估。
* **INCONCLUSIVE 准确率**：低置信转人工的案例中，确实「无法确定」的比例（避免误杀）。
* **覆盖率**：异常池命中 Tess 诊断的比例。

---

## 修订历史（V1.0.0 ➔ V2.0.0，5 轮评审闭环）

| 轮次 | 关键问题 | 终稿处置 |
| --- | --- | --- |
| R1 | Tess 补哪几环不清 / 与一、二阶段关系未定义 / 运营运维混用 / 缺置信度 / 可执行程度模糊 | §1.2 补链路定位、§1.4 拆角色、§4.2 加 confidence+INCONCLUSIVE、§3.2 锁「建议+人工确认」 |
| R2 | N1 缺置信度分支 / N2 severity 交 LLM / N3 损耗 LLM 编 | N1→§4.2+§3.2 三态；N2→§4.1+§4.3 算法算；N3→§4.1 算法算、前端渲染 |
| R3 | Prompt 红线≠系统保证 / 中置信盲区 / 数值仍经 LLM 文本 / severity 未落表 | → R0 Gatekeeper（§6）；§3.2 警戒线 <0.85；§7 剥离数字；§4.3 落表 |
| R4 | Gatekeeper `jsonschema` 返回 None 反向熔断 / 三态与 UI 矛盾 | §6.3 `data = dict(...)`；三态归一自洽 |
| R5 | Schema 漏 `DIAGNOSED_SUSPECT` / 前端吞诊断叙事 / 幻觉 ID 未清 root_cause / INCONCLUSIVE 三路径不一致 | §6.2 补枚举+`additionalProperties:false`；§7.2 补全叙事；§6.3 三路径一致清空 `root_cause` |
