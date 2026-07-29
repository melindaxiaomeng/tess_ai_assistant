"""L2-3 半自动处置（带审批流）测试。

覆盖：Gatekeeper 提案校验（黑名单/白名单/幻觉目标/参数/notify-null/有效）、
propose_remediation（INCONCLUSIVE 短路 + 有效提案）、RemediationStore 状态机
+ 持久化、以及 API 端到端审批流（含 CRITICAL 双人规则、未审批禁止执行）。
"""

import os
import tempfile

from tess_backend import contracts as C
from tess_backend.gatekeeper import validate_remediation
from tess_backend.remediation import (
    propose_remediation,
    RemediationStore,
    MockRemediationExecutor,
)
from tess_backend.tess_agent import MockLLMClient
from tess_backend import app as app_module
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# 1) Gatekeeper 提案校验
# ---------------------------------------------------------------------------

def test_denied_action_rejected():
    p = {"action_type": "DELETE_ACCOUNT", "target_id": "x", "rationale": "bad"}
    r = validate_remediation(p, {"x"})
    assert r["accepted"] is False
    assert "黑名单" in r["reason"]


def test_unknown_action_rejected():
    p = {"action_type": "LAUNCH_NUKES", "target_id": "x", "rationale": "bad"}
    r = validate_remediation(p, {"x"})
    assert r["accepted"] is False
    assert "未知" in r["reason"]


def test_hallucinated_target_rejected():
    p = {"action_type": "PAUSE_PUBLISHER", "target_id": "GhostPub", "rationale": "x", "params": {"duration_minutes": 30}}
    r = validate_remediation(p, {"Pub_X"})
    assert r["accepted"] is False
    assert "幻觉" in r["reason"]


def test_bad_param_rejected():
    p = {"action_type": "PAUSE_PUBLISHER", "target_id": "Pub_X", "rationale": "x", "params": {"duration_minutes": -5}}
    r = validate_remediation(p, {"Pub_X"})
    assert r["accepted"] is False
    assert "参数" in r["reason"]


def test_required_param_missing_rejected():
    p = {"action_type": "REROUTE_TRAFFIC", "target_id": "US", "rationale": "x", "params": {}}
    r = validate_remediation(p, {"US"})
    assert r["accepted"] is False
    assert "to" in r["reason"]


def test_notify_any_target_null_accepted():
    p = {"action_type": "NOTIFY_ONCALL", "target_id": None, "rationale": "x", "params": {"channel": "pager"}}
    r = validate_remediation(p, set())
    assert r["accepted"] is True
    assert r["proposal"]["action_type"] == "NOTIFY_ONCALL"


def test_valid_pause_accepted_and_normalized():
    p = {"action_type": "pause_publisher", "target_id": "Pub_X", "rationale": "转化缺失", "params": {"duration_minutes": 60}, "junk": 1}
    r = validate_remediation(p, {"Pub_X"})
    assert r["accepted"] is True
    # 剪枝 + 大写归一
    assert "junk" not in r["proposal"]
    assert r["proposal"]["action_type"] == "PAUSE_PUBLISHER"
    assert r["proposal"]["target_kind"] == "Publisher"


# ---------------------------------------------------------------------------
# 2) propose_remediation
# ---------------------------------------------------------------------------

_DIAG = {
    "status": "DIAGNOSED",
    "confidence": 0.92,
    "primary_contributor_id": "Pub_X",
    "summary": "Pub_X 映射变更",
    "root_cause_analysis": {"primary_factor": "p", "causal_chain": []},
}
_CTX = {
    "anomaly_metadata": {"event_id": "E1", "severity": "HIGH"},
    "top_contributors": [{"dimension_type": "Publisher", "dimension_value": "Pub_X", "impact_share": "80%"}],
}

def test_propose_inconclusive_short_circuits():
    inc = dict(_DIAG, status="INCONCLUSIVE", confidence=0.4)
    r = propose_remediation(inc, _CTX, MockLLMClient({"action_type": "PAUSE_PUBLISHER"}))
    assert r["accepted"] is False
    assert "不确定" in r["reason"]


def test_propose_valid_creates_normalized():
    llm_out = {
        "action_type": "PAUSE_PUBLISHER",
        "target_id": "Pub_X",
        "params": {"duration_minutes": 120},
        "rationale": "暂停以止血",
    }
    r = propose_remediation(_DIAG, _CTX, MockLLMClient(llm_out))
    assert r["accepted"] is True
    assert r["severity"] == "HIGH"
    assert r["proposal"]["action_type"] == "PAUSE_PUBLISHER"


# ---------------------------------------------------------------------------
# 3) RemediationStore 状态机 + 持久化
# ---------------------------------------------------------------------------

def test_store_state_machine_and_persistence():
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    tmp = tf.name
    tf.close()
    try:
        store = RemediationStore(persist_path=tmp)
        rec = store.create("diag:Pub_X", {"action_type": "PAUSE_PUBLISHER", "target_id": "Pub_X", "params": {}, "rationale": "x", "target_kind": "Publisher"}, "HIGH")
        rid = rec["id"]
        assert rec["state"] == C.REMEDIATION_PENDING

        # 不能执行未审批
        class Boom:
            def run(self, *a): raise AssertionError("不应被调用")
        try:
            store.execute(rid, Boom())
            assert False, "未审批却执行了"
        except ValueError:
            pass

        store.approve(rid, "alice")
        assert store.get(rid)["state"] == C.REMEDIATION_APPROVED

        ex = MockRemediationExecutor()
        store.execute(rid, ex)
        assert store.get(rid)["state"] == C.REMEDIATION_EXECUTED
        assert ex.calls[0]["action_type"] == "PAUSE_PUBLISHER"

        # 驳回路径（新单）
        r2 = store.create("diag:Pub_Y", {"action_type": "NOTIFY_ONCALL", "target_id": None, "params": {}, "rationale": "x", "target_kind": "any"}, "LOW")
        store.reject(r2["id"], "bob", "风险可接受")
        assert store.get(r2["id"])["state"] == C.REMEDIATION_REJECTED

        # 持久化：重开 store 应恢复最终态
        reopened = RemediationStore(persist_path=tmp)
        assert reopened.get(rid)["state"] == C.REMEDIATION_EXECUTED
        assert reopened.get(r2["id"])["state"] == C.REMEDIATION_REJECTED
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------
# 4) API 端到端审批流
# ---------------------------------------------------------------------------

def _client():
    app_module.REMEDIATION_STORE = RemediationStore()  # 隔离，避免跨测试污染
    app_module.REMEDIATION_EXECUTOR = MockRemediationExecutor()
    app_module._get_llm_client = lambda: MockLLMClient({
        "action_type": "PAUSE_PUBLISHER",
        "target_id": "Pub_X",
        "params": {"duration_minutes": 120},
        "rationale": "暂停以止血",
    })
    return TestClient(app_module.app)


def test_api_full_approval_flow():
    c = _client()
    # 提案
    resp = c.post("/tess/remediation/propose", json={"diagnosis": _DIAG, "context": _CTX})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    rid = body["remediation"]["id"]
    assert body["remediation"]["state"] == "PENDING"

    # 未审批执行 -> 409
    r = c.post(f"/tess/remediation/{rid}/execute")
    assert r.status_code == 409

    # 审批
    r = c.post(f"/tess/remediation/{rid}/approve", json={"approved_by": "alice"})
    assert r.status_code == 200
    assert r.json()["state"] == "APPROVED"

    # 执行
    r = c.post(f"/tess/remediation/{rid}/execute")
    assert r.status_code == 200
    j = r.json()
    assert j["state"] == "EXECUTED"
    assert j["outcome"]["ok"] is True


def test_api_inconclusive_no_proposal():
    c = _client()
    inc = dict(_DIAG, status="INCONCLUSIVE", confidence=0.4)
    r = c.post("/tess/remediation/propose", json={"diagnosis": inc, "context": _CTX})
    assert r.status_code == 200
    assert r.json()["accepted"] is False


def test_api_critical_requires_two_approvers():
    c = _client()
    crit_diag = dict(_DIAG, primary_contributor_id="Pub_X")
    crit_ctx = {
        "anomaly_metadata": {"event_id": "E9", "severity": "CRITICAL"},
        "top_contributors": [{"dimension_type": "Publisher", "dimension_value": "Pub_X", "impact_share": "90%"}],
    }
    body = c.post("/tess/remediation/propose", json={"diagnosis": crit_diag, "context": crit_ctx}).json()
    rid = body["remediation"]["id"]

    # 单人审批 -> 422
    r = c.post(f"/tess/remediation/{rid}/approve", json={"approved_by": "alice"})
    assert r.status_code == 422

    # 双人审批 -> 成功
    r = c.post(f"/tess/remediation/{rid}/approve", json={"approved_by": "alice", "second_approved_by": "bob"})
    assert r.status_code == 200
    assert r.json()["second_approved_by"] == "bob"
