"""P4 · Tess 数据分析模块（主动式 BI & Insights）。

定位：把 Tess 从「被动告警排查」升级为「主动式商业智能助手」。
运营日常不只是想看"哪里出错了"，更想知道：
  ① 每日/每周大盘与绩效复盘（谁跑得好）
  ② 哪些 Campaign 还有放量空间（增长挖掘）
  ③ 本月对账/毛利趋势是否符合预估（财务异动）

设计上完全复用 TeensingDataConnector 的传输层（Bearer token、Cloudflare UA、
{code,data} 外壳 unwrap），只新增"分析型"端点编排，不再引入新依赖。

真实可用端点（已用生产 token 探测确认，均返回数据）：
  GET /overview/daily-kpi          -> 逐日大盘 [{date,clicks,conversions,revenue,payout,profit}]（近 7 日）
  GET /overview/ranking            -> 广告主维度排名 [{rank,advertiser_id,advertiser_name,clicks,cvr,revenue,payout,margin,revenue_change,...}]
  GET /overview/ranking/fluctuation-> {rising:[...], falling:[...]}（涨跌榜，含 campaign 级环比）
  GET /overview/ranking/anomaly-warning -> {total,items[]}（被预警实体，source=anomaly-warning 主源）
  GET /overview/realtime-kpi       -> 实时小时级大盘 {items:[{hour,today_*,yesterday_*}]}（24 小时）
  GET /report                      -> 通用聚合报表（dimensions=campaign,publisher / date,hour,campaign；items 含 revenue,payout,profit,margin,cr）
  GET /campaign-quality/publisher  -> 各 publisher 质量/扣量 {items:[{publisher_id,publisher_name,total,<date>:{conversions,postback_conversions,clicks,q1_*}}]}
  GET /report/month                -> 月度报表 {items, total_data:{revenue,scrub_revenue,calc_revenue,payout,scrub_payout,calc_payout}}
                                       ⚠ 真实参数名为 start_date/end_date（YYYYMM，无横杠），非 report_month；report_month 仅作调用方契约，内部会转成 YYYYMM 透传
  GET /campaigns                   -> Campaign 主数据目录 {total,items:[{id,name,advertiser_id,country,cap,click_cap,payout_event,status,kpi,...}]}
  GET /publishers                  -> Publisher(渠道) 目录 {total,items:[{id,name,margin,payment_terms,click_caps,postback_url,...}]}
  GET /advertisers                 -> Advertiser(广告主) 目录 {total,items:[{id,name,bd,am,margin,contract_valid_to,...}]}
  GET /campaign-detail             -> 单 Campaign 详情 {t_campaign_id, campaigns, events, advertisers}（campaign_ids=）
  GET /campaign-quality            -> Campaign 级质量时序 {items:[{time_label,conversions,postback_conversions,q1_rate,q2_rate,reject_rate}]}（campaign_ids=）
  GET /campaign-kpi-trend          -> Campaign 指标趋势 {items:[{time_label,revenue,clicks,cvr,margin_rate,payout}]}（campaign_ids=）
  GET /campaign-ctit-etit          -> CTIT/ETIT 时间分布 {ctit:{5s,5-30s,...}, etit:{...}}（campaign_id= 单数！）
  GET /campaign-compare            -> 同期对比 {current_period,previous_period,revenue,profit,...}（campaign_ids= + date_start/date_end）
  GET /advertisers/{id}            -> 广告主档案 {id,name,user_name,bd,am,status,...}
  GET /advertisers/campaign-daily-kpi -> 广告主日 KPI {advertiser_id,total,campaigns[]}（advertiser_id=）
  GET /publisher-campaigns         -> Publisher 旗下 Campaign {items:[{publisher_id,campaign_id,campaign_name,margin}]}（publisher_id=）
  GET /mapping-publisher-channels  -> 渠道映射 {items:[{publisher_id,channel,replace_publisher_id,replace_channel}]}（publisher_id=）
  GET /replace-channels            -> 替换渠道规则 {items:[{advertiser_id,campaign_id,channel,replace_channel}]}（publisher_id= 或 advertiser_id=）
  GET /publisher-campaign-blocks   -> 屏蔽规则 {items:[{campaign_id,campaign_name,channels,status}]}（campaign_id= 或 publisher_id=）
  GET /global-settings             -> 全局设置 {timezone,global_cap,service_warning,currency}
  GET /publishers/campaign-daily-kpi -> Publisher 日 KPI {publisher_id,total,campaigns[]}（publisher_id=）
  GET /campaign-quality/publisher/channels -> 渠道级质量 {items:[]}（publisher_id=）
  POST /report/compare             -> 多 Campaign 对比（body: campaign_ids[],date_start,date_end）

已确认不可用（HTTP 404，本模块不依赖）：
  /external/invoices、/overview/summary|performance|kpi|quality|conversion|advertiser、
  /report/day|daily|campaign|publisher、/campaign-quality/campaign|advertiser、/campaign/list、
  /statistics/overview、/dashboard、/metrics、/kpi。
  （/report/export 存在但返回文件流非 JSON，BI 简报不适用。）
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
import json
import re

# ---------------------------------------------------------------------------
# BI 分析系统提示词（观点先行 / 数据支撑 / 动作导向）
# ---------------------------------------------------------------------------

BI_SYSTEM_PROMPT = """你叫 Tess，是 Teensing 平台的专属 AdTech 商业智能（BI）与数据分析助手。\
你的职责是基于传入的结构化业务数据，为运营或管理层输出有价值、可落地的洞察与优化建议。

【分析原则】
1. 观点先行：第一句必须直接给出核心结论（例如："昨日整体利润环比 +12%，主要由 Campaign_7030 的高 Margin 拉动"）。
2. 数据支撑：任何结论都必须带上具体数值（Revenue / Profit / Margin% / CVR% / 环比变化%），不得空泛。
3. 动作导向：分析末尾必须给出 2-3 条可执行的【推荐动作】(Recommended Actions)。

【输出版式（严格按此结构，顺序不可变）】
## 核心结论
- 用 1-2 句话给出最核心的结论（必须含关键数值）。

## 关键指标
- 凡涉及多项数值对比（如各 Campaign / Publisher / Advertiser 的指标），**必须用 Markdown 表格**呈现，列示例：| 实体 (id) | 指标A | 指标B | 状态 |。
- 指标表固定列出核心指标及其数值 / 环比。

## 潜能点 / 风险点
- 每点一行：明确实体 (id) + 数据表现 + 可能原因（用数据佐证）；多个实体对比时必须用表格。

## 推荐执行动作 (Recommended Actions)
1. 针对 [Publisher/Campaign (id)]，建议 [加大 Cap / 调整出价 / 核查扣量 / 暂停亏损计划]（给出具体数字建议）。
2. ...

【排版与红线】
- 全文尽量控制在 700 字以内，杜绝空泛铺垫与客套寒暄；长列表必须表格化，禁止用成段 bullet 堆叠。
- 严禁编造数据中不存在的实体、数值或趋势；如果某维度数据为空/缺失，明确说明"该维度暂无数据，建议补充口径"，不要臆测。
- 涉及金额/百分比必须直接引用输入数值，不得自行计算篡改。
- 输出全部使用中文，语气专业、简洁。
- 实体名称已附带 (id)，引用 Campaign / Publisher / Advertiser 时务必保留其 (id)，便于运营定位核对。
"""


# ---------------------------------------------------------------------------
# 数据加载器：按分析类型拼装结构化上下文
# ---------------------------------------------------------------------------

def _safe_api_get(connector, path: str, params: Optional[dict] = None, token: Optional[str] = None):
    """带容错的 Teensing 接口调用：成功返回 (data, None)，失败返回 (None, error_str)。"""
    try:
        return connector.api_get(path, params=params, token=token), None
    except Exception as e:  # noqa: BLE001 —— 分析场景宁可返回局部错误也不崩整轮
        return None, f"{path} 调用失败: {type(e).__name__}: {e}"


def fetch_bi_analysis_context(
    connector,
    analysis_type: str,
    token: Optional[str] = None,
    params: Optional[dict] = None,
) -> dict:
    """Tess 数据分析功能的数据加载器。

    analysis_type:
      'daily_summary'          -> 每日/每周大盘绩效复盘
      'scaling_opportunity'    -> 放量与高潜力 Campaign 挖掘
      'finance_check'          -> 月度对账与财务扣量分析
      'account_overview'       -> 账户全景（Campaign/Advertiser 主数据 + 广告主榜）
      'publisher_deepdive'     -> 渠道质量对比（Publisher 目录 + 扣量/质量率）
      'scaling_capacity'       -> 放量容量评估（Campaign Cap + 近 7 日利润/Margin + 全局 Cap）
      'campaign_detail'        -> 单 Campaign 微观下钻（配置/Cap/质量时序/CTIT-ETIT/指标趋势）
      'advertiser_deepdive'    -> 广告主维度（档案 + 日 KPI + 旗下 Campaign）
      'traffic_policy_check'   -> 流量策略核查（渠道映射/替换渠道/屏蔽规则）
      'kpi_compare'            -> 指标趋势与同期对比（KPI 趋势 + 区间对比）
      'campaign_ranking'       -> 跨 Campaign 排名/诊断（涨跌榜 rising/falling，按 revenue_change 排序，答"哪个 Campaign 环比下滑最快"）

      'pkg_deepdive'           -> 包名维度（/advertiser-publisher-pkg-maps 归因 + /report 聚合，答"com.x 这个包的跑量/转化"）
      'owner_performance'      -> AM/BD 负责人维度（/advertisers?am|bd= 解析名下广告主 + /report 聚合，答"Betty 名下客户消耗"）

    返回结构化的上下文 dict，供 LLM 生成简报。每个子数据源独立容错：
    单个接口失败只会在对应字段标记 error，不会拖垮整个分析。
    """
    params = params or {}
    today = datetime.now()
    yesterday = today - timedelta(days=1)

    # ---------------------------------------------------------------
    # 场景 1：每日/每周绩效复盘
    # ---------------------------------------------------------------
    if analysis_type == "daily_summary":
        daily_kpi, err_kpi = _safe_api_get(
            connector, "/overview/daily-kpi",
            params={"date": yesterday.strftime("%Y-%m-%d")}, token=token,
        )
        ranking, err_rank = _safe_api_get(connector, "/overview/ranking", token=token)
        fluc, err_fluc = _safe_api_get(connector, "/overview/ranking/fluctuation", token=token)

        # 取最近两日（today / yesterday）做环比
        kpi_list = daily_kpi if isinstance(daily_kpi, list) else []
        latest = kpi_list[-1] if kpi_list else None
        prev = kpi_list[-2] if len(kpi_list) >= 2 else None

        ranking_list = ranking if isinstance(ranking, list) else []
        fluc_map = fluc if isinstance(fluc, dict) else {}

        return {
            "analysis_type": "daily_summary",
            "date": yesterday.strftime("%Y-%m-%d"),
            "today_kpi": latest,
            "yesterday_kpi": prev,
            "overall_kpi_delta": _pct_delta(latest, prev),
            "top_campaigns": ranking_list[:5],
            "rising_gainers": (fluc_map.get("rising") or [])[:3],
            "falling_losers": (fluc_map.get("falling") or [])[:3],
            "errors": [e for e in (err_kpi, err_rank, err_fluc) if e],
        }

    # ---------------------------------------------------------------
    # 场景 2：放量与高潜力 Campaign 挖掘
    # ---------------------------------------------------------------
    elif analysis_type == "scaling_opportunity":
        start_7d = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        report, err_report = _safe_api_get(
            connector, "/report",
            params={
                "dimensions": "campaign,publisher",
                "date_start": start_7d,
                "date_end": end,
                "sort_by": "profit",
                "sort_order": "desc",
                "page": 1,
                "page_size": 20,
            },
            token=token,
        )
        quality, err_quality = _safe_api_get(connector, "/campaign-quality/publisher", token=token)

        items = (report or {}).get("items", []) if isinstance(report, dict) else []
        # 筛选 Margin > 25% 且 转化率(CR) > 1.0% 的潜能项
        # 字段注意：/report 维度下转化率字段是 "cr"（不是 cvr），margin 为百分比数值
        opportunities = [
            it for it in items
            if _to_float(it.get("margin")) > 25.0 and _to_float(it.get("cr")) > 1.0
        ]

        return {
            "analysis_type": "scaling_opportunity",
            "time_range": f"{start_7d} ~ {end}",
            "scaling_candidates": opportunities[:5],
            "raw_top_by_profit": items[:5],
            "publisher_quality": (quality or {}).get("items", []) if isinstance(quality, dict) else [],
            "errors": [e for e in (err_report, err_quality) if e],
        }

    # ---------------------------------------------------------------
    # 场景 3：月度对账与财务扣量分析
    # ---------------------------------------------------------------
    elif analysis_type == "finance_check":
        # 调用方契约（前端/文档）：可传 report_month="2026-06"（YYYY-MM，单月），
        # 或显式传 start_date/end_date（YYYYMM，支持自定义区间，如一个季度）。
        # 注意：Teensing /report/month 真实参数名为 start_date/end_date，且接受 YYYYMM（无横杠）格式；
        # 之前误传 report_month=2026-06 会被接口忽略 -> 返回全 0，现已修正。
        if params.get("start_date") and params.get("end_date"):
            sd, ed = str(params["start_date"]), str(params["end_date"])
            month_label = f"{sd}~{ed}"
        else:
            month = params.get("report_month") or today.strftime("%Y-%m")  # "2026-06"
            sd = ed = month.replace("-", "")  # "202606"
            month_label = month
        month_res, err_month = _safe_api_get(
            connector, "/report/month",
            params={"start_date": sd, "end_date": ed, "page_size": 50},
            token=token,
        )
        if not isinstance(month_res, dict):
            month_res = {}

        items = month_res.get("items", [])
        total_data = month_res.get("total_data", {})
        # 计算营收 (calc_revenue) 与 原始/实结营收 (revenue) 差异较大的记录
        reconcile_diffs = [
            it for it in items
            if abs(_to_float(it.get("calc_revenue")) - _to_float(it.get("revenue"))) > 500
        ]

        return {
            "analysis_type": "finance_check",
            "report_month": month_label,
            "total_summary": total_data,
            "discrepancy_items": reconcile_diffs[:10],
            "sample_items": items[:5],
            "errors": [err_month] if err_month else [],
        }

    # ---------------------------------------------------------------
    # 场景 4：账户全景（Campaign / Advertiser 主数据 + 广告主榜）
    # ---------------------------------------------------------------
    elif analysis_type == "account_overview":
        camps, err_c = _safe_api_get(
            connector, "/campaigns", params={"page": 1, "page_size": 100}, token=token,
        )
        advs, err_a = _safe_api_get(
            connector, "/advertisers", params={"page": 1, "page_size": 50}, token=token,
        )
        ranking, err_r = _safe_api_get(connector, "/overview/ranking", token=token)

        camps_list = (camps or {}).get("items", []) if isinstance(camps, dict) else []
        advs_list = (advs or {}).get("items", []) if isinstance(advs, dict) else []
        rank_list = ranking if isinstance(ranking, list) else []

        # 注：campaigns 总量 300 万+，此处仅对首页 100 条最新 Campaign 做抽样统计，
        # 不可当作全局占比；全局规模以 campaign_total / advertiser_total 为准。
        active = [c for c in camps_list if _to_float(c.get("status")) == 1]
        inactive = [c for c in camps_list if _to_float(c.get("status")) != 1]
        missing_cap = [c for c in camps_list if _to_float(c.get("cap")) <= 0]
        top_adv = sorted(rank_list, key=lambda x: _to_float(x.get("revenue")), reverse=True)[:5]

        return {
            "analysis_type": "account_overview",
            "campaign_total": (camps or {}).get("total") if isinstance(camps, dict) else None,
            "advertiser_total": (advs or {}).get("total") if isinstance(advs, dict) else None,
            "sample_size": len(camps_list),
            "sample_active_count": len(active),
            "sample_inactive_count": len(inactive),
            "sample_missing_cap_count": len(missing_cap),
            "sample_active_campaigns": _pick_many(
                active[:12], ["id", "name", "advertiser_id", "country", "cap", "weget", "status"]
            ),
            "sample_campaigns_missing_cap": _pick_many(
                missing_cap[:10], ["id", "name", "advertiser_id", "status"]
            ),
            "top_advertisers_by_revenue": _pick_many(
                top_adv, ["advertiser_id", "advertiser_name", "revenue", "profit", "margin", "revenue_change"]
            ),
            "errors": [e for e in (err_c, err_a, err_r) if e],
        }

    # ---------------------------------------------------------------
    # 场景 5：渠道质量对比（Publisher 目录 + 扣量/质量率）
    # ---------------------------------------------------------------
    elif analysis_type == "publisher_deepdive":
        pubs, err_p = _safe_api_get(
            connector, "/publishers", params={"page": 1, "page_size": 100}, token=token,
        )
        quality, err_q = _safe_api_get(connector, "/campaign-quality/publisher", token=token)

        pubs_list = (pubs or {}).get("items", []) if isinstance(pubs, dict) else []
        q_items = (quality or {}).get("items", []) if isinstance(quality, dict) else []
        pub_map = {_to_float(p.get("id")): p for p in pubs_list}

        enriched = []
        for it in q_items:
            pid = _to_float(it.get("publisher_id"))
            t = it.get("total") or {}
            conv = _to_float(t.get("conversions"))
            pb = _to_float(t.get("postback_conversions"))
            pub = pub_map.get(pid, {})
            enriched.append({
                "publisher_id": pid,
                "publisher_name": it.get("publisher_name"),
                "publisher_margin": _to_float(pub.get("margin")),
                "publisher_status": pub.get("status"),
                "total_clicks": _to_float(t.get("clicks")),
                "total_conversions": conv,
                "postback_conversions": pb,
                "postback_gap": round(conv - pb, 2),
                "q1_rate": _to_float(t.get("q1_rate")),
                "q2_rate": _to_float(t.get("q2_rate")),
                "reject_rate": _to_float(t.get("reject_rate")),
            })
        enriched.sort(key=lambda x: x["total_clicks"], reverse=True)

        # 标记疑似质量问题：有 reject / q1 / q2 扣量，或回传缺口 > 10%
        flagged = [
            e for e in enriched
            if e["reject_rate"] > 0 or e["q1_rate"] > 0 or e["q2_rate"] > 0
            or (e["total_conversions"] > 0 and e["postback_gap"] > 0.1 * e["total_conversions"])
        ]

        # 若调用方同时指定 publisher_id 与 campaign_ids，额外拉取该渠道的细分质量
        # （/campaign-quality/publisher/channels 实测需要 campaign_ids，否则 422；publisher 维度下钻无 campaign 上下文，跳过）
        channel_quality, err_ch = (None, None)
        if params.get("publisher_id") and params.get("campaign_ids"):
            channel_quality, err_ch = _safe_api_get(
                connector, "/campaign-quality/publisher/channels",
                params={"publisher_id": str(params["publisher_id"]),
                        "campaign_ids": params["campaign_ids"]}, token=token,
            )

        return {
            "analysis_type": "publisher_deepdive",
            "publisher_total": (pubs or {}).get("total") if isinstance(pubs, dict) else None,
            "quality_publisher_count": len(q_items),
            "top_publishers_by_clicks": enriched[:8],
            "flagged_quality_issues": flagged[:10],
            "channel_quality": (channel_quality or {}).get("items", []) if isinstance(channel_quality, dict) else [],
            "errors": [e for e in (err_p, err_q, err_ch) if e],
        }

    # ---------------------------------------------------------------
    # 场景 6：放量容量评估（Campaign Cap + 近 7 日利润/Margin）
    # ---------------------------------------------------------------
    elif analysis_type == "scaling_capacity":
        start_7d = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        report, err_r = _safe_api_get(
            connector, "/report",
            params={
                "dimensions": "campaign,publisher",
                "date_start": start_7d,
                "date_end": end,
                "sort_by": "profit",
                "sort_order": "desc",
                "page": 1,
                "page_size": 30,
            },
            token=token,
        )

        r_items = (report or {}).get("items", []) if isinstance(report, dict) else []

        # 按 campaign 聚合近 7 日表现（跨 publisher 求和）
        agg = defaultdict(lambda: {"name": "", "profit": 0.0, "revenue": 0.0, "payout": 0.0,
                                   "clicks": 0, "conversions": 0, "margin_sum": 0.0, "n": 0})
        for it in r_items:
            cid = _to_float(it.get("campaign_id"))
            a = agg[cid]
            a["name"] = a["name"] or it.get("campaign_name") or ""
            a["profit"] += _to_float(it.get("profit"))
            a["revenue"] += _to_float(it.get("revenue"))
            a["payout"] += _to_float(it.get("payout"))
            a["clicks"] += int(_to_float(it.get("clicks")))
            a["conversions"] += int(_to_float(it.get("conversions")))
            a["margin_sum"] += _to_float(it.get("margin"))
            a["n"] += 1

        # 用 report 命中的 campaign_id 精确回查 /campaigns 的 Cap 配置（避免 3M 全量）
        camp_map = {}
        cids = [str(int(c)) for c in agg.keys() if c]
        err_c = None
        for i in range(0, len(cids), 100):
            chunk = cids[i:i + 100]
            camps, e = _safe_api_get(
                connector, "/campaigns",
                params={"campaign_ids": ",".join(chunk), "page": 1, "page_size": 100},
                token=token,
            )
            if e:
                err_c = e
                continue
            for c in ((camps or {}).get("items", []) if isinstance(camps, dict) else []):
                camp_map[_to_float(c.get("id"))] = c

        candidates = []
        for cid, a in agg.items():
            camp = camp_map.get(cid, {})
            margin_avg = round(a["margin_sum"] / a["n"], 2) if a["n"] else 0.0
            candidates.append({
                "campaign_id": cid,
                "campaign_name": a["name"] or camp.get("name") or "unknown",
                "profit_7d": round(a["profit"], 2),
                "revenue_7d": round(a["revenue"], 2),
                "margin_avg": margin_avg,
                "clicks_7d": a["clicks"],
                "conversions_7d": a["conversions"],
                "cap": _to_float(camp.get("cap")),
                "click_cap": _to_float(camp.get("click_cap")),
                "monthly_cap": _to_float(camp.get("monthly_cap")),
            })
        candidates.sort(key=lambda x: x["profit_7d"], reverse=True)

        # 放量空间：盈利且高 Margin 但 Cap 偏低（0 < cap <= 500）
        scaling_room = [
            c for c in candidates
            if c["profit_7d"] > 0 and c["margin_avg"] > 25 and 0 < c["cap"] <= 500
        ][:8]
        # 容量浪费：近 7 日亏损却仍挂着 Cap
        over_cap_waste = [c for c in candidates if c["profit_7d"] <= 0 and c["cap"] > 0][:8]

        # 叠加全局 Cap 设置（/global-settings），用于判断账户级容量天花板
        gsettings, err_g = _safe_api_get(connector, "/global-settings", token=token)

        return {
            "analysis_type": "scaling_capacity",
            "time_range": f"{start_7d} ~ {end}",
            "global_cap": (gsettings or {}).get("global_cap") if isinstance(gsettings, dict) else None,
            "campaigns_with_performance": len(candidates),
            "scaling_room": scaling_room,
            "over_cap_waste": over_cap_waste,
            "top_by_profit": candidates[:8],
            "errors": [e for e in (err_r, err_c, err_g) if e],
        }

    # ---------------------------------------------------------------
    # 场景 7：单 Campaign 微观下钻（配置 / 质量 / CTIT-ETIT / 指标趋势）
    # ---------------------------------------------------------------
    elif analysis_type == "campaign_detail":
        cid = params.get("campaign_id")
        if not cid:
            return {
                "analysis_type": "campaign_detail",
                "errors": ["campaign_detail 需要 campaign_id 参数（问题里含 '<id>camp' 或 'ctit/etit'，或显式传 campaign_id）"],
            }
        cid = str(cid)
        cfg, err_cfg = _safe_api_get(
            connector, "/campaigns", params={"campaign_ids": cid, "page": 1, "page_size": 10}, token=token,
        )
        detail, err_det = _safe_api_get(connector, "/campaign-detail", params={"campaign_ids": cid}, token=token)
        quality, err_q = _safe_api_get(connector, "/campaign-quality", params={"campaign_ids": cid}, token=token)
        trend, err_t = _safe_api_get(connector, "/campaign-kpi-trend", params={"campaign_ids": cid}, token=token)
        ctit, err_c = _safe_api_get(connector, "/campaign-ctit-etit", params={"campaign_id": cid}, token=token)

        cfg_items = (cfg or {}).get("items", []) if isinstance(cfg, dict) else []
        det = detail if isinstance(detail, dict) else {}
        q_items = (quality or {}).get("items", []) if isinstance(quality, dict) else []
        t_items = (trend or {}).get("items", []) if isinstance(trend, dict) else []
        ctit_data = ctit if isinstance(ctit, dict) else {}

        return {
            "analysis_type": "campaign_detail",
            "campaign_id": cid,
            "campaign_config": _pick_many(cfg_items[:1],
                ["id", "name", "advertiser_id", "publisher_id", "country", "cap", "click_cap", "status", "kpi"]) if cfg_items else [],
            "detail": {k: det.get(k) for k in ("t_campaign_id", "campaigns", "events", "advertisers")},
            "quality_timeseries": _pick_many(q_items,
                ["time_label", "conversions", "postback_conversions", "q1_rate", "q2_rate", "reject_rate"])[:7],
            "kpi_trend": _pick_many(t_items,
                ["time_label", "revenue", "clicks", "cvr", "margin_rate", "payout"])[:7],
            "ctit_etit": {k: ctit_data.get(k) for k in ("ctit", "etit")},
            "errors": [e for e in (err_cfg, err_det, err_q, err_t, err_c) if e],
        }

    # ---------------------------------------------------------------
    # 场景 8：广告主维度（档案 + 日 KPI + 旗下 Campaign）
    # ---------------------------------------------------------------
    elif analysis_type == "advertiser_deepdive":
        aid = params.get("advertiser_id")
        if not aid:
            return {"analysis_type": "advertiser_deepdive",
                    "errors": ["advertiser_deepdive 需要 advertiser_id 参数"]}
        aid = str(aid)
        rec, err_r = _safe_api_get(connector, f"/advertisers/{aid}", token=token)
        daily, err_d = _safe_api_get(
            connector, "/advertisers/campaign-daily-kpi",
            params={"advertiser_id": aid}, token=token,
        )
        rec_d = rec if isinstance(rec, dict) else {}
        daily_d = daily if isinstance(daily, dict) else {}
        return {
            "analysis_type": "advertiser_deepdive",
            "advertiser_id": aid,
            "profile": {k: rec_d.get(k) for k in ("id", "name", "user_name", "bd", "am", "status", "status_name", "jointime")},
            "daily_kpi": {
                "advertiser_id": daily_d.get("advertiser_id"),
                "start_date": daily_d.get("start_date"),
                "end_date": daily_d.get("end_date"),
                "total": daily_d.get("total"),
                "campaigns": (daily_d.get("campaigns") or [])[:10],
            },
            "errors": [e for e in (err_r, err_d) if e],
        }

    # ---------------------------------------------------------------
    # 场景 9：流量策略核查（渠道映射 / 替换渠道 / 屏蔽规则）
    # ---------------------------------------------------------------
    elif analysis_type == "traffic_policy_check":
        pid = params.get("publisher_id")
        cid = params.get("campaign_id")
        if not pid and not cid:
            return {"analysis_type": "traffic_policy_check",
                    "errors": ["traffic_policy_check 需要 publisher_id 或 campaign_id 参数"]}
        # 若只给了 campaign_id，先解析其 publisher_id 以拉取映射/替换规则
        if not pid and cid:
            c0, _ = _safe_api_get(connector, "/campaigns",
                                  params={"campaign_ids": str(cid), "page": 1, "page_size": 1}, token=token)
            c0_items = (c0 or {}).get("items", []) if isinstance(c0, dict) else []
            pid = _to_float((c0_items[0] or {}).get("publisher_id")) if c0_items else None
        pid_s = str(int(pid)) if pid else None
        mapping, err_m = _safe_api_get(
            connector, "/mapping-publisher-channels",
            params={"publisher_id": pid_s} if pid_s else {}, token=token)
        replace, err_rp = _safe_api_get(
            connector, "/replace-channels",
            params={"publisher_id": pid_s} if pid_s else {}, token=token)
        block_params = {"campaign_id": str(cid)} if cid else ({"publisher_id": pid_s} if pid_s else {})
        blocks, err_b = _safe_api_get(connector, "/publisher-campaign-blocks", params=block_params, token=token)
        return {
            "analysis_type": "traffic_policy_check",
            "publisher_id": pid,
            "campaign_id": cid,
            "mapping_publisher_channels": (mapping or {}).get("items", []) if isinstance(mapping, dict) else [],
            "replace_channels": (replace or {}).get("items", []) if isinstance(replace, dict) else [],
            "blocks": (blocks or {}).get("items", []) if isinstance(blocks, dict) else [],
            "errors": [e for e in (err_m, err_rp, err_b) if e],
        }

    # ---------------------------------------------------------------
    # 场景 10：指标趋势与同期对比
    # ---------------------------------------------------------------
    elif analysis_type == "kpi_compare":
        cid = params.get("campaign_id")
        if not cid:
            return {"analysis_type": "kpi_compare",
                    "errors": ["kpi_compare 需要 campaign_id 参数（趋势/对比对象）"]}
        cid = str(cid)
        ds = params.get("date_start") or (today - timedelta(days=7)).strftime("%Y-%m-%d")
        de = params.get("date_end") or today.strftime("%Y-%m-%d")
        trend, err_t = _safe_api_get(connector, "/campaign-kpi-trend", params={"campaign_ids": cid}, token=token)
        compare, err_c = _safe_api_get(
            connector, "/campaign-compare",
            params={"campaign_ids": cid, "date_start": ds, "date_end": de}, token=token,
        )
        t_items = (trend or {}).get("items", []) if isinstance(trend, dict) else []
        cmp_d = compare if isinstance(compare, dict) else {}
        return {
            "analysis_type": "kpi_compare",
            "campaign_id": cid,
            "date_range": f"{ds} ~ {de}",
            "kpi_trend": _pick_many(t_items,
                ["time_label", "revenue", "clicks", "cvr", "margin_rate", "payout"])[:7],
            "period_compare": {k: cmp_d.get(k) for k in
                ("current_period", "previous_period", "revenue", "profit", "conversions", "cvr")},
            "errors": [e for e in (err_t, err_c) if e],
        }

    # ---------------------------------------------------------------
    # 场景 9：跨 Campaign 排名 / 诊断（"哪个 Campaign 利润环比下滑最快"）
    # ---------------------------------------------------------------
    elif analysis_type == "campaign_ranking":
        fluc, err_fluc = _safe_api_get(connector, "/overview/ranking/fluctuation", token=token)
        fluc_map = fluc if isinstance(fluc, dict) else {}
        rising = fluc_map.get("rising") or []
        falling = fluc_map.get("falling") or []

        # 按营收环比（revenue_change）排序：falling 升序（最负=跌最快）在前
        def _rc(x):
            try:
                return float(x.get("revenue_change") or 0)
            except (TypeError, ValueError):
                return 0.0
        falling_sorted = sorted(falling, key=_rc)[:10]
        rising_sorted = sorted(rising, key=lambda x: -_rc(x))[:10]

        return {
            "analysis_type": "campaign_ranking",
            "metric_note": "涨跌榜口径为营收(revenue)环比（revenue_change）；接口未提供独立利润(profit)环比字段，利润口径以营收环比代理。falling_top 已按 revenue_change 升序排列，[0] 即环比下滑最快的 Campaign。",
            "rising_top": rising_sorted,
            "falling_top": falling_sorted,
            "errors": [e for e in (err_fluc,) if e],
        }

    # ---------------------------------------------------------------
    # 场景 12：包名维度（跨 Campaign 消耗 / PKG 映射 / 转化率）
    # ---------------------------------------------------------------
    elif analysis_type == "pkg_deepdive":
        pkg = params.get("package_name")
        if not pkg:
            return {"analysis_type": "pkg_deepdive",
                    "errors": ["pkg_deepdive 需要 package_name（包名，如 com.xxx.yyy 或 pkg:xxx）"]}
        pkg = str(pkg)
        maps, err_m = _safe_api_get(
            connector, "/advertiser-publisher-pkg-maps",
            params={"packagename": pkg, "page": 1, "page_size": 100}, token=token)
        m_items = (maps or {}).get("items", []) if isinstance(maps, dict) else []
        adv_ids = sorted({_fmt_id(i.get("advertiser_id")) for i in m_items if i.get("advertiser_id")})
        pub_ids = sorted({_fmt_id(i.get("publisher_id")) for i in m_items if i.get("publisher_id")})

        start_7d = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        if adv_ids or pub_ids:
            r_params = {"dimensions": "campaign,publisher", "date_start": start_7d, "date_end": end,
                        "page": 1, "page_size": 100}
            # 主过滤用广告主（pkg-maps 归因出的归属广告主）；仅当无广告主时才退化按渠道过滤（兜底，较粗）
            if adv_ids:
                r_params["advertiser_ids"] = ",".join(adv_ids)
            elif pub_ids:
                r_params["publisher_ids"] = ",".join(pub_ids)
            report, err_r = _safe_api_get(connector, "/report", params=r_params, token=token)
            r_items = (report or {}).get("items", []) if isinstance(report, dict) else []
            # 注：/report 行 package_name 多为空，且映射渠道与实际投放渠道未必一致，
            # 故不再按 package_name 二次过滤（否则整体归零）；归因透明度由下方 publisher_ids 字段保留。
        else:
            report, err_r = (None, None)
            r_items = []
        agg = _aggregate_report(r_items)
        return {
            "analysis_type": "pkg_deepdive",
            "package_name": pkg,
            "pkg_maps_count": len(m_items),
            "advertiser_ids": adv_ids,
            "publisher_ids": pub_ids,
            "time_range": f"{start_7d} ~ {end}",
            "metric_note": "包名系统级表现由 /advertiser-publisher-pkg-maps 解析出归属的广告主/渠道，再用 /report 聚合近 7 日营收/利润/Margin；若 pkg-maps 无该包登记，则无法归因系统级消耗（接口未提供 /report?package_name 过滤）。",
            "report_summary": agg,
            "report_rows": _pick_many(r_items,
                ["date", "campaign_id", "campaign_name", "publisher_id", "publisher_name",
                 "revenue", "profit", "payout", "clicks", "conversions", "cvr", "margin_rate"], 15),
            "errors": [e for e in (err_m, err_r) if e],
        }

    # ---------------------------------------------------------------
    # 场景 13：AM/BD 负责人维度（名下广告主实时消耗与对账）
    # ---------------------------------------------------------------
    elif analysis_type == "owner_performance":
        uid = params.get("owner_user_id")
        if not uid:
            return {"analysis_type": "owner_performance",
                    "errors": ["owner_performance 需要 owner_user_id（由负责人姓名经 /users/options 解析，或显式透传）"]}
        role = params.get("owner_role") or None
        # 未指定角色则 am/bd 双查取并集
        adv_ids = set()
        for r in (["am", "bd"] if role is None else [role]):
            # 分页扫描（API 限制 page_size<=100，单角色最多取 5 页 = 500 个广告主）
            for pg in range(1, 6):
                advs, _ = _safe_api_get(connector, "/advertisers",
                                        params={r: str(uid), "page": pg, "page_size": 100}, token=token)
                items = (advs or {}).get("items", []) if isinstance(advs, dict) else []
                if not items:
                    break
                for it in items:
                    adv_ids.add(_fmt_id(it.get("id")))
                if len(items) < 100:
                    break
        adv_ids = sorted(adv_ids)
        start_7d = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        if adv_ids:
            report, err_r = _safe_api_get(
                connector, "/report",
                params={"dimensions": "advertiser", "date_start": start_7d, "date_end": end,
                        "advertiser_ids": ",".join(adv_ids), "page": 1, "page_size": 100}, token=token)
            r_items = (report or {}).get("items", []) if isinstance(report, dict) else []
        else:
            report, err_r = (None, None)
            r_items = []
        agg = _aggregate_report(r_items)
        return {
            "analysis_type": "owner_performance",
            "owner_role": role or "am+bd",
            "owner_user_id": str(uid),
            "owner_name": params.get("owner_name"),
            "advertiser_count": len(adv_ids),
            "advertiser_ids": adv_ids,
            "time_range": f"{start_7d} ~ {end}",
            "metric_note": "负责人名下广告主由 /advertisers?am= / ?bd= 解析；消耗经 /report(advertiser_ids) 聚合近 7 日营收/利润/Margin。注：Teensing 无 /advertisers/am/{id} 嵌套路由（实测 404），改走 am/bd 字段过滤。",
            "report_summary": agg,
            "report_rows": _pick_many(r_items,
                ["date", "advertiser_id", "advertiser_name", "campaign_id", "campaign_name",
                 "revenue", "profit", "payout", "clicks", "conversions"], 15),
            "errors": [e for e in (err_r,) if e],
        }

    else:
        raise ValueError(
            f"未知的 analysis_type={analysis_type!r}；"
            "支持: daily_summary / scaling_opportunity / finance_check / account_overview / "
            "publisher_deepdive / scaling_capacity / campaign_detail / advertiser_deepdive / "
            "traffic_policy_check / kpi_compare / campaign_ranking / pkg_deepdive / owner_performance"
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pick(item, keys):
    """从 dict 中抽取指定字段子集，便于裁剪后透传给 LLM。"""
    if not isinstance(item, dict):
        return item
    return {k: item.get(k) for k in keys}


def _pick_many(items, keys, limit: Optional[int] = None):
    out = []
    for it in (items or []):
        out.append(_pick(it, keys))
        if limit and len(out) >= limit:
            break
    return out


def _pct_delta(today: Optional[dict], yesterday: Optional[dict]) -> dict:
    """计算今日相对昨日的环比（仅对数值字段）。"""
    out = {}
    if not isinstance(today, dict) or not isinstance(yesterday, dict):
        return out
    for key in ("revenue", "profit", "payout", "clicks", "conversions"):
        a, b = _to_float(today.get(key)), _to_float(yesterday.get(key))
        if b != 0:
            out[key] = round((a - b) / b * 100, 2)
    return out


# 已知「名称字段 -> id 字段」配对：把 id 追加到名称后（如 "Nike MX (5832106)"），
# 覆盖 campaign / publisher / advertiser 三类实体，以及主数据目录通用的 name/id。
_NAME_ID_PAIRS = (
    ("campaign_name", "campaign_id"),
    ("publisher_name", "publisher_id"),
    ("advertiser_name", "advertiser_id"),
    ("name", "id"),  # /campaigns、/publishers、/advertisers 主数据目录
)


def _fmt_id(v) -> str:
    """把 id 规整成字符串；整型数值去掉 .0（如 5832106.0 -> '5832106'）。"""
    if v is None or v == "":
        return ""
    try:
        f = float(v)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return str(v)


def _enrich_names_with_ids(obj):
    """递归遍历上下文，给所有 (name, id) 配对的字段把 id 追加到名称后。

    仅增强 name 字段（"name (id)"），不改 id 字段本身（前端可能单独依赖 id 做跳转/动作）。
    """
    if isinstance(obj, dict):
        for name_field, id_field in _NAME_ID_PAIRS:
            if name_field in obj and id_field in obj:
                nm = obj.get(name_field)
                idv = _fmt_id(obj.get(id_field))
                if not nm or not idv:
                    continue
                obj[name_field] = f"{nm} ({idv})"
        for v in obj.values():
            _enrich_names_with_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            _enrich_names_with_ids(v)
    return obj


def _build_user_prompt(analysis_type: str, ctx: dict) -> str:
    """把结构化上下文拼成给 LLM 的 user prompt。

    注意：这里只透传分析所需字段，不做任何业务判定（判定交给 LLM + 本模块红线）。
    """
    intro = {
        "daily_summary": "以下是昨日/今日大盘与排名数据，请输出绩效复盘简报。",
        "scaling_opportunity": "以下是近 7 天按利润排序的 Campaign/Publisher 报表与质量数据，请挖掘放量潜力。",
        "finance_check": "以下是本月月度报表（含 revenue 与 calc_revenue 对账字段），请分析对账差异与毛利趋势。",
        "account_overview": "以下是 Campaign/Advertiser 主数据目录与广告主维度排名，请输出账户全景概览。注意：campaign 总量达数百万，下方 active/inactive/missing_cap 仅来自首页 100 条抽样，不能代表全局占比；全局规模请看 campaign_total/advertiser_total，头部广告主请看 top_advertisers_by_revenue。",
        "publisher_deepdive": "以下是 Publisher 渠道目录与扣量/质量率数据，请对比各渠道质量风险（reject/q1/q2 扣量、回传缺口），指出需核查的渠道。",
        "scaling_capacity": "以下是近 7 日各 Campaign 利润/Margin 与各自的 Cap 设置，请评估哪些 Campaign 有放量空间（盈利高 Margin 但 Cap 偏低）、哪些 Cap 被浪费（亏损仍挂 Cap）。",
        "campaign_detail": "以下是单个 Campaign 的配置、质量时序、CTIT/ETIT 分布与指标趋势，请做微观下钻分析（漏斗/事件质量/放量空间）。",
        "advertiser_deepdive": "以下是广告主档案与日 KPI 趋势，请分析该广告主的整体消耗与旗下活动表现。",
        "traffic_policy_check": "以下是该渠道/活动的映射、替换与屏蔽规则，请核查流量策略配置是否完整、是否存在风险。",
        "kpi_compare": "以下是该 Campaign 的指标趋势与同期对比，请分析波动原因与环比变化。",
        "campaign_ranking": "以下是各 Campaign 的环比涨跌榜（rising/falling，字段含 campaign_id、campaign_name、revenue、revenue_change）。请找出利润/营收环比下滑最快的 Campaign，给出 campaign_id、名称、下滑幅度与可能原因。注意：涨跌榜口径为营收(revenue)环比，接口未提供独立利润(profit)环比字段，若用户问「利润」请明确说明并以营收环比作答。",
        "pkg_deepdive": "以下是该包名在系统中的归属（广告主/渠道映射）与近 7 日跨 Campaign 营收/利润/Margin 表现，请分析该包的跑量情况、主要投放渠道与转化效率，并说明数据口径（来自 pkg-maps 归因 + /report 聚合，非单 campaign 视角）。",
        "owner_performance": "以下是该 AM/BD 负责人名下所有广告主近 7 日的消耗与利润表现，请汇总其业绩（总营收/利润、头部广告主、异常项），并说明数据口径（/advertisers?am|bd= 解析名下广告主 + /report 聚合）。",
    }.get(analysis_type, "请基于以下数据做商业分析。")

    return (
        f"{intro}\n\n"
        f"分析类型: {analysis_type}\n\n"
        f"```json\n{_compact_json(ctx)}\n```\n\n"
        "请严格按 System Prompt 的结构化版式输出分析简报（固定章节 + 必要时用表格）。"
    )


def _compact_json(obj, max_len: int = 6000) -> str:
    import json
    try:
        s = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        s = str(obj)
    return s if len(s) <= max_len else s[:max_len] + "\n... (上下文已截断)"


# ---------------------------------------------------------------------------
# 编排：数据加载 -> LLM 简报
# ---------------------------------------------------------------------------

def process_data_analysis_query(
    analysis_type: str,
    connector,
    llm,
    token: Optional[str] = None,
    params: Optional[dict] = None,
    operator_id: str = "anonymous",
    token_mode: str = "system",
) -> dict:
    """端到端执行一次数据分析。

    - connector: TeensingDataConnector 实例（提供 api_get）
    - llm: 实现 complete(system, user, json_mode=False) -> str 的客户端
           注意：BI 简报用 json_mode=False 返回 Markdown 文本
    - token: 上游 Teensing 取数用的 access_token（来自调用方 X-Teensing-Token，
             缺失时回退 TESS_SYSTEM_TOKEN）；决定「按谁的数据权限」回数据。
    - operator_id / token_mode: 审计字段，原样回显到 context_summary。
    """
    ctx = fetch_bi_analysis_context(connector, analysis_type, token=token, params=params)
    ctx = _enrich_names_with_ids(ctx)  # 名称追加 (id)，便于核对实体
    user_prompt = _build_user_prompt(analysis_type, ctx)
    report = llm.complete(BI_SYSTEM_PROMPT, user_prompt, json_mode=False)
    return {
        "analysis_type": analysis_type,
        "report": report,
        "context_summary": {
            "analysis_type": ctx.get("analysis_type"),
            "date_or_month": ctx.get("date") or ctx.get("report_month") or ctx.get("time_range"),
            "errors": ctx.get("errors", []),
            "operator_id": operator_id,
            "token_mode": token_mode,  # "user"=按调用方 token 权限取数; "system"=系统 token
        },
    }


# ---------------------------------------------------------------------------
# 自然语言问答（Tess AI Assistant 对话接口 /tess/ask）
# ---------------------------------------------------------------------------

ASK_SYSTEM_PROMPT = """你叫 Tess，是 Teensing 平台的专属 AdTech 智能数据分析助手。\
用户会用自然语言提问关于投放、营收、Campaign、渠道、异常诊断等业务问题。

请根据【下方提供的 Teensing 业务数据上下文】用简体中文作答：
- 观点先行：先给结论，再用数据支撑，最后给可执行建议；
- 数据必须来自上下文，严禁编造上下文里没有的数字、Campaign 名或实体；
- 若上下文不足以回答，明确说明「数据不足，暂无法确认」，不要臆测；
- 实体名称已附带 (id)，引用 Campaign / Publisher / Advertiser 时务必保留其 (id)，便于定位核对。
- 若回答涉及多个实体或多项数值对比，**优先用 Markdown 表格**呈现（列示例：| 实体 (id) | 指标 | 数值 |），避免用成段文字堆叠。
- 回答控制在 400 字以内，语气专业、简洁，禁止客套寒暄。
"""


# ---------------------------------------------------------------------------
# /tess/ask 深度下钻：可选 analysis_type（显式透传 OR 后端关键词推断）+ 浅层兜底
# ---------------------------------------------------------------------------

# 与 /tess/analytics 共用同一套深度取数层（fetch_bi_analysis_context）
ANALYSIS_TYPES = {
    "daily_summary",
    "scaling_opportunity",
    "finance_check",
    "account_overview",
    "publisher_deepdive",
    "scaling_capacity",
    "campaign_detail",
    "advertiser_deepdive",
    "traffic_policy_check",
    "kpi_compare",
    "campaign_ranking",
    "pkg_deepdive",
    "owner_performance",
}

# 关键词路由表（优先级自上而下：先匹配更具体的类型）。
# 英文关键词统一小写匹配；中文不区分大小写。
_ANALYSIS_KEYWORDS: list = [
    ("scaling_capacity", ["容量", "cap", "放量空间", "还能放", "预算上限", "放量容量", "容量评估"]),
    ("finance_check", ["对账", "毛利", "结算", "营收核对", "对账差异", "月报", "财务", "month", "invoice"]),
    ("publisher_deepdive", ["渠道", "publisher", "媒体质量", "扣量", "作弊", "渠道质量"]),
    ("account_overview", ["账户全景", "整体大盘", "总览", "概览", "全景", "account overview"]),
    ("daily_summary", ["复盘", "每日", "昨日", "昨天", "日报", "今日表现", "daily summary"]),
    ("scaling_opportunity", ["放量", "扩量", "加预算", "增长机会", "潜力", "机会", "加大投放", "scale"]),
    ("campaign_detail", ["ctit", "etit", "漏斗", "转化时间", "事件质量", "活动详情", "单活动", "campaign详情"]),
    ("advertiser_deepdive", ["广告主", "advertiser", "主户", "客户"]),
    ("traffic_policy_check", ["替换渠道", "切量", "切流量", "流量策略", "屏蔽", "block", "replace", "映射", "渠道映射"]),
    ("campaign_ranking", ["利润环比下滑", "利润下滑", "营收下滑", "环比下滑", "下滑最快", "跌幅最大", "掉得最快", "降幅最大", "哪个campaign", "哪个活动", "哪个 campaign", "谁掉得最快", "利润下降最快", "营收下降最快"]),
    ("pkg_deepdive", ["包名", "这个包", "应用包", "包的表现", "包跑量", "package", "pkg"]),
    ("owner_performance", ["负责人名下", "am 名下", "bd 名下", "名下客户", "名下广告主", "业绩盘点", "手上的客户", "负责的渠道"]),
    ("kpi_compare", ["环比", "对比", "波动", "暴跌", "暴涨", "趋势", "trend", "对比昨日"]),
]


def infer_analysis_type(question: str) -> Optional[str]:
    """用轻量关键词把自然语言问题映射到深度 analysis_type。

    命中即返回对应类型；都不命中返回 None（调用方应退回浅层全局上下文）。
    关键词路由不发起额外 LLM 调用，零成本、确定性、可预期。
    后续可升级为 LLM 路由（更准但多一次推理开销）。
    """
    if not question:
        return None
    q = question.lower()
    for atype, kws in _ANALYSIS_KEYWORDS:
        for kw in kws:
            if kw.lower() in q:
                return atype
    return None


def extract_entities(question: str, params: Optional[dict]) -> dict:
    """从自然语言问题中抽取五维实体（Campaign / Advertiser / Publisher / Package / Owner）。

    返回实体 dict（均为已识别的原始值，id 类可能为 None，名称/代号类记录待解析）：
        {
          "campaign_id":   str|None,
          "advertiser_id": str|None, "advertiser_name": str|None,
          "publisher_id":  str|None, "publisher_name": str|None, "channel": str|None,
          "package_name":  str|None,
          "owner_name":    str|None, "owner_role": "am"|"bd"|None,
          "owner_user_id": str|None,
        }
    显性参数（params 里已带的 id）优先级最高，直接采纳。

    注意：仅做「抽取」，不做名称->id 的 API 解析；解析在 resolve_entities 中完成
    （需要 connector）。两条语序都支持：数字+关键字 与 关键字+数字。
    """
    params = params or {}
    q = (question or "").lower()
    out = {
        "campaign_id": None, "advertiser_id": None, "advertiser_name": None,
        "publisher_id": None, "publisher_name": None, "channel": None,
        "package_name": None, "owner_name": None, "owner_role": None,
        "owner_user_id": None,
    }
    # 1) 显性参数优先（前端胶囊/显式透传）
    if params.get("campaign_id"):
        out["campaign_id"] = str(params["campaign_id"])
    if params.get("advertiser_id"):
        out["advertiser_id"] = str(params["advertiser_id"])
    if params.get("publisher_id"):
        out["publisher_id"] = str(params["publisher_id"])
    if params.get("package_name"):
        out["package_name"] = str(params["package_name"])
    if params.get("owner_user_id"):
        out["owner_user_id"] = str(params["owner_user_id"])
        out["owner_name"] = params.get("owner_name") or out["owner_name"]
        out["owner_role"] = params.get("owner_role") or out["owner_role"]

    # 通用：关键字在前、数字/代号在后，中间最多 15 个非数字字符（支持夹 id/分隔符）
    KW_BEFORE = r"(?:{kw})[^\d]{{0,15}}?(\d{{4,}})"

    # 2) Campaign（数字+camp / 关键字+数字 / id\d+ 紧贴 ctit/etit）
    if not out["campaign_id"]:
        m = re.search(r"(\d{4,})\s*(?:camp|campaign)\b", q)
        if not m:
            m = re.search(KW_BEFORE.format(kw=r"camp|campaign|cid|campaign_id"), q)
        if m:
            out["campaign_id"] = m.group(1)
        else:
            has_ctit = re.search(r"\b(ctit|etit)\b", q)
            if has_ctit:
                m2 = re.search(r"(?:id|#)\s*(\d{4,})", q)
                if m2:
                    out["campaign_id"] = m2.group(1)

    # 3) Advertiser（adv_数字 / advertiser 数字 / 广告主+名称）
    if not out["advertiser_id"]:
        m = (re.search(r"adv[:_\s]*(\d{4,})", q)
             or re.search(r"(\d{4,})\s*(?:adv|advertiser)\b", q)
             or re.search(r"(?:adv|advertiser|广告主|客户)[\s_:：\-]{0,5}(\d{4,})", q))
        if m:
            out["advertiser_id"] = m.group(1)
        else:
            # 名称形式：广告主/客户 前或后紧跟 ASCII 名称（如 oppo-mmp-Betty / Adv_xxx）
            m2 = (re.search(r"([A-Za-z0-9][A-Za-z0-9_\-]{2,})\s*(?:这个广告主|的广告主|这个客户|的客户|广告主)", q)
                  or re.search(r"(?:广告主|客户)[\s_:：\-]{0,5}([A-Za-z0-9][A-Za-z0-9_\-]{2,})", q))
            if m2:
                out["advertiser_name"] = m2.group(1)

    # 4) Publisher（pub_数字 / publisher 数字 / 渠道 数字 / sub_mkt 代号 / 渠道+名称）
    if not out["publisher_id"]:
        m = (re.search(r"pub[:_\s]*(\d{2,})", q)
             or re.search(r"(\d{2,})\s*(?:pub|publisher)\b", q)
             or re.search(r"(?:pub|publisher|渠道|媒体)[\s_:：\-]{0,5}(\d{2,})", q))
        if m:
            out["publisher_id"] = m.group(1)
        if not out["publisher_id"] and not out["publisher_name"]:
            # 渠道代号（sub_mkt_X 或 ASCII 代号）或渠道名称；纯 ASCII 限定避免误吞中文词
            m2 = (re.search(r"([A-Za-z0-9_]*sub_mkt[A-Za-z0-9_\-]*)", q)
                  or re.search(r"([A-Za-z0-9][A-Za-z0-9_\-]{2,})\s*(?:这个渠道|的渠道|这个媒体|的媒体|渠道)", q)
                  or re.search(r"(?:渠道|媒体)[\s_:：\-]{0,5}([A-Za-z0-9][A-Za-z0-9_\-]{2,})", q))
            if m2:
                tok = m2.group(1)
                if tok.isdigit():
                    out["publisher_id"] = tok
                elif tok.lower().startswith("sub_mkt") or re.search(r"[A-Za-z]", tok):
                    out["channel"] = tok
                else:
                    out["publisher_name"] = tok

    # 5) Package Name（com.x.y 点分标识符 / pkg: / 包名）
    if not out["package_name"]:
        m = (re.search(r"([a-z][a-z0-9_]*\.[a-z0-9_]+(?:\.[a-z0-9_]+)+)", q)
             or re.search(r"pkg[:\s:]*([\w.]+)", q)
             or re.search(r"包名[^\w]{0,5}([\w.]+)", q))
        if m:
            out["package_name"] = m.group(1)

    # 6) Owner（AM/BD/负责人 + 名称；或「名称 的客户/负责的/手上的/名下」）
    if not out["owner_name"] and not out["owner_user_id"]:
        m = re.search(r"\b(am|bd|负责人)[\s:：]*([\w\u4e00-\u9fa5]+)", q)
        if m:
            role = m.group(1).lower()
            out["owner_role"] = "am" if role == "am" else ("bd" if role == "bd" else None)
            out["owner_name"] = m.group(2)
        else:
            m2 = re.search(r"([\w\u4e00-\u9fa5]+)(?:\s*(?:的客户|负责的|手上|名下|管理))", q)
            if m2:
                out["owner_name"] = m2.group(1)
    return out


def resolve_entities(entities: dict, connector, token: Optional[str] = None) -> dict:
    """把 extract_entities 抽出的名称/代号解析成可下钻的 id（需要 connector 调 API）。

    成功解析则回填对应 *_id 字段；解析失败保持 None，由上层路由决定兜底。
    解析路径（均经实测存在）：
      - 广告主名 -> /advertisers 列表扫描 name 子串（API 的 name/search 过滤无效）
      - 渠道代号 -> /mapping-publisher-channels?channel= 反解 publisher_id
      - 渠道名   -> /publishers 列表扫描 name 子串
      - 负责人名 -> /users/options 扁平用户目录按 name/real_name 子串匹配
    """
    e = dict(entities)

    # 广告主名称 -> id
    if not e.get("advertiser_id") and e.get("advertiser_name"):
        aid = _find_in_list(connector, "/advertisers", e["advertiser_name"], token,
                            id_field="id", name_field="name")
        if aid:
            e["advertiser_id"] = aid

    # 渠道：代号 -> 映射反解；名称 -> 列表扫描
    if not e.get("publisher_id") and (e.get("publisher_name") or e.get("channel")):
        if e.get("channel"):
            ch, _ = _safe_api_get(connector, "/mapping-publisher-channels",
                                  params={"channel": e["channel"], "page": 1, "page_size": 5}, token=token)
            items = (ch or {}).get("items", []) if isinstance(ch, dict) else []
            if items:
                e["publisher_id"] = _fmt_id(items[0].get("publisher_id"))
        if not e.get("publisher_id") and e.get("publisher_name"):
            pid = _find_in_list(connector, "/publishers", e["publisher_name"], token,
                                id_field="id", name_field="name")
            if pid:
                e["publisher_id"] = pid

    # 负责人名称 -> user id
    if not e.get("owner_user_id") and e.get("owner_name"):
        opts, _ = _safe_api_get(connector, "/users/options", token=token)
        opts = opts if isinstance(opts, list) else []
        target = e["owner_name"].lower()
        for u in opts:
            nm = str(u.get("name") or "").lower()
            rn = str(u.get("real_name") or "").lower()
            if target in nm or target in rn or nm.startswith(target) or rn.startswith(target):
                e["owner_user_id"] = _fmt_id(u.get("id"))
                break
    return e


def _find_in_list(connector, path, name, token, id_field="id", name_field="name", max_pages=10):
    """分页扫描某列表接口，按 name 子串匹配返回首个 id（用于名称->id 解析）。"""
    target = (name or "").lower()
    if not target:
        return None
    for pg in range(1, max_pages + 1):
        d, _ = _safe_api_get(connector, path, params={"page": pg, "page_size": 100}, token=token)
        items = (d or {}).get("items", []) if isinstance(d, dict) else []
        if not items:
            break
        for it in items:
            nm = str(it.get(name_field) or "").lower()
            if target in nm or nm.startswith(target):
                return _fmt_id(it.get(id_field))
        if len(items) < 100:
            break
    return None


def _aggregate_report(items):
    """把 /report 多行归集为 total / by_campaign / by_publisher 汇总。"""
    if not items:
        return {"total": {"revenue": 0, "profit": 0, "payout": 0, "clicks": 0, "conversions": 0},
                "by_campaign": [], "by_publisher": []}
    tot = {"revenue": 0.0, "profit": 0.0, "payout": 0.0, "clicks": 0, "conversions": 0}
    by_c, by_p = {}, {}
    for it in items:
        rev = _to_float(it.get("revenue")); pr = _to_float(it.get("profit"))
        po = _to_float(it.get("payout")); cl = int(_to_float(it.get("clicks")))
        cv = int(_to_float(it.get("conversions")))
        tot["revenue"] += rev; tot["profit"] += pr; tot["payout"] += po
        tot["clicks"] += cl; tot["conversions"] += cv
        ckey = _fmt_id(it.get("campaign_id"))
        if ckey:
            c = by_c.setdefault(ckey, {"campaign_id": ckey, "campaign_name": it.get("campaign_name"),
                                      "revenue": 0.0, "profit": 0.0, "conversions": 0})
            c["revenue"] += rev; c["profit"] += pr; c["conversions"] += cv
        pkey = _fmt_id(it.get("publisher_id"))
        if pkey:
            p = by_p.setdefault(pkey, {"publisher_id": pkey, "publisher_name": it.get("publisher_name"),
                                      "revenue": 0.0, "profit": 0.0, "conversions": 0})
            p["revenue"] += rev; p["profit"] += pr; p["conversions"] += cv
    for c in by_c.values():
        c["revenue"] = round(c["revenue"], 2); c["profit"] = round(c["profit"], 2)
    for p in by_p.values():
        p["revenue"] = round(p["revenue"], 2); p["profit"] = round(p["profit"], 2)
    return {
        "total": {k: round(v, 2) for k, v in tot.items()},
        "by_campaign": sorted(by_c.values(), key=lambda x: x["revenue"], reverse=True)[:8],
        "by_publisher": sorted(by_p.values(), key=lambda x: x["revenue"], reverse=True)[:8],
    }


def extract_entity_id(question: str, params: Optional[dict]) -> tuple:
    """向后兼容薄封装：返回 (analysis_type 或 None, params)。

    新代码建议直接用 extract_entities + resolve_entities。此处保留以兼容旧调用方。
    """
    ents = extract_entities(question, params)
    if ents.get("campaign_id"):
        return "campaign_detail", {**params, "campaign_id": ents["campaign_id"]}
    if ents.get("advertiser_id"):
        return "advertiser_deepdive", {**params, "advertiser_id": ents["advertiser_id"]}
    if ents.get("publisher_id"):
        return "publisher_deepdive", {**params, "publisher_id": ents["publisher_id"]}
    return None, params


def fetch_qa_context(connector, token: Optional[str] = None, question: str = "") -> dict:
    """拉取一个紧凑的「全局态势」上下文作为问答 grounding。

    使用调用方透传的 token（按该用户权限取数），与 analytics 同源。
    多个子源独立容错，单个失败只在该源的 errors 里体现。
    """
    today = datetime.now()
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    daily_kpi, e1 = _safe_api_get(
        connector, "/overview/daily-kpi", params={"date": yesterday}, token=token
    )
    ranking, e2 = _safe_api_get(connector, "/overview/ranking", token=token)
    anomaly, e3 = _safe_api_get(connector, "/overview/ranking/anomaly-warning", token=token)
    quality, e4 = _safe_api_get(connector, "/campaign-quality/publisher", token=token)
    return {
        "daily_kpi_yesterday": (daily_kpi or {}).get("items") if isinstance(daily_kpi, dict) else daily_kpi,
        "ranking_top": (ranking if isinstance(ranking, list) else [])[:10],
        "anomaly_warning": (anomaly if isinstance(anomaly, list) else [])[:10],
        "quality_summary": quality,
        "errors": [e for e in (e1, e2, e3, e4) if e],
    }


def _trim_for_prompt(ctx: dict, limit: int = 6000) -> str:
    """把上下文序列化为适合塞进 LLM prompt 的紧凑 JSON 文本（截断保护）。"""
    try:
        text = json.dumps(ctx, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(ctx)
    if len(text) > limit:
        text = text[:limit] + "\n...（上下文过长已截断）"
    return text


def process_question(
    question: str,
    connector,
    llm,
    token: Optional[str] = None,
    params: Optional[dict] = None,
    operator_id: str = "anonymous",
    token_mode: str = "system",
    analysis_type: Optional[str] = None,
) -> dict:
    """端到端执行一次自然语言问答，支持深度下钻。

    上下文选择优先级（①②③ 走深度取数层，④ 走浅层全局兜底）：
      ① 显式 analysis_type（前端胶囊透传）        -> route_source="explicit"
      ② 五维实体抽取（extract_entities+resolve_entities）-> 按命中优先级
         campaign_id > advertiser_id > publisher_id > package_name > owner_user_id
         各自下钻到 campaign_detail / advertiser_deepdive / publisher_deepdive /
         pkg_deepdive / owner_performance，route_source="entity"
      ③ 关键词推断 analysis_type（infer_analysis_type）-> 命中则同样走深度上下文，route_source="inferred"
      ④ 三者皆无/不合法                           -> 退回浅层全局上下文 fetch_qa_context（原行为，无 route_source）

    - connector / llm / token：与 analytics 同源（token 决定按谁的数据权限取数）
    - 返回 answer（Markdown），并附 result / data 别名以兼容调用方 .answer/.result/.data 取值
    - context_summary 在走深度上下文时额外回显 analysis_type / route_source（explicit|entity|inferred）/ date_or_month
    """
    # —— 1. 决定用哪套上下文 ——
    route_source = None
    if analysis_type in ANALYSIS_TYPES:
        route_source = "explicit"
    else:
        # ② 五维实体抽取（优先级高于关键词表）：Campaign/Advertiser/Publisher/Package/Owner
        ents = extract_entities(question, params)
        ents = resolve_entities(ents, connector, token)  # 名称/代号 -> 可下钻 id
        params = params or {}  # 确保后续 {**params, ...} 字典展开安全（process_question 默认 params=None）
        ent_type = None
        if ents.get("campaign_id"):
            ent_type, params = "campaign_detail", {**params, "campaign_id": ents["campaign_id"]}
        elif ents.get("advertiser_id"):
            ent_type, params = "advertiser_deepdive", {**params, "advertiser_id": ents["advertiser_id"]}
        elif ents.get("publisher_id"):
            ent_type, params = "publisher_deepdive", {**params, "publisher_id": ents["publisher_id"]}
        elif ents.get("package_name"):
            ent_type, params = "pkg_deepdive", {**params, "package_name": ents["package_name"]}
        elif ents.get("owner_user_id"):
            ent_type, params = "owner_performance", {**params, "owner_user_id": ents["owner_user_id"],
                                                    "owner_role": ents.get("owner_role"),
                                                    "owner_name": ents.get("owner_name")}
        if ent_type:
            analysis_type = ent_type
            route_source = "entity"
        else:
            inferred = infer_analysis_type(question)
            if inferred:
                analysis_type = inferred
                route_source = "inferred"

    if route_source:  # ①②③ 走深度上下文（与 /tess/analytics 同一套取数）
        ctx = fetch_bi_analysis_context(connector, analysis_type, token=token, params=params)
        ctx = _enrich_names_with_ids(ctx)  # 名称追加 (id)，便于核对实体
        ctx_text = _trim_for_prompt(ctx, limit=9000)  # 深度上下文更大，放宽截断
        user_prompt = (
            f"【Teensing 业务数据上下文（深度下钻：{analysis_type}）】\n"
            f"{ctx_text}\n\n"
            f"【用户问题】\n{question}\n\n"
            "请结合上方深度数据作答；若数据不足以回答，明确告知「数据不足，暂无法确认」，不要臆测。"
        )
        summary_extra = {
            "analysis_type": analysis_type,
            "route_source": route_source,
            "date_or_month": ctx.get("date") or ctx.get("report_month") or ctx.get("time_range"),
        }
    else:  # ③ 浅层全局兜底（保持原行为）
        ctx = fetch_qa_context(connector, token=token, question=question)
        ctx = _enrich_names_with_ids(ctx)  # 名称追加 (id)，便于核对实体
        ctx_text = _trim_for_prompt(ctx, limit=6000)
        user_prompt = (
            "【Teensing 业务数据上下文】\n"
            f"{ctx_text}\n\n"
            f"【用户问题】\n{question}\n\n"
            "请基于上方上下文作答；若上下文无法支撑，明确告知数据不足。"
        )
        summary_extra = {}

    answer = llm.complete(ASK_SYSTEM_PROMPT, user_prompt, json_mode=False)
    context_summary = {
        "endpoint": "/tess/ask",
        "errors": ctx.get("errors", []),
        "operator_id": operator_id,
        "token_mode": token_mode,
        **summary_extra,
    }
    return {
        "answer": answer,
        "result": answer,  # 兼容调用方 .answer/.result/.data 取值
        "data": answer,
        "context_summary": context_summary,
    }
