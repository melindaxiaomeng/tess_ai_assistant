"""P7 · 预警存储单测：建库 / 存批 / 检索 / 最新批次。"""

import pytest

from tess_backend.alerts_store import AlertStore


def _sample_results():
    return [
        {"event_id": "E1", "diagnosis": {"status": "DIAGNOSED", "confidence": 0.92, "summary": "x"}},
        {"event_id": "E2", "diagnosis": {"status": "INCONCLUSIVE", "confidence": 0.0, "summary": "y"}},
    ]


def test_save_and_recent(tmp_path):
    store = AlertStore(str(tmp_path / "alerts.db"))
    n = store.save_batch(_sample_results())
    assert n == 2
    rows = store.recent(limit=10)
    assert len(rows) == 2
    # 倒序：最近写入的在前
    assert rows[0]["event_id"] == "E2"
    assert rows[0]["status"] == "INCONCLUSIVE"
    assert rows[0]["confidence"] == 0.0
    # diagnosis 原样还原
    assert rows[0]["diagnosis"]["summary"] == "y"


def test_latest_run(tmp_path):
    store = AlertStore(str(tmp_path / "alerts.db"))
    assert store.latest_run() is None
    store.save_batch(_sample_results())
    assert store.latest_run() is not None


def test_recent_limit(tmp_path):
    store = AlertStore(str(tmp_path / "alerts.db"))
    store.save_batch(
        [{"event_id": f"E{i}", "diagnosis": {"status": "DIAGNOSED"}} for i in range(5)]
    )
    assert len(store.recent(limit=3)) == 3
    assert len(store.recent(limit=100)) == 5
