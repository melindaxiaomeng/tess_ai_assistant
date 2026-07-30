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
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
)
from .gaid_vault import VAULT, RedactFilter
from .audit_log import QueryLogStore
from .alerts_store import AlertStore

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


def _get_llm_client() -> LLMClient:
    """依赖注入：生产读环境变量（默认 DeepSeek OpenAI 兼容端点）。

    只需填 TESS_LLM_API_KEY 即可跑；base_url / model 有合理默认值。
    """
    base_url = os.getenv("TESS_LLM_BASE_URL", "https://api.deepseek.com")
    api_key = os.getenv("TESS_LLM_API_KEY", "")
    model = os.getenv("TESS_LLM_MODEL", "deepseek-chat")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Tess LLM 未配置（请设置 TESS_LLM_API_KEY）",
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


def run_scheduled_diagnosis(limit: int = 20, connector=None, llm=None) -> list:
    """P7 定时预警：用共享服务 token 拉异常 → 诊断 → 存预警库。

    数据来源（两轮，统一进同一批预警）：
    (1) 异常预警 + 涨跌榜（/overview/ranking/*）→ 归一化 → 诊断（source="anomaly-warning"）
    (2) 实时 KPI 小时级曲线（/overview/realtime-kpi）→ 提取骤降异常点 → 诊断（source="realtime-kpi"）

    token：优先 TESS_SYSTEM_TOKEN，回退 TESS_DATA_API_KEY（共享服务 token，不按人过滤）。
    返回本轮诊断结果列表（同 /tess/diagnose-from-source 的 results 形状，meta 含 source 标签）。
    """
    connector = connector or _get_data_connector()
    llm = llm or _get_llm_client()
    token = os.getenv("TESS_SYSTEM_TOKEN") or None
    policy = load_policy()
    results: list = []

    # (1) 异常预警 + 涨跌榜
    raw_events = connector.fetch_recent_anomalies(limit, token=token)
    for raw in raw_events:
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
        logger.warning("realtime-kpi 拉取或分析失败，本轮跳过实时异常检测: %s", e)

    ALERTS.save_batch(results)
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


def _teensing_token(request: Request) -> str:
    """从请求头取运营 SaaS access_token（X-Teensing-Token），用于按权限拉数据；缺省空串。"""
    return request.headers.get("X-Teensing-Token", "") or ""


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


@app.post("/tess/diagnose-from-source")
def diagnose_from_source(payload: dict = None, request: Request = None) -> dict:
    """P5 数据接入：从 Teensing 真实异常数据源拉取最近 N 个异常，逐个诊断。

    body: { "limit": 5 }  （默认 5，最多 50）
    鉴权：生产模式需在前端请求头带运营 SaaS access_token（X-Teensing-Token），
          由 Tess 原样透传给 Teensing；Teensing 按该运营 RBAC/数据权限返回数据。
          运营身份（X-Operator-Id）用于 P6 问答审计归因。
    拉取到的原始事件经 normalize_to_context 转成 PRD §4.1 Context 后送编排层；
    处置执行器不受影响（仍走 Mock / 服务端配置）。
    诊断失败时单条降级为 INCONCLUSIVE，不影响其余事件。
    """
    operator = _operator_id(request) if request else "anonymous"
    token = _teensing_token(request) if request else ""
    payload = payload or {}
    limit = max(1, min(int(payload.get("limit", 5)), 50))
    try:
        connector = _get_data_connector()
    except Exception as e:  # 接入层未配置（如 teensing 缺 base_url）
        raise HTTPException(status_code=503, detail=f"数据接入层初始化失败：{e}")
    # 生产（teensing）模式必须带运营 token，否则无法按权限拉数据
    if isinstance(connector, TeensingDataConnector) and not token:
        raise HTTPException(
            status_code=400,
            detail="生产数据接入需在前端请求头携带 X-Teensing-Token（运营 SaaS access_token）",
        )
    raw_events = connector.fetch_recent_anomalies(limit, token=token or None)
    llm = _get_llm_client()
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


@app.get("/tess/alerts")
def get_alerts(limit: int = 50, source: str = None, min_severity: str = None,
               include_acked: bool = True) -> dict:
    """P7 定时预警拉取接口：Teensing / SaaS 后端可轮询此接口获取每小时诊断结果。

    返回最近 limit 条预警（含 run_time / event_id / status / confidence / source / diagnosis / ack*）。
    source 过滤：?source=realtime-kpi 只看实时 KPI 异常；?source=anomaly-warning 只看异常预警。
    min_severity 过滤：?min_severity=MEDIUM 只看 >= MEDIUM 的告警（LOW/MEDIUM/HIGH）。
    include_acked：默认 True（含已确认项）；置 false 则只返回「运营尚未确认」的告警。
    受全局 X-API-Key 守卫（若生产已开启）。共享 token 模式：全局可读，不按人过滤。
    """
    rows = ALERTS.recent(limit=limit, source=source or None, include_acked=include_acked)
    rows = _filter_by_min_severity(rows, min_severity)
    return {"count": len(rows), "alerts": rows}


@app.get("/tess/realtime-kpi/alerts")
def get_realtime_kpi_alerts(limit: int = 50, min_severity: str = None,
                            since_as_of: str = None, include_acked: bool = False) -> dict:
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

    include_acked：默认 False（只返回运营尚未确认的告警，已处理项不再刷屏）；
    传 ?include_acked=true 可连已确认项一起取回（如做历史/审计视图）。

    鉴权：受全局 X-API-Key 守卫（生产设 TESS_API_KEY 后，Teensing 请求头带
    X-API-Key: <共享密钥> 即可）。共享 token 模式：全局可读，不按人过滤。
    """
    if since_as_of:
        items = ALERTS.query_since(since_as_of, source="realtime-kpi", limit=limit,
                                   include_acked=include_acked)
        as_of = items[-1]["run_time"] if items else None
    else:
        batch = ALERTS.latest_batch(source="realtime-kpi", limit=limit, include_acked=include_acked)
        items = batch["alerts"]
        as_of = batch["run_time"]
    items = _filter_by_min_severity(items, min_severity)
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


@app.post("/tess/cron/run")
def cron_run(payload: dict = None) -> dict:
    """P7 手动触发一次定时诊断（便于立即验证，不必等下一个整点）。

    body: { "limit": 20 }
    结果同时写入预警库（GET /tess/alerts 可拉取）。
    """
    payload = payload or {}
    limit = int(payload.get("limit", os.getenv("TESS_SCHEDULE_LIMIT", "20")))
    results = run_scheduled_diagnosis(limit)
    return {"count": len(results), "results": results}


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
