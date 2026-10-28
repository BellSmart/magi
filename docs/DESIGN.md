# MAGI 公网化设计文档

> 状态：设计定稿，待开工
> 上游：`https://github.com/TomaszRewak/MAGI`（MIT, © 2023 Tomasz Rewak）
> 本文替代上游的 README 实现说明；上游代码仅作为行为参照与视觉素材来源。

---

## 1. 目标

把上游的单机 Demo（Dash + 单一 GPT 模型 + 浏览器传 key）改造成一个**可公网部署**的多用户服务：

| # | 目标 | 验收标准 |
|---|---|---|
| G1 | 鉴权 | 未登录无法访问任何页面与 API；无法绕过前端直接调用后端接口 |
| G2 | 可用 | Ubuntu 单机部署，systemd 托管，重启自愈，TLS 有效 |
| G3 | 检索增强 | 每个 agent 能调搜索 API，回答附带可点击来源 |
| G4 | 异构模型 | 三个 agent 可分别配置 provider 与 model，互不影响 |
| G5 | 投票可视化 | 逐票实时点亮，展示票型、分歧、条件与依据 |
| G6 | 成本可控 | 逐票记录 token 与费用；按用户的次数上限已取消 |

**非目标（v1 明确不做）**

- 公开注册。账号由管理员创建或邀请码开通。
- 移动端原生 App。
- 多机水平扩展（预留 Redis 接口，但 v1 单机 SQLite 即可）。
- 用户自定义人格 / 自定义 agent 数量（配置层已预留，UI 固定三人）。

---

## 2. 已确认决策

| ID | 决策 | 取舍说明 |
|---|---|---|
| D1 | 账号 + 口令登录，服务端会话 | 少量用户，按用户隔离历史与偏好最可控；不做 OAuth，不依赖外部 IdP |
| D2 | 统一 OpenAI 兼容层（`/v1/chat/completions`） | 配 `base_url` 即可接 DeepSeek / OpenRouter / vLLM / Ollama，工具调用格式统一 |
| D3 | 搜索用 Tavily | 面向 LLM 的检索，直接返回摘要与链接，省掉正文抽取 |
| D4 | 共享一次检索 + agent 可追加 | 同一问题检索一次并缓存，三 agent 共用；各自最多追加 2 次定向查询 |
| D5 | FastAPI + React/Vite | 鉴权中间件、async 并发、SSE 流式都是一等公民 |
| D6 | 全量持久化 | 问答、三票、来源、耗时、token 用量入库，支持回看与分享 |
| D7 | SSE 逐票推送 | 每台算完即推送，不做 token 级流式 |
| D8 | 三个 agent 分别为 DeepSeek / Kimi / 千问 | 全走 OpenAI 兼容层，`config/agents.yaml` 一处切换 |
| D9 | 无域名：Let's Encrypt 裸 IP 证书 | 2026-01 起 LE 支持 IP 证书，6 天有效期，须自动续期 |
| D10 | 账号手工创建，不做管理后台 | 建号/改密/封禁用 `python -m app.cli`，省掉一整个后台的鉴权面 |
| D11 | 界面保持日文汉字 | 質 問 / 解 決 / 合 意 / 拒 絶 / 情 報 / 状 態 / 誤 差 原样保留 |
| D12 | 每用户可为每个人格单独选择模型 | 只能从白名单挑，且 provider 必须已配 key；不存用户密钥 |
| D13 | 历史是独立页面，不是侧边栏 | 侧边栏在窄屏与长文本上都难用，独立页可深链 `#id` |
| D14 | 答申长度三档可选（簡潔/標準/詳細） | 长度指令放在 user 消息里，避免切换档位时把前缀缓存切碎 |
| D15 | 判定规则每用户可配（一票否决 / 多数决） | 默认否决；多数决下条件票不计入任何一方，同票则「状 態」 |

### 为什么不用 Dash 升级路线

上游锁定的依赖整体停在 2023 年：`dash==2.9.3`（当前 4.4.1）、`dash-extensions==1.0.0`（2.0.6）、`openai==0.27.6`（3.40.0，`ChatCompletion` 接口已被移除）。其中 `dash-local-react-components` 通过改写 Dash 内部组件注册与构建流程来现场编译 JSX，最后发版为 2024-08。在公网跑三年前的依赖等于放弃安全更新，而强行升级又要赌这个 hook 内部机制的包在 Dash 4 上仍然可用。两者都不适合生产。

**可复用的部分**：`assets/style.css`（纯 CSS 类名，原样搬）、6 个 React 组件（约 100 行，转为 TSX）、三个人格 prompt、投票聚合规则、"处理中"的闪烁语义。

---

## 3. 架构

```
                     ┌──────────────────────────────┐
  Internet ──TLS──►  │ nginx                        │
                     │  · TLS 终止 / HSTS           │
                     │  · 连接限流、请求体大小上限   │
                     │  · SSE: proxy_buffering off   │
                     └──────────────┬───────────────┘
                                    │ 127.0.0.1:8000
                     ┌──────────────▼───────────────┐
                     │ uvicorn (FastAPI)            │
                     │                              │
                     │  AuthMiddleware ── 会话校验   │
                     │  QuotaMiddleware ─ 并发/超时护栏  │
                     │                              │
                     │  POST /api/questions          │
                     │    ├─ 分类器（是否 yes/no）   │
                     │    ├─ 共享检索 ── Tavily      │
                     │    └─ 三个 agent 并发扇出     │
                     │         ├─ Provider 客户端    │
                     │         └─ web_search 工具循环│
                     │                              │
                     │  GET /api/questions/{id}/stream│ ── SSE
                     │  聚合器 → 判决 → 入库         │
                     └──────────────┬───────────────┘
                                    │
                     ┌──────────────▼───────────────┐
                     │ SQLite (WAL)                 │
                     │  users / sessions / questions │
                     │  agent_runs / search_calls    │
                     └──────────────────────────────┘
```

### 目录结构

```
magi/
├── app/                        # 后端
│   ├── main.py                 # FastAPI 装配
│   ├── config.py               # pydantic-settings，读 env + agents.yaml
│   ├── db.py                   # 引擎、会话、迁移
│   ├── models.py               # SQLAlchemy 模型
│   ├── auth/
│   │   ├── passwords.py        # argon2id
│   │   ├── sessions.py         # 服务端会话
│   │   ├── middleware.py       # 页面与 API 统一守卫
│   │   └── deps.py             # current_user
│   ├── api/
│   │   ├── auth.py             # login / logout / me
│   │   ├── questions.py        # 提交、SSE、历史、分享
│   │   └── admin.py            # 用户管理、用量
│   ├── agents/
│   │   ├── provider.py         # OpenAI 兼容客户端（重试/超时/用量）
│   │   ├── runner.py           # 单 agent 执行 + 工具循环
│   │   ├── tools.py            # web_search 工具定义
│   │   └── aggregator.py       # 投票聚合（纯函数，可单测）
│   ├── search/
│   │   ├── base.py             # SearchProvider 协议
│   │   └── tavily.py
│   └── quota.py                # 计数、限额、熔断
├── web/                        # 前端（Vite + React + TS）
│   ├── src/components/         # Magi/WiseMan/Response/Status/Header/Modal
│   ├── src/pages/              # Login / Console / History / Detail
│   ├── src/styles/             # 迁移自 assets/style.css
│   └── src/api/                # fetch 封装 + SSE 客户端
├── config/agents.yaml          # 三个 agent 的 provider/model/persona
├── deploy/
│   ├── nginx.conf
│   ├── magi.service
│   ├── install.sh              # 幂等的部署脚本
│   └── magi.env.example
├── tests/
└── docs/
```

---

## 4. 鉴权与会话（G1）

### 4.1 账号

- 口令用 **argon2id** 哈希（`argon2-cffi`），不存明文、不用 MD5/SHA。
- 口令策略：≥ 12 字符，不比对上万弱口令表（v1 从简，长度即门槛）。
- 账号由管理员创建，或一次性邀请码自助开通（邀请码表：`code, created_by, used_by, expires_at`）。
- `is_active` 封禁即踢下线：会话校验时连带检查用户状态。

### 4.2 会话

服务端会话，不用 JWT（要能立刻吊销）：

| 项 | 值 |
|---|---|
| 令牌 | 32 字节 `secrets.token_urlsafe`，只把 SHA-256 存库 |
| Cookie | `__Host-magi_sid`，`HttpOnly` `Secure` `SameSite=Lax` `Path=/` |
| 空闲过期 | 7 天（每次访问滑动续期） |
| 绝对过期 | 30 天 |
| 吊销 | 登出、改密、封禁时删除记录 |

### 4.3 守卫范围（这是最容易漏的地方）

在 Dash 时代，真正的入口是 `POST /_dash-update-component`；换成 FastAPI 后同理——**只保护页面不管用**，攻击者会直接打 API。

- `AuthMiddleware` 按路径前缀白名单放行，其余一律要求有效会话：
  - 放行：`/login`、`/api/auth/login`、静态资源、`/healthz`
  - 拦截：`/api/**`（除登录）、`/api/questions/*/stream`
- `GET /api/questions/{id}` 与 SSE 额外做**归属检查**：只能看自己的记录，或持有有效 `share_token` 的记录。
- SSE 鉴权：`EventSource` 不能自定义头，但会带 cookie，因此走同一套会话校验。

### 4.4 CSRF

`SameSite=Lax` 已挡住跨站 POST，仍加双保险：每个会话下发 CSRF token，所有非 GET 请求须在 `X-CSRF-Token` 中回传，服务端做常量时间比较。

### 4.5 登录防爆破

- 按 `IP + 账号` 双维度计数：15 分钟内失败 5 次 → 指数退避锁定（1/2/4/8 分钟，上限 30 分钟）。
- nginx 层对 `/api/auth/login` 限 `10r/m`。
- 失败响应统一文案，不区分"用户不存在"与"口令错误"。
- 可选 fail2ban 读取 401/429 日志封 IP（见 §10）。

---

## 5. 成本护栏（G6）

单次提问的真实开销：**1 次分类 + 3 次作答 + 1 次共享检索**，输出上限由用户自选（默认 2000 token/票）。

> **已取消按用户的每日提问次数上限。** 用户明确要求去掉次数限制：
> 次数上限在模型差价面前没有意义（一次 v4-pro 长回答抵得上几十次 flash 短回答）。
> 现在只保留并发、长度与超时三类"技术性"护栏，成本靠可观测性而非硬限来控制。

| 限制 | 默认值 | 说明 |
|---|---|---|
| 每用户并发提问 | 1 | 同一账号同时只允许一问在跑，超出的返回 409 |
| 提问长度 | 8000 字符 | 长文本可贴，超长拒绝 |
| 单票输出上限 | 256–8000 token | 用户在设置页自选，服务端夹紧 |
| 单次请求总超时 | 90s | 超时 agent 转 `abstain`，不拖垮整票 |

计费口径：`agent_runs.cache_hit_tokens / cache_miss_tokens / completion_tokens` 分别计价，
写入 `cost_yuan`；Tavily 按 `search_depth` 记 credits。历史详情里逐票可见。

---

## 6. Agent 层（G3 / G4）

### 6.1 配置

`config/agents.yaml`，密钥只从环境变量取（`${ENV:NAME}` 形式），配置里不出现明文。

```yaml
providers:
  deepseek:
    base_url: https://api.deepseek.com/v1
    api_key: ${ENV:DEEPSEEK_API_KEY}
    timeout_s: 60
  kimi:
    base_url: https://api.moonshot.ai/v1     # 大陆站为 api.moonshot.cn/v1
    api_key: ${ENV:MOONSHOT_API_KEY}
    timeout_s: 90
  qwen:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: ${ENV:DASHSCOPE_API_KEY}
    timeout_s: 60

classifier:                      # 共享的"是否 yes/no 问题"判定
  provider: deepseek
  model: deepseek-flash
  max_tokens: 8

agents:
  - id: melchior
    order: 1
    display: MELCHIOR • 1
    provider: deepseek
    model: deepseek-flash
    temperature: 0.7
    persona: >-
      You are a scientist. Your goal is to further our understanding of the
      universe and advance our technological progress.
    tools: [web_search]
    extra_searches: 2
  - id: balthasar
    order: 2
    display: BALTHASAR • 2
    provider: kimi
    model: kimi-k3.1-pro-highspeed
    reasoning_effort: low
    temperature: 0.7
    persona: >-
      You are a mother. Your goal is to protect your children and ensure their
      well-being.
    tools: [web_search]
    extra_searches: 2
  - id: casper
    order: 3
    display: CASPER • 3
    provider: qwen
    model: qwen3.8-max
    temperature: 0.8
    persona: >-
      You are a woman. Your goal is to pursue love, dreams and desires.
    tools: [web_search]
    extra_searches: 1
```

`persona` 与上游逐字一致，保证行为可比。改人格只改这个文件，不动代码。

### 6.2 单 agent 执行

```
输入：question, shared_sources[], persona, provider 配置
loop (≤ 4 步)：
    resp = chat(messages, tools)
    if resp.tool_calls and 剩余搜索预算 > 0:
        results = search(query)          # 记录 trace
        messages += tool_result
        continue
    return resp.content
```

- 工具用 OpenAI 兼容的 `tools` / `tool_calls` 格式；对不支持工具调用的本地模型，降级为"提示词里附上共享结果，不允许追加检索"。
- 每个 agent 独立 `asyncio.create_task`，`asyncio.gather(return_exceptions=True)`。
- 超时（`provider.timeout_s` + 全局 90s 上限）→ 该 agent 记 `abstain`。
- 重试：仅对 429 / 5xx / 连接错误做 2 次指数退避；4xx 参数错误不重试（重试也没用，白烧钱）。
- **不使用模块级全局 `api_key`**（上游 `ai.py` 的写法在多线程下有竞态）：每个 provider 实例自带 client 与 key。

### 6.3 分类与反问

- 分类器只做一件事：判断"这是不是一个是/否问题"（上游用 `logit_bias` 强逼单 token，依赖 cl100k 的 token id 9642/2822，换模型即失效）。新实现改为：`max_tokens=4` + 提示词约束 + 首个词解析，不绑定分词器。

> **实测踩到的坑（已修）**：DeepSeek 默认开启思考模式，**思维链同样计入 `max_tokens`**。
> 分类调用给了 8 个 token，结果 8 个全被思维链吃掉，`content` 返回空串。
> 更危险的是空串会被解析成"有条件同意"——若问题被判为是非题，三票会静默全变成
> 「状 態」且不报错。修法两条：分类调用传 `{"thinking": {"type": "disabled"}}`；
> 归类返回空内容时抛错（记弃权），而不是当成一张条件票。
- 判为非 yes/no 问题 → 三 agent 正常回答，最终状态 `info`（情 報），不做投票。
- 判为 yes/no 问题 → 三 agent 回答后再做一次归一（yes / no / 条件列表）。

### 6.4 检索

- **共享检索**：问题提交后立即用原问题检索一次（`search_depth=advanced`，`max_results=8`），结果缓存（键：规范化问题 + 日期分桶，TTL 6h），三 agent 共用，保证三票看到同一份事实基础——否则分歧无法归因。
- **追加检索**：agent 通过工具自行发起，受 `extra_searches` 与全局预算双重限制。
- 去重：URL 规范化（去 utm、去尾斜杠）后全局去重，同一链接只展示一次。
- 引用：每条来源存 `title / url / snippet / published_at / rank`，前端可点击，详情页可回溯。

---

## 7. 投票聚合（G5）

上游规则：`error > info > no > conditional > yes`，任一 `no` 即否决。保留其精神，但修掉两处会误伤的语义：

### 7.0 输入形态：疑问句 / 提案 / 都不是

上游只判断"这是不是一个是非疑问句"。实测立刻暴露问题——用户输入的多是**陈述句**：

```
中国是当今世界第一强国
欲望比爱更重要
美国军事实力已经弱于中国
```

这些在语言学上都不是疑问句，于是全部被判为非是否问题、永远走「情 報」。
但 MAGI 的本义是**对提案表决**：陈述句可以改写成"你是否同意"。

因此输入先归成三态：

| form | 判据 | 处理 |
|---|---|---|
| `question` | 可以回答是/否 | 投票 |
| `proposition` | 陈述句，可赞成/反对 | 投票（改写成"你同意吗"） |
| `none` | 求解释、how-to、开放讨论 | 不投票，输出「情 報」 |

`is_yes_no` 保留为派生字段（`form ∈ {question, proposition}`），兼容旧数据。

### 7.0.1 人格必须落在判断上，而不是语气

第一版人格是上游原句（"You are a scientist."），结果是三台用三种口吻说同一个结论——
差异只在语气，投票必然趋同。改为写清**优化什么 / 怎么判 / 盲区在哪**，并要求以
`YES` / `NO` / `CONDITIONAL` 开头表态。这既强制表态，也让票型能从首行直接读出，
省掉每台一次归类调用。

实测（六条真实提案）：

| 提案 | MELCHIOR | BALTHASAR | CASPER | 结论 |
|---|---|---|---|---|
| 中国是当今世界第一强国 | 拒絶 | 拒絶 | 拒絶 | 拒 絶 |
| 欲望比爱更重要 | 拒絶 | 拒絶 | 合意 | 拒 絶（2:1） |
| 碇真嗣爱明日香 | 状態 | 拒絶 | 合意 | 拒 絶（三票各异） |
| 网红经济…值得推广 | 状態 | 状態 | 合意 | 状 態 |
| 中国综合国力仍然弱于美国 | 状態 | 合意 | 合意 | 状 態 |

### 7.1 状态集合

| 状态 | 含义 | UI 文案 | 颜色 |
|---|---|---|---|
| `yes` | 无条件同意 | 合 意 | `#52e691` |
| `no` | 拒绝 | 拒 絶 | `#a41413` |
| `conditional` | 有条件同意 | 状 態 | `#ff8d00` / 斜纹 |
| `abstain` | **新增**：超时、限流、provider 不可用 | 棄 権 | `#6b7280` |
| `info` | 非 yes/no 问题，不投票 | 情 報 | `#3caee0` |
| `error` | 结构性失败（有效票 < 2） | 誤 差 | `#111` |

`abstain` 的引入理由：上游把任何异常都记成 `error`，而 `error` 优先级最高——一次网络抖动就让整个结果显示「誤 差」。弃权不应否决。

### 7.2 判定顺序

```
valid = 三票中状态 ∈ {yes, no, conditional} 的票
if len(valid) < 2:            → error      （达不到法定票数）
elif 任一 no:                 → no         （多数派不敌一票否决，保留 EVA 语义）
elif 任一 conditional:        → conditional
else:                         → yes        （全票无条件同意）
```

非 yes/no 问题走 `info` 分支，不进入上述判定。

### 7.3 展示

- **票型条**：三枚灯，按 agent 顺序排列，各自的颜色与文案；弃权显示为空心。
- **结论**：中央 `Response` 卡片显示最终汉字，并在下方附一行推断说明，例如「拒 絶 · 1 票否决 / 2 票合意」。
- **分歧视图**：任一票与结论不同时，该票标记为「少数意见」，展开可见其理由全文——这是三贤者系统最有价值的信息，上游只藏在弹窗里。
- **降级标记**：存在 `abstain` 时结论卡加一枚 `DEGRADED` 角标，并注明缺票原因。

### 7.4 状态栏

沿用上游的装饰字段，但让它们承载真实信息：

| 上游 | 新含义 |
|---|---|
| `CODE:473` | 固定装饰 |
| `FILE:MAGI_SYS` | 固定装饰 |
| `EXTENTION:7312 / 3023` | 7312 = 已判定为 yes/no 问题；3023 = 非 yes/no；`????` = 分类中 |
| `EX_MODE:OFF` | `ON` 表示本次启用了搜索 |
| `PRIORITY:AAA` | 实时票型，如 `AAB`（A=同意，B=拒绝，C=条件，X=弃权） |

---

## 8. API 与 SSE 协议

### 8.1 REST

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/auth/login` | `{username, password}` → 设会话 cookie |
| `POST` | `/api/auth/logout` | 吊销会话 |
| `GET` | `/api/me` | 当前用户、答申偏好、判定规则、上限 |
| `POST` | `/api/questions` | `{text}` → `{id}`，立即返回，后台执行 |
| `GET` | `/api/questions/{id}/stream` | SSE，逐票推送 |
| `GET` | `/api/questions/{id}` | 完整结果（断线重连/历史详情共用） |
| `GET` | `/api/questions` | 分页历史 |
| `POST` | `/api/questions/{id}/share` | 生成/撤销分享 token |

账号管理不设 HTTP 接口，只走 `python -m app.cli`（create-user / reset-password / disable-user / list-users）。少一个后台就少一整片需要鉴权的攻击面。

错误统一 `{"error": {"code": "...", "message": "..."}}`，`message` 面向用户可读。

### 8.2 SSE 事件

```
event: question.classified   data: {"is_yes_no": true}
event: search.shared         data: {"count": 8, "results": [{"title","url"}]}
event: agent.trace           data: {"agent": "melchior", "query": "...", "hits": 5}
event: agent.vote            data: {"agent": "melchior", "status": "no",
                                    "answer": "...", "conditions": null,
                                    "latency_ms": 4200, "sources": [...]}
event: verdict               data: {"status": "no", "tally": {"no":1,"yes":2},
                                    "degraded": false}
event: done                  data: {}
```

- 每 15s 发 `: keepalive` 注释帧，防代理断连。
- nginx 必须 `proxy_buffering off; proxy_read_timeout 300s;` 并回 `X-Accel-Buffering: no`。
- 断线重连：`Last-Event-ID` 支持从已存事件续推；若已结束，直接重放终态。
- 任务本身不受连接影响：提交后即写入 `questions` 行，SSE 只是观察者。

---

## 9. 前端

- **技术**：Vite + React 18 + TypeScript，无 UI 框架，样式沿用上游 CSS。
- **长文本输入**：用多行编辑器（`Enter` 换行、`Ctrl+Enter` 提交），上限 8000 字。
  真实待决事项常常是整段材料（草案、事故报告、会议记录），单行输入会逼用户删掉约束条件。
  配套约束：**检索 query 只取首段并压平空白（≤300 字）**——整篇文章当 query 只会召回噪声。
- **视觉延续**：`assets/style.css` 整体迁移；三个人格的主色、`flicker` 处理中动画、NERV 终端感的状态栏、`Magi` 的连线布局全部保留。
- **新增页面**：登录页（替换上游的 `access code` 输入框——这一步同时解决了密钥进浏览器的问题）、历史列表、详情页（含分享链接）、管理页。
- **SSE 客户端**：`EventSource` 封装成 hook，按事件类型分发到 `agentStates[3]`，某票到达即更新该卡片的颜色与文案，`verdict` 到达后定格结论。
- **安全**：不使用 `dangerouslySetInnerHTML`；模型输出按纯文本渲染（Markdown 渲染需先过白名单清洗）。- **CSP**：不再有 Dash 那种运行时注入脚本，可上严格策略：`default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'`。字体与图片全部本地化，不引 CDN。

---

## 10. 部署（Ubuntu 24.04 LTS，G2）

### 10.1 进程模型

```
nginx :443 ──► 127.0.0.1:8000 uvicorn (2 workers)
                    │
                    └──► SQLite WAL at /var/lib/magi/magi.db
```

- systemd 单元 `magi.service`：`User=magi`（专用系统用户）、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/magi`、`NoNewPrivileges=yes`、`Restart=always`。
- 密钥放在 `/etc/magi/magi.env`，`chmod 600`，owner `root:magi`，由 `EnvironmentFile=` 注入。
- 前端构建产物（`web/dist`）由 nginx 直接托管静态文件，后端只处理 `/api`。

### 10.2 防火墙（用户标注"待定"，这里给默认基线）

- `ufw default deny incoming` / `default allow outgoing`
- 只放行 `OpenSSH`、`80`、`443`；uvicorn 端口**绑定 127.0.0.1**，不经过防火墙也不对外。
- 可选叠加 Cloudflare（隐藏源站 IP + 自带 DDoS 与 WAF），代价是 SSE 要确认其不回缓冲。
- `fail2ban` 监控 nginx 的 401/429，10 分钟内 20 次即封 1 小时。

### 10.3 TLS：没有域名，用裸 IP 证书

Let's Encrypt 自 2026-01-15 起签发 IP 地址证书（GA），但**必须是 6 天有效期的 shortlived 档**。Certbot ≥ 5.4：

```bash
# 先用 staging 验证链路，再去掉 --staging
certbot certonly --webroot --webroot-path /var/www/certbot \
  --preferred-profile shortlived \
  --ip-address 203.0.113.10 \
  --deploy-hook 'systemctl reload nginx'
```

要点：

- nginx 插件**尚不支持** IP 证书，只能 webroot/standalone/manual；因此 nginx 配置里手写
  `ssl_certificate /etc/letsencrypt/live/203.0.113.10/fullchain.pem;` 及对应 `privkey.pem`。
- 6 天有效期意味着续期失败两天就会全站不可用。`certbot.timer` 默认每天跑两次，够用，但必须监控：
  证书剩余 < 3 天时告警。
- 80 端口必须保持可达（HTTP-01 校验）。
- 浏览器对 IP 证书的支持已经完整，不会再有自签证书那种红屏。
- 若将来买了域名，换成普通 90 天证书即可，代码无需改动。

HSTS（先 1 天，稳定后 1 年）、`X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`、`X-Frame-Options: DENY`、CSP 见 §9。

### 10.4 运维

- 备份：每日 `sqlite3 .backup` 到 `/var/backups/magi/`，保留 14 天，systemd timer 驱动。
- 日志：结构化 JSON 到 journald；记录 `request_id / user_id / question_id / agent / model / tokens / cost`，**默认不写 prompt 与回答正文**（隐私），需要排障时用管理员开关临时开启。
- 健康检查：`GET /healthz`（不鉴权，仅返回 `ok` 与版本），供 nginx 与外部监控使用。
- 压测：上线前用 20 并发跑一轮，确认 SSE 不断流、SQLite 未出现 `database is locked`。

---

## 11. 里程碑

| 阶段 | 内容 | 完成标志 |
|---|---|---|
| M0 | 骨架迁移 | 目录就位，CSS 与组件搬完，假 agent 跑通整条 SSE 链路 |
| M1 | 鉴权 | 登录/登出/会话/守卫/CSRF/防爆破，未登录打 API 全部 401 |
| M2 | Agent 与投票 | 三模型并发、逐票推送、聚合落库，聚合器单测覆盖 |
| M3 | 检索 | Tavily 接入、共享检索缓存、工具循环、引用展示、预算控制 |
| M4 | 历史与分享 | 列表、详情、分享链接、用量看板 |
| M5 | 部署 | nginx/systemd/ufw/certbot/备份，公网可访问且 TLS 有效 |
| M6 | 加固 | CSP、安全头、fail2ban、成本熔断、压测报告 |

每阶段结束跑 `pytest`，鉴权与聚合两块必须有测试。

---

## 12. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 公开站被刷额度 | 账单失控 | 逐票计量 + 并发上限；次数上限已按需求取消，需要时再加全局熔断 |
| 本地小模型不支持工具调用 | agent 行为不一致 | 检测能力后降级为"只用共享检索结果"，并在 UI 标注该 agent 未启用追加检索 |
| 三模型延迟差异大 | 逐票体验割裂 | 每 agent 独立超时（默认 60s）+ 全局 90s；超时转弃权并推送 |
| Tavily 配额或不可用 | 无检索，退化为纯模型 | `SearchProvider` 可替换；失败时降级为无检索并标注 |
| SQLite 单写并发 | 回调阻塞 | WAL + 短事务；写入集中在前台请求，后台任务不持有长锁 |
| 供应商 ToS / 数据流向 | 合规 | 公网部署前确认各 provider 的数据使用条款；敏感问题可在配置里关掉某 agent |

---

## 13. 决策落定情况

| 项 | 结论 |
|---|---|
| 域名与证书 | 无域名 → Let's Encrypt 裸 IP 证书（shortlived，6 天，自动续期） |
| 账号开通 | 管理员在服务器上 `python -m app.cli create-user`，不做邀请码 |
| 管理后台 | 不做。建号/改密/封禁/查询全部走 CLI |
| 模型选型 | MELCHIOR = `deepseek-flash`；BALTHASAR = `kimi-k3.1-pro-highspeed`；CASPER = `qwen3.8-max` |
| 界面语言 | 全量日文汉字，沿用上游语汇 |

### 仍需你定的两件事

1. **预算上限**：默认 `MAGI_GLOBAL_DAILY_BUDGET_YUAN=30`、每人每日 20 问。按当前选型粗估，
   单次提问约 ¥0.1–0.6（`kimi-k3.1-pro` 单价最高），20 问/人/天的上限对应单人日成本可达 ¥10 量级。
   如果人均用户多或有人反复试探，30 元/天可能不够。
2. **Kimi 与千问的单价**：`config/agents.yaml` 里两家的 `price_*` 目前填的是 0（待填），
   不填则成本统计会低估，熔断阈值失效。上线前需要按控制台计价补齐。

---

## 14. 实施进度

| 阶段 | 状态 | 说明 |
|---|---|---|
| M0 骨架迁移 | 完成 | 上游 Dash 代码移入 `legacy/`；`pyproject.toml`、配置、SQLite 模型就位 |
| M1 鉴权 | 完成 | argon2id、服务端会话、守卫中间件、CSRF、登录退避锁定、账号 CLI |
| M2 Agent 与投票 | 完成 | provider 客户端（重试/超时/用量记账）、三路并发、SSE 逐票、聚合器；39 项测试通过 |
| M3 检索 | 部分完成 | Tavily 共享检索、6 小时缓存、引用入库与展示、失败降级；agent 追加检索工具未做 |
| M4 前端 | 进行中 | 过渡控制台已复用上游 `style.css`（`/`），React/Vite 待做 |
| M5 部署 | 未开始 | nginx、systemd、ufw、IP 证书、备份 |
| M6 加固 | 未开始 | CSP、fail2ban、成本熔断实测、压测 |

### 过渡实现说明

M2 阶段先做了一个不依赖 Node 工具链的控制台（`app/static/console.html`），
直接复刻上游 DOM 结构并复用 `legacy/assets/style.css`，因此视觉与交互
（三台闪烁、逐票点亮、点击展开详情、状态栏）与原版一致。它不是废弃代码：
即使 M4 的 React 应用上线，这套无构建页面仍可作为降级路径保留。

### 当前配置状态

三家 agent **暂时全部指向 DeepSeek**（`deepseek-flash`），用于先打通链路。
这同时构成一个干净的对照实验：若三台在同模型下仍给出不同票型，
分歧就只能来自人格提示，而非模型差异。换回异构模型只需改
`config/agents.yaml` 里的 `provider` / `model` 两行。

---

## 15. 上下文缓存实测

DeepSeek 对相同前缀做上下文缓存，命中价约为未命中的 1/50。实测规律：

| 稳定前缀长度 | 第二次请求命中 | 命中率 |
|---|---|---|
| 367 token | 128 | 35% |
| 468 | 256 | 55% |
| 676 | 512 | 76% |
| 1092 | 896 | 82% |

结论：**命中率的上限 ≈ 稳定前缀 / 总 prompt**。缓存按 64 token 分块，
尾部（问题本身）永远 miss，所以前缀越长、占比越高，命中率才越高。

### 15.1 我们踩的两个坑

1. **没有解析缓存字段**。`usage` 里的 `prompt_cache_hit_tokens` /
   `prompt_cache_miss_tokens` 一直被忽略，成本按全价估算——系统性高估。
   现已按 2 元/M（miss）与 0.04 元/M（hit）分开计价，并落库、在详情页展示。
2. **注入的上下文过长**。每问把 8 条、每条 900 字的检索摘要塞进 prompt，
   总 prompt 达 4400 token，600 token 的系统提示被淹没，命中率只有 10%，
   单票成本 ¥0.015。

### 15.2 调整后

- 系统提示从 367 扩到 617 token，加的是**有约束力的判定规则**（证据纪律、
  可反驳性、代价不对称、范围纪律、两种失败模式），不是为凑长度灌水。
- 注入 prompt 的摘要截到 400 字（存储仍保留全文供详情页查看）。

| 指标 | 调整前 | 调整后 |
|---|---|---|
| 单次 prompt | ≈4400 token | ≈2700 token |
| 缓存命中率 | 8–12% | 23–30% |
| 单票成本 | ¥0.013–0.019 | ¥0.008–0.013 |
| 单问成本 | ≈¥0.050 | ≈¥0.030 |

### 15.3 还能怎么做

- **继续减上下文**：摘要再短、条数再少，收益立竿见影，但要权衡答案质量。
- **把共享块放前缀**：三台共用同一份检索结果，但人格在前、内容在后，
  三台无法互相命中；改成内容在前需要先发一次预热请求，多一次调用不划算。
- **响应级缓存**：同一问题直接复用上次结果，命中一次省 100%，比任何前缀优化都值——
  这也是 `search_calls` 已有 6 小时缓存、但回答还没做的部分。
