"""P7 · 隐私脱敏工具 —— 在「喂给 LLM 之前」对个人信息做无损脱敏。

设计原则：
- GAID（广告 ID）属个人信息(PI)。哈希后：同一 GAID 永远得到同一哈希，
  仍可跨事件去重 / 计数 / 关联，但外部（含 LLM 服务商 DeepSeek）无法还原真实 GAID。
- IP / UA 不做截断：IP 分析（地域 / ASN / 网段聚类）依赖完整 IP，截断会破坏分析准确性；
  IP 属 PI 但非敏感 PI，由部署拓扑（是否跨境）决定合规处置，本模块不擅自动它。
- 脱敏发生在编排层入口，因此真实 GAID 永不离开本网络、不会发送给 LLM 服务商。
"""

import copy
import hashlib
import hmac
import os

# 需要脱敏的 GAID 类字段名（匹配时大小写不敏感）
GAID_KEYS = {"gaid", "user_gaid", "google_advertising_id", "advertising_id", "aid"}

# 仅开发 / 测试用默认 salt；生产环境务必用环境变量 TESS_GAID_SALT 覆盖，
# 且若需跨系统按 GAID 关联，各系统须使用同一 salt。
_DEFAULT_SALT = "tess-dev-gaid-salt"


def _salt() -> str:
    return os.getenv("TESS_GAID_SALT") or _DEFAULT_SALT


def hash_gaid(gaid, salt: str | None = None) -> str:
    """对单个 GAID 做确定性哈希（HMAC-SHA256）。

    - 确定性：相同输入恒得相同输出，故可跨事件去重 / 关联。
    - salt 缺省读 TESS_GAID_SALT 环境变量；未设则用开发默认 salt。
    - 返回 64 位十六进制摘要。
    """
    if gaid is None:
        return ""
    key = (salt or _salt()).encode("utf-8")
    msg = str(gaid).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def deidentify_input(payload: dict, salt: str | None = None) -> dict:
    """深拷贝 payload，并把其中所有 GAID 类字段替换为哈希值。

    - IP / UA 等其它字段原样保留（IP 分析需要完整地址）。
    - 递归扫描整棵结构，匹配 GAID_KEYS（大小写不敏感）。
    返回新对象，绝不 mutate 入参。
    """
    if not isinstance(payload, dict):
        return payload

    data = copy.deepcopy(payload)

    def _walk(node) -> None:
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if str(k).lower() in GAID_KEYS and isinstance(v, (str, int, float)):
                    node[k] = hash_gaid(str(v), salt)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return data
