"""SQLAlchemy 统一数据库访问层 —— Tess 各存储模块的数据库基座。

设计要点：
- 后端由环境变量 TESS_DATABASE_URL 决定，做到「一套代码、两种数据库」：
  * 默认（未设置）：sqlite:///tess_alerts.db  —— 零外部依赖，开发与单测友好；
  * 生产 / 多模块 / 数据分析：postgresql+psycopg://user:pass@host:5432/tess
- 各存储模块（AlertStore、未来的审计/反馈/处置/分析）统一在 Base 上注册模型，
  init_all() 一次性建表，便于把多模块收敛到同一个库。
- AlertStore 等模块构造时各自持有一份 engine/session（按 db_url 隔离），
  既支持测试用临时 SQLite 文件，也支持生产指向同一 Postgres 实例。
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# 默认后端：本地 SQLite 文件（零依赖）。生产请通过 TESS_DATABASE_URL 切到 Postgres。
DEFAULT_URL = os.getenv("TESS_DATABASE_URL", "sqlite:///tess_alerts.db")

# 所有 Tess 存储模型共享的声明基类；新增表只需 class X(Base): __tablename__=...
Base = declarative_base()


def make_engine(url: str = DEFAULT_URL):
    """按 URL 构造引擎；sqlite 放开 check_same_thread 以兼容跨线程访问。

    url 支持：
      - sqlite 文件路径：sqlite:///abs/path.db
      - 完整 URL：postgresql+psycopg://user:pass@host:5432/dbname
    """
    connect_args: dict = {}
    if url.startswith("sqlite"):
        # 调度器在 asyncio.to_thread 中写、请求处理在 async 中读，需跨线程
        connect_args = {"check_same_thread": False}
    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,  # 自动剔除失效连接（Postgres 空闲超时场景）
        connect_args=connect_args,
    )


def make_session_factory(engine):
    """为该 engine 创建 session 工厂；expire_on_commit=False 便于在 session 关闭后读取属性。"""
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def init_all(engine) -> None:
    """按已注册模型建表（幂等）。AlertStore 等模块初始化时调用。"""
    Base.metadata.create_all(engine)
