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
