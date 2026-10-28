#!/usr/bin/env bash
# MAGI 公网加固：nginx 限速/安全头、fail2ban、ufw、内核参数、自动安全更新。
#
# 幂等，可以反复跑。
#   sudo bash deploy/harden.sh
#   SSH_PORT=2222 PORT=8080 sudo bash deploy/harden.sh   # 端口不是默认值时
#
# 注意：这一步会开启 ufw。脚本先放行 SSH 端口再 enable，所以当前连接不会断；
# 但如果你改过 sshd 端口又没传 SSH_PORT，会把自己锁在外面——先确认端口再跑。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$HERE/.." && pwd)"

NGINX_SITE=/etc/nginx/sites-available/magi
NGINX_LINK=/etc/nginx/sites-enabled/magi
BACKUP_DIR=/var/backups/magi
SSH_PORT="${SSH_PORT:-22}"
PORT="${PORT:-8080}"

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; }

[ "$(id -u)" -eq 0 ] || { warn "请用 root 运行：sudo bash deploy/harden.sh"; exit 1; }
[ -f "$SRC_DIR/deploy/nginx-limits.conf" ] || { warn "找不到 $SRC_DIR/deploy/nginx-limits.conf"; exit 1; }

install -d "$BACKUP_DIR"

# --- 1. nginx：限速区 + 安全头 ----------------------------------------------
say "nginx 限速与安全头"
install -d -m 0755 /etc/nginx/conf.d /etc/nginx/snippets
install -m 0644 "$SRC_DIR/deploy/nginx-limits.conf" /etc/nginx/conf.d/00-magi-limits.conf
install -m 0644 "$SRC_DIR/deploy/nginx-security.inc" /etc/nginx/snippets/magi-security.conf
echo "已安装 /etc/nginx/conf.d/00-magi-limits.conf"
echo "已安装 /etc/nginx/snippets/magi-security.conf"

# --- 2. 站点配置：没有 include 就换成模板 -----------------------------------
say "站点配置"
SITE_BACKUP=""
if [ -f "$NGINX_SITE" ] && grep -qs 'snippets/magi-security.conf' "$NGINX_SITE"; then
    echo "现有配置已包含安全片段，保持不变"
else
    if [ -f "$NGINX_SITE" ]; then
        STAMP="$(date +%Y%m%d-%H%M%S)"
        SITE_BACKUP="$BACKUP_DIR/nginx-site-$STAMP.conf"
        cp -a "$NGINX_SITE" "$SITE_BACKUP"
        echo "旧配置已备份：$SITE_BACKUP"
    fi
    CERT="$(ls /etc/letsencrypt/live/*/fullchain.pem 2>/dev/null | head -n1 || true)"
    if [ -n "$CERT" ]; then
        IP="$(basename "$(dirname "$CERT")")"
        sed "s|__IP__|$IP|g" "$SRC_DIR/deploy/nginx-tls.conf" > "$NGINX_SITE"
        echo "检测到证书（$IP），写入 nginx-tls.conf"
    else
        install -m 0644 "$SRC_DIR/deploy/nginx-8080.conf" "$NGINX_SITE"
        echo "没有证书，写入 nginx-8080.conf（监听 $PORT）"
    fi
fi
ln -sfn "$NGINX_SITE" "$NGINX_LINK"
rm -f /etc/nginx/sites-enabled/default

if nginx -t 2>/tmp/magi-nginx-test.log; then
    systemctl reload nginx
    echo "nginx 配置校验通过，已 reload"
else
    warn "nginx 配置校验失败："
    cat /tmp/magi-nginx-test.log >&2
    if [ -n "$SITE_BACKUP" ]; then
        cp -a "$SITE_BACKUP" "$NGINX_SITE"
        warn "已恢复旧站点配置：$SITE_BACKUP（nginx 未 reload，线上保持原样）"
    fi
    exit 1
fi

# --- 3. fail2ban ------------------------------------------------------------
say "fail2ban"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban >/dev/null

cat > /etc/fail2ban/filter.d/magi-auth.conf <<'FILTER'
# 登录失败：nginx 记的是 POST /api/auth/login ... 401
[Definition]
failregex = ^<HOST> - \S+ \[[^\]]+\] "POST /api/auth/login[^"]*" 40[13]\b
ignoreregex =
FILTER

cat > /etc/fail2ban/filter.d/magi-ratelimit.conf <<'FILTER'
# 被 nginx 限速挡下（429）说明这个 IP 在刷
[Definition]
failregex = ^<HOST> - \S+ \[[^\]]+\] "[^"]*" 429\b
ignoreregex =
FILTER

cat > /etc/fail2ban/jail.d/magi.local <<'JAIL'
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 6

[magi-auth]
enabled = true
filter = magi-auth
logpath = /var/log/nginx/access.log
maxretry = 6
findtime = 10m
bantime = 2h

[magi-ratelimit]
enabled = true
filter = magi-ratelimit
logpath = /var/log/nginx/access.log
maxretry = 30
findtime = 5m
bantime = 30m

[sshd]
enabled = true
JAIL

systemctl enable fail2ban >/dev/null 2>&1 || true
if systemctl restart fail2ban; then
    sleep 1
    fail2ban-client status 2>/dev/null | sed 's/^/  /' || true
else
    warn "fail2ban 启动失败，看 journalctl -u fail2ban -n 30"
fi

# --- 4. ufw ----------------------------------------------------------------
say "ufw"
if command -v ufw >/dev/null 2>&1; then
    ufw allow "$SSH_PORT"/tcp >/dev/null
    ufw allow "$PORT"/tcp >/dev/null
    ufw --force enable >/dev/null
    ufw status verbose | sed 's/^/  /'
    echo
    warn "注意：docker 发布端口（30033/9987 等）会绕过 ufw，在 docker 里跑的服务 Internet 仍然可达。"
else
    echo "未安装 ufw，跳过"
fi

# --- 5. 内核参数 ------------------------------------------------------------
say "内核参数"
cat > /etc/sysctl.d/99-magi-hardening.conf <<'SYSCTL'
# SYN cookies：SYN flood 时不至于把半连接队列打满
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 4096
net.core.somaxconn = 1024

# 不收 ICMP 重定向、不收源路由，防路由劫持
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0

# 广播 ICMP 不回应（smurf 放大）
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1

# 短连接回收快一点
net.ipv4.tcp_fin_timeout = 15
SYSCTL
sysctl --system >/dev/null
echo "已应用 /etc/sysctl.d/99-magi-hardening.conf"

# --- 6. 自动安全更新 --------------------------------------------------------
say "自动安全更新"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unattended-upgrades >/dev/null
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'AUTO'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
AUTO
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
echo "已启用 unattended-upgrades（只装安全补丁，不自动重启服务）"

# --- 7. 自检 ----------------------------------------------------------------
say "自检"
curl -fsS --max-time 5 "http://127.0.0.1:8000/healthz" && echo
for path in /.env /.git/config /api/auth/login; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT$path" || true)
    printf '  %-18s -> %s\n' "$path" "$code"
done
echo
echo "完成。日常查封禁：fail2ban-client status magi-auth"
echo "DDoS 说明：这里的限速只能挡应用层洪水，真正的流量型攻击要靠机房清洗或 Cloudflare。"
