"""P9 · 平台相关 HTTP 接线：admin 管理接口 + ask 打 platform_id + 列表/alerts 平台过滤 + 诊断打标。

不触达真实 LLM / Teensing：LLM 用固定回答的 CapturingLLM，connector 用 FakeConnector。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tess_backend.app as app_module
import tess_backend.chat_store as cs
from tess_backend.chat_store import ChatStore
from tess_backend.alerts_store import AlertStore
from fastapi.testclient import TestClient


class FakeConnector:
    """仅实现诊断所需方法，返回可控样本，避免依赖真实 Teensing。"""

    def fetch_recent_anomalies(self, limit, token=None):
        return [{
            "campaign_id": 1, "publisher_id": 2, "revenue": 999.0,
            "anomaly_metadata": {"event_id": "DIAG-1", "severity": "HIGH"},
        }]

    def fetch_campaign_time_series(self, cid, token=None, publisher_id=None):
        return {}

    def fetch_realtime_kpi(self, token=None):
        return {}


class CapturingLLM:
    def complete(self, system, user, json_mode=False):
        return "（测试用固定回答）"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_get_llm_client", lambda: CapturingLLM())
    monkeypatch.setattr(app_module, "_DATA_CONNECTOR", FakeConnector())
    cs._STORE = ChatStore(str(tmp_path / "http_chat.db"))
    # 注入一个测试用管理密钥（覆盖模块级从 env 读取的 _ADMIN_API_KEY）
    monkeypatch.setattr(app_module, "_ADMIN_API_KEY", "test-admin")
    return TestClient(app_module.app)


# ------------------- 平台管理接口（X-Admin-Key 守卫） -------------------

def test_admin_platforms_crud(client):
    # 未带密钥 -> 403
    assert client.get("/tess/admin/platforms").status_code == 403
    h = {"X-Admin-Key": "test-admin"}
    # 新增
    created = client.post(
        "/tess/admin/platforms",
        json={"id": "fm", "name": "Facemoji", "token": "tok-fm", "is_active": True},
        headers=h,
    )
    assert created.status_code == 200
    assert created.json()["platform"]["token"] == "tok-fm"
    # 列表
    listed = client.get("/tess/admin/platforms", headers=h)
    assert listed.json()["count"] == 1
    # 改
    upd = client.put("/tess/admin/platforms/fm", json={"is_active": False}, headers=h)
    assert upd.json()["platform"]["is_active"] is False
    # 删
    deleted = client.delete("/tess/admin/platforms/fm", headers=h)
    assert deleted.json()["deleted"] == "fm"
    assert client.get("/tess/admin/platforms", headers=h).json()["count"] == 0


def test_admin_bypasses_global_api_key_guard(client, monkeypatch):
    """生产开启 TESS_API_KEY 后，admin 接口仍只凭 X-Admin-Key 可用（不被全局 401 拦截）。"""
    monkeypatch.setattr(app_module, "_TESS_API_KEY", "global-key")
    h = {"X-Admin-Key": "test-admin"}
    # 只带管理密钥、不带 X-API-Key -> 应 200（走 _require_admin 独立守卫）
    res = client.get("/tess/admin/platforms", headers=h)
    assert res.status_code == 200
    # 全局守卫对其余 /tess/* 仍然生效：不带 X-API-Key 访问 alerts -> 401
    assert client.get("/tess/alerts").status_code == 401
    # 带对 X-API-Key 但没带管理密钥访问 admin -> 仍 403（不能靠调用方密钥越权）
    assert client.get(
        "/tess/admin/platforms", headers={"X-API-Key": "global-key"}
    ).status_code == 403


# ------------------- 预警平台过滤（?platform= / X-Platform-Id） -------------------

def test_alerts_platform_filter(client, monkeypatch, tmp_path):
    store = AlertStore(str(tmp_path / "alerts_pf.db"))
    monkeypatch.setattr(app_module, "ALERTS", store)
    store.save_batch(
        [{"event_id": "F1", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "anomaly-warning"}}],
        platform_id="facemoji", run_time="2026-07-30 12:00:00",
    )
    store.save_batch(
        [{"event_id": "B1", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "anomaly-warning"}}],
        platform_id="brandb", run_time="2026-07-30 12:00:00",
    )
    fm = client.get("/tess/alerts", headers={"X-Platform-Id": "facemoji"})
    assert fm.json()["count"] == 1 and fm.json()["alerts"][0]["platform_id"] == "facemoji"
    bb = client.get("/tess/alerts?platform=brandb")
    assert bb.json()["count"] == 1


# ------------------- 定时诊断按平台打标 -------------------

def test_run_diagnosis_tags_platform(client, monkeypatch, tmp_path):
    store = AlertStore(str(tmp_path / "diag.db"))
    monkeypatch.setattr(app_module, "ALERTS", store)
    # 指定平台运行一次诊断
    res = app_module.run_scheduled_diagnosis(
        limit=5, connector=FakeConnector(), llm=CapturingLLM(), platform_id="facemoji")
    assert len(res) >= 1
    fm = store.recent(platform="facemoji")
    assert len(fm) == 1 and fm[0]["platform_id"] == "facemoji"
    # 不带 platform_id -> 回退 default 平台
    app_module.run_scheduled_diagnosis(limit=5, connector=FakeConnector(), llm=CapturingLLM())
    assert len(store.recent(platform="default")) >= 1
