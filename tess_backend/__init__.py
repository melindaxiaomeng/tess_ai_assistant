"""Tess AI 智能归因与诊断系统 —— 后端核心（法案层）。

本包只落地 PRD V2.0.0 的「后端核心 P0-P2」：

- P0 契约层 (contracts)：Input / Output 的 Schema 与常量。
- P1 规则引擎 (rule_engine)：Severity 判定 + 损耗计算。
- P2 校验网关 (gatekeeper)：对 LLM 输出做 Schema + 不变量 + 幻觉 ID 校验，
  违规即熔断降级为 INCONCLUSIVE。

设计铁律（PRD §1.3 三层死锁）：
    Prompt 是建议，Gatekeeper 才是法典，前端 Template 才是渲染屏障。
LLM 不得生成 severity / 金额 / 百分比 / 跳转 URL —— 全部由本层与算法层死锁。
"""

__version__ = "2.0.0"
