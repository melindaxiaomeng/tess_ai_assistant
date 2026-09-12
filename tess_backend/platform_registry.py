"""P9 · 平台凭证注册表（DB 持久化）。

多平台场景支撑：每个平台有独立的「平台级系统 token」（各平台共用同一 Teensing
base_url），后端按请求头 X-Platform-Id 解析出该平台的 token，再去 Teensing 取数；
所有落库记录（chat_sessions / alerts）都打上 platform_id，以便按平台隔离与出报表。

表 tess_platforms：
  id          : 稳定字符串主键（如 "facemoji" / "brandb"），前端在 X-Platform-Id 携带
  name        : 展示名（运营可读）
  token       : 平台级系统 token（Text，不暴露给浏览器），仅服务端用于调 Teensing
  llm_api_key : 可选，该平台专用的 LLM（DeepSeek）API key —— 上层平台各自在 DeepSeek
                开独立 key，Tess 调 LLM 时优先用它（用量/账单天然按平台区分），
                为空则回退全局 TESS_LLM_API_KEY
  base_url    : 可选，NULL 则回退全局 TESS_DATA_API_BASE_URL（多平台共用 base_url 时留空）
  is_active   : 是否启用（禁用后不参与定时诊断，且取数回退全局 token）
  created_at / updated_at

鉴权：平台管理接口（增删改查）由独立的「管理密钥」X-Admin-Key 守卫
      （对应环境变量 TESS_ADMIN_API_KEY），与 Tess 自身 X-API-Key 隔离，
      避免普通调用方误改平台凭证。TESS_ADMIN_API_KEY 未设置时管理接口整体禁用（403）。
"""

from __future__ import annotations

import os
import time
from typing import Optional

from sqlalchemy import Boolean, String, Text, select
from sqlalchemy.orm import mapped_column

from .db import Base, make_engine, make_session_factory, init_all, ensure_column, _resolve_url


class Platform(Base):
    """平台凭证表：以 id 为主键。"""

    __tablename__ = "tess_platforms"

    id = mapped_column(String, primary_key=True)
    name = mapped_column(String, default="")
    token = mapped_column(Text, default="")
    llm_api_key = mapped_column(Text, nullable=True, default=None)
    base_url = mapped_column(String, nullable=True, default=None)
    is_active = mapped_column(Boolean, default=True)
    created_at = mapped_column(String, default=lambda: _now())
    updated_at = mapped_column(String, default=lambda: _now())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class PlatformRegistry:
    """平台凭证库：增删改查 + 取数解析。"""

    def __init__(self, db_url: Optional[str] = None):
        self.url = _resolve_url(db_url)
        self.engine = make_engine(self.url)
        self.Session = make_session_factory(self.engine)
        init_all(self.engine)
        # 幂等加列：已上线的旧库（无 llm_api_key）自动补列，不破坏存量数据
        ensure_column(self.engine, "tess_platforms", "llm_api_key", "TEXT")

    # —— 基础读写 ——
    def get(self, platform_id: str) -> Optional[Platform]:
        with self.Session() as s:
            return s.get(Platform, platform_id)

    def get_dict(self, platform_id: str) -> Optional[dict]:
        """按 id 取平台并序列化为 dict；不存在返回 None。"""
        row = self.get(platform_id)
        return _to_dict(row) if row else None

    def list(self, only_active: bool = False) -> list:
        with self.Session() as s:
            q = s.query(Platform)
            if only_active:
                q = q.filter(Platform.is_active == True)  # noqa: E712
            rows = q.order_by(Platform.id).all()
            return [_to_dict(r) for r in rows]

    def create(self, platform_id: str, name: str, token: str,
               base_url: Optional[str] = None, is_active: bool = True,
               llm_api_key: Optional[str] = None) -> dict:
        if not platform_id or not str(platform_id).strip():
            raise ValueError("platform_id 不能为空")
        if not token:
            raise ValueError("token 不能为空（平台级系统 token 必填）")
        with self.Session() as s:
            if s.get(Platform, platform_id) is not None:
                raise ValueError(f"platform_id={platform_id!r} 已存在")
            now = _now()
            row = Platform(
                id=platform_id, name=name or platform_id, token=token,
                llm_api_key=(llm_api_key or None),
                base_url=base_url, is_active=is_active,
                created_at=now, updated_at=now,
            )
            s.add(row)
            s.commit()
            return _to_dict(row)

    def update(self, platform_id: str, **fields) -> Optional[dict]:
        allowed = {"name", "token", "llm_api_key", "base_url", "is_active"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return None
        with self.Session() as s:
            row = s.get(Platform, platform_id)
            if row is None:
                return None
            for k, v in updates.items():
                setattr(row, k, v)
            row.updated_at = _now()
            s.commit()
            return _to_dict(row)

    def delete(self, platform_id: str) -> bool:
        with self.Session() as s:
            row = s.get(Platform, platform_id)
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True

    # —— 取数解析 ——
    def resolve(self, platform_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """按 platform_id 解析出 (token, base_url)。

        返回 (None, None) 的情况：platform_id 为空 / 平台不存在 / 平台已禁用。
        调用方据此回退到全局 TESS_SYSTEM_TOKEN / TESS_DATA_API_BASE_URL。
        """
        if not platform_id:
            return None, None
        row = self.get(platform_id)
        if row is None or not row.is_active:
            return None, None
        base = row.base_url or os.getenv("TESS_DATA_API_BASE_URL") or None
        return row.token, base

    def resolve_llm(self, platform_id: Optional[str]) -> Optional[str]:
        """按 platform_id 解析该平台专用的 LLM（DeepSeek）API key。

        返回 None 的情况：platform_id 为空 / 平台不存在 / 平台禁用 / 未配置 llm_api_key。
        调用方据此回退到全局 TESS_LLM_API_KEY。
        """
        if not platform_id:
            return None
        row = self.get(platform_id)
        if row is None or not row.is_active:
            return None
        return row.llm_api_key or None

    def active_platforms(self) -> list:
        """返回所有启用平台的取数配置：[ {id, token, llm_api_key, base_url}, ... ]。

        供定时诊断遍历：每个平台跑一遍，alert 打上对应 platform_id。
        base_url 为空则用全局默认；llm_api_key 为空则该平台 LLM 调用回退全局 key。
        """
        out = []
        for r in self.list(only_active=True):
            out.append({
                "id": r["id"],
                "token": r["token"],
                "llm_api_key": r.get("llm_api_key"),
                "base_url": r["base_url"] or os.getenv("TESS_DATA_API_BASE_URL") or None,
            })
        return out


def _to_dict(row: Platform) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "token": row.token,
        "llm_api_key": row.llm_api_key,
        "base_url": row.base_url,
        "is_active": row.is_active,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


# —— 模块级懒加载单例 ——
_REGISTRY: Optional[PlatformRegistry] = None


def get_platform_registry() -> PlatformRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = PlatformRegistry()
    return _REGISTRY
