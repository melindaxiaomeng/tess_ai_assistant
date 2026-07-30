"""P7 · 定时预警调度集成测试（不触真实 LLM / Teensing）。

- run_scheduled_diagnosis：拉 mock 异常 → 诊断 → 落预警库
- GET /tess/alerts：Teensing 轮询拉取接口
- POST /tess/cron/run：手动触发一次
"""

import pytest

from tess_backend import app as app_module
from tess_backend.tess_agent import MockLLMClient
from tess_backend.data_connector import MockDataConnector
from tess_backend.feedback import FeedbackStore
from tess_backend.alerts_store import AlertStore


def _mock_response(conf=0.92):
    return {
        "status": "DIAGNOSED",
        "confidence": conf,
        "summary": "Pub_Media_802 映射变更叠加第三方回调超时导致收益缺失",
        "primary_contributor_id": "Pub_Media_802",
        "root_cause_analysis": {
            "primary_factor": "映射规则变更 + 回调超时",
            "causal_chain": ["运营变更配置", "API 超时", "转化数据缺失", "毛利暴跌"],
        },
    }


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_get_llm_client", lambda: MockLLMClient(_mock_response(0.92)))
    monkeypatch.setattr(app_module, "_DATA_CONNECTOR", MockDataConnector())
    monkeypatch.setattr(app_module, "STORE", FeedbackStore())
    monkeypatch.setattr(app_module, "ALERTS", AlertStore(str(tmp_path / "alerts.db")))
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_run_scheduled_diagnosis_stores_alerts(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_get_llm_client", lambda: MockLLMClient(_mock_response(0.92)))
    monkeypatch.setattr(app_module, "_DATA_CONNECTOR", MockDataConnector())
    store = AlertStore(str(tmp_path / "alerts.db"))
    monkeypatch.setattr(app_module, "ALERTS", store)

    results = app_module.run_scheduled_diagnosis(limit=3)
    # 两类来源都应产出：异常预警(ERR-...) + 实时 KPI 骤降(REALTIME-...)
    sources = {r.get("meta", {}).get("source") for r in results}
    assert "anomaly-warning" in sources
    assert "realtime-kpi" in sources
    assert any(r["event_id"] == "ERR-20260728-0912" for r in results)
    assert any(r["event_id"].startswith("REALTIME-") for r in results)

    # 落库校验：两种来源都能按 source 过滤检索
    rows = store.recent(limit=50)
    assert len(rows) >= 2
    stored_sources = {r["source"] for r in rows}
    assert "anomaly-warning" in stored_sources
    assert "realtime-kpi" in stored_sources

    # 原始 anomaly_metadata 应随预警一并透传落库（供 Teensing 展示 current/benchmark/severity）
    assert any(r["anomaly_metadata"] for r in rows), "预警应携带原始 anomaly_metadata"
    sample = next(r for r in rows if r["anomaly_metadata"])
    assert "current_value" in sample["anomaly_metadata"]
    assert "benchmark_value" in sample["anomaly_metadata"]
    assert "severity" in sample["anomaly_metadata"]


def test_get_alerts_filter_by_source(client):
    app_module.run_scheduled_diagnosis(limit=3)
    # 只拉实时 KPI 异常
    resp = client.get("/tess/alerts?source=realtime-kpi")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert all(r["source"] == "realtime-kpi" for r in body["alerts"])


def test_get_alerts_endpoint(client):
    # 先跑一轮，写入预警库
    app_module.run_scheduled_diagnosis(limit=3)
    resp = client.get("/tess/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert "diagnosis" in body["alerts"][0]
    assert "source" in body["alerts"][0]


def test_cron_run_endpoint(client):
    resp = client.post("/tess/cron/run", json={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    # 同时写入预警库
    rows = app_module.ALERTS.recent(limit=10)
    assert len(rows) >= 1
