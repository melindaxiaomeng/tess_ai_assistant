// 演示用样例数据（与 tess-test-frontend.html 中的 SAMPLE 同源）。
// - SAMPLE_INPUT：算法层注入的「可信输入」（severity / calculated_loss 由算法算好，LLM 只能消费）。
// - MOCK_OUTPUT：LLM 归一化后的「理想输出」（只贡献文字叙事 + primary_contributor_id）。
// 二者字段严格对齐 TESS_INPUT_SCHEMA / TESS_OUTPUT_SCHEMA（见 tess_backend/contracts.py）。

export const SAMPLE_INPUT = {
  anomaly_metadata: {
    event_id: "ERR-20260728-0912",
    trigger_time: "2026-07-28 14:00:00",
    target_metric: "Overall Margin",
    current_value: "3.8%",
    benchmark_value: "14.2%",
    severity: "HIGH",
    calculated_loss: {
      loss_per_hour_usd: 350.0,
      calculation_basis: "基于过去 30 分钟 Cost 速率与缺失 Revenue 的差值计算",
    },
  },
  top_contributors: [
    {
      dimension_type: "Publisher",
      dimension_value: "Pub_Media_802",
      impact_share: "82%",
      metric_change: "Margin 从 15.1% 降至 -2.4%",
    },
  ],
  associated_signals: [
    {
      source: "Config_Change_Log",
      status: "INFO",
      detail: "13:25 运营将 Pub_Media_802 的 Postback 映射从 'default_v2' 切换为 'new_exp_v1'（CHG-4821）。",
    },
    {
      source: "AppsFlyer_Pull_API",
      status: "ERROR",
      detail: "13:25-14:00 期间该 Publisher Postback 接口持续 HTTP 504 占比 92%，与毛利断崖时间点吻合。",
    },
  ],
};

export const MOCK_OUTPUT = {
  status: "DIAGNOSED",
  confidence: 0.92,
  summary:
    "Pub_Media_802 于 13:25 被切换 Postback 映射至 new_exp_v1 后，AppsFlyer 回传持续 HTTP 504（占比 92%），导致该 Publisher 毛利从 15.1% 断崖至 -2.4%，为本次异常主因。",
  primary_contributor_id: "Pub_Media_802",
  root_cause_analysis: {
    primary_factor: "Postback 映射配置变更（CHG-4821）",
    causal_chain: [
      "13:25 运营将 Pub_Media_802 的 Postback 映射由 default_v2 切换为 new_exp_v1",
      "切换后 AppsFlyer 回传接口持续 HTTP 504（13:25-14:00 占比 92%）",
      "回传缺失导致 Revenue 无法计入，毛利从 15.1% 断崖至 -2.4%",
      "该 Publisher 贡献了 82% 的异常影响，确认为主因",
    ],
  },
};
