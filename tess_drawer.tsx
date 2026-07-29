/**
 * Tess 智能归因诊断抽屉 (Drawer)
 * ---------------------------------------------------------------
 * 对应 PRD V2.0.0 §7（前端屏障层 / 渲染屏障）。
 *
 * 三层死锁在此体现：
 *  - 所有 **数值 / Severity / 路由** 均来自可信的 inputData（算法层算好），
 *    LLM 只贡献定性叙事（summary + causal_chain）与 primary_contributor_id。
 *  - LLM 敢返回的数字/URL 一律不渲染，由前端用 inputData 强渲染，
 *    彻底斩断「模型篡改数字 / 注入非法路由」的可能。
 *
 * 这是一个自包含组件，可直接复制进 Teensing 前端工程。
 * 依赖：React 18+。样式使用 Tailwind 类名（可按你们设计令牌替换）。
 */

import React from "react";

// ---------------------------------------------------------------------------
// 类型（与后端 TESS_OUTPUT_SCHEMA / TESS_INPUT_SCHEMA 对齐）
// ---------------------------------------------------------------------------

export type TessStatus = "DIAGNOSED" | "DIAGNOSED_SUSPECT" | "INCONCLUSIVE";

export interface TessRootCause {
  primary_factor: string;
  causal_chain: string[];
}

export interface TessOutput {
  status: TessStatus;
  confidence: number; // 0.00 - 1.00
  summary: string;
  primary_contributor_id?: string | null;
  root_cause_analysis?: TessRootCause;
}

export interface Contributor {
  dimension_type: string;
  dimension_value: string;
  impact_share?: string;
  metric_change?: string;
}

export interface AnomalyMetadata {
  severity?: string;
  calculated_loss?: { loss_per_hour_usd?: number; calculation_basis?: string };
  [key: string]: unknown;
}

export interface TessInput {
  anomaly_metadata?: AnomalyMetadata;
  top_contributors?: Contributor[];
  [key: string]: unknown;
}

export interface TessDrawerProps {
  llmOutput: TessOutput;
  inputData: TessInput;
  onCallHuman?: () => void;
}

// ---------------------------------------------------------------------------
// 路由安全映射表（大小写不敏感匹配，杜绝大小写耦合）
// LLM 绝不拼接 URL —— 前端用 dimension_type 查表 + ID 拼参数。
// ---------------------------------------------------------------------------

const MODULE_ROUTE_MAP: Record<string, string> = {
  publisher: "/publisher/mapping-list?id=",
  advertiser: "/advertiser/detail?id=",
  campaign: "/campaign/overview?id=",
};

// ---------------------------------------------------------------------------
// 组件
// ---------------------------------------------------------------------------

export const TessDiagnosticDrawer: React.FC<TessDrawerProps> = ({
  llmOutput,
  inputData,
  onCallHuman,
}) => {
  const {
    status,
    confidence,
    summary,
    primary_contributor_id,
    root_cause_analysis,
  } = llmOutput;

  const anomaly_metadata = inputData.anomaly_metadata ?? {};
  const top_contributors = inputData.top_contributors ?? [];

  // 算法层确定的 Severity & 精确损耗（空值守卫）
  const severity = anomaly_metadata.severity ?? "UNKNOWN";
  const lossPerHour = anomaly_metadata.calculated_loss?.loss_per_hour_usd ?? 0;

  // 根据 LLM 决定的 ID 动态匹配 Input 中的真实统计对象（修 [0] 位错位）
  const matchedContributor =
    top_contributors.find(
      (c) => c.dimension_value === primary_contributor_id
    ) ?? top_contributors[0];

  const isSuspect = status === "DIAGNOSED_SUSPECT";
  const isInconclusive = status === "INCONCLUSIVE";

  // 大小写不敏感的路由获取
  const dimTypeKey = (
    matchedContributor?.dimension_type ?? ""
  ).toLowerCase();
  const routeBase = MODULE_ROUTE_MAP[dimTypeKey] ?? "/overview?id=";

  return (
    <div className="tess-drawer-content p-4 space-y-4">
      {/* 1. Header：Severity 业务标签 + Tess 置信度数字 */}
      <div className="flex items-center justify-between border-b pb-3">
        <div className="flex items-center gap-2">
          <span
            className={`px-2 py-1 text-xs font-bold rounded severity-badge ${severity.toLowerCase()}`}
          >
            [{severity}]
          </span>
          <h3 className="font-semibold text-lg">Tess 智能归因诊断</h3>
        </div>
        <div className="text-sm text-gray-500">
          置信度:{" "}
          <span className="font-mono font-bold">
            {(confidence * 100).toFixed(0)}%
          </span>
        </div>
      </div>

      {/* 2. 三态 Banner */}
      {isSuspect && (
        <div className="p-3 bg-yellow-50 border-l-4 border-yellow-400 text-yellow-800 text-sm">
          <b>中置信度诊断：</b> 维度数据高度集中，但缺少第三方 API
          报错日志直接佐证，请谨慎核实。
        </div>
      )}
      {isInconclusive && (
        <div className="p-3 bg-gray-100 border-l-4 border-gray-500 text-gray-700 text-sm">
          <b>归因无法确定：</b>{" "}
          当前日志信号不足或触发了校验熔断，已自动切换为人工排查流。
        </div>
      )}

      {/* 3. Tess 核心诊断叙事区（LLM 只贡献这部分文字） */}
      <div className="bg-slate-50 p-4 rounded-lg space-y-3">
        <h4 className="text-sm font-bold text-slate-700">诊断结论</h4>
        <p className="text-slate-900 font-medium text-sm leading-relaxed">
          {summary}
        </p>
        {root_cause_analysis?.causal_chain?.length ? (
          <div className="pt-2">
            <h5 className="text-xs font-semibold text-slate-500 mb-2">
              推导因果链条 (Causal Chain):
            </h5>
            <ol className="list-decimal list-inside space-y-1 text-xs text-slate-700">
              {root_cause_analysis.causal_chain.map((step, idx) => (
                <li key={idx} className="pl-1">
                  {step}
                </li>
              ))}
            </ol>
          </div>
        ) : null}
      </div>

      {/* 4. 物理数据卡片区：所有数值纯读 inputData，LLM 绝不触碰数字 */}
      {!isInconclusive && matchedContributor && (
        <div className="border p-4 rounded-lg bg-white space-y-2 text-sm">
          <div className="flex justify-between">
            <span className="text-gray-500">核心受影响维度:</span>
            <span className="font-semibold">
              {matchedContributor.dimension_value} (
              {matchedContributor.dimension_type})
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">该维度异常贡献率:</span>
            <span className="font-semibold text-amber-600">
              {matchedContributor.impact_share ?? "—"}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">预估损失速率:</span>
            <span className="font-semibold text-red-600">
              ${(Number(lossPerHour) || 0).toFixed(2)} / 小时
            </span>
          </div>
        </div>
      )}

      {/* 5. 动作触发区：路由安全拼接，拒绝 LLM 注入 */}
      <div className="pt-2">
        {!isInconclusive && matchedContributor ? (
          <a
            href={`${routeBase}${matchedContributor.dimension_value}`}
            className="block w-full text-center bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 rounded-md transition-colors"
          >
            前往 {matchedContributor.dimension_type} 配置页面一键处置
          </a>
        ) : (
          <button
            className="w-full bg-slate-800 hover:bg-slate-900 text-white font-medium py-2 rounded-md transition-colors"
            onClick={() =>
              onCallHuman ? onCallHuman() : alert("已发起即时通讯呼叫，通知值班运维！")
            }
          >
            一键呼叫值班运维 / 派发飞书排查群
          </button>
        )}
      </div>
    </div>
  );
};

export default TessDiagnosticDrawer;
