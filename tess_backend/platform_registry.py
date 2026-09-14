"""P9 · 平台注册表（DB 持久化）。

多平台场景支撑：X-Platform-Id 把请求归属到具体平台 —— 落库记录（chat_sessions /
alerts）打 platform_id 隔离报表，Tess 调 LLM 时优先用该平台的 llm_api_key
（上层平台各自在 DeepSeek 开独立 key，用量/账单按平台区分）。

注意：**取数已不走平台 token**（已废弃）。saas_v3.0 数据接口按运营个人 token
（X-Teensing-Token）鉴权，未带时回退全局 TESS_SYSTEM_TOKEN；tess_platforms.token
列仅作为历史遗留保留（旧客户端仍可传，但不参与任何鉴权）。

表 tess_platforms：
  id          : 稳定字符串主键（如 "melodong"），前端在 X-Platform-Id 携带
  name        : 展示名（运营可读）
  token       : 历史遗留（原平台级取数 token，已废弃，不再用于鉴权）
  llm_api_key : 可选，该平台专用的 LLM（DeepSeek）API key —— Tess 调 LLM 时优先用它，
                为空则回退全局 TESS_LLM_API_KEY
  base_url    : 历史遗留（各平台共用全局 TESS_DATA_API_BASE_URL）
  is_active   : 是否启用（禁用后不参与定时诊断、不解析 llm_api_key）
  expires_at  : 可选，「AI 对话框」付费授权的到期时间（ISO 字符串，如 "2026-09-30" 或
                "2026-09-30T23:59:59Z"）。为空 = 永久有效。
                到期后：/tess/ask、/tess/analytics、/tess/tool 一律 403（只禁新提问）；
                历史读取路径（/tess/chats、/tess/chat/{id}、/tess/chats/export、
                /tess/chats/stats）不受影响，运营仍可回看/导出既有问答。
  created_at / updated_at

鉴权：平台管理接口（增删改查）由独立的「管理密钥」X-Admin-Key 守卫
      （对应环境变量 TESS_ADMIN_API_KEY），与 Tess 自身 X-API-Key 隔离，
      避免普通调用方误改平台凭证。TESS_ADMIN_API_KEY 未设置时管理接口整体禁用（403）。
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta, timezone
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
    # AI 对话框付费授权到期时间（ISO 串，可空 = 永久有效）
    expires_at = mapped_column(String, nullable=True, default=None)
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
        # 幂等加列：AI 付费授权到期时间（旧库补列，默认 NULL = 永久有效）
        ensure_column(self.engine, "tess_platforms", "expires_at", "TEXT")

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

    def create(self, platform_id: str, name: str, token: str = "",
               base_url: Optional[str] = None, is_active: bool = True,
               llm_api_key: Optional[str] = None,
               expires_at: Optional[str] = None) -> dict:
        if not platform_id or not str(platform_id).strip():
            raise ValueError("platform_id 不能为空")
        with self.Session() as s:
            if s.get(Platform, platform_id) is not None:
                raise ValueError(f"platform_id={platform_id!r} 已存在")
            now = _now()
            row = Platform(
                id=platform_id, name=name or platform_id,
                token=(token or ""),  # 历史遗留字段，取数不再使用
                llm_api_key=(llm_api_key or None),
                base_url=base_url, is_active=is_active,
                expires_at=_normalize_expires(expires_at),
                created_at=now, updated_at=now,
            )
            s.add(row)
            s.commit()
            return _to_dict(row)

    def update(self, platform_id: str, **fields) -> Optional[dict]:
        allowed = {"name", "token", "llm_api_key", "base_url", "is_active"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        # expires_at 单独处理：允许显式清空（传 None / "" / "null" => 永久有效），
        # 这一点与其它字段「不传即不改」的语义不同，因为「取消到期」是常见操作。
        if "expires_at" in fields:
            updates["expires_at"] = _normalize_expires(fields.get("expires_at"))
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

    # —— LLM key 解析（取数平台 token 已废弃，saas_v3.0 按运营个人 token 鉴权）——
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
        """返回所有启用平台：[ {id, llm_api_key}, ... ]。

        供定时诊断遍历：每个平台跑一遍，alert 打上对应 platform_id、
        LLM 用各自的 llm_api_key（为空回退全局 key）。
        取数 token 统一由调用方用全局 TESS_SYSTEM_TOKEN（无运营 token 上下文）。
        """
        out = []
        for r in self.list(only_active=True):
            out.append({
                "id": r["id"],
                "llm_api_key": r.get("llm_api_key"),
            })
        return out

    # —— AI 对话框付费授权（按平台维度的到期时间）——
    def entitlement(self, platform_id: Optional[str]) -> dict:
        """计算某平台的「AI 问答授权」状态。

        判定顺序（只有「已注册 + 启用 + 未过期」才算可用）：
          - platform_id 为空      -> 放行（无平台上下文，如全局调用；不误伤）
          - 平台未注册            -> 放行（保持历史行为，避免品牌未登记就锁死）
          - 平台 is_active=False  -> 拒绝（reason=disabled）
          - expires_at 为空       -> 放行（永久有效）
          - expires_at < 现在     -> 拒绝（reason=expired）
        返回 dict：{platform_id, ai_enabled, expired, expires_at, days_left, reason}
        """
        if not platform_id:
            return {"platform_id": None, "ai_enabled": True, "expired": False,
                    "expires_at": None, "days_left": None, "reason": "no_platform"}
        row = self.get(platform_id)
        if row is None:
            return {"platform_id": platform_id, "ai_enabled": True, "expired": False,
                    "expires_at": None, "days_left": None, "reason": "unregistered"}
        expires_raw = getattr(row, "expires_at", None)
        if not row.is_active:
            return {"platform_id": platform_id, "ai_enabled": False, "expired": False,
                    "expires_at": expires_raw, "days_left": None, "reason": "disabled"}
        exp = _parse_expires(expires_raw)
        if exp is None:
            return {"platform_id": platform_id, "ai_enabled": True, "expired": False,
                    "expires_at": None, "days_left": None, "reason": "no_expiry"}
        now = datetime.now(timezone.utc)
        if now > exp:
            return {"platform_id": platform_id, "ai_enabled": False, "expired": True,
                    "expires_at": expires_raw, "days_left": 0, "reason": "expired"}
        days_left = max(0, math.ceil((exp - now).total_seconds() / 86400))
        return {"platform_id": platform_id, "ai_enabled": True, "expired": False,
                "expires_at": expires_raw, "days_left": days_left, "reason": "ok"}


def _normalize_expires(v: Optional[str]) -> Optional[str]:
    """入库前归一到期时间：去空白；空串 / "null" / None 一律存 None（= 永久有效）。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none"):
        return None
    return s


def _parse_expires(s: Optional[str]) -> Optional[datetime]:
    """把库里存的到期时间解析成带时区的 datetime；解析不了返回 None（视为不过期）。

    支持 "2026-09-30"（纯日期）与 "2026-09-30T23:59:59Z" / "2026-09-30T23:59:59+08:00"
    （带时间）。

    纯日期按**展示时区的当天 23:59:59** 算（默认 Asia/Shanghai），不是 UTC —— 否则运营
    在后台填「2026-09-30」，前端按北京时间一显示就变成「2026-10-01 07:59:59」，凭空多
    一天。带时间但没写时区的串仍按 UTC 解释（那是一个明确的时刻）。
    """
    if not s or not str(s).strip():
        return None
    raw = str(s).strip()
    date_only = "T" not in raw and " " not in raw
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if date_only:
        dt = dt.replace(hour=23, minute=59, second=59)
    if dt.tzinfo is None:
        tz, _ = _display_tz() if date_only else (timezone.utc, "UTC")
        dt = dt.replace(tzinfo=tz)
    return dt


def _display_tz():
    """展示时区：默认 Asia/Shanghai（运营填的到期日按北京时间看），可用
    TESS_DISPLAY_TZ 覆盖；宿主没装 tzdata 时退回固定 +08:00，不抛异常。"""
    name = os.getenv("TESS_DISPLAY_TZ", "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), name
    except Exception:
        return timezone(timedelta(hours=8)), "UTC+08:00"


def format_expiry(expires_raw: Optional[str]) -> dict:
    """把库里的 expires_at 原值展开成「前端能直接显示」的一组时间/日期字段。

    前端没必要自己解析 "2026-09-30" 到底是当天零点还是当天结束、也不用关心时区，
    这里一次性算好。没有到期时间（永久有效）时除 timezone 外全为 None。

    返回：
      expires_at_utc      规范化 UTC 串，如 "2026-09-30T15:59:59Z"
      expires_at_display  展示时区的 "YYYY-MM-DD HH:MM:SS"（前端直接显示这个）
      expires_date        "YYYY-MM-DD"
      expires_time        "HH:MM:SS"
      expires_at_ts       Unix 秒（前端算倒计时/比较大小用）
      timezone            展示时区名，如 "Asia/Shanghai"
    """
    tz, tzname = _display_tz()
    out = {
        "expires_at_utc": None,
        "expires_at_display": None,
        "expires_date": None,
        "expires_time": None,
        "expires_at_ts": None,
        "timezone": tzname,
    }
    exp = _parse_expires(expires_raw)
    if exp is None:
        return out
    local = exp.astimezone(tz)
    out.update({
        "expires_at_utc": exp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at_display": local.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_date": local.strftime("%Y-%m-%d"),
        "expires_time": local.strftime("%H:%M:%S"),
        "expires_at_ts": int(exp.timestamp()),
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
        "expires_at": getattr(row, "expires_at", None),
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
