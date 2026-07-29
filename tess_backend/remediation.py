"""L2-3 · 半自动处置（带审批流 / Semi-automatic Remediation with Approval）

设计核心（死锁原则的延伸）：
- LLM **只建议**处置动作，绝不触碰执行器（executor）。
- 真正执行必须经过人工审批：状态机 PENDING → APPROVED → EXECUTED/FAILED。
- 执行器通过依赖注入（默认 Mock），生产环境替换为 Teensing 平台真实 API 适配器。
- 提案先过 Gatekeeper.validate_remediation：黑名单物理拒绝、白名单校验、
  目标防幻觉（必须在候选集）、参数按规则校验。

与单事件/联合归因完全同哲学：LLM 只做定性建议，动作/目标/执行由后端持有。
"""

import json
import os
import time
from typing import Optional

from .contracts import (
    STATUS_DIAGNOSED,
    STATUS_DIAGNOSED_SUSPECT,
    ALLOWED_REMEDIATION_ACTIONS,
    REMEDIATION_PENDING,
    REMEDIATION_APPROVED,
    REMEDIATION_REJECTED,
    REMEDIATION_EXECUTED,
    REMEDIATION_FAILED,
)
from .gatekeeper import validate_remediation
from .tess_agent import _parse_json, MAX_RETRIES

REMEDIATION_SYSTEM_PROMPT = """你叫 Tess，是 Teensing 平台的专属 AdTech 智能数据分析与风控专家。\
现在需要你针对**已确诊**的异常根因，建议一项**处置动作**（remediation）。

### 你的角色边界（极重要）
- 你**只建议、绝不执行**。你返回的只是一段 JSON 提案，系统会交由人工审批后才执行。
- 你**不能**也**不应**直接改动任何业务系统、数据库或配置。

### 输入说明
- 下方会给出【已确诊的根因诊断结论】与【当前最高严重度】。
- 随后给出【允许建议的处置动作白名单】（action_type 必须完全一致）与
  【允许处置的目标 ID 列表】（target_id 只能从中选取）。
- NOTIFY_ONCALL 表示仅通知值班、不改动业务，此时 target_id 可填 null。

### 绝对红线（违反将导致提案被系统拒绝）：
1. action_type 只能从白名单中选取，**严禁 invent 不在白名单的动作**。
2. target_id 只能是给定候选 ID 之一，**严禁编造不存在的目标**（防幻觉）。
3. 数值/参数必须合理（如暂停时长须为正整数）。

### 输出约束（严格 JSON，无 Markdown、无代码围栏）：
- 字段仅：action_type(字符串)、target_id(字符串或 null)、params(对象)、rationale(字符串)。
- rationale 用一句话说明「为什么这个动作能缓解该根因」。
"""

# ---------------------------------------------------------------------------
# 上下文抽取：从 diagnosis + context 推出「候选目标集」与「最高严重度」
# ---------------------------------------------------------------------------

def _extract_context_for_remediation(diagnosis: dict, context: dict):
    """从诊断结论与原始上下文抽取候选目标与严重度。

    候选目标 = 诊断里的 primary_contributor_id / joint_primary_factor
             + context 里所有 top_contributors 的 dimension_value
             + joint context 的 event_ids（流量/配置类动作可能以事件为对象）。
    """
    targets: set = set()
    severity = "UNKNOWN"

    if isinstance(diagnosis, dict):
        for key in ("primary_contributor_id", "joint_primary_factor"):
            v = diagnosis.get(key)
            if v:
                targets.add(v)

    if isinstance(context, dict):
        meta = context.get("anomaly_metadata", {}) or {}
        if meta.get("severity"):
            severity = meta["severity"]
        for c in context.get("top_contributors", []) or []:
            dv = c.get("dimension_value")
            if dv:
                targets.add(dv)
        # 联合归因：events 列表
        for ev in context.get("events", []) or []:
            m = (ev.get("anomaly_metadata") or {})
            if m.get("severity"):
                severity = m["severity"]
            for c in ev.get("top_contributors", []) or []:
                dv = c.get("dimension_value")
                if dv:
                    targets.add(dv)
        if context.get("max_severity"):
            severity = context["max_severity"]
        for eid in context.get("event_ids", []) or []:
            if eid:
                targets.add(eid)

    return targets, severity


def _build_remediation_user_prompt(diagnosis, context, candidate_targets, severity) -> str:
    catalog = json.dumps(ALLOWED_REMEDIATION_ACTIONS, ensure_ascii=False, indent=2)
    return (
        "以下是对某异常（已确诊）的【根因诊断结论】，请据此建议一项处置动作：\n\n"
        f"{json.dumps(diagnosis, ensure_ascii=False, indent=2)}\n\n"
        f"当前最高严重度：{severity}\n\n"
        "你可以建议的处置动作**仅限**以下白名单（action_type 必须完全一致）：\n"
        f"{catalog}\n\n"
        f"允许处置的目标 ID 仅限：{sorted(candidate_targets)}\n"
        "（NOTIFY_ONCALL 可填 null 表示仅告警不改动）\n\n"
        "请严格返回 JSON：{\"action_type\": ..., \"target_id\": ..., \"params\": {...}, \"rationale\": ...}。"
    )


# ---------------------------------------------------------------------------
# 提案生成（LLM 建议层）
# ---------------------------------------------------------------------------

def propose_remediation(diagnosis: dict, context: dict, llm) -> dict:
    """端到端生成一次处置提案（LLM 建议 + Gatekeeper 校验）。

    Args:
        diagnosis: Gatekeeper 归一化后的诊断（单事件或联合）。
        context:   原始上下文（单事件 Input 或联合的 {events: [...]} 等），
                   用于抽取候选目标与严重度。
        llm:       一个 LLMClient（Mock 或真实后端）。
    Returns:
        {"accepted": bool, "reason": str, "proposal": normalized|None,
         "severity": str, "diagnosis_status": str}
    """
    if not isinstance(diagnosis, dict):
        return {
            "accepted": False,
            "reason": "诊断缺失，无法建议处置",
            "proposal": None,
            "severity": "UNKNOWN",
            "diagnosis_status": None,
        }

    status = diagnosis.get("status")
    # 不确定根因（INCONCLUSIVE）绝不自动处置——转人工
    if status not in (STATUS_DIAGNOSED, STATUS_DIAGNOSED_SUSPECT):
        return {
            "accepted": False,
            "reason": f"诊断状态为 {status}，根因不确定，Tess 不建议自动处置（转人工）",
            "proposal": None,
            "severity": "UNKNOWN",
            "diagnosis_status": status,
        }

    candidate_targets, severity = _extract_context_for_remediation(diagnosis, context)
    prompt = _build_remediation_user_prompt(diagnosis, context, candidate_targets, severity)

    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            raw = llm.complete(REMEDIATION_SYSTEM_PROMPT, prompt)
            parsed = _parse_json(raw)
            result = validate_remediation(parsed, candidate_targets)
            result["severity"] = severity
            result["diagnosis_status"] = status
            return result
        except Exception as e:  # 网络 / 解析异常 -> 重试
            last_err = e
            time.sleep(min(0.1 * (attempt + 1), 1.0))

    return {
        "accepted": False,
        "reason": f"Tess 处置建议生成失败（{type(last_err).__name__}），已转人工",
        "proposal": None,
        "severity": severity,
        "diagnosis_status": status,
    }


# ---------------------------------------------------------------------------
# 执行器（可插拔，默认 Mock；LLM 永不接触）
# ---------------------------------------------------------------------------

class MockRemediationExecutor:
    """测试 / 本地开发用的假执行器。

    真实环境替换为 Teensing 平台 API 适配器（同样实现 run() 即可）。
    """

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list = []

    def run(self, action_type: str, target_id, params: dict) -> dict:
        self.calls.append(
            {"action_type": action_type, "target_id": target_id, "params": params}
        )
        if self.fail:
            return {"ok": False, "detail": f"模拟执行 {action_type} 失败"}
        return {"ok": True, "detail": f"已对 {target_id} 执行 {action_type}"}


# ---------------------------------------------------------------------------
# 审批流状态机（持久化）
# ---------------------------------------------------------------------------

class RemediationStore:
    """处置单存储 + 审批流状态机。

    状态迁移：
        PENDING --approve()--> APPROVED --execute()--> EXECUTED / FAILED
                 --reject()-->  REJECTED
    规则：只有 APPROVED 才允许 execute；PENDING 才能 approve/reject。
    默认纯内存；传入 persist_path 时以 JSONL 追加落盘（按 id 最后一条为准）。
    """

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self.persist_path = persist_path
        self._items: dict = {}
        self._seq = 0
        if persist_path and os.path.exists(persist_path):
            with open(persist_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    self._items[rec["id"]] = rec  # 后写覆盖，最终态正确
                    try:
                        n = int(str(rec["id"]).split("-")[-1])
                        self._seq = max(self._seq, n)
                    except Exception:
                        pass

    def _gen_id(self) -> str:
        self._seq += 1
        return f"RM-{self._seq:04d}"

    def _persist(self, rec: dict) -> None:
        if self.persist_path:
            with open(self.persist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def create(self, ref: str, proposal: dict, severity: str) -> dict:
        """登记一条 PENDING 处置单。proposal 应已通过 validate_remediation。"""
        rid = self._gen_id()
        rec = {
            "id": rid,
            "ref": ref,
            "created_at": time.time(),
            "severity": severity,
            "proposal": proposal,
            "state": REMEDIATION_PENDING,
            "approved_by": None,
            "second_approved_by": None,
            "rejected_by": None,
            "reject_reason": None,
            "executed_at": None,
            "outcome": None,
        }
        self._items[rid] = rec
        self._persist(rec)
        return rec

    def get(self, rid: str):
        return self._items.get(rid)

    def list(self, state: str = None):
        if state:
            return [r for r in self._items.values() if r["state"] == state]
        return list(self._items.values())

    def approve(self, rid: str, by: str, second_by: str = None) -> dict:
        rec = self._items.get(rid)
        if not rec:
            raise KeyError(f"未找到处置单 {rid}")
        if rec["state"] != REMEDIATION_PENDING:
            raise ValueError(f"处置单 {rid} 状态为 {rec['state']}，仅 PENDING 可审批")
        rec["state"] = REMEDIATION_APPROVED
        rec["approved_by"] = by
        rec["second_approved_by"] = second_by
        self._persist(rec)
        return rec

    def reject(self, rid: str, by: str, reason: str = None) -> dict:
        rec = self._items.get(rid)
        if not rec:
            raise KeyError(f"未找到处置单 {rid}")
        if rec["state"] != REMEDIATION_PENDING:
            raise ValueError(f"处置单 {rid} 状态为 {rec['state']}，仅 PENDING 可驳回")
        rec["state"] = REMEDIATION_REJECTED
        rec["rejected_by"] = by
        rec["reject_reason"] = reason
        self._persist(rec)
        return rec

    def execute(self, rid: str, executor) -> dict:
        rec = self._items.get(rid)
        if not rec:
            raise KeyError(f"未找到处置单 {rid}")
        if rec["state"] != REMEDIATION_APPROVED:
            raise ValueError(
                f"处置单 {rid} 未处于 APPROVED，禁止执行（当前 {rec['state']}）"
            )
        p = rec["proposal"]
        outcome = executor.run(p["action_type"], p["target_id"], p.get("params", {}))
        rec["outcome"] = outcome
        rec["state"] = REMEDIATION_EXECUTED if outcome.get("ok") else REMEDIATION_FAILED
        rec["executed_at"] = time.time()
        self._persist(rec)
        return rec
