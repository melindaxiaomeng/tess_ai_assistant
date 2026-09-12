"""P9 · 平台注册表单测：CRUD / resolve_llm / active_platforms。

不触达网络：纯本地 SQLite（tess_platforms 表由 PlatformRegistry 自行建表）。
"""

import pytest

from tess_backend.platform_registry import PlatformRegistry


@pytest.fixture
def reg(tmp_path):
    r = PlatformRegistry(str(tmp_path / "platforms.db"))
    yield r


def test_create_and_get(reg):
    row = reg.create("facemoji", "Facemoji DSP", "tok-fm", is_active=True)
    assert row["id"] == "facemoji"
    assert row["token"] == "tok-fm"
    assert row["is_active"] is True
    got = reg.get_dict("facemoji")
    assert got["name"] == "Facemoji DSP"
    assert got["token"] == "tok-fm"


def test_create_duplicate_raises(reg):
    reg.create("p1", "P1", "tok")
    with pytest.raises(ValueError):
        reg.create("p1", "P1", "tok2")


def test_create_without_token_ok(reg):
    """token 为历史遗留字段（取数平台 token 已废弃），允许不填。"""
    row = reg.create("p2", "P2", "")
    assert row["token"] == ""
    # 位置参数也可省（token 默认空串）
    row2 = reg.create("p3", "P3")
    assert row2["token"] == ""


def test_update_and_delete(reg):
    reg.create("p1", "P1", "tok")
    upd = reg.update("p1", token="newtok", is_active=False)
    assert upd["token"] == "newtok"
    assert upd["is_active"] is False
    # 不存在返回 None
    assert reg.update("nope", token="x") is None
    assert reg.delete("p1") is True
    assert reg.delete("p1") is False


def test_active_platforms_excludes_disabled(reg):
    reg.create("a", "A", "t-a", is_active=True)
    reg.create("b", "B", "t-b", is_active=False)
    act = reg.active_platforms()
    ids = [p["id"] for p in act]
    assert ids == ["a"]


# ------------------- llm_api_key：平台级 LLM（DeepSeek）key -------------------

def test_llm_api_key_crud(reg):
    # 建平台时带 llm_api_key
    row = reg.create("melo", "Melodong", "tok", llm_api_key="sk-melo")
    assert row["llm_api_key"] == "sk-melo"
    assert reg.resolve_llm("melo") == "sk-melo"
    # 不带则为 NULL（回退全局 key）
    reg.create("plain", "Plain", "tok2")
    assert reg.get_dict("plain")["llm_api_key"] is None
    assert reg.resolve_llm("plain") is None
    # 可更新（换 key / 清空）
    upd = reg.update("melo", llm_api_key="sk-melo-2")
    assert upd["llm_api_key"] == "sk-melo-2"
    reg.update("melo", llm_api_key="")
    assert reg.resolve_llm("melo") is None
    # 禁用平台不解析
    reg.update("melo", llm_api_key="sk-x", is_active=False)
    assert reg.resolve_llm("melo") is None
    # 不存在 / 空参数 -> None
    assert reg.resolve_llm("missing") is None
    assert reg.resolve_llm(None) is None


def test_active_platforms_includes_llm_key(reg):
    reg.create("a", "A", "t-a", llm_api_key="sk-a")
    reg.create("b", "B", "t-b")
    act = {p["id"]: p for p in reg.active_platforms()}
    assert act["a"]["llm_api_key"] == "sk-a"
    assert act["b"]["llm_api_key"] is None


def test_llm_api_key_lazy_migration(tmp_path):
    """旧库（tess_platforms 无 llm_api_key 列）实例化时自动补列，不破坏存量数据。"""
    from sqlalchemy import create_engine, text

    db = str(tmp_path / "old_platforms.db")
    eng = create_engine(f"sqlite:///{db}")
    with eng.connect() as c:
        c.execute(text(
            "CREATE TABLE tess_platforms (id VARCHAR PRIMARY KEY, name VARCHAR, "
            "token TEXT, base_url VARCHAR, is_active BOOLEAN, "
            "created_at VARCHAR, updated_at VARCHAR)"
        ))
        c.execute(text(
            "INSERT INTO tess_platforms (id, name, token, is_active) "
            "VALUES ('old', 'Old Platform', 'tok-old', 1)"
        ))
        c.commit()

    r = PlatformRegistry(db)  # __init__ 里 ensure_column 幂等补列
    assert r.get_dict("old")["token"] == "tok-old"  # 存量数据不受影响
    r.update("old", llm_api_key="sk-migrated")
    assert r.resolve_llm("old") == "sk-migrated"
