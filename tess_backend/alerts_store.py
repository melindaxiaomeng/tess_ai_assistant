"""P7 · 预警存储 —— 定时诊断产出的预警落库（本地 SQLite），供 Teensing 拉取。

设计要点：
- 定时调度器（scheduler）每小时拉异常→诊断→把结果批量写入此库；
- Teensing / SaaS 后端通过 GET /tess/alerts 轮询拉取最新预警，无需前端点击触发；
- 共享服务 token 拉全量、不按人过滤（按用户选择），故预警为全局可读列表；
- 运营在 Teensing 侧确认/处理后，调 POST /tess/alerts/{id}/ack 回写状态，
  Tess 落库后默认拉取（include_acked=false）不再返回该告警，避免已处理项刷屏；
- 增量游标（since_as_of）：Teensing 带上次拿到的 as_of，只返回更新的批次，省流量；
- 默认用标准库 sqlite3，零额外依赖；路径可由 TESS_ALERTS_DB 覆盖。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional

DEFAULT_PATH = os.getenv("TESS_ALERTS_DB", "tess_alerts.db")

# 运营确认/处理状态枚举：
# - acknowledged  : 已查看/知晓（运营已读该告警）
# - resolved      : 已解决（运营已处理线上问题）
# - false_positive: 误报 / 正常流量波动（运营确认无异常）
ACK_RESOLUTIONS = ("acknowledged", "resolved", "false_positive")

_SELECT_COLS = (
    "id, run_time, event_id, status, confidence, source, diagnosis, "
    "anomaly_metadata, acked_at, resolution, acked_by, ack_note"
)


class AlertStore:
    """预警库：保存每小时诊断批次，支持按时间检索、运营确认回写、增量游标。"""

    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_time   TEXT NOT NULL,                 -- 批次时间（同批次相同）
                    event_id   TEXT,                          -- 异常实体标识
                    status     TEXT,                          -- DIAGNOSED / INCONCLUSIVE ...
                    confidence REAL,                          -- 诊断置信度
                    source     TEXT,                          -- 数据来源: anomaly-warning | realtime-kpi
                    diagnosis  TEXT                           -- Gatekeeper 归一化诊断（JSON）
                )
                """
            )
            # 存量库迁移：补充 anomaly_metadata 列（原始异常数值，供 Teensing 展示）
            try:
                c.execute("ALTER TABLE alerts ADD COLUMN anomaly_metadata TEXT")
            except sqlite3.OperationalError:
                pass  # 列已存在
            # 运营确认/处理状态（Teensing 回写，用于去重与"已处理"标记）
            for col in ("acked_at TEXT", "resolution TEXT", "acked_by TEXT", "ack_note TEXT"):
                try:
                    c.execute(f"ALTER TABLE alerts ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # 列已存在

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
    def _row_to_dict(row) -> dict:
        return {
            "id": row[0],
            "run_time": row[1],
            "event_id": row[2],
            "status": row[3],
            "confidence": row[4],
            "source": row[5],
            "diagnosis": json.loads(row[6]) if row[6] else None,
            "anomaly_metadata": json.loads(row[7]) if row[7] else None,
            # 运营确认/处理状态
            "acked_at": row[8],
            "resolution": row[9],
            "acked_by": row[10],
            "ack_note": row[11],
        }

    def save_batch(self, results: list, run_time: Optional[str] = None) -> int:
        """把一轮诊断的结果列表批量写入预警库。返回写入条数。"""
        run_time = run_time or time.strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for r in results or []:
            n = self._normalize_result(r)
            diag = n["diagnosis"]
            ameta = n["anomaly_metadata"]
            rows.append(
                (
                    run_time,
                    n["event_id"],
                    diag.get("status") if isinstance(diag, dict) else None,
                    diag.get("confidence") if isinstance(diag, dict) else None,
                    n["source"],
                    json.dumps(diag, ensure_ascii=False),
                    json.dumps(ameta, ensure_ascii=False) if ameta else None,
                )
            )
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                "INSERT INTO alerts (run_time, event_id, status, confidence, source, diagnosis, anomaly_metadata) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def recent(self, limit: int = 50, source: Optional[str] = None, include_acked: bool = True) -> list:
        """按时间倒序返回最近 limit 条预警；source 非空时按来源过滤。

        include_acked=False 时仅返回「未确认」项（默认 True=含已确认，向后兼容）。
        """
        with self._conn() as c:
            sql = f"SELECT {_SELECT_COLS} FROM alerts"
            params: list = []
            if source:
                sql += " WHERE source = ?"
                params.append(source)
                if not include_acked:
                    sql += " AND acked_at IS NULL"
            else:
                if not include_acked:
                    sql += " WHERE acked_at IS NULL"
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            cur = c.execute(sql, params)
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def latest_run(self) -> Optional[str]:
        """返回最近一次批次的 run_time；无数据返回 None。"""
        with self._conn() as c:
            cur = c.execute(
                "SELECT DISTINCT run_time FROM alerts ORDER BY run_time DESC LIMIT 1"
            )
            r = cur.fetchone()
            return r[0] if r else None

    def latest_batch(self, source: Optional[str] = None, limit: int = 50, include_acked: bool = True) -> dict:
        """返回最近一次诊断批次（run_time）的结果；可选按来源过滤。

        供 Teensing 轮询拉取：拿到的就是「上一轮整批结果」，不会跨批次错乱。
        无数据时 run_time=None、items=[]。
        """
        run_time = self.latest_run()
        if not run_time:
            return {"run_time": None, "count": 0, "alerts": []}
        with self._conn() as c:
            sql = f"SELECT {_SELECT_COLS} FROM alerts WHERE run_time = ?"
            params: list = [run_time]
            if source:
                sql += " AND source = ?"
                params.append(source)
            if not include_acked:
                sql += " AND acked_at IS NULL"
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            cur = c.execute(sql, params)
            out = [self._row_to_dict(r) for r in cur.fetchall()]
            return {"run_time": run_time, "count": len(out), "alerts": out}

    def query_since(self, since_run_time: str, source: Optional[str] = None,
                    limit: int = 200, include_acked: bool = True) -> list:
        """增量游标：返回 run_time 严格大于 since_run_time 的所有告警（可能跨多批）。

        Teensing 传上次拉取拿到的 as_of，即只拿到「更新批次」中的新增告警，
        不用每次重传整批历史，也无需客户端再比对去重。
        """
        with self._conn() as c:
            sql = f"SELECT {_SELECT_COLS} FROM alerts WHERE run_time > ?"
            params: list = [since_run_time]
            if source:
                sql += " AND source = ?"
                params.append(source)
            if not include_acked:
                sql += " AND acked_at IS NULL"
            sql += " ORDER BY run_time ASC, id ASC LIMIT ?"
            params.append(limit)
            cur = c.execute(sql, params)
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def ack(self, alert_id: int, resolution: str, acked_by: Optional[str] = None,
            note: Optional[str] = None) -> bool:
        """标记某条告警已被运营确认/处理。成功返回 True，id 不存在返回 False。

        resolution 必须是 ACK_RESOLUTIONS 之一（acknowledged/resolved/false_positive）。
        """
        if resolution not in ACK_RESOLUTIONS:
            raise ValueError(f"resolution 必须是 {ACK_RESOLUTIONS}")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as c:
            cur = c.execute(
                "UPDATE alerts SET acked_at=?, resolution=?, acked_by=?, ack_note=? WHERE id=?",
                (now, resolution, acked_by, note, alert_id),
            )
            return cur.rowcount > 0
