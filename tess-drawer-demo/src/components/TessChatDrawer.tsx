/**
 * Tess 自然语言问答抽屉（多轮会话 / chat_id）
 * ---------------------------------------------------------------
 * 对应后端 /tess/ask 的多轮能力（服务端 chat_id 会话）。
 *
 * 前端改动要点（3 处，已全部落在此组件，对照 FRONTEND_CHAT_INTEGRATION.md）：
 *  1) 生成 chat_id：抽屉首次挂载 / “新对话”时 crypto.randomUUID()，绑定到本抽屉实例。
 *  2) 携带 chat_id：每次 POST /tess/ask 的 body 都带同一个 chat_id（不传 = 单轮兜底）。
 *  3) 前端累积渲染：服务端只回本轮 answer，对话流由 messages 数组累积、顺序渲染；
 *     服务端用历史做“指代消解”（“它/这个”指上一轮实体），前端无需补实体。
 *
 * P3 增强（数据依据透传 + 👍/👎 纠错）：
 *  - 后端在 context_summary 里回传 provenance（查了哪些路径/表/实体/时间）与
 *    event_id（反馈锚点）。本组件把 provenance 渲染为默认收起的「数据依据」面板，
 *    并把 👍/👎 接回 POST /tess/feedback，运营点开即可核对取数逻辑、一键纠错。
 *
 * 可直接复制进 Teensing 前端工程（依赖 React 18+，样式用 Tailwind 类名）。
 */

import React, { useRef, useState } from "react";

export interface TessChatMessage {
  role: "user" | "assistant";
  content: string;
  // 后端 /tess/ask 回传的 context_summary（含 provenance + event_id），仅 assistant 消息有
  context_summary?: Record<string, any>;
}

export interface TessChatDrawerProps {
  backend: string; // 如 http://<Tess服务器IP>:8080
  apiKey?: string; // X-API-Key（生产必带，守卫 /tess/*）
  platformId?: string; // X-Platform-Id（平台标识；后端按它取该平台 token）
}

export const TessChatDrawer: React.FC<TessChatDrawerProps> = ({
  backend,
  apiKey,
  platformId,
}) => {
  // —— 改动 1：生成并持有 chat_id（每次“新对话”换一个新的）——
  const [chatId, setChatId] = useState<string>(() => crypto.randomUUID());
  const [messages, setMessages] = useState<TessChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // P3：每条 assistant 消息的投票状态（accurate/inaccurate）与纠错输入
  const [votes, setVotes] = useState<Record<number, string>>({});
  const [correctionFor, setCorrectionFor] = useState<number | null>(null);
  const [correctionText, setCorrectionText] = useState("");
  const listRef = useRef<HTMLDivElement>(null);

  const base = backend.replace(/\/+$/, "");

  const newChat = () => {
    setChatId(crypto.randomUUID()); // 换新会话：历史不继承
    setMessages([]);
    setError(null);
    setVotes({});
    setCorrectionFor(null);
    setCorrectionText("");
  };

  const send = async () => {
    const q = question.trim();
    if (!q || loading) return;
    setLoading(true);
    setError(null);
    // 先把用户问题压入本地对话流（前端累积，改动 3）
    const next = [...messages, { role: "user" as const, content: q }];
    setMessages(next);
    setQuestion("");

    try {
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };
      if (apiKey) headers["X-API-Key"] = apiKey;
      if (platformId) headers["X-Platform-Id"] = platformId;

      const res = await fetch(`${base}/tess/ask`, {
        method: "POST",
        headers,
        // —— 改动 2：携带同一 chat_id，开启多轮指代消解 ——
        body: JSON.stringify({ question: q, chat_id: chatId }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${d.detail || res.statusText}`);
      // 后端只回本轮 Markdown，取 answer / result / data 任一非空字段
      const answer: string = d.answer ?? d.result ?? d.data ?? "（无返回内容）";
      const cs: Record<string, any> | undefined = d.context_summary;
      // —— 改动 3：累积本轮 answer + context_summary，顺序渲染 ——
      setMessages([...next, { role: "assistant" as const, content: answer, context_summary: cs }]);
    } catch (e: any) {
      setError(e.message);
      setMessages([
        ...next,
        { role: "assistant" as const, content: `⚠️ 请求失败：${e.message}` },
      ]);
    } finally {
      setLoading(false);
      requestAnimationFrame(() => listRef.current?.scrollTo({ top: 1e9 }));
    }
  };

  // P3：👍/👎 纠错回传
  const sendFeedback = async (idx: number, vote: "accurate" | "inaccurate") => {
    const cs = messages[idx]?.context_summary;
    if (!cs?.event_id) return;
    setVotes((v) => ({ ...v, [idx]: vote }));
    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (apiKey) headers["X-API-Key"] = apiKey;
      if (platformId) headers["X-Platform-Id"] = platformId;
      await fetch(`${base}/tess/feedback`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          event_id: cs.event_id,
          vote,
          // 👎 时把运营填的“哪里不对”一并回传；👍 不传
          corrected_root_cause: vote === "inaccurate" ? correctionText || undefined : undefined,
        }),
      });
    } catch {
      /* 反馈落库失败不影响对话，静默即可 */
    } finally {
      setCorrectionFor(null);
      setCorrectionText("");
    }
  };

  return (
    <div className="flex flex-col h-[560px] bg-white border border-gray-200 rounded-lg overflow-hidden">
      {/* 顶部：会话标识 + 新对话 */}
      <div className="flex items-center justify-between px-4 py-2 border-b bg-gray-50">
        <div className="text-xs text-gray-500">
          Tess 问答 · 多轮会话
          <span className="ml-2 font-mono text-gray-400">
            chat_id: {chatId.slice(0, 8)}…
          </span>
        </div>
        <button
          className="text-xs border border-gray-300 rounded px-2 py-1 hover:bg-gray-100"
          onClick={newChat}
        >
          新对话
        </button>
      </div>

      {/* 消息流 */}
      <div ref={listRef} className="flex-1 overflow-auto p-4 space-y-3">
        {messages.length === 0 && (
          <div className="text-xs text-gray-400">
            试试：“广告主 1000839 在渠道 1000684 上近 7 日营收怎么样？” 然后再问
            “它昨天的营收呢？”（无需重复实体，chat_id 自动指代上一轮）
          </div>
        )}
        {messages.map((m, i) => (
          <div
            key={i}
            className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
          >
            <div className="max-w-[88%] space-y-2">
              <div
                className={`rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
                  m.role === "user"
                    ? "bg-blue-600 text-white"
                    : "bg-slate-100 text-slate-900"
                }`}
              >
                {m.content}
              </div>

              {/* P3：assistant 消息底部 —— 数据依据 + 👍/👎 */}
              {m.role === "assistant" && m.context_summary && (
                <div className="text-xs">
                  {/* 👍/👎 反馈行 */}
                  <div className="flex items-center gap-2">
                    {!votes[i] ? (
                      <>
                        <span className="text-gray-400">这个回答有用吗？</span>
                        <button
                          className="border border-gray-300 rounded px-2 py-0.5 hover:bg-gray-100"
                          onClick={() => sendFeedback(i, "accurate")}
                        >
                          👍 准
                        </button>
                        <button
                          className="border border-gray-300 rounded px-2 py-0.5 hover:bg-gray-100"
                          onClick={() => setCorrectionFor(i)}
                        >
                          👎 不准
                        </button>
                      </>
                    ) : (
                      <span className="text-gray-400">
                        已反馈：{votes[i] === "accurate" ? "准确 👍" : "不准确 👎"}
                      </span>
                    )}
                  </div>

                  {/* 👎 展开的纠错输入 */}
                  {correctionFor === i && (
                    <div className="mt-2 p-2 border rounded bg-amber-50 space-y-2">
                      <textarea
                        className="w-full border border-gray-300 rounded px-2 py-1 text-xs"
                        rows={2}
                        placeholder="哪里不对？（可选，帮助技术小哥定位）"
                        value={correctionText}
                        onChange={(e) => setCorrectionText(e.target.value)}
                      />
                      <div className="flex gap-2">
                        <button
                          className="bg-red-600 text-white rounded px-2 py-0.5 text-xs hover:bg-red-700"
                          onClick={() => sendFeedback(i, "inaccurate")}
                        >
                          提交纠错
                        </button>
                        <button
                          className="border border-gray-300 rounded px-2 py-0.5 text-xs hover:bg-gray-100"
                          onClick={() => {
                            setCorrectionFor(null);
                            setCorrectionText("");
                          }}
                        >
                          取消
                        </button>
                      </div>
                    </div>
                  )}

                  {/* 可折叠「数据依据」面板 */}
                  <details className="mt-1 group">
                    <summary className="cursor-pointer text-gray-400 hover:text-gray-600 select-none">
                      🔍 数据依据（查了哪些表/条件）
                    </summary>
                    <ProvenanceView cs={m.context_summary} />
                  </details>
                </div>
              )}
            </div>
          </div>
        ))}
        {loading && <div className="text-xs text-gray-400">[ Tess ] 正在分析…</div>}
        {error && <div className="text-xs text-red-500">{error}</div>}
      </div>

      {/* 输入区 */}
      <div className="border-t p-3 flex gap-2">
        <input
          className="flex-1 border border-gray-300 rounded-md px-3 py-2 text-sm"
          placeholder="向 Tess 提问（自然语言）…"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button
          className="bg-blue-600 hover:bg-blue-700 text-white rounded-md px-4 text-sm disabled:opacity-50"
          onClick={send}
          disabled={loading || !question.trim()}
        >
          发送
        </button>
      </div>
    </div>
  );
};

/** 把后端回传的 context_summary.provenance 渲染成可读依据块 */
function ProvenanceView({ cs }: { cs: Record<string, any> }) {
  const pv = cs?.provenance;
  const rows: Array<[string, React.ReactNode]> = [
    ["分析类型", cs?.analysis_type ?? "—"],
    ["路由来源", cs?.provenance?.route_source ?? cs?.route_source ?? "—"],
    ["时间范围", cs?.provenance?.date_or_month ?? cs?.date_or_month ?? "—"],
  ];
  if (pv?.entities && Object.keys(pv.entities).length) {
    rows.push([
      "过滤实体",
      Object.entries(pv.entities)
        .map(([k, v]) => `${k}=${v}`)
        .join("，"),
    ]);
  }
  if (pv?.tables?.length) {
    rows.push(["查询的表", pv.tables.join("、")]);
  }
  if (pv?.routes?.length) {
    rows.push([
      "取数路径",
      pv.routes.map((r: any) => `${r.path}（${r.source}）`).join("、"),
    ]);
  }
  if (!rows.some(([, v]) => v && v !== "—")) {
    return <div className="text-gray-400 p-2">本次未产生结构化取数依据。</div>;
  }
  return (
    <div className="mt-1 p-2 border rounded bg-gray-50 text-gray-600 leading-relaxed">
      {rows.map(([k, v], idx) =>
        v && v !== "—" ? (
          <div key={idx} className="flex gap-2">
            <span className="text-gray-400 shrink-0 w-16">{k}</span>
            <span className="break-all">{v}</span>
          </div>
        ) : null
      )}
    </div>
  );
}

export default TessChatDrawer;
