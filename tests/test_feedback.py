"""L2-1 反馈闭环单测：度量数学 + 投票校验 + 持久化往返。"""

import pytest

from tess_backend.feedback import FeedbackStore, VOTE_ACCURATE, VOTE_INACCURATE


def _seed(store: FeedbackStore) -> None:
    """10 次诊断 + 5 次反馈的确定场景：
    - 状态：6 DIAGNOSED / 2 SUSPECT / 2 INCONCLUSIVE
    - 反馈：4 accurate + 1 inaccurate(在 DIAGNOSED 上) -> 高置信误判率 1/5=0.2
    """
    for _ in range(6):
        store.observe_diagnosis("e", "DIAGNOSED", 0.92)
    for _ in range(2):
        store.observe_diagnosis("e", "DIAGNOSED_SUSPECT", 0.70)
    for _ in range(2):
        store.observe_diagnosis("e", "INCONCLUSIVE", 0.0)

    for _ in range(4):
        store.record_feedback("e", VOTE_ACCURATE, "DIAGNOSED", 0.92)
    store.record_feedback("e", VOTE_INACCURATE, "DIAGNOSED", 0.92)


def test_metrics_math():
    s = FeedbackStore()
    _seed(s)
    m = s.metrics()
    assert m["total_diagnoses"] == 10
    assert m["feedback_count"] == 5
    assert abs(m["feedback_coverage"] - 0.5) < 1e-9
    assert m["status_distribution"]["DIAGNOSED"] == 6
    assert m["status_distribution"]["INCONCLUSIVE"] == 2
    assert abs(m["downgrade_rate"] - 0.2) < 1e-9
    # 4 accurate + 1 inaccurate(diagnosed) = 5 条 diagnosed 反馈；误判率 1/5
    assert abs(m["inaccurate_on_diagnosed_rate"] - 0.2) < 1e-9
    # 误判率 ≥0.20 -> 触发阈值上调建议
    assert "⚠️" in m["suggestion"]


def test_vote_validation():
    s = FeedbackStore()
    with pytest.raises(ValueError):
        s.record_feedback("e", "bad", "DIAGNOSED", 0.9)


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "fb.jsonl"
    s1 = FeedbackStore(persist_path=str(path))
    _seed(s1)
    assert path.exists()

    s2 = FeedbackStore(persist_path=str(path))  # 模拟进程重启
    m = s2.metrics()
    assert m["total_diagnoses"] == 10
    assert m["feedback_count"] == 5
    # 再次落盘不应重复计数（只读恢复，不重写）
    assert len(s2._ledger) == 15  # 10 诊断 + 5 反馈


def test_report_contains_key_lines():
    s = FeedbackStore()
    _seed(s)
    rep = s.report()
    assert "Tess 反馈质量周报" in rep
    assert "高置信误判率" in rep
