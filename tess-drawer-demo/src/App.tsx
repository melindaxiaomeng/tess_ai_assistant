import React, { useState } from "react";
import { TessDiagnosticDrawer } from "./components/TessDiagnosticDrawer";
import type { TessOutput, TessInput } from "./components/TessDiagnosticDrawer";
import { SAMPLE_INPUT, MOCK_OUTPUT } from "./sample";

// 后端返回的归一化结果直接作为 llmOutput；原始输入作为 inputData。
// 渲染屏障：Drawer 内所有数值 / severity / 路由都来自 inputData，LLM 只贡献文字叙事。

export default function App() {
  const [backend, setBackend] = useState("https://8.141.113.22:8443");
  const [apiKey, setApiKey] = useState("");
  const [input, setInput] = useState(JSON.stringify(SAMPLE_INPUT, null, 2));

  // Drawer 当前展示的数据
  const [llmOutput, setLlmOutput] = useState<TessOutput>(MOCK_OUTPUT as TessOutput);
  const [inputData, setInputData] = useState<TessInput>(SAMPLE_INPUT as TessInput);

  const [health, setHealth] = useState<{ ok?: boolean; text?: string }>({});
  const [loading, setLoading] = useState(false);
  const [logs, setLogs] = useState<string[]>([]);
  const [tamper, setTamper] = useState(false); // 模拟 LLM 注入伪造字段

  // 数据分析（主动式 BI）状态
  const [analysisReport, setAnalysisReport] = useState<string>("");
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisType, setAnalysisType] = useState<string>("");

  const appendLog = (m: string) =>
    setLogs((l) => [...l, `[${new Date().toLocaleTimeString()}] ${m}`]);

  // 浏览器 Mixed Content 限制：HTTPS 页面无法 fetch HTTP 后端。
  // 从公网预览页连 http://IP:8080 会被拦截；本地 npm run dev（HTTP 页面）不受影响。
  const blockedByMixedContent = (): boolean => {
    if (
      typeof window !== "undefined" &&
      window.location.protocol === "https:" &&
      backend.startsWith("http://")
    ) {
      appendLog(
        "⚠️ 当前预览页是 HTTPS，浏览器禁止向 HTTP 后端发请求（Mixed Content 限制）。" +
          "请从本地运行 npm run dev 验证真诊断，或为后端启用 HTTPS。"
      );
      return true;
    }
    return false;
  };

  // 自签证书场景下，fetch 因证书不受信任而失败时给出可操作提示。
  const certHint = (): string => {
    if (
      backend.startsWith("https://") &&
      typeof window !== "undefined" &&
      window.location.protocol === "https:"
    ) {
      return `（若提示证书/网络错误，请先在浏览器新标签页打开 ${backend}/healthz 并手动信任自签证书）`;
    }
    return "";
  };

  async function healthCheck() {
    if (blockedByMixedContent()) return;
    try {
      const base = backend.replace(/\/+$/, "");
      const res = await fetch(`${base}/healthz`, {
        headers: apiKey ? { "X-API-Key": apiKey } : {},
      });
      const d = await res.json();
      setHealth({ ok: res.ok, text: `OK · v${d.version} · LLM已配置:${d.llm_configured}` });
      appendLog("健康检查通过：" + JSON.stringify(d));
    } catch (e: any) {
      setHealth({ ok: false, text: "连接失败" });
      appendLog("健康检查失败：" + e.message + certHint());
    }
  }

  async function sendDiagnose() {
    if (blockedByMixedContent()) return;
    let payload: any;
    try {
      payload = JSON.parse(input);
    } catch (e: any) {
      return appendLog("输入 JSON 解析失败：" + e.message);
    }
    setLoading(true);
    appendLog("POST /tess/diagnose …");
    try {
      const base = backend.replace(/\/+$/, "");
      const res = await fetch(`${base}/tess/diagnose`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiKey ? { "X-API-Key": apiKey } : {}),
        },
        body: JSON.stringify(payload),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${d.detail || res.statusText}`);
      setLlmOutput(d);
      setInputData(payload);
      appendLog("诊断返回 status=" + d.status);
    } catch (e: any) {
      appendLog("诊断失败：" + e.message + certHint());
    } finally {
      setLoading(false);
    }
  }

  function offlineDemo() {
    setLlmOutput(MOCK_OUTPUT as TessOutput);
    setInputData(SAMPLE_INPUT as TessInput);
    appendLog("离线演示：使用内置样例渲染（无需后端）");
  }

  // 主动式 BI：一键触发数据分析（六类场景）
  const ANALYTICS_TYPES = [
    { key: "daily_summary", label: "📊 昨日大盘复盘" },
    { key: "scaling_opportunity", label: "🚀 扩量潜力挖掘" },
    { key: "finance_check", label: "💰 本月对账差异" },
    { key: "account_overview", label: "🏢 账户全景概览" },
    { key: "publisher_deepdive", label: "🔍 渠道质量对比" },
    { key: "scaling_capacity", label: "📈 放量容量评估" },
  ];

  async function runAnalytics(type: string) {
    if (blockedByMixedContent()) return;
    setAnalysisType(type);
    setAnalysisLoading(true);
    setAnalysisReport("");
    appendLog(`POST /tess/analytics type=${type} …`);
    try {
      const base = backend.replace(/\/+$/, "");
      const res = await fetch(`${base}/tess/analytics`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiKey ? { "X-API-Key": apiKey } : {}),
        },
        body: JSON.stringify({ analysis_type: type, params: { report_month: "2026-08" } }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${d.detail || res.statusText}`);
      setAnalysisReport(d.report || "（无返回内容）");
      appendLog("分析返回字数=" + (d.report || "").length + " · errors=" + JSON.stringify(d.context_summary?.errors || []));
    } catch (e: any) {
      appendLog("分析失败：" + e.message + certHint());
      setAnalysisReport("⚠️ 分析失败：" + e.message);
    } finally {
      setAnalysisLoading(false);
    }
  }

  // 模拟 LLM 试图篡改：给 llmOutput 注入伪造的 severity / calculated_loss / 非法结论。
  // Drawer 只信 inputData，所以这些伪造值应被完全忽略。
  const shownOutput: TessOutput = tamper
    ? ({
        ...llmOutput,
        severity: "LOW",
        calculated_loss: { loss_per_hour_usd: 0.01 },
        root_cause_analysis: {
          primary_factor: llmOutput.root_cause_analysis?.primary_factor || "",
          causal_chain: [
            ...(llmOutput.root_cause_analysis?.causal_chain || []),
            "👿 [LLM伪造] 已悄悄把损失改为 $0.01 并把严重度降为 LOW",
          ],
        },
      } as unknown as TessOutput)
    : llmOutput;

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="bg-gradient-to-r from-blue-900 to-blue-600 text-white px-6 py-4">
        <h1 className="text-lg font-semibold">Tess 智能归因诊断抽屉 · 演示</h1>
        <p className="text-xs opacity-80 mt-1">
          把 <code>tess_drawer.tsx</code> 渲染屏障组件跑起来 · 支持连接 Tess 后端 / 离线演示
        </p>
      </header>

      {/* 后端配置条 */}
      <div className="bg-white border-b border-gray-200 px-6 py-3 flex flex-wrap items-center gap-3 text-sm">
        <label className="text-gray-500">后端地址</label>
        <input
          className="flex-1 min-w-[260px] border border-gray-300 rounded-md px-3 py-1.5 font-mono text-xs"
          value={backend}
          onChange={(e) => setBackend(e.target.value)}
          placeholder="http://<Tess服务器IP>:8080"
        />
        <label className="text-gray-500">API Key</label>
        <input
          className="w-[200px] border border-gray-300 rounded-md px-3 py-1.5 font-mono text-xs"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="可选（留空=不鉴权）"
        />
        <button
          className="border border-blue-600 text-blue-600 rounded-md px-3 py-1.5 hover:bg-blue-50"
          onClick={healthCheck}
        >
          健康检查
        </button>
        <span
          className={`w-2.5 h-2.5 rounded-full ${
            health.ok === undefined
              ? "bg-gray-300"
              : health.ok
              ? "bg-green-500"
              : "bg-red-500"
          }`}
        />
        <span className="text-gray-500">{health.text || "未检测"}</span>
      </div>

      {/* 主体 */}
      <div className="flex-1 grid grid-cols-1 lg:grid-cols-2 gap-4 p-6">
        {/* 左：控制面板 */}
        <div className="space-y-4">
          <div className="bg-white border border-gray-200 rounded-lg p-4">
            <h3 className="text-sm font-semibold mb-2">
              诊断输入（算法层注入的可信 Input）
            </h3>
            <textarea
              className="w-full h-72 border border-gray-300 rounded-md p-3 font-mono text-xs resize-y"
              value={input}
              onChange={(e) => setInput(e.target.value)}
            />
            <div className="flex flex-wrap gap-2 mt-3">
              <button
                className="bg-blue-600 hover:bg-blue-700 text-white rounded-md px-4 py-2 text-sm disabled:opacity-50"
                onClick={sendDiagnose}
                disabled={loading}
              >
                {loading ? "诊断中…" : "▶ 连接后端诊断"}
              </button>
              <button
                className="border border-blue-600 text-blue-600 rounded-md px-4 py-2 text-sm hover:bg-blue-50"
                onClick={offlineDemo}
              >
                ▶ 离线演示
              </button>
              <button
                className="border border-gray-300 text-gray-600 rounded-md px-4 py-2 text-sm hover:bg-gray-50"
                onClick={() => setInput(JSON.stringify(SAMPLE_INPUT, null, 2))}
              >
                载入样例
              </button>
            </div>
          </div>

          {/* 数据分析（主动式 BI 助手） */}
          <div className="bg-white border border-gray-200 rounded-lg p-4">
            <h3 className="text-sm font-semibold mb-2">数据分析 · 主动式 BI 助手</h3>
            <div className="flex flex-wrap gap-2 mb-3">
              {ANALYTICS_TYPES.map((t) => (
                <button
                  key={t.key}
                  className="border border-indigo-600 text-indigo-600 rounded-full px-3 py-1.5 text-xs hover:bg-indigo-50 disabled:opacity-50"
                  onClick={() => runAnalytics(t.key)}
                  disabled={analysisLoading}
                >
                  {t.label}
                </button>
              ))}
            </div>
            {analysisLoading && (
              <div className="text-xs text-gray-500">
                分析中…（拉取 Teensing 业务接口 + LLM 生成简报）
                {analysisType ? ` · ${analysisType}` : ""}
              </div>
            )}
            {analysisReport && (
              <pre className="text-xs whitespace-pre-wrap bg-slate-50 border border-gray-200 rounded-md p-3 max-h-80 overflow-auto">
                {analysisReport}
              </pre>
            )}
          </div>

          {/* 渲染屏障演示开关 */}
          <div className="bg-amber-50 border-l-4 border-amber-400 rounded-r-lg p-4 text-sm">
            <label className="flex items-start gap-2 cursor-pointer">
              <input
                type="checkbox"
                className="mt-1"
                checked={tamper}
                onChange={(e) => setTamper(e.target.checked)}
              />
              <span>
                <b>模拟 LLM 篡改攻击</b>（演示渲染屏障）：勾选后，我们强行往 LLM 返回里塞入
                <code className="mx-1 px-1 bg-white rounded">severity=LOW</code>、
                <code className="mx-1 px-1 bg-white rounded">loss=$0.01</code>
                ，并在因果链追加伪造结论。观察右侧 Drawer：
                <b>这些伪造值一律不生效</b>，仍显示 inputData 的
                <code className="mx-1 px-1 bg-white rounded">HIGH / $350</code>。
              </span>
            </label>
          </div>

          {/* 日志 */}
          <div className="bg-slate-900 text-slate-200 rounded-lg p-3 text-xs font-mono h-40 overflow-auto whitespace-pre-wrap">
            {logs.length === 0 ? "// 操作日志" : logs.join("\n")}
          </div>
        </div>

        {/* 右：Drawer 预览 */}
        <div>
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden h-full">
            <div className="px-4 py-2 border-b bg-gray-50 text-xs text-gray-500">
              抽屉预览（TessDiagnosticDrawer 组件实际渲染）
            </div>
            <TessDiagnosticDrawer llmOutput={shownOutput} inputData={inputData} />
          </div>
        </div>
      </div>

      {/* 底部说明 */}
      <footer className="px-6 py-3 text-xs text-gray-400 border-t">
        渲染屏障（PRD V2.0.0 §7）：所有数值 / Severity / 路由均来自可信 inputData，LLM
        仅贡献定性叙事。即使模型返回伪造字段，前端也只用算法层数据强渲染。
      </footer>
    </div>
  );
}
