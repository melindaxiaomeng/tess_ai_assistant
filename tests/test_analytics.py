"""analytics.py 新增分析类型与路由单测。

覆盖：
- 4 个新分析类型（account_profit_rollup / am_leaderboard / advertisers_missing_owner / metric_ranking）
  的取数逻辑与缺字段/空数据/正常路径；
- 关键词路由（infer_analysis_type）优先级（am_leaderboard 须先于 advertiser_deepdive）；
- 全量分页扫描（_scan_list_pages）与 am/bd 字段归一化（_coerce_owner_id）；
- 端到端 process_question 走关键词 -> 深度取数 -> LLM 提示词。
- 注册面：ANALYSIS_TYPES / app.SUPPORTED / tool_schemas.json 枚举同步。

用 MockConnector 替代真实 Teensing 接口（仅实现 api_get），无需网络/LLM。
"""

import json
from pathlib import Path

import pytest

from tess_backend import analytics
from tess_backend.analytics import (
    ANALYSIS_TYPES,
    fetch_bi_analysis_context,
    infer_analysis_type,
    process_question,
)


# ---------------------------------------------------------------------------
# Mock 基础设施
# ---------------------------------------------------------------------------

class MockConnector:
    """按 path + params 返回罐头数据，模拟 TeensingDataConnector.api_get。"""

    def __init__(self, advertisers=None, users=None, report_by_dim=None, report_by_ids=None):
        self.advertisers = advertisers or []
        self.users = users or []
        self.report_by_dim = report_by_dim or {}      # dimensions -> list[rows]
        self.report_by_ids = report_by_ids or {}     # advertiser_ids csv -> list[rows]
        self.calls = []

    def api_get(self, path, params=None, token=None):
        self.calls.append((path, dict(params or {})))
        if path == "/advertisers":
            pg = int((params or {}).get("page", 1))
            size = int((params or {}).get("page_size", 100))
            start = (pg - 1) * size
            chunk = self.advertisers[start:start + size]
            return {"total": len(self.advertisers), "items": chunk}
        if path == "/users/options":
            return self.users
        if path == "/report":
            dim = (params or {}).get("dimensions")
            if dim == "date":
                return {"items": self.report_by_dim.get("date", [])}
            if dim == "campaign":
                return {"items": self.report_by_dim.get("campaign", [])}
            # advertiser 维度：按 advertiser_ids 命中预设聚合
            aids = (params or {}).get("advertiser_ids")
            return {"items": self.report_by_ids.get(aids, [])}
        return None


class MockLLM:
    def __init__(self):
        self.last_system = None
        self.last_user = None
        self.calls = 0

    def complete(self, system, user, json_mode=False):
        self.last_system = system
        self.last_user = user
        self.calls += 1
        return "（Mock LLM 简报）"


# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------

ADVERTISERS = [
    {"id": 1001, "name": "Adv A", "am": 118, "status": 1},
    {"id": 1002, "name": "Adv B", "am": 118, "status": 1},
    {"id": 1003, "name": "Adv C", "am": None, "status": 1},   # 缺 AM
    {"id": 1004, "name": "Adv D", "am": 200, "status": 1},
    {"id": 1005, "name": "Adv E", "am": 0, "status": 1},     # 0 视为缺 AM
]

USERS = [
    {"id": 118, "real_name": "Betty"},
    {"id": 200, "name": "Tom"},
]

# am_leaderboard：按 advertiser_ids 聚合
REPORT_BY_IDS = {
    "1001,1002": [{"advertiser_id": 1001, "revenue": 500, "profit": 80, "clicks": 100, "conversions": 20},
                  {"advertiser_id": 1002, "revenue": 300, "profit": 40, "clicks": 80, "conversions": 10}],
    "1004": [{"advertiser_id": 1004, "revenue": 700, "profit": 200, "clicks": 200, "conversions": 30}],
}

REPORT_DATE = [
    {"date": "2026-09-20", "revenue": 1000, "profit": 100, "clicks": 500, "conversions": 50},
    {"date": "2026-09-21", "revenue": 1200, "profit": 120, "clicks": 600, "conversions": 60},
]

# metric_ranking 测试数据集：11 个中庸 campaign（点击 100 / 转化 10 / CVR 0.1）
# + 2 个特殊：900=超高点击低转化(CVR 0.002)、901=超高点击正常转化(CVR 0.1)
_REPORT_CAMPAIGN_FILLER = [
    {"campaign_id": i, "campaign_name": f"F{i}", "clicks": 100, "conversions": 10,
     "revenue": 50, "profit": 5}
    for i in range(10, 21)
]
REPORT_CAMPAIGN = _REPORT_CAMPAIGN_FILLER + [
    {"campaign_id": 900, "campaign_name": "HighClickLowCvr", "clicks": 5000, "conversions": 10,
     "revenue": 500, "profit": 20},   # CVR 0.002 —— 高点击低转化目标
    {"campaign_id": 901, "campaign_name": "HighClickNormalCvr", "clicks": 5000, "conversions": 500,
     "revenue": 500, "profit": 200},   # CVR 0.1  —— 高点击但转化正常（不应进高点击低转化）
]


def _connector(**overrides):
    kw = dict(advertisers=ADVERTISERS, users=USERS,
              report_by_dim={"date": REPORT_DATE, "campaign": REPORT_CAMPAIGN},
              report_by_ids=REPORT_BY_IDS)
    kw.update(overrides)
    return MockConnector(**kw)


# ---------------------------------------------------------------------------
# 1) 4 个新分析类型取数逻辑
# ---------------------------------------------------------------------------

def test_account_profit_rollup_sums_daily():
    ctx = fetch_bi_analysis_context(_connector(), "account_profit_rollup", token="t", params={"days": 14})
    assert ctx["analysis_type"] == "account_profit_rollup"
    assert ctx["total"]["revenue"] == 2200
    assert ctx["total"]["profit"] == 220
    assert len(ctx["daily"]) == 2
    assert ctx["errors"] == []


def test_account_profit_rollup_empty_report_is_honest():
    conn = _connector(report_by_dim={"date": [], "campaign": []})
    ctx = fetch_bi_analysis_context(conn, "account_profit_rollup", token="t", params={})
    assert ctx["total"]["revenue"] == 0
    # 上游无数据时不臆测，errors 标记（LLM 会据此如实告知数据不足）
    assert ctx["daily"] == []


def test_am_leaderboard_ranks_owners_and_resolves_names():
    ctx = fetch_bi_analysis_context(_connector(), "am_leaderboard", token="t", params={"days": 7})
    assert ctx["analysis_type"] == "am_leaderboard"
    board = ctx["leaderboard"]
    # 按利润降序：200(Tom, 120) 应排第一，118(Betty, 120) 第二
    assert board[0]["owner_user_id"] == "200"
    assert board[0]["owner_name"] == "Tom"
    assert board[0]["profit"] == 200
    assert board[1]["owner_user_id"] == "118"
    assert board[1]["owner_name"] == "Betty"
    assert board[1]["profit"] == 120  # 80+40
    # 仅统计有 am 的广告主（2 个 owner）
    assert ctx["total_owners_scanned"] == 2


def test_advertisers_missing_owner_finds_unconfigured():
    ctx = fetch_bi_analysis_context(_connector(), "advertisers_missing_owner", token="t", params={})
    assert ctx["analysis_type"] == "advertisers_missing_owner"
    assert ctx["scanned_total"] == 5
    assert ctx["missing_owner_count"] == 2  # 1003(None), 1005(0)
    ids = {m["advertiser_id"] for m in ctx["missing_advertisers"]}
    assert ids == {"1003", "1005"}


def test_metric_ranking_flags_high_click_low_cvr():
    ctx = fetch_bi_analysis_context(_connector(), "metric_ranking", token="t", params={"days": 7})
    assert ctx["analysis_type"] == "metric_ranking"
    top_ids = [c["campaign_id"] for c in ctx["top_by_clicks"]]
    lowest = ctx["lowest_cvr"]
    # 点击榜首必为 5000 点击的 900 或 901 之一
    assert ctx["top_by_clicks"][0]["campaign_id"] in ("900", "901")
    # 转化最低榜首 = 900（CVR 0.002，全场最低）
    assert lowest[0]["campaign_id"] == "900"
    # 高点击低转化：900 应命中；901 虽高点击但 CVR 正常（0.1）不应命中
    high_low = {c["campaign_id"] for c in ctx["high_click_low_cvr"]}
    assert "900" in high_low
    assert "901" not in high_low
    # CVR 已计算
    cvr_map = {c["campaign_id"]: c["cvr"] for c in ctx["top_by_clicks"] + ctx["lowest_cvr"]}
    assert cvr_map["900"] == 0.002


# ---------------------------------------------------------------------------
# 2) 全量分页扫描 & am/bd 归一化
# ---------------------------------------------------------------------------

def test_scan_list_pages_paginates():
    big = [{"id": i, "name": f"A{i}", "am": 1} for i in range(250)]
    conn = MockConnector(advertisers=big)
    items = analytics._scan_list_pages(conn, "/advertisers", {}, token="t", max_pages=30)
    assert len(items) == 250


def test_coerce_owner_id_variants():
    assert analytics._coerce_owner_id(None) is None
    assert analytics._coerce_owner_id(0) is None
    assert analytics._coerce_owner_id("") is None
    assert analytics._coerce_owner_id(118) == 118
    assert analytics._coerce_owner_id("200") == 200
    assert analytics._coerce_owner_id({"id": 305}) == 305
    assert analytics._coerce_owner_id({"user_id": 99}) == 99


# ---------------------------------------------------------------------------
# 3) 关键词路由优先级
# ---------------------------------------------------------------------------

def test_new_keywords_route_correctly():
    assert infer_analysis_type("帮我看看哪些广告主没有配置am") == "advertisers_missing_owner"
    assert infer_analysis_type("最近两周的利润怎么样") == "account_profit_rollup"
    assert infer_analysis_type("点击最大但是转化率最低的是谁") == "metric_ranking"


def test_am_leaderboard_wins_over_advertiser_deepdive():
    # 「哪个AM负责的客户收入利润最高」会同时命中 advertiser_deepdive 的「客户」词，
    # 但 am_leaderboard 必须优先（列表顺序在 advertiser_deepdive 之前）。
    assert infer_analysis_type("哪个AM负责的客户收入利润最高") == "am_leaderboard"


def test_new_types_registered():
    for t in ("account_profit_rollup", "am_leaderboard", "advertisers_missing_owner", "metric_ranking"):
        assert t in ANALYSIS_TYPES


# ---------------------------------------------------------------------------
# 4) 端到端 process_question：关键词 -> 深度取数 -> LLM 提示词
# ---------------------------------------------------------------------------

def test_process_question_routes_to_missing_owner():
    llm = MockLLM()
    res = process_question("哪些广告主没有配置am", _connector(), llm, token="t")
    assert res["context_summary"]["analysis_type"] == "advertisers_missing_owner"
    assert res["context_summary"]["route_source"] == "inferred"
    assert llm.calls == 1
    # LLM 收到的上下文应包含缺失清单
    assert "missing_advertisers" in llm.last_user


def test_process_question_routes_to_am_leaderboard_not_advertiser():
    llm = MockLLM()
    res = process_question("哪个AM负责的客户收入利润最高", _connector(), llm, token="t")
    assert res["context_summary"]["analysis_type"] == "am_leaderboard"
    assert "leaderboard" in llm.last_user


# ---------------------------------------------------------------------------
# 5) 注册面同步
# ---------------------------------------------------------------------------

def test_app_supported_includes_new_types():
    from tess_backend import app as app_module
    for t in ("account_profit_rollup", "am_leaderboard", "advertisers_missing_owner", "metric_ranking"):
        assert t in app_module.SUPPORTED


def test_tool_schemas_enum_includes_new_types():
    schema_path = Path(__file__).parent.parent / "tess_backend" / "tool_schemas.json"
    schemas = json.loads(schema_path.read_text(encoding="utf-8"))
    enums = [s["function"]["parameters"]["properties"]["analysis_type"]["enum"]
             for s in schemas if s["function"]["name"] in ("tess_analyze", "tess_ask")]
    for t in ("account_profit_rollup", "am_leaderboard", "advertisers_missing_owner", "metric_ranking"):
        assert all(t in e for e in enums), f"{t} 未在 tool_schemas.json 枚举中"
