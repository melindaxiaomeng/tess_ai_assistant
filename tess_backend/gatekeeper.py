"""P2 校验网关（Gatekeeper）—— 三层死锁的「法典层」。

LLM 返回 JSON 后、推给前端之前，必须经过本层纯代码校验。

设计哲学：「对措辞宽容，对安全无情」
- 宽：模型用词偏差（如把 DIAGNOSED 写成近义词 CONFIRMED）、或多嘴一个无害字段，
       不再整次熔断——先做归一化 / 剪枝，保住本可救命的诊断。
- 严：severity / calculated_loss 等系统判定字段，LLM 绝对无权输出，
       一旦越权即物理熔断；幻觉维度 ID 同样物理降级。

已落地的全部修复（源自 5 轮评审）：
  - P0-1: jsonschema.validate 成功返回 None -> 用原对象 data = dict(llm_response_json)（浅拷贝，避免 mutate 调用方）
  - P0-2: 三态归一与 §3.2 UI 表自洽
  - 漏枚举: 三态 + 置信度阈值，与 §5 评分指南一致
  - 幻觉 ID: primary_contributor_id 必须存在于 input.top_contributors
  - 最后 P1: INCONCLUSIVE 三条路径一律清空 root_cause_analysis
  - (a) 剪枝式：危险字段物理锁死 + status 近义收敛 + 未知字段剪枝，不再因措辞偏差废掉好诊断
"""

import jsonschema

from .contracts import TESS_GATEKEEPER_SCHEMA, TESS_JOINT_GATEKEEPER_SCHEMA
from .contracts import (
    STATUS_INCONCLUSIVE,
    STATUS_DIAGNOSED,
    STATUS_DIAGNOSED_SUSPECT,
    VALID_STATUSES,
)
from .contracts import (
    ALLOWED_REMEDIATION_ACTIONS,
    DENIED_REMEDIATION_ACTIONS,
)
from .thresholds import ThresholdPolicy, default_policy

# ---------------------------------------------------------------------------
# 安全策略常量
# ---------------------------------------------------------------------------

# 危险字段：LLM 绝对无权输出，出现即视为安全违规 -> 硬熔断
_DANGEROUS_FIELDS = {"severity", "calculated_loss"}

# status 近义词收敛表：模型用词偏差时，收敛到合法枚举而非整体熔断
_STATUS_SYNONYMS = {
    "CONFIRMED": STATUS_DIAGNOSED,
    "YES": STATUS_DIAGNOSED,
    "TRUE": STATUS_DIAGNOSED,
    "SUCCESS": STATUS_DIAGNOSED,
    "RESOLVED": STATUS_DIAGNOSED,
    "ROOT_CAUSE_FOUND": STATUS_DIAGNOSED,
    "DIAGNOSED_YES": STATUS_DIAGNOSED,
    "UNKNOWN": STATUS_INCONCLUSIVE,
    "FAILED": STATUS_INCONCLUSIVE,
    "UNSURE": STATUS_INCONCLUSIVE,
    "INSUFFICIENT": STATUS_INCONCLUSIVE,
    "CANNOT_DETERMINE": STATUS_INCONCLUSIVE,
    "NO_ROOT_CAUSE": STATUS_INCONCLUSIVE,
}

# 允许保留的顶层字段（其余剪枝，避免模型「多嘴」废掉诊断）
_ALLOWED_TOP_LEVEL = {
    "status",
    "confidence",
    "summary",
    "primary_contributor_id",
    "root_cause_analysis",
}

# 联合归因允许保留的顶层字段（在单事件基础上增加 joint_*）
_ALLOWED_JOINT_TOP_LEVEL = {
    "status",
    "confidence",
    "summary",
    "joint_primary_factor",
    "contributing_event_ids",
    "root_cause_analysis",
}


def _derive_status_from_confidence(confidence: float, policy: ThresholdPolicy) -> str:
    """无法识别的 status：按置信度兜底推导，而非整体熔断。"""
    if confidence < policy.suspect_floor:
        return STATUS_INCONCLUSIVE
    if confidence < policy.high_threshold:
        return STATUS_DIAGNOSED_SUSPECT
    return STATUS_DIAGNOSED


def _fuse(message: str) -> dict:
    """物理熔断兜底：清空因果链，强转 INCONCLUSIVE。"""
    return {
        "status": STATUS_INCONCLUSIVE,
        "confidence": 0.0,
        "summary": message,
        "root_cause_analysis": {
            "primary_factor": "系统熔断：LLM 输出逻辑违规",
            "causal_chain": ["响应校验失败", "Gatekeeper 触发熔断", "转人工处理"],
        },
    }


def validate_tess_output(
    llm_response_json: dict, input_data: dict, policy: ThresholdPolicy = None
) -> dict:
    """Tess 后端死锁校验网关（剪枝式）。

    Args:
        llm_response_json: LLM 返回、待校验的字典。
        input_data:        算法层注入的 Input（含 top_contributors 供幻觉 ID 校验）。
        policy:            置信度切点策略；为 None 时使用默认初版阈值（不读盘）。
    Returns:
        归一化后的安全字典（status 为三态之一，INCONCLUSIVE 时 root_cause 已清空）。
    """
    if policy is None:
        policy = default_policy()
    try:
        if not isinstance(llm_response_json, dict):
            raise ValueError("LLM 返回非 JSON 对象")

        data = dict(llm_response_json)  # 浅拷贝，避免 mutate 调用方原对象

        # 0. 危险字段物理锁死：severity / calculated_loss 绝不允许来自 LLM
        violated = _DANGEROUS_FIELDS & set(data.keys())
        if violated:
            return _fuse(
                f"[系统熔断] LLM 越权返回了系统判定字段 {sorted(violated)}，已转人工"
            )

        # 1. 剪枝：去掉 LLM 多嘴的无害字段，保住诊断
        for key in list(data.keys()):
            if key not in _ALLOWED_TOP_LEVEL:
                del data[key]

        # 2. status 近义词收敛 -> 合法枚举；无法识别则按置信度兜底推导
        raw_status = str(data.get("status", "")).strip().upper()
        if raw_status in _STATUS_SYNONYMS:
            data["status"] = _STATUS_SYNONYMS[raw_status]
        elif raw_status in VALID_STATUSES:
            pass  # 已是合法枚举，保留
        else:
            data["status"] = _derive_status_from_confidence(
                data.get("confidence", 0.0), policy
            )

        # 3. 结构校验（宽松尺：不锁 additionalProperties，仅校验类型/必填/枚举）
        jsonschema.validate(instance=data, schema=TESS_GATEKEEPER_SCHEMA)

        # 4. LLM 主动认输 / 置信度极低 -> 降级（与另两分支一致：清空因果链）
        if data["status"] == STATUS_INCONCLUSIVE or data["confidence"] < policy.suspect_floor:
            data["status"] = STATUS_INCONCLUSIVE
            data["confidence"] = min(data["confidence"], policy.inconclusive_cap)
            data["root_cause_analysis"] = {
                "primary_factor": "暂无法明确根因",
                "causal_chain": [],
            }
            return data

        # 5. 幻觉 ID 校验（ID 层屏障）：LLM 返回的 ID 必须存在于算法候选集
        valid_ids = {c["dimension_value"] for c in input_data.get("top_contributors", [])}
        returned_id = data.get("primary_contributor_id")
        if returned_id and returned_id not in valid_ids:
            return {
                "status": STATUS_INCONCLUSIVE,
                "confidence": 0.0,
                "summary": f"[系统降级] Tess 归因匹配了不存在的维度 ({returned_id})，已转人工",
                "root_cause_analysis": {
                    "primary_factor": "维度匹配失败：存在幻觉 ID",
                    "causal_chain": ["LLM 返还未知维度 ID", "Gatekeeper 拦截降级", "转人工排查"],
                },
            }

        # 6. 状态与置信度归一化（统一推导为三态枚举，与 §3.2 表自洽）
        if policy.suspect_floor <= data["confidence"] < policy.high_threshold:
            data["status"] = STATUS_DIAGNOSED_SUSPECT  # 中置信：保留诊断，前端展示警惕态
        else:
            data["status"] = STATUS_DIAGNOSED          # 高置信 (>= high_threshold)

        return data

    except Exception:
        # 🛡️ 极端异常兜底熔断（与另两分支一致：清空因果链）
        return _fuse("Tess 输出未通过后端 Gatekeeper 安全校验，已自动切入人工排查。")


def validate_joint_output(
    llm_response_json: dict, joint_context: dict, policy: ThresholdPolicy = None
) -> dict:
    """联合归因（L2-2）的死锁校验网关，与单事件版同哲学。

    Args:
        llm_response_json: LLM 返回、待校验的联合诊断字典。
        joint_context:      后端 correlate_events() 产出的相关性上下文，至少含
                            candidate_dimensions（集合/字典）与 event_ids（列表），
                            用于幻觉屏障校验。
        policy:            置信度切点策略；为 None 时用默认初版阈值。
    Returns:
        归一化后的安全联合诊断字典。
    """
    if policy is None:
        policy = default_policy()
    candidate_dims = set(joint_context.get("candidate_dimensions", {}).keys()) \
        if isinstance(joint_context.get("candidate_dimensions"), dict) \
        else set(joint_context.get("candidate_dimensions", []))
    valid_event_ids = set(joint_context.get("event_ids", []))
    try:
        if not isinstance(llm_response_json, dict):
            raise ValueError("LLM 返回非 JSON 对象")

        data = dict(llm_response_json)  # 浅拷贝，避免 mutate 调用方原对象

        # 0. 危险字段物理锁死：severity / calculated_loss 绝不允许来自 LLM
        violated = _DANGEROUS_FIELDS & set(data.keys())
        if violated:
            return _fuse(
                f"[系统熔断] LLM 越权返回了系统判定字段 {sorted(violated)}，已转人工"
            )

        # 1. 剪枝：去掉 LLM 多嘴的无害字段，保住联合诊断
        for key in list(data.keys()):
            if key not in _ALLOWED_JOINT_TOP_LEVEL:
                del data[key]

        # 2. status 近义词收敛 -> 合法枚举；无法识别则按置信度兜底推导
        raw_status = str(data.get("status", "")).strip().upper()
        if raw_status in _STATUS_SYNONYMS:
            data["status"] = _STATUS_SYNONYMS[raw_status]
        elif raw_status in VALID_STATUSES:
            pass
        else:
            data["status"] = _derive_status_from_confidence(
                data.get("confidence", 0.0), policy
            )

        # 3. 结构校验（宽松尺）
        jsonschema.validate(instance=data, schema=TESS_JOINT_GATEKEEPER_SCHEMA)

        # 4. LLM 主动认输 / 置信度极低 -> 降级（清空因果链）
        if data["status"] == STATUS_INCONCLUSIVE or data["confidence"] < policy.suspect_floor:
            data["status"] = STATUS_INCONCLUSIVE
            data["confidence"] = min(data["confidence"], policy.inconclusive_cap)
            data["root_cause_analysis"] = {
                "primary_factor": "暂无法明确共同根因",
                "causal_chain": [],
            }
            return data

        # 5. 幻觉屏障：joint_primary_factor 必须存在于后端候选维度集
        returned_factor = data.get("joint_primary_factor")
        if returned_factor and returned_factor not in candidate_dims:
            return {
                "status": STATUS_INCONCLUSIVE,
                "confidence": 0.0,
                "summary": f"[系统降级] 联合归因匹配了不存在的共性维度 ({returned_factor})，已转人工",
                "root_cause_analysis": {
                    "primary_factor": "共性维度匹配失败：存在幻觉 ID",
                    "causal_chain": ["LLM 返还未知共性维度", "Gatekeeper 拦截降级", "转人工排查"],
                },
            }

        # 5b. contributing_event_ids 必须是指定事件的子集，过滤掉未知事件
        raw_ids = data.get("contributing_event_ids") or []
        data["contributing_event_ids"] = [eid for eid in raw_ids if eid in valid_event_ids]

        # 6. 状态与置信度归一化（与 §3.2 三态表自洽）
        if policy.suspect_floor <= data["confidence"] < policy.high_threshold:
            data["status"] = STATUS_DIAGNOSED_SUSPECT
        else:
            data["status"] = STATUS_DIAGNOSED

        return data

    except Exception:
        return _fuse("Tess 联合归因未通过后端 Gatekeeper 安全校验，已自动切入人工排查。")

# ---------------------------------------------------------------------------
# L2-3 处置提案死锁校验（半自动处置带审批流）
# ---------------------------------------------------------------------------

# 处置提案允许的顶层字段（其余剪枝，避免模型「多嘴」废掉提案）
_ALLOWED_REMEDIATION_TOP_LEVEL = {
    "action_type",
    "target_id",
    "params",
    "rationale",
}

def _validate_param_rules(action_type: str, params: dict):
    """按白名单规则校验处置参数，返回 (ok, reason)。"""
    rules = ALLOWED_REMEDIATION_ACTIONS[action_type].get("param_rules", {})
    for pname, rule in rules.items():
        if rule.get("required") and pname not in params:
            return False, f"缺少必填参数 {pname}"
        if pname not in params:
            continue
        val = params[pname]
        if rule["type"] == "int":
            if isinstance(val, bool) or not isinstance(val, int):
                return False, f"{pname} 必须为整数"
            if "min" in rule and val < rule["min"]:
                return False, f"{pname} 必须 >= {rule['min']}"
            if "max" in rule and val > rule["max"]:
                return False, f"{pname} 必须 <= {rule['max']}"
        elif rule["type"] == "float":
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return False, f"{pname} 必须为数字"
            if "min" in rule and val < rule["min"]:
                return False, f"{pname} 必须 >= {rule['min']}"
        elif rule["type"] == "str":
            if not isinstance(val, str) or not val.strip():
                return False, f"{pname} 必须为非空字符串"
    return True, ""

def validate_remediation(proposal: dict, candidate_targets: set) -> dict:
    """Tess 处置提案死锁校验（与归因同哲学）。

    关键安全点：
    - 黑名单动作（如 DELETE_ACCOUNT）物理拒绝，纵深防御。
    - action_type 必须在白名单（LLM 无法发明动作）。
    - target_id 必须在候选集（防幻觉：不能对不存在的对象处置）。
    - NOTIFY_ONCALL 的 target_kind="any"，允许 null（仅告警不改动）。
    - 参数按白名单规则校验。
    Returns:
        {"accepted": bool, "reason": str, "proposal": normalized|None}
    """
    try:
        if not isinstance(proposal, dict):
            raise ValueError("处置提案非 JSON 对象")

        data = dict(proposal)  # 浅拷贝，避免 mutate 调用方
        # 1. 剪枝：去掉 LLM 多嘴的无害字段
        for key in list(data.keys()):
            if key not in _ALLOWED_REMEDIATION_TOP_LEVEL:
                del data[key]

        action_type = str(data.get("action_type", "")).strip().upper()
        target_id = data.get("target_id")
        params = data.get("params")
        if not isinstance(params, dict):
            params = {}

        # 2. 黑名单物理拒绝
        if action_type in DENIED_REMEDIATION_ACTIONS:
            return {
                "accepted": False,
                "reason": f"[系统拒绝] 动作 {action_type} 在禁用黑名单中，禁止自动处置",
                "proposal": None,
            }

        # 3. 白名单校验
        if action_type not in ALLOWED_REMEDIATION_ACTIONS:
            return {
                "accepted": False,
                "reason": (
                    f"[系统拒绝] 未知处置动作 {action_type}，"
                    f"仅允许 {sorted(ALLOWED_REMEDIATION_ACTIONS)}"
                ),
                "proposal": None,
            }

        # 4. 目标防幻觉校验
        target_kind = ALLOWED_REMEDIATION_ACTIONS[action_type]["target_kind"]
        if target_kind != "any":
            if not target_id or target_id not in candidate_targets:
                return {
                    "accepted": False,
                    "reason": (
                        f"[系统拒绝] 处置目标 {target_id!r} 不在候选集 "
                        f"{sorted(candidate_targets)}，疑似幻觉"
                    ),
                    "proposal": None,
                }

        # 5. 参数校验
        ok, reason = _validate_param_rules(action_type, params)
        if not ok:
            return {"accepted": False, "reason": f"[参数非法] {reason}", "proposal": None}

        # 6. 归一化输出
        normalized = {
            "action_type": action_type,
            "target_id": target_id,
            "params": params,
            "rationale": str(data.get("rationale", "")),
            "target_kind": target_kind,
        }
        return {"accepted": True, "reason": "ok", "proposal": normalized}

    except Exception as e:
        return {
            "accepted": False,
            "reason": f"[系统异常] {type(e).__name__}: {e}",
            "proposal": None,
        }
