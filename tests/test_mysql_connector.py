"""MysqlDataConnector 单测：用 Fake 注入假数据，验证 SQL→dict 映射与 advertiser_deepdive 全路径。

不连真实 MySQL（CI/本地无 172.22.0.6），仅验证取数层的形状/口径正确性；
真实数据库对拍见部署后在 43 机跑的 verify 脚本。
"""

import sys
import os
import types

import pytest

# 构造最小桩，避免 import tess_backend.app 触发 FastAPI/DB 初始化
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tess_backend.mysql_connector import (
    MysqlDataConnector,
    campaign_status_label,
)


# --------------------------------------------------------------------------
# Fake connector：重写 _query，根据 SQL 特征返回与真实表结构一致的假行
# --------------------------------------------------------------------------

class FakeMysqlDataConnector(MysqlDataConnector):
    def __init__(self, n_campaigns=35, daily_days=5, operator_id=None):
        super().__init__(platform_id="Melodong", operator_id=operator_id)
        self.n = n_campaigns
        self.daily_days = daily_days
        # 35 个 campaign，revenue 递减（id 越小营收越高），status 循环 1/2/3
        self.campaigns = []
        for i in range(1, n_campaigns + 1):
            cid = 900000 + i
            rev = 1000.0 - i * 10.0
            pay = rev * 0.5
            clk = int(rev * 10)
            conv = int(clk * 0.02)
            status = (i % 3) + 1  # 1/2/3
            self.campaigns.append({
                "campaign_id": cid, "name": f"Camp {cid}", "status": status,
                "revenue": rev, "payout": pay, "clicks": clk, "conversions": conv,
            })
        # 日明细：top n_campaigns 每个 daily_days 天（覆盖分页测试）
        self.daily = []
        for c in self.campaigns:
            for d in range(1, daily_days + 1):
                self.daily.append({
                    "campaign_id": c["campaign_id"],
                    "date": f"2026-08-{d:02d}",
                    "revenue": c["revenue"] / daily_days,
                    "payout": c["payout"] / daily_days,
                    "clicks": c["clicks"] // daily_days,
                    "conversions": c["conversions"] // daily_days,
                })

    def _query(self, sql, params=()):
        s = sql
        if "FROM advertiser" in s:
            return [{"id": 1000853, "name": "Adv Test", "user_name": "betty",
                     "bd": 5, "am": 1, "status": 1, "jointime": "2026-01-01 00:00:00"}]
        if "c.status AS status" in s:  # campaign_daily_kpi 聚合（带 status）
            rows = []
            for c in self.campaigns:
                rows.append({
                    "campaign_id": c["campaign_id"], "name": c["name"], "status": c["status"],
                    "revenue": c["revenue"], "payout": c["payout"],
                    "clicks": c["clicks"], "conversions": c["conversions"],
                })
            return rows
        if "GROUP BY rc.campaign_id, c.name" in s:  # 汇总（无 status）
            rows = []
            for c in self.campaigns:
                rows.append({
                    "campaign_id": c["campaign_id"], "campaign_name": c["name"],
                    "revenue": c["revenue"], "payout": c["payout"],
                    "clicks": c["clicks"], "conversions": c["conversions"],
                })
            return rows
        if "FROM report_campaign WHERE campaign_id IN" in s:  # 日明细
            # params: (cid1, cid2, ..., ds, de)
            cids = [int(p) for p in params[:-2]]
            return [r for r in self.daily if r["campaign_id"] in cids]
        if "FROM campaign WHERE id IN" in s:
            cids = set(int(p) for p in params)
            return [{"id": c["campaign_id"], "name": c["name"], "status": c["status"]}
                    for c in self.campaigns if c["campaign_id"] in cids]
        return []


# --------------------------------------------------------------------------
# 字段映射单测
# --------------------------------------------------------------------------

def test_status_label():
    assert campaign_status_label(1) == "Active"
    assert campaign_status_label(2) == "Paused"
    assert campaign_status_label(3) == "Stop"
    assert campaign_status_label(None) is None
    assert campaign_status_label("2") == "Paused"


def test_profile_shape():
    c = FakeMysqlDataConnector()
    p = c.api_get("/advertisers/1000853")
    assert p["id"] == 1000853
    assert p["name"] == "Adv Test"
    assert p["am"] == 1
    assert p["status_name"] is None  # advertiser 无 status_name 列


def test_campaign_daily_kpi_shape_and_status():
    c = FakeMysqlDataConnector()
    d = c.api_get("/advertisers/campaign-daily-kpi",
                  params={"advertiser_id": 1000853, "date_start": "2026-07-01", "date_end": "2026-10-31"})
    camps = d["campaigns"]
    assert len(camps) == 35
    # profit = revenue - payout
    for camp in camps:
        assert abs(camp["profit"] - (camp["revenue"] - camp["payout"])) < 1e-6
        assert camp["total"]["profit"] == camp["profit"]
        # status 数字 → 可读名，无 null
        assert camp["campaign_status_name"] in ("Active", "Paused", "Stop")
    # total 汇总
    assert abs(d["total"]["profit"] - (d["total"]["revenue"] - d["total"]["payout"])) < 1e-6


def test_report_summary_cr_margin_profit_and_pagination():
    c = FakeMysqlDataConnector(n_campaigns=35)
    # 第 1 页
    p1 = c.api_get("/report", params={"advertiser_ids": 1000853, "dimensions": "campaign",
                                      "date_start": "2026-07-01", "date_end": "2026-10-31",
                                      "page": 1, "page_size": 100})
    assert "items" in p1
    items = p1["items"]
    assert len(items) == 35  # 35 个 campaign 全部返回（一页 100）
    top = items[0]
    assert top["campaign_id"] == 900001
    # cr / margin / profit 已算
    rev, pay = top["revenue"], top["payout"]
    clk, conv = top["clicks"], top["conversions"]
    assert abs(top["profit"] - (rev - pay)) < 1e-6
    assert abs(top["cr"] - (conv / clk * 100)) < 1e-6 if clk else True
    assert "campaign_name" in top


def test_report_detail_daily_rows():
    c = FakeMysqlDataConnector(n_campaigns=35, daily_days=5)
    p = c.api_get("/report", params={"campaign_ids": "900001,900002", "dimensions": "campaign,date",
                                     "date_start": "2026-07-01", "date_end": "2026-10-31",
                                     "page": 1, "page_size": 100})
    items = p["items"]
    assert len(items) == 10  # 2 campaign × 5 天
    for it in items:
        # 形状需匹配 analytics 的日明细读取（date/cr/margin 经映射）
        assert it["date"]
        assert "cr" in it and "margin" in it
        assert it["campaign_id"] in (900001, 900002)


def test_campaigns_status_int():
    c = FakeMysqlDataConnector()
    r = c.api_get("/campaigns", params={"campaign_ids": "900001,900002,900003"})
    items = r["items"]
    assert len(items) == 3
    for it in items:
        assert it["campaign_id"] in (900001, 900002, 900003)
        assert isinstance(it["status"], int)  # 数字，由调用方 _camp_status_label 映射


def test_unknown_path_returns_empty():
    c = FakeMysqlDataConnector()
    assert c.api_get("/overview/ranking") == {"items": []}


# --------------------------------------------------------------------------
# 全路径：advertiser_deepdive 经 MysqlDataConnector（长周期）
# --------------------------------------------------------------------------

def test_advertiser_deepdive_long_span_full():
    from tess_backend.analytics import fetch_bi_analysis_context, _advertiser_deepdive_prompt_view

    c = FakeMysqlDataConnector(n_campaigns=35, daily_days=5)
    ctx = fetch_bi_analysis_context(
        c, "advertiser_deepdive", token=None,
        params={"advertiser_id": 1000853, "date_start": "2026-07-01", "date_end": "2026-10-31"},
    )
    assert ctx["analysis_type"] == "advertiser_deepdive"
    assert ctx["long_span"] is True
    # 头部 30 个 campaign 全部进入趋势
    assert len(ctx["campaign_trends"]) == 30, f"trends={len(ctx['campaign_trends'])}"
    # 汇总前 30
    assert len(ctx["campaign_summary"]) == 30
    # 月度趋势点口径正确（_monthly_from_daily 不保留 profit，故校验 margin_rate）
    for cid, series in ctx["campaign_trends"].items():
        for pt in series:
            rev = pt.get("revenue") or 0
            pay = pt.get("payout") or 0
            if rev > 0:
                assert abs(pt.get("margin_rate") - (rev - pay) / rev * 100) < 1e-4

    # prompt_view 压缩快照，status 不应再有 null
    view = _advertiser_deepdive_prompt_view(ctx)
    snap = view["campaigns_snapshot"]
    assert len(snap) == 30
    null_status = [s for s in snap if s.get("status") is None or s.get("status") == ""]
    assert null_status == [], f"仍有 {len(null_status)} 个 null status"
    # 至少一个有可读 status
    assert any(s.get("status") in ("Active", "Paused", "Stop") for s in snap)
