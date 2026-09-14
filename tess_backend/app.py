"""P4 · HTTP API —— 暴露 POST /tess/diagnose。

薄薄一层：只把请求体交给编排层 run_diagnosis，再把 Gatekeeper 归一化后的
安全结果返回前端。LLM 客户端通过依赖注入，便于测试时换成 Mock。

生产部署：
- 真实 LLM 用 HttpLLMClient(base_url, api_key, model)，api_key 从环境变量读取。
- uvicorn tess_backend.app:app --port 8080
"""

import asyncio
import hmac
import os
import re
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from .orchestrator import run_diagnosis
from .tess_agent import HttpLLMClient, LLMClient
from .feedback import FeedbackStore
from .thresholds import load_policy, reset_policy, default_policy
from .self_heal import propose_thresholds, apply_proposal
from .joint import run_joint_diagnosis
from .remediation import (
    propose_remediation,
    RemediationStore,
    MockRemediationExecutor,
)
from .data_connector import (
    get_data_connector,
    normalize_to_context,
    extract_realtime_anomalies,
    TeensingDataConnector,
    should_diagnose,
    _num,
)
from .analytics import process_data_analysis_query, process_question
from .analytics import extract_entities, resolve_entities
from .chat_store import load_history, record_turn, get_chat_store
from .tools_adapter import dispatch_tool, load_tool_schemas
from .gaid_vault import VAULT, RedactFilter
from .audit_log import QueryLogStore
from .alerts_store import AlertStore
from .platform_registry import get_platform_registry
from .dev_seed import DEMO_EVENT_IDS

app = FastAPI(title="Tess Diagnose API", version="2.3.0")

# L2-1 反馈闭环：模块级单例。设 TESS_FEEDBACK_PATH 可持久化 JSONL。
STORE = FeedbackStore(persist_path=os.getenv("TESS_FEEDBACK_PATH") or None)
# L2-3 半自动处置：模块级单例。设 TESS_REMEDIATION_PATH 可持久化 JSONL。
REMEDIATION_STORE = RemediationStore(persist_path=os.getenv("TESS_REMEDIATION_PATH") or None)
# 执行器默认 Mock；生产替换为 Teensing 平台真实 API 适配器。
REMEDIATION_EXECUTOR = MockRemediationExecutor()

# P6 问答审计：本地 SQLite，记录每个运营的「问题 + Tess 回答」。
AUDIT = QueryLogStore()
# P7 定时预警存储：每小时诊断结果落库，供 Teensing 轮询拉取。
ALERTS = AlertStore()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产请收紧为 Teensing 前端域名
    allow_methods=["*"],  # 含 GET(健康检查)/POST(诊断)/DELETE(GAID删除) 等
    allow_headers=["*"],
)

# 可选 API Key 鉴权（纵深防御）：仅当环境变量 TESS_API_KEY 被设置时才强制校验。
# - 开发期不设置 -> 行为与之前完全一致（无鉴权，测试前端照常可用）。
# - 生产期设置后 -> 所有 /tess/* 接口要求请求头 X-API-Key 匹配，否则返回 401；
#   /healthz 存活探针与 CORS 预检(OPTIONS) 一律放行，不影响监控与跨域。
_TESS_API_KEY = os.getenv("TESS_API_KEY", "")


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    if request.method == "OPTIONS":  # CORS 预检请求不带鉴权头，必须放行
        return await call_next(request)
    if request.url.path == "/healthz":  # 存活探针不鉴权
        return await call_next(request)
    # 平台管理接口走独立的 X-Admin-Key 守卫（_require_admin），
    # 不在全局 X-API-Key 范围内（运维通常只持有管理密钥，没有调用方密钥）。
    if request.url.path.startswith("/tess/admin/"):
        return await call_next(request)
    if _TESS_API_KEY:
        provided = request.headers.get("X-API-Key", "")
        if not provided or not hmac.compare_digest(provided, _TESS_API_KEY):
            return JSONResponse(
                status_code=401,
                content={"detail": "缺少或错误的 X-API-Key"},
            )
    return await call_next(request)

# 方案 C：Tess 本地日志若含原始 GAID，自动抹掉（日志脱敏，不落明文）
import logging
logging.getLogger("tess_backend").addFilter(RedactFilter())
logging.getLogger("uvicorn").addFilter(RedactFilter())


def _get_llm_client(platform_id: Optional[str] = None) -> LLMClient:
    """依赖注入：生产读环境变量（默认 DeepSeek OpenAI 兼容端点）。

    LLM key 解析优先级（与取数 token 的平台隔离同理）：
      1) 平台级 llm_api_key：按 X-Platform-Id 对应 tess_platforms 记录 ——
         上层平台（如 Melodong）各自在 DeepSeek 开独立 key，用量/账单按平台区分；
      2) 全局 TESS_LLM_API_KEY（兜底，所有平台共用）。
    base_url / model 仍取全局 TESS_LLM_BASE_URL / TESS_LLM_MODEL（共用同一个 LLM 服务）。
    """
    base_url = os.getenv("TESS_LLM_BASE_URL", "https://api.deepseek.com")
    model = os.getenv("TESS_LLM_MODEL", "deepseek-chat")
    api_key = ""
    if platform_id:
        try:
            api_key = get_platform_registry().resolve_llm(platform_id) or ""
        except Exception:
            api_key = ""
    if not api_key:
        api_key = os.getenv("TESS_LLM_API_KEY", "")
    if not api_key:
        hint = (
            f"（平台 {platform_id} 未配置 llm_api_key，且未设置全局 TESS_LLM_API_KEY）"
            if platform_id else "（请设置 TESS_LLM_API_KEY）"
        )
        raise HTTPException(
            status_code=503,
            detail=f"Tess LLM 未配置{hint}",
        )
    return HttpLLMClient(base_url, api_key, model, json_mode=True)


# P5 数据接入层：惰性初始化（避免 import 期因未配置 teensing 而崩溃）。
_DATA_CONNECTOR = None


def _get_data_connector():
    """按 TESS_DATA_CONNECTOR 选择 mock / teensing 实现，首次使用时创建并缓存。"""
    global _DATA_CONNECTOR
    if _DATA_CONNECTOR is None:
        _DATA_CONNECTOR = get_data_connector()
    return _DATA_CONNECTOR


def run_scheduled_diagnosis(limit: int = 20, connector=None, llm=None,
                           platform_id: Optional[str] = None) -> list:
    """P7 定时预警：按平台分别拉异常 → 诊断 → 存预警库（每条告警打 platform_id）。

    数据来源（两轮，统一进同一批预警）：
    (1) 异常预警 + 涨跌榜（/overview/ranking/*）→ 归一化 → 诊断（source="anomaly-warning"）
    (2) 实时 KPI 小时级曲线（/overview/realtime-kpi）→ 提取骤降异常点 → 诊断（source="realtime-kpi"）

    platform_id 指定时只跑该平台；为 None 时遍历所有启用平台；
    若未注册任何平台，跑一遍全局（platform_id="default"，旧行为）。
    取数 token：定时诊断无请求上下文（没有运营个人 token），统一用全局 TESS_SYSTEM_TOKEN。
    LLM key 仍按平台隔离：优先平台 llm_api_key（DeepSeek 按平台开 key、账单分开算），
    回退全局 TESS_LLM_API_KEY；显式传入 llm 时（测试/内部调用）直接用。
    各平台共用同一 Teensing base_url，故单 connector 复用。
    返回本轮所有平台汇总诊断结果列表（meta 含 source 标签）。
    """
    connector = connector or _get_data_connector()
    policy = load_policy()

    # 构造待跑平台清单：(platform_id, token) —— 平台仅用于打标与 LLM key 区分，
    # 取数 token 统一全局 TESS_SYSTEM_TOKEN（平台 token 已废弃）
    sys_token = os.getenv("TESS_SYSTEM_TOKEN") or ""
    targets: list = []
    if platform_id:
        targets.append((platform_id, sys_token))
    else:
        try:
            plats = get_platform_registry().active_platforms()
        except Exception:
            plats = []
        if plats:
            for p in plats:
                targets.append((p["id"], sys_token))
        else:
            targets.append(("default", sys_token))

    all_results: list = []
    for pid, token in targets:
        platform_llm = llm or _get_llm_client(pid)  # 平台 key > 全局 key
        results = _diagnose_one_platform(connector, platform_llm, token, pid, limit, policy)
        if results:
            ALERTS.save_batch(results, platform_id=pid)
        all_results.extend(results)
    return all_results


def _diagnose_one_platform(connector, llm, token, platform_id: str, limit: int, policy) -> list:
    """对单个平台跑完整诊断两轮，返回结果列表（不落库；由调用方按 platform_id 落库）。"""
    results: list = []

    # (1) 异常预警 + 涨跌榜
    raw_events = connector.fetch_recent_anomalies(limit, token=token)
    for raw in raw_events:
        # 拉取该 campaign 的历史时间序列，作为诊断的「时间锚点」注入上下文
        cid = raw.get("campaign_id")
        fetcher = getattr(connector, "fetch_campaign_time_series", None)
        history_baseline = None
        if cid is not None and callable(fetcher):
            try:
                # 透传 publisher_id，使历史曲线只含「报警的 (campaign,publisher) 对」，而非整 campaign 混合值
                history_baseline = fetcher(
                    str(cid), token=token,
                    publisher_id=raw.get("publisher_id"),
                )
            except Exception as e:
                logger.warning("平台 %s 拉取 campaign %s 历史趋势失败: %s", platform_id, cid, e)
        # 营收门槛降噪 + 小投放猝死豁免：低营收且无断崖式下跌则跳过，不进诊断
        if not should_diagnose(raw, history_baseline):
            logger.info(
                "平台 %s campaign %s 营收 %.2f 低于门槛且无断崖式下跌，跳过诊断",
                platform_id, cid, _num(raw.get("revenue")),
            )
            continue
        if history_baseline is not None:
            raw["history_baseline"] = history_baseline
        ctx = normalize_to_context(raw)
        VAULT.ingest(ctx)
        event_id = (ctx.get("anomaly_metadata") or {}).get("event_id", "UNKNOWN")
        diag = _safe_diagnose(ctx, llm, policy)
        STORE.observe_diagnosis(
            event_id, diag.get("status", "UNKNOWN"), diag.get("confidence", 0.0)
        )
        results.append(
            {
                "event_id": event_id,
                "diagnosis": diag,
                "meta": {"source": "anomaly-warning"},
                "anomaly_metadata": ctx.get("anomaly_metadata"),
            }
        )

    # (2) 实时 KPI 小时级曲线：每小时也拉一遍，看是否数据异常
    try:
        raw_kpi = connector.fetch_realtime_kpi(token=token)
        for ctx in extract_realtime_anomalies(raw_kpi):
            VAULT.ingest(ctx)
            event_id = (ctx.get("anomaly_metadata") or {}).get("event_id", "UNKNOWN")
            diag = _safe_diagnose(ctx, llm, policy)
            STORE.observe_diagnosis(
                event_id, diag.get("status", "UNKNOWN"), diag.get("confidence", 0.0)
            )
            results.append(
                {
                    "event_id": event_id,
                    "diagnosis": diag,
                    "meta": {"source": "realtime-kpi"},
                    "anomaly_metadata": ctx.get("anomaly_metadata"),
                }
            )
    except Exception as e:  # realtime 拉取/解析失败不应拖垮整批预警
        logger.warning("平台 %s realtime-kpi 拉取或分析失败，本轮跳过实时异常检测: %s", platform_id, e)

    return results


def _safe_diagnose(ctx: dict, llm, policy) -> dict:
    """单条诊断；异常不拖垮整批，降级为 INCONCLUSIVE。"""
    try:
        return run_diagnosis(ctx, llm, policy=policy)
    except HTTPException:
        raise
    except Exception as e:
        return {
            "status": "INCONCLUSIVE",
            "confidence": 0.0,
            "summary": f"Tess 诊断链路异常，已自动切入人工排查：{type(e).__name__}",
            "root_cause_analysis": {
                "primary_factor": f"系统异常：{type(e).__name__}",
                "causal_chain": ["编排链路异常", "转人工处理"],
            },
        }


def _operator_id(request: Request) -> str:
    """从请求头取运营身份（X-Operator-Id）；缺省记 anonymous。"""
    return request.headers.get("X-Operator-Id", "anonymous") or "anonymous"


def _platform_id(request: Request) -> Optional[str]:
    """取平台标识：优先请求头 X-Platform-Id，其次查询参数 ?platform=；缺省 None。

    用于把一次请求归属到具体平台（LLM key 解析 + 落库打标 + 报表隔离）。
    """
    pid = request.headers.get("X-Platform-Id", "") or ""
    if not pid and getattr(request, "query_params", None):
        pid = request.query_params.get("platform", "") or ""
    return pid.strip() or None


def _teensing_token(request: Optional[Request]) -> str:
    """从请求头取运营 SaaS access_token（X-Teensing-Token），用于按权限拉数据；缺省空串。"""
    if not request:
        return ""
    return request.headers.get("X-Teensing-Token", "") or ""


def _resolve_access_token(request: Optional[Request], platform_id: Optional[str] = None) -> tuple:
    """解析本次调 saas_v3.0 数据接口用的 token 与模式。

    优先级（平台 token 已废弃：saas_v3.0 实际按运营 token 鉴权，平台级取数 token 无用武之地）：
      1) 运营个人 token（X-Teensing-Token：前端逐请求带当前登录运营的 access_token，
         saas_v3.0 按该运营的 RBAC/数据权限返回数据 —— 各运营各看各的）
      2) 全局 TESS_SYSTEM_TOKEN（兜底，写在后端 .env / compose，前端不接触；
         主要供定时诊断等无请求上下文的场景）

    返回 (effective_token:str, token_mode:str)，token_mode ∈ {user, system}。
    """
    user_token = _teensing_token(request)
    if user_token:
        return user_token, "user"
    system_token = os.getenv("TESS_SYSTEM_TOKEN") or None
    return (system_token or ""), "system"


# —— 平台管理接口鉴权（独立于 Tess 自身 X-API-Key，避免普通调用方误改平台凭证）——
_ADMIN_API_KEY = os.getenv("TESS_ADMIN_API_KEY", "")


def _require_admin(request: Request) -> None:
    """平台 CRUD 管理密钥校验：X-Admin-Key 必须等于 TESS_ADMIN_API_KEY。

    TESS_ADMIN_API_KEY 未设置时管理接口整体禁用（403），避免误开。
    """
    if not _ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="平台管理接口未启用（请设置 TESS_ADMIN_API_KEY）")
    provided = request.headers.get("X-Admin-Key", "") or ""
    if not provided or not hmac.compare_digest(provided, _ADMIN_API_KEY):
        raise HTTPException(status_code=403, detail="缺少或错误的 X-Admin-Key")


# —— AI 对话框付费授权闸门（按平台维度的到期时间，见 platform_registry.entitlement）——
def _require_ai_entitlement(request: Request) -> dict:
    """只拦「新建提问」类入口：/tess/ask、/tess/analytics、/tess/tool。

    **历史读取路径不经过这里**（/tess/chats、/tess/chat/{id}、/tess/chats/export、
    /tess/chats/stats），所以平台到期后运营仍可回看 / 导出既有问答 —— 满足
    「到期只禁新提问，不锁历史」。

    判定（委托 PlatformRegistry.entitlement）：
      - 未带 X-Platform-Id / ?platform=  -> 放行（无平台上下文，全局兜底路径，不误伤）
      - 平台未注册                        -> 放行（避免品牌还没登记就被锁死）
      - 平台 is_active=False              -> 403 PLATFORM_DISABLED
      - expires_at 已过                   -> 403 AI_EXPIRED
      - 未设 expires_at / 未到期          -> 放行

    到期错误体带机器可读 code，前端据此弹「已到期，请联系续费」：
      { "detail": { "code": "AI_EXPIRED", "message": "...", "platform_id": "...",
                    "expires_at": "...", "days_left": 0 } }
    """
    platform_id = _platform_id(request)
    try:
        ent = get_platform_registry().entitlement(platform_id)
    except Exception:
        # 授权库异常不应阻断正常问答（宁可放过，避免误伤生产）
        return {"platform_id": platform_id, "ai_enabled": True, "reason": "registry_error"}

    if ent.get("ai_enabled"):
        return ent

    if ent.get("reason") == "disabled":
        raise HTTPException(status_code=403, detail={
            "code": "PLATFORM_DISABLED",
            "message": f"平台「{platform_id}」已停用，暂不可使用 AI 问答。",
            "platform_id": platform_id,
        })
    raise HTTPException(status_code=403, detail={
        "code": "AI_EXPIRED",
        "message": f"AI 功能已于 {ent.get('expires_at')} 到期，请联系我们续费后继续使用。",
        "platform_id": platform_id,
        "expires_at": ent.get("expires_at"),
        "days_left": 0,
    })


@app.post("/tess/diagnose")
def diagnose(payload: dict, request: Request) -> dict:
    """接收异常上下文 Input，返回 Gatekeeper 归一化后的归因结果。

    每次诊断都会登记进反馈_ledger（observe_diagnosis），用于算覆盖率 / 降级率；
    同时写入 P6 问答审计（operator_id 来自 X-Operator-Id 请求头）。
    """
    operator = _operator_id(request)
    llm = _get_llm_client()
    event_id = (payload or {}).get("anomaly_metadata", {}).get("event_id", "UNKNOWN")
    # 方案 C：Tess 自身持有 `哈希↔原始` GAID 加密映射（内部 join 用）
    if payload:
        VAULT.ingest(payload)
    policy = load_policy()  # 读学习后的阈值（无则默认初版）
    try:
        result = run_diagnosis(payload, llm, policy=policy)
    except HTTPException:
        raise
    except Exception as e:  # 编排层任何意外都不应把堆栈泄露给前端
        result = {
            "status": "INCONCLUSIVE",
            "confidence": 0.0,
            "summary": "Tess 诊断链路异常，已自动切入人工排查。",
            "root_cause_analysis": {
                "primary_factor": f"系统异常：{type(e).__name__}",
                "causal_chain": ["编排链路异常", "转人工处理"],
            },
        }
    STORE.observe_diagnosis(event_id, result["status"], result["confidence"])
    # P6 审计：记录「谁问了什么 → Tess 答了什么」
    AUDIT.log_query(
        operator_id=operator,
        endpoint="/tess/diagnose",
        question=payload,
        answer=result,
        status=result.get("status"),
        confidence=result.get("confidence"),
        meta={"event_id": event_id},
    )
    return result


# 支持的 analysis_type 全集（/tess/analytics 预设 + /tess/ask 深度下钻新增类型）
SUPPORTED = (
    "daily_summary", "scaling_opportunity", "finance_check",
    "account_overview", "publisher_deepdive", "scaling_capacity",
    "campaign_detail", "advertiser_deepdive", "traffic_policy_check", "kpi_compare",
    "campaign_ranking", "pkg_deepdive", "owner_performance", "cross_dimension",
)


@app.post("/tess/analytics")
def post_analytics(payload: dict, request: Request) -> dict:
    """主动式数据分析（Proactive BI）：按分析类型拉取 Teensing 业务数据，由 LLM 生成简报。

    请求体：
      {
        "analysis_type": "daily_summary" | "scaling_opportunity" | "finance_check"
                         | "account_overview" | "publisher_deepdive" | "scaling_capacity"
                         | "campaign_detail" | "advertiser_deepdive" | "traffic_policy_check" | "kpi_compare"
                         | "campaign_ranking" | "pkg_deepdive" | "owner_performance" | "cross_dimension",
        # cross_dimension 需 params 内 ≥2 个实体 id（campaign_id/advertiser_id/publisher_id/package_name/owner_user_id）
        "params": { "report_month": "2026-08" }   # finance_check 可选
        # 实体下钻可选参数：campaign_id / advertiser_id / publisher_id（如 campaign_detail 需 campaign_id）
        #   pkg_deepdive 需 package_name（包名，如 com.xxx.yyy）；owner_performance 需 owner_user_id（负责人）
      }
    请求头（鉴权与按权限取数）：
      - X-API-Key        : Tess 与 saas 之间的共享密钥（网关注入，生产设了 TESS_API_KEY 后必带）
      - X-Teensing-Token : 运营 SaaS access_token（**按人取数，最高优先级**）：
                            前端逐请求带当前登录运营的 token，Tess 原样转发给 saas_v3.0 数据接口，
                            按该运营 RBAC/数据权限返回数据 —— 各运营各看各的。
      - X-Platform-Id    : 平台标识；未带运营 token 时按它从 tess_platforms 取该平台的 token。
      - X-Operator-Id    : 可选，运营身份，仅用于审计归因（回显到 context_summary.operator_id）
    返回：
      {
        "analysis_type": str,
        "report": "Markdown 简报（含 📊/💡/🚀 三段）",
        "context_summary": { "analysis_type", "date_or_month", "errors",
                             "operator_id", "token_mode" }
            # token_mode: "user"=按运营个人 token 取数; "system"=全局兜底
      }
    """
    _require_ai_entitlement(request)  # 付费授权闸门：到期/停用 -> 403
    analysis_type = (payload or {}).get("analysis_type")
    if analysis_type not in SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的 analysis_type={analysis_type!r}（支持 {', '.join(SUPPORTED)}）",
        )
    params = (payload or {}).get("params") or {}
    for _k in ("campaign_id", "advertiser_id", "publisher_id"):
        if (payload or {}).get(_k) is not None:
            params[_k] = (payload or {}).get(_k)
    try:
        connector = get_data_connector()
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=503, detail=f"Tess 未配置 Teensing 数据源：{e}")
    platform_id = _platform_id(request)
    operator = _operator_id(request) if request else "anonymous"
    effective_token, token_mode = _resolve_access_token(request, platform_id)
    # LLM key 按平台隔离：平台 llm_api_key > 全局 TESS_LLM_API_KEY
    llm = _get_llm_client(platform_id)
    # 按访问者权限取数（核心）：
    #   优先用运营个人 token（X-Teensing-Token，按人 RBAC，各看各的）；
    #   未带则按 X-Platform-Id 从 tess_platforms 取该平台的 token；
    #   再缺失时回退到全局 TESS_SYSTEM_TOKEN（写死后端配置，前端不接触）。
    # 生产（真实连接器）下没有任何 token 则无法按权限取数 -> 400
    if isinstance(connector, TeensingDataConnector) and not effective_token:
        raise HTTPException(
            status_code=400,
            detail="生产数据接入需取数凭据：运营 X-Teensing-Token、或在后端配置 TESS_SYSTEM_TOKEN",
        )
    try:
        result = process_data_analysis_query(
            analysis_type, connector, llm,
            token=effective_token, params=params,
            operator_id=operator, token_mode=token_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # 数据 API / LLM 异常都不应泄露堆栈
        raise HTTPException(
            status_code=502, detail=f"数据分析执行失败：{type(e).__name__}: {e}"
        )
    return result


@app.post("/tess/ask")
def post_ask(payload: dict, request: Request) -> dict:
    """Tess AI Assistant 自然语言问答接口。

    请求体：
      {
        "question": "自然语言问题（如：昨天营收为什么跌了？哪些 Campaign 最赚钱？）",
        "chat_id": "可选，多轮会话 ID；首次由前端生成并原样带回即可开启多轮指代消解；留空则单轮",
        "analysis_type": "campaign_detail" | ...   # 可选：显式深度下钻类型（前端胶囊透传，支持 14 种含 cross_dimension）
        "params": { "report_month": "2026-08" }  # 可选：随 analysis_type 透传（如财务对账月份）
        # 实体下钻可选参数（也可放在 params 里）：campaign_id / advertiser_id / publisher_id
      }
    请求头（鉴权与按权限取数，同 /tess/analytics）：
      - X-API-Key        : Tess<->saas 共享密钥（网关注入，生产设了 TESS_API_KEY 后必带）
      - X-Teensing-Token : 运营 SaaS access_token（按人取数，最高优先级；Tess 原样转发给 saas_v3.0，
                           按该运营 RBAC 返回数据，各运营各看各的）
      - X-Platform-Id    : 平台标识（落库打标 / 报表隔离 / 按平台选 llm_api_key，与取数无关）；
                           未带运营 token 时回退全局 TESS_SYSTEM_TOKEN（后端配置，前端不接触）；
                           生产连接器下两者皆无 -> 400
      - X-Operator-Id    : 可选，审计归因
    深度下钻说明：
      - 路由优先级：① 显式 analysis_type（前端胶囊透传）> ② 问题正则识别实体 id（如
        "5845554camp"/"ctit" -> campaign_detail，自动抽取 campaign_id；"广告主 1000734" -> advertiser_deepdive）
        > ③ 关键词映射到深度类型 > ④ 都不命中退回浅层全局上下文；
      - 传了非法 analysis_type -> 400。
    返回：
      {
        "answer": "Markdown 回答",
        "result": "<同 answer>",     # 兼容调用方 .answer/.result/.data 取值
        "data":   "<同 answer>",
        "context_summary": {
          "errors", "operator_id", "token_mode",
          "analysis_type",   # 仅深度下钻时存在：实际使用的分析类型
          "route_source",    # 仅深度下钻时存在："explicit"(前端透传) | "entity"(问题正则识别 id) | "inferred"(后端关键词)
          "date_or_month"    # 仅深度下钻时存在：日期/月份/时间范围
        }
      }
    """
    _require_ai_entitlement(request)  # 付费授权闸门：到期/停用 -> 403
    question = (payload or {}).get("question")
    if not question or not str(question).strip():
        raise HTTPException(status_code=400, detail="缺少 question 字段或为空")
    # 可选：深度下钻 analysis_type（前端胶囊显式透传）
    analysis_type = (payload or {}).get("analysis_type")
    if analysis_type is not None and analysis_type not in SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的 analysis_type={analysis_type!r}（支持 {', '.join(SUPPORTED)}）",
        )
    params = dict((payload or {}).get("params") or {})
    for _k in ("campaign_id", "advertiser_id", "publisher_id"):
        if (payload or {}).get(_k) is not None:
            params[_k] = (payload or {}).get(_k)
    # —— 多轮会话：chat_id 存在则加载历史上下文并回退上一轮实体 ——
    chat_id = (payload or {}).get("chat_id")
    history_text, history_entities = (None, None)
    if chat_id:
        history_text, history_entities = load_history(chat_id)
    try:
        connector = get_data_connector()
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=503, detail=f"Tess 未配置 Teensing 数据源：{e}")
    platform_id = _platform_id(request)
    operator = _operator_id(request) if request else "anonymous"
    effective_token, token_mode = _resolve_access_token(request, platform_id)
    # LLM key 按平台隔离：平台 llm_api_key > 全局 TESS_LLM_API_KEY
    llm = _get_llm_client(platform_id)
    if isinstance(connector, TeensingDataConnector) and not effective_token:
        raise HTTPException(
            status_code=400,
            detail="生产数据接入需取数凭据：运营 X-Teensing-Token、或在后端配置 TESS_SYSTEM_TOKEN",
        )
    try:
        result = process_question(
            question, connector, llm,
            token=effective_token, operator_id=operator, token_mode=token_mode,
            analysis_type=analysis_type, params=params,
            history=history_text, history_entities=history_entities,
        )
    except Exception as e:  # 数据 API / LLM 异常都不应泄露堆栈
        raise HTTPException(
            status_code=502, detail=f"问答执行失败：{type(e).__name__}: {e}"
        )
    # —— 多轮会话落库：把本轮问答写回（chat_id 为空则单轮，不写）——
    cs = result.get("context_summary", {})
    if chat_id:
        try:
            _ents = extract_entities(question, params)
            _ents = resolve_entities(_ents, connector, effective_token)
        except Exception:
            _ents = {}
        record_turn(
            chat_id, operator, question, result.get("answer", ""),
            _ents,
            analysis_type=cs.get("analysis_type"),
            route_source=cs.get("route_source"),
            platform_id=platform_id or "default",
            usage=cs.get("llm_usage"),
        )
    # P6 审计：记录「谁问了什么 -> Tess 答了什么」
    AUDIT.log_query(
        operator_id=operator,
        endpoint="/tess/ask",
        question=question,
        answer=result.get("answer", ""),
        status="answered",
        confidence=1.0,
        meta={
            "token_mode": token_mode,
            "analysis_type": cs.get("analysis_type"),
            "route_source": cs.get("route_source"),
            "llm_usage": cs.get("llm_usage"),
        },
    )
    return result


@app.post("/tess/diagnose-from-source")
def diagnose_from_source(payload: dict = None, request: Request = None) -> dict:
    """P5 数据接入：从 Teensing 真实异常数据源拉取最近 N 个异常，逐个诊断。

    body: { "limit": 5 }  （默认 5，最多 50）
    鉴权：按权限取数（优先运营 X-Teensing-Token 按人取数，缺省回退全局 TESS_SYSTEM_TOKEN）。
          运营身份（X-Operator-Id）用于 P6 问答审计归因。
    拉取到的原始事件经 normalize_to_context 转成 PRD §4.1 Context 后送编排层；
    处置执行器不受影响（仍走 Mock / 服务端配置）。
    诊断失败时单条降级为 INCONCLUSIVE，不影响其余事件。
    """
    operator = _operator_id(request) if request else "anonymous"
    platform_id = _platform_id(request) if request else None
    effective_token, _mode = _resolve_access_token(request, platform_id)
    payload = payload or {}
    limit = max(1, min(int(payload.get("limit", 5)), 50))
    try:
        connector = _get_data_connector()
    except Exception as e:  # 接入层未配置（如 teensing 缺 base_url）
        raise HTTPException(status_code=503, detail=f"数据接入层初始化失败：{e}")
    # 生产模式必须有运营/平台/全局任一 token，否则无法取数
    if isinstance(connector, TeensingDataConnector) and not effective_token:
        raise HTTPException(
            status_code=400,
            detail="生产数据接入需取数凭据：运营 X-Teensing-Token、或在后端配置 TESS_SYSTEM_TOKEN",
        )
    raw_events = connector.fetch_recent_anomalies(limit, token=effective_token or None)
    llm = _get_llm_client(platform_id)  # 平台 llm_api_key > 全局 TESS_LLM_API_KEY
    results = []
    for raw in raw_events:
        ctx = normalize_to_context(raw)
        VAULT.ingest(ctx)
        policy = load_policy()
        event_id = (ctx.get("anomaly_metadata") or {}).get("event_id", "UNKNOWN")
        try:
            diag = run_diagnosis(ctx, llm, policy=policy)
        except HTTPException:
            raise
        except Exception as e:  # 单条异常不拖垮整批
            diag = {
                "status": "INCONCLUSIVE",
                "confidence": 0.0,
                "summary": f"Tess 诊断链路异常，已自动切入人工排查：{type(e).__name__}",
                "root_cause_analysis": {
                    "primary_factor": f"系统异常：{type(e).__name__}",
                    "causal_chain": ["编排链路异常", "转人工处理"],
                },
            }
        STORE.observe_diagnosis(event_id, diag.get("status", "UNKNOWN"), diag.get("confidence", 0.0))
        results.append({"event_id": event_id, "diagnosis": diag})
        # P6 审计：逐条记录「该运营问的某个异常 → Tess 回答」
        AUDIT.log_query(
            operator_id=operator,
            endpoint="/tess/diagnose-from-source",
            question=ctx,
            answer=diag,
            status=diag.get("status"),
            confidence=diag.get("confidence"),
            meta={"event_id": event_id, "source_limit": limit},
        )
    return {"count": len(results), "results": results}


@app.get("/tess/query-log")
def query_log(operator_id: str = None, limit: int = 100) -> dict:
    """P6 问答审计：返回最近的问答记录（可选按运营过滤）。

    受全局 X-API-Key 守卫（若生产已开启）。operator_id 对应调用方传入的 X-Operator-Id。
    """
    rows = AUDIT.recent(operator_id=operator_id, limit=limit)
    return {"count": len(rows), "logs": rows}


_SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _filter_by_min_severity(items: list, min_severity: str) -> list:
    """按最低严重度过滤（LOW < MEDIUM < HIGH）；min_severity 为空则不过滤。"""
    if not min_severity:
        return items
    floor = _SEVERITY_RANK.get(min_severity.upper())
    if floor is None:
        return items
    out = []
    for it in items:
        sev = (it.get("anomaly_metadata") or {}).get("severity")
        if sev and _SEVERITY_RANK.get(sev, 0) >= floor:
            out.append(it)
    return out


def _extract_revenue(meta: dict) -> float | None:
    """从 anomaly_metadata 抽取用于 min_revenue 过滤的营收数值。

    优先级：meta.revenue（直接营收字段）> target_metric=='Revenue' 时的 current_value。
    既无营收字段、也非 Revenue 指标 → 返回 None（视为未知）。
    """
    if not isinstance(meta, dict):
        return None
    r = meta.get("revenue")
    if isinstance(r, (int, float)):
        return float(r)
    if meta.get("target_metric") == "Revenue":
        cv = meta.get("current_value")
        if isinstance(cv, (int, float)):
            return float(cv)
    return None


def _filter_by_min_revenue(items: list, min_revenue: float | None) -> list:
    """按最低营收（USD）过滤；min_revenue 为空则不过滤。

    契合 'Rev>20 才显示' 的语义：只有明确营收且达标（>= min_revenue）的告警才展示；
    营收未知（无 revenue 字段且非 Revenue 指标）的记录在设定 min_revenue 时一律排除。
    """
    if min_revenue is None:
        return items
    try:
        floor = float(min_revenue)
    except (TypeError, ValueError):
        return items
    out = []
    for it in items:
        rev = _extract_revenue(it.get("anomaly_metadata") or {})
        if rev is not None and rev >= floor:
            out.append(it)
    return out


def _extract_campaign_publisher(item: dict) -> dict:
    """给告警记录补上顶层 campaign_id / publisher_id，方便 Teensing 直接消费。

    背景：真实 anomaly-warning 记录落库时只在 event_id 里藏了 campaign_id（纯数字，
    即上游 campaign_id），anomaly_metadata 没显式存 campaign_id/publisher_id；而
    diagnosis.primary_contributor_id 多为「发布商/版位名」而非 campaign，不能当 campaign_id。
    这里统一抽出干净字段：
      - campaign_id：anomaly_metadata.campaign_id → 纯数字 event_id（真实记录 event_id
        即 campaign_id）→ 否则 None。realtime-kpi 类 event_id（RT-HOUR-13）不会误判。
      - publisher_id：anomaly_metadata.publisher_id → 否则 None。
    """
    out = dict(item)
    meta = out.get("anomaly_metadata") or {}
    cid = meta.get("campaign_id")
    if cid is None:
        eid = str(out.get("event_id") or "")
        if eid.isdigit():                      # 真实 anomaly-warning：event_id 即 campaign_id
            cid = eid
        elif eid.startswith("AW-CAMP-") or eid.startswith("CAMP-"):
            m = re.search(r"(\d+)", eid)
            cid = m.group(1) if m else None
    pid = meta.get("publisher_id")
    if cid is not None and str(cid).isdigit():
        cid = int(cid)
    if pid is not None and str(pid).isdigit():
        pid = int(pid)
    out["campaign_id"] = cid
    out["publisher_id"] = pid
    return out


@app.get("/tess/alerts")
def get_alerts(limit: int = 50, source: str = None, min_severity: str = None,
               min_revenue: float = None, include_acked: bool = True,
               request: Request = None) -> dict:
    """P7 定时预警拉取接口：Teensing / SaaS 后端可轮询此接口获取每小时诊断结果。

    返回最近 limit 条预警（含 run_time / event_id / status / confidence / source / diagnosis / ack*）。
    source 过滤：?source=realtime-kpi 只看实时 KPI 异常；?source=anomaly-warning 只看异常预警。
    min_severity 过滤：?min_severity=MEDIUM 只看 >= MEDIUM 的告警（LOW/MEDIUM/HIGH）。
    min_revenue 过滤：?min_revenue=20 只看营收 >= 20 USD 的告警（贴合 'Rev>20 才显示'；
        营收未知的记录在设定 min_revenue 时一律排除）。
    platform 过滤：?platform=facemoji 或请求头 X-Platform-Id，只看该平台告警（分平台隔离）。
    include_acked：默认 True（含已确认项）；置 false 则只返回「运营尚未确认」的告警。
    受全局 X-API-Key 守卫（若生产已开启）。共享 token 模式：全局可读，不按人过滤。
    """
    platform = _platform_id(request) if request else None
    rows = ALERTS.recent(limit=limit, source=source or None, include_acked=include_acked,
                         platform=platform)
    rows = _filter_by_min_severity(rows, min_severity)
    rows = _filter_by_min_revenue(rows, min_revenue)
    rows = [_extract_campaign_publisher(r) for r in rows]
    return {"count": len(rows), "alerts": rows}


@app.get("/tess/realtime-kpi/alerts")
def get_realtime_kpi_alerts(limit: int = 50, min_severity: str = None,
                            min_revenue: float = None, since_as_of: str = None,
                            include_acked: bool = False,
                            request: Request = None) -> dict:
    """Teensing 专用拉取接口：返回最近一轮对 realtime-kpi 的诊断结果批次。

    与通用 /tess/alerts 的区别：只针对 realtime-kpi 来源，且返回「最近一次整批」
    （按 run_time 聚批），Teensing 轮询拿到的就是上一轮全部结果，不会跨批次错乱。

    返回形状：
    {
      "as_of": "<批次时间 run_time>",        # Teensing 据此去重：相同 as_of 即同批
      "generated_at": "<响应生成时间>",
      "count": N,
      "items": [ { id, run_time, event_id, status, confidence, source, diagnosis,
                   anomaly_metadata: { severity, current_value, benchmark_value, ... },
                   acked_at, resolution, acked_by, ack_note }, ... ]
    }

    增量游标：?since_as_of=2026-07-30 13:00:00 只返回比该批次更新的所有告警
    （可能跨多批），Teensing 用上次的 as_of 传入即可只拿新增，省流量且不重复。
    默认不传 since_as_of 时，行为等同「返回最近一批整批」。

    min_severity 过滤：?min_severity=MEDIUM 只返回 >= MEDIUM 的告警，
    可避免大量 LOW（微跌）刷屏。

    min_revenue 过滤：?min_revenue=20 只看营收 >= 20 USD 的告警
    （营收未知的记录在设定 min_revenue 时一律排除）。

    include_acked：默认 False（只返回运营尚未确认的告警，已处理项不再刷屏）；
    传 ?include_acked=true 可连已确认项一起取回（如做历史/审计视图）。

    鉴权：受全局 X-API-Key 守卫（生产设 TESS_API_KEY 后，Teensing 请求头带
    X-API-Key: <共享密钥> 即可）。共享 token 模式：全局可读，不按人过滤。
    platform 过滤：?platform=facemoji 或请求头 X-Platform-Id，只看该平台告警（分平台隔离）。
    """
    platform = _platform_id(request) if request else None
    if since_as_of:
        items = ALERTS.query_since(since_as_of, source="realtime-kpi", limit=limit,
                                   include_acked=include_acked, platform=platform)
        as_of = items[-1]["run_time"] if items else None
    else:
        batch = ALERTS.latest_batch(source="realtime-kpi", limit=limit, include_acked=include_acked,
                                    platform=platform)
        items = batch["alerts"]
        as_of = batch["run_time"]

    # 注：演示记录（RT-HOUR-*）原先会被「按 event_id 强制置顶合并」始终显示，
    # 现已移除该逻辑 —— 本接口只返回真实 cron 批次结果，不再混入任何 demo。
    # 若曾灌过 demo，可用 POST /tess/dev/clear-demo 物理清库。

    items = _filter_by_min_severity(items, min_severity)
    items = _filter_by_min_revenue(items, min_revenue)
    items = [_extract_campaign_publisher(it) for it in items]
    return {
        "as_of": as_of,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "items": items,
    }


@app.post("/tess/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, payload: dict = None) -> dict:
    """运营确认/处理回写：Teensing 在运营查看 / 解决 / 确认正常波动后调用，标记该告警已处理。

    body: {
      "resolution": "acknowledged" | "resolved" | "false_positive",
      "acked_by":   "alice",          # 可选，处理人（运营身份）
      "note":       "已重启采集链路"   # 可选，备注
    }

    resolution 含义：
      - acknowledged  : 已查看/知晓（运营已读）
      - resolved      : 已解决（运营已处理线上问题）
      - false_positive: 误报 / 正常流量波动（运营确认无异常）

    标记后，默认拉取（include_acked=false）不再返回该告警，避免已处理项刷屏；
    拉取时传 include_acked=true 仍可查回（用于历史/审计）。

    鉴权：受全局 X-API-Key 守卫保护（生产设 TESS_API_KEY 后必须带 X-API-Key）。
    """
    payload = payload or {}
    resolution = payload.get("resolution")
    if resolution not in ("acknowledged", "resolved", "false_positive"):
        raise HTTPException(
            status_code=422,
            detail="resolution 必须是 acknowledged | resolved | false_positive 之一",
        )
    ok = ALERTS.ack(alert_id, resolution, payload.get("acked_by"), payload.get("note"))
    if not ok:
        raise HTTPException(status_code=404, detail=f"未找到告警 {alert_id}")
    return {
        "ok": True,
        "id": alert_id,
        "resolution": resolution,
        "acked_by": payload.get("acked_by"),
        "acked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.post("/tess/dev/seed-demo")
def dev_seed_demo(payload: dict = None) -> dict:
    """开发/演示专用：向预警库灌入一批演示数据，便于前端联调无需等待真实异常。

    ⚠️ 安全开关：仅当环境变量 TESS_DEV_SEED=1/true 时启用；生产默认关闭（返回 403），
       避免误把演示数据写进生产库或被人用 API Key 刷库。
    鉴权：仍受全局 X-API-Key 守卫（生产设了 TESS_API_KEY 就必须带）。

    body: { "clear": false }   默认追加；clear=true 时先清空 alerts 表再灌（演示刷新）。
    返回：{ ok, written, run_time }
    """
    if os.getenv("TESS_DEV_SEED", "0").lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(
            status_code=403, detail="dev seed disabled (set TESS_DEV_SEED=1 to enable)"
        )
    payload = payload or {}
    from .dev_seed import build_demo_results
    results = build_demo_results()
    if payload.get("clear"):
        with ALERTS.Session() as s:
            s.execute(text("DELETE FROM alerts"))
            s.commit()
        print("🧹 dev seed: 已清空 alerts 表")
    run_time = time.strftime("%Y-%m-%d %H:%M:%S")
    written = ALERTS.save_batch(results, run_time=run_time)
    return {"ok": True, "written": written, "run_time": run_time}


@app.post("/tess/dev/clear-demo")
def dev_clear_demo(payload: dict = None) -> dict:
    """开发/演示专用：从预警库物理删除演示数据（seed-demo 的逆操作）。

    ⚠️ 安全开关：同 seed-demo，仅当 TESS_DEV_SEED=1/true 时启用；否则 403。
    鉴权：仍受全局 X-API-Key 守卫。

    body: { "source": "realtime-kpi" }   默认只清 realtime-kpi 的 demo（RT-HOUR-*），
                                       即 /tess/realtime-kpi/alerts 接口掺入的测试数据；
          { "source": "anomaly-warning" } 清 anomaly-warning 的 demo（AW-CAMP-*）；
          { "all": true }                 清全部 demo（两个 source 都清）。
    返回：{ ok, deleted, sources }
    """
    if os.getenv("TESS_DEV_SEED", "0").lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(
            status_code=403, detail="dev clear disabled (set TESS_DEV_SEED=1 to enable)"
        )
    payload = payload or {}
    if payload.get("all"):
        sources = [None]
    else:
        src = payload.get("source")  # None -> 仅 realtime-kpi（本接口默认语义）
        sources = [src or "realtime-kpi"]
    total = 0
    done = []
    for s in sources:
        n = ALERTS.delete_by_event_ids(DEMO_EVENT_IDS, source=s)
        total += n
        done.append({"source": s or "all", "deleted": n})
    return {"ok": True, "deleted": total, "sources": done}


@app.post("/tess/cron/run")
def cron_run(payload: dict = None, request: Request = None) -> dict:
    """P7 手动触发一次定时诊断（便于立即验证，不必等下一个整点）。

    body: { "limit": 20, "platform": "facemoji" }
      - 不传 platform：遍历所有启用平台各跑一遍（每平台告警打对应 platform_id）。
      - 传 platform：只跑该平台（便于单独对某平台即时验证）。
    结果同时写入预警库（GET /tess/alerts 可拉取，按 ?platform= 过滤）。
    """
    payload = payload or {}
    limit = int(payload.get("limit", os.getenv("TESS_SCHEDULE_LIMIT", "20")))
    platform = payload.get("platform") or (_platform_id(request) if request else None)
    results = run_scheduled_diagnosis(limit, platform_id=platform)
    return {"count": len(results), "platform": platform or "all", "results": results}


# —— AI 付费授权状态查询（客户端启动时调用，判断「AI 对话框」是否可用）——
@app.get("/tess/entitlement")
def get_entitlement(request: Request) -> dict:
    """查询当前平台的 AI 问答授权状态（公开只读，受全局 X-API-Key 守卫）。

    平台标识来源：请求头 X-Platform-Id，或查询参数 ?platform=。
    返回：
      {
        "platform_id": "Melodong",
        "ai_enabled": true,          # 是否可用（未到期 + 平台启用）
        "expired": false,            # 是否已过期
        "expires_at": "2026-09-30",  # 到期时间（null = 永久有效）
        "days_left": 18,             # 剩余天数（永久有效时为 null）
        "reason": "ok"               # ok | no_expiry | expired | disabled
                                     # | unregistered | no_platform
      }
    前端用法：启动时拉一次；`ai_enabled === false` 就把 AI 入口隐藏或显示
    「已到期，请联系续费」。**到期只影响新建提问**，历史会话/导出仍可访问。
    缓存建议：客户端不要长缓存（续费后要尽快生效），建议 ≤60s 或每次进页面重拉。
    """
    platform_id = _platform_id(request)
    try:
        return get_platform_registry().entitlement(platform_id)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"授权信息读取失败：{type(e).__name__}: {e}")


# —— P9 平台管理接口（独立管理密钥 X-Admin-Key 守卫）——
@app.get("/tess/admin/platforms")
def admin_list_platforms(request: Request) -> dict:
    """列出全部平台凭证（仅管理端用，受 X-Admin-Key 守卫）。

    返回：{ count, platforms: [ {id, name, token, llm_api_key, base_url, is_active,
            expires_at, created_at, updated_at}, ... ] }
    注意：llm_api_key 原样返回（管理端需要查看/复制），但仅管理密钥可见，
    普通调用方拿不到。token 为历史遗留字段（取数平台 token 已废弃，不再用于鉴权）。
    expires_at 为 AI 对话框付费授权到期时间（null = 永久有效）。
    """
    _require_admin(request)
    rows = get_platform_registry().list()
    return {"count": len(rows), "platforms": rows}


@app.post("/tess/admin/platforms")
def admin_create_platform(payload: dict, request: Request) -> dict:
    """新增一个平台（受 X-Admin-Key 守卫）。

    body: { "id": "melodong", "name": "Melodong",
            "llm_api_key": "<该平台专用 DeepSeek key，可选>", "is_active": true,
            "expires_at": "2026-09-30" }   # 可选：AI 授权到期时间，不填 = 永久有效
      - id：稳定字符串主键，前端在 X-Platform-Id 携带；必填、不可重复。
      - llm_api_key：可选，该平台专用 LLM（DeepSeek）API key —— Tess 调 LLM 时优先用它，
        用量/账单按平台区分；为空则回退全局 TESS_LLM_API_KEY。
      - expires_at：可选，AI 对话框付费授权到期时间。支持 "YYYY-MM-DD"（当天 23:59:59
        UTC 结束）或带时间的 ISO 串；留空/不填 = 永久有效。
      - token / base_url：历史遗留字段（取数平台 token 已废弃，saas_v3.0 按运营个人
        token 鉴权），可不填；仅为兼容旧客户端保留。
    """
    _require_admin(request)
    payload = payload or {}
    pid = payload.get("id")
    if not pid:
        raise HTTPException(status_code=422, detail="id 为必填")
    try:
        row = get_platform_registry().create(
            platform_id=pid, name=payload.get("name", ""),
            token=payload.get("token") or "",
            llm_api_key=payload.get("llm_api_key"),
            base_url=payload.get("base_url"), is_active=bool(payload.get("is_active", True)),
            expires_at=payload.get("expires_at"),
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "platform": row}


@app.put("/tess/admin/platforms/{platform_id}")
def admin_update_platform(platform_id: str, payload: dict, request: Request) -> dict:
    """更新平台（受 X-Admin-Key 守卫）。可改 name / llm_api_key / is_active / expires_at
    （token / base_url 为历史遗留字段，保留可改仅为兼容）。

    body（部分字段即可）：{ "llm_api_key": "<新DeepSeek key>", "is_active": false,
                            "expires_at": "2026-12-31" }
    expires_at 支持显式清空（传 null / ""）：传了但为空 = 取消到期（永久有效），
    这与其它字段「不传即不改」的语义不同。
    """
    _require_admin(request)
    payload = payload or {}
    row = get_platform_registry().update(platform_id, **payload)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到平台 {platform_id}")
    return {"ok": True, "platform": row}


@app.delete("/tess/admin/platforms/{platform_id}")
def admin_delete_platform(platform_id: str, request: Request) -> dict:
    """删除平台（受 X-Admin-Key 守卫）。注意：已落库的 chat_sessions / alerts 的
    platform_id 不会因此被改（历史记录保留原归属），仅停止该平台后续参与定时诊断。
    """
    _require_admin(request)
    ok = get_platform_registry().delete(platform_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"未找到平台 {platform_id}")
    return {"ok": True, "deleted": platform_id}


@app.post("/tess/gaid/resolve")
def gaid_resolve(payload: dict = None) -> dict:
    """方案 C 的内部 join：按哈希 GAID 还原原始 GAID 返给最终用户。

    body: { "hashed": "<HMAC-SHA256 哈希值>" }
    仅对 Tess 已 ingest 过的哈希有效；未知哈希返回 404（绝不编造原始值）。
    """
    payload = payload or {}
    h = payload.get("hashed")
    if not h:
        raise HTTPException(status_code=400, detail="缺少 hashed")
    original = VAULT.resolve(h)
    if original is None:
        raise HTTPException(status_code=404, detail="未知哈希 GAID（未在本服务 ingest 过）")
    return {"hashed": h, "original": original}


@app.delete("/tess/gaid/{hashed}")
def gaid_delete(hashed: str) -> dict:
    """被遗忘权：删除某哈希对应的 `哈希↔原始` 映射。"""
    deleted = VAULT.delete(hashed)
    return {"deleted": deleted}


@app.get("/tess/thresholds")
def get_thresholds() -> dict:
    """返回当前生效的置信度切点策略（默认初版 / 学习后）。"""
    return load_policy().to_dict()

@app.post("/tess/thresholds/reset")
def reset_thresholds() -> dict:
    """恢复默认初版阈值（删除学习文件）。"""
    reset_policy()
    return {"ok": True, "policy": default_policy().to_dict()}

@app.post("/tess/feedback/self-heal")
def self_heal(payload: dict = None) -> dict:
    """反馈自愈：依据历史投票，产出（或应用）阈值提案。

    body: { "apply": true|false }  默认 dry-run（只提案不落盘）。
    apply=true 且提案被采纳时才写盘，Gatekeeper 下次加载即生效。
    """
    payload = payload or {}
    apply = bool(payload.get("apply", False))
    records = STORE.labeled_feedback()
    proposal = propose_thresholds(records)
    if apply:
        policy = apply_proposal(proposal)
        if policy is not None:
            return {"ok": True, "applied": policy.to_dict(), "proposal": proposal.to_dict()}
        return {
            "ok": False,
            "applied": None,
            "proposal": proposal.to_dict(),
            "note": "提案未采纳，未改动阈值。",
        }
    return {"ok": True, "applied": None, "proposal": proposal.to_dict()}


@app.post("/tess/joint-diagnose")
def joint_diagnose(payload: dict = None) -> dict:
    """L2-2 联合归因：对一批疑似同源异常事件，产出共同根因诊断。

    body: { "events": [ 单事件 Input, ... ] }
    后端先确定性聚合（共性维度 / 聚合损耗 / 最高严重度），再让 LLM 产出联合叙事；
    LLM 输出仍过 Gatekeeper 死锁校验（severity/损耗锁死、幻觉维度降级）。
    返回 { "diagnosis": {...}, "correlation": {...} }。
    """
    payload = payload or {}
    events = payload.get("events", [])
    llm = _get_llm_client()
    policy = load_policy()
    try:
        result = run_joint_diagnosis(events, llm, policy=policy)
    except HTTPException:
        raise
    except Exception as e:  # 编排层任何意外都不应把堆栈泄露给前端
        result = {
            "diagnosis": {
                "status": "INCONCLUSIVE",
                "confidence": 0.0,
                "summary": "Tess 联合归因链路异常，已自动切入人工排查。",
                "root_cause_analysis": {
                    "primary_factor": f"系统异常：{type(e).__name__}",
                    "causal_chain": ["联合归因编排异常", "转人工处理"],
                },
            },
            "correlation": {
                "event_count": len(events),
                "candidate_dimensions": {},
                "aggregated_loss_per_hour_usd": 0.0,
                "max_severity": "UNKNOWN",
                "event_ids": [e.get("anomaly_metadata", {}).get("event_id") for e in events],
            },
        }
    corr = result.get("correlation", {})
    STORE.observe_joint(
        corr.get("event_ids", []),
        result["diagnosis"]["status"],
        result["diagnosis"]["confidence"],
    )
    return result


@app.post("/tess/feedback")
def feedback(payload: dict) -> dict:
    """抽屉底部 👍/👎 的回传入口。

    payload: { event_id, vote(accurate|inaccurate), tess_status, confidence,
                corrected_root_cause?, corrected_contributor_id? }
    """
    try:
        rec = STORE.record_feedback(
            event_id=payload["event_id"],
            vote=payload["vote"],
            tess_status=payload["tess_status"],
            confidence=float(payload["confidence"]),
            corrected_root_cause=payload.get("corrected_root_cause"),
            corrected_contributor_id=payload.get("corrected_contributor_id"),
        )
        return {"ok": True, "recorded": rec}
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"缺少字段：{e}")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/tess/feedback/metrics")
def feedback_metrics() -> dict:
    """返回反馈质量度量（降级率 / 高置信误判率 / 覆盖率 / 阈值建议）。"""
    return STORE.metrics()

# ---------------------------------------------------------------------------
# L2-3 半自动处置（带审批流）
# ---------------------------------------------------------------------------

def _derive_remediation_ref(diagnosis: dict, context: dict) -> str:
    """从诊断/上下文抽取一个人类可读的处置单标识（仅用于展示）。"""
    if isinstance(diagnosis, dict):
        jf = diagnosis.get("joint_primary_factor")
        if jf:
            return f"joint:{jf}"
        pc = diagnosis.get("primary_contributor_id")
        if pc:
            return f"diag:{pc}"
    return "unknown"

@app.post("/tess/remediation/propose")
def remediation_propose(payload: dict = None) -> dict:
    """L2-3 处置提案：依据已确诊诊断，让 LLM 建议一项处置动作。

    body: { "diagnosis": {...Gatekeeper 归一化诊断...},
            "context":    {...原始上下文，用于抽取候选目标/严重度...} }
    仅当 diagnosis.status 为 DIAGNOSED / DIAGNOSED_SUSPECT 才提案；
    提案过 Gatekeeper 校验后登记为 PENDING 处置单，待人工审批。
    返回 { "accepted": bool, "reason", "remediation": {...}|null }。
    """
    payload = payload or {}
    diagnosis = payload.get("diagnosis")
    context = payload.get("context", {}) or {}
    if not isinstance(diagnosis, dict):
        raise HTTPException(status_code=422, detail="缺少合法 diagnosis")
    llm = _get_llm_client()
    result = propose_remediation(diagnosis, context, llm)
    if not result["accepted"]:
        return {
            "accepted": False,
            "reason": result["reason"],
            "remediation": None,
            "diagnosis_status": result.get("diagnosis_status"),
        }
    ref = _derive_remediation_ref(diagnosis, context)
    rec = REMEDIATION_STORE.create(ref, result["proposal"], result.get("severity", "UNKNOWN"))
    STORE.observe_remediation(rec["state"])
    return {
        "accepted": True,
        "reason": result["reason"],
        "remediation": rec,
        "diagnosis_status": result.get("diagnosis_status"),
    }

@app.get("/tess/remediation")
def remediation_list(state: str = None) -> dict:
    """列出处置单（可选 ?state=PENDING|APPROVED|...）。"""
    items = REMEDIATION_STORE.list(state)
    return {"items": items, "count": len(items)}

@app.get("/tess/remediation/{rid}")
def remediation_get(rid: str) -> dict:
    rec = REMEDIATION_STORE.get(rid)
    if not rec:
        raise HTTPException(status_code=404, detail=f"未找到处置单 {rid}")
    return rec

@app.post("/tess/remediation/{rid}/approve")
def remediation_approve(rid: str, payload: dict = None) -> dict:
    """人工审批通过：PENDING -> APPROVED。

    body: { "approved_by": "alice", "second_approved_by"?: "bob" }
    CRITICAL 级处置强制双人审批（必须提供 second_approved_by）。
    """
    payload = payload or {}
    by = payload.get("approved_by")
    if not by:
        raise HTTPException(status_code=422, detail="缺少 approved_by")
    rec = REMEDIATION_STORE.get(rid)
    if not rec:
        raise HTTPException(status_code=404, detail=f"未找到处置单 {rid}")
    # CRITICAL 双人力规则（纵深防御：高危动作需两人确认）
    if rec.get("severity") == "CRITICAL":
        second = payload.get("second_approved_by")
        if not second:
            raise HTTPException(
                status_code=422, detail="CRITICAL 级处置需双人审批（请提供 second_approved_by）"
            )
        rec = REMEDIATION_STORE.approve(rid, by, second)
    else:
        rec = REMEDIATION_STORE.approve(rid, by)
    STORE.observe_remediation(rec["state"])
    return rec

@app.post("/tess/remediation/{rid}/reject")
def remediation_reject(rid: str, payload: dict = None) -> dict:
    """人工驳回：PENDING -> REJECTED。

    body: { "rejected_by": "alice", "reason"?: "风险可接受" }
    """
    payload = payload or {}
    by = payload.get("rejected_by")
    if not by:
        raise HTTPException(status_code=422, detail="缺少 rejected_by")
    rec = REMEDIATION_STORE.get(rid)
    if not rec:
        raise HTTPException(status_code=404, detail=f"未找到处置单 {rid}")
    rec = REMEDIATION_STORE.reject(rid, by, payload.get("reason"))
    STORE.observe_remediation(rec["state"])
    return rec

@app.post("/tess/remediation/{rid}/execute")
def remediation_execute(rid: str) -> dict:
    """执行已审批的处置：APPROVED -> EXECUTED / FAILED。

    执行器为服务端配置（默认 Mock），绝不接受客户端指定——LLM 也永不接触。
    未处于 APPROVED 的处置单一律拒绝执行。
    """
    rec = REMEDIATION_STORE.get(rid)
    if not rec:
        raise HTTPException(status_code=404, detail=f"未找到处置单 {rid}")
    try:
        rec = REMEDIATION_STORE.execute(rid, REMEDIATION_EXECUTOR)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    STORE.observe_remediation(rec["state"])
    return rec


# ---------------------------------------------------------------------------
# 混合架构：LLM Tool Calling 宽工具（强约束，内部一律走确定性引擎）
# ---------------------------------------------------------------------------

@app.get("/tess/tools")
def list_tools() -> dict:
    """返回供 LLM 消费的 tool schema 清单（tess_analyze / tess_ask / tess_fetch_warning）。

    LLM 的 tool_call 只产出结构化参数块；真正端点选择与参数矫正由
    POST /tess/tool 在服务端完成，杜绝裸暴露细粒度 API。
    """
    return {"tools": load_tool_schemas()}


@app.get("/tess/chat/{chat_id}")
def get_chat(chat_id: str) -> dict:
    """读取某会话的多轮历史（受全局 X-API-Key 守卫，若生产已开启）。"""
    msgs = get_chat_store().get_messages(chat_id)
    return {"chat_id": chat_id, "messages": msgs, "count": len(msgs)}


@app.delete("/tess/chat/{chat_id}")
def delete_chat(chat_id: str) -> dict:
    """清空某会话的历史（受全局 X-API-Key 守卫，若生产已开启）。"""
    deleted = get_chat_store().delete(chat_id)
    return {"chat_id": chat_id, "deleted": deleted}


@app.get("/tess/chats")
def list_chats(request: Request, limit: int = 100) -> dict:
    """列出当前运营的历史会话（受 X-API-Key 守卫；按 X-Operator-Id 隔离）。

    返回 { sessions: [{chat_id, operator_id, title, message_count, created_at, updated_at}], count }。
    前端可据此渲染历史会话侧边栏，点击某条后调用 GET /tess/chat/{chat_id} 取完整消息恢复。
    """
    operator = _operator_id(request)
    platform = _platform_id(request)
    sessions = get_chat_store().list_sessions(operator_id=operator, platform_id=platform, limit=limit)
    return {"sessions": sessions, "count": len(sessions)}


@app.get("/tess/chats/export")
def export_chats(request: Request, format: str = "json"):
    """导出全量对话用于运营报表（受 X-API-Key 守卫；按 X-Operator-Id 隔离）。

    - format=json（默认）：返回 { count, rows:[{chat_id, operator_id, question, answer,
      ts, analysis_type, route_source, campaign_id, advertiser_id, publisher_id,
      package_name, owner_user_id, llm_prompt_tokens, llm_completion_tokens,
      llm_total_tokens}] }（llm_* 为该轮 LLM 用量，旧数据为 null）
    - format=csv：扁平 CSV（同名列）直接下载，可用 Excel / BI 打开
    """
    operator = _operator_id(request)
    platform = _platform_id(request)
    rows = get_chat_store().export_rows(operator_id=operator, platform_id=platform)
    if format == "csv":
        import csv
        import io

        buf = io.StringIO()
        fields = ["chat_id", "operator_id", "platform_id", "question", "answer", "ts",
                  "analysis_type", "route_source", "campaign_id", "advertiser_id",
                  "publisher_id", "package_name", "owner_user_id",
                  "llm_prompt_tokens", "llm_completion_tokens", "llm_total_tokens"]
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return Response(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=tess_chats.csv"},
        )
    return {"count": len(rows), "rows": rows}


@app.get("/tess/chats/stats")
def chat_stats(request: Request):
    """聚合运营分析指标（受 X-API-Key 守卫；按 X-Operator-Id 隔离）。

    返回：total_sessions / total_turns / top_questions / top_entities /
    per_operator / analysis_type_distribution / route_source_distribution /
    daily_buckets —— 前端可据此渲染「问题热点 / 实体热度 / 各运营活跃度」报表看板。
    """
    operator = _operator_id(request)
    platform = _platform_id(request)
    return get_chat_store().aggregate_stats(operator_id=operator, platform_id=platform)


@app.post("/tess/tool")
def call_tool(payload: dict, request: Request) -> dict:
    """LLM Tool Calling 宽工具统一入口：由 LLM 的 tool_call 触发。

    body: {
      "tool": "tess_analyze" | "tess_ask" | "tess_fetch_warning",
      "arguments": { ...工具参数... }      # LLM 产出的结构化参数块
    }
    鉴权 / 按平台取数同 /tess/analytics、/tess/ask（X-API-Key + X-Platform-Id）。
    后端依据 arguments 强约束路由到确定性引擎，不应出现静默错数或幻觉。
    """
    _require_ai_entitlement(request)  # 付费授权闸门：到期/停用 -> 403
    tool = (payload or {}).get("tool")
    if tool not in ("tess_analyze", "tess_ask", "tess_fetch_warning"):
        raise HTTPException(status_code=400, detail=f"不支持的 tool={tool!r}")
    try:
        return dispatch_tool(tool, (payload or {}).get("arguments") or {}, request)
    except HTTPException:
        raise
    except Exception as e:  # 数据 API / LLM 异常都不应泄露堆栈
        raise HTTPException(status_code=502, detail=f"tool 执行失败：{type(e).__name__}: {e}")


@app.get("/healthz")
def healthz() -> dict:
    """K8s/Docker 存活探针：不依赖 LLM，仅报告进程与配置状态。"""
    return {
        "status": "ok",
        "service": "tess-diagnose",
        "version": app.version,
        "llm_configured": bool(os.getenv("TESS_LLM_API_KEY")),
    }


# ---------------------------------------------------------------------------
# P7 定时预警调度器（进程内 asyncio 循环，间隔可配）
# ---------------------------------------------------------------------------

logger = logging.getLogger("tess_backend.app")


async def _scheduler_loop() -> None:
    """每小时（间隔可配）跑一轮 run_scheduled_diagnosis；单轮失败不影响下一轮。"""
    interval = int(os.getenv("TESS_SCHEDULE_INTERVAL", "3600"))
    limit = int(os.getenv("TESS_SCHEDULE_LIMIT", "20"))
    while True:
        try:
            await asyncio.to_thread(run_scheduled_diagnosis, limit)
        except Exception as e:  # 单轮异常（如 LLM 未配置、数据 API 不通）仅记录
            logger.exception("P7 定时诊断本轮失败: %s", e)
        await asyncio.sleep(interval)


@app.on_event("startup")
async def _startup_scheduler() -> None:
    if os.getenv("TESS_SCHEDULE_ENABLED", "false").lower() in ("1", "true", "yes", "on"):
        logger.info(
            "P7 定时预警调度已启用，间隔 %ss", os.getenv("TESS_SCHEDULE_INTERVAL", "3600")
        )
        asyncio.create_task(_scheduler_loop())


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("TESS_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
