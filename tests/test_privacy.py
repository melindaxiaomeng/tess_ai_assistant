"""隐私脱敏测试：GAID 哈希（确定性 / 去重可用）+ IP 原样保留 + 不 mutate 入参。"""


def test_hash_deterministic():
    from tess_backend.privacy import hash_gaid
    assert hash_gaid("gaid_123") == hash_gaid("gaid_123")


def test_hash_distinct():
    from tess_backend.privacy import hash_gaid
    assert hash_gaid("gaid_123") != hash_gaid("gaid_456")


def test_hash_salt_changes_output():
    from tess_backend.privacy import hash_gaid
    a = hash_gaid("gaid_123", "saltA")
    b = hash_gaid("gaid_123", "saltB")
    assert a != b


def test_deidentify_hashes_gaid_top_level():
    from tess_backend.privacy import deidentify_input, hash_gaid
    p = {"gaid": "abc", "ip": "1.2.3.4", "ua": "x"}
    out = deidentify_input(p)
    assert out["gaid"] != "abc"
    assert out["gaid"] == hash_gaid("abc")
    # IP / UA 原样保留
    assert out["ip"] == "1.2.3.4"
    assert out["ua"] == "x"


def test_deidentify_leaves_ip_intact_for_analysis():
    # 用户关切：IP 截断会破坏 IP 分析 -> 确认不截断、保留完整 IP
    from tess_backend.privacy import deidentify_input, hash_gaid
    p = {"user_context": {"gaid": "g1", "ip": "203.0.113.45", "ua": "Mozilla"}}
    out = deidentify_input(p)
    assert out["user_context"]["ip"] == "203.0.113.45"  # 完整 IP 保留
    assert out["user_context"]["gaid"] == hash_gaid("g1")


def test_deidentify_nested_and_list():
    from tess_backend.privacy import deidentify_input, hash_gaid
    p = {"events": [{"google_advertising_id": "adv1"}, {"gaid": "g2"}]}
    out = deidentify_input(p)
    assert out["events"][0]["google_advertising_id"] == hash_gaid("adv1")
    assert out["events"][1]["gaid"] == hash_gaid("g2")


def test_deidentify_does_not_mutate_input():
    from tess_backend.privacy import deidentify_input
    p = {"gaid": "abc"}
    deidentify_input(p)
    assert p["gaid"] == "abc"  # 入参未被改


def test_run_diagnosis_keeps_raw_gaid_out_of_llm_prompt():
    # 集成验证：真实 GAID 不得出现在发给 LLM 的 user prompt 中；IP 仍在。
    from tess_backend.orchestrator import run_diagnosis

    captured = {}

    class Recorder:
        def complete(self, system: str, user: str) -> str:
            captured["user"] = user
            return ('{"status":"INCONCLUSIVE","confidence":0.3,"summary":"s",'
                    '"root_cause_analysis":{"primary_factor":"p","causal_chain":[]}}')

    payload = {
        "anomaly_metadata": {
            "event_id": "E1",
            "severity": "LOW",
            "calculated_loss": {"loss_per_hour_usd": 1.0},
        },
        "top_contributors": [
            {"dimension_type": "Publisher", "dimension_value": "Pub_X", "impact_share": "80%"}
        ],
        "gaid": "real_gaid_999",
        "ip": "1.2.3.4",
    }
    run_diagnosis(payload, Recorder())
    assert "real_gaid_999" not in captured["user"]   # 真实 GAID 未进 prompt
    assert "1.2.3.4" in captured["user"]             # IP 仍在（供分析）
