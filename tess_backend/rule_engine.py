"""P1 规则引擎 —— 由算法层算好 Severity 与损耗，作为 Input 注入给 LLM。

LLM 绝不生成这些数值（PRD §1.3 死锁原则 / N2 / N3）。
本模块为纯函数，单测友好。
"""

from .contracts import (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_LOW,
    SEVERITY_UNKNOWN,
)


def _rank(severity: str) -> int:
    """严重度排序权重，供 max 聚合使用。"""
    return {
        SEVERITY_UNKNOWN: -1,
        SEVERITY_LOW: 0,
        SEVERITY_MEDIUM: 1,
        SEVERITY_HIGH: 2,
        SEVERITY_CRITICAL: 3,
    }.get(severity, -1)


def _severity_from_margin(margin_pct: float) -> str:
    """按 §4.3 条件 A（毛利率）判定档位。"""
    if margin_pct < 0:
        return SEVERITY_CRITICAL
    if margin_pct < 10:
        return SEVERITY_HIGH
    if margin_pct < 15:
        return SEVERITY_MEDIUM
    return SEVERITY_LOW


def _severity_from_loss(loss_per_hour_usd: float) -> str:
    """按 §4.3 条件 B（预估每小时损失）判定档位。"""
    if loss_per_hour_usd >= 500:
        return SEVERITY_CRITICAL
    if loss_per_hour_usd >= 100:
        return SEVERITY_HIGH
    if loss_per_hour_usd >= 20:
        return SEVERITY_MEDIUM
    return SEVERITY_LOW


def compute_severity(margin_pct: float, loss_per_hour_usd: float) -> str:
    """Severity = max(Rule_Margin, Rule_Loss)（§4.3 聚合策略）。

    ⚠️ 产品待拍板（PRD §4.3）：当前取「最严重」聚合。
    若业务希望「损失金额优先」或「双低才算低」，需改此处。
    """
    return max(
        _severity_from_margin(margin_pct),
        _severity_from_loss(loss_per_hour_usd),
        key=_rank,
    )


def calculate_loss_per_hour(
    cost_rate_usd: float,
    missing_revenue_usd: float,
) -> float:
    """损耗速率 = 仍在发生的消耗速率 + 缺失的收益缺口（§4.1 calculated_loss）。

    cost_rate_usd:   异常期间每分钟/每小时仍在燃烧的投放成本。
    missing_revenue_usd: 因收益缺失（Postback 丢失等）而未收回的收入。
    两者均为非负；损耗 = 二者之和（向下不为负）。
    """
    loss = (cost_rate_usd or 0.0) + (missing_revenue_usd or 0.0)
    return max(0.0, float(loss))


def aggregate_severity(severities: list) -> str:
    """跨多事件聚合严重度：取「最严重」一档（与 compute_severity 的 max 策略一致）。

    未知 / 非法档位按 UNKNOWN 处理，不污染聚合结果。
    """
    best = SEVERITY_UNKNOWN
    for s in severities or []:
        if s not in (
            SEVERITY_CRITICAL,
            SEVERITY_HIGH,
            SEVERITY_MEDIUM,
            SEVERITY_LOW,
            SEVERITY_UNKNOWN,
        ):
            continue
        if _rank(s) > _rank(best):
            best = s
    return best


def aggregate_loss_per_hour(losses: list) -> float:
    """跨多事件聚合损耗速率（USD/小时）：非负求和。

    各事件损耗均为「仍在燃烧」的速率，叠加即总体燃烧速率（保守上界）。
    """
    return sum(max(0.0, float(x)) for x in (losses or []))
