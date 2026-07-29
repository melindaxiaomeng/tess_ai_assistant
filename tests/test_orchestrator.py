"""P4 编排层单测 + P6 端到端（R6 示例）。

R6 示例：Margin 3.8% / Loss $350 -> Severity = HIGH（非 CRITICAL），
损耗 $350/小时。用于验收终稿 DoD。
"""

from tess_backend.contracts import STATUS_DIAGNOSED, STATUS_INCONCLUSIVE
from tess_backend.orchestrator import enrich_with_rule_engine, run_diagnosis
from tess_backend.tess_agent import MockLLMClient


def _valid_response(conf, contributor="Pub_Media_802", status=STATUS_DIAGNOSED):
    return {
        "status": status,
        "confidence": conf,
        "summary": "Pub_Media_802 映射变更叠加第三方回调超时导致收益缺失",
        "primary_contributor_id": contributor,
        "root_cause_analysis": {
            "primary_factor": "映射规则变更 + 回调超时",
            "causal_chain": ["运营变更配置", "API 超时", "转化数据缺失", "毛利暴跌"],
        },
    }


def _input_full():
    return {
        "anomaly_metadata": {
            "event_id": "ERR-20260728-0912",
            "current_value": "3.8%",
            "severity": "HIGH",
            "calculated_loss": {
                "loss_per_hour_usd": 350.0,
                "calculation_basis": "基于过去 30 分钟 Cost 速率与缺失 Revenue 的差值计算",
            },
        },
        "top_contributors": [
            {
                "dimension_type": "Publisher",
                "dimension_value": "Pub_Media_802",
                "impact_share": "82%",
            }
        ],
    }


def _input_bare():
    """缺失 severity 与 calculated_loss，仅给 current_value + 成本/收益缺口。"""
    return {
        "anomaly_metadata": {
            "event_id": "ERR-20260728-0913",
            "current_value": "3.8%",
            "cost_rate_usd": 350.0,
            "missing_revenue_usd": 0.0,
        },
        "top_contributors": [
            {
                "dimension_type": "Publisher",
                "dimension_value": "Pub_Media_802",
                "impact_share": "82%",
            }
        ],
    }


def test_enrich_keeps_existing_severity_and_loss():
    enriched = enrich_with_rule_engine(_input_full())
    meta = enriched["anomaly_metadata"]
    assert meta["severity"] == "HIGH"
    assert meta["calculated_loss"]["loss_per_hour_usd"] == 350.0


def test_enrich_computes_when_missing():
    enriched = enrich_with_rule_engine(_input_bare())
    meta = enriched["anomaly_metadata"]
    # margin 3.8 -> HIGH(<10)；loss 350 -> HIGH(>=100) -> max(HIGH, HIGH) = HIGH
    assert meta["severity"] == "HIGH"
    assert meta["calculated_loss"]["loss_per_hour_usd"] == 350.0


def test_orchestrator_does_not_mutate_caller():
    original = _input_bare()
    run_diagnosis(original, MockLLMClient(_valid_response(0.92)))
    # 编排层应深拷贝，调用方原 dict 不应被注入 severity / calculated_loss
    assert "severity" not in original["anomaly_metadata"]
    assert "calculated_loss" not in original["anomaly_metadata"]


def test_end_to_end_r6_example():
    """P6 验收：R6 示例跑通，输出三态自洽、Severity=HIGH、损失=$350。"""
    out = run_diagnosis(_input_full(), MockLLMClient(_valid_response(0.92)))
    assert out["status"] == STATUS_DIAGNOSED
    assert out["confidence"] == 0.92

    # severity / 损耗由算法层算好，不在 LLM 输出里（死锁原则）
    assert "severity" not in out
    assert "calculated_loss" not in out

    # 单独验证规则引擎对 R6 的判定
    enriched = enrich_with_rule_engine(_input_full())
    assert enriched["anomaly_metadata"]["severity"] == "HIGH"
    assert enriched["anomaly_metadata"]["calculated_loss"]["loss_per_hour_usd"] == 350.0


def test_end_to_end_inconclusive_fallback():
    out = run_diagnosis(_input_full(), MockLLMClient(
        _valid_response(0.40, status=STATUS_INCONCLUSIVE)))
    assert out["status"] == STATUS_INCONCLUSIVE
    assert out["root_cause_analysis"]["causal_chain"] == []
