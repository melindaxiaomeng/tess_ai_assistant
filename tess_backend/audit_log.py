"""P6 · 问答审计日志 —— 记录「每个运营」的请求问题与 Tess 回答。

设计要点：
- 本地 SQLite 存储（零外部依赖），表 query_log 持久化每次诊断。
- 记录 operator_id（由调用方经 X-Operator-Id 头传入，缺省 anonymous）、
  endpoint、question（原始输入/问题）、answer（Tess 归一化诊断）、
  status、confidence、meta（可选附加信息）。
- recent() 支持按 operator_id 过滤，便于「查某个人问过什么 / 答了什么」。
- 配合 P5 数据接入的 token 透传：operator_id 与拉数据用的 X-Teensing-Token
  同源，天然实现「按访问者权限回数据 + 按访问者留痕」的闭环。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

_DEFAULT_PATH = os.getenv("TESS_AUDIT_DB", "tess_audit.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueryLogStore:
    """SQLite 支撑的问答审计存储。线程安全（连接级锁）。"""

    def __init__(self, db_path: str = _DEFAULT_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          TEXT NOT NULL,
                    operator_id TEXT NOT NULL DEFAULT 'anonymous',
                    endpoint    TEXT NOT NULL,
                    question    TEXT,
                    answer      TEXT,
                    status      TEXT,
                    confidence  REAL,
                    meta_json   TEXT
                )
                """
            )
            conn.commit()

    def log_query(
        self,
        *,
        operator_id: str = "anonymous",
        endpoint: str,
        question,
        answer,
        status: Optional[str] = None,
        confidence: Optional[float] = None,
        meta: Optional[dict] = None,
    ) -> int:
        """写入一条问答记录，返回自增 id。

        question / answer 可为任意可 JSON 序列化对象；非字符串会归一化为 JSON 文本。
        """
        ts = _now_iso()
        q = question if isinstance(question, str) else json.dumps(
            question, ensure_ascii=False, default=str
        )
        a = answer if isinstance(answer, str) else json.dumps(
            answer, ensure_ascii=False, default=str
        )
        m = json.dumps(meta or {}, ensure_ascii=False) if meta is not None else None
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO query_log
                    (ts, operator_id, endpoint, question, answer, status, confidence, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, operator_id or "anonymous", endpoint, q, a, status, confidence, m),
            )
            conn.commit()
            return cur.lastrowid

    def recent(self, operator_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        """返回最近的问答记录（默认最近 100 条），可限定某运营。"""
        limit = max(1, min(int(limit), 1000))
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if operator_id:
                rows = conn.execute(
                    "SELECT * FROM query_log WHERE operator_id=? ORDER BY id DESC LIMIT ?",
                    (operator_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM query_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]
