"""P0 契约层 —— Tess 前后端共用数据结构定义。

两把「尺子」：
- TESS_OUTPUT_SCHEMA  ：对外发布的「理想契约」（前端 / LLM 期望规格），
  开启 additionalProperties: false，作为文档与前端对照的权威来源。
- TESS_GATEKEEPER_SCHEMA：Gatekeeper 内部校验用的「宽松尺」，不锁 additionalProperties。
  Gatekeeper 先手动剪枝未知字段 + 死锁危险字段（severity / caculated_loss），
  再归一化 status，最后用这把尺做结构校验。
  设计哲学：「对措辞宽容，对安全无情」——模型用词偏差（如 CONFIRMED）不再废掉好诊断，
  但越权返回 severity 等行为仍物理熔断。
"""

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# LLM 输出的三态（与 §3.2 UI 表、§6 Gatekeeper 归一化严格一致）
STATUS_DIAGNOSED = "DIAGNOSED"
STATUS_DIAGNOSED_SUSPECT = "DIAGNOSED_SUSPECT"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"

VALID_STATUSES = (
    STATUS_DIAGNOSED,
    STATUS_DIAGNOSED_SUSPECT,
    STATUS_INCONCLUSIVE,
)

# 算法层算出的严重度（LLM 不得生成，只能消费）
SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"
SEVERITY_UNKNOWN = "UNKNOWN"

VALID_SEVERITIES = (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_LOW,
    SEVERITY_UNKNOWN,
)

# 置信度阈值（与 §3.2 / §5 评分指南一致）
CONFIDENCE_HIGH_THRESHOLD = 0.85   # >= 0.85 -> DIAGNOSED
CONFIDENCE_SUSPECT_FLOOR = 0.60   # [0.60, 0.85) -> DIAGNOSED_SUSPECT；< 0.60 -> INCONCLUSIVE
# INCONCLUSIVE 时把置信度钳制到此上限，避免与「高/中置信」语义冲突
INCONCLUSIVE_CONFIDENCE_CAP = 0.59


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# LLM 输出 Schema（法典）。LLM 只允许产出：
#   status(三态) / confidence / summary / primary_contributor_id(可 null) / root_cause_analysis
# 任何其它字段（severity、loss、calculated_* 等）直接 ValidationError -> 熔断。
TESS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": list(VALID_STATUSES),
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string"},
        "primary_contributor_id": {"type": ["string", "null"]},
        "root_cause_analysis": {
            "type": "object",
            "properties": {
                "primary_factor": {"type": "string"},
                "causal_chain": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["primary_factor", "causal_chain"],
            "additionalProperties": False,
        },
    },
    "required": ["status", "confidence", "summary", "root_cause_analysis"],
    "additionalProperties": False,  # 杜绝 LLM 返回任何未经授权的字段（如 severity）
}

# Gatekeeper 内部校验用的「宽松尺」：与 TESS_OUTPUT_SCHEMA 同结构，但
# 不锁 additionalProperties —— 未知字段已由 Gatekeeper 在校验前手动剪枝。
# 这样模型「多嘴」一个无害字段不会整次诊断被熔断。
TESS_GATEKEEPER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": list(VALID_STATUSES),
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string"},
        "primary_contributor_id": {"type": ["string", "null"]},
        "root_cause_analysis": {
            "type": "object",
            "properties": {
                "primary_factor": {"type": "string"},
                "causal_chain": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["primary_factor", "causal_chain"],
        },
    },
    "required": ["status", "confidence", "summary", "root_cause_analysis"],
}

# 联合归因（L2-2）输出 Schema（理想契约，锁字段）。
# LLM 只允许产出：
#   status(三态) / confidence / summary / joint_primary_factor(可 null) /
#   contributing_event_ids / root_cause_analysis
# severity / calculated_loss / 聚合损耗 仍由后端规则层算，LLM 不得生成。
TESS_JOINT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": list(VALID_STATUSES),
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string"},
        "joint_primary_factor": {"type": ["string", "null"]},
        "contributing_event_ids": {"type": "array", "items": {"type": "string"}},
        "root_cause_analysis": {
            "type": "object",
            "properties": {
                "primary_factor": {"type": "string"},
                "causal_chain": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["primary_factor", "causal_chain"],
            "additionalProperties": False,
        },
    },
    "required": ["status", "confidence", "summary", "root_cause_analysis"],
    "additionalProperties": False,  # 杜绝 LLM 返回 severity / loss 等
}

# 联合归因 Gatekeeper 内部校验用的「宽松尺」：同结构，不锁 additionalProperties。
TESS_JOINT_GATEKEEPER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": list(VALID_STATUSES),
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string"},
        "joint_primary_factor": {"type": ["string", "null"]},
        "contributing_event_ids": {"type": "array", "items": {"type": "string"}},
        "root_cause_analysis": {
            "type": "object",
            "properties": {
                "primary_factor": {"type": "string"},
                "causal_chain": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["primary_factor", "causal_chain"],
        },
    },
    "required": ["status", "confidence", "summary", "root_cause_analysis"],
}

# 算法层 / 规则引擎注入给 LLM 的 Input Schema（供后端自检 Input 合法性）。
# 注意：severity 与 calculated_loss 必须由算法层算好，LLM 只消费。
TESS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "anomaly_metadata": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "trigger_time": {"type": "string"},
                "target_metric": {"type": "string"},
                "current_value": {"type": "string"},
                "benchmark_value": {"type": "string"},
                "severity": {"type": "string", "enum": list(VALID_SEVERITIES)},
                "calculated_loss": {
                    "type": "object",
                    "properties": {
                        "loss_per_hour_usd": {"type": "number", "minimum": 0.0},
                        "calculation_basis": {"type": "string"},
                    },
                    "required": ["loss_per_hour_usd"],
                    "additionalProperties": False,
                },
            },
            "required": ["event_id", "severity", "calculated_loss"],
            "additionalProperties": False,
        },
        "top_contributors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dimension_type": {"type": "string"},
                    "dimension_value": {"type": "string"},
                    "impact_share": {"type": "string"},
                    "metric_change": {"type": "string"},
                },
                "required": ["dimension_type", "dimension_value"],
                "additionalProperties": False,
            },
        },
        "associated_signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "status": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["source", "status"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["anomaly_metadata", "top_contributors"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# L2-3 半自动处置（带审批流）常量
# ---------------------------------------------------------------------------
# 处置动作白名单：LLM Prompt 与 Gatekeeper 共用的「唯一权威来源」。
# 任何不在白名单的动作，Gatekeeper 一律拒绝；LLM 无法凭空发明动作。
# 每项：desc=人类可读说明，target_kind=目标维度类型，param_rules=参数校验规则。
ALLOWED_REMEDIATION_ACTIONS = {
    "PAUSE_PUBLISHER": {
        "desc": "暂停指定 Publisher 投放",
        "target_kind": "Publisher",
        "param_rules": {"duration_minutes": {"type": "int", "min": 1, "max": 1440}},
    },
    "ADJUST_BID": {
        "desc": "下调/调整出价",
        "target_kind": "Publisher|Campaign",
        "param_rules": {"new_bid": {"type": "float", "min": 0.0}},
    },
    "ROLLBACK_CONFIG": {
        "desc": "回滚最近一次配置变更",
        "target_kind": "ConfigId",
        "param_rules": {},
    },
    "REROUTE_TRAFFIC": {
        "desc": "将流量切换至备用路由/区域",
        "target_kind": "Region",
        "param_rules": {"to": {"type": "str", "required": True}},
    },
    "NOTIFY_ONCALL": {
        "desc": "通知值班（不改动业务，仅告警）",
        "target_kind": "any",
        "param_rules": {"channel": {"type": "str", "required": False}},
    },
}

# 高危动作黑名单：无论 LLM 如何措辞，一律物理拒绝（纵深防御）。
DENIED_REMEDIATION_ACTIONS = {
    "DELETE_ACCOUNT",
    "PURGE_DATA",
    "DISABLE_SAFETY",
    "FULL_SHUTDOWN",
    "DROP_DATABASE",
    "REVOKE_ALL_KEYS",
}

# LLM 输出的处置提案 Schema（宽松尺：Gatekeeper 会先剪枝再校验）。
REMEDIATION_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "action_type": {"type": "string"},
        "target_id": {"type": ["string", "null"]},
        "params": {"type": "object"},
        "rationale": {"type": "string"},
    },
    "required": ["action_type", "rationale"],
}

# 处置工作流状态机（审批流核心）
REMEDIATION_PENDING = "PENDING"        # 已提案，待人工审批
REMEDIATION_APPROVED = "APPROVED"      # 已审批通过，待执行
REMEDIATION_REJECTED = "REJECTED"      # 人工驳回
REMEDIATION_EXECUTED = "EXECUTED"      # 执行成功
REMEDIATION_FAILED = "FAILED"          # 执行失败（仍记录，便于复盘）
REMEDIATION_EXPIRED = "EXPIRED"        # 超时未审批（可选）
VALID_REMEDIATION_STATES = (
    REMEDIATION_PENDING,
    REMEDIATION_APPROVED,
    REMEDIATION_REJECTED,
    REMEDIATION_EXECUTED,
    REMEDIATION_FAILED,
    REMEDIATION_EXPIRED,
)
