#!/usr/bin/env python3
"""验证 /tess/analytics 六类数据分析场景。

两种运行模式：
  1) 模块直跑（默认，需 .env 配置真实 Teensing + LLM）：
       python verify_analytics.py
       python verify_analytics.py --no-llm          # 仅校验数据拉取+整形，不耗 token
       python verify_analytics.py --type scaling_capacity

  2) HTTP 模式（部署后验证线上端点，需服务器地址 + API Key）：
       python verify_analytics.py --http https://your-host --api-key <X-API-Key>
       # 按用户权限取数：透传终端用户 token
       python verify_analytics.py --http https://your-host --api-key <key> --user-token <运营access_token>

可选 --type 限定单个场景；--json 输出机器可读结果；--user-token 模拟线上 X-Teensing-Token 透传。
可选 --ask "问题" 测试 /tess/ask 自然语言问答端点（同样支持 --http/--api-key/--user-token/--no-llm）。

退出码：全部场景 0 errors 且（LLM 模式下）report 非空 => 0；否则 1。
"""
import argparse
import json
import os
import sys
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)


def _load_dotenv(path=os.path.join(PROJECT_ROOT, ".env")):
    """极简 .env 加载：仅注入尚未存在的环境变量（不覆盖已有 shell 环境）。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

ALL_TYPES = [
    "daily_summary",
    "scaling_opportunity",
    "finance_check",
    "account_overview",
    "publisher_deepdive",
    "scaling_capacity",
    "campaign_detail",
    "advertiser_deepdive",
    "traffic_policy_check",
    "kpi_compare",
]


class SimpleLLMClient:
    """极简 OpenAI 兼容 LLM 客户端（urllib 直连，避免依赖 tess_agent/jsonschema）。

    仅实现 analytics 所需接口：complete(system, user, json_mode=None) -> str。
    """

    def __init__(self, base_url, api_key, model):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def complete(self, system: str, user: str, json_mode=None):
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        return d["choices"][0]["message"]["content"]


def load_dotenv(path=os.path.join(PROJECT_ROOT, ".env")):
    """极简 .env 加载（避免依赖 python-dotenv）。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def build_params(campaign_id=None, advertiser_id=None, publisher_id=None):
    """从命令行传入的实体 id 组装 params（仅保留非空的）。"""
    p = {}
    if campaign_id is not None:
        p["campaign_id"] = campaign_id
    if advertiser_id is not None:
        p["advertiser_id"] = advertiser_id
    if publisher_id is not None:
        p["publisher_id"] = publisher_id
    return p or None


def run_module(types, use_llm, user_token=None, campaign_id=None, advertiser_id=None, publisher_id=None):
    from tess_backend.data_connector import get_data_connector
    from tess_backend.analytics import (
        fetch_bi_analysis_context,
        process_data_analysis_query,
    )

    connector = get_data_connector()
    # 默认用系统 token；传入 --user-token 则按该用户的权限取数（模拟线上透传）
    token = user_token or (os.getenv("TESS_SYSTEM_TOKEN") or None)
    params = build_params(campaign_id, advertiser_id, publisher_id)

    results = []
    for at in types:
        if use_llm:
            try:
                llm = SimpleLLMClient(
                    os.getenv("TESS_LLM_BASE_URL", "https://api.deepseek.com"),
                    os.getenv("TESS_LLM_API_KEY", ""),
                    os.getenv("TESS_LLM_MODEL", "deepseek-chat"),
                )
                res = process_data_analysis_query(at, connector, llm, token=token, params=params)
                report = res.get("report", "")
                ctx_err = res.get("context_summary", {}).get("errors", [])
                results.append({
                    "analysis_type": at,
                    "mode": "module+llm",
                    "data_errors": ctx_err,
                    "report_len": len(report),
                    "report_head": report[:200],
                    "ok": (not ctx_err) and bool(report.strip()),
                })
            except Exception as e:  # noqa: BLE001
                results.append({
                    "analysis_type": at, "mode": "module+llm",
                    "data_errors": [f"EXC: {type(e).__name__}: {e}"],
                    "report_len": 0, "report_head": "", "ok": False,
                })
        else:
            ctx = fetch_bi_analysis_context(connector, at, token=token, params=params)
            errs = ctx.get("errors", [])
            # 抽取每个场景的关键计数，便于一眼判断是否有数据
            keys = ("campaign_total", "advertiser_total", "top_advertisers_by_revenue",
                    "flagged_quality_issues", "scaling_room", "over_cap_waste",
                    "top_by_profit", "today_kpi", "rising_gainers", "falling_losers",
                    "scaling_candidates", "total_summary", "campaign_config",
                    "quality_timeseries", "kpi_trend", "ctit_etit", "profile",
                    "daily_kpi", "mapping_publisher_channels", "replace_channels", "blocks")
            summary = {k: _len_or_val(ctx.get(k)) for k in keys if k in ctx}
            results.append({
                "analysis_type": at, "mode": "module+data-only",
                "params": params, "data_errors": errs, "context_summary": summary,
                "ok": not errs,
            })
    return results


def _len_or_val(v):
    if isinstance(v, (list, dict, str)):
        return len(v)
    return v


def run_http(base_url, api_key, types, user_token=None):
    import urllib.request

    base = base_url.rstrip("/")
    results = []
    for at in types:
        payload = json.dumps({"analysis_type": at}).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-API-Key": api_key}
        # 按用户权限取数：透传终端用户的 Teensing access_token
        if user_token:
            headers["X-Teensing-Token"] = user_token
        req = urllib.request.Request(
            f"{base}/tess/analytics",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            report = d.get("report", "")
            errs = (d.get("context_summary") or {}).get("errors", [])
            results.append({
                "analysis_type": at, "mode": "http",
                "http_status": resp.status,
                "data_errors": errs,
                "report_len": len(report),
                "report_head": report[:200],
                "ok": (not errs) and bool(report.strip()),
            })
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            results.append({
                "analysis_type": at, "mode": "http", "http_status": e.code,
                "data_errors": [f"HTTP {e.code}: {body[:200]}"],
                "report_len": 0, "report_head": "", "ok": False,
            })
        except Exception as e:  # noqa: BLE001
            results.append({
                "analysis_type": at, "mode": "http",
                "data_errors": [f"EXC: {type(e).__name__}: {e}"],
                "report_len": 0, "report_head": "", "ok": False,
            })
    return results


def run_ask(question, use_llm, user_token=None, base_url=None, api_key=None,
            analysis_type=None, campaign_id=None, advertiser_id=None, publisher_id=None):
    """验证 /tess/ask 自然语言问答端点。

    - base_url+api_key 给定 -> HTTP 模式打线上端点（带可选 X-Teensing-Token）
    - 否则 -> 模块直跑（需 .env 真实 LLM；--no-llm 时仅校验数据拉取）
    """
    params = build_params(campaign_id, advertiser_id, publisher_id)

    if base_url and api_key:
        import urllib.request

        base = base_url.rstrip("/")
        body = {"question": question}
        if analysis_type:
            body["analysis_type"] = analysis_type
        # 顶层实体 id（app.py 会将顶层 id 合并进 params）
        if campaign_id is not None:
            body["campaign_id"] = campaign_id
        if advertiser_id is not None:
            body["advertiser_id"] = advertiser_id
        if publisher_id is not None:
            body["publisher_id"] = publisher_id
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-API-Key": api_key}
        if user_token:
            headers["X-Teensing-Token"] = user_token
        req = urllib.request.Request(f"{base}/tess/ask", data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            answer = d.get("answer", "")
            cs = d.get("context_summary") or {}
            errs = cs.get("errors", [])
            return [{
                "analysis_type": f"ask:{cs.get('analysis_type', 'shallow')}", "mode": "http",
                "http_status": resp.status, "route_source": cs.get("route_source"),
                "data_errors": errs, "answer_len": len(answer),
                "answer_head": answer[:200], "ok": (not errs) and bool(answer.strip()),
            }]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            return [{"analysis_type": "ask", "mode": "http", "http_status": e.code,
                     "data_errors": [f"HTTP {e.code}: {body[:200]}"], "answer_len": 0,
                     "answer_head": "", "ok": False}]
        except Exception as e:  # noqa: BLE001
            return [{"analysis_type": "ask", "mode": "http",
                     "data_errors": [f"EXC: {type(e).__name__}: {e}"], "answer_len": 0,
                     "answer_head": "", "ok": False}]

    # 模块直跑
    from tess_backend.data_connector import get_data_connector
    from tess_backend.analytics import process_question, fetch_qa_context

    connector = get_data_connector()
    token = user_token or (os.getenv("TESS_SYSTEM_TOKEN") or None)
    if use_llm:
        llm = SimpleLLMClient(
            os.getenv("TESS_LLM_BASE_URL", "https://api.deepseek.com"),
            os.getenv("TESS_LLM_API_KEY", ""),
            os.getenv("TESS_LLM_MODEL", "deepseek-chat"),
        )
        try:
            res = process_question(question, connector, llm, token=token,
                                   operator_id="verify-script", token_mode="user" if user_token else "system",
                                   analysis_type=analysis_type, params=params)
            answer = res.get("answer", "")
            cs = res.get("context_summary", {})
            errs = cs.get("errors", [])
            return [{"analysis_type": f"ask:{cs.get('analysis_type', 'shallow')}", "mode": "module+llm",
                     "route_source": cs.get("route_source"), "data_errors": errs,
                     "answer_len": len(answer), "answer_head": answer[:200],
                     "ok": (not errs) and bool(answer.strip())}]
        except Exception as e:  # noqa: BLE001
            return [{"analysis_type": "ask", "mode": "module+llm",
                     "data_errors": [f"EXC: {type(e).__name__}: {e}"], "answer_len": 0,
                     "answer_head": "", "ok": False}]
    else:
        # --no-llm：若带实体 id，则直接验证对应深度类型的取数（更贴近真实下钻）；否则验证浅层 fetch_qa_context
        if params:
            ctx2 = fetch_bi_analysis_context(connector, analysis_type or "campaign_detail",
                                            token=token, params=params)
            errs = ctx2.get("errors", [])
            return [{"analysis_type": f"ask:{analysis_type or 'campaign_detail(params)'}",
                     "mode": "module+data-only", "params": params, "data_errors": errs,
                     "context_summary": {k: _len_or_val(ctx2.get(k))
                                         for k in ("campaign_config", "quality_timeseries",
                                                   "kpi_trend", "ctit_etit", "profile", "daily_kpi",
                                                   "mapping_publisher_channels", "replace_channels", "blocks")
                                         if k in ctx2},
                     "ok": not errs}]
        ctx2 = fetch_qa_context(connector, token=token, question=question)
        errs = ctx2.get("errors", [])
        return [{"analysis_type": "ask", "mode": "module+data-only",
                 "data_errors": errs,
                 "context_summary": {"daily_kpi": _len_or_val(ctx2.get("daily_kpi_yesterday")),
                                    "ranking_top": len(ctx2.get("ranking_top", [])),
                                    "anomaly_warning": len(ctx2.get("anomaly_warning", [])),
                                    "quality_summary": _len_or_val(ctx2.get("quality_summary"))},
                 "ok": not errs}]


def main():
    ap = argparse.ArgumentParser(description="验证 /tess/analytics 十类场景 与 /tess/ask 问答（含实体下钻）")
    ap.add_argument("--http", help="HTTP 模式：服务器 base url（如 https://host）")
    ap.add_argument("--api-key", help="HTTP 模式：X-API-Key")
    ap.add_argument("--user-token", help="透传终端用户 Teensing access_token（X-Teensing-Token）；用于按用户权限取数测试")
    ap.add_argument("--no-llm", action="store_true", help="模块模式：只校验数据，不调 LLM")
    ap.add_argument("--type", choices=ALL_TYPES, help="只跑单一 analytics 场景")
    ap.add_argument("--ask", help="测试 /tess/ask 自然语言问答：传入一个问题字符串")
    ap.add_argument("--analysis-type", choices=ALL_TYPES,
                    help="配合 --ask：显式指定深度下钻 analysis_type（前端胶囊透传）；不传则由后端关键词推断")
    ap.add_argument("--campaign-id", type=int, help="实体下钻：campaign_id（campaign_detail / kpi_compare）")
    ap.add_argument("--advertiser-id", type=int, help="实体下钻：advertiser_id（advertiser_deepdive）")
    ap.add_argument("--publisher-id", type=int, help="实体下钻：publisher_id（traffic_policy_check）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非可读报告")
    args = ap.parse_args()

    if args.ask:
        results = run_ask(args.ask, use_llm=not args.no_llm,
                          user_token=args.user_token, base_url=args.http, api_key=args.api_key,
                          analysis_type=args.analysis_type,
                          campaign_id=args.campaign_id, advertiser_id=args.advertiser_id,
                          publisher_id=args.publisher_id)
    else:
        types = [args.type] if args.type else ALL_TYPES
        if args.http:
            if not args.api_key:
                print("ERROR: --http 模式需要 --api-key", file=sys.stderr)
                sys.exit(2)
            results = run_http(args.http, args.api_key, types, user_token=args.user_token)
        else:
            load_dotenv()
            results = run_module(types, use_llm=not args.no_llm, user_token=args.user_token,
                                 campaign_id=args.campaign_id, advertiser_id=args.advertiser_id,
                                 publisher_id=args.publisher_id)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("=" * 70)
        for r in results:
            print(f"[{'OK' if r['ok'] else 'FAIL'}] {r['analysis_type']}  ({r['mode']})")
            if r.get("data_errors"):
                print("    data_errors:", r["data_errors"])
            if "context_summary" in r:
                print("    context:", r["context_summary"])
            if r.get("report_len") is not None:
                print(f"    report_len={r['report_len']}")
                if r.get("report_head"):
                    print("    head:", r["report_head"].replace("\n", " ")[:120])
        print("=" * 70)

    ok_all = all(r["ok"] for r in results)
    print(f"SUMMARY: {sum(r['ok'] for r in results)}/{len(results)} passed")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
