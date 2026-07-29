"""P5 · 可学习的阈值策略 (ThresholdPolicy)

把 Gatekeeper 的置信度阈值从「写死的常量」升级为「可持久化、可学习的策略」。

设计哲学（严格对齐三层死锁）：
- 阈值只是 Gatekeeper 内部的「路由切点」，由后端代码 / 配置持有，绝不由 LLM 或反馈直接注入。
- 反馈学习（L2-1 self-heal）只能产出一份「阈值提案 (HealProposal)」；
  是否落地由运维显式 apply（或人审后改 thresholds.json）。
- 因此反馈数据永远不会绕过死锁直接改写 severity / 数值 / 路由，
  它只通过「后端批准的配置」间接影响切点。
"""

import json
import os
from dataclasses import asdict, dataclass
from typing import Optional

from .contracts import (
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_SUSPECT_FLOOR,
    INCONCLUSIVE_CONFIDENCE_CAP,
)

# 学习策略落盘路径：环境变量优先，否则包内默认文件（已被 .gitignore 忽略）
DEFAULT_THRESHOLD_PATH = os.path.join(os.path.dirname(__file__), "thresholds.json")

# 切点的合法边界（自愈提案不可越界，保证安全下限）
MIN_SUSPECT_FLOOR = 0.30
MAX_SUSPECT_FLOOR = 0.90
MIN_HIGH_GAP = 0.05
MAX_HIGH_THRESHOLD = 0.98


@dataclass
class ThresholdPolicy:
    suspect_floor: float           # [floor, high) -> DIAGNOSED_SUSPECT；< floor -> INCONCLUSIVE
    high_threshold: float          # >= high -> DIAGNOSED
    source: str = "default"       # "default" | "learned"
    version: int = 0              # 每次 learn / apply 自增

    @property
    def inconclusive_cap(self) -> float:
        # INCONCLUSIVE 时置信度钳制上限，避免与「高/中置信」语义冲突
        return min(INCONCLUSIVE_CONFIDENCE_CAP, self.suspect_floor - 0.01)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ThresholdPolicy":
        return cls(
            suspect_floor=float(d["suspect_floor"]),
            high_threshold=float(d["high_threshold"]),
            source=str(d.get("source", "learned")),
            version=int(d.get("version", 0)),
        )


def default_policy() -> ThresholdPolicy:
    """PRD §3.2 / §5 初版阈值（不落盘，永远可作为安全基线）。"""
    return ThresholdPolicy(
        suspect_floor=CONFIDENCE_SUSPECT_FLOOR,
        high_threshold=CONFIDENCE_HIGH_THRESHOLD,
        source="default",
        version=0,
    )


def _resolve_path(path: Optional[str]) -> str:
    return path or os.getenv("TESS_THRESHOLD_PATH") or DEFAULT_THRESHOLD_PATH


def load_policy(path: Optional[str] = None) -> ThresholdPolicy:
    """读取已落盘的学习策略；不存在或损坏则返回默认策略（不写盘）。"""
    p = _resolve_path(path)
    if not os.path.exists(p):
        return default_policy()
    try:
        with open(p, "r", encoding="utf-8") as f:
            return ThresholdPolicy.from_dict(json.load(f))
    except Exception:
        return default_policy()


def save_policy(policy: ThresholdPolicy, path: Optional[str] = None) -> str:
    """把策略落盘（apply 时调用）。返回实际路径。"""
    p = _resolve_path(path)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(policy.to_dict(), f, ensure_ascii=False, indent=2)
    return p


def reset_policy(path: Optional[str] = None) -> ThresholdPolicy:
    """恢复默认策略（删掉学习文件，回到初版阈值）。"""
    p = _resolve_path(path)
    if os.path.exists(p):
        os.remove(p)
    return default_policy()
