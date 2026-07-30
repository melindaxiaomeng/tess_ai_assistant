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


def test_latest_batch_filters_by_source(tmp_path):
    """latest_batch 只返回最近一批，且按 source 过滤。

    注意：save_batch 默认用 time.strftime 作 run_time，同秒内两次调用会撞成同一批；
    故此处显式传入不同 run_time 以真正验证「最近一批」语义。
    """
    store = AlertStore(str(tmp_path / "alerts.db"))
    # 第一批(12:00)：realtime-kpi x2
    store.save_batch(
        [
            {"event_id": "R1", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "realtime-kpi"}},
            {"event_id": "R2", "diagnosis": {"status": "INCONCLUSIVE"}, "meta": {"source": "realtime-kpi"}},
        ],
        run_time="2026-07-30 12:00:00",
    )
    # 第二批(13:00)：anomaly-warning x1（更新批次）
    store.save_batch(
        [{"event_id": "A1", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "anomaly-warning"}}],
        run_time="2026-07-30 13:00:00",
    )
    # 仅 realtime-kpi：最近一批(13:00)里没有 realtime-kpi → 空
    batch = store.latest_batch(source="realtime-kpi")
    assert batch["run_time"] == "2026-07-30 13:00:00"
    assert batch["count"] == 0

    # 全量最近一批：应只含 A1
    all_batch = store.latest_batch()
    assert all_batch["run_time"] == "2026-07-30 13:00:00"
    assert all_batch["count"] == 1
    assert all_batch["alerts"][0]["event_id"] == "A1"

    # 空库返回安全默认值
    empty = AlertStore(str(tmp_path / "empty.db"))
    assert empty.latest_batch() == {"run_time": None, "count": 0, "alerts": []}


def test_anomaly_metadata_roundtrip(tmp_path):
    """原始 anomaly_metadata（current/benchmark/severity）应随预警一并落库与返回。"""
    store = AlertStore(str(tmp_path / "am.db"))
    meta = {
        "event_id": "REALTIME-GAP-09-17",
        "current_value": 0.0,
        "benchmark_value": 1234.5,
        "severity": "HIGH",
    }
    store.save_batch([
        {
            "event_id": "REALTIME-GAP-09-17",
            "diagnosis": {"status": "DIAGNOSED", "confidence": 0.9, "summary": "掉零"},
            "meta": {"source": "realtime-kpi"},
            "anomaly_metadata": meta,
        }
    ], run_time="2026-07-30 13:00:00")
    rows = store.recent(source="realtime-kpi")
    assert rows[0]["anomaly_metadata"] == meta
    batch = store.latest_batch(source="realtime-kpi")
    assert batch["alerts"][0]["anomaly_metadata"]["severity"] == "HIGH"

    # 兼容：无 anomaly_metadata 的旧形状也能存（字段为 None）
    store.save_batch([
        {"event_id": "OLD", "diagnosis": {"status": "INCONCLUSIVE"}, "meta": {"source": "anomaly-warning"}},
    ], run_time="2026-07-30 14:00:00")
    old_rows = store.recent(source="anomaly-warning")
    assert old_rows[0]["anomaly_metadata"] is None


def test_ack_marks_alert(tmp_path):
    """运营确认回写：ack 后带 resolution/acked_by/ack_note/acked_at，且默认拉取过滤掉已确认项。"""
    store = AlertStore(str(tmp_path / "ack.db"))
    store.save_batch([
        {"event_id": "R1", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "realtime-kpi"},
         "anomaly_metadata": {"severity": "HIGH"}},
    ], run_time="2026-07-30 13:00:00")
    aid = store.recent(source="realtime-kpi")[0]["id"]
    assert store.ack(aid, "resolved", acked_by="alice", note="已重启采集链路") is True

    # include_acked=True 仍能查到，且 ack 字段齐全
    acked = store.recent(source="realtime-kpi", include_acked=True)
    assert acked[0]["resolution"] == "resolved"
    assert acked[0]["acked_by"] == "alice"
    assert acked[0]["ack_note"] == "已重启采集链路"
    assert acked[0]["acked_at"] is not None

    # include_acked=False（默认）：已确认项被过滤
    assert store.recent(source="realtime-kpi", include_acked=False) == []


def test_ack_invalid_resolution(tmp_path):
    store = AlertStore(str(tmp_path / "ack2.db"))
    store.save_batch([{"event_id": "R1", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "realtime-kpi"}}],
                     run_time="2026-07-30 13:00:00")
    aid = store.recent()[0]["id"]
    with pytest.raises(ValueError):
        store.ack(aid, "bogus")


def test_ack_unknown_id(tmp_path):
    store = AlertStore(str(tmp_path / "ack3.db"))
    assert store.ack(999999, "resolved") is False


def test_query_since_incremental(tmp_path):
    """增量游标：只返回比 since_run_time 更新的所有批次。"""
    store = AlertStore(str(tmp_path / "since.db"))
    store.save_batch([{"event_id": "B1", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "realtime-kpi"}}],
                     run_time="2026-07-30 12:00:00")
    store.save_batch([{"event_id": "B2", "diagnosis": {"status": "DIAGNOSED"}, "meta": {"source": "realtime-kpi"}}],
                     run_time="2026-07-30 13:00:00")
    # 比 12:00 新：应只包含 13:00 批次的 B2
    newer = store.query_since("2026-07-30 12:00:00", source="realtime-kpi")
    assert len(newer) == 1
    assert newer[0]["event_id"] == "B2"
    # 无更新：空列表
    assert store.query_since("2026-07-30 13:00:00", source="realtime-kpi") == []
