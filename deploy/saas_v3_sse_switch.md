# saas_v3 前端切 SSE 流式 —— 根治 Tess ask 的 504/524 超时

> 问题：Tess `/tess/ask` 在重查询（长周期 + 多 Campaign）下，JSON 同步路径要求「取数 + LLM 生成」整段算完才返回首字节，合计 >100s 时 Cloudflare 免费版砍成 **524（连接超时）/ 504（网关超时）**。
> 根治：前端改用 SSE 流式（`Accept: text/event-stream`）。后端（`app.py` 内容协商，已随 `475f59c` 起就绪）**无需改动**，只要前端触发流式即可。
> 为什么能根治：SSE 的 `start` 帧在 <1s 内发出首字节，Cloudflare 见到输出即不再 524；LLM 后续逐 token 流出，总耗时再长也不超时。

---

## 第 0 步：先验证后端 SSE 已就绪（服务器终端）

绕过 Cloudflare，直接打源站，确认流式真能边算边出字：

```bash
curl -N -X POST http://localhost:8080/tess/ask \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question":"广告主1000853，7,8,9月和10月，campaign维度数据有什么变化？"}' \
  --max-time 200
```

预期：1 秒内看到第一行 `data: {"type":"start",...}`，随后持续 `progress` / `context_ready` / `token` / `done` 帧。若能这样，说明后端根治链路已通，只差前端接。

> 顺带隔离当前 524 是哪层：同样问题用 JSON 路径 localhost 测耗时
> ```bash
> curl -s -o /dev/null -w "HTTP:%{http_code} 耗时:%{time_total}s\n" -X POST http://localhost:8080/tess/ask \
>   -H "Content-Type: application/json" \
>   -d '{"question":"广告主1000853，7,8,9月和10月，campaign维度数据有什么变化？"}'
> ```
> 若耗时 >100s → 源站自己慢（确认部署到 `95b5b0c` 及之后：`git -C /root/tess log --oneline -1`）。

---

## 第 1 步：前端加流式读取函数

新建/覆盖 `src/api/tessAskStream.ts`（或你项目里调 `/tess/ask` 的模块）：

```ts
// tessAskStream.ts
export type TessAskEvent =
  | { type: "start"; question: string }
  | { type: "progress"; stage: string; label: string; index: number; total: number }
  | { type: "context_ready"; stages: number }
  | { type: "token"; text: string }
  | { type: "done"; answer: string; context_summary: Record<string, unknown>; elapsed_ms: number }
  | { type: "error"; message: string };

type Handlers = Partial<{
  onStart: (e: Extract<TessAskEvent, { type: "start" }>) => void;
  onProgress: (e: Extract<TessAskEvent, { type: "progress" }>) => void;
  onContextReady: (e: Extract<TessAskEvent, { type: "context_ready" }>) => void;
  onToken: (e: Extract<TessAskEvent, { type: "token" }>) => void;
  onDone: (e: Extract<TessAskEvent, { type: "done" }>) => void;
  onError: (e: Extract<TessAskEvent, { type: "error" }>) => void;
}>;

export async function streamTessAsk(
  question: string,
  handlers: Handlers,
  signal?: AbortSignal
): Promise<void> {
  const res = await fetch("/tess/ask", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Accept": "text/event-stream",          // 关键：触发后端 SSE 流式（不带则回退普通 JSON）
      "X-API-Key": import.meta.env.VITE_TESS_API_KEY ?? "",
      "X-Platform-Id": import.meta.env.VITE_PLATFORM_ID ?? "",
    },
    body: JSON.stringify({ question }),
    signal,
  });

  if (!res.ok || !res.body) {
    handlers.onError?.({ type: "error", message: `HTTP ${res.status}` });
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    // SSE 事件以 "\n\n" 分隔
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) !== -1) {
      const raw = buf.slice(0, sep).trim();
      buf = buf.slice(sep + 2);
      if (!raw.startsWith("data:")) continue;
      const json = raw.slice(5).trim();
      try {
        const evt = JSON.parse(json) as TessAskEvent;
        switch (evt.type) {
          case "start": handlers.onStart?.(evt); break;
          case "progress": handlers.onProgress?.(evt); break;
          case "context_ready": handlers.onContextReady?.(evt); break;
          case "token": handlers.onToken?.(evt); break;
          case "done": handlers.onDone?.(evt); break;
          case "error": handlers.onError?.(evt); break;
        }
      } catch {
        // 忽略畸形帧
      }
    }
  }
}
```

> 注意：`EventSource` 只支持 GET，而 `/tess/ask` 是 POST，**必须用 `fetch` + `ReadableStream`** 自己解析，不能用 EventSource。

---

## 第 2 步：React 组件接进度条 + 逐字

参考 `TessAskBox.tsx`（进度条 + 逐字渲染），核心回调：

```tsx
import { useRef, useState } from "react";
import { Progress, Button, Input, Typography, message } from "antd";
import { streamTessAsk, TessAskEvent } from "./tessAskStream";

type Stage = { stage: string; label: string; index: number; total: number };

export function TessAskBox() {
  const [question, setQuestion] = useState("");
  const [stages, setStages] = useState<Stage[]>([]);
  const [streamingText, setStreamingText] = useState("");
  const [finalAnswer, setFinalAnswer] = useState("");
  const [status, setStatus] = useState<"idle" | "streaming" | "done" | "error">("idle");
  const ctrl = useRef<AbortController | null>(null);

  async function ask() {
    ctrl.current?.abort();
    ctrl.current = new AbortController();
    setStages([]); setStreamingText(""); setFinalAnswer(""); setStatus("streaming");

    await streamTessAsk(
      question,
      {
        onProgress: (e) => setStages((s) => [...s.filter((x) => x.stage !== e.stage), e]),
        onToken: (e) => setStreamingText((t) => t + e.text),
        onDone: (e) => { setFinalAnswer(e.answer); setStreamingText(""); setStatus("done"); },
        onError: (e) => { setStatus("error"); message.error(e.message); },
      },
      ctrl.current.signal
    );
  }

  const total = stages[stages.length - 1]?.total ?? 1;
  const pct = status === "done" ? 100 : Math.round((stages.length / total) * 100);

  return (
    <div style={{ maxWidth: 720 }}>
      <Input.TextArea value={question} onChange={(e) => setQuestion(e.target.value)}
        placeholder="问 Tess，例如：最近两周利润 + 哪个 AM 最高 + …" rows={3} />
      <Button type="primary" onClick={ask} style={{ marginTop: 8 }}>提问</Button>
      {status !== "idle" && (
        <div style={{ marginTop: 16 }}>
          <Progress percent={pct} status={status === "error" ? "exception" : "active"} />
          {stages.map((s) => (
            <div key={s.stage} style={{ fontSize: 12, color: "#888" }}>{s.index}/{s.total} · {s.label}</div>
          ))}
          <Typography.Paragraph style={{ marginTop: 12, whiteSpace: "pre-wrap" }}>
            {finalAnswer || streamingText}
          </Typography.Paragraph>
        </div>
      )}
    </div>
  );
}
```

---

## 第 3 步：nginx 关缓冲（否则 SSE 被缓冲、前端收不到实时流）

saas_v3 网关里代理 `/tess/` 到 Tess:8080 的 location，**必须加 `proxy_buffering off`**：

```nginx
location /tess/ {
    proxy_pass http://tess:8080;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;          # 关键：SSE 不能缓冲
    proxy_cache off;
    chunked_transfer_encoding on;
    proxy_read_timeout 300s;      # 长连接留余量（Cloudflare 100s 那层不可调，源站要稳在 100s 内开始输出）
}
```

> 后端 SSE 响应已带 `X-Accel-Buffering: no`，nginx 会自动不缓冲；但显式 `proxy_buffering off` 更稳，两者都有最佳。

---

## 第 4 步：部署后验证（公网）

```bash
curl -N -X POST https://saasv3.melo.support/tess/ask \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question":"广告主1000853，7,8,9月和10月，campaign维度数据有什么变化？"}' \
  --max-time 200
```

应能在 1s 内见 `start` 帧并开始持续输出，不再 524/504。

---

## 兜底说明（零前端改动也能治 524 —— 已随后端落地）

> 重要更新：自本版后端起，**不带 `Accept: text/event-stream` 的普通 JSON 路径也不再零首字节**——
> `post_ask` 的非流式分支改为返回 `StreamingResponse`，先 yield 一个字节（首字节 <1s 到达网关），
> 再跑「取数 + LLM 生成」，算完补 JSON 正文。前端 `fetch().json()` 照常解析，完全无感。
> 这意味着 **524 现在可以只改后端 + 改 nginx 缓冲就根治，无需动前端**。

若暂不改前端（仍走 JSON 同步路径）：
- 后端取数已优化（`95b5b0c` 长周期改 `/report`；`5cfac53` 长周期按月切片并发），单月/长周期取数都压在预算内；
- 非流式路径已先吐首字节 → Cloudflare 不再因「零首字节」砍 524；
- **唯一硬依赖**：nginx 代理 `/tess/` 必须 `proxy_buffering off`（见第 3 步），否则 nginx 把首字节缓在 8KB 缓冲里不转发，Cloudflare 仍零首字节 → 524。

## 第 3 步对两条路径都强制

无论走 SSE 还是普通 JSON 早期刷新，**nginx `/tess/` 都必须 `proxy_buffering off`**（否则首字节被缓冲，524 照旧）。第 3 步配置对两种方案都是必选项。

## 后端状态

`app.py` 的 `post_ask` 已实现内容协商（`want_stream = "text/event-stream" in accept or body.stream`）：

- 带 `Accept: text/event-stream` → `StreamingResponse`（SSE），worker 线程跑 `process_question`，`queue.Queue` 桥接 `start/progress/context_ready/token/done` 帧，响应头含 `X-Accel-Buffering: no`。
- 不带 → **同样返回 `StreamingResponse`（media_type=application/json）**，但先 yield 一个字节再算，算完补 JSON 正文；响应头含 `X-Accel-Buffering: no`。前端无需任何改动即可规避 524。

本版后端需重新 build 部署（commit `5cfac53` 起的 app.py 改动）才会生效。
