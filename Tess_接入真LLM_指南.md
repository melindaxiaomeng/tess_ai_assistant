# Tess · 接入真实 LLM（DeepSeek）指南

> 前置：后端 MVP 闭环（P0–P6）已写完，31 条单测全绿。
> 本指南只解决一件事：**让 Tess 从「Mock 跑通链路」变成「真的调 DeepSeek 出诊断」**。
> 代码层面零改动（已是 OpenAI 兼容），只需配环境变量 + 起服务。

---

## 1. 依赖确认（fastapi / httpx 已装则跳过）

```bash
cd "/Users/menlinda.meng/Desktop/ai/Tess AI Assistant"
/Users/menlinda.meng/.workbuddy/binaries/python/envs/tess/bin/pip install fastapi httpx
```

## 2. 填密钥（唯一必做的人工步骤）

```bash
cp .env.example .env
# 编辑 .env，把 TESS_LLM_API_KEY 换成你的真实 Key
```

`.env` 内容（默认即 DeepSeek，只需改 Key）：

```
TESS_LLM_BASE_URL=https://api.deepseek.com
TESS_LLM_API_KEY=sk-xxxx
TESS_LLM_MODEL=deepseek-chat
```

> 用其他 OpenAI 兼容网关？只改 `TESS_LLM_BASE_URL` / `TESS_LLM_MODEL` 即可，
> 鉴权统一走 `Authorization: Bearer <key>`，无需改代码。

## 3. 起服务

```bash
cd "/Users/menlinda.meng/Desktop/ai/Tess AI Assistant"
/Users/menlinda.meng/.workbuddy/binaries/python/envs/tess/bin/python -m \
  tess_backend.app
# 或用 uvicorn：
/Users/menlinda.meng/.workbuddy/binaries/python/envs/tess/bin/uvicorn \
  tess_backend.app:app --port 8080 --reload
```

> 注意：必须在**项目根目录**下启动，保证 `tess_backend` 包可被 import。

## 4. 冒烟测试（验证真 LLM 链路打通）

两种方式任选其一：

### A. 脚本（推荐，带 .env 自动加载）

```bash
/Users/menlinda.meng/.workbuddy/binaries/python/envs/tess/bin/python smoke_test.py
```

脚本会用 R6 示例（Margin 3.8% / Loss $350 → HIGH）真实调一次 DeepSeek，
打印 Gatekeeper 归一化后的诊断结果（含 status / confidence / summary / causal_chain）。

### B. curl 直连 API

```bash
curl -X POST http://localhost:8080/tess/diagnose \
  -H "Content-Type: application/json" \
  -d '{
    "anomaly_metadata": {
      "event_id": "ERR-20260728-0912",
      "current_value": "3.8%",
      "severity": "HIGH",
      "calculated_loss": {
        "loss_per_hour_usd": 350.0,
        "calculation_basis": "基于过去 30 分钟 Cost 速率与缺失 Revenue 的差值计算"
      }
    },
    "top_contributors": [
      {"dimension_type": "Publisher", "dimension_value": "Pub_Media_802", "impact_share": "82%"}
    ],
    "associated_signals": [
      {"source": "AppsFlyer_Pull_API", "status": "WARNING",
       "detail": "13:30-14:00 期间 Postback 接口 HTTP 504 占比 45%"}
    ]
  }'
```

## 5. 接前端

把 `tess_drawer.tsx` 交给前端组，挂到三个入口（详见 PRD §3.1）：
异常池每行「Tess 诊断」按钮 / 预警通知「查看 Tess 诊断」深链 / 大盘异常图表浮标。
前端拿到的就是第 4 步返回的结构化 JSON。

---

## 常见问题排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `503 Tess LLM 未配置` | 没设 `TESS_LLM_API_KEY` 或 `.env` 未加载 | 确认 `.env` 存在且 Key 已填；脚本方式会自动加载 `.env` |
| `RuntimeError: LLM HTTP 401` | Key 错误 / 失效 | 去 DeepSeek 后台重置 Key |
| `RuntimeError: LLM HTTP 429` | 额度/限速 | 稍后重试，或调小并发；Agent 层已带重试退避 |
| 返回 `INCONCLUSIVE` + 置信度 0.0 | LLM 连续失败被熔断兜底 | 看服务日志的 `RuntimeError` 明细（已带 HTTP 状态码） |
| 解析偶尔失败 | 模型偶发输出非纯 JSON | 已开 `response_format=json_object` + 容错截取；若仍频繁，调 `temperature` 或换 `deepseek-reasoner` |

> **死锁仍在**：即便接了真 LLM，`severity` / `calculated_loss` 也绝不会出现在 LLM 输出里
> （`additionalProperties:false` 在 Gatekeeper 物理锁死），前端数字一律从 Input 渲染。
