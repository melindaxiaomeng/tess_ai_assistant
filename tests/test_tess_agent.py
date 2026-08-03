"""P3 TessAgent 单测：LLM 调用层 + 解析 + 重试 + Gatekeeper 出口。"""

import json

from tess_backend.contracts import STATUS_DIAGNOSED, STATUS_DIAGNOSED_SUSPECT, STATUS_INCONCLUSIVE
from tess_backend.tess_agent import MockLLMClient, TessAgent, SYSTEM_PROMPT, _build_user_prompt


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


def test_system_prompt_steers_suspect_hypotheses():
    # 红线固化：Prompt 必须引导模型在「有量化异常」时输出 SUSPECT 假设档，
    # 而非一律 INCONCLUSIVE（修复 anomaly-warning 全 INCONCLUSIVE 的根因）。
    assert "DIAGNOSED_SUSPECT" in SYSTEM_PROMPT
    assert "假设归因指引" in SYSTEM_PROMPT
    assert "假设，待核实" in SYSTEM_PROMPT
    # 同时保留原红线：真正无信号时仍必须 INCONCLUSIVE
    assert "INCONCLUSIVE" in SYSTEM_PROMPT


def test_prompt_includes_history_baseline():
    """history_baseline 必须进入喂给 LLM 的 user prompt（json.dumps 整个 ctx）。"""
    input_data = {
        "anomaly_metadata": {"event_id": "7030636"},
        "history_baseline": {
            "campaign_id": "7030636",
            "granularity": "day",
            "time_series": [
                {"timestamp": "2026-08-03", "revenue": 10.0, "margin_percent": -80.0},
            ],
        },
    }
    prompt = _build_user_prompt(input_data)
    assert "history_baseline" in prompt
    assert "2026-08-03" in prompt


def test_system_prompt_guides_timeseries_analysis():
    """System Prompt 必须包含时间序列曲线分析的判定法则。"""
    assert "history_baseline" in SYSTEM_PROMPT
    assert "断崖式下跌" in SYSTEM_PROMPT
    assert "渐进式恶化" in SYSTEM_PROMPT
    assert "从 [" in SYSTEM_PROMPT  # 判定指示里的「从 [具体陡降日期] 开始发生断崖式下跌」


def _anomaly_warning_input_negative_margin():
    """模拟 anomaly-warning 单点快照（毛利转负、severity=MEDIUM）。"""
    return {
        "anomaly_metadata": {
            "event_id": "6590339",
            "target_metric": "Profit",
            "current_value": -0.34,
            "benchmark_value": None,
            "severity": "MEDIUM",
            "calculated_loss": {
                "delta": None,
                "direction": "unknown",
                "metric": "Profit",
                "current_value": -0.34,
                "benchmark_value": None,
                "margin": -3.45,
                "cvr": 0.0,
            },
        },
        "top_contributors": [
            {
                "dimension_type": "Campaign",
                "dimension_value": "Sudoku_CPI_AF_ru",
                "impact_share": "100%",
                "metric_change": None,
                "margin": -3.45,
            }
        ],
        "associated_signals": [],
    }


def _suspect_hypothesis_response(contributor_id="Sudoku_CPI_AF_ru"):
    return {
        "status": "DIAGNOSED_SUSPECT",
        "confidence": 0.65,
        "summary": "疑似出价过高导致亏损投放（假设，待核实）",
        "primary_contributor_id": contributor_id,
        "root_cause_analysis": {
            "primary_factor": "疑似成本失控 / 出价过高（假设）",
            "causal_chain": [
                "margin 为负 (-3.45%)",
                "profit 为负 (-0.34)",
                "需核实：核对投放后台成本曲线与 ROI",
            ],
        },
    }


def test_suspect_hypothesis_passes_full_pipeline():
    # 端到端：单点异常快照 + LLM 返回 SUSPECT 假设 -> 经 Gatekeeper 仍为 DIAGNOSED_SUSPECT，
    # 且因果链被保留（不被当 INCONCLUSIVE 清空）。
    agent = TessAgent(MockLLMClient(_suspect_hypothesis_response()))
    out = agent.diagnose(_anomaly_warning_input_negative_margin())
    assert out["status"] == STATUS_DIAGNOSED_SUSPECT
    assert out["confidence"] == 0.65
    assert out["primary_contributor_id"] == "Sudoku_CPI_AF_ru"
    assert len(out["root_cause_analysis"]["causal_chain"]) == 3


def test_suspect_hypothesis_rejected_on_hallucinated_id():
    # 假设档若指向不存在的维度，Gatekeeper 仍应降级为 INCONCLUSIVE（幻觉 ID 拦截不变）。
    agent = TessAgent(MockLLMClient(_suspect_hypothesis_response("ghost_campaign")))
    out = agent.diagnose(_anomaly_warning_input_negative_margin())
    assert out["status"] == STATUS_INCONCLUSIVE


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
