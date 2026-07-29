"""P2 Gatekeeper 单测：三态归一、幻觉 ID、severity 越权熔断、INCONCLUSIVE 三路径一致清空。"""

import pytest

from tess_backend.gatekeeper import validate_tess_output
from tess_backend.contracts import (
    STATUS_DIAGNOSED,
    STATUS_DIAGNOSED_SUSPECT,
    STATUS_INCONCLUSIVE,
)


# 算法层注入的 Input（供幻觉 ID 校验的候选集）
INPUT = {
    "anomaly_metadata": {
        "event_id": "ERR-20260728-0912",
        "severity": "HIGH",
        "calculated_loss": {"loss_per_hour_usd": 350.0},
    },
    "top_contributors": [
        {"dimension_type": "Publisher", "dimension_value": "Pub_Media_802", "impact_share": "82%"},
    ],
    "associated_signals": [],
}


def _valid_llm(confidence, status="DIAGNOSED", pid="Pub_Media_802", chain=None):
    return {
        "status": status,
        "confidence": confidence,
        "summary": "Pub_Media_802 映射变更叠加回调超时导致收益缺失",
        "primary_contributor_id": pid,
        "root_cause_analysis": {
            "primary_factor": "映射规则变更",
            "causal_chain": chain or ["变更配置", "API 超时", "收益缺失"],
        },
    }


def test_high_confidence_normalized_to_diagnosed():
    out = validate_tess_output(_valid_llm(0.92), INPUT)
    assert out["status"] == STATUS_DIAGNOSED
    assert out["confidence"] == 0.92
    # 因果链原样保留
    assert len(out["root_cause_analysis"]["causal_chain"]) == 3


def test_mid_confidence_normalized_to_suspect():
    # 0.60 <= C < 0.85 -> DIAGNOSED_SUSPECT（保留诊断，不转人工）
    out = validate_tess_output(_valid_llm(0.70), INPUT)
    assert out["status"] == STATUS_DIAGNOSED_SUSPECT
    assert len(out["root_cause_analysis"]["causal_chain"]) == 3


def test_inconclusive_early_return_clears_root_cause():
    # 最后 P1 修复点：LLM 主动认输 / confidence<0.60 的早返回分支，
    # 必须把 root_cause 清空，否则「无法确定」卡片仍会渲染推测因果链。
    llm = _valid_llm(0.40, status=STATUS_INCONCLUSIVE)
    out = validate_tess_output(llm, INPUT)
    assert out["status"] == STATUS_INCONCLUSIVE
    assert out["confidence"] <= 0.59
    assert out["root_cause_analysis"]["primary_factor"] == "暂无法明确根因"
    assert out["root_cause_analysis"]["causal_chain"] == []


def test_inconclusive_low_confidence_clears_root_cause():
    # confidence < 0.60 而非显式 INCONCLUSIVE，同样清空
    out = validate_tess_output(_valid_llm(0.55), INPUT)
    assert out["status"] == STATUS_INCONCLUSIVE
    assert out["root_cause_analysis"]["causal_chain"] == []


def test_hallucination_id_triggers_circuit_break():
    # 返回了不存在的维度 ID -> 幻觉，物理降级
    out = validate_tess_output(_valid_llm(0.95, pid="Pub_Evil_999"), INPUT)
    assert out["status"] == STATUS_INCONCLUSIVE
    assert out["confidence"] == 0.0
    assert "不存在的维度" in out["summary"]
    assert out["root_cause_analysis"]["causal_chain"] == [
        "LLM 返还未知维度 ID", "Gatekeeper 拦截降级", "转人工排查",
    ]


def test_severity_injection_triggers_circuit_break():
    # LLM 越权返回 severity 字段 -> 危险字段物理锁死 -> 熔断 INCONCLUSIVE
    # （剪枝式 Gatekeeper 仍保留对 severity/calculated_loss 的无情熔断）
    llm = _valid_llm(0.95)
    llm["severity"] = "CRITICAL"  # 越权字段
    out = validate_tess_output(llm, INPUT)
    assert out["status"] == STATUS_INCONCLUSIVE
    assert out["confidence"] == 0.0
    assert "熔断" in out["root_cause_analysis"]["primary_factor"]


def test_synonym_status_confirmed_preserved():
    # (a) 剪枝式：模型把 DIAGNOSED 写成近义词 CONFIRMED，
    # 不应被整体熔断，而应收敛为 DIAGNOSED 并保留诊断。
    out = validate_tess_output(_valid_llm(0.95, status="CONFIRMED"), INPUT)
    assert out["status"] == STATUS_DIAGNOSED
    assert out["confidence"] == 0.95
    assert len(out["root_cause_analysis"]["causal_chain"]) == 3  # 因果链保住


def test_unknown_status_derived_from_confidence():
    # (a) 完全无法识别的 status：按置信度兜底推导，而非废掉诊断。
    out = validate_tess_output(_valid_llm(0.90, status="MAYBE_WEIRD"), INPUT)
    assert out["status"] == STATUS_DIAGNOSED
    out_low = validate_tess_output(_valid_llm(0.70, status="SOMETHING_ODD"), INPUT)
    assert out_low["status"] == STATUS_DIAGNOSED_SUSPECT


def test_harmless_extra_field_pruned_not_fused():
    # (a) 模型多嘴一个无害字段（如 note），应被剪枝而非整体熔断。
    llm = _valid_llm(0.92)
    llm["note"] = "这是模型顺手加的无关备注"
    llm["debug_info"] = {"tokens": 123}
    out = validate_tess_output(llm, INPUT)
    assert out["status"] == STATUS_DIAGNOSED
    assert "note" not in out          # 已剪枝
    assert "debug_info" not in out    # 已剪枝
    assert len(out["root_cause_analysis"]["causal_chain"]) == 3  # 诊断保住


def test_none_bug_regression():
    # P0-1 回归：jsonschema.validate 成功返回 None，必须用原对象而非 None。
    # 一个完全合法的输入不能被「反向熔断」成 INCONCLUSIVE。
    out = validate_tess_output(_valid_llm(0.90), INPUT)
    assert out["status"] == STATUS_DIAGNOSED  # 关键：没有被废掉
    assert out["confidence"] == 0.90


def test_primary_contributor_id_null_allowed():
    # primary_contributor_id 允许为 null（确实无法确定主因维度）
    out = validate_tess_output(_valid_llm(0.90, pid=None), INPUT)
    assert out["status"] == STATUS_DIAGNOSED  # 不应因 null 触发整响应熔断


def test_malformed_output_circuit_breaks():
    # 完全非法的输出（缺字段 / 类型错）-> 兜底熔断
    out = validate_tess_output({"foo": "bar"}, INPUT)
    assert out["status"] == STATUS_INCONCLUSIVE
    assert out["confidence"] == 0.0
