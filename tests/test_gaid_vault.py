"""安全版 C：GAID 金库（加密映射 / 内部 join / 日志脱敏 / 被遗忘权）测试。"""

import logging

import pytest

from tess_backend.gaid_vault import GaidVault, RedactFilter, VAULT
from tess_backend.privacy import hash_gaid


# --------------------------------------------------------------------------- #
# 1) 核心：ingest → resolve → delete（内存态，确定性哈希可还原）
# --------------------------------------------------------------------------- #
def test_ingest_resolve_delete():
    v = GaidVault()  # 内存态
    v.ingest({"gaid": "G1", "nested": {"user_gaid": "G1"}})
    h = hash_gaid("G1")
    assert v.resolve(h) == "G1"
    # 同一原始值只在映射里出现一次（去重）
    assert len(v._map) == 1
    assert v.delete(h) is True
    assert v.resolve(h) is None


def test_ingest_multiple_fields_same_value_collapses():
    v = GaidVault()
    v.ingest({"gaid": "X", "user_gaid": "X", "google_advertising_id": "X"})
    assert len(v._map) == 1
    assert v.resolve(hash_gaid("X")) == "X"


# --------------------------------------------------------------------------- #
# 2) 静态加密落盘：写出后新实例能从加密文件还原
# --------------------------------------------------------------------------- #
def test_persistence_roundtrip(tmp_path):
    p = str(tmp_path / "vault.enc")
    v1 = GaidVault(path=p)
    assert v1._cipher_kind in ("fernet", "stdlib-hmac")
    v1.ingest({"gaid": "G_PERSIST"})
    v2 = GaidVault(path=p)  # 重新从磁盘加载
    assert v2.resolve(hash_gaid("G_PERSIST")) == "G_PERSIST"


def test_persistence_file_is_encrypted(tmp_path):
    p = str(tmp_path / "vault.enc")
    GaidVault(path=p).ingest({"gaid": "SECRET_ON_DISK"})
    raw = open(p, "rb").read()
    # 磁盘上的文件不应包含明文 GAID
    assert b"SECRET_ON_DISK" not in raw


# --------------------------------------------------------------------------- #
# 3) 日志脱敏：redact() 与 RedactFilter
# --------------------------------------------------------------------------- #
def test_redact_masks_known_original():
    v = GaidVault()
    v.ingest({"gaid": "RAW_GAID_X"})
    out = v.redact("log line contains RAW_GAID_X and other text")
    assert "RAW_GAID_X" not in out
    assert "***REDACTED***" in out


def test_redact_filter_applies_to_record():
    VAULT.clear()
    VAULT.ingest({"gaid": "TOPSECRET_GAID"})
    f = RedactFilter()
    rec = logging.LogRecord(
        "tess_backend", logging.INFO, "p", 1, "user %s hit", ("TOPSECRET_GAID",), None
    )
    f.filter(rec)
    assert "TOPSECRET_GAID" not in rec.getMessage()
    assert "***REDACTED***" in rec.getMessage()


# --------------------------------------------------------------------------- #
# 4) 端点：resolve / delete（被遗忘权）
# --------------------------------------------------------------------------- #
def _client_with_vault(monkeypatch):
    import tess_backend.app as app_module
    from tess_backend.tess_agent import MockLLMClient

    fresh = GaidVault()
    monkeypatch.setattr(app_module, "VAULT", fresh)
    monkeypatch.setattr(
        app_module,
        "_get_llm_client",
        lambda: MockLLMClient(
            {
                "status": "DIAGNOSED",
                "confidence": 0.9,
                "summary": "s",
                "primary_contributor_id": "Pub_X",
                "root_cause_analysis": {"primary_factor": "p", "causal_chain": []},
            }
        ),
    )
    from fastapi.testclient import TestClient

    return fresh, TestClient(app_module.app)


def test_resolve_endpoint(monkeypatch):
    fresh, c = _client_with_vault(monkeypatch)
    fresh.ingest({"gaid": "GAID_ABC123"})
    h = hash_gaid("GAID_ABC123")
    r = c.post("/tess/gaid/resolve", json={"hashed": h})
    assert r.status_code == 200
    assert r.json()["original"] == "GAID_ABC123"
    # 未知哈希 -> 404，绝不编造
    r2 = c.post("/tess/gaid/resolve", json={"hashed": "deadbeef"})
    assert r2.status_code == 404


def test_delete_endpoint_erases(monkeypatch):
    fresh, c = _client_with_vault(monkeypatch)
    fresh.ingest({"user_gaid": "U_999"})
    h = hash_gaid("U_999")
    r = c.delete(f"/tess/gaid/{h}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    # 删除后 resolve 应 404
    assert c.post("/tess/gaid/resolve", json={"hashed": h}).status_code == 404


def test_diagnose_ingests_gaid_then_resolvable(monkeypatch):
    fresh, c = _client_with_vault(monkeypatch)
    payload = {
        "anomaly_metadata": {"event_id": "E_TEST", "severity": "HIGH"},
        "top_contributors": [
            {"dimension_type": "Publisher", "dimension_value": "Pub_X"}
        ],
        "gaid": "DIAG_GAID_1",
    }
    r = c.post("/tess/diagnose", json=payload)
    assert r.status_code == 200
    # 诊断后 Tess 已持有映射，内部 join 可还原原始 GAID
    assert fresh.resolve(hash_gaid("DIAG_GAID_1")) == "DIAG_GAID_1"


# --------------------------------------------------------------------------- #
# 5) Fernet 路径（若 cryptography 可用则自动升级）
# --------------------------------------------------------------------------- #
def test_fernet_path_if_available():
    try:
        import cryptography  # noqa
    except ImportError:
        pytest.skip("cryptography 未安装，使用 stdlib 兜底（已覆盖）")
    v = GaidVault(key="test-fernet-key")
    assert v._cipher_kind == "fernet"
    v.ingest({"gaid": "F_GAID"})
    assert v.resolve(hash_gaid("F_GAID")) == "F_GAID"
