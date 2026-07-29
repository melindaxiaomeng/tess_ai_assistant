"""L2-1 · 反馈自愈 (Self-Healing Threshold Tuner)

把 L2-1 反馈_ledger 里的人工投票，转成 Gatekeeper 置信度切点的「学习提案」。

核心假设（贴合现有反馈信号）：
- vote=accurate  → 这次诊断是对的 → 应「信任」（非 INCONCLUSIVE）。
- vote=inaccurate→ 这次诊断是错的 → 应「降级为 INCONCLUSIVE」（别再轻信）。

因此最优问题退化为：找一个 INCONCLUSIVE 切点 s，使
    accurate 且 conf>=s  （→信任）   记对
    inaccurate 且 conf<s  （→降级）   记对
最大化。DIAGNOSED vs SUSPECT 的切点 h 反馈无标签，
保持保守：h = max(s+gap, 默认h)，仅确保与 s 拉开距离，不擅自抬高「高置信」承诺。

安全护栏（绝不让反馈直接改写死锁层）：
- 样本不足（< MIN_SAMPLES）不提案。
- 提案必须比「默认策略」在历史数据上的模拟准确率提升 >= MIN_IMPROVEMENT，否则不采纳。
- 切点严格限制在合法边界内，保证安全下限。
- 提案只产出数据；是否落盘由调用方显式 apply（或人审后改 thresholds.json）。
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .thresholds import (
    MAX_HIGH_THRESHOLD,
    MAX_SUSPECT_FLOOR,
    MIN_HIGH_GAP,
    MIN_SUSPECT_FLOOR,
    ThresholdPolicy,
    default_policy,
    save_policy,
)

# 学习超参
MIN_SAMPLES = 20
MIN_IMPROVEMENT = 0.05     # 提案须至少相对提升 5pt 才采纳
_SWEEP_STEP = 0.01
_HIGH_GAP_DEFAULT = 0.20   # 无标签时 h 相对 s 的默认抬升


@dataclass
class HealProposal:
    accepted: bool
    reason: str
    samples: int
    current: dict             # {suspect_floor, high_threshold}
    proposed: dict
    current_accuracy: float
    proposed_accuracy: float
    prompt_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "samples": self.samples,
            "current": self.current,
            "proposed": self.proposed,
            "current_accuracy": round(self.current_accuracy, 4),
            "proposed_accuracy": round(self.proposed_accuracy, 4),
            "prompt_hint": self.prompt_hint,
        }


def _simulate_accuracy(records: List[Tuple[float, bool]], s: float) -> float:
    """用切点 s 模拟：accurate&conf>=s 或 inaccurate&conf<s 记对。"""
    if not records:
        return 0.0
    ok = sum(
        1 for conf, acc in records if (acc and conf >= s) or (not acc and conf < s)
    )
    return ok / len(records)


def propose_thresholds(
    records: List[Tuple[float, bool]],
    current: Optional[ThresholdPolicy] = None,
    min_samples: int = MIN_SAMPLES,
    min_improvement: float = MIN_IMPROVEMENT,
) -> HealProposal:
    """依据带标签反馈，产出（或否决）一份阈值学习提案。

    Args:
        records:          [(confidence, is_accurate), ...]，通常来自 store.labeled_feedback()。
        current:          当前生效策略；默认用初版默认阈值作为基线。
        min_samples:      最小样本量护栏。
        min_improvement:  最小相对提升护栏（低于则不采纳）。
    """
    cur = current or default_policy()
    n = len(records)
    cur_acc = _simulate_accuracy(records, cur.suspect_floor)

    # 护栏 1：样本不足
    if n < min_samples:
        return HealProposal(
            accepted=False,
            reason=(
                f"样本不足：仅 {n} 条带标签反馈（需 ≥ {min_samples}），暂不调整阈值。"
            ),
            samples=n,
            current=cur.to_dict(),
            proposed=cur.to_dict(),
            current_accuracy=cur_acc,
            proposed_accuracy=cur_acc,
        )

    # 扫描候选切点 s ∈ [MIN, MAX]，以 1pt 为步
    best_s = cur.suspect_floor
    best_acc = cur_acc
    lo = int(MIN_SUSPECT_FLOOR * 100)
    hi = int(MAX_SUSPECT_FLOOR * 100)
    for i in range(lo, hi + 1):
        s = i / 100.0
        acc = _simulate_accuracy(records, s)
        # 准确率更高则更新；平局时优先选最接近当前值（最小扰动）
        closer = abs(s - cur.suspect_floor) < abs(best_s - cur.suspect_floor)
        if acc > best_acc + 1e-9 or (
            abs(acc - best_acc) <= 1e-9 and closer
        ):
            best_acc = acc
            best_s = s

    # 推导 h：保守，仅保证与 s 拉开距离
    best_h = max(best_s + _HIGH_GAP_DEFAULT, cur.high_threshold)
    best_h = min(best_h, MAX_HIGH_THRESHOLD)
    if best_h - best_s < MIN_HIGH_GAP:
        best_h = min(best_s + MIN_HIGH_GAP, MAX_HIGH_THRESHOLD)

    proposed = ThresholdPolicy(
        suspect_floor=round(best_s, 2),
        high_threshold=round(best_h, 2),
        source="learned",
        version=cur.version + 1,
    )

    improvement = best_acc - cur_acc
    # 护栏 2：必须有实质提升
    if improvement < min_improvement:
        return HealProposal(
            accepted=False,
            reason=(
                f"历史模拟准确率 {cur_acc:.1%} → {best_acc:.1%}，"
                f"提升 {improvement:.1%} 低于阈值 {min_improvement:.0%}，暂不调整。"
            ),
            samples=n,
            current=cur.to_dict(),
            proposed=cur.to_dict(),
            current_accuracy=cur_acc,
            proposed_accuracy=best_acc,
        )

    hint = (
        f"建议同步更新 System Prompt §5 置信度评分指南："
        f"将 INCONCLUSIVE 下限定为 < {proposed.suspect_floor:.2f}，"
        f"高置信 DIAGNOSED 起点上调至 ≥ {proposed.high_threshold:.2f}。"
    )
    return HealProposal(
        accepted=True,
        reason=(
            f"基于 {n} 条反馈，切点由 {cur.suspect_floor:.2f} 调整为 {proposed.suspect_floor:.2f}，"
            f"历史模拟准确率 {cur_acc:.1%} → {best_acc:.1%}（+{improvement:.1%}）。"
        ),
        samples=n,
        current=cur.to_dict(),
        proposed=proposed.to_dict(),
        current_accuracy=cur_acc,
        proposed_accuracy=best_acc,
        prompt_hint=hint,
    )


def apply_proposal(
    proposal: HealProposal, path: Optional[str] = None
) -> Optional[ThresholdPolicy]:
    """仅当提案 accepted 时落盘学习策略；返回新策略或 None。"""
    if not proposal.accepted:
        return None
    policy = ThresholdPolicy.from_dict(proposal.proposed)
    save_policy(policy, path)
    return policy
