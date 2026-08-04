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
  GET /campaigns                   -> Campaign 主数据目录 {total,items:[{id,name,advertiser_id,country,cap,click_cap,payout_event,status,kpi,...}]}
  GET /publishers                  -> Publisher(渠道) 目录 {total,items:[{id,name,margin,payment_terms,click_caps,postback_url,...}]}
  GET /advertisers                 -> Advertiser(广告主) 目录 {total,items:[{id,name,bd,am,margin,contract_valid_to,...}]}

已确认不可用（HTTP 404，本模块不依赖）：
  /external/invoices、/overview/summary|performance|kpi|quality|conversion|advertiser、
  /report/day|daily|campaign|publisher、/campaign-quality/campaign|advertiser、/campaign/list、
  /statistics/overview、/dashboard、/metrics、/kpi。
  （/report/export 存在但返回文件流非 JSON，BI 简报不适用。）
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# BI 分析系统提示词（观点先行 / 数据支撑 / 动作导向）
# ---------------------------------------------------------------------------

BI_SYSTEM_PROMPT = """你叫 Tess，是 Teensing 平台的专属 AdTech 商业智能（BI）与数据分析助手。\
你的职责是基于传入的结构化业务数据，为运营或管理层输出有价值、可落地的洞察与优化建议。

【分析原则】
1. 观点先行：第一句必须直接给出核心结论（例如："昨日整体利润环比 +12%，主要由 Campaign_7030 的高 Margin 拉动"）。
2. 数据支撑：任何结论都必须带上具体数值（Revenue / Profit / Margin% / CVR% / 环比变化%），不得空泛。
3. 动作导向：分析末尾必须给出 2-3 条可执行的【推荐动作】(Recommended Actions)。

【输出格式（严格按此 Markdown 模板，使用 emoji 标题）】
📊 **数据复盘 / 洞察摘要**
- [核心结论 1，含数值]
- [核心结论 2，含数值]

💡 **潜能点 / 风险点**
- [点 1]：数据表现 + 可能原因（用数据佐证）
- [点 2]：数据表现 + 可能原因

🚀 **推荐执行动作 (Recommended Actions)**
1. 针对 [Publisher/Campaign]，建议 [加大 Cap / 调整出价 / 核查扣量 / 暂停亏损计划]（给出具体数字建议）。
2. ...

【红线】
- 严禁编造数据中不存在的实体、数值或趋势；如果某维度数据为空/缺失，明确说明"该维度暂无数据，建议补充口径"，不要臆测。
- 涉及金额/百分比必须直接引用输入数值，不得自行计算篡改。
- 输出全部使用中文，语气专业、简洁，禁止客套寒暄。
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
      'scaling_capacity'       -> 放量容量评估（Campaign Cap + 近 7 日利润/Margin）

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
        month = params.get("report_month") or today.strftime("%Y-%m")
        month_res, err_month = _safe_api_get(
            connector, "/report/month",
            params={"report_month": month, "page_size": 50},
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
            "report_month": month,
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

        return {
            "analysis_type": "publisher_deepdive",
            "publisher_total": (pubs or {}).get("total") if isinstance(pubs, dict) else None,
            "quality_publisher_count": len(q_items),
            "top_publishers_by_clicks": enriched[:8],
            "flagged_quality_issues": flagged[:10],
            "errors": [e for e in (err_p, err_q) if e],
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

        return {
            "analysis_type": "scaling_capacity",
            "time_range": f"{start_7d} ~ {end}",
            "campaigns_with_performance": len(candidates),
            "scaling_room": scaling_room,
            "over_cap_waste": over_cap_waste,
            "top_by_profit": candidates[:8],
            "errors": [e for e in (err_r, err_c) if e],
        }

    else:
        raise ValueError(
            f"未知的 analysis_type={analysis_type!r}；"
            "支持: daily_summary / scaling_opportunity / finance_check / "
            "account_overview / publisher_deepdive / scaling_capacity"
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
    }.get(analysis_type, "请基于以下数据做商业分析。")

    return (
        f"{intro}\n\n"
        f"分析类型: {analysis_type}\n\n"
        f"```json\n{_compact_json(ctx)}\n```\n\n"
        "请严格按 System Prompt 的 Markdown 模板输出分析简报。"
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
