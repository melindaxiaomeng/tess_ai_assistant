"""P5 · 数据接入层单测 + 端点集成测试。

- MockDataConnector 返回样例；normalize_to_context 正确映射 PRD §4.1 Context。
- /tess/diagnose-from-source：mock connector + MockLLMClient 端到端跑通。
不触达真实 Teensing API / 真实 LLM。
"""

import pytest

from tess_backend.data_connector import (
    MockDataConnector,
    TeensingDataConnector,
    get_data_connector,
    normalize_to_context,
)
from tess_backend import app as app_module
from tess_backend.tess_agent import MockLLMClient
from tess_backend.contracts import STATUS_DIAGNOSED
from tess_backend.feedback import FeedbackStore


def _mock_response(conf=0.92):
    return {
        "status": STATUS_DIAGNOSED,
        "confidence": conf,
        "summary": "Pub_Media_802 映射变更叠加第三方回调超时导致收益缺失",
        "primary_contributor_id": "Pub_Media_802",
        "root_cause_analysis": {
            "primary_factor": "映射规则变更 + 回调超时",
            "causal_chain": ["运营变更配置", "API 超时", "转化数据缺失", "毛利暴跌"],
        },
    }


def test_mock_connector_returns_samples():
    c = MockDataConnector()
    events = c.fetch_recent_anomalies(limit=10)
    assert len(events) >= 1
    assert events[0]["event_id"] == "ERR-20260728-0912"


def test_normalize_to_context_maps_fields():
    raw = MockDataConnector().fetch_recent_anomalies(1)[0]
    ctx = normalize_to_context(raw)
    meta = ctx["anomaly_metadata"]
    assert meta["event_id"] == "ERR-20260728-0912"
    assert meta["current_value"] == "3.8%"
    assert meta["severity"] == "HIGH"
    assert ctx["top_contributors"][0]["dimension_value"] == "Pub_Media_802"
    assert ctx["associated_signals"][0]["source"] == "AppsFlyer_Pull_API"


def test_get_data_connector_default_is_mock(monkeypatch):
    monkeypatch.delenv("TESS_DATA_CONNECTOR", raising=False)
    c = get_data_connector()
    assert isinstance(c, MockDataConnector)


def test_get_data_connector_teensing_unconfigured(monkeypatch):
    monkeypatch.setenv("TESS_DATA_CONNECTOR", "teensing")
    monkeypatch.delenv("TESS_DATA_API_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        get_data_connector()


@pytest.fixture
def client(monkeypatch):
    # LLM 用 Mock，不触真实模型
    monkeypatch.setattr(
        app_module, "_get_llm_client", lambda: MockLLMClient(_mock_response(0.92))
    )
    # 数据接入层用 Mock connector，避免触真实 Teensing
    from tess_backend.data_connector import MockDataConnector as MC

    monkeypatch.setattr(app_module, "_DATA_CONNECTOR", MC())
    # 隔离反馈单例，避免影响其他测试
    monkeypatch.setattr(app_module, "STORE", FeedbackStore())
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_diagnose_from_source_endpoint(client):
    resp = client.post("/tess/diagnose-from-source", json={"limit": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    first = body["results"][0]
    assert first["event_id"] == "ERR-20260728-0912"
    assert first["diagnosis"]["status"] == STATUS_DIAGNOSED
    # 死锁：诊断输出不含 severity / calculated_loss
    assert "severity" not in first["diagnosis"]
    assert "calculated_loss" not in first["diagnosis"]


# ---- P6：token 透传 + fluctuation 归一化 ----

def test_teensing_token_is_forwarded(monkeypatch):
    """TeensingDataConnector 必须把调用方 token 作为 Bearer 透传给 Teensing。"""
    captured = {}

    def fake_get(self, path, params=None, token=None):
        captured["token"] = token
        # 模拟 Teensing 统一返回结构 {code,data,...} 与 fluctuation 形状
        if path == "/overview/ranking/anomaly-warning":
            return {
                "code": 0,
                "data": {
                    "falling": [{"name": "Pub_A", "entity_type": "publisher"}],
                    "rising": [],
                },
            }
        if path == "/overview/ranking/fluctuation":
            return {
                "code": 0,
                "data": {
                    "falling": [
                        {"name": "Pub_A", "change": -15.0, "profit": 120.0, "revenue": 500.0}
                    ],
                    "rising": [],
                },
            }
        return {"code": 0, "data": {}}

    monkeypatch.setattr(TeensingDataConnector, "_http_get", fake_get)
    c = TeensingDataConnector(base_url="https://saas.example.com/api/v1")
    raws = c.fetch_recent_anomalies(limit=5, token="OPERATOR_JWT_xyz")
    # token 透传校验
    assert captured["token"] == "OPERATOR_JWT_xyz"
    # 归并：anomaly-warning 的实体 + fluctuation 的量化字段
    assert len(raws) == 1
    assert raws[0]["name"] == "Pub_A"
    assert raws[0]["change"] == -15.0  # 来自 fluctuation
    ctx = normalize_to_context(raws[0])
    meta = ctx["anomaly_metadata"]
    assert meta["event_id"] == "Pub_A"
    assert meta["severity"] == "HIGH"  # change <= -10
    assert meta["target_metric"] == "Profit"
    assert meta["current_value"] == 120.0
    assert meta["benchmark_value"] == 135.0  # 120 - (-15)


def test_teensing_requires_token_via_app(client, monkeypatch):
    """生产(teensing)模式下，/tess/diagnose-from-source 缺 X-Teensing-Token 应 400。"""
    monkeypatch.setattr(app_module, "_DATA_CONNECTOR", TeensingDataConnector(
        base_url="https://saas.example.com/api/v1"
    ))
    resp = client.post(
        "/tess/diagnose-from-source",
        json={"limit": 2},
        headers={"X-Operator-Id": "alice"},  # 有运营身份但无 token
    )
    assert resp.status_code == 400
    assert "X-Teensing-Token" in resp.json()["detail"]


def test_audit_log_records_per_operator(client):
    """/tess/diagnose 应把问答写入审计，并按 X-Operator-Id 归因。"""
    from tess_backend.app import AUDIT
    client.post(
        "/tess/diagnose",
        json={
            "anomaly_metadata": {"event_id": "E-AUDIT", "severity": "HIGH"},
            "top_contributors": [],
        },
        headers={"X-Operator-Id": "carol"},
    )
    rows = AUDIT.recent(operator_id="carol")
    assert len(rows) >= 1
    assert rows[0]["operator_id"] == "carol"
    assert rows[0]["endpoint"] == "/tess/diagnose"
