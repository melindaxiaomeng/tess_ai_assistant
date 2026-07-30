# Tess 诊断服务 · 独立部署指南

Tess 后端是一个**独立的 FastAPI 服务**，与 Teensing 主平台解耦部署：
LLM 调外部（DeepSeek）、含审批流与 Gatekeeper 死锁、用 JSONL 文件持久化反馈/处置单。

---

## 1. 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `TESS_LLM_API_KEY` | ✅ | 空 | DeepSeek（或任意 OpenAI 兼容）Key；缺失时 `/tess/diagnose` 返回 503 |
| `TESS_LLM_BASE_URL` | | `https://api.deepseek.com` | LLM 网关 |
| `TESS_LLM_MODEL` | | `deepseek-chat` | 模型名 |
| `TESS_PORT` | | `8080` | 容器内监听端口 |
| `TESS_FEEDBACK_PATH` | | 内存 | 反馈 JSONL 持久化路径（容器化请指向挂载卷） |
| `TESS_REMEDIATION_PATH` | | 内存 | 处置单 JSONL 持久化路径（容器化请指向挂载卷） |
| `TESS_THRESHOLD_PATH` | | 包内 `thresholds.json` | 学习后阈值策略落盘位置 |

> ⚠️ 生产务必用环境变量/secret 注入 Key，**不要**把 `.env` 写进镜像或提交仓库（已在 `.gitignore`）。

---

## 2. 三种启动方式

### A. 本地直接起（开发/验证）
```bash
pip install -r requirements.txt
export TESS_LLM_API_KEY=sk-xxx
cd /path/to/Tess\ AI\ Assistant
PYTHONPATH=. python -m tess_backend.app
# 或：uvicorn tess_backend.app:app --host 0.0.0.0 --port 8080
```

### B. Docker（单容器）
```bash
export TESS_LLM_API_KEY=sk-xxx
docker build -t tess-diagnose .
docker run -d --name tess-diagnose \
  -p 8080:8080 \
  -e TESS_LLM_API_KEY=$TESS_LLM_API_KEY \
  -v tess-data:/data \
  tess-diagnose
```

### C. Docker Compose（推荐，含卷挂载 + 健康检查）
```bash
export TESS_LLM_API_KEY=sk-xxx        # compose 从宿主机 env 读取
# 或用 .env 文件：echo "TESS_LLM_API_KEY=sk-xxx" > .env
docker compose up --build -d
curl http://localhost:8080/healthz     # => {"status":"ok",...}
```

---

## 3. 健康检查

`GET /healthz` —— 不依赖 LLM，仅报告进程与配置状态，供 K8s/Docker 探针使用：
```json
{ "status": "ok", "service": "tess-diagnose", "version": "2.3.0", "llm_configured": true }
```

---

## 4. 反向代理（生产建议）

Tess 只放内网，前端经网关调用。最小 Nginx 示例：
```nginx
location /tess/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_read_timeout 120s;   # LLM 调用可能较慢
}
```
并把 `app.py` 的 CORS `allow_origins` 从 `["*"]` 收紧为 Teensing 前端域名。

---

## 5. 持久化与多副本注意

- **当前 MVP（单副本）**：JSONL 文件持久化足够，`docker-compose` 已用命名卷 `tess-data` 挂载 `/data`，容器重建不丢反馈/处置单。
- **多副本 / 高可用**：JSONL 不支持并发写，**必须先换成外部存储**（Redis / Postgres）再扩副本。这涉及 `feedback.py` / `remediation.py` 的 Store 后端切换，属于下一步，不要直接 `docker compose up --scale tess=3`。
- 学习后的阈值（`thresholds.json`）同样走文件，多副本场景需改成共享存储或配置中心。

---

## 6. 接口清单（独立服务暴露）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tess/diagnose` | 单事件归因 |
| POST | `/tess/joint-diagnose` | 联合归因（多事件） |
| POST | `/tess/feedback` | 👍/👎 回传 |
| GET | `/tess/feedback/metrics` | 反馈质量度量 |
| POST | `/tess/feedback/self-heal` | 反馈自愈提案（apply=true 落盘） |
| GET | `/tess/thresholds` | 当前阈值策略 |
| POST | `/tess/thresholds/reset` | 恢复默认阈值 |
| POST | `/tess/remediation/propose` | 处置提案 |
| GET | `/tess/remediation` | 处置单列表 |
| GET | `/tess/remediation/{id}` | 处置单详情 |
| POST | `/tess/remediation/{id}/approve` | 审批（CRITICAL 双人） |
| POST | `/tess/remediation/{id}/reject` | 驳回 |
| POST | `/tess/remediation/{id}/execute` | 执行（须 APPROVED） |
| GET | `/healthz` | 存活探针 |

---

## 7. 接真执行器（半自动处置）

`REMEDIATION_EXECUTOR` 默认是 `MockRemediationExecutor`。接 Teensing 真实处置 API 时，
新建一个实现 `run(proposal) -> dict` 的适配器类，替换 `app.py` 中该单例即可，**死锁/Gatekeeper 逻辑完全不动**。

---

## 8. 前端与 Tess 在「同云不同 VPC」时的连通方案

典型场景：Teensing 前端在 **VPC-A**，Tess 新购 ECS 在 **VPC-B（华北6）**，两者同属阿里云但 VPC 不同。
**结论：仍然完全内网，不暴露公网、不触发 ICP 备案**——只需把两个 VPC 在阿里云私有骨干网上打通。

### 8.1 同地域（推荐，零带宽费）
用 **专有网络对等连接（VPC Peering）**：
1. VPC 控制台 → **对等连接** → 创建，发起端选 VPC-A，接收端选 VPC-B（同地域）。
2. 两端都 **接受** 连接请求。
3. 在 **VPC-A 的路由表** 加一条：目的 CIDR = VPC-B 网段，下一跳 = 该对等连接。
4. 在 **VPC-B 的路由表** 加一条：目的 CIDR = VPC-A 网段，下一跳 = 该对等连接。
> 对等连接按流量计费（同地域极低/免费），无需买带宽包。

### 8.2 跨地域（如前端在其它 Region）
用 **云企业网 CEN（Cloud Enterprise Network）**：
1. CEN 控制台 → 创建 CEN 实例。
2. 把 VPC-A、VPC-B 都 **加载** 到该 CEN 实例。
3. 跨地域需购买 **带宽包** 并购买跨地域带宽（按 MU 计费）；同地域互连免费。
4. 两端 VPC 路由表自动学习，无需手配静态路由。

### 8.2.1 跨境特别说明（前端在香港 cn-hongkong）
你的场景是 **大陆（华北6）↔ 香港**，属于「跨境」CEN，比同地域多了两步：
1. 创建 CEN 时，跨地域带宽包的 **互通区域** 选 **「中国内地 ↔ 中国香港」**（不是普通跨地域）。
2. 购买 **跨境带宽包** 并分配带宽（跨境带宽包按月付费，比同地域贵；MVP 2–5Mbps 足够）。
3. 安全组源填 **香港 VPC-A 里前端那台的私网 IP（172.x 段）**，不要填公网 IP。
4. 前端调 Tess 用 **华北6 VPC-B 的私网 IP（10.x 段）**，流量走 CEN 骨干、不过公网 → **不触发 ICP 备案**。
> ⚠️ 合规：内地↔香港的跨境数据传输，若承载大量个人信息/受监管数据，建议先与贵司合规团队确认；CEN 为阿里云托管骨干，常规业务诊断数据一般可直接使用，但切勿把敏感 PII 明文塞进 root_cause 文本。
> ⏱️ 延迟：HK↔乌兰察布物理距离远，骨干 RTT 约 40–60ms，叠加 DeepSeek 调用，单次诊断约 1–3s，内部可接受。
> 💡 备选：若觉得跨境 CEN 太重，也可在华北6 安全组白名单香港出口 IP 后，给 Tess 开一个 **受控公网端点**（TLS + mTLS/API Key），前端走加密公网调；但该路径数据经过公网、安全性弱于 CEN，且公网 IP:端口暴露存在合规灰区，优先选 CEN。

### 8.3 安全组放行（关键）
Tess 这台机器（VPC-B）的 **安全组入方向** 必须显式放行前端的私网地址 → 8080，否则仍会被拦：
- 协议：`TCP`，端口：`8080`
- 授权对象：`VPC-A 前端服务器的私网 IP`（或填前端安全组 ID `sg-xxxx` 作为源，更稳）
- 出方向：放通到 DeepSeek 公网（`443`）即可（Tess 要调 LLM）

> 应用层 `docker-compose.yml` 里 `ports: "8080:8080"` 是绑宿主机全网卡，**收口靠安全组**，这是正确做法；若要更狠可改成只绑私网网卡 `"10.x.x.x:8080:8080"`。

### 8.4 前端怎么调 Tess
前端**用 Tess 的私网 IP 调**（不要用公网 IP / 不要绑公网域名），跨 VPC 流量走对等连接/CEN：
```javascript
// 前端示例（fetch）
const TESS_BASE = "http://<Tess私网IP>:8080";   // 如 http://10.2.3.4:8080
const r = await fetch(`${TESS_BASE}/tess/diagnose`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});
```

### 8.5 连通性验证（Tess 起好后，在 VPC-A 前端机上跑）
```bash
ping <Tess私网IP>                          # 同云内私网可达（安全组可能禁 ICMP，通不过也别慌）
curl -m 5 http://<Tess私网IP>:8080/healthz   # 应返回 {"status":"ok",...}
```
若 `curl` 超时：先查 VPC-B 安全组入方向是否放行了前端私网 IP:8080，再查路由表对等连接/CEN 是否生效。

> 一句话：同云不同 VPC = 建一条对等连接/CEN，前端拿 Tess 私网 IP 调，全程不碰公网、不备案。

---

## 9. 个人信息脱敏（GAID 哈希）

Tess 处理的异常数据可能含用户级标识（如 **GAID 广告 ID**、IP、UA）。编排层在把数据喂给 LLM **之前** 会做脱敏（`tess_backend/privacy.py`）：

- **GAID 自动哈希（HMAC-SHA256，确定性）**：同一个 GAID 永远得到同一个哈希，仍可跨事件去重 / 计数 / 关联；但真实 GAID **永不离开本网络、不会发送给 LLM 服务商（DeepSeek）**。已在 `run_diagnosis` 入口默认开启。
- **IP / UA 原样保留**：IP 分析（地域 / ASN / 网段聚类）依赖完整 IP，**刻意不做截断**，否则会破坏分析准确性。IP 属个人信息但非敏感个人信息，合规处置由部署拓扑（是否跨境）决定。
- **salt 配置**：读环境变量 `TESS_GAID_SALT`；未设则用开发默认 salt。生产应设强随机 salt，且若需跨系统按 GAID 关联，各系统须使用同一 salt（否则哈希对不上）。

```bash
export TESS_GAID_SALT="$(openssl rand -hex 32)"   # 生产建议
```

> 数据分类提醒：GAID + IP 在 PIPL / GDPR 下通常属「个人信息(PI)」，但非「敏感个人信息」；若真实数据**无购买/金融/健康等敏感字段**，合规等级较低。开发 / 测试盒即使灌入真实数据，只要量级不大、且 GAID 已哈希，风险可控；生产仍需按正式拓扑做好出境合规。

### 9.1 安全版 C：Tess 自身持有加密 GAID 映射（方案 C）

若业务要求 Tess 把**原始 GAID 还原后**吐给最终用户（而非由上游服务器做 join），则采用方案 C：**Tess 服务器自身持有 `哈希GAID ↔ 原始GAID` 的加密映射**，内部 join 后返回原始值，日志本地但脱敏。

**数据流**
```
原始GAID → /tess/diagnose（VAULT.ingest 存入加密映射；之后 deidentify_input 仅留哈希给 LLM）
         → LLM 只收到哈希
         → 需要还原时 POST /tess/gaid/resolve {hashed} → 返回原始GAID
         → 日志经 RedactFilter 自动抹掉原始 GAID
```

**环境变量（新增）**

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `TESS_GAID_VAULT_KEY` | | `tess-dev-gaid-vault-key` | 映射表加密主密钥；生产务必设强随机值 |
| `TESS_GAID_VAULT_PATH` | | 内存 | 加密映射表落盘路径（容器化请指向挂载卷）；不设则仅进程内存（仍为加密态） |

**安全属性**
- **LLM 侧零暴露不变**：发给 DeepSeek 的永远是哈希，与映射表存哪无关。
- **静态加密**：优先 `cryptography` 的 **Fernet**；未装则自动回退 **stdlib HMAC 流密码 + 完整性标签**（已在 `tess_backend/gaid_vault.py` 实现，无外部依赖）。
- **被遗忘权**：`DELETE /tess/gaid/{hashed}` 删除对应映射。
- **日志脱敏**：`RedactFilter` 在 emit 阶段把已知原始 GAID 替换为 `***REDACTED***`，Tess 本地日志不落明文。

**端点（内部 / 需鉴权，生产建议加 API Key 中间件）**
- `POST /tess/gaid/resolve` — body `{ "hashed": "<哈希值>" }` → `{ "hashed", "original" }`；未知哈希返回 404（绝不编造）。
- `DELETE /tess/gaid/{hashed}` — `{ "deleted": true/false }`。

**风险须知（与方案 A 的区别）**：方案 C 下 Tess 本身成为 PII 仓库，因此
1. 爆破半径集中在 Tess（Tess 被攻破 = 原始 GAID 泄露）；
2. 数据驻留辖区随 Tess 部署地扩大（跨境合规需重新评估）；
3. 须承担 PII 删除 / 被遗忘权义务。
方案 A（Tess 只返哈希、上游同盐 join）仍是最干净的零 PII 设计；方案 C 仅在确需 Tess 还原时使用，并应配合密钥管理（KMS）、访问控制与日志审计。

```bash
export TESS_GAID_VAULT_KEY="$(openssl rand -hex 32)"   # 生产建议
export TESS_GAID_VAULT_PATH=/data/gaid_vault.enc        # 容器内指向挂载卷
```

---

## 10. Tess 测试服务器快速部署

> 形态定位：**开发 / 测试用**，非生产。该机器含公网 IPv4，故可直接被浏览器 / 测试前端访问 `http://<公网IP>:8080`。与香港前端的跨境联调属预发 / 生产阶段，开发期先本机直连测功能即可。

### 10.1 机器规格与可行性
- 2vCPU / 2GiB / 40GB ESSD / 3Mbps 固定带宽 / Alibaba Cloud Linux 3。
- Docker + uvicorn 单进程内存占用 ~150–300MB，2GiB 完全够用；`3Mbps` 带宽仅影响你本机浏览器访问延迟，诊断耗时主要在 LLM 往返（1–3s），与带宽无关。

### 10.2 从 Mac 上传代码到云机器
在 **Mac 本地** 执行（排除无关目录，保持轻量）：

```bash
# 把项目同步到云机器 /root/tess/，自动排除 .git/.workbuddy/缓存
rsync -avz --exclude='.git' --exclude='.workbuddy' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
  -e ssh \
  "/Users/menlinda.meng/Desktop/ai/Tess AI Assistant/" \
  root@<公网IP>:/root/tess/
```
> 若用密码登录而非密钥，给 ssh 加 `-o PubkeyAuthentication=no`，或在 `~/.ssh/config` 配好 Host。

### 10.3 SSH 登录
```bash
ssh root@<公网IP>
cd /root/tess
```

### 10.4 一键部署
```bash
# 可选：先填 Key（诊断需要；健康检查 / GAID 演练无需）
export TESS_LLM_API_KEY=sk-xxx

bash deploy-devbox.sh
```
脚本会：装 Docker → 生成 `.env` → `docker compose up -d --build` → 轮询 `/healthz`。
成功后会提示把测试前端的「后端地址」填成 `http://<公网IP>:8080`。

### 10.5 验证清单
1. **健康检查**：浏览器 / curl `http://<公网IP>:8080/healthz` → `{"status":"ok",...}`。
2. **测试前端**：打开 `tess-test-frontend.html`，后端地址填 `http://<公网IP>:8080`，先点「健康检查」，再「发送诊断」。
3. **隐私演练（无需 Key）**：Tab③ 输入原始 GAID → 走闭环，验证返回结果无明文、resolve 能还原、delete 生效。

### 10.6 安全提醒（测试服务器直接暴露公网）
- **安全组最小开放**：在阿里云控制台把 `8080` 入方向来源改为**你本机公网 IP / 或公司网段**，不要 `0.0.0.0/0`；用完可临时放开，测完收紧。
- **公网裸奔风险**：当前 API 无鉴权。开发期可接受（仅你访问），但**切勿长期公网裸奔**。生产前务必加一层 API Key 中间件（FastAPI 依赖注入校验 `X-API-Key` 头），可让我帮实现。
- **勿放真实敏感数据**：测试服务器只用于功能验证；真实生产数据走正式拓扑（香港同地域方案 A 或合规留大陆方案）。

### 10.7 常用运维
```bash
docker compose logs -f tess     # 看日志
docker compose restart tess     # 重启
docker compose down             # 停止并移除容器（数据卷 tess-data 保留）
docker compose up -d --build    # 改代码后重新构建
```

---

## 11. P5 数据接入（拉取真实异常）+ P6 审计（按人留痕）

### 11.1 数据接入层
- 默认 `TESS_DATA_CONNECTOR=mock`：调用 `POST /tess/diagnose-from-source` 即拉内置样例并真诊断，零配置可用。
- 生产接真实数据：设 `TESS_DATA_CONNECTOR=teensing` + `TESS_DATA_API_BASE_URL=https://<saas-host>/api/v1`，再调同一端点。
- 连接器轮询两个 Teensing 端点并归并：
  - `GET /overview/ranking/anomaly-warning`（异常预警，标出异常实体）
  - `GET /overview/ranking/fluctuation`（涨跌榜，含 `revenue/clicks/cvr/profit/margin/change`）
  - 经 `normalize_to_context()` 归一化为 PRD §4.1 Context 后送诊断编排。

### 11.2 鉴权透传（按访问者权限回数据）
- 生产模式**不**在 Tess 落库任何 SaaS 凭据。前端调用 Tess 时，在请求头带上：
  - `X-Teensing-Token: <当前运营已登录 SaaS 的 access_token>`
  - Tess 原样作为 `Authorization: Bearer` 转发给 Teensing；Teensing 按该运营的 RBAC / 数据权限返回数据。**无需额外账号体系，天然不越权。**
- `POST /tess/diagnose-from-source` 在 teensing 模式下**强制要求** `X-Teensing-Token`，缺则返回 `400`。
- 另有兜底环境变量 `TESS_DATA_API_KEY`（服务端固定凭据），仅在无前端透传的特殊场景使用。

### 11.3 问答审计（记录每个人问了什么、答了什么）
- 每次 `POST /tess/diagnose` 与 `POST /tess/diagnose-from-source` 都会写入本地 SQLite 审计库（路径由 `TESS_AUDIT_DB` 指定，默认 `tess_audit.db`）。
- 前端在请求头带 `X-Operator-Id: <运营标识>`，审计即按该运营归因（缺省记 `anonymous`）。
- 查询：`GET /tess/query-log?operator_id=<可选>&limit=100`，返回最近问答记录（受全局 `X-API-Key` 守卫约束，若已开启）。

### 11.4 调用示例（curl）
```bash
# 按当前运营权限拉取并诊断
curl -X POST http://localhost:8080/tess/diagnose-from-source \
  -H "Content-Type: application/json" \
  -H "X-Operator-Id: alice" \
  -H "X-Teensing-Token: <alice 的 SaaS access_token>" \
  -d '{"limit": 5}'

# 查看某运营的问答审计
curl "http://localhost:8080/tess/query-log?operator_id=alice" \
  -H "X-API-Key: <若生产已开启>"

---

## 12. P7 主动预警 + Teensing 拉取接口

Tess 在进程内每小时（间隔可配 `TESS_SCHEDULE_INTERVAL`）自动：拉异常/实时 KPI → 诊断 → 落预警库。
**Teensing 后端无需前端点击，定时轮询下方接口即可拿到 Tess 算出的异常结果。**

### 12.1 启用调度
```bash
TESS_SCHEDULE_ENABLED=true
TESS_SCHEDULE_INTERVAL=3600      # 秒，默认 1 小时
TESS_SCHEDULE_LIMIT=20
TESS_SYSTEM_TOKEN=<共享服务 token>   # 定时任务拉 Teensing 数据时用的 Bearer，留空回退 TESS_DATA_API_KEY
TESS_REALTIME_DROP_THRESHOLD=0.3    # 实时 KPI 同比跌幅阈值，超此判异常
```

### 12.2 Teensing 拉取接口（二选一）
- **通用**：`GET /tess/alerts?source=realtime-kpi` —— 最近 N 条（跨批次混合）。
- **Teensing 专用（推荐）**：`GET /tess/realtime-kpi/alerts` —— 只返回**最近一轮整批** realtime-kpi 诊断，结构更干净：
  ```json
  {
    "as_of": "2026-07-30 13:00:00",     // 批次时间，Teensing 据此去重：相同 as_of 即同一批
    "generated_at": "2026-07-30 13:05:12",
    "count": 1,
    "items": [
      { "id": 42, "run_time": "...", "event_id": "REALTIME-GAP-09-17",
        "status": "DIAGNOSED", "confidence": 0.91, "source": "realtime-kpi",
        "diagnosis": { "...": "Gatekeeper 归一化诊断" },
        "anomaly_metadata": { "current_value": 0.0, "benchmark_value": 12345.6,
          "severity": "HIGH", "calculated_loss": 12345.6 } }
    ]
  }
  ```

### 12.3 鉴权
- 生产设 `TESS_API_KEY=<强随机值>` 后，所有 `/tess/*`（含上面拉取接口）强制要求 `X-API-Key` 请求头，否则 401。
- Teensing 后端轮询时带上 `X-API-Key: <同一值>` 即可（共享密钥，不按人过滤）。
- 手动触发一次诊断（验证用，不必等整点）：`POST /tess/cron/run` `{"limit":20}`。

### 12.4 调用示例（curl）
```bash
# Teensing 专用拉取接口
curl "http://<tess-host>:8080/tess/realtime-kpi/alerts?limit=50" \
  -H "X-API-Key: <TESS_API_KEY>"

# 手动触发一轮（立即产生最新批次，便于联调）
curl -X POST "http://<tess-host>:8080/tess/cron/run" \
  -H "Content-Type: application/json" -d '{"limit": 20}'
``` 

