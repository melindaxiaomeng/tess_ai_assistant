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
    extract_realtime_anomalies,
    _severity_for_drop,
    _safe_benchmark,
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


def test_safe_benchmark_absolute_when_sensible():
    # 普通绝对差：413.896 相对 +321.86 -> 基线 92.036
    assert _safe_benchmark(413.896, 321.86) == 92.036


def test_safe_benchmark_negative_base_falls_back_to_percent():
    # 当前 14.4、环比 +107.2 按绝对差会得 -92.8（不可能）；应回退百分比 -> 6.95
    b = _safe_benchmark(14.4, 107.2)
    assert b is not None and b >= 0
    assert abs(b - 6.95) < 0.1


def test_safe_benchmark_no_change_is_none():
    assert _safe_benchmark(14.4, None) is None


def test_normalize_benchmark_never_negative_for_rising_spike():
    # 复现 6797051：anomaly-warning revenue 14.4 + fluctuation revenue_change 107.2
    raw = {
        "campaign_id": 6797051,
        "campaign_name": "recl-game.friends-RU",
        "revenue": 14.4,
        "profit": 1.2,
        "cvr": 0.057878,
        "margin": 9.72,
        "revenue_change": 107.2,
        "_direction": "rising",
    }
    ctx = normalize_to_context(raw)
    meta = ctx["anomaly_metadata"]
    assert meta["benchmark_value"] is not None
    assert meta["benchmark_value"] >= 0, "基线收入不得为负"
    assert meta["current_value"] == 14.4


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
        # 真实 Teensing 结构：anomaly-warning 返回 data.items[]，fluctuation 返回
        # data.rising/falling[]（含 revenue_change 环比）
        if path == "/overview/ranking/anomaly-warning":
            return {
                "code": 0,
                "data": {
                    "total": 1,
                    "items": [
                        {
                            "campaign_id": 111,
                            "campaign_name": "Pub_A",
                            "revenue": 500.0,
                            "profit": 120.0,
                            "cvr": 0.1,
                            "margin": 5.0,
                        }
                    ],
                },
            }
        if path == "/overview/ranking/fluctuation":
            return {
                "code": 0,
                "data": {
                    "falling": [
                        {
                            "campaign_id": 111,
                            "campaign_name": "Pub_A",
                            "revenue": 500.0,
                            "revenue_change": -15.0,
                            "profit": 120.0,
                            "cvr": 0.1,
                            "margin": 5.0,
                        }
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
    # 归并：anomaly-warning 的 items + fluctuation 的量化字段(revenue_change)
    assert len(raws) == 1
    assert raws[0]["campaign_name"] == "Pub_A"
    assert raws[0]["revenue_change"] == -15.0  # 来自 fluctuation
    assert raws[0]["_direction"] == "falling"
    ctx = normalize_to_context(raws[0])
    meta = ctx["anomaly_metadata"]
    assert meta["event_id"] == "111"  # campaign_id 优先
    assert meta["severity"] == "HIGH"  # change <= -10
    assert meta["target_metric"] == "Revenue"  # 有环比+revenue 优先 Revenue
    assert meta["current_value"] == 500.0
    assert meta["benchmark_value"] == 515.0  # 500 - (-15)


def test_normalize_to_context_carries_history_baseline():
    """normalize_to_context 必须把 raw.history_baseline 透传到返回值，使其进入 LLM Prompt。"""
    raw = {
        "campaign_id": "7030636",
        "campaign_name": "com.cp.sto.op.id1000026152_PH",
        "revenue": 10.0,
        "profit": -8.0,
        "cvr": 0.0005,
        "margin": -80.0,
        "history_baseline": {
            "campaign_id": "7030636",
            "granularity": "day",
            "time_series": [
                {"timestamp": "2026-08-02", "revenue": 120.0, "margin_percent": -12.5},
                {"timestamp": "2026-08-03", "revenue": 10.0, "margin_percent": -80.0},
            ],
        },
    }
    ctx = normalize_to_context(raw)
    assert "history_baseline" in ctx
    assert ctx["history_baseline"]["campaign_id"] == "7030636"
    assert ctx["history_baseline"]["time_series"][-1]["margin_percent"] == -80.0


def test_fetch_campaign_time_series_mock():
    """MockDataConnector 返回带断崖下跌的 7 天样例序列。"""
    c = MockDataConnector()
    ts = c.fetch_campaign_time_series("7030636")
    assert ts["campaign_id"] == "7030636"
    assert ts["granularity"] == "day"
    assert ts["data_points_count"] == 7
    assert len(ts["time_series"]) == 7
    assert ts["time_series"][-1]["margin_percent"] == -80.0


def test_fetch_campaign_time_series_teensing(monkeypatch):
    """TeensingDataConnector 调 /report 并正确派生 CVR / Margin。"""
    def fake_get(self, path, params=None, token=None):
        assert path == "/report"
        assert params.get("campaign_ids") == "7030636"
        assert params.get("dimensions") == "date,campaign"
        assert params.get("date_start") and params.get("date_end")
        assert params.get("page") == 1
        return {
            "code": 0,
            "data": {
                "items": [
                    {"date": "2026-08-01", "revenue": 100.0, "payout": 60.0, "clicks": 1000, "conversions": 20},
                    {"date": "2026-08-02", "revenue": 50.0, "payout": 40.0, "clicks": 500, "conversions": 5},
                ]
            },
        }

    monkeypatch.setattr(TeensingDataConnector, "_http_get", fake_get)
    c = TeensingDataConnector(base_url="https://saas.example.com/api/v1")
    ts = c.fetch_campaign_time_series("7030636", token="JWT")
    assert ts["granularity"] == "day"
    assert len(ts["time_series"]) == 2
    first = ts["time_series"][0]
    assert first["profit"] == 40.0          # 100 - 60
    assert first["cvr_percent"] == 2.0      # 20/1000*100
    assert first["margin_percent"] == 40.0  # 40/100*100
    assert ts["time_series"][1]["cvr_percent"] == 1.0  # 5/500*100


def test_low_revenue_campaigns_skipped(monkeypatch):
    """anomaly-warning 中 Rev < TESS_MIN_REVENUE_USD 的低价值 campaign 不进诊断。"""
    monkeypatch.setenv("TESS_MIN_REVENUE_USD", "20")

    def fake_get(self, path, params=None, token=None):
        if path == "/overview/ranking/anomaly-warning":
            return {
                "code": 0,
                "data": {
                    "total": 2,
                    "items": [
                        {  # Rev 500 -> 保留
                            "campaign_id": 111,
                            "campaign_name": "Big_A",
                            "revenue": 500.0,
                            "profit": 120.0,
                        },
                        {  # Rev 15 < 20 -> 跳过
                            "campaign_id": 222,
                            "campaign_name": "Tiny_B",
                            "revenue": 15.0,
                            "profit": 3.0,
                        },
                    ],
                },
            }
        if path == "/overview/ranking/fluctuation":
            return {"code": 0, "data": {"rising": [], "falling": []}}
        return {"code": 0, "data": {}}

    monkeypatch.setattr(TeensingDataConnector, "_http_get", fake_get)
    c = TeensingDataConnector(base_url="https://saas.example.com/api/v1")
    raws = c.fetch_recent_anomalies(limit=10, token="OPERATOR_JWT_xyz")
    assert len(raws) == 1
    assert raws[0]["campaign_id"] == 111
    assert all((r.get("revenue") or 0) >= 20 for r in raws)


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


# ---- P7b：实时 KPI 曲线拉取 + 异常提取 ----

def test_mock_connector_realtime_kpi_has_anomaly():
    """MockDataConnector.fetch_realtime_kpi 返回真实结构 data.items[]。"""
    c = MockDataConnector()
    kpi = c.fetch_realtime_kpi()
    assert isinstance(kpi, dict)
    assert "items" in kpi["data"]


def test_extract_realtime_anomalies_banding():
    """样例：hour 12 同比 -50% 判 HIGH；其余微跌(7%~11%)判 LOW。任何下跌都算异常。"""
    c = MockDataConnector()
    kpi = c.fetch_realtime_kpi()
    ctxs = extract_realtime_anomalies(kpi)
    assert len(ctxs) >= 1
    ids = [x["anomaly_metadata"]["event_id"] for x in ctxs]
    assert any(i.startswith("REALTIME-DROP-") for i in ids)
    # hour 12 同比 -50% -> HIGH，且 current < benchmark
    h12 = [x for x in ctxs if x["anomaly_metadata"]["event_id"] == "REALTIME-DROP-12"][0]
    assert h12["anomaly_metadata"]["severity"] == "HIGH"
    assert h12["anomaly_metadata"]["current_value"] < h12["anomaly_metadata"]["benchmark_value"]
    # 至少一条微跌被判定为 LOW（任何下跌都报）
    lows = [x for x in ctxs if x["anomaly_metadata"]["severity"] == "LOW"]
    assert len(lows) >= 1


def test_severity_bands_for_drops():
    """跌幅分档：<=30% LOW, 30%<drop<50% MEDIUM, >=50% HIGH。"""
    assert _severity_for_drop(0.10) == "LOW"
    assert _severity_for_drop(0.30) == "LOW"
    assert _severity_for_drop(0.31) == "MEDIUM"
    assert _severity_for_drop(0.49) == "MEDIUM"
    assert _severity_for_drop(0.50) == "HIGH"
    assert _severity_for_drop(1.0) == "HIGH"


def test_any_drop_flagged_as_low():
    """任何下跌都报：1% 微跌 -> LOW 异常（grace_hours=0 解除延迟窗口约束）。"""
    raw = {"code": 0, "data": {"items": [
        {"hour": "10", "today_revenue": 990.0, "yesterday_revenue": 1000.0},
    ]}}
    ctxs = extract_realtime_anomalies(raw, grace_hours=0)
    assert len(ctxs) == 1
    meta = ctxs[0]["anomaly_metadata"]
    assert meta["event_id"] == "REALTIME-DROP-10"
    assert meta["severity"] == "LOW"
    assert meta["current_value"] == 990.0
    assert meta["benchmark_value"] == 1000.0


def test_40pct_drop_is_medium():
    raw = {"code": 0, "data": {"items": [
        {"hour": "10", "today_revenue": 600.0, "yesterday_revenue": 1000.0},
    ]}}
    ctxs = extract_realtime_anomalies(raw, grace_hours=0)
    assert ctxs[0]["anomaly_metadata"]["severity"] == "MEDIUM"


def test_60pct_drop_is_high():
    raw = {"code": 0, "data": {"items": [
        {"hour": "10", "today_revenue": 400.0, "yesterday_revenue": 1000.0},
    ]}}
    ctxs = extract_realtime_anomalies(raw, grace_hours=0)
    assert ctxs[0]["anomaly_metadata"]["severity"] == "HIGH"


def test_extract_realtime_anomalies_threshold():
    """阈值抬高到 0.99 时，样例曲线不应触发任何异常。"""
    c = MockDataConnector()
    kpi = c.fetch_realtime_kpi()
    ctxs = extract_realtime_anomalies(kpi, drop_threshold=0.99)
    assert ctxs == []


def test_realtime_clicks_drop_flagged_without_revenue_drop():
    """补全数据源：Revenue 持平但 Clicks 同比 -40% 也应报警，头条取 Clicks。"""
    raw = {"code": 0, "data": {"items": [
        {"hour": "10", "today_revenue": 1000.0, "yesterday_revenue": 1000.0,
         "today_clicks": 600.0, "yesterday_clicks": 1000.0,
         "today_conversions": 50.0, "yesterday_conversions": 50.0},
    ]}}
    ctxs = extract_realtime_anomalies(raw, grace_hours=0)
    assert len(ctxs) == 1
    meta = ctxs[0]["anomaly_metadata"]
    assert meta["event_id"] == "REALTIME-DROP-10"
    assert meta["target_metric"] == "Clicks"
    assert meta["severity"] == "MEDIUM"  # clicks 跌 40%
    mb = ctxs[0]["metric_breakdown"]
    assert {r["metric"] for r in mb} == {"Revenue", "Clicks", "Conversions", "CVR"}
    clicks = next(r for r in mb if r["metric"] == "Clicks")
    assert clicks["drop_ratio"] == 0.4


def test_realtime_breakdown_has_four_metrics():
    """每个 realtime DROP 上下文必须带 metric_breakdown（4 指标含派生 CVR）。"""
    c = MockDataConnector()
    ctxs = extract_realtime_anomalies(c.fetch_realtime_kpi())
    assert ctxs, "样例应至少检测出一条 realtime 异常"
    for ctx in ctxs:
        mb = ctx.get("metric_breakdown")
        assert mb, "realtime DROP 上下文必须带 metric_breakdown"
        assert {r["metric"] for r in mb} == {"Revenue", "Clicks", "Conversions", "CVR"}


def test_extract_realtime_trailing_zeros_not_gap():
    """真实返回（hour 09 起 today 全 0）是「每小时滚动更新、快照尚未覆盖」的正常尾部，
    不应误判为数据掉零(GAP)。as_of_hour 从数据推断=08，09-23 属未来/未就绪。
    关键正确性：尾零绝不聚合成「数据中断」告警；任何出现的告警都只来自已完整过去、
    且今日<昨日的真实微跌（如 hour 07），且时段 <= 08。"""
    real_raw = {
        "code": 0,
        "message": "success",
        "data": {
            "items": [
                {"hour": "00", "today_revenue": 1387.795, "today_clicks": 2645837, "today_conversions": 5588, "yesterday_revenue": 1202.416, "yesterday_clicks": 3489430, "yesterday_conversions": 5028},
                {"hour": "01", "today_revenue": 1726.969, "today_clicks": 2886768, "today_conversions": 7324, "yesterday_revenue": 1473.08, "yesterday_clicks": 3697004, "yesterday_conversions": 6927},
                {"hour": "02", "today_revenue": 1758.089, "today_clicks": 2992134, "today_conversions": 8137, "yesterday_revenue": 1409.218, "yesterday_clicks": 3304045, "yesterday_conversions": 6907},
                {"hour": "03", "today_revenue": 1671.006, "today_clicks": 2773103, "today_conversions": 7729, "yesterday_revenue": 1670.051, "yesterday_clicks": 3310794, "yesterday_conversions": 8276},
                {"hour": "04", "today_revenue": 1651.021, "today_clicks": 2714043, "today_conversions": 8392, "yesterday_revenue": 1596.898, "yesterday_clicks": 3156141, "yesterday_conversions": 7727},
                {"hour": "05", "today_revenue": 1642.383, "today_clicks": 2891099, "today_conversions": 8317, "yesterday_revenue": 1518.93, "yesterday_clicks": 2638601, "yesterday_conversions": 7928},
                {"hour": "06", "today_revenue": 1645.222, "today_clicks": 2876079, "today_conversions": 8559, "yesterday_revenue": 1477.255, "yesterday_clicks": 2691996, "yesterday_conversions": 8341},
                {"hour": "07", "today_revenue": 1333.766, "today_clicks": 2853292, "today_conversions": 7016, "yesterday_revenue": 1429.479, "yesterday_clicks": 2667018, "yesterday_conversions": 7657},
                {"hour": "08", "today_revenue": 1357.255, "today_clicks": 2975250, "today_conversions": 6800, "yesterday_revenue": 1390.104, "yesterday_clicks": 2749025, "yesterday_conversions": 7098},
                {"hour": "09", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 1424.944, "yesterday_clicks": 2858079, "yesterday_conversions": 7481},
                {"hour": "10", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 1354.531, "yesterday_clicks": 2797587, "yesterday_conversions": 6864},
                {"hour": "11", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 1231.165, "yesterday_clicks": 2798602, "yesterday_conversions": 6477},
                {"hour": "12", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 1242.933, "yesterday_clicks": 2624107, "yesterday_conversions": 6424},
                {"hour": "13", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 1114.358, "yesterday_clicks": 2561683, "yesterday_conversions": 5874},
                {"hour": "14", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 1098.172, "yesterday_clicks": 2525926, "yesterday_conversions": 6447},
                {"hour": "15", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 963.406, "yesterday_clicks": 2534758, "yesterday_conversions": 6084},
                {"hour": "16", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 910.702, "yesterday_clicks": 2579412, "yesterday_conversions": 5357},
                {"hour": "17", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 914.082, "yesterday_clicks": 2626853, "yesterday_conversions": 4496},
                {"hour": "18", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 873.301, "yesterday_clicks": 2514577, "yesterday_conversions": 4050},
                {"hour": "19", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 716.333, "yesterday_clicks": 2377858, "yesterday_conversions": 2827},
                {"hour": "20", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 649.714, "yesterday_clicks": 2067953, "yesterday_conversions": 2415},
                {"hour": "21", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 512.278, "yesterday_clicks": 1600090, "yesterday_conversions": 1978},
                {"hour": "22", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 479.023, "yesterday_clicks": 1238829, "yesterday_conversions": 1892},
                {"hour": "23", "today_revenue": 0.0, "today_clicks": 0, "today_conversions": 0, "yesterday_revenue": 442.465, "yesterday_clicks": 1130998, "yesterday_conversions": 1917},
            ]
        },
        "meta": "",
    }
    ctxs = extract_realtime_anomalies(real_raw)
    # 关键正确性：尾部全 0 绝不聚合成「数据中断」(GAP) 告警
    gaps = [c for c in ctxs if c["anomaly_metadata"]["event_id"].startswith("REALTIME-GAP-")]
    assert gaps == []
    # 所有出现的告警时段都 <= 08（09-23 尾零绝不参与判定），且仅来自真实今日<昨日
    for c in ctxs:
        h = int(c["anomaly_metadata"]["event_id"].split("-")[-1])
        assert h <= 8


def test_extract_realtime_single_hour_gap_detected():
    """若数据更新到 16h，但 14h 单点掉零（13/15/16 有值），应识别出 14h 掉零。"""
    raw = {
        "code": 0,
        "data": {
            "items": [
                {"hour": "13", "today_revenue": 1300.0, "yesterday_revenue": 1400.0},
                {"hour": "14", "today_revenue": 0.0, "yesterday_revenue": 1400.0},
                {"hour": "15", "today_revenue": 1300.0, "yesterday_revenue": 1400.0},
                {"hour": "16", "today_revenue": 1300.0, "yesterday_revenue": 1400.0},
            ]
        },
    }
    ctxs = extract_realtime_anomalies(raw)
    gaps = [c for c in ctxs if c["anomaly_metadata"]["event_id"].startswith("REALTIME-GAP-")]
    assert len(gaps) == 1
    assert gaps[0]["anomaly_metadata"]["event_id"] == "REALTIME-GAP-14-14"
    assert gaps[0]["anomaly_metadata"]["current_value"] == 0.0
    assert gaps[0]["anomaly_metadata"]["severity"] == "HIGH"


def test_realtime_kpi_token_forwarded(monkeypatch):
    """TeensingDataConnector.fetch_realtime_kpi 必须把 token 作为 Bearer 透传。"""
    captured = {}

    def fake_get(self, path, params=None, token=None):
        captured["path"] = path
        captured["token"] = token
        return {"code": 0, "data": {"items": []}}

    monkeypatch.setattr(TeensingDataConnector, "_http_get", fake_get)
    c = TeensingDataConnector(base_url="https://saas.example.com/api/v1")
    c.fetch_realtime_kpi(token="OPERATOR_JWT_abc")
    assert captured["path"] == "/overview/realtime-kpi"
    assert captured["token"] == "OPERATOR_JWT_abc"


def test_realtime_calculated_loss_is_dict_not_scalar():
    """回归：_drop_context / _gap_context 必须把 calculated_loss 写成 dict
    {"loss_per_hour_usd": ...}，否则 enrich_with_rule_engine 会对 float 调 .get()
    抛 AttributeError。契约见 contracts.py:174。"""
    from tess_backend.orchestrator import run_diagnosis

    raw = {
        "code": 0,
        "message": "success",
        "data": {
            "items": [
                # 同比暴跌 -> _drop_context
                {"hour": "00", "today_revenue": 900.0, "today_clicks": 100,
                 "yesterday_revenue": 1500.0, "yesterday_clicks": 100, "yesterday_conversions": 50},
                # 连续掉零 -> _gap_context
                {"hour": "01", "today_revenue": 0.0, "today_clicks": 0,
                 "yesterday_revenue": 800.0, "yesterday_clicks": 0, "yesterday_conversions": 0},
                {"hour": "02", "today_revenue": 0.0, "today_clicks": 0,
                 "yesterday_revenue": 700.0, "yesterday_clicks": 0, "yesterday_conversions": 0},
            ]
        },
    }
    ctxs = extract_realtime_anomalies(raw, as_of_hour=23, grace_hours=0)
    assert ctxs, "应至少提取出一条 realtime 异常"

    for ctx in ctxs:
        cl = (ctx.get("anomaly_metadata") or {}).get("calculated_loss")
        # 必须是 dict 形状，不能是标量 float
        assert isinstance(cl, dict) and "loss_per_hour_usd" in cl, (
            f"calculated_loss 必须是 dict，实际: {cl!r}"
        )

    # 端到端：run_diagnosis 不应因标量 calculated_loss 抛 AttributeError
    mock_llm = MockLLMClient(
        {
            "status": STATUS_DIAGNOSED,
            "confidence": 0.9,
            "summary": "demo",
            "primary_contributor_id": (ctxs[0]["top_contributors"][0]["dimension_value"]),
            "root_cause_analysis": {"primary_factor": "x", "causal_chain": ["a"]},
        }
    )
    for ctx in ctxs:
        diag = run_diagnosis(ctx, mock_llm)  # 不抛异常即通过
        assert diag.get("status") in (
            "DIAGNOSED",
            "DIAGNOSED_SUSPECT",
            "INCONCLUSIVE",
        )
