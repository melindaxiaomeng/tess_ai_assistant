"""P5 · 数据接入层 —— 从 Teensing 真实异常数据源拉取（轮询）异常事件，
归一化为 PRD §4.1 标准 Context，交给编排层 run_diagnosis 诊断。

设计要点：
- DataConnector 为抽象接口（Protocol），便于测试时换 Mock、生产时换 Teensing。
- TeensingDataConnector 读环境变量（TESS_DATA_API_BASE_URL 等），调用真实异常数据 API。
- **鉴权透传（P6）**：token 不在 Tess 落库，而是「每次调用由调用方经请求头
  X-Teensing-Token 传入」，connector 原样作为 Bearer 转发给 Teensing；
  Teensing 按该运营的 RBAC/数据权限返回数据 —— 实现「按访问者权限回数据」。
- normalize_to_context() 兼容两种上游形状：
  (a) 直接就是 PRD §4.1 Context（Mock 样例走这里）；
  (b) Teensing fluctuation/anomaly 形状（含 name/change/revenue/profit/margin）。
- 默认用标准库 urllib（与 tess_agent.HttpLLMClient 一致），零额外依赖。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Optional, Protocol

logger = logging.getLogger("tess_backend.data_connector")

# campaign 级告警的最低营收门槛：低于该值的低价值 campaign 不送诊断（降噪）。
# 可通过环境变量 TESS_MIN_REVENUE_USD 调整，默认 20 美元。
MIN_REVENUE_USD = float(os.getenv("TESS_MIN_REVENUE_USD", "20"))

# 开发/测试用样例原始事件（结构接近 Teensing 预期返回，已可直接归一化为 Context）
_SAMPLE_RAW = {
    "event_id": "ERR-20260728-0912",
    "trigger_time": "2026-07-28 14:00:00",
    "target_metric": "Overall Margin",
    "current_value": "3.8%",
    "benchmark_value": "14.2%",
    "severity": "HIGH",
    "calculated_loss": {
        "loss_per_hour_usd": 350.0,
        "calculation_basis": "基于过去 30 分钟 Cost 速率与缺失 Revenue 的差值计算",
    },
    "top_contributors": [
        {
            "dimension_type": "Publisher",
            "dimension_value": "Pub_Media_802",
            "impact_share": "82%",
            "metric_change": "Margin 从 15.1% 降至 -2.4%",
        }
    ],
        "associated_signals": [
        {
            "source": "AppsFlyer_Pull_API",
            "status": "WARNING",
            "detail": "13:30-14:00 期间 Postback 接口 HTTP 504 (Gateway Timeout) 占比 45%",
        }
    ],
}

# 开发/测试用样例：realtime-kpi 真实结构（data.items[] 逐小时 today/yesterday 同比）。
# hour 12 模拟一次 Revenue 同比暴跌 50%，便于单测默认阈值命中、高阈值(0.99)不命中。
def _build_sample_realtime_kpi():
    rows = [
        ("00", 1387.795, 1202.416),
        ("01", 1726.969, 1473.080),
        ("02", 1758.089, 1409.218),
        ("03", 1671.006, 1670.051),
        ("04", 1651.021, 1596.898),
        ("05", 1642.383, 1518.930),
        ("06", 1645.222, 1477.255),
        ("07", 1333.766, 1429.479),
        ("08", 1357.255, 1390.104),
        ("09", 1300.000, 1400.000),
        ("10", 1280.000, 1400.000),
        ("11", 1250.000, 1400.000),
        ("12", 700.000, 1400.000),
        ("13", 1300.000, 1400.000),
        ("14", 1280.000, 1400.000),
        ("15", 1250.000, 1400.000),
        ("16", 1200.000, 1400.000),
        ("17", 1250.000, 1400.000),
        ("18", 1300.000, 1400.000),
        ("19", 1280.000, 1400.000),
        ("20", 1300.000, 1400.000),
        ("21", 1250.000, 1400.000),
        ("22", 1200.000, 1400.000),
        ("23", 1100.000, 1400.000),
    ]
    items = [
        {
            "hour": h,
            "today_revenue": today,
            "today_clicks": int(today * 1500),
            "today_conversions": int(today * 4),
            "yesterday_revenue": yest,
            "yesterday_clicks": int(yest * 1800),
            "yesterday_conversions": int(yest * 4),
        }
        for h, today, yest in rows
    ]
    return {"code": 0, "message": "success", "data": {"items": items}, "meta": ""}


_SAMPLE_REALTIME_KPI = _build_sample_realtime_kpi()


class DataConnector(Protocol):
    """数据接入抽象：拉取真实异常事件，归一化为 Tess Context。

    token: 调用方传入的「运营 SaaS access_token」，用于按访问者权限拉数据；
           具体实现若不依赖鉴权（如 Mock）可忽略该参数。
    """

    def fetch_recent_anomalies(self, limit: int = 20, token: Optional[str] = None) -> list[dict]:
        """拉取最近 limit 个原始异常事件（Teensing 原始结构）。"""
        ...

    def fetch_anomaly_event(self, event_id: str, token: Optional[str] = None) -> Optional[dict]:
        """按 event_id 拉取单个原始异常事件；不存在返回 None。"""
        ...

    def fetch_realtime_kpi(self, token: Optional[str] = None) -> dict:
        """拉取实时 KPI 小时级曲线（GET /overview/realtime-kpi）。"""
        ...

    def fetch_campaign_time_series(
        self, campaign_id: str, token: Optional[str] = None,
        days: int = 7, granularity: str = "day",
    ) -> dict:
        """拉取指定 Campaign 连续历史趋势（GET /report），为诊断提供时间锚点。"""
        ...


class MockDataConnector:
    """开发/测试用：返回内置样例原始事件，无需任何外部依赖。"""

    def __init__(self, samples: Optional[list[dict]] = None):
        self.samples: list[dict] = samples if samples is not None else [_SAMPLE_RAW]

    def fetch_recent_anomalies(self, limit: int = 20, token: Optional[str] = None) -> list[dict]:
        return self.samples[:limit]

    def fetch_anomaly_event(self, event_id: str, token: Optional[str] = None) -> Optional[dict]:
        for s in self.samples:
            if s.get("event_id") == event_id:
                return s
        return None

    def fetch_realtime_kpi(self, token: Optional[str] = None) -> dict:
        """返回内置样例实时 KPI 曲线，含一处骤降异常，供开发/测试直接跑通。"""
        return _SAMPLE_REALTIME_KPI

    def fetch_campaign_time_series(
        self, campaign_id, token=None, days=7, granularity="day"
    ) -> dict:
        """测试用：返回一条带断崖式下跌的 7 天样例序列，验证 history_baseline 透传。"""
        series = [
            {"timestamp": "2026-07-28", "revenue": 520.0, "profit": 230.1, "cvr_percent": 1.15, "margin_percent": 44.25, "clicks": 5000, "conversions": 57},
            {"timestamp": "2026-07-29", "revenue": 413.8, "profit": 182.0, "cvr_percent": 0.98, "margin_percent": 43.98, "clicks": 4200, "conversions": 41},
            {"timestamp": "2026-07-30", "revenue": 480.2, "profit": 210.0, "cvr_percent": 1.10, "margin_percent": 43.73, "clicks": 4600, "conversions": 50},
            {"timestamp": "2026-07-31", "revenue": 495.0, "profit": 218.5, "cvr_percent": 1.12, "margin_percent": 44.14, "clicks": 4700, "conversions": 52},
            {"timestamp": "2026-08-01", "revenue": 410.0, "profit": 178.0, "cvr_percent": 0.95, "margin_percent": 43.41, "clicks": 4300, "conversions": 41},
            {"timestamp": "2026-08-02", "revenue": 120.0, "profit": -15.0, "cvr_percent": 0.12, "margin_percent": -12.50, "clicks": 1500, "conversions": 2},
            {"timestamp": "2026-08-03", "revenue": 10.0,  "profit": -8.0,  "cvr_percent": 0.05, "margin_percent": -80.00, "clicks": 200, "conversions": 0},
        ]
        return {
            "campaign_id": str(campaign_id),
            "granularity": granularity,
            "range": "2026-07-28 to 2026-08-03",
            "data_points_count": len(series),
            "time_series": series,
        }


class TeensingDataConnector:
    """生产用：轮询 Teensing 真实异常数据 API。

    必需环境变量：TESS_DATA_API_BASE_URL（异常数据 API 根地址，需含 /api/v1 前缀，
                  例：https://saas.teensing.com/api/v1）
    可选：TESS_DATA_API_TIMEOUT（请求超时秒，默认 10）

    鉴权：token 由每次调用透传（来自请求头 X-Teensing-Token），作为 Bearer 发给
          Teensing；环境变量 TESS_DATA_API_KEY 仅作兜底（多数场景不使用）。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 10,
    ):
        self.base_url = (base_url or os.getenv("TESS_DATA_API_BASE_URL", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("TESS_DATA_API_KEY", "")
        self.timeout = int(os.getenv("TESS_DATA_API_TIMEOUT", str(timeout)))
        if not self.base_url:
            raise RuntimeError(
                "TeensingDataConnector 未配置 TESS_DATA_API_BASE_URL，无法拉取真实异常数据"
            )

    # ----- 传输层 -----

    def _headers(self, token: Optional[str] = None) -> dict:
        # 必须带浏览器级 UA：Cloudflare Bot 防护会把 Python-urllib 默认 UA 拦成 403
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; TessDiagnosis/1.0)",
        }
        # 优先用调用方透传的运营 token；缺省再用环境变量兜底 token
        auth = token or self.api_key
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        return headers

    @staticmethod
    def _unwrap(resp: dict) -> dict:
        """Teensing 统一返回 {code,message,data,meta}；拦截器解包 data，这里同样处理。"""
        if isinstance(resp, dict) and "data" in resp and "code" in resp:
            return resp.get("data") if isinstance(resp.get("data"), dict) else resp
        return resp

    def _http_get(
        self, path: str, params: Optional[dict] = None, token: Optional[str] = None
    ) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers=self._headers(token), method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"Teensing 数据 API HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Teensing 数据 API 连接失败：{e.reason}") from e
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Teensing 数据 API 返回非 JSON：{e}") from e

    # ----- DataConnector 接口实现 -----

    def fetch_recent_anomalies(
        self, limit: int = 20, token: Optional[str] = None
    ) -> list[dict]:
        """拉取最近异常实体（涨跌榜 + 异常预警归并）。

        主数据源：GET /overview/ranking/fluctuation（含 revenue/clicks/cvr/profit/margin/change）
        辅助标记：GET /overview/ranking/anomaly-warning（标出被预警的实体）
        二者按实体名(name)归并，构造可供 normalize_to_context 使用的原始事件。
        """
        warn = self._unwrap(
            self._http_get(
                "/overview/ranking/anomaly-warning",
                params={"page_size": limit},
                token=token,
            )
        )
        fluc = self._unwrap(
            self._http_get("/overview/ranking/fluctuation", token=token)
        )

        # 涨跌榜按 campaign_id（回退 campaign_name）建索引，并标记方向
        # 真实结构：data.rising[]/data.falling[]，含 revenue_change（环比变化）
        fluc_map: dict = {}
        if isinstance(fluc, dict):
            for grp in ("rising", "falling"):
                for it in fluc.get(grp) or []:
                    if not isinstance(it, dict):
                        continue
                    cid = it.get("campaign_id") or it.get("campaign_name") or it.get("name")
                    if cid is None:
                        continue
                    merged = dict(it)
                    merged["_direction"] = grp
                    fluc_map[cid] = merged

        # 异常预警项：真实结构为 data.items[]（无显式环比变化），用涨跌榜补齐
        raws: list[dict] = []
        if isinstance(warn, dict):
            for it in warn.get("items") or []:
                if not isinstance(it, dict):
                    continue
                # 低营收 campaign 跳过诊断（降噪）：Rev < 门槛不查
                if _num(it.get("revenue")) < MIN_REVENUE_USD:
                    continue
                cid = it.get("campaign_id") or it.get("campaign_name") or it.get("name")
                merged = dict(it)
                if cid is not None and cid in fluc_map:
                    # fluctuation 的环比变化/方向补齐到预警项上
                    for k, v in fluc_map[cid].items():
                        merged.setdefault(k, v)
                    merged["_direction"] = fluc_map[cid].get("_direction", "unknown")
                else:
                    merged["_direction"] = merged.get("_direction", "unknown")
                raws.append(merged)

        # 若 anomaly-warning 为空，则直接用 fluctuation 列表（含完整环比信号）
        if not raws and isinstance(fluc, dict):
            for grp in ("rising", "falling"):
                for it in fluc.get(grp) or []:
                    if isinstance(it, dict):
                        merged = dict(it)
                        merged["_direction"] = grp
                        raws.append(merged)

        return raws[:limit]

    def fetch_anomaly_event(
        self, event_id: str, token: Optional[str] = None
    ) -> Optional[dict]:
        """按 campaign_id（event_id）在 fluctuation 中查找单条（best-effort）。"""
        fluc = self._unwrap(
            self._http_get("/overview/ranking/fluctuation", token=token)
        )
        if isinstance(fluc, dict):
            target = str(event_id)
            for grp in ("rising", "falling"):
                for it in fluc.get(grp) or []:
                    if not isinstance(it, dict):
                        continue
                    cid = str(it.get("campaign_id") or it.get("campaign_name") or "")
                    if cid == target:
                        merged = dict(it)
                        merged["_direction"] = grp
                        return merged
        return None

    def fetch_realtime_kpi(self, token: Optional[str] = None) -> dict:
        """拉取实时 KPI 小时级曲线（GET /overview/realtime-kpi）。

        真实返回结构（已对齐）：
          {"code":0,"message":"success","data":{"items":[
            {"hour":"00","today_revenue":..,"today_clicks":..,"today_conversions":..,
             "yesterday_revenue":..,"yesterday_clicks":..,"yesterday_conversions":..}, ...]}}
        _unwrap 解包外层提取 data.items；extract_realtime_anomalies() 负责解析与异常判定。
        """
        return self._unwrap(
            self._http_get("/overview/realtime-kpi", token=token)
        )

    def fetch_campaign_time_series(
        self,
        campaign_id: str,
        token: Optional[str] = None,
        days: int = 7,
        granularity: str = "day",  # "day"（按天 7~30 天）或 "hour"（按小时 24~72 小时）
    ) -> dict:
        """拉取指定 Campaign 的连续历史趋势，为 Tess 诊断提供「时间锚点」。

        接口：GET /report（Teensing 原生支持的、按 dimensions 聚合的通用报表接口）。
        若 /report 不可用，可改用同域 GET /campaign-kpi-trend（同样返回按时间聚合的序列）。

        返回（对齐 anomaly_context.history_baseline 形状）：
          {"campaign_id","granularity","range","data_points_count",
           "time_series":[{"timestamp","revenue","profit","cvr_percent","margin_percent",
                           "clicks","conversions"}, ...]}
        拉取/解析失败返回 {"campaign_id","granularity","time_series":[]}（不抛异常，避免拖垮整轮诊断）。
        """
        try:
            now = datetime.now()
            if granularity == "hour":
                hours = int(os.getenv("TESS_TS_HOURS", "24"))
                start_dt = now - timedelta(hours=hours)
                dimensions = ["date", "hour", "campaign"]
            else:
                days = int(os.getenv("TESS_TS_DAYS", str(days)))
                start_dt = now - timedelta(days=days)
                dimensions = ["date", "campaign"]
            start_date = start_dt.strftime("%Y-%m-%d")
            end_date = now.strftime("%Y-%m-%d")

            # 注意：dimensions 以逗号拼接传参（"date,hour,campaign"）；若 Teensing 后端
            # 要求重复键 "dimensions=date&dimensions=hour&dimensions=campaign"，请在此改为对应拼接方式。
            params = {
                "dimensions": ",".join(dimensions),
                "campaign_id": str(campaign_id),
                "start_date": start_date,
                "end_date": end_date,
                "sort_by": "date",
                "sort_order": "asc",
                "page_size": 100,
            }
            resp = self._unwrap(
                self._http_get("/report", params=params, token=token)
            )
            if not isinstance(resp, dict):
                return {"campaign_id": str(campaign_id), "granularity": granularity, "time_series": []}
            raw_items = resp.get("items") or []
            series: list[dict] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                time_key = (
                    f"{item.get('date')} {item.get('hour')}:00"
                    if "hour" in dimensions else item.get("date")
                )
                revenue = _num(item.get("revenue"))
                payout = _num(item.get("payout"))
                profit_raw = item.get("profit")
                profit = _num(profit_raw) if profit_raw not in (None, "") else revenue - payout
                clicks = int(_num(item.get("clicks")))
                conversions = int(_num(item.get("conversions")))
                cvr = round((conversions / clicks * 100), 2) if clicks > 0 else 0.0
                margin = round((profit / revenue * 100), 2) if revenue > 0 else 0.0
                series.append({
                    "timestamp": time_key,
                    "revenue": round(revenue, 2),
                    "profit": round(profit, 2),
                    "cvr_percent": cvr,
                    "margin_percent": margin,
                    "clicks": clicks,
                    "conversions": conversions,
                })
            return {
                "campaign_id": str(campaign_id),
                "granularity": granularity,
                "range": f"{start_date} to {end_date}",
                "data_points_count": len(series),
                "time_series": series,
            }
        except Exception as e:  # 单 campaign 历史拉取失败不应拖垮整轮诊断
            logger.warning("fetch_campaign_time_series 失败 campaign=%s: %s", campaign_id, e)
            return {"campaign_id": str(campaign_id), "granularity": granularity, "time_series": []}


# ----- 归一化：Teensing 原始事件 -> PRD §4.1 Context -----


def normalize_to_context(raw: dict) -> dict:
    """把 Teensing 原始 anomaly 映射为 PRD §4.1 Context（各 connector 共用）。

    兼容两种上游形状：
    (a) 直接就是 Context（含 anomaly_metadata / top_contributors / associated_signals）
        —— Mock 样例、上游已规范化数据走这里。
    (b) Teensing fluctuation/anomaly 形状（含 name / change / revenue / profit / margin）
        —— 真实接入走这里；字段映射见下方 TODO(接入)，可按实际返回结构微调。

    下游编排/诊断逻辑无需改动，归一化在此完成。
    """
    # (a) 已是 Context 形状：原样抽取
    if "anomaly_metadata" in raw or "top_contributors" in raw or "associated_signals" in raw:
        meta_src = raw.get("anomaly_metadata", raw)
        meta = {
            "event_id": raw.get("event_id") or meta_src.get("event_id"),
            "trigger_time": meta_src.get("trigger_time"),
            "target_metric": meta_src.get("target_metric"),
            "current_value": meta_src.get("current_value"),
            "benchmark_value": meta_src.get("benchmark_value"),
            "severity": meta_src.get("severity"),
            "calculated_loss": meta_src.get("calculated_loss"),
        }
        history = raw.get("history_baseline")
        result = {
            "anomaly_metadata": meta,
            "top_contributors": raw.get("top_contributors", []),
            "associated_signals": raw.get("associated_signals", []),
        }
        if history is not None:
            result["history_baseline"] = history
        return result

    # (b) Teensing fluctuation/anomaly-warning 真实形状（已对齐 saas.melo.support 接口）
    #     实体标识：campaign_id（稳定）优先，回退 campaign_name / advertiser_name
    #     环比变化：fluctuation 用 revenue_change；anomaly-warning 无则 None
    entity_id = raw.get("campaign_id") or raw.get("advertiser_id") or raw.get("publisher_id")
    entity_name = (
        raw.get("campaign_name")
        or raw.get("advertiser_name")
        or raw.get("name")
        or raw.get("entity")
        or "UNKNOWN"
    )
    direction = raw.get("_direction", "unknown")
    change = raw.get("revenue_change")
    if change is None and raw.get("change") is not None:
        change = raw.get("change")
    revenue = raw.get("revenue")
    profit = raw.get("profit")
    cvr = raw.get("cvr")
    margin = raw.get("margin")
    clicks = raw.get("clicks")
    conversions = raw.get("conversions")

    # target_metric 与 current_value：优先用「带环比变化」的 Revenue（最直观）；
    # 否则用 Profit（最能反映亏损）；再否则退化到 Revenue / Metric
    if change is not None and revenue is not None:
        target_metric = "Revenue"
        current_value = revenue
    elif profit is not None:
        target_metric = "Profit"
        current_value = profit
    elif revenue is not None:
        target_metric = "Revenue"
        current_value = revenue
    else:
        target_metric = "Metric"
        current_value = None

    benchmark_value = _safe_benchmark(current_value, change)

    # severity：有环比变化按幅度（下跌>=50% 或 <= -10 判 HIGH）；无变化但有负毛利则 MEDIUM
    severity = "LOW"
    if isinstance(change, (int, float)):
        if change < 0 or direction == "falling":
            drop_ratio = (-change / current_value) if current_value else 0.0
            severity = "HIGH" if (drop_ratio >= 0.5 or change <= -10) else "MEDIUM"
        elif change >= 10:
            severity = "HIGH"
        elif change > 0:
            severity = "MEDIUM"
    elif isinstance(margin, (int, float)) and margin < 0:
        severity = "MEDIUM"

    loss = {
        "delta": change,
        "direction": direction,
        "metric": target_metric,
        "current_value": current_value,
        "benchmark_value": benchmark_value,
        "margin": margin,
        "cvr": cvr,
    }
    event_id = str(entity_id) if entity_id not in (None, "") else str(entity_name)
    meta = {
        "event_id": event_id,
        "trigger_time": raw.get("trigger_time"),
        "target_metric": target_metric,
        "current_value": current_value,
        "benchmark_value": benchmark_value,
        "severity": severity,
        "calculated_loss": loss,
    }
    top_contributors = [
        {
            "dimension_type": "Campaign",
            "dimension_value": entity_name,
            "impact_share": "100%",
            "metric_change": (
                f"{target_metric} 环比 {change:+g}" if isinstance(change, (int, float)) else None
            ),
            "advertiser_name": raw.get("advertiser_name"),
            "publisher_name": raw.get("publisher_name"),
            "revenue": revenue,
            "profit": profit,
            "cvr": cvr,
            "margin": margin,
            "clicks": clicks,
            "conversions": conversions,
        }
    ]
    history = raw.get("history_baseline")
    result = {
        "anomaly_metadata": meta,
        "top_contributors": top_contributors,
        "associated_signals": [],
    }
    if history is not None:
        result["history_baseline"] = history
    return result


def _hour_int(v):
    """把 '00'/'0'/0/None 等归一为 int 小时；非法返回 None。"""
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def _num(v):
    """转 float，失败返回 0.0。"""
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _safe_benchmark(current_value, change):
    """由 current 与环比变化推算上一周期基线，杜绝不可能的负收入基线。

    revenue 类指标非负。若按绝对差 (current - change) 推出负基线，说明 Teensing 的
    revenue_change 更可能是**百分比**（如 +107.2 表示上涨 107.2%），则按
    current / (1 + change/100) 反推；仍不合理则置空，绝不向 LLM 投喂荒谬数值。
    """
    if not isinstance(change, (int, float)) or not isinstance(current_value, (int, float)):
        return None
    additive = current_value - change
    # 收入类指标的上一周期基线不应为负；出现负值时改用百分比解释
    if additive < 0 and change > 0 and current_value > 0:
        denom = 1.0 + change / 100.0
        if denom > 0:
            pct = current_value / denom
            if pct >= 0:
                return round(pct, 4)
        return None
    if additive < 0:
        return None
    return round(additive, 4)


def _parse_realtime_items(raw):
    """从 realtime-kpi 返回中抽出 items 列表（兼容已 unwrap / 未 unwrap 两种形状）。

    真实结构：{"code":0,"data":{"items":[ {hour,today_revenue,...}, ... ]}}
    TeensingDataConnector._unwrap 已解包外层，故也可能直接是 {"items":[...]}。
    """
    if not isinstance(raw, dict):
        return []
    if isinstance(raw.get("items"), list):
        return raw["items"]
    data = raw.get("data")
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    return []


def _severity_for_drop(drop: float) -> str:
    """按同比跌幅分档严重度（用户规则）：
    - 跌幅 <= 30%          -> LOW
    - 30% < 跌幅 < 50%     -> MEDIUM
    - 跌幅 >= 50%          -> HIGH
    drop = (yesterday - today) / yesterday，取值范围 [0, 1]（1 即掉零/100% 跌）。
    """
    if drop >= 0.50:
        return "HIGH"
    if drop > 0.30:
        return "MEDIUM"
    return "LOW"


def _compute_cvr(conv: float, clk: float):
    """转化率 = conversions / clicks；缺点击量返回 None。"""
    if clk and clk > 0:
        return conv / clk
    return None


def _drop_ratio(today, yest):
    """同比跌幅 (yest-today)/yest，范围 [0,1]；掉零=1.0；无法计算返回 None。"""
    if yest is None or today is None:
        return None
    if yest > 0 and today > 0:
        return (yest - today) / yest
    if yest > 0 and today <= 0:
        return 1.0
    return None


# 参与同比下降检测的指标体系（名称 / 今日键 / 昨日键，均为 Teensing 真实字段）
_METRIC_DEFS = (
    ("Revenue", "today_revenue", "yesterday_revenue"),
    ("Clicks", "today_clicks", "yesterday_clicks"),
    ("Conversions", "today_conversions", "yesterday_conversions"),
)


def _metric_breakdown(it: dict) -> list:
    """计算该小时全部指标（含派生 CVR）的今日/昨日/同比跌幅，供 LLM 归因拆解。

    返回 [{metric, today, yesterday, drop_ratio}, ...]，drop_ratio=None 表示无法计算
    （如昨日为 0）。Revenue/Clicks/Conversions 来自接口原字段，CVR 为 conversions/clicks 派生。
    """
    cache = {}
    rows = []
    for name, tk, yk in _METRIC_DEFS:
        t = _num(it.get(tk))
        y = _num(it.get(yk))
        cache[name] = (t, y)
        d = _drop_ratio(t, y)
        rows.append({
            "metric": name,
            "today": round(t, 4) if t else 0.0,
            "yesterday": round(y, 4) if y else 0.0,
            "drop_ratio": round(d, 4) if d is not None else None,
        })
    cvr_t = _compute_cvr(cache["Conversions"][0], cache["Clicks"][0])
    cvr_y = _compute_cvr(cache["Conversions"][1], cache["Clicks"][1])
    cvr_d = _drop_ratio(cvr_t, cvr_y)
    rows.append({
        "metric": "CVR",
        "today": round(cvr_t, 6) if cvr_t is not None else None,
        "yesterday": round(cvr_y, 6) if cvr_y is not None else None,
        "drop_ratio": round(cvr_d, 4) if cvr_d is not None else None,
    })
    return rows


def _gap_context(start, end, yest_total, breakdown=None):
    """连续掉零聚合为一条「数据中断」告警（PRD §4.1 形状）。"""
    span = f"{start:02d}-{end:02d}" if start != end else f"{start:02d}"
    ctx = {
        "anomaly_metadata": {
            "event_id": f"REALTIME-GAP-{start:02d}-{end:02d}",
            "trigger_time": f"hour {span}",
            "target_metric": "Revenue",
            "current_value": 0.0,
            "benchmark_value": round(yest_total, 2),
            "severity": "HIGH",
            "calculated_loss": {"loss_per_hour_usd": round(yest_total, 2)},
        },
        "top_contributors": [
            {
                "dimension_type": "Hour",
                "dimension_value": span,
                "impact_share": "100%",
                "metric_change": f"今日收益跌至 0，昨日同期合计 {yest_total:,.2f}",
            }
        ],
        "associated_signals": [
            {
                "source": "realtime-kpi",
                "type": "data_gap",
                "hours": span,
                "yesterday_revenue_total": round(yest_total, 2),
            }
        ],
    }
    if breakdown is not None:
        ctx["metric_breakdown"] = breakdown
    return ctx


def _drop_context(it, h, rev_today, rev_yest, primary_drop, breakdown):
    """单小时同比暴跌告警（PRD §4.1 形状）。

    primary_drop 取该小时「跌幅最大的异常指标」；alert 以该指标为头条
    （target_metric/current_value/benchmark_value/severity 均描述它），但
    calculated_loss 永远按 Revenue 实损计算。完整多维拆解见 metric_breakdown。
    """
    primary = max(
        (r for r in breakdown if r["drop_ratio"] is not None),
        key=lambda r: r["drop_ratio"],
    )
    pname = primary["metric"]
    ptoday = primary["today"]
    pyest = primary["yesterday"]
    return {
        "anomaly_metadata": {
            "event_id": f"REALTIME-DROP-{h:02d}",
            "trigger_time": f"hour {h:02d}",
            "target_metric": pname,
            "current_value": ptoday,
            "benchmark_value": pyest,
            "severity": _severity_for_drop(primary_drop),
            "calculated_loss": {"loss_per_hour_usd": round(rev_yest - rev_today, 2)},
        },
        "metric_breakdown": breakdown,
        "top_contributors": [
            {
                "dimension_type": "Metric",
                "dimension_value": pname,
                "impact_share": "primary",
                "metric_change": f"{pname} {ptoday:,.4f} 较昨日 {pyest:,.4f} 下跌 {primary_drop*100:.1f}%",
            }
        ],
        "associated_signals": [
            {
                "source": "realtime-kpi",
                "type": "metric_drop",
                "hour": h,
                "revenue_today": round(rev_today, 2),
                "revenue_yesterday": round(rev_yest, 2),
                "revenue_drop_ratio": round((rev_yest - rev_today) / rev_yest, 4) if rev_yest > 0 else None,
                "breakdown": breakdown,
            }
        ],
    }


def extract_realtime_anomalies(
    raw: dict,
    drop_threshold: Optional[float] = None,
    as_of_hour: Optional[int] = None,
    grace_hours: Optional[int] = None,
) -> list[dict]:
    """把 realtime-kpi 真实返回解析为异常 Context 列表（PRD §4.1 形状）。

    真实返回结构（已对齐）：
      {"code":0,"message":"success","data":{"items":[
        {"hour":"00","today_revenue":1387.795,"today_clicks":2645837,
         "today_conversions":5588,"yesterday_revenue":1202.416,
         "yesterday_clicks":3489430,"yesterday_conversions":5028}, ...]}}

    锚定策略（关键，避免误报）：
    - as_of_hour（数据「更新到的小时」）默认**从数据自身推断** = 最后一个 today_revenue>0 的小时。
      这样能天然区分「已完整过去的时段」与「接口每小时滚动更新、快照尚未覆盖的未来时段」，
      避免把尾部全 0（数据未就绪）误判为掉零。
    - grace_hours（延迟容忍窗口，默认 1，可配 TESS_REALTIME_GRACE_HOURS）：
      当前小时及延迟窗口内 today_revenue=0 视为「数据尚未就绪（接口滞后 15-30min）」，不报掉零。
      仅对 h <= as_of_hour - grace_hours 的「已完整过去」小时做判定。

    异常规则（仅对已完整过去的小时生效）：
    1) 数据掉零：yesterday_revenue > 0 且 today_revenue <= 0 → 100% 跌幅，判 HIGH，
       连续掉零聚合为一条「数据中断」告警。
    2) 多指标同比下跌（补全数据源）：对 Revenue / Clicks / Conversions 三项接口原字段
       及其派生 CVR（conversions/clicks）分别计算同比跌幅，任一指标 drop > drop_threshold
       即判异常，头条（target_metric / 严重度 / current / benchmark）取跌幅最大者，
       完整多维拆解置于 metric_breakdown 供 LLM 归因（如 Revenue 跌但 Clicks 持平 →
       指向转化率/CVR 问题，而非流量问题）。drop = (yesterday-today)/yesterday，分档：
       - drop <= 30%        -> LOW
       - 30% < drop < 50%  -> MEDIUM
       - drop >= 50%        -> HIGH
       drop_threshold（可配 TESS_REALTIME_DROP_THRESHOLD，默认 0.0）作为「最低跌幅门槛」：
       仅当 drop > drop_threshold 才上报，默认 0.0 表示任何下跌都报；调高可忽略微跌噪声。

    若所有 today_revenue 均为 0（无法锚定 as_of_hour），返回空——不误报。
    """
    if drop_threshold is None:
        try:
            drop_threshold = float(os.getenv("TESS_REALTIME_DROP_THRESHOLD", "0.0"))
        except (ValueError, TypeError):
            drop_threshold = 0.0
    if grace_hours is None:
        try:
            grace_hours = int(os.getenv("TESS_REALTIME_GRACE_HOURS", "1"))
        except (ValueError, TypeError):
            grace_hours = 1

    items = _parse_realtime_items(raw)
    if not items:
        logger.warning("realtime-kpi 未解析到 items，跳过异常检测")
        return []

    # 从数据推断「更新到的小时」；调用方可显式覆盖（如测试固定窗口）
    if as_of_hour is None:
        hrs = [
            _hour_int(it.get("hour"))
            for it in items
            if _num(it.get("today_revenue")) > 0
        ]
        as_of_hour = max(hrs) if hrs else None
    if as_of_hour is None:
        logger.warning("realtime-kpi 所有 today_revenue 均为 0，无法锚定更新小时，跳过检测")
        return []

    cutoff = as_of_hour - grace_hours  # 仅 h <= cutoff 的小时参与掉零/暴跌判定
    contexts: list[dict] = []
    gap_start = None
    gap_prev = None
    gap_yest_total = 0.0
    gap_last_item = None

    for it in sorted(items, key=lambda x: _hour_int(x.get("hour")) or 0):
        h = _hour_int(it.get("hour"))
        if h is None or h > cutoff:
            # 当前小时及延迟窗口内 / 未来小时：数据尚未就绪，不判掉零
            if gap_start is not None:
                contexts.append(_gap_context(gap_start, gap_prev, gap_yest_total, _metric_breakdown(gap_last_item)))
                gap_start, gap_prev, gap_yest_total, gap_last_item = None, None, 0.0, None
            continue

        rev_t = _num(it.get("today_revenue"))
        rev_y = _num(it.get("yesterday_revenue"))

        if rev_y > 0 and rev_t <= 0:
            # 数据掉零（已完整过去的小时仍无数据）
            if gap_start is None:
                gap_start = h
            gap_prev = h
            gap_yest_total += rev_y
            gap_last_item = it
        else:
            if gap_start is not None:
                contexts.append(_gap_context(gap_start, gap_prev, gap_yest_total, _metric_breakdown(gap_last_item)))
                gap_start, gap_prev, gap_yest_total, gap_last_item = None, None, 0.0, None
            if rev_y > 0 and rev_t > 0:
                # 多指标同比：Revenue/Clicks/Conversions/CVR 任一跌幅超阈值即报警，
                # 头条取跌幅最大者；完整拆解见 metric_breakdown 供 LLM 归因。
                breakdown = _metric_breakdown(it)
                anomalous = [
                    r for r in breakdown
                    if r["drop_ratio"] is not None and r["drop_ratio"] > drop_threshold
                ]
                if anomalous:
                    worst = max(anomalous, key=lambda r: r["drop_ratio"])
                    contexts.append(_drop_context(it, h, rev_t, rev_y, worst["drop_ratio"], breakdown))

    if gap_start is not None:
        contexts.append(_gap_context(gap_start, gap_prev, gap_yest_total, _metric_breakdown(gap_last_item)))

    return contexts


def get_data_connector() -> DataConnector:
    """按环境变量 TESS_DATA_CONNECTOR 选择具体实现（默认 mock，零配置可跑）。

    - 未设置 / "mock" -> MockDataConnector（开发、测试、离线演示）
    - "teensing"      -> TeensingDataConnector（生产，需 TESS_DATA_API_BASE_URL）
    """
    kind = os.getenv("TESS_DATA_CONNECTOR", "mock").lower()
    if kind == "teensing":
        return TeensingDataConnector()
    if kind == "mock":
        return MockDataConnector()
    raise ValueError(f"未知 TESS_DATA_CONNECTOR={kind!r}（支持 mock | teensing）")
