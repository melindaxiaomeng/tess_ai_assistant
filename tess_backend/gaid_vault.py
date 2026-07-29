"""P8 · GAID 金库（安全版 C）。

方案 C：Tess 服务器自身持有 `哈希GAID ↔ 原始GAID` 的加密映射，
内部 join 后可向最终用户返回原始 GAID；日志本地存储但脱敏。

安全属性：
- 发给 LLM（DeepSeek）的永远是哈希，本模块不参与该路径（见 privacy.py）。
- 映射表静态加密：优先 Fernet（若 cryptography 可用），否则回退 stdlib HMAC 流密码。
- 可选落盘（TESS_GAID_VAULT_PATH）；不设则仅进程内存（仍为加密态）。
- redact() 用于日志脱敏：把已知原始 GAID 从文本中抹掉。
- delete() 提供被遗忘权（按哈希删映射）。
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets

from .privacy import GAID_KEYS, hash_gaid


# --------------------------------------------------------------------------- #
# 加密层：优先 Fernet，回退 stdlib HMAC 流密码（同样满足静态加密要求）
# --------------------------------------------------------------------------- #
def _derive_fernet_key(master: str) -> bytes:
    # Fernet 要求 32 字节 url-safe base64 密钥
    return base64.urlsafe_b64encode(hashlib.sha256(master.encode("utf-8")).digest())


class _FernetCipher:
    def __init__(self, master: str) -> None:
        from cryptography.fernet import Fernet  # 仅此处 import，缺失则抛错回退

        self._f = Fernet(_derive_fernet_key(master))

    def encrypt(self, data: bytes) -> bytes:
        return self._f.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        return self._f.decrypt(token)


class _StdlibCipher:
    """HMAC-SHA256 密钥流 + HMAC 完整性标签（无外部依赖兜底）。

    提供机密性 + 完整性，足够满足"静态加密落盘"需求；若后续装了
    cryptography 会自动升级到 Fernet（见 GaidVault.__init__）。
    """

    def __init__(self, master: str) -> None:
        self._key = hashlib.sha256(master.encode("utf-8")).digest()

    def encrypt(self, data: bytes) -> bytes:
        iv = secrets.token_bytes(16)
        ks = self._keystream(iv, len(data))
        ct = bytes(a ^ b for a, b in zip(data, ks))
        tag = hmac.new(self._key, iv + ct, hashlib.sha256).digest()
        return iv + ct + tag

    def decrypt(self, token: bytes) -> bytes:
        iv, ct, tag = token[:16], token[16:-32], token[-32:]
        exp = hmac.new(self._key, iv + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(exp, tag):
            raise ValueError("GAID 金库完整性校验失败（可能被篡改）")
        ks = self._keystream(iv, len(ct))
        return bytes(a ^ b for a, b in zip(ct, ks))

    def _keystream(self, iv: bytes, n: int) -> bytes:
        out = b""
        blk = 0
        while len(out) < n:
            out += hmac.new(
                self._key, iv + blk.to_bytes(4, "big"), hashlib.sha256
            ).digest()
            blk += 1
        return out[:n]


class GaidVault:
    """加密存储 `哈希GAID ↔ 原始GAID` 的映射。

    线程安全说明：单进程 FastAPI 下足够；多 worker 各自持独立金库，
    因此 redact() 仅能覆盖本进程见过的原始 GAID（生产建议单副本或共享 KMS）。
    """

    def __init__(self, path: str | None = None, key: str | None = None) -> None:
        self._path = path or os.getenv("TESS_GAID_VAULT_PATH") or None
        master = key or os.getenv("TESS_GAID_VAULT_KEY") or "tess-dev-gaid-vault-key"
        try:
            self._cipher = _FernetCipher(master)
            self._cipher_kind = "fernet"
        except Exception:
            self._cipher = _StdlibCipher(master)
            self._cipher_kind = "stdlib-hmac"
        self._map: dict[str, str] = {}
        self._known_originals: set[str] = set()
        if self._path and os.path.exists(self._path):
            self._load()

    # ---- 写入 / 读取 ----------------------------------------------------- #
    def ingest(self, payload) -> None:
        """扫描 payload 中的 GAID 字段，存储 `哈希→原始` 映射。

        payload 里的原始 GAID 与 privacy.deidentify_input 用同一 salt 计算的
        哈希一致，因此后续 resolve(哈希) 能还原出原始值。
        """
        found: list[str] = []
        self._scan(payload, found)
        for original in found:
            if not original:
                continue
            h = hash_gaid(original)
            self._map[h] = original
            self._known_originals.add(original)
        if self._path:
            self._save()

    def resolve(self, hashed: str):
        """按哈希还原原始 GAID；未知则返回 None。"""
        return self._map.get(hashed)

    def delete(self, hashed: str) -> bool:
        """被遗忘权：删除某哈希对应的映射。返回是否真的删了。"""
        if hashed in self._map:
            original = self._map.pop(hashed)
            self._known_originals.discard(original)
            if self._path:
                self._save()
            return True
        return False

    # ---- 日志脱敏 ------------------------------------------------------- #
    def redact(self, text: str) -> str:
        """把已知原始 GAID 从文本中抹掉（供日志使用）。"""
        if not text:
            return text
        for orig in self._known_originals:
            if orig and orig in text:
                text = text.replace(orig, "***REDACTED***")
        return text

    # ---- 内部工具 ------------------------------------------------------- #
    def _scan(self, node, out: list) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in GAID_KEYS and isinstance(v, (str, int, float)):
                    out.append(str(v))
                else:
                    self._scan(v, out)
        elif isinstance(node, list):
            for item in node:
                self._scan(item, out)

    def _save(self) -> None:
        blob = json.dumps(self._map, ensure_ascii=False).encode("utf-8")
        token = self._cipher.encrypt(blob)
        with open(self._path, "wb") as f:
            f.write(token)

    def _load(self) -> None:
        with open(self._path, "rb") as f:
            token = f.read()
        blob = self._cipher.decrypt(token)
        self._map = json.loads(blob.decode("utf-8"))
        self._known_originals = set(self._map.values())

    def clear(self) -> None:
        """测试 / 运维用：清空内存映射。"""
        self._map.clear()
        self._known_originals.clear()


class RedactFilter(logging.Filter):
    """日志过滤器：把已知原始 GAID 从日志消息中抹掉（脱敏）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = VAULT.redact(record.msg)
            if isinstance(record.args, (tuple, list)):
                record.args = tuple(
                    VAULT.redact(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: VAULT.redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        except Exception:
            pass
        return True


# 模块级单例（生产通过 TESS_GAID_VAULT_KEY / TESS_GAID_VAULT_PATH 配置）
VAULT = GaidVault()
