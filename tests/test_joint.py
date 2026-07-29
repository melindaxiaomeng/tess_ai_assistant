"""L2-2 测试：联合归因（后端聚合 + 死锁 Gatekeeper + 编排 + 端点）。

覆盖：
- 后端聚合 correlate_events（共性维度计数 / 聚合损耗 / 最高严重度）
- Gatekeeper 幻觉屏障（joint_primary_factor 不在候选集 -> INCONCLUSIVE）
- Gatekeeper 危险字段物理熔断（severity 越权）
- 正常路径：三态归一 + contributing_event_ids 过滤
- 编排 run_joint_diagnosis（Mock LLM）死锁成立
- HTTP 端点 /tess/joint-diagnose
"""

from tess_backend.joint import correlate_events, run_joint_diagnosis
from tess_backend.gatekeeper import validate_joint_output
from tess_backend.tess_agent import MockLLMClient
from tess_backend.contracts import (
    STATUS_DIAGNOSED,
    STATUS_INCONCLUSIVE,
)

EV_A = {
    "anomaly_metadata": {
        "event_id": "E1", "severity": "HIGH",
        "calculated_loss": {"loss_per_hour_usd": 100.0},
    },
    "top_contributors": [
        {"dimension_type": "Publisher", "dimension_value": "Pub_X", "impact_share": "80%"}
    ],
}
EV_B = {
    "anomaly_metadata": {
        "event_id": "E2", "severity": "CRITICAL",
        "calculated_loss": {"loss_per_hour_usd": 400.0},
    },
    "top_contributors": [
        {"dimension_type": "Publisher", "dimension_value": "Pub_X", "impact_share": "85%"},
        {"dimension_type": "Region", "dimension_value": "US"},
    ],
}

JOINT_OK = {
    "status": "DIAGNOSED",
    "confidence": 0.92,
    "summary": "Pub_X 映射变更导致 E1/E2 共同收益缺口",
    "joint_primary_factor": "Pub_X",
    "contributing_event_ids": ["E1", "E2"],
    "root_cause_analysis": {
        "primary_factor": "Pub_X 变更",
        "causal_chain": ["变更", "超时", "双事件"],
    },
}


def _ctx():
    return correlate_events([EV_A, EV_B])


def test_correlate_events():
    ctx = _ctx()
    assert ctx["event_count"] == 2
    assert ctx["event_ids"] == ["E1", "E2"]
    assert ctx["candidate_dimensions"]["Pub_X"] == 2
    assert ctx["candidate_dimensions"]["US"] == 1
    assert ctx["aggregated_loss_per_hour_usd"] == 500.0
    assert ctx["max_severity"] == "CRITICAL"
    assert len(ctx["per_event"]) == 2


def test_validate_joint_hallucination():
    ctx = _ctx()
    bad = dict(JOINT_OK, joint_primary_factor="Ghost_Dim")
    out = validate_joint_output(bad, ctx)
    assert out["status"] == STATUS_INCONCLUSIVE
    assert out["confidence"] == 0.0


def test_validate_joint_dangerous_field_fused():
    ctx = _ctx()
    bad = dict(JOINT_OK, severity="CRITICAL")  # LLM 越权返回系统字段
    out = validate_joint_output(bad, ctx)
    assert out["status"] == STATUS_INCONCLUSIVE


def test_validate_joint_ok_and_filter_ids():
    ctx = _ctx()
    resp = dict(JOINT_OK, contributing_event_ids=["E1", "E2", "E9_GHOST"])
    out = validate_joint_output(resp, ctx)
    assert out["status"] == STATUS_DIAGNOSED
    assert out["joint_primary_factor"] == "Pub_X"
    # E9_GHOST 不在输入事件中，被过滤
    assert out["contributing_event_ids"] == ["E1", "E2"]


def test_run_joint_diagnosis_mock():
    result = run_joint_diagnosis([EV_A, EV_B], MockLLMClient(JOINT_OK))
    diag = result["diagnosis"]
    corr = result["correlation"]
    assert diag["status"] == STATUS_DIAGNOSED
    assert diag["joint_primary_factor"] == "Pub_X"
    # 死锁：LLM 绝不持有 severity / loss
    assert "severity" not in diag
    assert "calculated_loss" not in diag
    # 后端聚合正确透出
    assert corr["aggregated_loss_per_hour_usd"] == 500.0
    assert corr["max_severity"] == "CRITICAL"
    assert corr["event_count"] == 2


def test_joint_endpoint():
    from tess_backend import app as app_module
    from fastapi.testclient import TestClient

    # 直接打桩 _get_llm_client，不触达真实模型
    app_module._get_llm_client = lambda: MockLLMClient(JOINT_OK)
    c = TestClient(app_module.app)
    resp = c.post("/tess/joint-diagnose", json={"events": [EV_A, EV_B]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["diagnosis"]["status"] == STATUS_DIAGNOSED
    assert body["correlation"]["event_count"] == 2
