# 部署到 Ubuntu

面向 Ubuntu 22.04 / 24.04，单机。整套东西由三个文件组成：

| 文件 | 作用 |
|---|---|
| `deploy/install.sh` | 建用户、装依赖、同步代码、建 venv、写 systemd、配 nginx（HTTP 引导态） |
| `deploy/enable-tls.sh` | 签 Let's Encrypt 的 **IP 证书**，把 nginx 切到 HTTPS |
| `deploy/magi-cli` | 账号管理命令的包装器，自动载入 `/etc/magi/magi.env` 并降权到 `magi` |

拓扑：

```
Internet ──443──► nginx（TLS、SSE 不缓冲）──► 127.0.0.1:8000 uvicorn
                                                  └──► /var/lib/magi/magi.db
```

---

## 0. 前置

- 一台有**公网 IPv4** 的 Ubuntu，能 root
- 80 与 443 端口对外可达（IP 证书的校验与续期都依赖 80）
- 一个 DeepSeek API key；Tavily key 可选（不给就不检索）
- **没有域名**也能跑：证书签给 IP 本身

## 1. 把代码送上去

推荐先用 git 推到自己的仓库，服务器上 clone，后续更新只要 `git pull`：

```bash
# 服务器
sudo apt-get install -y git
sudo git clone <你的仓库地址> /root/magi-src
```

不想用 git 也可以从 Windows 直接推（PowerShell 自带 scp）：

```powershell
# 排除 .venv 与 data，否则会传几百兆
robocopy D:\magi C:\temp\magi-deploy /E /XD .venv data .git __pycache__ /XF .env
scp -r C:\temp\magi-deploy user@<服务器IP>:/root/magi-src
```

> 不要传 `.env`。生产配置由服务器自己生成在 `/etc/magi/magi.env`。

## 2. 安装

```bash
cd /root/magi-src
sudo bash deploy/install.sh
```

它会做：装 python3-venv/nginx/rsync → 建系统用户 `magi` → 同步代码到 `/opt/magi` →
建 venv 并 `pip install -e` → 生成 `/etc/magi/magi.env`（含随机 `MAGI_SECRET_KEY`）→
装 `magi.service` 并启动 → 写 nginx 引导配置（此时除 ACME 校验外一律 503）。

## 3. 填 API key

```bash
sudo nano /etc/magi/magi.env     # DEEPSEEK_API_KEY / TAVILY_API_KEY
sudo systemctl restart magi
journalctl -u magi -n 20 --no-pager | grep -i provider   # 缺 key 会在这里点名
```

## 4. 签证书、切 HTTPS

```bash
sudo bash deploy/enable-tls.sh <公网IP> you@example.com
```

脚本会用 snap 装 certbot（apt 版本太旧，不支持 `--ip-address`），以
`--preferred-profile shortlived` 签发 6 天有效期的 IP 证书，然后把 nginx 换成
完整配置并 reload。

**这一步会在证书签发前保持 503** —— 在 TLS 就绪前不暴露明文登录。

## 5. 防火墙

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw --force enable
```

uvicorn 只监听 `127.0.0.1:8000`，不需要为它开端口。

## 6. 建账号

```bash
sudo magi-cli create-user --username alice
sudo magi-cli list-users
```

口令默认要求 12 位；确需弱口令时加 `--allow-weak-password`（会打印警告）。

## 7. 验证清单

```bash
curl -fsS https://<IP>/healthz                       # {"status":"ok","version":"0.1.0"}
curl -sI https://<IP>/ | head -1                     # 303（未登录跳转）
systemctl list-timers | grep -i certbot              # 续期任务存在
openssl x509 -in /etc/letsencrypt/live/<IP>/fullchain.pem -noout -dates
```

浏览器打开 `https://<IP>/`，登录后提一个问题，确认：

- 三块盾牌**逐块点亮**（不是等到最后一起亮）→ 说明 SSE 没被 nginx 缓冲
- 参考资料面板出现链接
- 历史页能按天折叠、能删除

---

## 日常运维

**更新代码**

```bash
cd /root/magi-src && sudo git pull
sudo bash deploy/install.sh        # 幂等，会保留 magi.env 与证书
```

**日志**

```bash
journalctl -u magi -f
journalctl -u magi --since "1 hour ago" | grep -i error
```

**备份**（SQLite 必须用 `.backup`，直接复制文件在 WAL 模式下可能不一致）

```bash
sudo install -d -o magi -g magi /var/backups/magi
sudo -u magi sqlite3 /var/lib/magi/magi.db ".backup '/var/backups/magi/magi-$(date +%F).db'"
```

放进 crontab（`sudo crontab -e`）：

```cron
17 4 * * * sqlite3 /var/lib/magi/magi.db ".backup '/var/backups/magi/magi-$(date +\%F).db'" && find /var/backups/magi -name 'magi-*.db' -mtime +14 -delete
```

> 备份里含用户的问答内容与检索记录，按敏感数据处理。

---

## 排错

| 现象 | 原因与处理 |
|---|---|
| 浏览器 503 `TLS is not configured` | 还在引导态，执行第 4 步签证书 |
| `https://IP` 打不开但 `curl 127.0.0.1:8000/healthz` 正常 | nginx 没起来或配置没生效：`nginx -t`、`systemctl status nginx` |
| 登录成功却立刻跳回登录页 | cookie 带 `Secure` 但你在用 http 访问；本项目的 `__Host-` 前缀要求 https |
| 三块盾牌一起亮、不是逐块 | nginx 缓冲没关：确认用的是 `deploy/nginx-tls.conf`（含 `proxy_buffering off`） |
| 服务起不来看 `Read-only file system` | `/var/lib/magi` 权限或 `MAGI_DATABASE_URL` 指向了别处；`ProtectSystem=strict` 只放行 `/var/lib/magi` |
| 某台总在 90 秒后弃权 | 思考强度设成了 MAX 且问题复杂；调低强度或改 `provider.timeout_s` |
| 证书 6 天后过期 | certbot 定时器没跑：`systemctl list-timers \| grep certbot`，或手动 `certbot renew` |

## 与设计文档的差异

`docs/DESIGN.md` §10 描述了 nginx 托管前端静态文件、Redis、迁移工具等；
当前实现更简单：**所有请求（包括静态资源）都走 uvicorn**。
在这个量级（三个用户）下没有性能差异，却少了一层路径重复。
等前端换成 Vite 构建产物、或并发上来之后，再把静态资源交给 nginx。
