"""L2-1 · 反馈学习闭环 (Feedback Learning Loop)

把抽屉底部的 👍/👎 从「只记日志」升级为可度量的质量信号，
反哺 Gatekeeper 阈值与 Prompt 调优。

设计原则（与三层死锁严格一致）：
- 反馈只是「数据」，绝不参与改变 severity / 数值 / 路由的死锁层。
- 它只影响「运维侧度量 + 阈值建议」，不触碰信任内核。
- 因此本模块不依赖 LLM、不依赖前端，可独立单测。
"""

import json
import os
from collections import Counter
from typing import Optional

VOTE_ACCURATE = "accurate"
VOTE_INACCURATE = "inaccurate"
VALID_VOTES = (VOTE_ACCURATE, VOTE_INACCURATE)

# 被标「不准确」、但其时 Tess 是高/中置信确诊 → 最危险的误判信号
_DIAGNOSED_STATUSES = ("DIAGNOSED", "DIAGNOSED_SUSPECT")


class FeedbackStore:
    """反馈_ledger：诊断登记 + 投票收集 + 质量度量。

    默认纯内存（适合测试 / 单进程 demo）；
    传入 persist_path 时，每条记录以 JSONL 追加落盘，进程重启可恢复。
    """

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self.persist_path = persist_path
        self._ledger: list[dict] = []
        if persist_path and os.path.exists(persist_path):
            with open(persist_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._ledger.append(json.loads(line))

    # ---- 写入 ----------------------------------------------------------
    def observe_diagnosis(
        self, event_id: str, status: str, confidence: float
    ) -> None:
        """编排层每完成一次诊断就登记一次（算覆盖率 / 降级率用）。"""
        self._append(
            {
                "kind": "diagnosis",
                "event_id": event_id,
                "status": status,
                "confidence": confidence,
            }
        )

    def record_feedback(
        self,
        event_id: str,
        vote: str,
        tess_status: str,
        confidence: float,
        corrected_root_cause: Optional[str] = None,
        corrected_contributor_id: Optional[str] = None,
    ) -> dict:
        if vote not in VALID_VOTES:
            raise ValueError(f"vote 必须为 {VALID_VOTES}，收到：{vote!r}")
        rec = {
            "kind": "feedback",
            "event_id": event_id,
            "vote": vote,
            "tess_status": tess_status,
            "confidence": confidence,
            "corrected_root_cause": corrected_root_cause,
            "corrected_contributor_id": corrected_contributor_id,
        }
        self._append(rec)
        return rec

    # ---- 联合归因（L2-2）反馈 -----------------------------------
    def observe_joint(
        self, event_ids: list, status: str, confidence: float
    ) -> None:
        """联合归因完成一次就登记一次（算联合覆盖率 / 降级率用）。"""
        self._append(
            {
                "kind": "joint_diagnosis",
                "event_ids": list(event_ids),
                "status": status,
                "confidence": confidence,
            }
        )

    def record_joint_feedback(
        self,
        event_ids: list,
        vote: str,
        tess_status: str,
        confidence: float,
        corrected_joint_factor: Optional[str] = None,
    ) -> dict:
        if vote not in VALID_VOTES:
            raise ValueError(f"vote 必须为 {VALID_VOTES}，收到：{vote!r}")
        rec = {
            "kind": "joint_feedback",
            "event_ids": list(event_ids),
            "vote": vote,
            "tess_status": tess_status,
            "confidence": confidence,
            "corrected_joint_factor": corrected_joint_factor,
        }
        self._append(rec)
        return rec

    def _append(self, rec: dict) -> None:
        self._ledger.append(rec)
        if self.persist_path:
            with open(self.persist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- 半自动处置（L2-3）观测 ---------------------------------
    def observe_remediation(self, state: str) -> None:
        """处置单每发生一次状态变更就登记一次（算处置闭环率用）。"""
        self._append({"kind": "remediation", "state": state})

    # ---- 自愈数据抽取 --------------------------------------------------
    def labeled_feedback(self) -> list:
        """抽取带置信度的投票标签，供反馈自愈 (self-heal) 使用。

        Returns:
            [(confidence: float, is_accurate: bool), ...]
            is_accurate 由 vote == "accurate" 决定。
        """
        out = []
        for r in self._ledger:
            if r.get("kind") != "feedback":
                continue
            if "confidence" not in r or r.get("vote") not in VALID_VOTES:
                continue
            out.append((float(r["confidence"]), r["vote"] == VOTE_ACCURATE))
        return out

    # ---- 度量 ----------------------------------------------------------
    def metrics(self) -> dict:
        diagnoses = [r for r in self._ledger if r["kind"] == "diagnosis"]
        feedback = [r for r in self._ledger if r["kind"] == "feedback"]
        joint_diag = [r for r in self._ledger if r["kind"] == "joint_diagnosis"]
        joint_fb = [r for r in self._ledger if r["kind"] == "joint_feedback"]
        remediation = [r for r in self._ledger if r["kind"] == "remediation"]
        total = len(diagnoses)
        fb_count = len(feedback)
        joint_total = len(joint_diag)
        joint_fb_count = len(joint_fb)

        status_dist = dict(Counter(d["status"] for d in diagnoses))
        vote_dist = dict(Counter(f["vote"] for f in feedback))
        downgrade_rate = (status_dist.get("INCONCLUSIVE", 0) / total) if total else 0.0

        # 最危险指标：被标「不准确」、但其时 Tess 是高/中置信确诊
        risky = [
            f
            for f in feedback
            if f["vote"] == VOTE_INACCURATE
            and f["tess_status"] in _DIAGNOSED_STATUSES
        ]
        diagnosed_fb = [
            f for f in feedback if f["tess_status"] in _DIAGNOSED_STATUSES
        ]
        inaccurate_on_diagnosed = (
            (len(risky) / len(diagnosed_fb)) if diagnosed_fb else 0.0
        )

        suggestion = self._suggest(downgrade_rate, inaccurate_on_diagnosed)

        return {
            "total_diagnoses": total,
            "feedback_count": fb_count,
            "feedback_coverage": (fb_count / total) if total else 0.0,
            "status_distribution": status_dist,
            "vote_distribution": vote_dist,
            "downgrade_rate": round(downgrade_rate, 4),
            "inaccurate_on_diagnosed_rate": round(inaccurate_on_diagnosed, 4),
            "suggestion": suggestion,
            "joint_diagnoses": joint_total,
            "joint_feedback_count": joint_fb_count,
            "remediation_count": len(remediation),
        }

    @staticmethod
    def _suggest(downgrade_rate: float, inaccurate_on_diagnosed: float) -> str:
        if inaccurate_on_diagnosed >= 0.20:
            return (
                "⚠️ 高/中置信确诊被标「不准确」比例过高（≥20%），"
                "建议上调 SUSPECT 阈值或收紧 Prompt 置信度评分指南。"
            )
        if downgrade_rate >= 0.40:
            return (
                "降级率偏高（≥40%），多数事件 Tess 无法确诊，"
                "建议补充算法层信号（如更细的 API 报错日志接入）。"
            )
        return "✅ 当前反馈指标健康，暂无需调整阈值。"

    def report(self) -> str:
        m = self.metrics()
        lines = [
            "===== Tess 反馈质量周报 =====",
            f"诊断总数        : {m['total_diagnoses']}",
            f"反馈数 / 覆盖率 : {m['feedback_count']} / {m['feedback_coverage']:.1%}",
            f"状态分布        : {m['status_distribution']}",
            f"投票分布        : {m['vote_distribution']}",
            f"降级率          : {m['downgrade_rate']:.1%}",
            f"高置信误判率    : {m['inaccurate_on_diagnosed_rate']:.1%}",
            f"建议            : {m['suggestion']}",
        ]
        return "\n".join(lines)
