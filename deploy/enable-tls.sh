#!/usr/bin/env bash
# 签发 Let's Encrypt 的 IP 地址证书，并把 nginx 切到 HTTPS。
#
# 没有域名时，这是唯一能拿到"浏览器不报警"证书的方式。
# 注意：IP 证书必须是 6 天有效期的 shortlived 档，靠 certbot 定时任务续期。
#
# 用法：sudo bash deploy/enable-tls.sh <公网IP> <邮箱>

set -euo pipefail

IP="${1:-}"
EMAIL="${2:-${LE_EMAIL:-}}"
WEBROOT=/var/www/certbot
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash deploy/enable-tls.sh <IP> <邮箱>"
[ -n "$IP" ] || die "用法：sudo bash deploy/enable-tls.sh <公网IP> <邮箱>"
[ -n "$EMAIL" ] || die "需要邮箱（Let's Encrypt 的到期通知）"

say "安装 certbot"
# Ubuntu 软件源自带的 certbot 版本太旧，不支持 --ip-address / --preferred-profile。
if ! command -v snap >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq snapd
    systemctl enable --now snapd.socket >/dev/null 2>&1 || true
fi
snap install --classic certbot >/dev/null 2>&1 || snap refresh certbot >/dev/null 2>&1 || true

CERTBOT=/snap/bin/certbot
[ -x "$CERTBOT" ] || CERTBOT="$(command -v certbot || true)"
[ -n "$CERTBOT" ] || die "certbot 安装失败"

VERSION="$("$CERTBOT" --version 2>/dev/null | head -n1 || true)"
echo "certbot: $VERSION"
NUMVER="$(printf '%s' "$VERSION" | awk '{print $2}')"
if [ -n "$NUMVER" ]; then
    # 低于 5.4 没有 --ip-address / --preferred-profile
    OLDEST="$(printf '%s\n%s\n' "5.4" "$NUMVER" | sort -V | head -n1)"
    if [ "$OLDEST" != "5.4" ]; then
        echo "警告：certbot $NUMVER 低于 5.4，IP 证书功能不可用"
    fi
fi

say "确认 80 端口可用（校验需要）"
# 引导配置里 /var/www/certbot 是公开可读的，这里只检查目录存在。
[ -d "$WEBROOT" ] || die "$WEBROOT 不存在，请先运行 install.sh"

say "申请证书（IP=$IP）"
if [ -d "/etc/letsencrypt/live/$IP" ]; then
    echo "证书已存在，跳过签发；如需强制续期：$CERTBOT renew --force-renewal"
else
    "$CERTBOT" certonly \
        --webroot -w "$WEBROOT" \
        --ip-address "$IP" \
        --preferred-profile shortlived \
        --non-interactive --agree-tos -m "$EMAIL" \
        --deploy-hook "systemctl reload nginx"
fi

say "切换 nginx 到 HTTPS"
sed "s|__IP__|$IP|g" "$HERE/nginx-tls.conf" > /etc/nginx/sites-available/magi
ln -sfn /etc/nginx/sites-available/magi /etc/nginx/sites-enabled/magi
nginx -t
systemctl reload nginx

say "校验结果"
echo "证书有效期："
openssl x509 -in "/etc/letsencrypt/live/$IP/fullchain.pem" -noout -dates || true
echo
echo "HTTPS 自检："
curl -fsS --max-time 10 "https://$IP/healthz" || echo "（本机访问自身公网 IP 可能被防火墙挡住，属正常）"
echo
echo "续期任务：systemctl list-timers | grep -i certbot"
echo "证书只有 6 天有效期，务必确认定时器在跑；失败两次就会全站 TLS 失效。"
