"""P8 · 多轮会话存储 —— 服务端维护对话历史，支撑「它 / 这个 / 昨天那个」指代消解。

存储底座：SQLAlchemy（见 db.py），与 AlertStore / FeedbackStore 同库（TESS_DATABASE_URL 或默认 sqlite）。

设计要点：
- 纯服务端会话：前端只需首次生成一个 chat_id 并每次原样带回，无需自行攒历史数组；
- 每轮 = 一条 user 消息 + 一条 assistant 消息；append 时按 TESS_CHAT_HISTORY_LIMIT
  （默认 10 轮 = 20 条）裁剪，仅保留最近 N 轮，避免 token 膨胀；
- 每条 user 消息附带 meta.entities（该轮解析出的五维实体 id），供下一轮无实体追问时回退沿用；
- 模块级 get_chat_store() 懒加载单例；load_history() / record_turn() 封装多轮读写，供 app.py 与 tools_adapter 复用。
"""

from __future__ import annotations

import os
import time
from typing import Optional

from sqlalchemy import JSON, String, desc
from sqlalchemy.orm import mapped_column

from .db import Base, make_engine, make_session_factory, init_all

# 历史保留轮数（每轮 = 一问一答）；超出则仅保留最近 N 轮。可用环境变量覆盖。
DEFAULT_HISTORY_LIMIT = int(os.getenv("TESS_CHAT_HISTORY_LIMIT", "10"))


def _resolve_url(db_url: Optional[str]) -> str:
    """把构造参数归一为 SQLAlchemy URL（与 alerts_store._resolve_url 同语义）。"""
    if db_url is None:
        env = os.getenv("TESS_DATABASE_URL")
        if env:
            return env
        return "sqlite:///" + os.path.abspath("tess_alerts.db")
    if "://" in db_url:
        return db_url
    return "sqlite:///" + os.path.abspath(db_url)


class ChatSession(Base):
    """会话表：以 chat_id 为主键，messages 用 JSON 落地（兼容 sqlite/Postgres）。"""

    __tablename__ = "chat_sessions"

    chat_id = mapped_column(String, primary_key=True)
    operator_id = mapped_column(String, index=True, default="anonymous")
    messages = mapped_column(JSON, default=list)  # list[{role, content, ts, meta?}]
    created_at = mapped_column(String, default=lambda: _now())
    updated_at = mapped_column(String, default=lambda: _now())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# 五维实体键（用于 carried 实体回退，不含 owner_role/owner_name 这类修饰键作为 presence 判定）
_ENTITY_PRESENCE_KEYS = ("campaign_id", "advertiser_id", "publisher_id", "package_name", "owner_user_id")
_ENTITY_CARRY_KEYS = ("campaign_id", "advertiser_id", "publisher_id", "package_name", "owner_user_id", "owner_role", "owner_name")


class ChatStore:
    """会话库：维护多轮对话历史，支持增、查、删、裁剪。"""

    def __init__(self, db_url: Optional[str] = None):
        self.url = _resolve_url(db_url)
        self.engine = make_engine(self.url)
        self.Session = make_session_factory(self.engine)
        init_all(self.engine)  # 幂等建表

    def append(self, chat_id: str, operator_id: str, role: str, content: str,
               meta: Optional[dict] = None, limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        """追加一条消息；会话不存在则创建；超出 limit 条（=limit 轮）则裁剪尾部。"""
        with self.Session() as s:
            sess = s.get(ChatSession, chat_id)
            now = _now()
            msg = {"role": role, "content": content, "ts": now}
            if meta is not None:
                msg["meta"] = meta
            if sess is None:
                sess = ChatSession(
                    chat_id=chat_id, operator_id=operator_id or "anonymous",
                    messages=[msg], created_at=now, updated_at=now,
                )
                s.add(sess)
            else:
                sess.messages = (sess.messages or []) + [msg]
                if len(sess.messages) > limit:
                    sess.messages = sess.messages[-limit:]
                sess.updated_at = now
                if operator_id:
                    sess.operator_id = operator_id
            s.commit()

    def get_messages(self, chat_id: str, limit: Optional[int] = None) -> list:
        """返回该会话的消息列表；limit 给定则只取最近 limit 条。"""
        with self.Session() as s:
            sess = s.get(ChatSession, chat_id)
            if not sess:
                return []
            msgs = [dict(m) for m in (sess.messages or [])]
            if limit:
                msgs = msgs[-limit:]
            return msgs

    def get_last_user_entities(self, chat_id: str) -> Optional[dict]:
        """取最近一条 user 消息里记录的 meta.entities（上一轮解析出的实体），供回退沿用。"""
        msgs = self.get_messages(chat_id)
        for m in reversed(msgs):
            if m.get("role") == "user" and isinstance(m.get("meta"), dict) and m["meta"].get("entities"):
                return m["meta"]["entities"]
        return None

    def delete(self, chat_id: str) -> bool:
        with self.Session() as s:
            sess = s.get(ChatSession, chat_id)
            if sess is None:
                return False
            s.delete(sess)
            s.commit()
            return True

    def list_sessions(self, operator_id: Optional[str] = None, limit: int = 100) -> list:
        """列出会话摘要（按 updated_at 倒序）；operator_id 给定则按运营隔离。

        返回字段：chat_id / operator_id / title(首条 user 问题) / message_count /
        created_at / updated_at —— 供前端历史会话侧边栏渲染。
        """
        with self.Session() as s:
            q = s.query(ChatSession)
            if operator_id:
                q = q.filter(ChatSession.operator_id == operator_id)
            q = q.order_by(ChatSession.updated_at.desc())
            rows = q.limit(limit).all()
            out = []
            for row in rows:
                msgs = row.messages or []
                title = ""
                for m in msgs:
                    if m.get("role") == "user":
                        title = m.get("content", "")
                        break
                if not title and msgs:
                    title = msgs[0].get("content", "")
                out.append({
                    "chat_id": row.chat_id,
                    "operator_id": row.operator_id,
                    "title": title,
                    "message_count": len(msgs),
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                })
            return out

    @staticmethod
    def format_history(messages: list, turns: int = 6) -> str:
        """把最近 turns 轮（=2*turns 条）消息拼成 LLM 可读的「历史对话」文本。"""
        recent = messages[-turns * 2:] if turns else messages
        lines = []
        for m in recent:
            who = "用户" if m.get("role") == "user" else "Tess"
            lines.append(f"{who}：{m.get('content', '')}")
        return "\n".join(lines)

    def export_rows(self, operator_id: Optional[str] = None) -> list:
        """导出全量「轮」记录（按轮合并 user+assistant），供运营报表 / BI 拉取。

        返回 list[dict]，字段：
          chat_id / operator_id / question / answer / ts（该轮提问时间）/
          analysis_type / route_source / campaign_id / advertiser_id /
          publisher_id / package_name / owner_user_id
        operator_id 给定则按运营隔离。
        """
        with self.Session() as s:
            q = s.query(ChatSession)
            if operator_id:
                q = q.filter(ChatSession.operator_id == operator_id)
            rows = q.all()
        out = []
        for row in rows:
            msgs = row.messages or []
            pending_q = None
            for m in msgs:
                if m.get("role") == "user":
                    pending_q = m
                elif m.get("role") == "assistant" and pending_q is not None:
                    meta = pending_q.get("meta") or {}
                    ents = meta.get("entities") or {}
                    out.append({
                        "chat_id": row.chat_id,
                        "operator_id": row.operator_id,
                        "question": pending_q.get("content", ""),
                        "answer": m.get("content", ""),
                        "ts": pending_q.get("ts"),
                        "analysis_type": meta.get("analysis_type"),
                        "route_source": meta.get("route_source"),
                        "campaign_id": ents.get("campaign_id"),
                        "advertiser_id": ents.get("advertiser_id"),
                        "publisher_id": ents.get("publisher_id"),
                        "package_name": ents.get("package_name"),
                        "owner_user_id": ents.get("owner_user_id"),
                    })
                    pending_q = None
        return out

    def aggregate_stats(self, operator_id: Optional[str] = None) -> dict:
        """聚合运营分析指标，供 /tess/chats/stats 报表看板。

        含：会话数 / 轮数 / Top 问题 / Top 实体 / 各运营提问量 /
        分析类型分布（单维各类型 + cross_dimension + None=QA）/ 路由来源分布 / 按天分桶。
        """
        from collections import Counter

        rows = self.export_rows(operator_id)
        q_counter = Counter(r["question"] for r in rows)
        ent_counter: Counter = Counter()
        for r in rows:
            for k in ("campaign_id", "advertiser_id", "publisher_id", "package_name", "owner_user_id"):
                v = r.get(k)
                if v is not None:
                    ent_counter[f"{k}={v}"] += 1
        op_counter = Counter(r["operator_id"] for r in rows)
        at_counter = Counter(str(r["analysis_type"]) for r in rows)
        rs_counter = Counter(str(r["route_source"]) for r in rows)
        day_counter = Counter((r["ts"] or "")[:10] for r in rows)
        return {
            "total_sessions": len({r["chat_id"] for r in rows}),
            "total_turns": len(rows),
            "top_questions": [{"question": q, "count": c} for q, c in q_counter.most_common(20)],
            "top_entities": [{"entity": e, "count": c} for e, c in ent_counter.most_common(20)],
            "per_operator": [{"operator_id": o, "count": c} for o, c in op_counter.most_common(50)],
            "analysis_type_distribution": dict(at_counter),
            "route_source_distribution": dict(rs_counter),
            "daily_buckets": dict(sorted(day_counter.items())),
        }


# —— 模块级懒加载单例 + 多轮读写助手（供 app.py / tools_adapter 复用）——
_STORE: Optional[ChatStore] = None


def get_chat_store() -> ChatStore:
    global _STORE
    if _STORE is None:
        _STORE = ChatStore()
    return _STORE


def load_history(chat_id: str):
    """返回 (history_text, history_entities)；无会话则返回 (None, None)。"""
    if not chat_id:
        return None, None
    store = get_chat_store()
    msgs = store.get_messages(chat_id)
    if not msgs:
        return None, None
    return ChatStore.format_history(msgs), store.get_last_user_entities(chat_id)


def record_turn(chat_id: str, operator_id: str, question: str, answer: str,
                entities: Optional[dict], analysis_type: Optional[str] = None,
                route_source: Optional[str] = None) -> None:
    """把一轮问答写回会话；chat_id 为空则不写（单轮模式）。

    每条 user 消息的 meta 额外记录 analysis_type / route_source，
    供运营分析「问题类型分布（单维/cross/QA）」使用（见 /tess/chats/export|stats）。
    """
    if not chat_id:
        return
    store = get_chat_store()
    store.append(chat_id, operator_id, "user", question, meta={
        "entities": entities or {},
        "analysis_type": analysis_type,
        "route_source": route_source,
    })
    store.append(chat_id, operator_id, "assistant", answer)
