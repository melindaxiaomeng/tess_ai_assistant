"""Tess 样例：强对齐输入 → 预期高置信 DIAGNOSED。

演示"正常确诊"的样子：时间窗对齐、配置变更日志与 API 报错日志齐全、
维度贡献集中，模型应判 DIAGNOSED (confidence >= 0.85)。
"""
import os
import json

# 手动加载 .env（避免引入 dotenv 依赖）
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from tess_backend.orchestrator import run_diagnosis
from tess_backend.tess_agent import HttpLLMClient, _build_user_prompt, _parse_json

# 强对齐样例：13:25 配置变更 → 之后 API 504 持续 → 毛利断崖（时间点完全吻合）
STRONG_INPUT = {
    "anomaly_metadata": {
        "event_id": "ERR-20260728-0912",
        "trigger_time": "2026-07-28 14:00:00",
        "target_metric": "Overall Margin",
        "current_value": "3.8%",
        "benchmark_value": "14.2%",
        "severity": "HIGH",
        "calculated_loss": {
            "loss_per_hour_usd": 350.00,
            "calculation_basis": "基于过去 30 分钟 Cost 速率与缺失 Revenue 的差值计算",
        },
    },
    "top_contributors": [
        {
            "dimension_type": "Publisher",
            "dimension_value": "Pub_Media_802",
            "impact_share": "82%",
            "metric_change": "Margin 从 15.1% 降至 -2.4%",
        }
    ],
    "associated_signals": [
        {
            "source": "Config_Change_Log",
            "status": "INFO",
            "detail": "13:25 运营在 Teensing 后台将 Pub_Media_802 的 Postback 映射规则从 'default_v2' 切换为 'new_exp_v1'（变更单 CHG-4821，操作人 alice）。",
        },
        {
            "source": "AppsFlyer_Pull_API",
            "status": "ERROR",
            "detail": "13:25-14:00 期间（即配置变更后），Pub_Media_802 的 Postback 回传接口持续 HTTP 504 (Gateway Timeout) 占比 92%，与该 Publisher 毛利从 15.1% 断崖跌至 -2.4% 的时间点完全吻合。",
        },
    ],
}


def main() -> None:
    client = HttpLLMClient(
        base_url=os.environ["TESS_LLM_BASE_URL"],
        api_key=os.environ["TESS_LLM_API_KEY"],
        model=os.environ.get("TESS_LLM_MODEL", "deepseek-chat"),
    )

    # 先看模型原始返回（便于理解它怎么想的）
    raw = client.complete(
        # 复用 Agent 的 system prompt（不回显 Key）
        __import__("tess_backend.tess_agent", fromlist=["SYSTEM_PROMPT"]).SYSTEM_PROMPT,
        _build_user_prompt(STRONG_INPUT),
    )
    print("===== 模型原始返回 =====")
    print(raw)

    print("\n===== 完整 run_diagnosis（Gatekeeper 现已原生处理近义词）=====")
    result = run_diagnosis(STRONG_INPUT, client)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
