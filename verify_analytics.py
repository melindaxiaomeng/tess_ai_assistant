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

退出码：全部场景 0 errors 且（LLM 模式下）report 非空 => 0；否则 1。
"""
import argparse
import json
import os
import sys
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

ALL_TYPES = [
    "daily_summary",
    "scaling_opportunity",
    "finance_check",
    "account_overview",
    "publisher_deepdive",
    "scaling_capacity",
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


def run_module(types, use_llm, user_token=None):
    from tess_backend.data_connector import get_data_connector
    from tess_backend.analytics import (
        fetch_bi_analysis_context,
        process_data_analysis_query,
    )

    connector = get_data_connector()
    # 默认用系统 token；传入 --user-token 则按该用户的权限取数（模拟线上透传）
    token = user_token or (os.getenv("TESS_SYSTEM_TOKEN") or None)

    results = []
    for at in types:
        if use_llm:
            try:
                llm = SimpleLLMClient(
                    os.getenv("TESS_LLM_BASE_URL", "https://api.deepseek.com"),
                    os.getenv("TESS_LLM_API_KEY", ""),
                    os.getenv("TESS_LLM_MODEL", "deepseek-chat"),
                )
                res = process_data_analysis_query(at, connector, llm, token=token)
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
            ctx = fetch_bi_analysis_context(connector, at, token=token)
            errs = ctx.get("errors", [])
            # 抽取每个场景的关键计数，便于一眼判断是否有数据
            keys = ("campaign_total", "advertiser_total", "top_advertisers_by_revenue",
                    "flagged_quality_issues", "scaling_room", "over_cap_waste",
                    "top_by_profit", "today_kpi", "rising_gainers", "falling_losers",
                    "scaling_candidates", "total_summary")
            summary = {k: _len_or_val(ctx.get(k)) for k in keys if k in ctx}
            results.append({
                "analysis_type": at, "mode": "module+data-only",
                "data_errors": errs, "context_summary": summary,
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


def main():
    ap = argparse.ArgumentParser(description="验证 /tess/analytics 六类场景")
    ap.add_argument("--http", help="HTTP 模式：服务器 base url（如 https://host）")
    ap.add_argument("--api-key", help="HTTP 模式：X-API-Key")
    ap.add_argument("--user-token", help="透传终端用户 Teensing access_token（X-Teensing-Token）；用于按用户权限取数测试")
    ap.add_argument("--no-llm", action="store_true", help="模块模式：只校验数据，不调 LLM")
    ap.add_argument("--type", choices=ALL_TYPES, help="只跑单一场景")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非可读报告")
    args = ap.parse_args()

    types = [args.type] if args.type else ALL_TYPES

    if args.http:
        if not args.api_key:
            print("ERROR: --http 模式需要 --api-key", file=sys.stderr)
            sys.exit(2)
        results = run_http(args.http, args.api_key, types, user_token=args.user_token)
    else:
        load_dotenv()
        results = run_module(types, use_llm=not args.no_llm, user_token=args.user_token)

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
