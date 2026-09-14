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

# import 期固化真实的 _get_llm_client（部分测试文件裸赋值打桩且不还原，
# 全量跑时会污染模块属性；这里保存原始引用供 llm key 优先级用例恢复使用）
_REAL_GET_LLM_CLIENT = app_module._get_llm_client


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
    monkeypatch.setattr(app_module, "_get_llm_client", lambda *a, **k: CapturingLLM())
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


def test_admin_create_platform_without_token(client):
    """取数平台 token 已废弃：注册平台只需 id（token 可不填）。"""
    h = {"X-Admin-Key": "test-admin"}
    created = client.post(
        "/tess/admin/platforms",
        json={"id": "melodong", "name": "Melodong", "llm_api_key": "sk-melo"},
        headers=h,
    )
    assert created.status_code == 200
    body = created.json()["platform"]
    assert body["id"] == "melodong"
    assert body["token"] == ""  # 历史遗留字段，默认空
    assert body["llm_api_key"] == "sk-melo"
    # 缺 id -> 422
    bad = client.post("/tess/admin/platforms", json={"name": "x"}, headers=h)
    assert bad.status_code == 422
    client.delete("/tess/admin/platforms/melodong", headers=h)


def test_admin_platforms_llm_api_key_roundtrip(client):
    """admin 接口可写入 / 查看 / 更新平台级 DeepSeek key。"""
    h = {"X-Admin-Key": "test-admin"}
    created = client.post(
        "/tess/admin/platforms",
        json={"id": "melo", "name": "Melodong", "token": "tok-melo",
              "llm_api_key": "sk-melo"},
        headers=h,
    )
    assert created.status_code == 200
    assert created.json()["platform"]["llm_api_key"] == "sk-melo"
    # 列表带出
    listed = client.get("/tess/admin/platforms", headers=h)
    assert listed.json()["platforms"][0]["llm_api_key"] == "sk-melo"
    # 更新
    upd = client.put(
        "/tess/admin/platforms/melo", json={"llm_api_key": "sk-melo-2"}, headers=h
    )
    assert upd.json()["platform"]["llm_api_key"] == "sk-melo-2"
    client.delete("/tess/admin/platforms/melo", headers=h)


def test_get_llm_client_platform_key_priority(monkeypatch, tmp_path):
    """_get_llm_client：平台 llm_api_key > 全局 TESS_LLM_API_KEY。"""
    from tess_backend.platform_registry import PlatformRegistry

    reg = PlatformRegistry(str(tmp_path / "llm_prio.db"))
    reg.create("melodong", "Melodong", "tok", llm_api_key="sk-melo")
    reg.create("nokey", "NoKey", "tok")  # 未配平台 key
    monkeypatch.setattr(app_module, "get_platform_registry", lambda: reg)
    # 恢复真实实现（别的测试可能裸赋值打桩过该属性）
    monkeypatch.setattr(app_module, "_get_llm_client", _REAL_GET_LLM_CLIENT)

    # ① 平台 key 压过全局
    monkeypatch.setenv("TESS_LLM_API_KEY", "sk-global")
    assert app_module._get_llm_client("melodong").api_key == "sk-melo"
    # ② 平台未配 key -> 回退全局
    assert app_module._get_llm_client("nokey").api_key == "sk-global"
    # ③ 不带平台 -> 全局
    assert app_module._get_llm_client().api_key == "sk-global"
    # ④ 平台 key 存在但全局删掉 -> 仍可用平台 key
    monkeypatch.delenv("TESS_LLM_API_KEY", raising=False)
    assert app_module._get_llm_client("melodong").api_key == "sk-melo"
    # ⑤ 都没有 -> 503（提示里带平台 id）
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        app_module._get_llm_client("nokey")
    assert ei.value.status_code == 503
    assert "nokey" in ei.value.detail


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


# ------------------- AI 付费授权（按平台维度的到期时间） -------------------

@pytest.fixture
def ent_client(monkeypatch, tmp_path):
    """带独立平台库的 client：便于断言「到期 -> 403 / 历史可读」等授权行为。"""
    from tess_backend.platform_registry import PlatformRegistry

    reg = PlatformRegistry(str(tmp_path / "ent.db"))
    monkeypatch.setattr(app_module, "get_platform_registry", lambda: reg)
    monkeypatch.setattr(app_module, "_get_llm_client", lambda *a, **k: CapturingLLM())
    monkeypatch.setattr(app_module, "_DATA_CONNECTOR", FakeConnector())
    cs._STORE = ChatStore(str(tmp_path / "ent_chat.db"))
    monkeypatch.setattr(app_module, "_ADMIN_API_KEY", "test-admin")
    return TestClient(app_module.app), reg


def test_entitlement_endpoint_states(ent_client):
    """GET /tess/entitlement 覆盖各授权状态。"""
    c, reg = ent_client
    reg.create("future_p", "Future", expires_at="2999-01-01")
    reg.create("past_p", "Past", expires_at="2020-01-01")
    reg.create("perm_p", "Perm")
    reg.create("off_p", "Off", is_active=False)

    r = c.get("/tess/entitlement", headers={"X-Platform-Id": "future_p"})
    assert r.status_code == 200
    assert r.json()["ai_enabled"] is True and r.json()["expired"] is False
    assert r.json()["reason"] == "ok" and r.json()["days_left"] > 0

    assert c.get("/tess/entitlement", headers={"X-Platform-Id": "past_p"}).json()["reason"] == "expired"
    # ?platform= 与请求头等效
    assert c.get("/tess/entitlement?platform=perm_p").json()["reason"] == "no_expiry"
    assert c.get("/tess/entitlement", headers={"X-Platform-Id": "off_p"}).json()["reason"] == "disabled"
    # 未注册 / 未带平台 -> 放行，不误伤
    assert c.get("/tess/entitlement", headers={"X-Platform-Id": "nope"}).json()["reason"] == "unregistered"
    assert c.get("/tess/entitlement").json()["reason"] == "no_platform"


def test_ask_blocked_when_expired(ent_client):
    """到期后新建提问 -> 403，错误体带机器可读 code。"""
    c, reg = ent_client
    reg.create("past_p", "Past", expires_at="2020-01-01")
    r = c.post("/tess/ask", json={"question": "昨天营收如何"},
               headers={"X-Platform-Id": "past_p"})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["code"] == "AI_EXPIRED"
    assert detail["platform_id"] == "past_p"
    assert detail["expires_at"] == "2020-01-01"


def test_analytics_and_tool_blocked_when_expired(ent_client):
    """/tess/analytics、/tess/tool 同为「提问」入口，一并拦截。"""
    c, reg = ent_client
    reg.create("past_p", "Past", expires_at="2020-01-01")
    h = {"X-Platform-Id": "past_p"}
    assert c.post("/tess/analytics", json={"analysis_type": "daily_summary"}, headers=h).status_code == 403
    assert c.post("/tess/tool", json={"tool": "tess_ask", "arguments": {"question": "x"}}, headers=h).status_code == 403


def test_ask_allowed_before_expiry(ent_client):
    """未到期的平台仍可正常提问。"""
    c, reg = ent_client
    reg.create("future_p", "Future", expires_at="2999-01-01")
    r = c.post("/tess/ask", json={"question": "什么是 CTIT"},
               headers={"X-Platform-Id": "future_p"})
    assert r.status_code == 200


def test_expired_platform_can_still_read_history(ent_client):
    """核心契约：到期只禁新提问，历史读取路径一律放行（运营可回看/导出）。"""
    c, reg = ent_client
    reg.create("past_p", "Past", expires_at="2020-01-01")
    h = {"X-Platform-Id": "past_p"}
    assert c.get("/tess/chats", headers=h).status_code == 200
    assert c.get("/tess/chats/export", headers=h).status_code == 200
    assert c.get("/tess/chats/stats", headers=h).status_code == 200
    assert c.get("/tess/chat/whatever", headers=h).status_code == 200


def test_admin_platforms_expires_at_roundtrip(client):
    """admin 接口可写入 / 更新 / 清空平台到期时间。"""
    h = {"X-Admin-Key": "test-admin"}
    created = client.post(
        "/tess/admin/platforms",
        json={"id": "exp1", "name": "Exp", "expires_at": "2026-12-31"},
        headers=h,
    )
    assert created.status_code == 200
    assert created.json()["platform"]["expires_at"] == "2026-12-31"
    # 列表带出
    listed = client.get("/tess/admin/platforms", headers=h).json()["platforms"]
    by_id = {p["id"]: p for p in listed}
    assert by_id["exp1"]["expires_at"] == "2026-12-31"
    # 改到期时间
    upd = client.put("/tess/admin/platforms/exp1", json={"expires_at": "2027-06-30"}, headers=h)
    assert upd.json()["platform"]["expires_at"] == "2027-06-30"
    # 显式传 null -> 清空（永久有效）
    cleared = client.put("/tess/admin/platforms/exp1", json={"expires_at": None}, headers=h)
    assert cleared.json()["platform"]["expires_at"] is None
    client.delete("/tess/admin/platforms/exp1", headers=h)
