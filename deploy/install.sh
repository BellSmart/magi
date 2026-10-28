#!/usr/bin/env bash
# MAGI 安装脚本（Ubuntu 22.04 / 24.04）
#
# 幂等：可以重复运行。已存在的 /etc/magi/magi.env 与已签发的证书不会被覆盖。
# 用法：sudo bash deploy/install.sh

set -euo pipefail

APP_DIR=/opt/magi
DATA_DIR=/var/lib/magi
ETC_DIR=/etc/magi
WEBROOT=/var/www/certbot
SERVICE_USER=magi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash deploy/install.sh"
[ -f "$SRC_DIR/pyproject.toml" ] || die "找不到 pyproject.toml，请在仓库根目录执行"

say "系统依赖"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-venv python3-pip nginx rsync sqlite3 curl ca-certificates

say "系统用户与目录"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$APP_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR"
install -d -o root -g "$SERVICE_USER" -m 0750 "$ETC_DIR"
install -d -o www-data -g www-data -m 0755 "$WEBROOT"

say "同步代码 → $APP_DIR"
rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude 'data' --exclude '.env' \
    --exclude '__pycache__' --exclude '*.pyc' \
    "$SRC_DIR"/ "$APP_DIR"/
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

say "Python 虚拟环境"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"

# 国内机器直连 files.pythonhosted.org 经常超时，允许用 PIP_INDEX_URL 指定镜像：
#   PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ bash deploy/install.sh
PIP_OPTS="--quiet --timeout 60 --retries 5"
if [ -n "${PIP_INDEX_URL:-}" ]; then
    PIP_OPTS="$PIP_OPTS -i $PIP_INDEX_URL"
    echo "使用 PyPI 镜像：$PIP_INDEX_URL"
fi
# shellcheck disable=SC2086
"$APP_DIR/.venv/bin/pip" install $PIP_OPTS --upgrade pip
# shellcheck disable=SC2086
"$APP_DIR/.venv/bin/pip" install $PIP_OPTS -e "$APP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.venv"

say "环境变量文件"
if [ ! -f "$ETC_DIR/magi.env" ]; then
    SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    install -m 0640 -o root -g "$SERVICE_USER" \
        "$SRC_DIR/deploy/magi.env.example" "$ETC_DIR/magi.env"
    sed -i "s|__GENERATE__|$SECRET|" "$ETC_DIR/magi.env"
    NEED_KEYS=1
else
    NEED_KEYS=0
fi
chown root:"$SERVICE_USER" "$ETC_DIR/magi.env"
chmod 0640 "$ETC_DIR/magi.env"

say "命令行包装器 /usr/local/bin/magi-cli"
install -m 0755 "$SRC_DIR/deploy/magi-cli" /usr/local/bin/magi-cli

say "systemd 服务"
install -m 0644 "$SRC_DIR/deploy/magi.service" /etc/systemd/system/magi.service
systemctl daemon-reload
systemctl enable magi.service >/dev/null
systemctl restart magi.service

say "nginx"
# 站点配置会 include 这两个文件，缺了 nginx -t 直接失败，所以先装。
install -d -m 0755 /etc/nginx/conf.d /etc/nginx/snippets
install -m 0644 "$SRC_DIR/deploy/nginx-limits.conf" /etc/nginx/conf.d/00-magi-limits.conf
install -m 0644 "$SRC_DIR/deploy/nginx-security.inc" /etc/nginx/snippets/magi-security.conf
if grep -qs "letsencrypt" /etc/nginx/sites-available/magi; then
    echo "已配置 HTTPS，保持现有站点配置不动"
else
    install -m 0644 "$SRC_DIR/deploy/nginx-http.conf" /etc/nginx/sites-available/magi
fi
ln -sfn /etc/nginx/sites-available/magi /etc/nginx/sites-enabled/magi
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

say "健康检查"
sleep 2
if curl -fsS http://127.0.0.1:8000/healthz >/dev/null; then
    echo "应用已启动：$(curl -fsS http://127.0.0.1:8000/healthz)"
else
    echo "应用未响应，看日志：journalctl -u magi -n 50 --no-pager"
fi

cat <<'EOF'

────────────────────────────────────────────────────────
安装完成。接下来三步：

1) （已不需要）服务端不再持有厂商密钥，用户各自在「設 定」页填写
     sudo nano /etc/magi/magi.env
     sudo systemctl restart magi
   应用启动时会检查并警告缺失的 key：
     journalctl -u magi -n 20 --no-pager | grep -i provider

2) 签发 IP 证书并切到 HTTPS（需要 80 端口对外可达）
     sudo bash deploy/enable-tls.sh <公网IP> you@example.com

3) 防火墙（若尚未启用）
     sudo ufw allow OpenSSH
     sudo ufw allow 80,443/tcp
     sudo ufw --force enable

4) 公网加固（限速 / fail2ban / ufw / 内核参数 / 自动安全更新）
     sudo bash deploy/harden.sh
   SSH 端口不是 22 时：SSH_PORT=2222 sudo bash deploy/harden.sh

创建账号：
     sudo magi-cli create-user --username alice
────────────────────────────────────────────────────────
EOF

if [ "${NEED_KEYS:-0}" -eq 1 ]; then
    printf '\033[1;33m提示：%s 是刚生成的；厂商 API キー由各用户自己在页面上填写。\033[0m\n' "$ETC_DIR/magi.env"
fi
