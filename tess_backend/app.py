"""P4 · HTTP API —— 暴露 POST /tess/diagnose。

薄薄一层：只把请求体交给编排层 run_diagnosis，再把 Gatekeeper 归一化后的
安全结果返回前端。LLM 客户端通过依赖注入，便于测试时换成 Mock。

生产部署：
- 真实 LLM 用 HttpLLMClient(base_url, api_key, model)，api_key 从环境变量读取。
- uvicorn tess_backend.app:app --port 8080
"""

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

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
from .gaid_vault import VAULT, RedactFilter

app = FastAPI(title="Tess Diagnose API", version="2.3.0")

# L2-1 反馈闭环：模块级单例。设 TESS_FEEDBACK_PATH 可持久化 JSONL。
STORE = FeedbackStore(persist_path=os.getenv("TESS_FEEDBACK_PATH") or None)
# L2-3 半自动处置：模块级单例。设 TESS_REMEDIATION_PATH 可持久化 JSONL。
REMEDIATION_STORE = RemediationStore(persist_path=os.getenv("TESS_REMEDIATION_PATH") or None)
# 执行器默认 Mock；生产替换为 Teensing 平台真实 API 适配器。
REMEDIATION_EXECUTOR = MockRemediationExecutor()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产请收紧为 Teensing 前端域名
    allow_methods=["POST"],
    allow_headers=["*"],
)

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


@app.post("/tess/diagnose")
def diagnose(payload: dict) -> dict:
    """接收异常上下文 Input，返回 Gatekeeper 归一化后的归因结果。

    每次诊断都会登记进反馈_ledger（observe_diagnosis），用于算覆盖率 / 降级率。
    """
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
    return result

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


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("TESS_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
