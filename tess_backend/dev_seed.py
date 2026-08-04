"""P7 开发用演示数据 —— 供前端联调 / 演示环境灌入 tess_alerts 库。

同一份数据被两处复用，避免漂移：
  - seed_demo_alerts.py（本地脚本：直接写 SQLite/Postgres）
  - app.py 的 POST /tess/dev/seed-demo（运行时经 API 灌库，便于无服务器访问权限时也能造数）

数据覆盖维度（方便前端验证各种 UI 态）：
  source:   anomaly-warning（5 条） + realtime-kpi（3 条）
  status:   DIAGNOSED / DIAGNOSED_SUSPECT / INCONCLUSIVE 三态齐全
  severity: HIGH / MEDIUM / LOW 三档齐全
注意：top_contributors 塞进 anomaly_metadata（alerts 端点只回 diagnosis + anomaly_metadata，
      抽屉组件要的 top_contributors 需从 anomaly_metadata 取）。
"""

from __future__ import annotations

ANOMALY_WARNINGS = [
    {
        "event_id": "AW-CAMP-7028915",
        "anomaly_metadata": {
            "event_id": "AW-CAMP-7028915",
            "severity": "HIGH",
            "trigger_time": "2026-08-03 14:00",
            "target_metric": "Revenue",
            "current_value": 540.20,
            "benchmark_value": 1820.70,
            "drop_ratio": 0.70,
            "calculated_loss": {"loss_per_hour_usd": 1280.50, "calculation_basis": "近 1 小时营收缺口 × 预估持续时长"},
            "campaign_id": 7028915,
            "publisher_id": 1000571,
            "top_contributors": [
                {"dimension_type": "campaign", "dimension_value": "7028915",
                 "impact_share": "primary", "metric_change": "营收 540.20 较昨日 1820.70 下跌 70.3%"},
                {"dimension_type": "publisher", "dimension_value": "1000571",
                 "impact_share": "0.82", "metric_change": "facemoji dsp 渠道贡献主要跌幅"},
            ],
        },
        "diagnosis": {
            "status": "DIAGNOSED_SUSPECT",
            "confidence": 0.72,
            "summary": "campaign 7028915 营收较昨日同段下跌约 70%，且高度集中在 facemoji dsp（publisher 1000571）渠道，疑似该渠道出价策略异常或转化追踪链路断裂。缺少第三方报错日志直接佐证，暂列疑似。",
            "primary_contributor_id": "7028915",
            "root_cause_analysis": {
                "primary_factor": "渠道出价/追踪异常",
                "causal_chain": [
                    "昨日同段营收稳定（1820.7）→ 今日骤降至 540.2",
                    "降幅集中在 facemoji dsp 单渠道（impact_share 0.82）",
                    "疑似该渠道出价被调低 / 追踪 SDK 回传丢失",
                    "需运营比对渠道后台出价与回传日志确认",
                ],
            },
        },
    },
    {
        "event_id": "AW-CAMP-6880231",
        "anomaly_metadata": {
            "event_id": "AW-CAMP-6880231",
            "severity": "HIGH",
            "trigger_time": "2026-08-03 13:30",
            "target_metric": "Conversions",
            "current_value": 210.00,
            "benchmark_value": 1340.00,
            "drop_ratio": 0.84,
            "calculated_loss": {"loss_per_hour_usd": 2310.00, "calculation_basis": "转化缺口 × 客单价"},
            "campaign_id": 6880231,
            "publisher_id": 1000233,
            "top_contributors": [
                {"dimension_type": "publisher", "dimension_value": "1000233",
                 "impact_share": "primary", "metric_change": "转化 210 较昨日 1340 下跌 84%"},
            ],
        },
        "diagnosis": {
            "status": "DIAGNOSED",
            "confidence": 0.93,
            "summary": "campaign 6880231 转化量下跌 84%，根因明确：publisher 1000233 在 13:10 完成 SDK 升级后转化回传丢失，平台侧转化计数归零。已比对发布时间线与回传缺口高度吻合。",
            "primary_contributor_id": "1000233",
            "root_cause_analysis": {
                "primary_factor": "SDK 升级导致转化回传丢失",
                "causal_chain": [
                    "13:10 publisher 1000233 上线新版 SDK",
                    "13:10 后该 publisher 转化回传中断",
                    "campaign 6880231 转化量同步从 1340 跌至 210",
                    "rollback SDK 或重配回传即可恢复",
                ],
            },
        },
    },
    {
        "event_id": "AW-CAMP-7011223",
        "anomaly_metadata": {
            "event_id": "AW-CAMP-7011223",
            "severity": "MEDIUM",
            "trigger_time": "2026-08-03 12:00",
            "target_metric": "Revenue",
            "current_value": 980.00,
            "benchmark_value": 1180.00,
            "drop_ratio": 0.17,
            "calculated_loss": {"loss_per_hour_usd": 200.00, "calculation_basis": "缺口较小，估算区间宽"},
            "campaign_id": 7011223,
            "publisher_id": 1000880,
            "top_contributors": [
                {"dimension_type": "campaign", "dimension_value": "7011223",
                 "impact_share": "primary", "metric_change": "营收波动 -17%，处于正常抖动区间"},
            ],
        },
        "diagnosis": {
            "status": "INCONCLUSIVE",
            "confidence": 0.0,
            "summary": "当前信号不足或触发校验熔断，已自动切换为人工排查：营收仅小幅波动 17%，未达异常阈值，建议持续观察。",
            "root_cause_analysis": {
                "primary_factor": "暂无法明确根因",
                "causal_chain": [],
            },
        },
    },
    {
        "event_id": "AW-CAMP-7099881",
        "anomaly_metadata": {
            "event_id": "AW-CAMP-7099881",
            "severity": "LOW",
            "trigger_time": "2026-08-03 11:00",
            "target_metric": "Clicks",
            "current_value": 320.00,
            "benchmark_value": 540.00,
            "drop_ratio": 0.41,
            "calculated_loss": {"loss_per_hour_usd": 42.00, "calculation_basis": "小投放，绝对损失低"},
            "campaign_id": 7099881,
            "publisher_id": 1000501,
            "top_contributors": [
                {"dimension_type": "campaign", "dimension_value": "7099881",
                 "impact_share": "primary", "metric_change": "点击 320 较昨日 540 下跌 41%"},
            ],
        },
        "diagnosis": {
            "status": "DIAGNOSED_SUSPECT",
            "confidence": 0.61,
            "summary": "campaign 7099881 为小投放计划，点击下跌 41% 但绝对损失低（约 42 USD/h）。疑似定向受众在今日时段的覆盖收窄，建议观察下一时段是否恢复。",
            "primary_contributor_id": "7099881",
            "root_cause_analysis": {
                "primary_factor": "受众覆盖时段性收窄",
                "causal_chain": [
                    "小投放计划日预算有限",
                    "今日 11 时段受众在线密度下降",
                    "点击量随之回落，绝对值影响小",
                ],
            },
        },
    },
    {
        "event_id": "AW-CAMP-6844120",
        "anomaly_metadata": {
            "event_id": "AW-CAMP-6844120",
            "severity": "HIGH",
            "trigger_time": "2026-08-03 10:00",
            "target_metric": "Revenue",
            "current_value": 120.00,
            "benchmark_value": 1500.00,
            "drop_ratio": 0.92,
            "calculated_loss": {"loss_per_hour_usd": 1380.00, "calculation_basis": "营收缺口 × 持续时长"},
            "campaign_id": 6844120,
            "publisher_id": 1000233,
            "top_contributors": [
                {"dimension_type": "campaign", "dimension_value": "6844120",
                 "impact_share": "primary", "metric_change": "营收 120 较昨日 1500 下跌 92%"},
            ],
        },
        "diagnosis": {
            "status": "DIAGNOSED",
            "confidence": 0.88,
            "summary": "campaign 6844120 营收几近归零，已确认根因为该计划预算在 09:50 耗尽并暂停投放，非系统故障。运营已手动充值恢复。",
            "primary_contributor_id": "6844120",
            "root_cause_analysis": {
                "primary_factor": "预算耗尽导致计划暂停",
                "causal_chain": [
                    "09:50 计划日预算耗尽自动暂停",
                    "营收从 1500 跌至 120",
                    "运营已充值并重新开启投放",
                ],
            },
        },
        # 这条用于演示「已处理」态
        "ack": {"resolution": "resolved", "acked_by": "alice", "note": "已充值恢复投放，确认无系统问题"},
    },
]

REALTIME_KPI = [
    {
        "event_id": "RT-HOUR-14",
        "anomaly_metadata": {
            "event_id": "RT-HOUR-14",
            "severity": "HIGH",
            "trigger_time": "hour 14",
            "target_metric": "Revenue",
            "current_value": 500.00,
            "benchmark_value": 1250.00,
            "drop_ratio": 0.60,
            "calculated_loss": {"loss_per_hour_usd": 750.00, "calculation_basis": "实时大盘营收缺口"},
            "top_contributors": [
                {"dimension_type": "Metric", "dimension_value": "Revenue",
                 "impact_share": "primary", "metric_change": "Revenue 500.0 较昨日 1250.0 下跌 60.0%"},
            ],
        },
        "diagnosis": {
            "status": "DIAGNOSED_SUSPECT",
            "confidence": 0.68,
            "summary": "实时大盘 14 时营收较昨日同段下跌 60%，多条核心计划同步回落，疑似平台级采集或结算延迟，建议核对实时管道。",
            "primary_contributor_id": "Revenue",
            "root_cause_analysis": {
                "primary_factor": "实时营收管道延迟/回落",
                "causal_chain": [
                    "14 时实时营收 500 远低于昨日 1250",
                    "多条计划同步下跌，非单点问题",
                    "疑似实时结算管道延迟或上游数据断流",
                ],
            },
        },
    },
    {
        "event_id": "RT-HOUR-13",
        "anomaly_metadata": {
            "event_id": "RT-HOUR-13",
            "severity": "MEDIUM",
            "trigger_time": "hour 13",
            "target_metric": "Revenue",
            "current_value": 880.00,
            "benchmark_value": 1420.00,
            "drop_ratio": 0.38,
            "calculated_loss": {"loss_per_hour_usd": 540.00, "calculation_basis": "实时大盘营收缺口"},
            "top_contributors": [
                {"dimension_type": "Metric", "dimension_value": "Revenue",
                 "impact_share": "primary", "metric_change": "Revenue 880.0 较昨日 1420.0 下跌 38.0%"},
            ],
        },
        "diagnosis": {
            "status": "DIAGNOSED_SUSPECT",
            "confidence": 0.64,
            "summary": "实时大盘 13 时营收较昨日下跌 38%，处于中等波动，需结合 14 时段判断是否持续恶化。",
            "primary_contributor_id": "Revenue",
            "root_cause_analysis": {
                "primary_factor": "时段性营收波动",
                "causal_chain": ["13 时营收 880 低于昨日 1420", "波动中等，持续观察"],
            },
        },
    },
    {
        "event_id": "RT-HOUR-15",
        "anomaly_metadata": {
            "event_id": "RT-HOUR-15",
            "severity": "LOW",
            "trigger_time": "hour 15",
            "target_metric": "Clicks",
            "current_value": 760000.00,
            "benchmark_value": 820000.00,
            "drop_ratio": 0.07,
            "calculated_loss": {"loss_per_hour_usd": 0.00, "calculation_basis": "点击微跌，影响可忽略"},
            "top_contributors": [
                {"dimension_type": "Metric", "dimension_value": "Clicks",
                 "impact_share": "primary", "metric_change": "Clicks 760k 较昨日 820k 下跌 7%"},
            ],
        },
        "diagnosis": {
            "status": "INCONCLUSIVE",
            "confidence": 0.0,
            "summary": "实时大盘 15 时仅点击微跌 7%，属正常抖动，未达异常阈值，已转人工观察。",
            "root_cause_analysis": {
                "primary_factor": "暂无法明确根因",
                "causal_chain": [],
            },
        },
    },
]


def build_demo_results() -> list:
    """构造可与 AlertStore.save_batch 直接对接的演示结果列表。

    每条：{event_id, diagnosis, meta:{source}, anomaly_metadata}
    """
    results = []
    for r in ANOMALY_WARNINGS:
        item = {
            "event_id": r["event_id"],
            "diagnosis": r["diagnosis"],
            "meta": {"source": "anomaly-warning"},
            "anomaly_metadata": r["anomaly_metadata"],
        }
        if "ack" in r:
            item["_ack"] = r["ack"]
        results.append(item)
    for r in REALTIME_KPI:
        results.append({
            "event_id": r["event_id"],
            "diagnosis": r["diagnosis"],
            "meta": {"source": "realtime-kpi"},
            "anomaly_metadata": r["anomaly_metadata"],
        })
    return results


# 全部演示 event_id（供 realtime-kpi 端点「置顶」合并，避免被 cron 新批次覆盖）。
# 单一事实来源：由上面两份列表推导，新增演示记录时无需手动维护。
DEMO_EVENT_IDS = [r["event_id"] for r in (ANOMALY_WARNINGS + REALTIME_KPI)]
