"""P9 · 平台凭证注册表单测：CRUD / resolve / active_platforms。

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


def test_create_missing_token_raises(reg):
    with pytest.raises(ValueError):
        reg.create("p2", "P2", "")


def test_update_and_delete(reg):
    reg.create("p1", "P1", "tok")
    upd = reg.update("p1", token="newtok", is_active=False)
    assert upd["token"] == "newtok"
    assert upd["is_active"] is False
    # 不存在返回 None
    assert reg.update("nope", token="x") is None
    assert reg.delete("p1") is True
    assert reg.delete("p1") is False


def test_resolve_active_vs_disabled(reg):
    reg.create("active", "A", "tok-a", is_active=True)
    reg.create("off", "O", "tok-o", is_active=False)
    tok, base = reg.resolve("active")
    assert tok == "tok-a"
    # 禁用 / 不存在 / 空 -> (None, None)
    assert reg.resolve("off") == (None, None)
    assert reg.resolve("missing") == (None, None)
    assert reg.resolve(None) == (None, None)


def test_active_platforms_excludes_disabled(reg):
    reg.create("a", "A", "t-a", is_active=True)
    reg.create("b", "B", "t-b", is_active=False)
    act = reg.active_platforms()
    ids = [p["id"] for p in act]
    assert ids == ["a"]
