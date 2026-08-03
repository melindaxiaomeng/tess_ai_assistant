"""P3 · Tess Agent —— LLM 调用层（三层死锁的「建议层」封装）。

职责边界（与 PRD 一致）：
- 只负责「把结构化 Input 喂给 LLM，拿回一段 JSON 文本，解析，再交给 Gatekeeper」。
- 绝对不持有任何数值 / Severity / 路由：这些要么是算法层算好的（Input），
  要么由 Gatekeeper 死锁校验。Agent 自己不做任何业务判定。
- 任何网络 / 解析异常都走重试；重试耗尽则返回与 Gatekeeper 一致的 INCONCLUSIVE 兜底。
"""

import json
import logging
import re
import time
from typing import List, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

from .contracts import STATUS_INCONCLUSIVE
from .gatekeeper import validate_tess_output
from .thresholds import ThresholdPolicy, default_policy

MAX_RETRIES = 2  # 额外重试次数；实际最多尝试 MAX_RETRIES + 1 次


# ---------------------------------------------------------------------------
# System Prompt（PRD §5，含红线 + 置信度评分指南 + Schema 绑定）
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你叫 Tess，是 Teensing 平台的专属 AdTech 智能数据分析与风控专家。\
你的任务是根据传入的结构化业务数据、异动指标和关联日志，进行严谨的根因分析（Root Cause Analysis）。

### 绝对红线规则（违反将导致系统失效）：
1. 严禁幻觉与猜测：如果传入数据**完全没有任何可依据的量化异常**，或信号自相矛盾，\
必须将 status 设为 "INCONCLUSIVE"，并将 confidence 设为低于 0.6，在 summary 中明确指出\
“数据信号不足，无法推导明确根因，建议人工介入”。\
但若数据呈现**可归纳的量化异常模式**（见下方「假设归因指引」），你应当输出 **DIAGNOSED_SUSPECT 假设档**\
（confidence 0.60~0.72），并显式标注结论为「假设 / 待核实」——**绝不**伪装成已确认根因。
2. 数值严禁篡改：损耗金额等财务数据（calculated_loss）必须直接沿用输入数值，\
严禁自行计算、捏造或修改任何货币与百分比数字。
3. 严禁修改 Severity：异常严重程度已由系统规则引擎判定，你只需在建议动作中匹配该严重程度的处置紧迫感。
4. 严禁编造操作员 / 实体身份：summary 与 root_cause_analysis 中出现的人名、账号、操作员 ID、团队名等主体实体，\
必须严格来自输入数据中真实出现的内容（例如日志原文里明确写到的 "alice"、工单号 "CHG-4821"）。\
若输入仅以泛称（如「运营」「系统」「相关团队」）指代、并未给出具体身份，你必须沿用该泛称，\
绝不得凭空生成具体人名 / 账号（如 alice、admin、user_001 等）；不确定的主体一律用「运营 / 系统 / 相关团队」等泛称表述。

### 归因置信度 (Confidence) 评分指南：
- 0.85 ~ 1.00：存在明确的操作日志或 API 报错日志，且与指标下滑时间点完全吻合 -> DIAGNOSED。
- 0.72 ~ 0.84：维度贡献集中（Top 1 > 80%）但缺乏直接技术报错日志佐证，或有较强旁证的假设 -> DIAGNOSED_SUSPECT。
- 0.60 ~ 0.71：**假设档（无确认证据，仅从指标模式归纳）**：数据呈现清晰量化异常（负毛利 / 异常 CVR / 收入异常飙升 / 利润为负等），\
但缺少日志或维度拆解来锁定唯一根因 -> DIAGNOSED_SUSPECT，且 summary 与 root_cause_analysis 必须显式标注「假设，待核实」。
- < 0.60：数据无任何可依据的信号（指标缺失 / 全部处于正常区间 / 信息自相矛盾）-> 强制 "INCONCLUSIVE"。

### 假设归因指引（DIAGNOSED_SUSPECT 假设档应如何写）：
当只能从单点指标快照归纳、无法锁定唯一根因时，给出**可检验的假设**而非放弃：
- 毛利为负 / margin < 0：疑似出价过高或成本失控导致亏损投放；建议核对投放后台成本曲线与 ROI。
- CVR 显著偏低 / 骤降：疑似素材疲劳、受众饱和，或归因 / 埋点异常；建议抽查素材表现与转化回传链路。
- revenue 异常飙升（direction=rising 且变化幅度大）：疑似异常流量 / 刷量，或偶发病毒式增长；建议核查流量质量与设备分布。
- profit 为负且 revenue 为正：消耗已超过收入，疑似预算 / 出价策略失衡。
要求：summary 以「疑似…（假设，待核实）」开头；root_cause_analysis.primary_factor 表述为假设；\
causal_chain 末项写明「需核实：<具体证据 / 动作>」。不得编造确定结论或具体责任人。
适用范围：仅在指标呈现**明显**异常时给假设（如 severity≥MEDIUM、毛利转负、收入异常飙升）；\
**轻微**波动（severity=LOW 的微跌、单点无异常指标）缺乏归因价值，保持 INCONCLUSIVE。

### 输出约束：
- 必须且只能返回符合指定 JSON Schema 的标准 JSON。
- 允许字段仅：status(三态枚举)、confidence(0.0-1.0)、summary(字符串)、\
primary_contributor_id(字符串或 null)、root_cause_analysis{primary_factor, causal_chain[]}。
- 严禁返回 severity、calculated_loss 等系统字段；不得包含任何 Markdown 标记、代码围栏或前导文字。
"""


# ---------------------------------------------------------------------------
# LLM 客户端抽象
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMClient(Protocol):
    """任何 LLM 后端只需实现 complete(system, user) -> str。"""

    def complete(self, system: str, user: str) -> str:
        ...


class MockLLMClient:
    """测试 / 本地开发用的假 LLM。

    responses 可以是单个 dict（每次都返回它），或 dict 列表（依次返回，便于模拟重试）。
    """

    def __init__(self, responses) -> None:
        if isinstance(responses, dict):
            responses = [responses]
        self._responses: List[dict] = list(responses)
        self._idx = 0
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        if self._idx >= len(self._responses):
            return json.dumps(self._responses[-1], ensure_ascii=False)
        payload = self._responses[self._idx]
        self._idx += 1
        return json.dumps(payload, ensure_ascii=False)


class HttpLLMClient:
    """真实 LLM 后端（OpenAI 兼容 /chat/completions，已验证 DeepSeek）。

    仅用标准库 urllib，不引入额外依赖。
    - json_mode=True 时附加 response_format=json_object，显著降低解析失败率
      （要求 Prompt 中出现 'JSON' 字样，System Prompt 已满足）。
    - 出错时把 HTTP 状态码 + 响应体一并抛出，方便排查配置问题。
    - 缺少 API Key 时 complete() 直接抛异常，由 Agent 的重试 / 兜底逻辑接管。
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: float = 30.0, json_mode: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.json_mode = json_mode

    def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise RuntimeError("HttpLLMClient 缺少 API Key，无法调用真实 LLM")
        import urllib.request
        import urllib.error

        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,  # PRD §6.1：低温度保证格式确定性
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            raise RuntimeError(f"LLM HTTP {e.code}: {detail[:500]}") from e
        return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 解析与兜底
# ---------------------------------------------------------------------------

def _parse_json(raw: str) -> dict:
    """把 LLM 返回的文本解析成 dict。

    容忍常见的「代码围栏」「前后多余文字」：先尝试整段解析，失败则截取第一个
    '{' 到最后一个 '}' 之间的内容重试。仍失败则抛 JSONDecodeError（触发重试）。
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise


def _build_user_prompt(input_data: dict) -> str:
    return (
        "以下是本次异常的诊断上下文（由算法层注入，数值与 Severity 已计算好，"
        "你只需做定性归因）：\n\n"
        f"{json.dumps(input_data, ensure_ascii=False, indent=2)}\n\n"
        "请严格按照 System Prompt 中的 JSON Schema 输出归因结果。"
    )


def _agent_failure_fallback() -> dict:
    """重试耗尽 / LLM 连续失败时的兜底，形状与 Gatekeeper 熔断分支保持一致。"""
    return {
        "status": STATUS_INCONCLUSIVE,
        "confidence": 0.0,
        "summary": "Tess 调用 LLM 失败或多次重试无果，已自动切入人工排查。",
        "root_cause_analysis": {
            "primary_factor": "系统熔断：LLM 调用 / 解析连续失败",
            "causal_chain": ["LLM 服务异常", "重试耗尽", "转人工处理"],
        },
    }


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class TessAgent:
    """编排单次诊断：构造 Prompt -> 调 LLM -> 解析 -> 交付 Gatekeeper。"""

    def __init__(self, llm: LLMClient, max_retries: int = MAX_RETRIES,
                 policy: ThresholdPolicy = None) -> None:
        self.llm = llm
        self.max_retries = max_retries
        self.policy = policy or default_policy()

    def diagnose(self, input_data: dict) -> dict:
        prompt = _build_user_prompt(input_data)

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = self.llm.complete(SYSTEM_PROMPT, prompt)
                parsed = _parse_json(raw)
                # 出口必须是 Gatekeeper：LLM 说的话在此被死锁校验
                return validate_tess_output(parsed, input_data, self.policy)
            except Exception as e:  # 网络 / 解析异常 -> 重试
                last_err = e
                logger.warning("LLM 调用第 %d 次失败：%r", attempt + 1, e)
                time.sleep(min(0.1 * (attempt + 1), 1.0))  # 轻微退避

        # 重试耗尽仍未拿到合法输出
        return _agent_failure_fallback()
