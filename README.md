# MAGI

三贤者投票系统：同一问题同时交给三个带不同人格设定的 agent，各自检索、各自作答，
再按固定规则聚合成一个结论。

本仓库基于 [TomaszRewak/MAGI](https://github.com/TomaszRewak/MAGI)（MIT）重写：
原版是本地 Dash demo（单一模型、浏览器传 API key），这一版是可公网部署的多用户服务。
原始实现完整保留在 [`legacy/`](legacy/)，作为行为与视觉的参照。

设计决策、安全模型、SSE 协议、部署方案见 **[docs/DESIGN.md](docs/DESIGN.md)**。

## 三个子系统

| Agent | 人格 | 模型 |
|---|---|---|
| MELCHIOR • 1 | 科学家：追求对宇宙的理解与技术进展 | DeepSeek `deepseek-flash` |
| BALTHASAR • 2 | 母亲：保护孩子并确保其福祉 | Kimi `kimi-k3.1-pro-highspeed` |
| CASPER • 3 | 女人：追求爱、梦想与欲望 | 千问 `qwen3.8-max` |

投票规则：任一「拒絶」即否决，全部无条件同意才「合 意」，存在有条件同意则「状 態」，
非是/否问题输出「情 報」。超时或不可用的 agent 记「棄 権」，不参与否决，
但有效票不足 2 张时整体为「誤 差」。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env
python -m app.cli gen-secret       # 把输出填进 .env 的 MAGI_SECRET_KEY

python -m app.cli create-user --username alice
uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000/login> 登录，登录后进入控制台。

### 先看界面（不需要 API key）

```bash
MAGI_AGENTS_CONFIG_PATH=config/agents.mock.yaml uvicorn app.main:app --reload --port 8000
```

mock provider 不发任何网络请求，三台分别给出 yes / no / yes，可以直接看到
逐票推送、一票否决和详情弹窗。链路与真实调用完全一致，只是模型换成了假的。

### 接真实的模型

这个项目只提供框架：**服务端不保存任何厂商密钥**。每个用户登录后到「設 定」页，
先从下拉选单里挑服务商、粘自己的 API キー，再在「人 格 モ デ ル」里挑各人格要用的模型。
key 按用户 AES-GCM 加密存库，费用也记在各自的账号上。

内置的服务商（`config/agents.yaml` 的 `providers`）：

| provider | 说明 | base_url |
|---|---|---|
| `deepseek` | DeepSeek 官方 | `https://api.deepseek.com/v1` |
| `kimi` | 月之暗面（大陆站为 `api.moonshot.cn`） | `https://api.moonshot.ai/v1` |
| `qwen` | 阿里云百炼 / DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `siliconflow` | 硅基流动（MaaS 聚合，一个 key 通吃多家开源模型） | `https://api.siliconflow.cn/v1` |
| `openrouter` | OpenRouter（海外聚合） | `https://openrouter.ai/api/v1` |
| `zhipu` | 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` |

```bash
# 用户侧的操作就三步：
#   登录 → 設定 → A P I キ ー → 选服务商、贴 key、保存
#   設定 → 人 格 モ デ ル → 每台挑一个该服务商的模型 → 保存
```

**接别家**（vLLM、Ollama、自建网关，或者表里没列的服务商）只改 YAML 两处，不用动代码：

```yaml
providers:
  my_gateway:
    label: 我的网关
    base_url: https://llm.example.com/v1
    api_key: ""            # 留空：真正的 key 由用户在设置页填

models:
  - provider: my_gateway
    model: qwen3-32b
    label: 我的网关 · Qwen3 32B
```

之所以要白名单：`base_url` 由服务端决定，用户只能填 key。若允许用户自带地址，
就等于把「往任意主机发请求」和「把别人的 key 送到任意主机」两件事一起开放了（SSRF）。

> 模型 ID 各家会更新换代，配置里写的只是常用默认值。填之前用
> `curl <base_url>/models -H "Authorization: Bearer $KEY"` 核对一下。
>
> 检索目前用的是 DeepSeek 的自研搜索（与模型共用同一个 key）。如果用户只配了别家
> 的 key，检索会跳过，三位 agent 直接凭模型知识作答——要恢复检索，配一个 DeepSeek key，
> 或在 `config/agents.yaml` 里把 `search.provider` 切回 `tavily` 并填 `TAVILY_API_KEY`。

## 部署与更新

单机部署：systemd + nginx + uvicorn，脚本都在 [`deploy/`](deploy/) 里，
完整步骤与排错见 **[deploy/README.md](deploy/README.md)**。

```bash
# 首次安装（Ubuntu 22.04 / 24.04）
sudo bash deploy/install.sh

# 之后更新：源码目录或 tar.gz 都可以
sudo bash deploy/update.sh
sudo bash deploy/update.sh /root/magi-20260101.tar.gz
```

`update.sh` 的动作按顺序是：把数据库快照到 `/var/backups/magi/`（SQLite 用 `.backup`，
只留最近 10 份）→ `rsync` 覆盖 `/opt/magi` → `pyproject.toml` 有变化才 `pip install -e` →
`systemctl restart magi` → 轮询 `/healthz` 最多 30 秒，失败时打印 journal 并给出回滚命令。
它**不动** nginx 配置、`/etc/magi/magi.env` 和数据库内容；表结构变更由应用启动时自动迁移。

### 公网加固

部署到公网时先跑一遍：

```bash
sudo bash deploy/harden.sh          # sshd 不是 22 就 SSH_PORT=2222 ...
```

它会装 nginx 限速（登录 10 次/分、提问 6 次/分、单 IP 并发 20）、安全响应头与
扫描路径 404、fail2ban（登录爆破与 429 洪水）、ufw（只放行 SSH 与站点端口）、
SYN cookies 一类内核参数，以及只装安全补丁的 unattended-upgrades。幂等，可重复跑。

应用本身的对策是：Argon2 口令 + 登录失败锁定、HttpOnly/SameSite=Lax 会话 cookie、
CSRF 双提交校验、SQLAlchemy 全参数化查询、前端只用 `textContent` 渲染外部内容、
模型 `base_url` 服务端白名单（防 SSRF）。**限速只能在应用层挡洪水**，真正的流量型
DDoS 要靠机房清洗或 Cloudflare，那需要一个域名。详见 [deploy/README.md](deploy/README.md) 第 5 步。

## 账号管理

没有管理后台，账号操作都在服务器上执行：

```bash
python -m app.cli create-user --username alice [--admin] [--quota 50]
python -m app.cli reset-password --username alice   # 同时吊销该用户全部会话
python -m app.cli disable-user --username alice     # 立即踢下线
python -m app.cli enable-user --username alice
python -m app.cli list-users
```

用户名**大小写敏感**（`Alice` 与 `alice` 是两个账号）。口令默认要求 ≥ 12 位；
确有必要时可加 `--allow-weak-password` 跳过校验，CLI 会打印警告。

口令走 `getpass` 交互输入；在脚本里可用管道喂入：

```bash
echo "$PASSWORD" | python -m app.cli create-user --username alice
```

## 测试

```bash
pytest
```

鉴权与投票聚合是重点覆盖对象：未登录必须打不进任何接口、CSRF 必须校验、
封禁必须立刻生效。

## 配置

- 环境变量见 [`.env.example`](.env.example)，全部以 `MAGI_` 前缀。
- 三个 agent 的模型、人格、检索预算见 [`config/agents.yaml`](config/agents.yaml)。
- 换模型只改 YAML，不动代码；厂商密钥由用户自己填，按用户加密存库。

## 目录

```
app/            后端（FastAPI）
  auth/         口令、会话、守卫中间件
  agents/       provider 客户端、runner、分类器、聚合器
  api/          路由（认证、提问、SSE）
  orchestrator.py  一次提问的编排
  models.py     SQLAlchemy 模型
  cli.py        账号管理
config/         agent 与 provider 配置（真实 / mock 两份）
web/            前端（React + Vite，M4 阶段接入）
deploy/         nginx / systemd / 安装与更新脚本
legacy/         上游 Dash 实现，仅作参照
docs/           设计文档
tests/
```

## 许可

代码沿用上游 MIT 许可，见 [LICENSE](LICENSE)。人格设定与原版逐字一致，以保证行为可比。
