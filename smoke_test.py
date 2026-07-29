"""Tess · 真实 LLM 链路冒烟测试。

前置：已 `cp .env.example .env` 并填好 TESS_LLM_API_KEY。
运行：
  /Users/menlinda.meng/.workbuddy/binaries/python/envs/tess/bin/python smoke_test.py

脚本会自动加载同目录 .env，用 R6 示例（Margin 3.8% / Loss $350 -> HIGH）
真实调一次 DeepSeek，打印 Gatekeeper 归一化后的诊断结果。
"""

import os
import sys

# --- 极简 .env 加载（不引入额外依赖）---
def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from tess_backend.orchestrator import run_diagnosis  # noqa: E402
from tess_backend.tess_agent import HttpLLMClient      # noqa: E402

# R6 示例输入（算法层已注入 severity + calculated_loss）
R6_INPUT = {
    "anomaly_metadata": {
        "event_id": "ERR-20260728-0912",
        "current_value": "3.8%",
        "severity": "HIGH",
        "calculated_loss": {
            "loss_per_hour_usd": 350.0,
            "calculation_basis": "基于过去 30 分钟 Cost 速率与缺失 Revenue 的差值计算",
        },
    },
    "top_contributors": [
        {
            "dimension_type": "Publisher",
            "dimension_value": "Pub_Media_802",
            "impact_share": "82%",
        }
    ],
    "associated_signals": [
        {
            "source": "AppsFlyer_Pull_API",
            "status": "WARNING",
            "detail": "13:30-14:00 期间 Postback 接口 HTTP 504 占比 45%",
        }
    ],
}


def main() -> None:
    if not os.getenv("TESS_LLM_API_KEY"):
        print("✗ 未检测到 TESS_LLM_API_KEY。请先 `cp .env.example .env` 并填好 Key。")
        sys.exit(1)

    client = HttpLLMClient(
        base_url=os.getenv("TESS_LLM_BASE_URL", "https://api.deepseek.com"),
        api_key=os.getenv("TESS_LLM_API_KEY"),
        model=os.getenv("TESS_LLM_MODEL", "deepseek-chat"),
    )

    print("▶ 正在用 R6 示例真实调用 DeepSeek …\n")
    result = run_diagnosis(R6_INPUT, client)
    print("✓ 返回（Gatekeeper 已归一化）：")
    print(__import__("json").dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
