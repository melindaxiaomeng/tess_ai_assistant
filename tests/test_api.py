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
