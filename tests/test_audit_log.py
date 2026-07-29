"""P6 · 问答审计日志单测：SQLite QueryLogStore 写入/检索/按运营过滤。"""

import pytest

from tess_backend.audit_log import QueryLogStore


@pytest.fixture
def store(tmp_path):
    return QueryLogStore(db_path=str(tmp_path / "audit_test.db"))


def test_log_and_recent(store):
    rid = store.log_query(
        operator_id="alice",
        endpoint="/tess/diagnose",
        question={"anomaly_metadata": {"event_id": "E1"}},
        answer={"status": "DIAGNOSED", "confidence": 0.92},
        status="DIAGNOSED",
        confidence=0.92,
    )
    assert rid >= 1
    rows = store.recent()
    assert len(rows) == 1
    assert rows[0]["operator_id"] == "alice"
    assert rows[0]["status"] == "DIAGNOSED"
    assert rows[0]["confidence"] == 0.92


def test_recent_filters_by_operator(store):
    store.log_query(operator_id="alice", endpoint="/x", question="q", answer="a")
    store.log_query(operator_id="bob", endpoint="/x", question="q", answer="a")
    store.log_query(operator_id="alice", endpoint="/x", question="q", answer="a")
    alice_rows = store.recent(operator_id="alice")
    assert len(alice_rows) == 2
    assert all(r["operator_id"] == "alice" for r in alice_rows)


def test_recent_limit(store):
    for i in range(5):
        store.log_query(operator_id="alice", endpoint="/x", question="q", answer="a")
    rows = store.recent(limit=3)
    assert len(rows) == 3


def test_anonymous_default(store):
    store.log_query(endpoint="/tess/diagnose", question="q", answer="a")
    rows = store.recent()
    assert rows[0]["operator_id"] == "anonymous"
