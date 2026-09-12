# 前端多轮会话对接要点（Tess 抽屉 / Drawer）

> 适用对象：前端同学
> 后端依赖：`/tess/ask`（含 `chat_id` 多轮）、`/tess/tool`（宽工具 `tess_ask` 同样支持 `chat_id`）
> 一句话：**前端只负责"生成并原样带回 `chat_id`"，不存历史、不拼 prompt；对话流由前端自己累积渲染。**

---

## 1. chat_id 是什么

- 服务端用 `chat_id` 标识一次连续对话，多轮问答存在服务端（SQLite / PG，按 `chat_id` 隔离，会话之间不串）。
- 一旦换 `chat_id`，就是新会话（历史不继承）。

## 2. 生成 chat_id（首次提问时）

在抽屉打开 / 用户发起第一次提问时生成一个全局唯一 ID，绑到该抽屉实例上。

```js
// 推荐浏览器原生 API，无需后端
const chat_id = crypto.randomUUID();
```

- 一个抽屉 = 一个 `chat_id`；
- "新对话"按钮 = 生成新的 `chat_id`；
- 抽屉关闭不必清，用户可再次打开同一 `chat_id` 续聊（可用 `GET /tess/chat/{chat_id}` 恢复历史）。

## 3. 携带 chat_id（每次请求）

`POST /tess/ask` 请求体加一个可选字段 `chat_id`（字符串；**不传 = 单轮兜底**）：

```js
await fetch("/tess/ask", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": "YOUR_KEY",        // 生产必带（TESS_API_KEY 设了之后）
    "X-Teensing-Token": userToken,  // ★ 当前登录运营的 saas access_token（按人取数，最高优先级）
    "X-Platform-Id": platformId,    // 平台标识（如 "Melodong"）；未带运营 token 时按它取平台 token
    "X-Operator-Id": userId,        // 可选，审计归因
  },
  body: JSON.stringify({
    question: "它昨天的营收怎么样？",
    chat_id,                        // ← 多轮关键：每次原样带回同一个
    analysis_type: undefined,       // 可选：前端胶囊显式透传（14 种之一 / cross_dimension）
  }),
});
```

若走 LLM Tool Calling 宽工具（`POST /tess/tool`），`tess_ask` 的 arguments 同样带 `chat_id`：

```json
{ "tool": "tess_ask", "arguments": { "question": "它昨天的营收怎么样？", "chat_id": "..." } }
```

## 4. 抽屉内多轮"拼接"怎么做（前端视角）

**服务端不会把历史拼进返回结果**——历史只用于"指代消解"（让 LLM 知道"它/这个"指上一轮的实体）。返回体始终只是**本轮**的 Markdown：

```json
{ "answer": "Markdown 回答", "result": "<同 answer>", "data": "<同 answer>",
  "context_summary": { "analysis_type": "...", "route_source": "entity|explicit|inferred" } }
```

所以"对话流"由前端自己累积，顺序渲染即可：

```js
const messages = []; // 抽屉内累积
messages.push({ role: "user", content: question });
messages.push({ role: "assistant", content: resp.answer }); // 注意取 resp.answer
// UI 按 messages 顺序渲染（Markdown 卡片）
```

**实体回退说明**：若本轮问题没有显式实体（如"它昨天的营收"），服务端会自动沿用上一轮解析出的 `campaign_id` / `advertiser_id` 等，前端**无需补实体**，只要保证 `chat_id` 不变。

## 5. 历史会话端点（可选）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tess/chats` | **列出当前运营的全部历史会话**（侧边栏用）→ 见下 |
| GET | `/tess/chat/{chat_id}` | 读单个会话完整历史 → `{ chat_id, messages:[...], count }`，用于点击历史项后恢复抽屉 |
| DELETE | `/tess/chat/{chat_id}` | 清空该会话（"清空对话"按钮） |

### 5.1 `GET /tess/chats` 返回结构（历史会话列表）
受 `X-API-Key` 守卫；按请求头 `X-Operator-Id` 隔离（不传则归到 `anonymous` 桶）。

```json
{
  "count": 2,
  "sessions": [
    {
      "chat_id": "sess-1",
      "operator_id": "opX",
      "title": "广告主 1000839 近 7 日营收",   // 取首条 user 问题，可直接作侧边栏标题
      "message_count": 4,
      "created_at": "2026-09-12T08:00:00Z",
      "updated_at": "2026-09-12T08:05:00Z"
    }
  ]
}
```

### 5.2 历史侧边栏接法（3 步）
1. 抽屉打开时 `GET /tess/chats` 拉列表 → 渲染成侧边栏（标题用 `title`，副信息用 `updated_at` + `message_count`）。
2. 点击某条 → 拿它的 `chat_id` 调 `GET /tess/chat/{chat_id}` 取 `messages[]`，把 `role`+`content` 灌进当前 `messages` 数组渲染，并把抽屉的 `chat_id` 设为该项（后续追问沿用，**自动继承该会话的实体指代**）。
3. "新对话"按钮：生成新 `chat_id` 并清空 `messages`（与列表脱钩）。

> 注意：当前前端策略是"抽屉打开期间恒定 chat_id、关闭/刷新换新 id"，所以每个抽屉打开会落一条新会话；历史列表会逐条累积，点击即可恢复任意一条。若想"重开抽屉接着聊上次的"，把当前 `chat_id` 存 `localStorage` 即可（无需新接口）。

## 6. 运营分析报表接口（出报表用）

> 这两个接口用于**把大家问的问题 / 回答 / 实体 / 谁问的 / 何时**导出来做优化分析。受 `X-API-Key` 守卫；按 `X-Operator-Id` 隔离（不传则只看 `anonymous` 桶）。

### 6.1 `GET /tess/chats/export?format=json|csv`
导出全量「轮」记录（每轮 = 一问一答合并成一行），可直接拉进 Excel / BI。

- `format=json`（默认）：
```json
{
  "count": 2,
  "rows": [
    { "chat_id":"sess-1", "operator_id":"opX", "question":"广告主 1000839 的营收",
      "answer":"### 诊断结论...", "ts":"2026-09-12T08:00:00Z",
      "analysis_type":"advertiser_deepdive", "route_source":"entity",
      "campaign_id":null, "advertiser_id":1000839, "publisher_id":null,
      "package_name":null, "owner_user_id":null,
      "llm_prompt_tokens":100, "llm_completion_tokens":40, "llm_total_tokens":140 }
  ]
}
```
- `format=csv`：同字段的扁平 CSV 下载（文件名 `tess_chats.csv`），`question` / `answer` 若含换行会被 csv 模块正确包裹。

字段说明：`analysis_type` 区分「单维下钻类型 / cross_dimension / null(纯问答兜底)」，是做「问题类型分布」分析的关键维度；`route_source`（`explicit|entity|inferred`）区分问题是怎么被路由的；`llm_*_tokens` 为该轮 LLM 用量（来自 LLM 响应的 usage 字段，供成本核算；旧数据 / 未记录时为 `null`）。

### 6.2 `GET /tess/chats/stats`
后端算好的聚合指标，前端可直接渲染成报表看板：
```json
{
  "total_sessions": 12, "total_turns": 58,
  "top_questions": [{"question":"广告主 1000839 的营收","count":9}],
  "top_entities":   [{"entity":"advertiser_id=1000839","count":11}],
  "per_operator":   [{"operator_id":"opX","count":40}],
  "analysis_type_distribution": {"advertiser_deepdive":20,"cross_dimension":8,"campaign_detail":12,"None":18},
  "route_source_distribution":   {"entity":40,"explicit":6,"inferred":2,"None":10},
  "daily_buckets": {"2026-09-10":15,"2026-09-11":23,"2026-09-12":20},
  "llm_usage": { "turns_with_usage":58, "prompt_tokens":12000, "completion_tokens":5000,
                 "total_tokens":17000, "per_day_total_tokens":{"2026-09-12":17000},
                 "per_platform_total_tokens":{"Melodong":17000} }
}
```
- `top_questions`：高频问题原文（优化话术 / 预设胶囊的线索）
- `top_entities`：被问得最多的 campaign / 广告主 / 渠道（运营重点对象）
- `per_operator`：各运营提问量（活跃度 / 培训重点）
- `analysis_type_distribution`：**单维 vs 交叉维度 vs 纯问答** 占比（判断要不要强化某类下钻）
- `daily_buckets`：提问按天分布
- `llm_usage`：LLM 用量汇总（总/输入/输出 token、按天、按平台），可乘以模型单价直接核算成本

### 6.3 验证（部署后）
```bash
# 导出 CSV（拿去 Excel）
curl -s -H "X-API-Key: $TESS_API_KEY" -H "X-Operator-Id: opX" \
  "http://<Tess服务器IP>:8080/tess/chats/export?format=csv" -o tess_chats.csv

# 看聚合
curl -s -H "X-API-Key: $TESS_API_KEY" -H "X-Operator-Id: opX" \
  "http://<Tess服务器IP>:8080/tess/chats/stats" | jq .
```

## 7. 注意事项

- **`chat_id` 必须每次相同才能多轮**；换 id = 新会话（历史不继承）。
- 不传 `chat_id` 也能正常问答，只是无法追问指代（退化为单轮兜底）。
- 服务端默认保留最近 **10 轮**（`TESS_CHAT_HISTORY_LIMIT` 可配），更老的被裁剪，前端无需处理。
- 多轮历史存服务端 SQLite（生产在持久卷 / PG），容器重启不丢。

---

### 最小可跑示例（伪代码）

```js
class TessDrawer {
  constructor() { this.chat_id = crypto.randomUUID(); }
  async ask(q) {
    const resp = await fetch("/tess/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": KEY, "X-Teensing-Token": TOKEN, "X-Platform-Id": PID },
      body: JSON.stringify({ question: q, chat_id: this.chat_id }),
    }).then(r => r.json());
    return resp.answer;          // 渲染这个
  }
  newChat() { this.chat_id = crypto.randomUUID(); }
}
```

---

### 仓库内可运行参考实现

本仓库 `tess-drawer-demo/` 已内置多轮问答抽屉，可直接 `npm install && npm run dev` 跑起来看效果：

- `tess-drawer-demo/src/components/TessChatDrawer.tsx` —— 多轮问答组件（含上面 3 处改动 + `新对话` 按钮 + 消息流累积渲染）。
- `tess-drawer-demo/src/App.tsx` —— 顶部配置栏的 `Platform Id` 输入（X-Platform-Id），底部挂载 `TessChatDrawer`，把 `backend / apiKey / platformId` 透传下去。
- 演示：先问“广告主 X 在渠道 Y 上近 7 日营收怎么样？”，再问“它昨天的营收呢？”即可看到 chat_id 自动指代、无需重复实体。

---

## 8. 多平台（P9）对接要点

> 多个平台（各自独立的 Teensing 租户 token，共用同一 base_url）共用同一套 Tess 后端。
> 每个平台用**不同的平台级系统 token**取数，且所有落库（对话 / 预警）都会打上 `platform_id`，
> 以便按平台隔离、分平台出报表。

### 8.1 前端请求头（按人 > 按平台，两级取数）

在 §3 的 headers 基础上（与 `X-Operator-Id` 同级）：

```js
headers: {
  "Content-Type": "application/json",
  "X-API-Key": KEY,            // 对外接口鉴权（网关注入）
  "X-Teensing-Token": userToken, // ★ 当前登录运营的 saas access_token（按人取数，最高优先级）
  "X-Operator-Id": userId,     // 谁问的（审计）
  "X-Platform-Id": platformId, // 平台标识（如 "Melodong"），与注册 id 逐字符一致
}
```

- **优先级**：`X-Teensing-Token`（运营个人 token，Tess 原样转发给 saas_v3.0 数据接口，
  按该运营 RBAC/数据权限返回数据，各运营各看各的）> 平台级 token（`X-Platform-Id`
  从 `tess_platforms` 表解析）> 全局 `TESS_SYSTEM_TOKEN`（后端 `.env` / compose）。
- 带 `X-Teensing-Token` 时 `token_mode` 为 `"user"`；带平台头时为 `"platform"`；兜底 `"system"`。
- 多轮 `POST /tess/ask` 带 `X-Platform-Id` 时，本轮问答会被打上该 `platform_id`，
  后续 `GET /tess/chats` / `/tess/chats/export` / `/tess/chats/stats` 也支持 `?platform=` 过滤。
- 预警拉取 `GET /tess/alerts` / `/tess/realtime-kpi/alerts` 同样支持 `?platform=` 或头过滤，
  只返回该平台告警。

### 8.2 平台管理接口（管理端用，独立密钥 `X-Admin-Key`）

不是给前端调用方用的，是给运维在后台增删改平台凭证的。受 `TESS_ADMIN_API_KEY` 守卫
（未设置时整体禁用 403）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/tess/admin/platforms` | 列出全部平台（含 token、llm_api_key、base_url、启用状态） |
| POST | `/tess/admin/platforms` | 新增：body `{ id, name, token, llm_api_key?, base_url?, is_active? }` |
| PUT | `/tess/admin/platforms/{id}` | 改：body 任意子集 `{ name, token, llm_api_key, base_url, is_active }` |
| DELETE | `/tess/admin/platforms/{id}` | 删（历史记录保留原 platform_id，仅停该平台后续定时诊断） |

字段说明：
- `token`：平台级**取数** token（Tess 调 saas_v3.0 数据接口用），必填。
- `llm_api_key`：该平台专用 **LLM（DeepSeek）key**，可选 —— 上层平台各自在 DeepSeek 开
  独立 key（如 Melodong 的 `sk-1e62c...`），Tess 调 LLM 时优先用它，用量/账单按平台
  区分；为空则回退全局 `TESS_LLM_API_KEY`。

```bash
# 新增一个平台（token 由平台提供；llm_api_key 填该平台在 DeepSeek 开的专用 key）
curl -s -X POST "https://<host>/tess/admin/platforms" \
  -H "X-Admin-Key: $TESS_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"id":"melodong","name":"Melodong","token":"<平台级系统token>","llm_api_key":"sk-<该平台DeepSeek key>","is_active":true}'

# 只跑某平台的一次诊断（即时验证）
curl -s -X POST "https://<host>/tess/cron/run" \
  -H "X-API-Key: $TESS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"platform":"facemoji","limit":20}'
```

