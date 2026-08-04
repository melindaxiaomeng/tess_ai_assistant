"""P7 · 预警存储 —— 定时诊断产出的预警落库，供 Teensing 拉取。

存储底座：SQLAlchemy（见 db.py）。后端由构造参数 / TESS_DATABASE_URL 决定：
- 开发 / 单测：AlertStore("path/to/x.db") 或默认 sqlite（零依赖）；
- 生产：AlertStore() 且环境变量 TESS_DATABASE_URL 指向 PostgreSQL。

设计要点：
- 定时调度器每小时拉异常→诊断→把结果批量写入此库；
- Teensing / SaaS 后端通过 GET /tess/alerts 轮询拉取最新预警，无需前端点击触发；
- 运营在 Teensing 侧确认/处理后，调 POST /tess/alerts/{id}/ack 回写状态，
  Tess 落库后默认拉取（include_acked=false）不再返回该告警，避免已处理项刷屏；
- 增量游标（since_as_of）：Teensing 带上次拿到的 as_of，只返回更新的批次，省流量；
- 公开方法签名与旧 sqlite3 实现保持一致，便于平滑迁移与单测复用。
"""

from __future__ import annotations

import os
import time
from typing import Optional

from sqlalchemy import Float, Integer, JSON, String, Text, select
from sqlalchemy.orm import mapped_column

from .db import Base, make_engine, make_session_factory, init_all

DEFAULT_PATH = os.getenv("TESS_ALERTS_DB", "tess_alerts.db")

# 运营确认/处理状态枚举：
# - acknowledged  : 已查看/知晓（运营已读该告警）
# - resolved      : 已解决（运营已处理线上问题）
# - false_positive: 误报 / 正常流量波动（运营确认无异常）
ACK_RESOLUTIONS = ("acknowledged", "resolved", "false_positive")


class Alert(Base):
    """预警表（与旧 alerts 表 schema 对齐，diagnosis / anomaly_metadata 用 JSON 落地）。"""

    __tablename__ = "alerts"

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_time = mapped_column(String, nullable=False, index=True)   # 批次时间（同批次相同）
    event_id = mapped_column(String, index=True)                  # 异常实体标识
    status = mapped_column(String)                                # DIAGNOSED / INCONCLUSIVE ...
    confidence = mapped_column(Float)                             # 诊断置信度
    source = mapped_column(String, index=True)                    # anomaly-warning | realtime-kpi
    diagnosis = mapped_column(JSON)                               # Gatekeeper 归一化诊断
    anomaly_metadata = mapped_column(JSON)                        # 原始异常数值（current/benchmark/severity）
    acked_at = mapped_column(String)                             # 运营确认时间
    resolution = mapped_column(String)                           # acknowledged/resolved/false_positive
    acked_by = mapped_column(String)                            # 处理人
    ack_note = mapped_column(Text)                              # 备注


def _resolve_url(db_url: Optional[str]) -> str:
    """把构造参数归一为 SQLAlchemy URL。

    - None        -> 优先 TESS_DATABASE_URL，其次旧 TESS_ALERTS_DB，再回退默认 sqlite 文件；
    - 含 "://"    -> 视为完整 URL（sqlite:///... 或 postgresql+psycopg://...）原样使用；
    - 其它        -> 当作文件系统路径（兼容旧单测 AlertStore("x.db")），转 sqlite URL。
    """
    if db_url is None:
        env = os.getenv("TESS_DATABASE_URL")
        if env:
            return env
        legacy = os.getenv("TESS_ALERTS_DB")
        if legacy:
            return "sqlite:///" + os.path.abspath(legacy)
        return "sqlite:///" + os.path.abspath(DEFAULT_PATH)
    if "://" in db_url:
        return db_url
    return "sqlite:///" + os.path.abspath(db_url)


class AlertStore:
    """预警库：保存每小时诊断批次，支持按时间检索、运营确认回写、增量游标。"""

    def __init__(self, db_url: Optional[str] = None):
        self.url = _resolve_url(db_url)
        self.engine = make_engine(self.url)
        self.Session = make_session_factory(self.engine)
        init_all(self.engine)  # 幂等建表（Postgres 新建；sqlite 已存在则跳过）

    @staticmethod
    def _normalize_result(r: dict) -> dict:
        """兼容 {event_id, diagnosis, meta, anomaly_metadata} 与旧形状。"""
        if not isinstance(r, dict):
            return {"event_id": None, "diagnosis": {}, "source": None, "anomaly_metadata": None}
        diag = r.get("diagnosis") or {}
        event_id = r.get("event_id")
        if not event_id and isinstance(diag, dict):
            event_id = (diag.get("anomaly_metadata") or {}).get("event_id")
        meta = r.get("meta") or {}
        source = meta.get("source")
        anomaly_metadata = r.get("anomaly_metadata")
        if not anomaly_metadata and isinstance(diag, dict):
            # 兜底：诊断对象里若仍带了 anomaly_metadata 也一并保留
            anomaly_metadata = diag.get("anomaly_metadata")
        return {
            "event_id": event_id,
            "diagnosis": diag,
            "source": source,
            "anomaly_metadata": anomaly_metadata,
        }

    @staticmethod
    def _row_to_dict(a: Alert) -> dict:
        return {
            "id": a.id,
            "run_time": a.run_time,
            "event_id": a.event_id,
            "status": a.status,
            "confidence": a.confidence,
            "source": a.source,
            "diagnosis": a.diagnosis,
            "anomaly_metadata": a.anomaly_metadata,
            # 运营确认/处理状态
            "acked_at": a.acked_at,
            "resolution": a.resolution,
            "acked_by": a.acked_by,
            "ack_note": a.ack_note,
        }

    def save_batch(self, results: list, run_time: Optional[str] = None) -> int:
        """把一轮诊断的结果列表批量写入预警库。返回写入条数。"""
        run_time = run_time or time.strftime("%Y-%m-%d %H:%M:%S")
        objs = []
        for r in results or []:
            n = self._normalize_result(r)
            diag = n["diagnosis"]
            objs.append(
                Alert(
                    run_time=run_time,
                    event_id=n["event_id"],
                    status=diag.get("status") if isinstance(diag, dict) else None,
                    confidence=diag.get("confidence") if isinstance(diag, dict) else None,
                    source=n["source"],
                    diagnosis=diag,
                    anomaly_metadata=n["anomaly_metadata"],
                )
            )
        if not objs:
            return 0
        with self.Session() as s:
            s.add_all(objs)
            s.commit()
        return len(objs)

    def recent(self, limit: int = 50, source: Optional[str] = None, include_acked: bool = True) -> list:
        """按时间倒序返回最近 limit 条预警；source 非空时按来源过滤。

        include_acked=False 时仅返回「未确认」项（默认 True=含已确认，向后兼容）。
        """
        with self.Session() as s:
            q = select(Alert)
            if source:
                q = q.where(Alert.source == source)
            if not include_acked:
                q = q.where(Alert.acked_at.is_(None))
            q = q.order_by(Alert.id.desc()).limit(limit)
            rows = s.execute(q).scalars().all()
        return [self._row_to_dict(a) for a in rows]

    def latest_run(self) -> Optional[str]:
        """返回最近一次批次的 run_time；无数据返回 None。"""
        with self.Session() as s:
            return s.execute(
                select(Alert.run_time).distinct().order_by(Alert.run_time.desc()).limit(1)
            ).scalar_one_or_none()

    def latest_batch(self, source: Optional[str] = None, limit: int = 50, include_acked: bool = True) -> dict:
        """返回最近一次诊断批次（run_time）的结果；可选按来源过滤。

        供 Teensing 轮询拉取：拿到的就是「上一轮整批结果」，不会跨批次错乱。
        无数据时 run_time=None、items=[]。
        """
        run_time = self.latest_run()
        if not run_time:
            return {"run_time": None, "count": 0, "alerts": []}
        with self.Session() as s:
            q = select(Alert).where(Alert.run_time == run_time)
            if source:
                q = q.where(Alert.source == source)
            if not include_acked:
                q = q.where(Alert.acked_at.is_(None))
            q = q.order_by(Alert.id.desc()).limit(limit)
            out = [self._row_to_dict(a) for a in s.execute(q).scalars().all()]
        return {"run_time": run_time, "count": len(out), "alerts": out}

    def query_since(self, since_run_time: str, source: Optional[str] = None,
                    limit: int = 200, include_acked: bool = True) -> list:
        """增量游标：返回 run_time 严格大于 since_run_time 的所有告警（可能跨多批）。

        Teensing 传上次拉取拿到的 as_of，即只拿到「更新批次」中的新增告警，
        不用每次重传整批历史，也无需客户端再比对去重。
        """
        with self.Session() as s:
            q = select(Alert).where(Alert.run_time > since_run_time)
            if source:
                q = q.where(Alert.source == source)
            if not include_acked:
                q = q.where(Alert.acked_at.is_(None))
            q = q.order_by(Alert.run_time.asc(), Alert.id.asc()).limit(limit)
            rows = s.execute(q).scalars().all()
        return [self._row_to_dict(a) for a in rows]

    def get_by_event_ids(self, event_ids: list, source: Optional[str] = None,
                         limit: int = 50, include_acked: bool = True) -> list:
        """按 event_id 集合捞取记录，每 event_id 取最新一条（id 最大者）。

        用途：演示/置顶记录可能落在较旧的批次，被 cron 新批次覆盖后
        latest_batch 捞不到；用 event_id 直接定位可让其始终可见，不受批次覆盖影响。
        """
        if not event_ids:
            return []
        with self.Session() as s:
            q = select(Alert).where(Alert.event_id.in_(event_ids))
            if source:
                q = q.where(Alert.source == source)
            if not include_acked:
                q = q.where(Alert.acked_at.is_(None))
            q = q.order_by(Alert.id.desc()).limit(limit * 5)
            rows = s.execute(q).scalars().all()
        seen = {}
        for a in rows:
            d = self._row_to_dict(a)
            if a.event_id not in seen:
                seen[a.event_id] = d
        return list(seen.values())[:limit]

    def ack(self, alert_id: int, resolution: str, acked_by: Optional[str] = None,
            note: Optional[str] = None) -> bool:
        """标记某条告警已被运营确认/处理。成功返回 True，id 不存在返回 False。

        resolution 必须是 ACK_RESOLUTIONS 之一（acknowledged/resolved/false_positive）。
        """
        if resolution not in ACK_RESOLUTIONS:
            raise ValueError(f"resolution 必须是 {ACK_RESOLUTIONS}")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.Session() as s:
            obj = s.get(Alert, alert_id)
            if obj is None:
                return False
            obj.acked_at = now
            obj.resolution = resolution
            obj.acked_by = acked_by
            obj.ack_note = note
            s.commit()
        return True
