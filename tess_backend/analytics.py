"""P4 · Tess 数据分析模块（主动式 BI & Insights）。

定位：把 Tess 从「被动告警排查」升级为「主动式商业智能助手」。
运营日常不只是想看"哪里出错了"，更想知道：
  ① 每日/每周大盘与绩效复盘（谁跑得好）
  ② 哪些 Campaign 还有放量空间（增长挖掘）
  ③ 本月对账/毛利趋势是否符合预估（财务异动）

设计上完全复用 TeensingDataConnector 的传输层（Bearer token、Cloudflare UA、
{code,data} 外壳 unwrap），只新增"分析型"端点编排，不再引入新依赖。

真实可用端点（已用生产 token 探测确认）：
  GET /overview/daily-kpi        -> 逐日大盘 [{date,clicks,conversions,revenue,payout,profit}]
  GET /overview/ranking          -> 排名榜 [{campaign_id,name,clicks,cvr,revenue,payout,margin,revenue_change,...}]
  GET /overview/ranking/fluctuation -> {rising:[...], falling:[...]}
  GET /report                    -> 通用聚合报表（dimensions=date|campaign|publisher,...）
  GET /campaign-quality/publisher-> 各 publisher 质量/扣量数据
  GET /report/month              -> 月度报表 {items, total_data:{revenue,scrub_revenue,calc_revenue,payout,...}}

未实现（404，故本模块不依赖）：
  GET /external/archived-invoices  —— 对账差异改用 /report/month 的 revenue vs calc_revenue 代替。
"""

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

    else:
        raise ValueError(
            f"未知的 analysis_type={analysis_type!r}；"
            "支持: daily_summary / scaling_opportunity / finance_check"
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


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
) -> dict:
    """端到端执行一次数据分析。

    - connector: TeensingDataConnector 实例（提供 api_get）
    - llm: 实现 complete(system, user, json_mode=False) -> str 的客户端
           注意：BI 简报用 json_mode=False 返回 Markdown 文本
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
        },
    }
