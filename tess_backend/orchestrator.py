"""P4 · 编排层 —— 把特征提取 / 规则引擎 / LLM / Gatekeeper 串成一次诊断。

职责边界：
- 算法层该算的（Severity、损耗）在此确保已注入 Input，LLM 只消费。
- 真正的「特征提取」依赖你们既有统计引擎，本层只做「兜底注入」：
  若 Input 已带 severity / calculated_loss 则信任之；否则用规则引擎现算。
- 之后交给 TessAgent（其内部必然经过 Gatekeeper）。
"""

import copy
import re

from .contracts import SEVERITY_UNKNOWN
from .rule_engine import compute_severity, calculate_loss_per_hour
from .tess_agent import TessAgent
from .thresholds import ThresholdPolicy, default_policy
from .privacy import deidentify_input


def _parse_margin_pct(current_value) -> float:
    """从 '3.8%' / '15.1%' / -2.4 之类解析出毛利率数值（百分比）。

    解析失败默认 100.0（即视为安全档），避免误判严重度。
    """
    if current_value is None:
        return 100.0
    if isinstance(current_value, (int, float)):
        return float(current_value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(current_value))
    return float(m.group()) if m else 100.0


def enrich_with_rule_engine(input_data: dict) -> dict:
    """确保 anomaly_metadata 携带算法层算好的 severity 与 calculated_loss。

    返回深拷贝，绝不 mutate 调用方原对象。
    """
    data = copy.deepcopy(input_data)
    meta = data.setdefault("anomaly_metadata", {})

    margin = _parse_margin_pct(meta.get("current_value"))
    loss_obj = meta.get("calculated_loss") or {}
    loss = loss_obj.get("loss_per_hour_usd")

    # 损耗兜底计算：若 Input 没带 calculated_loss，尝试用成本/收益缺口现算
    if loss is None:
        cost = float(meta.get("cost_rate_usd") or 0.0)
        missing_rev = float(meta.get("missing_revenue_usd") or 0.0)
        loss = calculate_loss_per_hour(cost, missing_rev)
        meta["calculated_loss"] = {
            "loss_per_hour_usd": loss,
            "calculation_basis": "编排层兜底：cost_rate + missing_revenue 差值",
        }
    else:
        loss = float(loss)

    # Severity：Input 已带则信任，否则现算
    if not meta.get("severity"):
        meta["severity"] = compute_severity(margin, loss)
    elif meta["severity"] not in (
        "CRITICAL",
        "HIGH",
        "MEDIUM",
        "LOW",
    ):
        meta["severity"] = SEVERITY_UNKNOWN

    return data


def run_diagnosis(input_data: dict, llm, policy: ThresholdPolicy = None) -> dict:
    """端到端跑一次 Tess 诊断。

    Args:
        input_data: 原始异常上下文（异常池 / 触发层传来）。
        llm:        一个 LLMClient（Mock 或真实后端）。
        policy:      置信度切点策略；为 None 时用默认初版阈值（不读盘）。
    Returns:
        Gatekeeper 归一化后的安全字典，可直接返回前端。
    """
    # 喂给 LLM 前先脱敏：真实 GAID 永不离开本网络、不发送给 LLM 服务商。
    # IP / UA 原样保留（IP 分析依赖完整地址）。
    input_data = deidentify_input(input_data)
    enriched = enrich_with_rule_engine(input_data)
    agent = TessAgent(llm, policy=policy or default_policy())
    return agent.diagnose(enriched)
