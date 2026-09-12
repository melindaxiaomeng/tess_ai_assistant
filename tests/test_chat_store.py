"""P8 · 多轮会话存储 + process_question 多轮指代消解 单测。

不触达真实 LLM / Teensing：LLM 用捕获 prompt 的 FakeLLM，connector 用返回空 JSON 的 FakeConnector。
LLM 客户端与 /tess/ask、/tess/tool 接线通过 FastAPI TestClient 验证。
"""

import os
import sys
import tempfile

import pytest

# 让测试可直接 import tess_backend（与既有 test_api.py 同约定）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tess_backend import analytics
from tess_backend import chat_store as cs
from tess_backend.chat_store import ChatStore, get_chat_store


class FakeConnector:
    """仅实现 _safe_api_get 所需的 api_get，返回空结果，避免依赖真实 /report。"""

    def api_get(self, path, params=None, token=None):
        return {}


class CapturingLLM:
    """捕获完整 (system, user) 调用，便于断言历史上下文是否注入。"""

    def __init__(self):
        self.calls = []

    def complete(self, system, user, json_mode=False):
        self.calls.append((system, user))
        return "（测试用固定回答）"


@pytest.fixture
def store(tmp_path):
    """每个测试用独立临时 SQLite，避免污染默认库文件。"""
    s = ChatStore(str(tmp_path / "chat.db"))
    cs._STORE = s
    yield s
    cs._STORE = None


# ----------------------------- ChatStore 基础 -----------------------------

def test_append_and_get_messages(store):
    store.append("c1", "op1", "user", "你好")
    store.append("c1", "op1", "assistant", "你好，有什么可以帮你？")
    msgs = store.get_messages("c1")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "你好"
    assert msgs[1]["role"] == "assistant"


def test_get_messages_missing_session(store):
    assert store.get_messages("nope") == []


def test_prune_to_limit(store):
    # 限制 4 条（=2 轮），写入 6 条后应只保留最近 4 条
    for i in range(6):
        store.append("c2", "op", "user" if i % 2 == 0 else "assistant", f"m{i}", limit=4)
    msgs = store.get_messages("c2")
    assert len(msgs) == 4
    assert msgs[0]["content"] == "m2"  # 仅保留 m2..m5


def test_get_last_user_entities(store):
    store.append("c3", "op", "user", "广告主1000839的营收", meta={"entities": {"advertiser_id": 1000839}})
    store.append("c3", "op", "assistant", "营收如下")
    assert store.get_last_user_entities("c3") == {"advertiser_id": 1000839}


def test_get_last_user_entities_empty(store):
    assert store.get_last_user_entities("missing") is None


def test_delete(store):
    store.append("c4", "op", "user", "x")
    assert store.delete("c4") is True
    assert store.delete("c4") is False
    assert store.get_messages("c4") == []


def test_format_history():
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    text = ChatStore.format_history(msgs, turns=2)
    assert "用户：q1" in text and "Tess：a1" in text and "用户：q2" in text
    # turns=1 仅保留最近一轮
    text1 = ChatStore.format_history(msgs, turns=1)
    assert "q1" not in text1 and "q2" in text1


def test_load_history_and_record_turn(store):
    cs.record_turn("c5", "op", "广告主1000839营收", "回答", {"advertiser_id": 1000839})
    assert cs.record_turn("", "op", "q", "a", {}) is None  # 空 chat_id 不写
    text, ents = cs.load_history("c5")
    assert ents == {"advertiser_id": 1000839}
    assert "广告主1000839营收" in text


# ------------------- process_question 多轮指代消解 -------------------

def test_history_injected_into_prompt():
    llm = CapturingLLM()
    hist = "用户：广告主 1000839 的营收\nTess：营收是 100"
    analytics.process_question("什么是 CTIT", FakeConnector(), llm, history=hist)
    assert llm.calls, "应当调用了一次 LLM"
    user_prompt = llm.calls[0][1]
    assert "历史对话" in user_prompt and "广告主 1000839" in user_prompt


def test_carry_forward_entity_when_no_explicit_entity():
    llm = CapturingLLM()
    # 本轮无任何实体，但上一轮解析出 campaign_id=5845554 -> 应回退到 campaign_detail
    res = analytics.process_question(
        "它昨天的营收怎么样", FakeConnector(), llm,
        history_entities={"campaign_id": 5845554},
    )
    cs_dict = res["context_summary"]
    assert cs_dict.get("analysis_type") == "campaign_detail"
    assert cs_dict.get("route_source") == "entity"


def test_no_carry_forward_when_explicit_entity_present():
    llm = CapturingLLM()
    # 本轮已显式带 advertiser_id -> 不被 history 的 campaign_id 覆盖，也不误触 cross
    res = analytics.process_question(
        "广告主 1000839 的营收", FakeConnector(), llm,
        params={"advertiser_id": 1000839},
        history_entities={"campaign_id": 5845554},
    )
    cs_dict = res["context_summary"]
    assert cs_dict.get("analysis_type") == "advertiser_deepdive"
    assert cs_dict.get("route_source") == "entity"


def test_no_history_stays_single_turn():
    llm = CapturingLLM()
    # 既无实体也无历史 -> 浅层兜底，不应有 analysis_type / route_source
    res = analytics.process_question("今天大盘怎么样", FakeConnector(), llm)
    cs_dict = res["context_summary"]
    assert "analysis_type" not in cs_dict
    assert "route_source" not in cs_dict
    assert "历史对话" not in llm.calls[0][1]


# ------------------- HTTP 接线（/tess/ask 多轮 + 会话端点） -------------------

def _client(monkeypatch, tmp_path):
    import tess_backend.app as app_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "_get_llm_client", lambda: CapturingLLM())
    monkeypatch.setattr(app_module, "_DATA_CONNECTOR", FakeConnector())
    cs._STORE = ChatStore(str(tmp_path / "http_chat.db"))
    return TestClient(app_module.app)


def test_ask_multi_turn_records_and_carries(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    # 第一轮：带 advertiser_id
    r1 = c.post("/tess/ask", json={"question": "广告主 1000839 的营收", "chat_id": "sess1"})
    assert r1.status_code == 200
    # 第二轮：无实体追问 -> 应回退沿用 advertiser_id
    r2 = c.post("/tess/ask", json={"question": "它昨天的利润怎么样", "chat_id": "sess1"})
    assert r2.status_code == 200
    assert r2.json()["context_summary"]["analysis_type"] == "advertiser_deepdive"
    assert r2.json()["context_summary"]["route_source"] == "entity"
    # 历史已落库（2 问 2 答）
    hist = c.get("/tess/chat/sess1").json()
    assert hist["count"] == 4


def test_ask_single_turn_no_store(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.post("/tess/ask", json={"question": "什么是 CTIT"})
    assert r.status_code == 200
    # 未传 chat_id -> 不应建立任何会话
    assert c.get("/tess/chat/nonexistent").json()["count"] == 0


def test_chat_delete_endpoint(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    c.post("/tess/ask", json={"question": "q", "chat_id": "sessD"})
    assert c.get("/tess/chat/sessD").json()["count"] == 2
    d = c.delete("/tess/chat/sessD")
    assert d.json()["deleted"] is True
    assert c.get("/tess/chat/sessD").json()["count"] == 0


def test_list_sessions_unit(store):
    store.append("cA", "opX", "user", "广告主1000839的营收")
    store.append("cA", "opX", "assistant", "营收如下")
    store.append("cB", "opX", "user", "campaign 5845554 的 CTIT")
    store.append("cC", "opY", "user", "别的运营的问题")
    all_x = store.list_sessions(operator_id="opX")
    assert len(all_x) == 2
    by_id = {s["chat_id"]: s for s in all_x}
    # title 取首条 user 问题；message_count 正确
    assert by_id["cA"]["title"] == "广告主1000839的营收"
    assert by_id["cA"]["message_count"] == 2
    assert by_id["cB"]["title"] == "campaign 5845554 的 CTIT"
    assert by_id["cB"]["message_count"] == 1
    # operator 隔离：opY 只看自己的
    all_y = store.list_sessions(operator_id="opY")
    assert len(all_y) == 1 and all_y[0]["chat_id"] == "cC"


def test_chats_list_endpoint(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    c.post("/tess/ask", json={"question": "广告主 1000839 的营收", "chat_id": "sessL1"})
    c.post("/tess/ask", json={"question": "它昨天的利润", "chat_id": "sessL1"})
    c.post("/tess/ask", json={"question": "另一个会话", "chat_id": "sessL2"})
    r = c.get("/tess/chats")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    ids = {s["chat_id"] for s in body["sessions"]}
    assert ids == {"sessL1", "sessL2"}
    s1 = next(s for s in body["sessions"] if s["chat_id"] == "sessL1")
    assert s1["title"] == "广告主 1000839 的营收"
    assert s1["message_count"] == 4


# ------------------- 报表导出 / 聚合（P9 运营分析） -------------------

def test_export_rows_and_aggregate(store):
    store.append("cX", "opX", "user", "广告主营收",
                 meta={"entities": {"advertiser_id": 1000839},
                       "analysis_type": "advertiser_deepdive", "route_source": "entity"})
    store.append("cX", "opX", "assistant", "营收 100")
    store.append("cX", "opX", "user", "campaign 营收",
                 meta={"entities": {"campaign_id": 5845554},
                       "analysis_type": "campaign_detail", "route_source": "entity"})
    store.append("cX", "opX", "assistant", "营收 200")
    store.append("cY", "opY", "user", "QA 问题",
                 meta={"entities": {}, "analysis_type": None, "route_source": None})
    store.append("cY", "opY", "assistant", "这是 CTIT 解释")

    rows = store.export_rows()
    assert len(rows) == 3  # 3 个 user+assistant 配对
    assert rows[0]["question"] == "广告主营收"
    assert rows[0]["analysis_type"] == "advertiser_deepdive"
    assert rows[0]["advertiser_id"] == 1000839
    assert rows[0]["answer"] == "营收 100"

    # 按 operator 隔离
    only_x = store.export_rows(operator_id="opX")
    assert len(only_x) == 2
    assert all(r["operator_id"] == "opX" for r in only_x)

    stats = store.aggregate_stats()
    assert stats["total_sessions"] == 2
    assert stats["total_turns"] == 3
    assert stats["analysis_type_distribution"].get("advertiser_deepdive") == 1
    assert stats["analysis_type_distribution"].get("campaign_detail") == 1
    assert stats["analysis_type_distribution"].get("None") == 1
    assert stats["route_source_distribution"].get("entity") == 2
    assert stats["route_source_distribution"].get("None") == 1
    assert stats["top_entities"][0]["entity"] == "advertiser_id=1000839"
    assert stats["per_operator"][0]["operator_id"] == "opX"
    assert any(k for k in stats["daily_buckets"])  # 有日期分桶


def test_export_and_stats_endpoints(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    # 制造数据：带 chat_id 的问答（落库 analysis_type / route_source）
    c.post("/tess/ask", json={"question": "广告主 1000839 的营收", "chat_id": "sessE1"})
    c.post("/tess/ask", json={"question": "它昨天的利润", "chat_id": "sessE1"})

    # export json
    rj = c.get("/tess/chats/export?format=json")
    assert rj.status_code == 200
    jbody = rj.json()
    assert jbody["count"] == 2
    assert jbody["rows"][0]["analysis_type"] == "advertiser_deepdive"
    assert jbody["rows"][0]["route_source"] == "entity"

    # export csv
    rc = c.get("/tess/chats/export?format=csv")
    assert rc.status_code == 200
    assert rc.headers["content-type"].startswith("text/csv")
    assert "question" in rc.text and "analysis_type" in rc.text

    # stats
    rs = c.get("/tess/chats/stats")
    assert rs.status_code == 200
    sbody = rs.json()
    assert sbody["total_sessions"] == 1
    assert sbody["total_turns"] == 2
    assert sbody["analysis_type_distribution"].get("advertiser_deepdive") == 2


# ------------------- P9 分平台隔离 -------------------

def test_platform_filtering_unit(store):
    store.append("cP1", "op", "user", "q1", platform_id="facemoji")
    store.append("cP1", "op", "assistant", "a1", platform_id="facemoji")
    store.append("cP2", "op", "user", "q2", platform_id="brandb")
    # 列表按平台隔离
    fm = store.list_sessions(operator_id="op", platform_id="facemoji")
    assert len(fm) == 1 and fm[0]["chat_id"] == "cP1"
    bb = store.list_sessions(operator_id="op", platform_id="brandb")
    assert len(bb) == 1 and bb[0]["chat_id"] == "cP2"
    # 不带平台过滤 -> 全部
    allp = store.list_sessions(operator_id="op")
    assert len(allp) == 2


def test_export_and_stats_platform_filter(store):
    store.append("cX", "op", "user", "q", platform_id="facemoji",
                 meta={"entities": {"campaign_id": 1}, "analysis_type": "campaign_detail",
                       "route_source": "entity"})
    store.append("cX", "op", "assistant", "a", platform_id="facemoji")
    store.append("cY", "op", "user", "q2", platform_id="brandb",
                 meta={"entities": {}, "analysis_type": None, "route_source": None})
    store.append("cY", "op", "assistant", "a2", platform_id="brandb")
    fm = store.export_rows(platform_id="facemoji")
    assert len(fm) == 1 and fm[0]["platform_id"] == "facemoji"
    bb = store.export_rows(platform_id="brandb")
    assert len(bb) == 1 and bb[0]["platform_id"] == "brandb"
    stats = store.aggregate_stats(platform_id="facemoji")
    assert stats["total_turns"] == 1
    assert stats["per_platform"][0]["platform_id"] == "facemoji"


def test_ask_tags_platform_via_header(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.post("/tess/ask", json={"question": "广告主 1000839 的营收", "chat_id": "sessP"},
               headers={"X-Platform-Id": "facemoji"})
    assert r.status_code == 200
    # 带平台头过滤到该平台会话
    fm = c.get("/tess/chats", headers={"X-Platform-Id": "facemoji"})
    assert fm.json()["count"] == 1
    # 默认（无平台头）也能看到全部
    assert c.get("/tess/chats").json()["count"] == 1
