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

### 接 DeepSeek

在 `.env` 里填 `DEEPSEEK_API_KEY`，保持默认的 `config/agents.yaml` 即可——
当前三台都指向 `deepseek-flash`，人格设定各不相同，因此"同一模型 × 三种人格"
本身就构成一个对照实验。

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
- 换模型只改 YAML，不动代码；密钥只从环境变量读，不写进仓库。

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
deploy/         nginx / systemd / 安装脚本（M5 阶段）
legacy/         上游 Dash 实现，仅作参照
docs/           设计文档
tests/
```

## 许可

代码沿用上游 MIT 许可，见 [LICENSE](LICENSE)。人格设定与原版逐字一致，以保证行为可比。
