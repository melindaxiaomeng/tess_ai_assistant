# Tess Ask 流式（SSE）前端改造指南

> 适用：saas_v3 前端调用 `/tess/ask` 的模块（Tess AI 归因/问答）。
> 后端方案：轻量 SSE 流式（A 方案）。本文档是**前后端对齐的事件协议 + 前端改造代码**，后端严格按此 contract 输出。

---

## 1. 背景与目标

当前 `/tess/ask` 是**同步阻塞**返回：前端发问后干等整段算完（复合问题约 56s）才拿到一个 JSON。

改为 SSE 流式后：
- 后端**边算边推**：先推「进度」，取数完成后 LLM **逐 token 吐字**。
- 前端 56s 期间有进度条、不白转圈；且持续有数据流出，顺带免疫 nginx 的 idle 超时。
- **不缩短总耗时**（56s 取数是硬成本），也救不了 Cloudflare 100s 硬上限（但当前 56s < 100s 已不 504）。

---

## 2. 前后端事件协议（contract · 必须严格对齐）

**请求**：保持 `POST /tess/ask`，body 不变 `{ "question": "..." }`，请求头 `X-API-Key` / `X-Platform-Id` 照旧。

**内容协商（重要）**：后端默认仍返回普通 JSON（兼容旧调用方 / 集成测试）。前端要拿到 SSE 流，必须二选一触发流式：
- 请求头带 `Accept: text/event-stream`；**或**
- body 带 `"stream": true`。

两者都不带 → 返回原 JSON（无 progress / token 流）。saas_v3 前端改造请统一带 `Accept: text/event-stream`（见第 4 节 fetch 示例）。

**响应头**（流式时后端负责）：
```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no     # 关键：让 nginx 不缓冲 SSE
```

**帧格式**：每帧 `data: <json>\n\n`（SSE 标准，用 `\n\n` 分隔事件；不用 `event:` 字段，type 写在 json 里，前端更省事）。

**事件 type 枚举**：

| type | 字段 | 含义 | 前端动作 |
|---|---|---|---|
| `start` | `question: string` | 已收到问题，开始处理 | 进入「处理中」态 |
| `progress` | `stage: string, label: string, index: number, total: number` | 某个子类型取数完成 | 进度条走到 `index/total`，展示 `label` |
| `context_ready` | `stages: number` | 全部取数完成，开始 LLM 生成 | 切到「生成中」 |
| `token` | `text: string` | LLM 吐出的一个片段 | 追加到正文（可 markdown 渲染） |
| `done` | `answer: string, context_summary: object, elapsed_ms: number` | 完整答案 + 元信息 | 定稿，进度 100% |
| `error` | `message: string` | 中途失败 | 报错提示 |

**`stage` 取值示例**（复合问题）：`account_profit_rollup` / `am_leaderboard` / `metric_ranking` / `advertisers_missing_owner`；单意图问题则只有一个 `progress`，`total=1`。

---

## 3. 为什么用 `fetch + ReadableStream`（不用 EventSource）

`EventSource` 只支持 **GET**，而 `/tess/ask` 是 **POST**（带 question body）。所以必须用 `fetch` 拿 `res.body` 的 `ReadableStream` 自己解析 SSE。

---

## 4. 前端改造代码（TypeScript / React + AntD5）

### 4.1 流式读取函数

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

### 4.2 React 组件用法（进度条 + 逐字）

```tsx
// TessAskBox.tsx
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
    ctrl.current?.abort();              // 取消上一次
    ctrl.current = new AbortController();
    setStages([]); setStreamingText(""); setFinalAnswer(""); setStatus("streaming");

    await streamTessAsk(
      question,
      {
        onProgress: (e) =>
          setStages((s) => {
            const next = [...s.filter((x) => x.stage !== e.stage), e];
            return next.sort((a, b) => a.index - b.index);
          }),
        onToken: (e) => setStreamingText((t) => t + e.text),
        onDone: (e) => {
          setFinalAnswer(e.answer);
          setStreamingText("");
          setStatus("done");
        },
        onError: (e) => {
          setStatus("error");
          message.error(e.message);
        },
      },
      ctrl.current.signal
    );
  }

  const total = stages[stages.length - 1]?.total ?? 1;
  const current = stages.length;
  const pct = status === "done" ? 100 : Math.round((current / total) * 100);

  return (
    <div style={{ maxWidth: 720 }}>
      <Input.TextArea
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        placeholder="问 Tess，例如：最近两周利润 + 哪个 AM 最高 + …"
        rows={3}
      />
      <Button type="primary" onClick={ask} style={{ marginTop: 8 }}>
        提问
      </Button>

      {status !== "idle" && (
        <div style={{ marginTop: 16 }}>
          <Progress percent={pct} status={status === "error" ? "exception" : "active"} />
          {stages.map((s) => (
            <div key={s.stage} style={{ fontSize: 12, color: "#888" }}>
              {s.index}/{s.total} · {s.label}
            </div>
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

> 要点：正文用 `whiteSpace: pre-wrap` 保留换行；若想渲染 markdown，把 `streamingText`/`finalAnswer` 喂给 `react-markdown` 即可（流式阶段建议用 `remark` 容错解析，或等 `done` 后再整体渲染）。

---

## 5. nginx 必须改（否则 SSE 被缓冲、前端收不到实时流）

saas_v3 网关里代理 `/tess/` 到 Tess:8080 的 location，**必须关缓冲**：

```nginx
location /tess/ {
    proxy_pass http://tess:8080;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;          # 关键：SSE 不能缓冲
    proxy_cache off;
    chunked_transfer_encoding on;
    proxy_read_timeout 120s;      # 留余量（Cloudflare 100s 那层不可调，源站要稳在 100s 内）
}
```

如果 nginx 不便改，也可由**后端在 SSE 响应头里加 `X-Accel-Buffering: no`**（见 contract 响应头），让 nginx 自动不缓冲。两者有一即可，建议都加。

---

## 6. 后端约定（已实现）

后端已将 `/tess/ask` 改为 `StreamingResponse`，按内容协商输出（见第 2 节）：
1. 立即 `yield start`；
2. 复合分发每完成一个子类型 `yield progress`（单意图则只 1 条，`index=1,total=1`）；
3. 取数全完 `yield context_ready`；
4. LLM 生成逐 token `yield token`（HttpLLMClient 走 DeepSeek SSE；异常自动回退 `complete`）；
5. 结束 `yield done`（含 `answer` / `context_summary` / `elapsed_ms`）；
6. 任意异常 `yield error` 并关闭。

实现要点：`process_question` 新增可选回调 `on_progress / on_context_ready / on_token`（默认 None，不影响既有调用方）；端点用 worker 线程跑 `process_question`、用 `queue.Queue` 桥接 SSE 帧，Starlette 自动在独立线程迭代生成器，不阻塞事件循环。不带 `Accept: text/event-stream` 且 body 无 `stream:true` 时，端点返回原 JSON（向后兼容）。

前端按第 2 节 contract 解析即可，无需关心后端内部实现。

---

## 7. 注意事项

- **取消**：用 `AbortController`，用户切换问题/离开页面时 `ctrl.abort()`，避免旧流污染新回答。
- **Cloudflare**：免费/Pro 版单请求 100s 硬上限不可配；只要源站 100s 内开始输出并持续 flush 即不 504。若未来取数 >100s，需升级为「异步任务 + 轮询/SSE 拉取」架构（B 方案）。
- **错误**：`error` 事件或 `!res.ok` 都要 `message.error` 提示；`done` 前若连接中断，前端应保留已收到的 `streamingText` 并提示「回答未完成」。
- **鉴权头**：`X-API-Key` 走网关注入，前端一般不需自己带；若前端直连 Tess 才需（见 `VITE_TESS_API_KEY`）。
