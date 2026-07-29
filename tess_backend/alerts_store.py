"""P7 · 预警存储 —— 定时诊断产出的预警落库（本地 SQLite），供 Teensing 拉取。

设计要点：
- 定时调度器（scheduler）每小时拉异常→诊断→把结果批量写入此库；
- Teensing / SaaS 后端通过 GET /tess/alerts 轮询拉取最新预警，无需前端点击触发；
- 共享服务 token 拉全量、不按人过滤（按用户选择），故预警为全局可读列表；
- 默认用标准库 sqlite3，零额外依赖；路径可由 TESS_ALERTS_DB 覆盖。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional

DEFAULT_PATH = os.getenv("TESS_ALERTS_DB", "tess_alerts.db")


class AlertStore:
    """预警库：保存每小时诊断批次，支持按时间倒序检索。"""

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
                    diagnosis  TEXT                           -- Gatekeeper 归一化诊断（JSON）
                )
                """
            )

    @staticmethod
    def _normalize_result(r: dict) -> dict:
        """兼容 {event_id, diagnosis, meta} 与 {event_id, diagnosis} 两种结果形状。"""
        if not isinstance(r, dict):
            return {"event_id": None, "diagnosis": {}}
        diag = r.get("diagnosis") or {}
        event_id = r.get("event_id")
        if not event_id and isinstance(diag, dict):
            event_id = (diag.get("anomaly_metadata") or {}).get("event_id")
        return {"event_id": event_id, "diagnosis": diag}

    def save_batch(self, results: list, run_time: Optional[str] = None) -> int:
        """把一轮诊断的结果列表批量写入预警库。返回写入条数。"""
        run_time = run_time or time.strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for r in results or []:
            n = self._normalize_result(r)
            diag = n["diagnosis"]
            rows.append(
                (
                    run_time,
                    n["event_id"],
                    diag.get("status") if isinstance(diag, dict) else None,
                    diag.get("confidence") if isinstance(diag, dict) else None,
                    json.dumps(diag, ensure_ascii=False),
                )
            )
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                "INSERT INTO alerts (run_time, event_id, status, confidence, diagnosis) "
                "VALUES (?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def recent(self, limit: int = 50) -> list:
        """按时间倒序返回最近 limit 条预警。"""
        with self._conn() as c:
            cur = c.execute(
                "SELECT id, run_time, event_id, status, confidence, diagnosis "
                "FROM alerts ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            out = []
            for row in cur.fetchall():
                out.append(
                    {
                        "id": row[0],
                        "run_time": row[1],
                        "event_id": row[2],
                        "status": row[3],
                        "confidence": row[4],
                        "diagnosis": json.loads(row[5]) if row[5] else None,
                    }
                )
            return out

    def latest_run(self) -> Optional[str]:
        """返回最近一次批次的 run_time；无数据返回 None。"""
        with self._conn() as c:
            cur = c.execute(
                "SELECT DISTINCT run_time FROM alerts ORDER BY run_time DESC LIMIT 1"
            )
            r = cur.fetchone()
            return r[0] if r else None
