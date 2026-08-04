# 六类 BI 分析简报对照（真实数据 + 真实 LLM 生成）

> 生成方式：本地 `verify_analytics.py` 模式（真实 Teensing 接口 + DeepSeek `deepseek-chat`），无任何模拟数据。

> 生成时间：2026-08-04


---

## 📊 昨日大盘复盘  (`daily_summary`)

**底层接口**：GET /overview/daily-kpi + /overview/ranking + /overview/ranking/fluctuation


📊 **数据复盘 / 洞察摘要**
- 昨日（2026-07-29）整体利润 $13,048.46，环比 +3.59%，Revenue $28,398.42（+2.56%），增长主要由头部 Campaign 拉动，整体大盘健康上行。
- 头部 5 个 Campaign 合计 Revenue $22,125.61，占总 Revenue 的 77.9%；其中 xiaomi2（$9,827.10，Margin 41.4%）与 oppo-mmp-Betty（$3,345.47，Margin 55.67%）贡献最大增量，两者 Revenue 环比分别 +$681.21 与 +$244.52。

💡 **潜能点 / 风险点**
- 潜能点（oppo-mmp-Betty）：CVR 高达 0.76%，Margin 55.67% 为头部最高，且 Revenue 环比 +$244.52（+7.9%），说明流量质量与变现效率俱佳，具备放量空间。
- 风险点（shareit勿绑_joy）：头部唯一 Revenue 环比下滑（-$85.32，-3.0%），CVR 1.85% 虽高但 Margin 仅 41.41%，且其子 Campaign "ru.yandex.music_RU" 单日 Revenue 下降 $75.3（-43%），需核查是否为流量波动或扣量问题。
- 风险点（falling_losers 整体）：前三跌幅 Campaign 合计 Revenue 损失 $338.4，其中 "S-encm-yandex.browser-RU"（-193.9）与 "encm-ff111aab-BD"（-69.3）均属 xiaomi-1000220-cora，该广告主虽整体上涨，但部分计划波动大，需关注稳定性。

🚀 **推荐执行动作 (Recommended Actions)**
1. 针对 oppo-mmp-Betty（advertiser_id 1000839），建议将 Cap 上调 20-30%（当前日 Revenue $3,345，可尝试提至 $4,000-$4,300），利用其高 CVR（0.76%）与高 Margin（55.67%）扩大利润。
2. 针对 shareit勿绑_joy（advertiser_id 1000439）及 "ru.yandex.music_RU" Campaign，建议核查近 24h 流量来源与扣量情况，若确认无异常可维持；若持续下滑，建议将预算向 oppo-mmp-Betty 或 appnext_Jason 倾斜。
3. 针对 xiaomi-1000220-cora 的亏损计划（"S-encm-yandex.browser-RU" 与 "encm-ff111aab-BD"），建议暂停或下调出价 10-15%，并将预算转移至其上升计划 "recl-2Gis-RU(mipicks)"（新增 $203.7，CVR 0.13% 偏低但增长快）。


---

## 🚀 扩量潜力挖掘  (`scaling_opportunity`)

**底层接口**：GET /report(campaign,publisher 按 profit 降序) + /campaign-quality/publisher


📊 **数据复盘 / 洞察摘要**
- 近7天利润最高的 Campaign 为 **Nike MX iOS CPA s2s 1104**（Publisher: adot-jason），贡献利润 **$1,818.9**，Margin **56.97%**，Revenue $3,193，是当前最核心的利润引擎。
- 紧随其后的是 **sports.caliente.mx.calientedeportes_MX**（hopemobi-vicky），利润 **$1,040.2**，Margin 47.58%，CVR 4.67%，量级与 Nike 接近（Clicks 66,905 vs 76,581），但单价较低（$0.7 vs $1.0）。
- **Алиса AI ассистент**（doubleint-xdj-jason）表现最亮眼：CVR 高达 **19.23%**（行业显著偏高），Margin **68.79%** 为五者中最高，利润 $901.98，具备极强放量潜力。

💡 **潜能点 / 风险点**
- **潜能点 - Алиса AI ассистент**：CVR 19.23% 远超其他 Campaign（4-8%区间），且 Margin 68.79% 最高，说明转化质量与利润空间俱佳。当前仅 26,221 clicks，建议优先扩容流量入口，预计利润弹性最大。
- **潜能点 - Nike MX iOS CPA**：单价 $1.0 为最高，CVR 4.17% 处于中位，但利润绝对值第一（$1,818.9）。该 Campaign 已具备规模效应，可尝试小幅提价或增加 Publisher 覆盖以进一步放量。
- **风险点 - com.vitastudio.mahjong_RU**：Clicks 高达 96,328（五者中最大），但 CVR 仅 3.42%，ECPC $4.67 为最低，说明流量质量偏弱、转化效率低。虽然 Margin 62.09% 尚可，但若继续放量需警惕边际利润下滑。

🚀 **推荐执行动作 (Recommended Actions)**
1. 针对 **Алиса AI ассистент**（doubleint-xdj-jason），建议 **加大 Cap 50%**（当前 26k clicks → 目标 40k+），并优先分配预算，利用其 19.23% CVR 与 68.79% Margin 快速拉升利润。
2. 针对 **Nike MX iOS CPA**（adot-jason），建议 **增加 1-2 个同类型 Publisher** 复制该 Campaign 模式，或与 adot-jason 协商 **提高单价至 $1.1**（当前 $1.0），在保持 CVR 稳定的前提下扩大 Revenue。
3. 针对 **com.vitastudio.mahjong_RU**（Shareit reseller），建议 **核查流量质量**（CVR 3.42% 偏低），若确认存在低质流量，可 **降低出价 10-15%** 或暂停部分子渠道，避免无效消耗；同时关注 postback 回传率（1,798/3,294=54.6%），确认归因口径无异常。


---

## 💰 本月对账差异  (`finance_check`)

**底层接口**：GET /report/month（revenue vs calc_revenue）


📊 **数据复盘 / 洞察摘要**
- 本月（2026-08）报表中 revenue、calc_revenue、payout、calc_payout 均为 0，对账差异为 0，但核心业务指标（转化数、收入、支出）全部为空值，属于无有效业务数据月份。
- 对账字段（revenue vs calc_revenue、payout vs calc_payout）完全一致（均为 0），不存在差异项（discrepancy_items 为空），但该一致性建立在“无数据”基础上，不代表真实业务健康。

💡 **潜能点 / 风险点**
- 数据缺失风险：total_summary 中 conversions、postback_conversions、scrub_conversions 均为 0，且 sample_items 为空，说明本月无转化回传或未接入数据源。可能原因：① 报表口径未同步（如时区/币种问题）；② 广告投放未起量或追踪链接未生效；③ 数据管道故障导致漏采。
- 对账“零差异”假象：由于 calc_revenue 与 revenue 同为 0，差异率无法计算（分母为 0），该月份对账结论不具备参考价值，需优先排查数据采集链路而非业务表现。

🚀 **推荐执行动作 (Recommended Actions)**
1. 针对 [数据管道/回传系统]，建议立即核查 postback 与 S2S 回传日志，确认 2026-08 期间是否有转化事件被静默丢弃（重点检查服务器端错误码与超时重试机制）。
2. 针对 [报表口径]，建议与工程/数据团队对齐 revenue 与 calc_revenue 的统计时点与币种换算规则，补充缺失的 sample_items 抽样明细，否则下月对账仍无法定位差异根因。
3. 针对 [投放侧]，若确认数据链路正常，则需复盘本月 Campaign 是否因预算/定向问题导致零消耗，建议拉取广告平台侧消耗与点击数据交叉验证，避免“假零”掩盖真实投放异常。


---

## 🏢 账户全景  (`account_overview`)

**底层接口**：GET /campaigns + /advertisers + /overview/ranking


📊 **数据复盘 / 洞察摘要**
- 账户全局规模庞大：当前 Campaign 总量达 **3,028,727 条**，广告主总数 **757 家**，属于典型的超大规模账户矩阵，任何策略调整需以批量/规则化方式推进，而非人工逐条干预。
- 首页 100 条抽样 Campaign **全部为 Active 状态**（100/100），且 **无任何 Campaign 缺失 Cap**（0/100）；但需注意该样本仅代表首页最新创建的 100 条，**不能代表全局健康度**，全局 active/inactive 比例需依赖全量数据拉取。
- 头部广告主集中度明显：Top 5 广告主合计 Revenue 约 **$22,125.61**，其中 **xiaomi2（ID 1000729）** 以 **$9,827.10** 位居榜首，且环比增长 **+681.21%**，为当前最强增长引擎。

💡 **潜能点 / 风险点**
- **增长引擎（高潜能）**：xiaomi2（1000729）与 xiaomi-1000220-cora（1000372）分别贡献 $9,827.10 与 $5,315.74 收入，环比增幅高达 **+681.21%** 与 **+378.53%**，且 Margin 均在 **41%-46%** 区间，属于高增长+中高利润组合，建议优先保障其 Cap 与流量供给。
- **高利润但规模待放大**：oppo-mmp-Betty（1000839）与 appnext_Jason（1000691）Margin 分别达 **55.67%** 与 **60.62%**，显著高于账户平均水准，但收入体量（$3,345.47 / $922.10）与头部仍有差距，存在通过加量提升利润总额的空间。
- **风险点（下滑预警）**：shareit勿绑_joy（1000439）收入 **$2,715.21**，环比 **-85.32%**，为 Top 5 中唯一下滑广告主，需核查是否因 Cap 收紧、素材衰退或竞品挤压导致，防止利润池进一步萎缩。

🚀 **推荐执行动作 (Recommended Actions)**
1. 针对 **xiaomi2（1000729）** 与 **xiaomi-1000220-cora（1000372）**，建议 **上调 Cap 上限 20%-30%**（基于当前 $9,827 / $5,316 的日收入体量），并优先分配优质流量，锁定高增长窗口期。
2. 针对 **oppo-mmp-Betty（1000839）** 与 **appnext_Jason（1000691）**，建议 **加大预算倾斜与出价竞争力**（Margin 55%+/60%+ 具备充足加价空间），目标将日收入分别推升至 $5,000+ 与 $2,000+，以提升整体利润贡献。
3. 针对 **shareit勿绑_joy（1000439）**，建议 **立即核查 Cap 设置与投放素材**，确认 -85.32% 下滑原因；若为 Cap 限制则恢复容量，若为素材/渠道问题则暂停亏损计划并替换创意。


---

## 🔍 渠道质量对比  (`publisher_deepdive`)

**底层接口**：GET /publishers + /campaign-quality/publisher


📊 **数据复盘 / 洞察摘要**
- **回传缺口（postback_gap）是当前最突出的质量风险信号**：在 90 个质量关注 Publisher 中，多个头部渠道回传缺口巨大，例如 adermobi_allmyfit对接_joy（1000676）缺口达 53,255 单（回传率仅 56.7%）、全结adset-xdj-vicky（1000684）缺口 47,777 单（回传率 72.0%）、leapmob（1000141）缺口 51,028 单（回传率 45.9%），远高于正常水平。
- **点击量异常集中且转化率极低**：adermobi_ddj_allmyfit对接_joy（1000679）点击 1.22 亿次仅转化 35,192 次（CVR 0.029%），adswake-vicky（1000581）点击 2,044 万次仅转化 380 次（CVR 0.0019%），bidmatrix_AMF(new)（1000665）点击 775 万次仅转化 177 次（CVR 0.0023%），存在明显的无效流量或机器流量嫌疑。
- **q1/q2/reject 扣量率在所有渠道均为 0.0%**：该维度暂无有效数据，说明当前未启用或未上报扣量规则，无法据此评估质量，建议补充扣量口径数据。

💡 **潜能点 / 风险点**
- **回传缺口超 50% 的渠道（高风险）**：adermobi_allmyfit对接_joy（1000676）回传率 56.7%、leapmob（1000141）回传率 45.9%、adermobi_ddj_allmyfit对接_joy（1000679）回传率 49.4%。可能原因：回传链路故障、转化数据被渠道截留或存在虚假转化上报，需立即核查对接日志与转化归因。
- **CVR 极低 + 点击量异常（疑似刷量）**：adswake-vicky（1000581）CVR 0.0019%、bidmatrix_AMF(new)（1000665）CVR 0.0023%、adermobi_ddj_allmyfit对接_joy（1000679）CVR 0.029%，均远低于正常水平（正常 CVR 通常 >0.1%）。结合点击量高达千万级，强烈提示存在无效流量、机器点击或劣质激励流量。
- **高转化但回传缺口大的渠道（结算风险）**：全结adset-xdj-vicky（1000684）转化 170,566 次但回传仅 122,789 次（缺口 47,777），bitech（1000647）转化 96,563 次但回传 67,500 次（缺口 29,063），若按回传结算将造成显著利润损失，需确认结算口径。

🚀 **推荐执行动作 (Recommended Actions)**
1. **立即核查 adermobi_allmyfit对接_joy（1000676）与 leapmob（1000141）**：回传缺口均超 5 万单，建议暂停其新增流量分配，并联合技术团队核查 postback 回传日志与转化归因链路，确认是否存在回传丢失或数据截留。
2. **对 adswake-vicky（1000581）与 bidmatrix_AMF(new)（1000665）执行流量质量审计**：CVR 低于 0.003% 且点击量达千万级，建议立即拉取点击 IP/设备指纹数据，识别机器流量占比；若确认刷量，应暂停合作并追回已产生费用。
3. **针对全结adset-xdj-vicky（1000684）与 bitech（1000647）调整结算策略**：在回传缺口未修复前，建议将结算依据从"转化数"切换为"有效回传数"，并设定 7 天整改期，逾期未改善则降低出价或缩减预算。


---

## 📈 放量容量评估  (`scaling_capacity`)

**底层接口**：GET /report(按 campaign 聚合利润) → /campaigns?campaign_ids= 反查 Cap


📊 **数据复盘 / 洞察摘要**
- 近 7 日共有 8 个 Campaign 存在明确放量空间（高 Margin 且 Cap 偏低），其中 **Nike MX iOS CPA s2s 1104** 利润最高（$1,818.9，Margin 56.97%），但 Cap 仅 420，且无 Click Cap 限制，是优先放量对象。
- **Cap 浪费（亏损仍挂 Cap）维度暂无数据**：`over_cap_waste` 数组为空，建议补充该维度口径后再评估止损动作。

💡 **潜能点 / 风险点**
- **放量空间最大**：`Nike MX iOS CPA s2s 1104`（ID 5832106）—— 7 日利润 $1,818.9，Margin 56.97%，Cap 仅 420（日均 60），且 Click Cap 为 0（无点击上限）。按当前 CVR 4.17%（3,193 转化 / 76,581 点击）推算，Cap 若提升 50% 至 630，预计可额外贡献约 $909 利润（按当前 Margin 水平）。
- **次优放量候选**：`Алиса AI ассистент`（ID 7001201）—— Margin 61.59% 为全场最高之一，利润 $1,287，Cap 仅 260，但 Click Cap 高达 200,000（当前 7 日点击 59,445，远未触顶），说明实际瓶颈在转化 Cap 而非流量，建议优先提升 Cap 至 400。
- **高点击低 Cap 风险点**：`com.cp.sto.op.id1000026152_PH`（ID 7030636）—— 7 日点击 112.5 万次，但 Cap 仅 400，Margin 53.23%，利润 $1,245.83。点击量远超 Cap 承载能力（转化率仅 0.4%），可能存在流量质量稀释或 Cap 设置过保守，需核查是否因 Cap 限制导致高 Margin 流量被浪费。

🚀 **推荐执行动作 (Recommended Actions)**
1. 针对 **Nike MX iOS CPA s2s 1104**（ID 5832106），建议将 Cap 从 420 提升至 **630（+50%）**，并同步监控 Margin 是否维持在 55% 以上；若 Margin 无显著下滑，可继续阶梯式上调至 800。
2. 针对 **Алиса AI ассистент**（ID 7001201），建议将 Cap 从 260 提升至 **400（+54%）**，利用其 200,000 的 Click Cap 余量（当前仅用 29.7%）充分承接流量，预计可新增利润约 $700/周。
3. 针对 **com.cp.sto.op.id1000026152_PH**（ID 7030636），建议先核查 Cap 400 是否在近 7 日有触顶记录（当前数据未显示触顶），若未触顶则说明流量质量或出价需优化；若已触顶，建议提升 Cap 至 **600** 并观察 CVR 是否稳定在 0.4% 以上，否则优先调整定向而非放量。
