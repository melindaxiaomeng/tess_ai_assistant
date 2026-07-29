"""P5 / L2-1 测试：反馈自愈阈值学习 + 策略驱动的 Gatekeeper。

覆盖：
- 样本不足不提案
- 清晰可分时产出采纳提案，切点落在正确区间
- 不可分时（无信号）否决提案
- apply 落盘 / load_policy 读取 / reset 恢复
- Gatekeeper 消费自定义策略（状态重推导 + INCONCLUSIVE 钳制）
"""

import os

from tess_backend.thresholds import (
    ThresholdPolicy,
    load_policy,
    reset_policy,
    default_policy,
)
from tess_backend.feedback import FeedbackStore
from tess_backend.self_heal import propose_thresholds, apply_proposal
from tess_backend.gatekeeper import validate_tess_output
from tess_backend.contracts import STATUS_DIAGNOSED, STATUS_DIAGNOSED_SUSPECT


def _seed(store: FeedbackStore, accurate, inaccurate):
    for c in accurate:
        store.record_feedback("e", "accurate", "DIAGNOSED", c)
    for c in inaccurate:
        store.record_feedback("e", "inaccurate", "DIAGNOSED", c)


def test_insufficient_samples():
    store = FeedbackStore()
    _seed(store, [0.9, 0.85], [0.4, 0.3])
    recs = store.labeled_feedback()
    prop = propose_thresholds(recs, min_samples=20)
    assert prop.accepted is False
    assert "样本不足" in prop.reason
    assert prop.samples == 4


def test_clear_separation_accepted():
    # 高置信但也有「错的高置信」-> 应把切点抬到 0.79 附近
    store = FeedbackStore()
    accurate = [0.90, 0.88, 0.86, 0.84]
    inaccurate = [0.78, 0.72, 0.55, 0.50, 0.45]
    _seed(store, accurate, inaccurate)
    recs = store.labeled_feedback()
    prop = propose_thresholds(recs, min_samples=1)
    assert prop.accepted is True
    assert prop.proposed["suspect_floor"] == 0.79
    assert prop.proposed["high_threshold"] == 0.98
    assert prop.current_accuracy == 0.0 or prop.current_accuracy < 0.8
    assert prop.proposed_accuracy == 1.0
    assert "INCONCLUSIVE" in prop.prompt_hint


def test_no_signal_rejected():
    # accurate / inaccurate 同分布 -> 任何切点都分不开，否决
    store = FeedbackStore()
    _seed(store, [0.8, 0.8, 0.4, 0.4], [0.8, 0.8, 0.4, 0.4])
    recs = store.labeled_feedback()
    prop = propose_thresholds(recs, min_samples=1)
    assert prop.accepted is False
    assert prop.proposed == prop.current  # 不改动阈值


def test_apply_and_reset(tmp_path):
    path = str(tmp_path / "thresholds.json")
    store = FeedbackStore()
    _seed(store, [0.90, 0.88, 0.86, 0.84], [0.78, 0.72, 0.55, 0.50, 0.45])
    recs = store.labeled_feedback()
    prop = propose_thresholds(recs, min_samples=1)
    assert prop.accepted is True

    # 落盘
    policy = apply_proposal(prop, path=path)
    assert policy is not None
    assert os.path.exists(path)
    loaded = load_policy(path)
    assert loaded.suspect_floor == 0.79
    assert loaded.source == "learned"

    # reset 恢复默认并删文件
    reset_policy(path)
    assert not os.path.exists(path)
    assert load_policy(path).suspect_floor == default_policy().suspect_floor


def test_gatekeeper_consumes_custom_policy():
    inp = {
        "anomaly_metadata": {"event_id": "E1", "severity": "HIGH", "calculated_loss": {"loss_per_hour_usd": 10}},
        "top_contributors": [{"dimension_type": "channel", "dimension_value": "A"}],
    }
    llm_out = {"status": "DIAGNOSED", "confidence": 0.90, "summary": "x",
               "primary_contributor_id": "A",
               "root_cause_analysis": {"primary_factor": "p", "causal_chain": ["c"]}}

    # 默认 high=0.85：0.90 >= 0.85 -> DIAGNOSED
    d_default = validate_tess_output(llm_out, inp)
    assert d_default["status"] == STATUS_DIAGNOSED

    # 自定义 high=0.95：0.90 < 0.95 -> DIAGNOSED_SUSPECT
    custom = ThresholdPolicy(suspect_floor=0.70, high_threshold=0.95, source="learned", version=1)
    d_custom = validate_tess_output(llm_out, inp, policy=custom)
    assert d_custom["status"] == STATUS_DIAGNOSED_SUSPECT


def test_inconclusive_cap_uses_policy():
    inp = {"top_contributors": []}
    llm_out = {"status": "INCONCLUSIVE", "confidence": 0.70, "summary": "s",
               "root_cause_analysis": {"primary_factor": "p", "causal_chain": []}}
    # floor=0.40 -> cap = min(0.59, 0.39) = 0.39
    custom = ThresholdPolicy(suspect_floor=0.40, high_threshold=0.85, source="learned", version=1)
    d = validate_tess_output(llm_out, inp, policy=custom)
    assert d["status"] == "INCONCLUSIVE"
    assert d["confidence"] == 0.39
