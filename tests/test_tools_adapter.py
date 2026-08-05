"""混合架构宽工具适配器单测：参数矫正（确定性，无需 LLM/connector）。

仅覆盖 resolve_analyze_params 的纯逻辑与 /tess/tools 接线；
真正的取数/LLM 由既有 47 例 pytest 与 verify_analytics.py 端到端保障。
"""

import pytest
from fastapi.testclient import TestClient

from tess_backend import app as app_module
from tess_backend.tools_adapter import (
    resolve_analyze_params,
    load_tool_schemas,
)

TOOL_NAMES = {"tess_analyze", "tess_ask", "tess_fetch_warning"}


# ---------- resolve_analyze_params：自动选 analysis_type ----------

def test_two_entities_selects_cross_dimension():
    atype, params = resolve_analyze_params({
        "advertiser_id": "1000839", "publisher_id": 1000684,
    })
    assert atype == "cross_dimension"
    assert params["advertiser_id"] == 1000839          # 字符串 id 被矫正为 int
    assert params["publisher_id"] == 1000684


def test_three_entities_selects_cross_dimension():
    atype, params = resolve_analyze_params({
        "campaign_id": 5845554, "advertiser_id": 1000839, "publisher_id": 1000684,
    })
    assert atype == "cross_dimension"
    assert set(params) == {"campaign_id", "advertiser_id", "publisher_id"}


def test_single_campaign_maps_to_campaign_detail():
    atype, params = resolve_analyze_params({"campaign_id": "5845554"})
    assert atype == "campaign_detail"
    assert params == {"campaign_id": 5845554}


def test_single_package_or_owner_maps_correctly():
    assert resolve_analyze_params({"package_name": "com.x.y"})[0] == "pkg_deepdive"
    assert resolve_analyze_params({"owner_user_id": 118})[0] == "owner_performance"
    assert resolve_analyze_params({"advertiser_id": 1})[0] == "advertiser_deepdive"
    assert resolve_analyze_params({"publisher_id": 2})[0] == "publisher_deepdive"


def test_package_plus_owner_is_cross_dimension():
    atype, params = resolve_analyze_params({
        "package_name": "link.merge.puzzle.onnect.number", "owner_user_id": 118,
    })
    assert atype == "cross_dimension"
    assert params["package_name"] == "link.merge.puzzle.onnect.number"
    assert params["owner_user_id"] == 118


def test_explicit_type_passthrough():
    atype, params = resolve_analyze_params({
        "analysis_type": "finance_check", "campaign_id": 5,
    })
    assert atype == "finance_check"
    assert params["campaign_id"] == 5


def test_explicit_invalid_type_raises():
    with pytest.raises(ValueError):
        resolve_analyze_params({"analysis_type": "not_a_real_type"})


def test_no_entities_defaults_to_account_overview():
    atype, params = resolve_analyze_params({})
    assert atype == "account_overview"
    assert params == {}


def test_unknown_fields_are_dropped():
    atype, params = resolve_analyze_params({
        "campaign_id": 1, "some_random_key": "x", "prompt": "ignore me",
    })
    assert atype == "campaign_detail"
    assert set(params) == {"campaign_id"}


def test_owner_role_whitelist():
    # 合法角色保留
    _, p1 = resolve_analyze_params({"owner_user_id": 1, "owner_role": "am"})
    assert p1.get("owner_role") == "am"
    # 非法角色被丢弃
    _, p2 = resolve_analyze_params({"owner_user_id": 1, "owner_role": "ceo"})
    assert "owner_role" not in p2


# ---------- load_tool_schemas / GET /tess/tools ----------

def test_load_tool_schemas_returns_three_tools():
    schemas = load_tool_schemas()
    assert isinstance(schemas, list)
    assert {s["function"]["name"] for s in schemas} == TOOL_NAMES
    # 每个 tool 都有参数白名单
    for s in schemas:
        assert s["function"]["parameters"]["type"] == "object"


def test_get_tess_tools_endpoint():
    client = TestClient(app_module.app)
    resp = client.get("/tess/tools")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    assert {t["function"]["name"] for t in tools} == TOOL_NAMES


def test_post_tess_tool_rejects_unknown_tool():
    client = TestClient(app_module.app)
    resp = client.post("/tess/tool", json={"tool": "evil_raw_api", "arguments": {}})
    assert resp.status_code == 400
