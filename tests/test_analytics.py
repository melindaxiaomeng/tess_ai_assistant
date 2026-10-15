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
from datetime import datetime
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

    def __init__(self, advertisers=None, users=None, report_by_dim=None, report_by_ids=None, daily_kpi=None):
        self.advertisers = advertisers or []
        self.users = users or []
        self.report_by_dim = report_by_dim or {}      # dimensions -> list[rows]
        self.report_by_ids = report_by_ids or {}     # advertiser_ids csv -> list[rows]
        self.daily_kpi = daily_kpi or []              # /overview/daily-kpi 返回的账户级日汇总
        self.calls = []

    def api_get(self, path, params=None, token=None, timeout=None):
        self.calls.append((path, dict(params or {})))
        if path == "/advertisers":
            pg = int((params or {}).get("page", 1))
            size = int((params or {}).get("page_size", 100))
            start = (pg - 1) * size
            chunk = self.advertisers[start:start + size]
            return {"total": len(self.advertisers), "items": chunk}
        if path == "/users/options":
            return self.users
        if path == "/overview/ranking/fluctuation":
            # 默认返回空（触发 /report 回退）；测试可 override api_get 来模拟成功
            return {"rising": [], "falling": []}
        if path == "/overview/daily-kpi":
            return self.daily_kpi
        if path == "/report":
            dim = (params or {}).get("dimensions")
            if dim == "date":
                return {"items": self.report_by_dim.get("date", [])}
            if dim == "campaign":
                return {"items": self.report_by_dim.get("campaign", [])}
            if dim == "date,campaign":
                # /report 维度聚合：按 campaign_ids 命中预设的 campaign × 月 扁平表
                cids = [c for c in ((params or {}).get("campaign_ids") or "").split(",") if c]
                if cids:
                    months = ["2026-07-31", "2026-08-31", "2026-09-30", "2026-10-31"]
                    items = []
                    for ci, cc in enumerate(cids):
                        for mi, m in enumerate(months):
                            items.append({
                                "date": m, "campaign_id": cc, "campaign_name": f"Camp {cc}",
                                "revenue": 100 + mi * 20 + ci, "profit": 40 + mi * 5,
                                "payout": 50 + mi * 3, "clicks": 500 + mi * 50,
                                "conversions": 50 + mi * 5, "cr": round(0.1 + mi * 0.01, 3),
                                "cvr": round(0.1 + mi * 0.01, 3), "margin_rate": 0.4,
                            })
                    return {"items": items}
                # campaign_ranking：按 date_start/date_end 生成两天数据
                # （prev 高、curr 低，模拟下滑，便于断言 profit_change）
                ds = (params or {}).get("date_start", "2026-10-13")
                de = (params or {}).get("date_end", "2026-10-14")
                items = []
                for d in (ds, de):
                    is_prev = (d == ds)
                    base = {"revenue": 1000 if is_prev else 800,
                            "profit": 200 if is_prev else 100}
                    for cid in ("8001", "8002", "8003"):
                        items.append({
                            "date": d, "campaign_id": cid, "campaign_name": f"Camp {cid}",
                            "revenue": base["revenue"], "profit": base["profit"],
                        })
                return {"items": items}
            if dim == "date,hour,campaign":
                # campaign_ranking 回退场景：按 date 返回两天的 campaign 小时明细
                ds = (params or {}).get("date_start", "2026-10-13")
                de = (params or {}).get("date_end", "2026-10-14")
                items = []
                for d in (ds, de):
                    for cid in ("8001", "8002", "8003"):
                        base = {"2026-10-13": {"revenue": 1000, "profit": 200},
                                "2026-10-14": {"revenue": 800, "profit": 100}}[d]
                        # 3 个小时行，汇总后等于 base
                        for h in range(3):
                            items.append({
                                "date": d, "hour": h, "campaign_id": cid,
                                "campaign_name": f"Camp {cid}",
                                "revenue": base["revenue"] / 3, "profit": base["profit"] / 3,
                            })
                return {"items": items}
            # advertiser 维度：按 advertiser_ids 命中预设聚合
            aids = (params or {}).get("advertiser_ids")
            return {"items": self.report_by_ids.get(aids, [])}
        if path == "/advertisers/campaign-daily-kpi":
            aid = (params or {}).get("advertiser_id")
            # 返回 15 个 campaign 以覆盖长周期『只拉前 8 个趋势』分支
            camps = [{"campaign_id": 9000 + i, "revenue": 100 * (16 - i)} for i in range(1, 16)]
            return {
                "advertiser_id": aid,
                "start_date": (params or {}).get("date_start", "2026-10-07"),
                "end_date": (params or {}).get("date_end", "2026-10-13"),
                "total": {"revenue": 1234, "profit": 567},
                "campaigns": camps,
            }
        if path == "/campaign-kpi-trend":
            # 逐 Campaign 趋势；campaign_ids 为单 id（advertiser_deepdive 逐条并发拉）
            # 返回跨 4 个月（7-10 月）的代表点，验证长周期月度聚合
            cid = (params or {}).get("campaign_ids")
            months = ["2026-07-31", "2026-08-31", "2026-09-30", "2026-10-31"]
            if cid:
                items = [{"time_label": m, "revenue": 100 + i * 10, "clicks": 50 + i * 5,
                          "cvr": round(0.1 + i * 0.01, 2), "margin_rate": 0.4, "payout": 60}
                         for i, m in enumerate(months)]
                return {"items": items}
            return {"items": []}
        if path.startswith("/advertisers/"):
            # /advertisers/{id} 档案查询（advertiser_deepdive 用）
            aid = path.split("/")[-1]
            rec = next((a for a in self.advertisers if str(a.get("id")) == str(aid)), None)
            return rec or {"id": aid, "name": f"Adv {aid}", "status": 1}
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

    def stream(self, system, user):
        self.last_system = system
        self.last_user = user
        self.calls += 1
        text = "（Mock LLM 简报）"
        for i in range(0, len(text), 3):
            yield text[i:i + 3]


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

# account_profit_rollup 现走 /overview/daily-kpi（固定近 7 日），日期需在测试运行时的 7 日窗口内
DAILY_KPI = [
    {"date": "2026-10-08", "revenue": 1000, "profit": 100, "payout": 500, "clicks": 500, "conversions": 50},
    {"date": "2026-10-09", "revenue": 1200, "profit": 120, "payout": 600, "clicks": 600, "conversions": 60},
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
              report_by_ids=REPORT_BY_IDS,
              daily_kpi=DAILY_KPI)
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
    conn = _connector(daily_kpi=[])
    ctx = fetch_bi_analysis_context(conn, "account_profit_rollup", token="t", params={})
    assert ctx["total"]["revenue"] == 0
    # 上游无数据时不臆测，daily 为空（LLM 会据此如实告知数据不足）
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


def test_process_question_compound_multi_dispatch():
    """复合问题（一句含 4 个意图）应走 inferred_multi，逐类型取数并合并上下文。"""
    llm = MockLLM()
    q = ("帮我总结一下最近两周的利润，然后看看哪个AM负责的客户收入利润最高，"
         "以及点击最大但是转化率最低；帮我看看哪些广告主没有配置am")
    res = process_question(q, _connector(), llm, token="t")
    cs = res["context_summary"]
    assert cs["route_source"] == "inferred_multi"
    mts = set(cs["multi_types"])
    # 4 个意图应全部命中：利润汇总 / AM 排名 / 点击转化极端 / 缺 AM 广告主
    assert {"account_profit_rollup", "am_leaderboard",
            "metric_ranking", "advertisers_missing_owner"} <= mts
    # LLM 应收到合并后的多份上下文（每段都带【深度下钻：...】标记）
    assert llm.last_user.count("【深度下钻：") == len(mts)
    assert "account_profit_rollup" in llm.last_user
    assert "am_leaderboard" in llm.last_user
    assert "metric_ranking" in llm.last_user
    assert "advertisers_missing_owner" in llm.last_user


def test_process_question_single_intent_still_single_route():
    """单意图问题不应被误判为复合，保持原 inferred 路由（回归保护）。"""
    llm = MockLLM()
    res = process_question("哪些广告主没有配置am", _connector(), llm, token="t")
    assert res["context_summary"]["route_source"] == "inferred"
    assert res["context_summary"]["analysis_type"] == "advertisers_missing_owner"


def test_scan_list_pages_merges_multiple_pages():
    """_scan_list_pages 并发扫页后仍完整合并所有 items（含多页 + 末页不满）。"""
    # 250 个广告主 -> 3 页（100 + 100 + 50）
    advs = [{"id": i, "name": f"A{i}", "am": (i % 3) + 100} for i in range(250)]
    c = MockConnector(advertisers=advs)
    items = analytics._scan_list_pages(c, "/advertisers", {}, "t", max_pages=30)
    assert len(items) == 250
    # 顺序不强制，但内容应全
    ids = {it["id"] for it in items}
    assert ids == set(range(250))


def test_compound_dispatch_survives_subtype_failure():
    """复合分发的任一子类型取数异常时，应被捕获（转 errors）而非让整轮崩溃。"""
    class BoomConnector(MockConnector):
        def api_get(self, path, params=None, token=None, timeout=None):
            raise RuntimeError("upstream boom")

    llm = MockLLM()
    # 多意图：应包含 account_profit_rollup + am_leaderboard 等多个子类型
    res = process_question(
        "最近两周的利润，哪个AM负责的客户收入利润最高，点击最大但转化最低的，哪些广告主没配AM",
        BoomConnector(), llm, token="t",
    )
    assert res["context_summary"]["route_source"] == "inferred_multi"
    # 不抛异常，answer 正常返回；errors 应记录到取数失败
    assert res["answer"]
    assert res["context_summary"]["errors"]


# ---------------------------------------------------------------------------
# 4.5) SSE 流式回调：on_progress / on_context_ready / on_token
# ---------------------------------------------------------------------------

def test_process_question_compound_emits_progress_and_tokens():
    """复合问题：on_progress 应覆盖每个子类型（index/total），on_token 应逐块推送。"""
    llm = MockLLM()
    progress = []
    tokens = []

    def on_progress(stage, label, index, total):
        progress.append((stage, index, total))

    def on_token(text):
        tokens.append(text)

    q = ("帮我总结一下最近两周的利润，然后看看哪个AM负责的客户收入利润最高，"
         "以及点击最大但是转化率最低；帮我看看哪些广告主没有配置am")
    res = process_question(q, _connector(), llm, token="t",
                           on_progress=on_progress, on_token=on_token)
    cs = res["context_summary"]
    assert cs["route_source"] == "inferred_multi"
    n = len(cs["multi_types"])
    # 每个子类型各推一次 progress，total 均为 n
    assert len(progress) == n
    assert all(t == n for _, _, t in progress)
    # index 应为 1..n 覆盖
    assert {idx for _, idx, _ in progress} == set(range(1, n + 1))
    # on_token 被调用，且拼接后等于最终 answer
    assert tokens
    assert "".join(tokens) == res["answer"]


def test_process_question_single_route_emits_one_progress():
    """单意图问题：on_progress 应只推一次（index=1,total=1）。"""
    llm = MockLLM()
    progress = []
    res = process_question("哪些广告主没有配置am", _connector(), llm, token="t",
                           on_progress=lambda s, l, i, t: progress.append((s, i, t)))
    assert res["context_summary"]["route_source"] == "inferred"
    assert len(progress) == 1
    assert progress[0][0] == "advertisers_missing_owner"
    assert progress[0][1] == 1 and progress[0][2] == 1


def test_process_question_stream_falls_back_without_stream():
    """llm 无 stream 方法时（仅 complete），不应调用 on_token，但 answer 正常产出。"""
    class NoStreamLLM:
        def complete(self, system, user, json_mode=False):
            return "plain answer"

    tokens = []
    res = process_question("哪些广告主没有配置am", _connector(), NoStreamLLM(), token="t",
                           on_token=lambda t: tokens.append(t))
    assert res["answer"] == "plain answer"
    assert tokens == []  # 无 stream 方法 -> 不推 token


def test_tess_agent_mock_client_stream_yields_and_joins():
    """tess_agent.MockLLMClient.stream 应逐块吐字，拼接后等于 complete。"""
    from tess_backend.tess_agent import MockLLMClient

    client = MockLLMClient({"answer": "hello world"})
    chunks = list(client.stream("sys", "user"))
    assert "".join(chunks) == client.complete("sys", "user")
    assert len(chunks) >= 1  # 至少切成一块


def test_tess_agent_http_client_has_stream():
    """HttpLLMClient 应提供 stream 方法（真实流式由线上验证，这里只校验可调用）。"""
    from tess_backend.tess_agent import HttpLLMClient

    c = HttpLLMClient("https://api.deepseek.com/v1", "fake-key", "deepseek-chat")
    assert callable(getattr(c, "stream", None))


# ---------------------------------------------------------------------------
# 6) /tess/ask 端点：SSE 内容协商 + 帧顺序（HTTP 层）
# ---------------------------------------------------------------------------

def test_extract_advertiser_id_digit_before_keyword():
    """回归：『数字 + 这个广告主』应抽出 advertiser_id，而非误当名称。

    复现线上 bug：问『1000908这个广告主…』返回『数据不足』——
    根因是 extract_entities 只支持『广告主1000908』(关键字在前) 与
    『1000908 advertiser』(英文)，不支持『数字+中文关键字』语序，
    导致 advertiser_id 一直为 None、1000908 被当 advertiser_name 走名称解析失败。
    """
    from tess_backend.analytics import extract_entities
    for q in ("1000908这个广告主，最近一周有什么变化？还有什么增长空间？",
              "1000908的广告主最近有增长空间吗",
              "1000908 广告主 环比"):
        e = extract_entities(q, {})
        assert e.get("advertiser_id") == "1000908", q
        assert e.get("advertiser_name") is None, q  # 不应误填名称


def test_extract_date_range_month_list():
    """回归：『7,8,9月和10月』应抽出 date_start/date_end，覆盖长周期下钻盲区。

    复现线上 bug：问『广告主1000853,7,8,9月和10月…』时系统不识别月份范围，
    取数按默认范围拉全量，4 个月数据触发 80s 超时降级『数据不足』。
    """
    from tess_backend.analytics import extract_entities
    e = extract_entities("广告主1000853，7,8,9月和10月，campaign维度数据有什么变化？", {})
    assert e.get("advertiser_id") == "1000853"
    # 当年 7 月 1 日 ~ 10 月 31 日
    assert e.get("date_start") == f"{datetime.now().year}-07-01"
    assert e.get("date_end") == f"{datetime.now().year}-10-31"


def test_extract_date_range_last_n_months():
    """『最近3个月』应抽到相对当前日期的范围。"""
    from tess_backend.analytics import extract_entities
    e = extract_entities("最近3个月的整体表现", {})
    assert e.get("date_start") and e.get("date_end")
    ds = datetime.strptime(e["date_start"], "%Y-%m-%d")
    assert (datetime.now() - ds).days >= 85


class ReportFirstConnector(MockConnector):
    """模拟真实上游对长周期下钻的支持：/report 批量聚合全区间汇总 + 日明细；

    campaign-daily-kpi 走 advertiser_id 只回近期活跃 campaign（9001-9015），
    其余头部 campaign（9016-9030）由步骤3 的 campaign_ids 过滤回补 campaign_status_name
    （修复『只出 8 个』与『status 全 null』两个痛点）。"""

    def api_get(self, path, params=None, token=None, timeout=None):
        self.calls.append((path, dict(params or {})))
        p = params or {}
        if path == "/advertisers/campaign-daily-kpi":
            aid = p.get("advertiser_id")
            p_ids = [c for c in (p.get("campaign_ids") or "").split(",") if c]
            if p_ids:
                # campaign_ids 过滤（步骤3 状态回填专用）：返回这些 campaign 的逐日行，
                # 每行带人类可读 campaign_status_name —— 模拟真实上游对该接口的行为。
                camps = [{"campaign_id": int(c), "campaign_name": f"Camp {c}",
                          "campaign_status_name": "Stop" if int(c) % 2 else "Active"}
                         for c in p_ids]
                return {"advertiser_id": aid, "campaigns": camps, "total": {"revenue": 1}}
            camps = [{"campaign_id": 9000 + i, "name": f"Camp {9000 + i}",
                      "campaign_status_name": "Active" if i % 2 == 0 else "Paused",
                      "revenue": 100 * (16 - i)} for i in range(1, 16)]
            return {"advertiser_id": aid, "campaigns": camps,
                    "total": {"revenue": 1234, "profit": 567}}
        if path == "/campaigns":
            cids = [c for c in (p.get("campaign_ids") or "").split(",") if c]
            # 模拟真实 /campaigns：只回 status 数字（status_name 恒空），由 _camp_status_label 映射
            items = [{"campaign_id": int(c), "campaign_name": f"Camp {c}",
                      "status": 1 if int(c) % 2 == 0 else 3} for c in cids]
            return {"items": items}
        if path == "/report":
            dim = p.get("dimensions")
            if dim == "campaign" and p.get("advertiser_ids"):
                # 全区间汇总：top30 campaign（含 9001），按 revenue 排序；尊重分页（避免重复）
                items = [{"campaign_id": 9000 + i, "campaign_name": f"Camp {9000 + i}",
                          "revenue": 100 * (16 - i), "profit": 10 * (16 - i),
                          "payout": 5 * (16 - i), "clicks": 50, "conversions": 5}
                         for i in range(1, 31)]
                pg = int(p.get("page", 1))
                sz = int(p.get("page_size", 100))
                return {"items": items[(pg - 1) * sz: pg * sz]}
            if dim == "campaign,date" and p.get("campaign_ids"):
                cids = [c for c in p.get("campaign_ids").split(",") if c]
                months = ["2026-07-31", "2026-08-31", "2026-09-30", "2026-10-31"]
                items = []
                for ci, cc in enumerate(cids):
                    for d in months:
                        items.append({"date": d, "campaign_id": int(cc),
                                      "revenue": 100 + ci, "clicks": 50,
                                      "cr": 0.1, "margin": 0.4, "payout": 60})
                pg = int(p.get("page", 1))
                sz = int(p.get("page_size", 100))
                return {"items": items[(pg - 1) * sz: pg * sz]}
            return {"items": []}
        if path == "/campaign-kpi-trend":
            return {"items": []}  # 新策略长周期不再用，返回空以验证未被调用
        return super().api_get(path, params, token, timeout)


def test_advertiser_deepdive_long_span_monthly_aggregation():
    """长周期（>35天）下钻：用 /report 批量聚合（一次覆盖头部 ~30 campaign 全区间），

    本地 _monthly_from_daily 把日明细聚合成月度点，并用 /campaigns 主数据补权威
    campaign_status_name。修复线上『只出 8 个 campaign』与『status 全 null』两个痛点。
    """
    from tess_backend.analytics import (
        fetch_bi_analysis_context,
        _advertiser_deepdive_prompt_view,
    )
    conn = ReportFirstConnector(
        advertisers=_connector().advertisers,
        users=_connector().users,
        report_by_dim=_connector().report_by_dim,
        report_by_ids=_connector().report_by_ids,
    )
    ctx = fetch_bi_analysis_context(
        conn, "advertiser_deepdive", token="t",
        params={"advertiser_id": "1000853", "date_start": "2026-07-01", "date_end": "2026-10-31"},
    )
    assert ctx["date_range"] == "2026-07-01 ~ 2026-10-31"
    assert ctx["long_span"] is True
    # 长周期走 /report 批量聚合，头部 ~30 个 campaign 全部进入趋势（不再是旧的 8 个截断）
    assert "9001" in ctx["campaign_trends"], "应含头部 campaign 9001 的月度趋势"
    assert len(ctx["campaign_trends"]) >= 30, f"应聚合头部 ~30 个 campaign，实际 {len(ctx['campaign_trends'])}"
    series = ctx["campaign_trends"]["9001"]
    # 趋势按月聚合（4 个月 -> 4 个代表点）
    assert len(series) == 4, f"长周期趋势应聚合为 4 个月点，实际 {len(series)}"
    assert series[0]["time_label"] == "2026-07-31"
    assert series[-1]["time_label"] == "2026-10-31"

    # 回归：长周期必须走 /report 批量聚合（一次覆盖 30 campaign 全区间），
    # 不再逐 campaign 逐月打 /campaign-kpi-trend（旧策略会因上游只回最近 7 天而 7/8/9 月全空）。
    report_calls = [(p, pr) for (p, pr) in conn.calls if p == "/report"]
    trend_calls = [(p, pr) for (p, pr) in conn.calls if p == "/campaign-kpi-trend"]
    assert report_calls, "长周期应调用 /report 批量聚合"
    assert trend_calls == [], f"长周期新策略不应调用 /campaign-kpi-trend 逐月，实际 {len(trend_calls)}"

    # 回归（用户截图痛点）：头部全部 campaign 的 status 不应再为 null ——
    # 近期活跃者来自 campaign-daily-kpi 快照，停投但有营收者由步骤3 campaign_ids 回填补上。
    view = _advertiser_deepdive_prompt_view(ctx)
    snap = {str(c["campaign_id"]): c for c in view["campaigns_snapshot"]}
    assert len(snap) >= 30, f"快照应含全部头部 ~30 campaign，实际 {len(snap)}"
    null_status = [cid for cid, c in snap.items() if not c.get("status")]
    assert not null_status, f"头部 campaign status 不应为 null，仍有 null 的: {null_status}"


def test_advertiser_deepdive_prompt_view_keeps_monthly_trends_before_truncation():
    """回归：advertiser_deepdive 的 LLM prompt 视图必须把『月度趋势』排在最前、

    把日级 KPI 压缩为近期快照，否则日级明细会把截断预算吃光、把真正的月度对比数据裁掉
    （线上 1000853：daily_kpi 快照占满 14000 字，campaign_trends 被截断在 Ajaib，
    LLM 误报 7/8/9 月无数据）。本测试用 15 个 campaign × 100 行日级明细模拟 bloated 日 KPI，
    断言精简后趋势存活、且 14000 字截断后最后一个 campaign 的月度点仍在。
    """
    from tess_backend.analytics import _advertiser_deepdive_prompt_view, _trim_for_prompt

    # 模拟真实上游：daily_kpi.campaigns 带大量日级明细行；campaign_trends 为月度聚合
    bloated_campaigns = []
    for i in range(1, 16):
        cid = 9000 + i
        daily_rows = [
            {"date": f"2026-10-{d:02d}", "revenue": 10, "clicks": 5, "conversions": 1,
             "payout": 4, "cvr": 0.2, "margin_rate": 0.4, "impressions": 100,
             "note": "x" * 50}  # 长备注模拟冗余字段
            for d in range(1, 15)  # 14 天日级明细
        ]
        bloated_campaigns.append({
            "campaign_id": cid, "name": f"Camp {cid}", "status": 1,
            "revenue": 100 * i, "clicks": 50 * i, "conversions": 5 * i,
            "payout": 40 * i, "daily": daily_rows,
        })
    monthly_trends = {}
    for i in range(1, 16):
        cid = 9000 + i
        monthly_trends[str(cid)] = [
            {"time_label": m, "revenue": 100 + k * 10, "clicks": 50 + k * 5,
             "cvr": 0.1, "margin_rate": 0.4, "payout": 60}
            for k, m in enumerate(["2026-07-31", "2026-08-31", "2026-09-30", "2026-10-31"])
        ]
    raw_ctx = {
        "analysis_type": "advertiser_deepdive",
        "advertiser_id": "1000853",
        "date_range": "2026-07-01 ~ 2026-10-31",
        "long_span": True,
        "profile": {"id": "1000853", "name": "Flymobi_API_NEW"},
        "daily_kpi": {"campaigns": bloated_campaigns},
        "campaign_trends": monthly_trends,
        "errors": [],
    }
    # 原始序列化（含日级明细）体量巨大
    raw_text = _trim_for_prompt(raw_ctx, limit=14000)
    # 精简视图：月度趋势最前、日 KPI 压缩
    view = _advertiser_deepdive_prompt_view(raw_ctx)
    view_text = _trim_for_prompt(view, limit=14000)

    # 1) 月度趋势必须存在且含全部 15 个 campaign
    assert set(view["campaign_trends_monthly"].keys()) == {str(9000 + i) for i in range(1, 16)}
    # 2) 近期快照只保留 7 个紧凑字段，丢弃日级明细（无 'daily' 键）
    snap = view["campaigns_snapshot"]
    assert len(snap) == 15
    assert "daily" not in snap[0], "近期快照不应含日级明细行"
    assert set(snap[0].keys()) == {"campaign_id", "name", "status", "revenue", "clicks", "conversions", "payout"}
    # 3) 月度趋势在序列化文本中排在近期快照之前
    assert view_text.index("campaign_trends_monthly") < view_text.index("campaigns_snapshot")
    # 4) 精简后体量显著缩小（对比未截断的真实原始序列化大小）
    raw_full = len(json.dumps(raw_ctx, ensure_ascii=False, default=str))
    assert len(view_text) < raw_full * 0.5, f"精简后应显著变小：{len(view_text)} vs {raw_full}"
    # 5) 14000 字截断后，最后一个 campaign（9015）的月度点仍存活（趋势在最前）
    assert "9015" in view_text, "截断后最后一个 campaign 的月度趋势应存活"


def test_advertiser_deepdive_campaign_daily_kpi_full_interval_first_then_short_fallback():
    """回归：长周期下钻的 campaign-daily-kpi 快照源应优先全区间（ds~de，覆盖 7-10 月全部头部

    campaign 的 campaign_status_name），全区间超时才回退短区间（最近 30 天）兜底。
    验证回退路径：全区间主调用超时后，短区间兜底仍能拿到 campaign 列表并产出趋势（不丢 campaign）。
    """
    from tess_backend.analytics import fetch_bi_analysis_context

    class FullFirstFallbackConnector(MockConnector):
        def api_get(self, path, params=None, token=None, timeout=None):
            self.calls.append((path, dict(params or {})))
            if path == "/report":
                # 强制 /report 无数据 -> 走回退路径（campaign-daily-kpi 列表 + /campaign-kpi-trend）
                return {"items": []}
            if path == "/advertisers/campaign-daily-kpi":
                ds = (params or {}).get("date_start")
                if ds == "2026-07-01":  # 全区间主调用 -> 模拟后端读取超时
                    raise TimeoutError("The read operation timed out")
                # 短区间兜底（最近 30 天）-> 成功返回列表
                return {
                    "advertiser_id": (params or {}).get("advertiser_id"),
                    "campaigns": [{"campaign_id": 9001, "revenue": 100}],
                }
            return super().api_get(path, params, token, timeout)

    base = _connector()
    conn = FullFirstFallbackConnector(
        advertisers=base.advertisers, users=base.users,
        report_by_dim=base.report_by_dim, report_by_ids=base.report_by_ids,
    )
    ctx = fetch_bi_analysis_context(
        conn, "advertiser_deepdive", token="t",
        params={"advertiser_id": "1000853", "date_start": "2026-07-01", "date_end": "2026-10-31"},
    )
    kpi_calls = [(p, pr) for (p, pr) in conn.calls if p == "/advertisers/campaign-daily-kpi"]
    assert len(kpi_calls) == 2, f"全区间超时后应触发一次短区间兜底，实际 {len(kpi_calls)} 次"
    # 第一次必为全区间（date_start == 整段），第二次为短区间兜底
    assert kpi_calls[0][1]["date_start"] == "2026-07-01", "长周期应优先全区间拿快照"
    assert kpi_calls[1][1]["date_start"] != "2026-07-01", "兜底应是短区间"
    # 兜底拿到 campaign 列表后，逐 campaign /campaign-kpi-trend 应产出趋势（而非因列表空而缺失）
    assert "9001" in ctx["campaign_trends"], "兜底拿到 campaign 列表后，应产出趋势"


def test_process_question_routes_digit_first_advertiser_to_deepdive():
    """端到端：『1000908这个广告主…』应路由到 advertiser_deepdive 且带 id，而非降级『数据不足』。"""
    llm = MockLLM()
    q = "1000908这个广告主，最近一周有什么变化？还有什么增长空间？"
    res = process_question(q, _connector(), llm, token="t")
    cs = res["context_summary"]
    assert cs["route_source"] == "entity"
    assert cs["analysis_type"] == "advertiser_deepdive"
    # 实体抽取带入了 advertiser_id，下游取数不再因缺参数报错（errors 应为空）
    assert cs.get("errors") == []
    # LLM 上下文应含该广告主画像，而非『数据不足，暂无法确认』
    assert "1000908" in llm.last_user
    assert "数据不足" not in res["answer"]


def test_advertiser_deepdive_includes_campaign_trends():
    """advertiser_deepdive 应并发拉取旗下每个 Campaign 的趋势，补全 per-campaign 明细。

    回归：此前只返回聚合 KPI + 旗下 campaign 营收，LLM 因缺 per-campaign 明细
    而报『其余 Campaign 明细数据不足，暂无法确认』。
    """
    res = fetch_bi_analysis_context(_connector(), "advertiser_deepdive",
                                   token="t", params={"advertiser_id": "1000908"})
    assert res["analysis_type"] == "advertiser_deepdive"
    trends = res["campaign_trends"]
    # 旗下 9001/9002 两条 campaign 都应拉到趋势
    assert "9001" in trends and "9002" in trends
    assert trends["9001"][-1]["revenue"] == 130  # 末日营收，供 LLM 判断变化方向
    # daily_kpi 的 campaigns 列表也应保留（不再 [:10] 之外的截断导致整体缺失）
    cids = [str(c["campaign_id"]) for c in res["daily_kpi"]["campaigns"]]
    assert "9001" in cids and "9002" in cids


def test_single_type_deepdive_fetch_timeout_degrades(monkeypatch):
    """单类型深度下钻（如 advertiser_deepdive）取数若超过整体预算，须降级为
    『数据不足』错误而非让整次 HTTP 卡到 Cloudflare 100s 504（此前单类型路径无预算）。

    回归：把 TESS_ASK_TOTAL_TIMEOUT 调小到 1s，让 mock 取数 sleep 2s 触发超时，
    断言 process_question 在预算内返回、context_summary.errors 含『超时』、且仍能产出 answer。
    """
    import time as _t
    from tess_backend import analytics as _analytics

    def _slow_fetch(connector, at, token=None, params=None):
        _t.sleep(2.0)  # 故意超过 1s 预算
        return {"analysis_type": at}

    monkeypatch.setattr(_analytics, "fetch_bi_analysis_context", _slow_fetch)
    monkeypatch.setenv("TESS_ASK_TOTAL_TIMEOUT", "1")

    llm = MockLLM()
    t0 = _t.monotonic()
    res = process_question(
        "1000908 这个广告主最近一周怎样",
        _connector(), llm, token="t",
        analysis_type="advertiser_deepdive",
        params={"advertiser_id": "1000908"},
    )
    elapsed = _t.monotonic() - t0
    # 不应卡到 2s（预算 1s 即熔断），留余量给 LLM 兜底生成
    assert elapsed < 5.0, f"取数超时未生效，耗时 {elapsed:.1f}s"
    cs = res["context_summary"]
    assert cs["analysis_type"] == "advertiser_deepdive"
    assert any("超时" in (e or "") for e in (cs.get("errors") or []))
    # 即使取数超时，LLM 仍应基于『数据不足』产出 answer，而非整次 504
    assert res.get("answer")


def _parse_sse(text):
    """把 SSE 文本解析成事件 dict 列表（同前端 tessAskStream 的解析逻辑）。"""
    events = []
    buf = ""
    for raw in text.split("\n\n"):
        raw = raw.strip()
        if not raw.startswith("data:"):
            continue
        events.append(json.loads(raw[5:].strip()))
    return events


def test_ask_sse_stream_emits_ordered_frames():
    """带 Accept: text/event-stream -> SSE 流式，帧顺序 start/progress/context_ready/token/done。"""
    from tess_backend import app as app_module
    from fastapi.testclient import TestClient

    app_module._require_ai_entitlement = lambda req: None
    app_module.get_data_connector = lambda *a, **k: _connector()
    app_module._get_llm_client = lambda pid: MockLLM()
    app_module._resolve_access_token = lambda req, pid: ("", "system")
    app_module._platform_id = lambda req: "default"
    app_module._operator_id = lambda req: "anonymous"

    client = TestClient(app_module.app)
    r = client.post(
        "/tess/ask",
        json={"question": "哪些广告主没有配置am"},
        headers={"Accept": "text/event-stream"},
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    events = _parse_sse(r.text)
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert "progress" in types and types.count("progress") == 1
    assert "context_ready" in types
    assert "token" in types
    assert types[-1] == "done"
    done = [e for e in events if e["type"] == "done"][0]
    assert done["answer"]
    # token 拼接应等于 done.answer
    assert "".join(e["text"] for e in events if e["type"] == "token") == done["answer"]


def test_ask_non_stream_returns_json():
    """不带 Accept -> 保持原 JSON 返回（向后兼容 / 既有集成测试）。"""
    from tess_backend import app as app_module
    from fastapi.testclient import TestClient

    app_module._require_ai_entitlement = lambda req: None
    app_module.get_data_connector = lambda *a, **k: _connector()
    app_module._get_llm_client = lambda pid: MockLLM()
    app_module._resolve_access_token = lambda req, pid: ("", "system")
    app_module._platform_id = lambda req: "default"
    app_module._operator_id = lambda req: "anonymous"

    client = TestClient(app_module.app)
    r = client.post("/tess/ask", json={"question": "哪些广告主没有配置am"})
    assert r.status_code == 200
    # 非流式也必须走「先吐首字节」的 StreamingResponse，否则长问题零首字节被 Cloudflare 砍 524
    assert r.headers.get("x-accel-buffering") == "no", "非流式应由 StreamingResponse 先吐首字节规避 524"
    assert "application/json" in r.headers.get("content-type", "")
    body = r.json()
    assert "answer" in body
    assert body["context_summary"]["route_source"] == "inferred"


def test_ask_non_stream_json_with_nonstandard_types():
    """回归：res 含 datetime/Decimal 等非标准 JSON 类型时，非流式也必须返回合法 JSON。

    曾因直接用标准库 json.dumps(res) 遇到不可序列化字段抛异常，
    只剩首字节空格 -> 客户端 json.loads 报 Expecting value: line 1 column 2。
    修复后用 FastAPI 同款 jsonable_encoder 兜底，且正文生成包在 try 内，任何异常都吐合法 JSON。
    """
    from tess_backend import app as app_module
    from fastapi.testclient import TestClient
    from datetime import datetime
    from decimal import Decimal

    app_module._require_ai_entitlement = lambda req: None
    app_module.get_data_connector = lambda *a, **k: _connector()
    app_module._get_llm_client = lambda pid: MockLLM()
    app_module._resolve_access_token = lambda req, pid: ("", "system")
    app_module._platform_id = lambda req: "default"
    app_module._operator_id = lambda req: "anonymous"

    def _fake_pq(*a, **k):
        return {
            "answer": "ok",
            "context_summary": {
                "route_source": "advertiser_deepdive",
                "generated_at": datetime(2026, 10, 14, 12, 0, 0),
                "total": Decimal("1234.56"),
            },
            "campaign_trends": {
                "9001": [{"date": datetime(2026, 7, 31), "cr": Decimal("0.12")}]
            },
        }

    orig = app_module.process_question
    app_module.process_question = _fake_pq
    try:
        client = TestClient(app_module.app)
        r = client.post("/tess/ask", json={"question": "广告主1000853 7-10月"})
        assert r.status_code == 200
        body = r.json()  # 必须能解析，不能 JSONDecodeError
        assert body["answer"] == "ok"
        # 非标准类型应被 jsonable_encoder 转成字符串/数字，而非抛异常
        assert body["context_summary"]["generated_at"].startswith("2026-10-14")
        assert body["context_summary"]["total"] == 1234.56
    finally:
        app_module.process_question = orig


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


def test_campaign_ranking_uses_fluctuation_and_enriches_with_report():
    """campaign_ranking 用 fluctuation 拿全量涨跌榜，再用 /report?campaign_ids 回补
    profit_change 与 campaign_name；排序优先 profit_change。"""
    class RankingConnector(MockConnector):
        def api_get(self, path, params=None, token=None, timeout=None):
            self.calls.append((path, dict(params or {})))
            if path == "/overview/ranking/fluctuation":
                return {
                    "rising": [{"campaign_id": "8001", "campaign_name": "Camp 8001", "revenue_change": 50}],
                    "falling": [{"campaign_id": "8002", "campaign_name": "Camp 8002", "revenue_change": -30}],
                }
            if path == "/report" and (params or {}).get("dimensions") == "date,hour,campaign":
                ds = (params or {}).get("date_start")
                de = (params or {}).get("date_end")
                items = []
                for d in (ds, de):
                    is_prev = (d == ds)
                    base = {"revenue": 1000 if is_prev else 800, "profit": 200 if is_prev else 100}
                    for cid in ("8001", "8002", "8003"):
                        for h in range(3):
                            items.append({
                                "date": d, "hour": h, "campaign_id": cid, "campaign_name": f"Camp {cid}",
                                "revenue": base["revenue"] / 3, "profit": base["profit"] / 3,
                            })
                return {"items": items}
            return super().api_get(path, params, token, timeout)

    conn = RankingConnector()
    ctx = fetch_bi_analysis_context(conn, "campaign_ranking", token="t")
    assert ctx["analysis_type"] == "campaign_ranking"
    # 应同时调用 fluctuation 与 /report?campaign_ids
    assert any(c[0] == "/overview/ranking/fluctuation" for c in conn.calls)
    report_calls = [c for c in conn.calls if c[0] == "/report"]
    assert report_calls, "应调用 /report 回补"
    assert report_calls[0][1].get("campaign_ids"), "report 应带 campaign_ids 过滤"
    assert ctx["falling_top"], "falling_top 不应为空"
    top = ctx["falling_top"][0]
    assert top["campaign_id"] == "8002", "下滑最快应为 8002"
    assert top.get("profit_change") == -100, "利润环比应为 本期100 - 上期200 = -100"
    assert top.get("campaign_name"), "campaign_name 应由 /report 回补"
    assert ctx["rising_top"][0]["campaign_id"] == "8001"


def test_campaign_ranking_empty_when_fluctuation_empty():
    """fluctuation 返回空时，campaign_ranking 优雅返回空榜单 + 说明错误，不崩溃。"""
    conn = _connector()  # 默认 MockConnector 的 fluctuation 返回空
    ctx = fetch_bi_analysis_context(conn, "campaign_ranking", token="t")
    assert ctx["analysis_type"] == "campaign_ranking"
    assert not ctx["rising_top"] and not ctx["falling_top"], "榜单应为空"
    assert ctx["errors"], "应说明 fluctuation 无数据"
