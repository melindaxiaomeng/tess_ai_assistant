"""P3 TessAgent 单测：LLM 调用层 + 解析 + 重试 + Gatekeeper 出口。"""

import json

from tess_backend.contracts import STATUS_DIAGNOSED, STATUS_DIAGNOSED_SUSPECT, STATUS_INCONCLUSIVE
from tess_backend.tess_agent import MockLLMClient, TessAgent


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------

def _base_input():
    return {
        "anomaly_metadata": {
            "event_id": "ERR-20260728-0912",
            "trigger_time": "2026-07-28 14:00:00",
            "target_metric": "Overall Margin",
            "current_value": "3.8%",
            "benchmark_value": "14.2%",
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
                "metric_change": "Margin 从 15.1% 降至 -2.4%",
            }
        ],
        "associated_signals": [
            {
                "source": "AppsFlyer_Pull_API",
                "status": "WARNING",
                "detail": "13:30-14:00 Postback 接口 HTTP 504 占比 45%",
            }
        ],
    }


def _valid_response(confidence, status=STATUS_DIAGNOSED, contributor_id="Pub_Media_802"):
    return {
        "status": status,
        "confidence": confidence,
        "summary": "Pub_Media_802 映射变更叠加第三方回调超时导致收益缺失",
        "primary_contributor_id": contributor_id,
        "root_cause_analysis": {
            "primary_factor": "映射规则变更 + 回调超时",
            "causal_chain": ["运营变更配置", "API 超时", "转化数据缺失", "毛利暴跌"],
        },
    }


class _FailingThenOkClient:
    """前 N 次 complete() 抛异常，之后开始返回合法 JSON（模拟重试成功）。"""

    def __init__(self, fail_times, payload):
        self._fail = fail_times
        self._payload = payload

    def complete(self, system, user):
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("simulated LLM network error")
        return json.dumps(self._payload, ensure_ascii=False)


class _AlwaysFailingClient:
    def complete(self, system, user):
        raise RuntimeError("simulated persistent LLM failure")


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

def test_high_confidence_becomes_diagnosed():
    agent = TessAgent(MockLLMClient(_valid_response(0.92)))
    out = agent.diagnose(_base_input())
    assert out["status"] == STATUS_DIAGNOSED
    assert out["confidence"] == 0.92
    # LLM 的因果链被保留（死锁只校验不篡改）
    assert len(out["root_cause_analysis"]["causal_chain"]) == 4


def test_mid_confidence_becomes_suspect():
    # 维度集中但无直接日志佐证：0.70 -> DIAGNOSED_SUSPECT
    agent = TessAgent(MockLLMClient(_valid_response(0.70)))
    out = agent.diagnose(_base_input())
    assert out["status"] == STATUS_DIAGNOSED_SUSPECT


def test_llm_says_inconclusive_is_respected():
    agent = TessAgent(
        MockLLMClient(_valid_response(0.40, status=STATUS_INCONCLUSIVE))
    )
    out = agent.diagnose(_base_input())
    assert out["status"] == STATUS_INCONCLUSIVE
    # 早返回分支也清空因果链（最后那个 P1 修复）
    assert out["root_cause_analysis"]["causal_chain"] == []


def test_severity_injection_is_blocked_by_gatekeeper():
    bad = _valid_response(0.95)
    bad["severity"] = "CRITICAL"  # LLM 越权返回 severity
    agent = TessAgent(MockLLMClient(bad))
    out = agent.diagnose(_base_input())
    # additionalProperties:false -> schema 违规 -> 熔断
    assert out["status"] == STATUS_INCONCLUSIVE
    assert out["confidence"] == 0.0


def test_hallucinated_id_is_downgraded():
    agent = TessAgent(
        MockLLMClient(_valid_response(0.95, contributor_id="Pub_Ghost_999"))
    )
    out = agent.diagnose(_base_input())
    assert out["status"] == STATUS_INCONCLUSIVE
    assert out["confidence"] == 0.0
    assert "不存在的维度" in out["summary"]


def test_retry_succeeds_after_transient_failures():
    client = _FailingThenOkClient(fail_times=2, payload=_valid_response(0.92))
    agent = TessAgent(client, max_retries=2)
    out = agent.diagnose(_base_input())
    assert out["status"] == STATUS_DIAGNOSED
    assert client._fail == 0  # 第 3 次（索引耗尽后）才成功


def test_persistent_failure_returns_fallback():
    agent = TessAgent(_AlwaysFailingClient(), max_retries=1)
    out = agent.diagnose(_base_input())
    assert out["status"] == STATUS_INCONCLUSIVE
    assert out["confidence"] == 0.0
    assert "重试" in out["summary"] or "多次重试无果" in out["summary"]


class _RawStringClient:
    """返回给定字符串（用于测试围栏 / 脏文本容错）。"""

    def __init__(self, text):
        self._text = text

    def complete(self, system, user):
        return self._text


def test_parse_tolerates_code_fences():
    from tess_backend.tess_agent import _parse_json

    fenced = "```json\n" + json.dumps(_valid_response(0.92), ensure_ascii=False) + "\n```"
    parsed = _parse_json(fenced)
    assert parsed["confidence"] == 0.92


def test_agent_tolerates_fenced_llm_output():
    fenced = "```json\n" + json.dumps(_valid_response(0.92), ensure_ascii=False) + "\n```"
    agent = TessAgent(_RawStringClient(fenced))
    out = agent.diagnose(_base_input())
    assert out["status"] == STATUS_DIAGNOSED
    assert out["confidence"] == 0.92
