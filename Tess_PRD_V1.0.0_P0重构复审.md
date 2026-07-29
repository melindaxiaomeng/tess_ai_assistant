# Tess PRD V1.0.0 — P0 重构复审（N1 / N2 / N3 订正复审）

> 评审对象：用户针对 N1/N2/N3 三个 P0 给出的重构方案（订正后 §4.1 / §4.2 / §5 Prompt / §3.2 UI）。
> 总体判断：**三个 P0 在「设计意图」上已闭环，方向完全正确**（规则引擎锁数值/严重性，LLM 只做语言理解与归因拆解）。但作为「不补不可交付」的硬核重构，仍有 3 类必须补的残余，否则上线仍会出问题：
> - **① Prompt 约束 ≠ 系统保证**：需要一层后端校验/强约束（最关键）；
> - **② 0.60–0.84 中置信度盲区**：UI 只拦 <0.6，但「无直接技术报错日志」的诊断照样正常展示；
> - **③ 数值仍经 LLM 之手**：`loss_statement` / `affected_dimensions` 仍是 LLM 自由文本重组数字，应改由前端直接渲染结构化字段。

---

## 一、N1 复审（置信度 + INCONCLUSIVE）

**已闭合**：✅ `confidence` 字段、✅ `INCONCLUSIVE` 分支、✅ 低置信转人工 CTA（§3.2 UI 逻辑）。评分指南的三档划分也合理。

**残余：**

- **R1（中置信盲区，P0）**：评分指南中 `0.60–0.84` 定义为「维度贡献集中但**缺乏直接技术报错日志佐证**」——本质是无硬证据的诊断。但 §3.2 的 UI 警戒仅触发于 `confidence < 0.6`。于是 0.60–0.84 的病例会以**正常的 DIAGNOSED 卡片**展示，用户无从察觉"这结论没硬证据"。
  - 修订：UI 警戒阈值提到 `confidence < 0.85`，或新增「中置信」视觉态（如橙色 `[证据不足]` 标签 + 提示"结论缺乏直接日志佐证"）。
- **R2（status↔confidence 无强制，P0）**：Prompt 说"不足→INCONCLUSIVE + confidence<0.6"，但未声明**不变量**。模型完全可能返回 `status: DIAGNOSED, confidence: 0.45`。
  - 修订：明确不变量 `DIAGNOSED ⇒ confidence ≥ 0.6；INCONCLUSIVE ⇒ confidence < 0.6`，并由后端校验（见 R0）。
- **R3（INCONCLUSIVE 的 causal_chain 未定义，P2）**：schema 未规定 INCONCLUSIVE 时 causal_chain 应为空。
  - 修订：明确 `status=INCONCLUSIVE ⇒ causal_chain: []`，`primary_factor: "暂无法明确根因"`。
- **R4（升级动作不应由 LLM 生成，P1）**：§3.2 说 INCONCLUSIVE 时主按钮变「一键呼叫值班运维」。但 §4.2 的 `recommended_actions` 若由 LLM 生成，"转人工"动作可能漏写或措辞漂移。
  - 修订：INCONCLUSIVE 时，升级动作由**后端注入**标准「转人工/转飞书群」卡片，不走 LLM。

---

## 二、N2 复审（Severity 作为输入）

**已闭合**：✅ `severity` 移出输出、✅ 作为 `anomaly_metadata` 输入注入、✅ §5 红线"严禁修改 Severity"。

**残余：**

- **R5（阈值规则未正式落表，P0）**：你口头给了规则示例（"Margin < 0% 或 预估每小时损失 > $500 ➔ CRITICAL"），但 PRD 里**没有正式的 severity 映射表**。dev 无法据此实现规则引擎，UI 也无法对齐标签。
  - 修订：在 §4.1 或新增 §4.3 补一张阈值表，例如：

  | 指标条件 | Severity |
  |---|---|
  | Margin < 0% 或 loss_per_hour ≥ $500 | CRITICAL |
  | Margin 0–5% 或 loss_per_hour $200–500 | HIGH |
  | Margin 5–10% | MEDIUM |
  | 其余 | LOW |

- **R6（示例自相矛盾，P0）**：§4.1 示例里 `severity: CRITICAL` 但 `loss_per_hour_usd: 350`，而 R5 规则说 `>$500 才 CRITICAL`。两处对不上，评审必被问。
  - 修订：要么把示例 loss 改成 ≥500，要么调阈值，使示例自洽。
- **R7（severity→动作风格无映射，P2）**：§5 说"匹配该严重程度的处置紧迫感"，但没给 severity→措辞风格的指引。可补一句小指南（CRITICAL 用"立即"、LOW 用"建议观察"）。

---

## 三、N3 复审（损耗金额算法算）

**已闭合**：✅ `calculated_loss` 作为算法层输入、✅ §5 红线"数值严禁篡改"、✅ `loss_statement` 定位为转述。

**残余：**

- **R8（loss_statement 仍是 LLM 重组数字，P0）**：虽然"严禁修改"，但 `loss_statement` 是 LLM 自由文本（"约 $350.00/小时的损耗"）。模型仍可能把 `$350.00` 格式化成 `$350` / `350美元` / 漏写单位。
  - 修订：**前端直接用 `calculated_loss.loss_per_hour_usd` 组句**（"当前异常预估造成约 $X/小时损耗"），LLM 只输出定性描述（如"损耗持续累积中"），**完全不碰数字**。
- **R9（即上轮 N5 未修，P0）**：`impact_scope.affected_dimensions: "Publisher: Pub_Media_802 (82%)"` 里的 `82%` 来自 `top_contributors.impact_share`，却仍由 LLM 重写，同样有改数风险。
  - 修订：前端直接渲染 `top_contributors` 的维度+贡献度，LLM 只补定性说明。这同时关掉上轮 N5。
- **R10（可选透明字段，P2）**：`calculated_loss` 目前只有 `loss_per_hour_usd` + `calculation_basis`。可补 `cost_rate` / `revenue_gap` 增强可信度与可审计性。

---

## 四、最关键的一条（跨 N1/N2/N3）

### R0 — Prompt 红线 ≠ 系统保证（P0，必须补）

§5 的"绝对红线""严禁猜测""数值严禁篡改"写得很好，但这些都是**对模型的请求，不是系统保证**。模型违反概率非零，而一旦违反，损失的正是你最想锁死的"数值真实性 / 严重性标准 / 逻辑底线"。

**必须在 LLM 之外加一层「后端输出校验 / 强约束」：**

1. **JSON Schema 校验**：类型、枚举（`status`/`severity`）、必填项。
2. **业务不变量校验**：
   - `status=DIAGNOSED ⇒ confidence ≥ 0.6`；`status=INCONCLUSIVE ⇒ confidence < 0.6`；
   - 输出**不得含 `severity` 字段**（若模型擅自输出，后端丢弃，以输入为准）；
   - `loss_statement` 中的数值必须 `== calculated_loss.loss_per_hour_usd`（或干脆按 R8 改前端渲染绕过）。
3. **违规处理**：校验失败不直接信任输出，而是**降级为 `INCONCLUSIVE` + 转人工**，并在日志记一笔"Tess 输出违反不变量"。

> 这一层，才是"规则引擎死死锁住底线"的真正落地。没有它，底线其实还捏在模型手里。

---

## 五、上轮 P1/P2 backlog 状态（本次未触及，需继续跟踪）

| 编号 | 内容 | 状态 |
|---|---|---|
| N4 | Prompt 与 §4.2 schema 未绑定（漂移风险） | 仍开（R0 的 Schema 校验部分覆盖） |
| N5 | impact_scope 自由文本 vs top_contributors | **本次由 R9 覆盖，建议并入 R9 一起关** |
| N6 | 原始痛点「配置文件比对」未被设计覆盖 | 仍开 |
| N7 | 算法层失败无兜底 | 仍开 |
| N8 | 流式输出 vs 严格 JSON 矛盾 | 仍开 |
| N9 | 反馈闭环缺评估集/准确率度量 | 仍开 |
| N10 | 数据窗口"1 小时"硬编码 | 仍开 |
| N11 | 缺 Out of Scope / 依赖 / DoD | 仍开 |
| N12 | 多入口输入一致性未约束 | 仍开 |

---

## 六、本轮 P0 收尾修订清单

**P0（不补不可交付）**
1. **R0** 加后端输出校验/强约束层（Schema + 不变量 + 违规降级）。**【最关键】**
2. **R1** UI 警戒阈值提到 `confidence < 0.85`，或加「中置信/证据不足」视觉态。
3. **R2** 后端强制 `status ↔ confidence` 不变量。
4. **R5** 正式落 severity 阈值映射表（dev 可实现）。
5. **R6** 修 §4.1 示例矛盾（CRITICAL 与 $350 不自洽）。
6. **R8 / R9** 数值与占比改**前端直接渲染** `calculated_loss` / `top_contributors`，LLM 不碰任何数字。

**P1**
7. **R4** INCONCLUSIVE 的升级动作由后端注入，不走 LLM。
8. **R7** 补 severity→动作风格的简短映射。

**P2**
9. **R3** 明确 `INCONCLUSIVE ⇒ causal_chain: []`。
10. **R10** `calculated_loss` 可选补 `cost_rate` / `revenue_gap`。

---

## 给评审会的一句话
> 三个 P0 的"设计"已经对了，但"实现保证"还差最后一层：prompt 里写"严禁"不代表模型真的不做。把 **R0 后端校验层** 补上，并把**所有数字（损耗、占比）从 LLM 手里拿回到前端渲染**，这份重构才真正"死锁了底线"，可以放心交付。
