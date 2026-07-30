"""P4 · API 单测：用 FastAPI TestClient 验证 POST /tess/diagnose。

LLM 客户端通过 monkeypatch 注入 Mock，不触达真实模型。
需先安装 fastapi + httpx（venv）：pip install fastapi httpx
"""

import pytest

from tess_backend import app as app_module
from tess_backend.contracts import STATUS_DIAGNOSED
from tess_backend.tess_agent import MockLLMClient
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


def _r6_input():
    return {
        "anomaly_metadata": {
            "event_id": "ERR-20260728-0912",
            "current_value": "3.8%",
            "severity": "HIGH",
            "calculated_loss": {
                "loss_per_hour_usd": 350.0,
                "calculation_basis": "基于过去 30 分钟 Cost 速率与缺失 Revenue 的差值计算",
            },
        },
        "top_contributors": [
            {
                "dimension_type": "Publisher",
                "dimension_value": "Pub_Media_802",
                "impact_share": "82%",
            }
        ],
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        app_module, "_get_llm_client", lambda: MockLLMClient(_mock_response(0.92))
    )
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_diagnose_endpoint_returns_200(client):
    resp = client.post("/tess/diagnose", json=_r6_input())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == STATUS_DIAGNOSED
    # 死锁：LLM 绝不持有 severity / loss
    assert "severity" not in body
    assert "calculated_loss" not in body


@pytest.fixture
def fb_client(monkeypatch):
    # 反馈端点测试用独立 STORE，避免被 diagnose 单例污染
    monkeypatch.setattr(app_module, "STORE", FeedbackStore())
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_feedback_endpoint_records(fb_client):
    resp = fb_client.post(
        "/tess/feedback",
        json={
            "event_id": "ERR-20260728-0912",
            "vote": "accurate",
            "tess_status": "DIAGNOSED",
            "confidence": 0.92,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    metrics = fb_client.get("/tess/feedback/metrics").json()
    assert metrics["feedback_count"] == 1
    assert metrics["vote_distribution"]["accurate"] == 1


def test_feedback_endpoint_rejects_bad_vote(fb_client):
    resp = fb_client.post(
        "/tess/feedback",
        json={
            "event_id": "ERR-X",
            "vote": "meh",
            "tess_status": "DIAGNOSED",
            "confidence": 0.9,
        },
    )
    assert resp.status_code == 422


@pytest.fixture
def kpi_alert_client(monkeypatch):
    """用临时预警库替换单例，避免污染其它测试。"""
    import tempfile

    from tess_backend.alerts_store import AlertStore

    monkeypatch.setattr(
        app_module, "_get_llm_client", lambda: MockLLMClient(_mock_response(0.92))
    )
    monkeypatch.setattr(app_module, "ALERTS", AlertStore(tempfile.mktemp(suffix=".db")))
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_realtime_kpi_alerts_endpoint(kpi_alert_client):
    """Teensing 拉取接口：只返回最近一批 realtime-kpi 来源的预警，且透传原始 anomaly_metadata。"""
    app_module.ALERTS.save_batch([
        {
            "event_id": "REALTIME-GAP-09-17",
            "diagnosis": {"status": "DIAGNOSED", "confidence": 0.9, "summary": "掉零"},
            "meta": {"source": "realtime-kpi"},
            "anomaly_metadata": {
                "event_id": "REALTIME-GAP-09-17",
                "current_value": 0.0,
                "benchmark_value": 1234.5,
                "severity": "HIGH",
            },
        },
        {"event_id": "A1", "diagnosis": {"status": "DIAGNOSED", "confidence": 0.8}, "meta": {"source": "anomaly-warning"}},
    ])
    resp = kpi_alert_client.get("/tess/realtime-kpi/alerts")
    assert resp.status_code == 200
    body = resp.json()
    # 契约字段齐备
    assert "as_of" in body and body["as_of"]
    assert "generated_at" in body and body["generated_at"]
    assert body["count"] == 1  # 仅 realtime-kpi
    item = body["items"][0]
    assert item["event_id"] == "REALTIME-GAP-09-17"
    assert item["source"] == "realtime-kpi"
    assert item["diagnosis"]["status"] == "DIAGNOSED"
    # 原始数值透传：Teensing 可直接展示「昨日基准 / 今日 / 严重度」
    assert item["anomaly_metadata"]["current_value"] == 0.0
    assert item["anomaly_metadata"]["benchmark_value"] == 1234.5
    assert item["anomaly_metadata"]["severity"] == "HIGH"


def test_realtime_kpi_alerts_endpoint_empty(kpi_alert_client):
    """无数据时返回安全默认结构。"""
    resp = kpi_alert_client.get("/tess/realtime-kpi/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["as_of"] is None
    assert body["count"] == 0
    assert body["items"] == []


def test_realtime_kpi_alerts_min_severity(kpi_alert_client):
    """min_severity 过滤：只返回 >= 指定严重度的告警，避免 LOW 微跌刷屏。"""
    app_module.ALERTS.save_batch([
        {"event_id": "H", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "realtime-kpi"},
         "anomaly_metadata": {"severity": "HIGH"}},
        {"event_id": "M", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "realtime-kpi"},
         "anomaly_metadata": {"severity": "MEDIUM"}},
        {"event_id": "L", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "realtime-kpi"},
         "anomaly_metadata": {"severity": "LOW"}},
    ])
    # 不过滤：3 条全返回
    all_ = kpi_alert_client.get("/tess/realtime-kpi/alerts").json()
    assert all_["count"] == 3
    # min_severity=MEDIUM：仅 HIGH + MEDIUM 共 2 条
    med = kpi_alert_client.get("/tess/realtime-kpi/alerts?min_severity=MEDIUM").json()
    assert med["count"] == 2
    assert {i["event_id"] for i in med["items"]} == {"H", "M"}
    # min_severity=HIGH：仅 1 条
    high = kpi_alert_client.get("/tess/realtime-kpi/alerts?min_severity=HIGH").json()
    assert high["count"] == 1
    assert high["items"][0]["event_id"] == "H"


def test_alert_ack_endpoint_filters_default_pull(kpi_alert_client):
    """运营确认回写：标记后默认拉取（include_acked=false）不再返回该告警。"""
    app_module.ALERTS.save_batch([
        {"event_id": "R1", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "realtime-kpi"},
         "anomaly_metadata": {"severity": "HIGH"}},
    ])
    aid = app_module.ALERTS.recent(source="realtime-kpi")[0]["id"]

    # 默认拉取包含该告警
    before = kpi_alert_client.get("/tess/realtime-kpi/alerts").json()
    assert before["count"] == 1

    # 运营确认「正常波动」
    resp = kpi_alert_client.post(
        f"/tess/alerts/{aid}/ack",
        json={"resolution": "false_positive", "acked_by": "alice", "note": "正常流量波动"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True, "id": aid, "resolution": "false_positive",
        "acked_by": "alice", "acked_at": resp.json()["acked_at"],
    }

    # 默认拉取（include_acked=false）：已确认项被过滤
    after = kpi_alert_client.get("/tess/realtime-kpi/alerts").json()
    assert after["count"] == 0

    # 显式 include_acked=true：仍可查回，且带 resolution
    with_ack = kpi_alert_client.get("/tess/realtime-kpi/alerts?include_acked=true").json()
    assert with_ack["count"] == 1
    assert with_ack["items"][0]["resolution"] == "false_positive"
    assert with_ack["items"][0]["acked_by"] == "alice"


def test_alert_ack_invalid_resolution(kpi_alert_client):
    app_module.ALERTS.save_batch([{"event_id": "R1", "diagnosis": {"status": "DIAGNOSED"},
                                   "meta": {"source": "realtime-kpi"}}])
    aid = app_module.ALERTS.recent()[0]["id"]
    resp = kpi_alert_client.post(f"/tess/alerts/{aid}/ack", json={"resolution": "bogus"})
    assert resp.status_code == 422


def test_alert_ack_unknown_id(kpi_alert_client):
    resp = kpi_alert_client.post("/tess/alerts/999999/ack", json={"resolution": "resolved"})
    assert resp.status_code == 404


def test_realtime_kpi_alerts_since_as_of(kpi_alert_client):
    """增量游标 since_as_of：只返回比该批次更新的告警。"""
    app_module.ALERTS.save_batch([{"event_id": "B1", "diagnosis": {"status": "DIAGNOSED"},
                                   "meta": {"source": "realtime-kpi"}}],
                                 run_time="2026-07-30 12:00:00")
    app_module.ALERTS.save_batch([{"event_id": "B2", "diagnosis": {"status": "DIAGNOSED"},
                                   "meta": {"source": "realtime-kpi"}}],
                                 run_time="2026-07-30 13:00:00")
    resp = kpi_alert_client.get("/tess/realtime-kpi/alerts?since_as_of=2026-07-30 12:00:00").json()
    assert resp["count"] == 1
    assert resp["items"][0]["event_id"] == "B2"
    assert resp["as_of"] == "2026-07-30 13:00:00"
