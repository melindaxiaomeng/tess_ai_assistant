# Tess 归因诊断抽屉 · 演示工程

把仓库根目录的 `tess_drawer.tsx`（渲染屏障组件）跑成一个可视化演示页，
便于给团队 / 客户演示「诊断 → 抽屉渲染 → 渲染屏障」全链路。

> 渲染屏障（PRD V2.0.0 §7）：抽屉内所有**数值 / Severity / 路由**均来自可信的
> `inputData`（算法层算好），LLM 只贡献定性叙事（summary / causal_chain /
> primary_contributor_id）。即使模型返回伪造字段，前端也只用算法层数据强渲染。

## 工程结构

| 文件 | 说明 |
| --- | --- |
| `src/components/TessDiagnosticDrawer.tsx` | 复制自根目录 `tess_drawer.tsx`，渲染屏障组件本体（未改逻辑） |
| `src/App.tsx` | 演示宿主：后端配置、输入编辑、调用 `/tess/diagnose`、离线演示、渲染屏障对抗演示 |
| `src/sample.ts` | 演示样例（与 `tess-test-frontend.html` 同源） |
| `src/index.css` | Tailwind 指令 + Severity 标签配色（设计令牌替换点） |

## 本地运行

```bash
npm install
npm run dev          # 默认 http://localhost:5173
# 或构建后预览
npm run build && npm run preview   # 预览默认 http://localhost:4173
```

## 连接真实 Tess 后端（Tess 测试服务器）

> 默认后端地址已预填为 `https://8.141.113.22:8443`（HTTPS 反代，非原 8080）。

**现在是 HTTPS 直连（推荐给客户演示）**
已为 Tess 测试服务器准备 nginx HTTPS 反代脚本 `deploy/setup-https-proxy.sh`：
在服务器执行 `sudo bash deploy/setup-https-proxy.sh`，即可把本地 `127.0.0.1:8080` 暴露为 `https://8.141.113.22:8443`。
之后**公网预览页（HTTPS）即可直连真后端**，不再受浏览器 Mixed Content 限制。

首次使用两步（一次性）：
1. 阿里云安全组放行 TCP **8443** 入站（来源建议限你本机 / 演示用 IP）。
2. 浏览器新标签页打开 `https://8.141.113.22:8443/healthz`，点「高级 → 继续访问」手动信任自签证书。

之后在公网预览页点「健康检查」应显示绿 + `llm_configured:true`，再点「▶ 连接后端诊断」即看真结果。

> 自签证书会被浏览器标记「不安全」，属正常；若要无告警受信任证书，需绑定域名并完成 ICP 备案后改用 Let's Encrypt（仅替换脚本里的证书路径）。

**本地 `npm run dev` 验证（备选）**
本地 `npm run dev`（http://localhost:5173），后端地址已预填，点健康检查 → 连接后端诊断即可；本地 HTTP 页面对 HTTP/HTTPS 后端均不受 Mixed Content 限制。

> 离线演示：直接点「▶ 离线演示」，用内置样例渲染，无需后端，立即可看。
> 若后端设了 `TESS_API_KEY`，填到「API Key」框即可。

## 渲染屏障演示（重点给客户看）

勾选「模拟 LLM 篡改攻击」：程序强行往 LLM 返回里注入
`severity=LOW`、`loss=$0.01` 及一条伪造因果链。
观察右侧抽屉——**这些伪造值一律不生效**，仍显示 `inputData` 的 `HIGH / $350`，
直观证明前端对 LLM 输出做了死锁式强渲染。

## 接口契约（对应 `tess_backend/contracts.py`）

- `POST /tess/diagnose`：请求体 = `TESS_INPUT_SCHEMA`，返回 `TESS_OUTPUT_SCHEMA`（作为 `llmOutput`）。
- 数值 / severity 必须由算法层算好放入 `inputData`，LLM 只允许产出文字叙事。
