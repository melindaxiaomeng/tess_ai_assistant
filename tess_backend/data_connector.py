"""P5 · 数据接入层 —— 从 Teensing 真实异常数据源拉取（轮询）异常事件，
归一化为 PRD §4.1 标准 Context，交给编排层 run_diagnosis 诊断。

设计要点：
- DataConnector 为抽象接口（Protocol），便于测试时换 Mock、生产时换 Teensing。
- TeensingDataConnector 读环境变量，调用真实异常数据 API（HTTP 轮询）。
- normalize_to_context() 把上游原始 anomaly 映射成 Tess 消费的 PRD §4.1 Context；
  真实字段映射见函数内 TODO(接入)，由接入方按 Teensing 实际返回结构填写。
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


class DataConnector(Protocol):
    """数据接入抽象：拉取真实异常事件，归一化为 Tess Context。"""

    def fetch_recent_anomalies(self, limit: int = 20) -> list[dict]:
        """拉取最近 limit 个原始异常事件（Teensing 原始结构）。"""
        ...

    def fetch_anomaly_event(self, event_id: str) -> Optional[dict]:
        """按 event_id 拉取单个原始异常事件；不存在返回 None。"""
        ...


class MockDataConnector:
    """开发/测试用：返回内置样例原始事件，无需任何外部依赖。"""

    def __init__(self, samples: Optional[list[dict]] = None):
        self.samples: list[dict] = samples if samples is not None else [_SAMPLE_RAW]

    def fetch_recent_anomalies(self, limit: int = 20) -> list[dict]:
        return self.samples[:limit]

    def fetch_anomaly_event(self, event_id: str) -> Optional[dict]:
        for s in self.samples:
            if s.get("event_id") == event_id:
                return s
        return None


class TeensingDataConnector:
    """生产用：轮询 Teensing 真实异常数据 API。

    必需环境变量：TESS_DATA_API_BASE_URL（异常数据 API 根地址）
    可选：TESS_DATA_API_KEY（Bearer Token，留空则不带鉴权）
          TESS_DATA_API_TIMEOUT（请求超时秒，默认 10）
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

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _http_get(self, path: str, params: Optional[dict] = None) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
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

    def fetch_recent_anomalies(self, limit: int = 20) -> list[dict]:
        # TODO(接入): 确认 Teensing 异常列表端点路径与分页参数名（如 page/limit/pageSize）。
        # 下方默认调用 GET /anomalies?limit=N；若返回结构为 {items:[...]} 则取 items。
        resp = self._http_get("/anomalies", {"limit": limit})
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            for key in ("items", "data", "anomalies", "results"):
                if isinstance(resp.get(key), list):
                    return resp[key]
        logger.warning("Teensing 异常列表返回结构未识别，已按空列表处理：%r", resp)
        return []

    def fetch_anomaly_event(self, event_id: str) -> Optional[dict]:
        # TODO(接入): 确认单事件端点路径（如 /anomalies/{id} 或 /events/{id}）。
        resp = self._http_get(f"/anomalies/{event_id}")
        return resp if isinstance(resp, dict) else None


# ----- 归一化：Teensing 原始事件 -> PRD §4.1 Context -----


def normalize_to_context(raw: dict) -> dict:
    """把 Teensing 原始 anomaly 映射为 PRD §4.1 Context（各 connector 共用）。

    TODO(接入): 按 Teensing 实际返回字段调整下方映射。
    当前默认假设 raw 已接近 Context 形状；若 Teensing 字段名不同，
    在此处做字段对齐即可，无需改动下游编排/诊断逻辑。
    """
    # 兼容「原始事件直接就是 Context」与「嵌套在 meta 里」两种情况
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
