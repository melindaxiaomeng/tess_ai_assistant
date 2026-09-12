"""混合架构：LLM Tool Calling 宽工具适配器。

设计原则（与 MEMORY.md「架构决策」对齐）：
- LLM 的 tool_call 只产出「结构化参数块」；端点的选择与参数矫正**全部在服务端**完成。
- 仅暴露 3 个强约束宽工具：tess_analyze / tess_ask / tess_fetch_warning，
  每个内部都走已验证的确定性引擎（process_data_analysis_query / process_question / 预警库），
  绝不把 12 个细粒度 API 裸暴露给 LLM。
- 参数白名单 + 类型矫正 + 枚举约束，过滤掉任何未知字段，杜绝静默错数与幻觉。
"""

import json
import os
from pathlib import Path

from .analytics import (
    process_data_analysis_query,
    process_question,
    ANALYSIS_TYPES,
)

_SCHEMA_PATH = Path(__file__).parent / "tool_schemas.json"

# 单实体 -> 对应深度下钻类型（命中优先级：campaign > advertiser > publisher > package > owner）
_SINGLE_DIM_TYPE = {
    "campaign_id": "campaign_detail",
    "advertiser_id": "advertiser_deepdive",
    "publisher_id": "publisher_deepdive",
    "package_name": "pkg_deepdive",
    "owner_user_id": "owner_performance",
}

# 允许 LLM 透传的实体字段白名单（其余字段一律忽略）
_ENTITY_KEYS = (
    "campaign_id",
    "advertiser_id",
    "publisher_id",
    "package_name",
    "owner_user_id",
    "owner_role",
)


def _as_int(v):
    """宽松转 int；失败/None 返回 None（由调用方决定是否必需）。"""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def load_tool_schemas() -> list:
    """读取 tool_schemas.json，返回 tool schema 清单（OpenAI function-calling 风格）。"""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_analyze_params(args: dict) -> tuple:
    """参数矫正：白名单化 LLM 入参并自动选 analysis_type。

    返回 (analysis_type, params)，params 使用引擎期望的**单数**键
    （campaign_id / advertiser_id / publisher_id / package_name / owner_user_id / owner_role），
    由 fetch_bi_analysis_context 内部转复数打 /report。

    路由规则：
      - 显式 analysis_type 合法 -> 直接透传（仍走白名单校验）。
      - 否则按命中实体维度数：>=2 -> cross_dimension；1 -> 对应单维类型；0 -> account_overview。
    """
    args = args or {}
    params: dict = {}

    cid = _as_int(args.get("campaign_id"))
    if cid is not None:
        params["campaign_id"] = cid
    aid = _as_int(args.get("advertiser_id"))
    if aid is not None:
        params["advertiser_id"] = aid
    pid = _as_int(args.get("publisher_id"))
    if pid is not None:
        params["publisher_id"] = pid
    pkg = args.get("package_name")
    if pkg:
        params["package_name"] = str(pkg)
    oid = _as_int(args.get("owner_user_id"))
    if oid is not None:
        params["owner_user_id"] = oid
    role = args.get("owner_role")
    if role in ("am", "bd"):
        params["owner_role"] = role

    explicit = args.get("analysis_type")
    if explicit:
        if explicit not in ANALYSIS_TYPES:
            raise ValueError(f"不支持的 analysis_type={explicit!r}（支持 {', '.join(sorted(ANALYSIS_TYPES))}）")
        return explicit, params

    present = [k for k in ("campaign_id", "advertiser_id", "publisher_id", "package_name", "owner_user_id") if k in params]
    if len(present) >= 2:
        return "cross_dimension", params
    if len(present) == 1:
        return _SINGLE_DIM_TYPE[present[0]], params
    # 无实体：退回账户全景，保证一定有数据可答
    return "account_overview", params


def _resolve_runtime(request):
    """复用 app.py 的 connector/llm/token 装配逻辑（懒加载，避免顶层循环依赖）。

    返回 (connector, llm, token, token_mode, operator, platform_id)，
    token 按「按人 > 全局」两级解析（平台 token 已废弃）：
      1) 运营个人 token（X-Teensing-Token，按人 RBAC 取数）
      2) 全局 TESS_SYSTEM_TOKEN。
    platform_id 不参与取数，仅用于落库打标与 LLM key 解析。
    """
    from .app import (
        _get_data_connector,
        _get_llm_client,
        _operator_id,
        _platform_id,
        _resolve_access_token,
        TeensingDataConnector,
    )
    from fastapi import HTTPException

    connector = _get_data_connector()
    operator = _operator_id(request) if request is not None else "anonymous"
    platform_id = _platform_id(request) if request is not None else None
    # LLM key 按平台隔离：平台 llm_api_key（DeepSeek 按平台开 key）> 全局 TESS_LLM_API_KEY
    llm = _get_llm_client(platform_id)
    effective_token, token_mode = _resolve_access_token(request, platform_id)
    if isinstance(connector, TeensingDataConnector) and not effective_token:
        raise HTTPException(
            status_code=400,
            detail="生产数据接入需取数凭据：运营 X-Teensing-Token、或在后端配置 TESS_SYSTEM_TOKEN",
        )
    return connector, llm, effective_token, token_mode, operator, platform_id


def dispatch_tool(tool_name: str, args: dict, request=None) -> dict:
    """统一入口：把 LLM 的 tool_call 映射到确定性引擎。

    三个工具分别对应：
      - tess_analyze      -> process_data_analysis_query（结构化分析，含 cross_dimension 真 join）
      - tess_ask          -> process_question（自然语言问答 + 浅层兜底 fetch_qa_context）
      - tess_fetch_warning-> get_alerts / get_realtime_kpi_alerts（预警库拉取）
    """
    from fastapi import HTTPException

    args = args or {}

    if tool_name == "tess_analyze":
        analysis_type, params = resolve_analyze_params(args)
        connector, llm, token, token_mode, operator, platform_id = _resolve_runtime(request)
        return process_data_analysis_query(
            analysis_type, connector, llm,
            token=token, params=params,
            operator_id=operator, token_mode=token_mode,
        )

    if tool_name == "tess_ask":
        question = args.get("question")
        if not question or not str(question).strip():
            raise HTTPException(status_code=400, detail="缺少 question 字段或为空")
        analysis_type = args.get("analysis_type")
        params = {k: args[k] for k in _ENTITY_KEYS if args.get(k) is not None}
        chat_id = args.get("chat_id")
        history_text, history_entities = (None, None)
        if chat_id:
            from .chat_store import load_history

            history_text, history_entities = load_history(chat_id)
        connector, llm, token, token_mode, operator, platform_id = _resolve_runtime(request)
        result = process_question(
            str(question), connector, llm,
            token=token, params=params,
            operator_id=operator, token_mode=token_mode,
            analysis_type=analysis_type,
            history=history_text, history_entities=history_entities,
        )
        if chat_id:
            from .chat_store import record_turn
            from .analytics import extract_entities, resolve_entities

            try:
                _ents = extract_entities(str(question), params)
                _ents = resolve_entities(_ents, connector, token)
            except Exception:
                _ents = {}
            cs = result.get("context_summary", {})
            record_turn(
                chat_id, operator, str(question), result.get("answer", ""),
                _ents,
                analysis_type=cs.get("analysis_type"),
                route_source=cs.get("route_source"),
                platform_id=platform_id or "default",
                usage=cs.get("llm_usage"),
            )
        return result

    if tool_name == "tess_fetch_warning":
        from .app import get_alerts, get_realtime_kpi_alerts

        scope = args.get("scope", "anomaly-warning")
        if scope not in ("anomaly-warning", "realtime-kpi", "all"):
            raise HTTPException(
                status_code=400,
                detail="scope 必须是 anomaly-warning | realtime-kpi | all 之一",
            )
        limit = max(1, min(int(args.get("limit", 20)), 50))
        min_sev = args.get("min_severity")
        min_rev = args.get("min_revenue")
        include_acked = bool(args.get("include_acked", True))
        if scope == "realtime-kpi":
            return get_realtime_kpi_alerts(
                limit=limit, min_severity=min_sev,
                min_revenue=min_rev, include_acked=include_acked,
                request=request,
            )
        source = None if scope == "all" else scope
        return get_alerts(
            limit=limit, source=source, min_severity=min_sev,
            min_revenue=min_rev, include_acked=include_acked,
            request=request,
        )

    raise HTTPException(status_code=400, detail=f"未知 tool: {tool_name}")
