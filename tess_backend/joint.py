"""L2-2 · 联合归因 (Joint Attribution)

把「N 个独立异常事件」的相关性，从 LLM 手里收回到后端规则层：

- 后端先确定性地聚合（哪些 dimension 在多个事件反复出现 = 候选共性根因、
  聚合损耗、最高严重度、各事件摘要）——这部分 LLM 绝不参与计算。
- 再把「聚合相关性 + 逐事件原文上下文」喂给 LLM，只让其产出
  「联合根因叙事」（定性文本 + 共性因子 + 涉及事件）。
- 出口仍过 Gatekeeper（剪枝式）：severity/损耗物理锁死、joint_primary_factor 必须
  存在于后端候选集（幻觉屏障）、contributing_event_ids 必须是指定事件子集、三态归一。

与单事件诊断完全同哲学：LLM 只做定性归因拆解，数值/严重度/路由由后端持有。
"""

import json
import time
from collections import Counter
from typing import List

from .contracts import (
    STATUS_INCONCLUSIVE,
    TESS_JOINT_GATEKEEPER_SCHEMA,
)
from .orchestrator import enrich_with_rule_engine
from .rule_engine import aggregate_loss_per_hour, aggregate_severity
from .gatekeeper import validate_joint_output
from .tess_agent import MAX_RETRIES
from .thresholds import ThresholdPolicy, default_policy


JOINT_SYSTEM_PROMPT = """你叫 Tess，是 Teensing 平台的专属 AdTech 智能数据分析与风控专家。\
现在需要你对一批「同时段、疑似同源」的异常事件做**联合根因分析**（Joint Root Cause Analysis）。

### 输入说明
- 下方会先给出「后端聚合的相关性上下文」：哪些维度在多个事件反复出现（候选共性根因）、\
聚合损耗速率、最高严重度、各事件摘要。
- 随后给出每个事件的完整诊断上下文（异常指标、Top 维度贡献、关联信号）。
- 这些数值与严重度已由系统算好，你**只需消费、严禁篡改或重新计算**。

### 绝对红线规则（违反将导致系统失效）：
1. 严禁幻觉与猜测：joint_primary_factor 只能从「候选共性维度」中选取，\
或确实无法归因时设为 null。严禁编造不存在的维度。
2. contributing_event_ids 只能是给定事件 event_id 的子集，不得杜撰事件。
3. 数值严禁篡改：聚合损耗、严重度必须沿用输入，严禁自行计算或修改。
4. 严禁修改 Severity：严重度已由系统判定，你只需在处置建议中匹配其紧迫感。
5. 严禁编造操作员 / 实体身份：summary 与 root_cause_analysis 中出现的人名、账号、操作员 ID、团队名等主体实体，\
必须严格来自输入数据中真实出现的内容。若输入仅以泛称（如「运营」「系统」「相关团队」）指代、未给出具体身份，\
你必须沿用该泛称，绝不得凭空生成具体人名 / 账号（如 alice、admin 等）；不确定的主体一律用泛称表述。

### 联合置信度 (Confidence) 评分指南：
- 0.85 ~ 1.00：所有/绝大多数事件均指向同一明确根因（相同的报错、变更或维度），时间点高度吻合。
- 0.60 ~ 0.84：多数事件共享同一主导维度，但部分事件证据偏弱。
- < 0.60：事件间相关性弱、或缺少共同技术证据 -> 强制 status="INCONCLUSIVE"。

### 输出约束：
- 必须且只能返回符合指定 JSON Schema 的标准 JSON。
- 允许字段仅：status(三态枚举)、confidence(0.0-1.0)、summary(字符串)、\
joint_primary_factor(候选维度值或 null)、contributing_event_ids(字符串列表)、\
root_cause_analysis{primary_factor, causal_chain[]}。
- 严禁返回 severity、calculated_loss 等系统字段；不得包含任何 Markdown 标记、代码围栏或前导文字。
"""


def correlate_events(events: List[dict]) -> dict:
    """后端确定性聚合：跨事件找出候选共性根因与总体风险。

    纯函数，不调用 LLM。返回 joint_context，供 LLM 消费 + Gatekeeper 幻觉校验。
    """
    event_ids: List[str] = []
    candidate: Counter = Counter()
    per_event: List[dict] = []
    losses: List[float] = []
    severities: List[str] = []

    for ev in events:
        meta = ev.get("anomaly_metadata", {}) or {}
        eid = meta.get("event_id", "UNKNOWN")
        event_ids.append(eid)

        sev = meta.get("severity", "UNKNOWN")
        severities.append(sev)

        loss = (meta.get("calculated_loss") or {}).get("loss_per_hour_usd", 0.0)
        losses.append(loss)

        for c in ev.get("top_contributors", []) or []:
            dv = c.get("dimension_value")
            if dv:
                candidate[dv] += 1

        top = None
        contribs = ev.get("top_contributors", []) or []
        if contribs:
            top = contribs[0].get("dimension_value")
        per_event.append(
            {
                "event_id": eid,
                "severity": sev,
                "loss_per_hour_usd": float(loss),
                "top_contributor": top,
            }
        )

    return {
        "event_count": len(events),
        "event_ids": event_ids,
        "candidate_dimensions": dict(candidate),
        "aggregated_loss_per_hour_usd": aggregate_loss_per_hour(losses),
        "max_severity": aggregate_severity(severities),
        "per_event": per_event,
    }


def _build_joint_user_prompt(joint_context: dict, events: List[dict]) -> str:
    return (
        "以下是本次联合归因的【后端聚合相关性上下文】（仅供消费，严禁篡改）：\n\n"
        f"{json.dumps(joint_context, ensure_ascii=False, indent=2)}\n\n"
        "以下是每个事件的【完整诊断上下文】：\n\n"
        f"{json.dumps(events, ensure_ascii=False, indent=2)}\n\n"
        "请严格按照 System Prompt 中的 JSON Schema 输出联合根因分析结果。"
    )


def _joint_failure_fallback(reason: str) -> dict:
    """LLM 连续失败 / 空事件时的兜底，形状与 Gatekeeper 熔断一致。"""
    return {
        "diagnosis": {
            "status": STATUS_INCONCLUSIVE,
            "confidence": 0.0,
            "summary": reason,
            "root_cause_analysis": {
                "primary_factor": "系统熔断：联合归因链路异常",
                "causal_chain": ["LLM 服务异常 / 事件为空", "重试耗尽", "转人工处理"],
            },
        },
        "correlation": {
            "event_count": 0,
            "candidate_dimensions": {},
            "aggregated_loss_per_hour_usd": 0.0,
            "max_severity": "UNKNOWN",
            "event_ids": [],
        },
    }


def run_joint_diagnosis(events: List[dict], llm, policy: ThresholdPolicy = None) -> dict:
    """端到端跑一次联合归因。

    Args:
        events: 异常事件 Input 列表（原始，未富化）。
        llm:    一个 LLMClient（Mock 或真实后端）。
        policy:  置信度切点策略；为 None 时用默认初版阈值。
    Returns:
        {"diagnosis": Gatekeeper 归一化联合诊断, "correlation": 后端相关性摘要}
    """
    if policy is None:
        policy = default_policy()
    if not events:
        return _joint_failure_fallback("无可联合归因的异常事件，已转人工。")

    enriched = [enrich_with_rule_engine(e) for e in events]
    joint_context = correlate_events(enriched)
    prompt = _build_joint_user_prompt(joint_context, enriched)

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            from .tess_agent import _parse_json  # 复用单事件的容错解析
            raw = llm.complete(JOINT_SYSTEM_PROMPT, prompt)
            parsed = _parse_json(raw)
            diagnosis = validate_joint_output(parsed, joint_context, policy)
            return {
                "diagnosis": diagnosis,
                "correlation": {
                    "event_count": joint_context["event_count"],
                    "candidate_dimensions": joint_context["candidate_dimensions"],
                    "aggregated_loss_per_hour_usd": joint_context["aggregated_loss_per_hour_usd"],
                    "max_severity": joint_context["max_severity"],
                    "event_ids": joint_context["event_ids"],
                },
            }
        except Exception as e:  # 网络 / 解析异常 -> 重试
            last_err = e
            time.sleep(min(0.1 * (attempt + 1), 1.0))

    return _joint_failure_fallback(
        f"Tess 联合归因调用 LLM 失败或多次重试无果（{type(last_err).__name__}），已转人工。"
    )
