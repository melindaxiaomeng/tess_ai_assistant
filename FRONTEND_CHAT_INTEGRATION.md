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
    "X-Teensing-Token": userToken,  // 运营 SaaS access_token，按权限取数（RBAC）
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

## 5. 历史管理端点（可选）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tess/chat/{chat_id}` | 读服务端历史 → `{ chat_id, messages:[...], count }`，用于刷新/恢复抽屉 |
| DELETE | `/tess/chat/{chat_id}` | 清空该会话（"清空对话"按钮） |

## 6. 注意事项

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
      headers: { "Content-Type": "application/json", "X-API-Key": KEY, "X-Teensing-Token": TOKEN },
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
- `tess-drawer-demo/src/App.tsx` —— 顶部配置栏新增 `Teensing Token` 输入，底部挂载 `TessChatDrawer`，把 `backend / apiKey / token` 透传下去。
- 演示：先问“广告主 X 在渠道 Y 上近 7 日营收怎么样？”，再问“它昨天的营收呢？”即可看到 chat_id 自动指代、无需重复实体。
