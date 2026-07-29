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
from typing import Optional, Protocol

logger = logging.getLogger("tess_backend.data_connector")

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

# 开发/测试用样例：实时 KPI 小时级曲线（最后一点 revenue/clicks/profit 骤降，可触发异常）
_SAMPLE_REALTIME_KPI = {
    "success": True,
    "data": {
        "series": [
            {"time": "2026-07-29 09:00", "revenue": 1200, "clicks": 8000, "profit": 300},
            {"time": "2026-07-29 10:00", "revenue": 1180, "clicks": 7900, "profit": 290},
            {"time": "2026-07-29 11:00", "revenue": 1210, "clicks": 8100, "profit": 305},
            {"time": "2026-07-29 12:00", "revenue": 1190, "clicks": 8050, "profit": 298},
            {"time": "2026-07-29 13:00", "revenue": 200, "clicks": 1500, "profit": 40},
        ]
    },
}


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
        headers = {"Accept": "application/json"}
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
            self._http_get("/overview/ranking/anomaly-warning", token=token)
        )
        fluc = self._unwrap(
            self._http_get("/overview/ranking/fluctuation", token=token)
        )
        # 量化指标按实体名归并（fluctuation 含数值）
        fluc_map: dict = {}
        if isinstance(fluc, dict):
            for grp in ("rising", "falling"):
                for it in fluc.get(grp) or []:
                    if isinstance(it, dict) and it.get("name"):
                        fluc_map[it["name"]] = it
        raws: list[dict] = []
        if isinstance(warn, dict):
            for grp in ("rising", "falling"):
                for it in warn.get(grp) or []:
                    if not isinstance(it, dict):
                        continue
                    name = it.get("name") or it.get("entity") or it.get("entity_name")
                    merged = dict(it)
                    if name and name in fluc_map:
                        # fluctuation 的量化字段补齐到预警项上
                        for k, v in fluc_map[name].items():
                            merged.setdefault(k, v)
                    merged["_direction"] = grp
                    raws.append(merged)
        # 若 anomaly-warning 为空，则直接用 fluctuation 列表
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
        """按实体名（event_id）在 fluctuation 中查找单条（best-effort）。"""
        fluc = self._unwrap(
            self._http_get("/overview/ranking/fluctuation", token=token)
        )
        if isinstance(fluc, dict):
            for grp in ("rising", "falling"):
                for it in fluc.get(grp) or []:
                    if isinstance(it, dict) and it.get("name") == event_id:
                        merged = dict(it)
                        merged["_direction"] = grp
                        return merged
        return None

    def fetch_realtime_kpi(self, token: Optional[str] = None) -> dict:
        """拉取实时 KPI 小时级曲线（GET /overview/realtime-kpi）。

        返回结构文档未给字段细节，extract_realtime_anomalies() 做了鲁棒解析，
        TODO(接入): 拿到真实返回 JSON 后，如有字段差异在该函数/解析处对齐。
        """
        return self._unwrap(
            self._http_get("/overview/realtime-kpi", token=token)
        )


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
        return {
            "anomaly_metadata": meta,
            "top_contributors": raw.get("top_contributors", []),
            "associated_signals": raw.get("associated_signals", []),
        }

    # (b) Teensing fluctuation/anomaly 形状
    name = (
        raw.get("name")
        or raw.get("entity")
        or raw.get("entity_name")
        or "UNKNOWN"
    )
    direction = raw.get("_direction", "falling")
    change = raw.get("change")  # 主指标日环比变化（数值，可为 % 或绝对值）
    profit = raw.get("profit")
    revenue = raw.get("revenue")
    margin = raw.get("margin")

    # TODO(接入): 按 Teensing 实际字段语义校准。以下为保守默认：
    #   target_metric 取 profit（最具业务意义的亏损指标），缺则 revenue
    #   current_value 取当前 profit/revenue；benchmark 以 change 反推基线
    target_metric = "Profit" if profit is not None else ("Revenue" if revenue is not None else "Metric")
    current_value = profit if profit is not None else revenue

    benchmark_value = None
    if isinstance(change, (int, float)) and isinstance(current_value, (int, float)):
        benchmark_value = current_value - change

    # severity 由变化幅度推导（可经 TESS 阈值配置进一步细化）
    severity = "LOW"
    if isinstance(change, (int, float)):
        if change <= -10:
            severity = "HIGH"
        elif change < 0:
            severity = "MEDIUM"
        elif change >= 10:
            severity = "HIGH"
        elif change > 0:
            severity = "MEDIUM"

    loss = {
        "delta": change,
        "direction": direction,
        "metric": target_metric,
        "current_value": current_value,
        "benchmark_value": benchmark_value,
        "margin": margin,
    }
    meta = {
        "event_id": name,
        "trigger_time": raw.get("trigger_time"),
        "target_metric": target_metric,
        "current_value": current_value,
        "benchmark_value": benchmark_value,
        "severity": severity,
        "calculated_loss": loss,
    }
    top_contributors = [
        {
            "dimension_type": "Entity",
            "dimension_value": name,
            "impact_share": "100%",
            "metric_change": (
                f"{target_metric} {change:+g}" if isinstance(change, (int, float)) else None
            ),
        }
    ]
    return {
        "anomaly_metadata": meta,
        "top_contributors": top_contributors,
        "associated_signals": [],
    }


def extract_realtime_anomalies(
    raw: dict, threshold: Optional[float] = None, min_points: int = 3
) -> list[dict]:
    """把实时 KPI 小时级曲线解析为异常 Context 列表（PRD §4.1 形状）。

    判定逻辑：对曲线中的每个指标，取最新一小时值 vs 前 N 小时基线均值，
    当跌幅 (baseline - current)/baseline >= 阈值（默认 30%）时，判定该指标当前点异常，
    生成一个 Context 交由 Tess 诊断。

    TODO(接入): 文档未给 /overview/realtime-kpi 的精确返回结构。当前假设：
      { "data": { "series": [ {"time": "...", "<metric>": <num>, ...}, ... ] } }
    若真实返回为「按 metric 分组的 points 列表」等其它形状，在此处做字段对齐即可，
    下游诊断逻辑无需改动。
    """
    if threshold is None:
        try:
            threshold = float(os.getenv("TESS_REALTIME_DROP_THRESHOLD", "0.3"))
        except (ValueError, TypeError):
            threshold = 0.3

    if not isinstance(raw, dict):
        return []
    data = raw.get("data", raw)
    series = data.get("series") if isinstance(data, dict) else None
    if not isinstance(series, list) or len(series) < min_points:
        logger.warning("realtime-kpi 曲线点数不足(%s)，跳过异常检测", len(series) if isinstance(series, list) else "非列表")
        return []

    latest = series[-1]
    if not isinstance(latest, dict):
        return []
    metric_keys = [
        k for k in latest.keys()
        if k != "time" and isinstance(latest.get(k), (int, float))
    ]

    contexts: list[dict] = []
    for m in metric_keys:
        values = [
            p.get(m)
            for p in series
            if isinstance(p, dict) and isinstance(p.get(m), (int, float))
        ]
        if len(values) < min_points:
            continue
        current = values[-1]
        baseline = sum(values[:-1]) / len(values[:-1])
        if not baseline or current is None:
            continue
        drop = (baseline - current) / baseline
        if drop < threshold:
            continue
        ctx = {
            "anomaly_metadata": {
                "event_id": f"REALTIME-{m}-{latest.get('time', 'latest')}",
                "trigger_time": latest.get("time"),
                "target_metric": m,
                "current_value": current,
                "benchmark_value": round(baseline, 4),
                "calculated_loss": round(abs(current - baseline), 4),
                "severity": "HIGH" if drop >= 0.5 else "MEDIUM",
            },
            "top_contributors": [],
            "associated_signals": [
                {
                    "source": "realtime-kpi",
                    "metric": m,
                    "baseline": round(baseline, 4),
                    "current": current,
                    "drop_ratio": round(drop, 4),
                }
            ],
        }
        contexts.append(ctx)
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
