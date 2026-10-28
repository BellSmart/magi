#!/usr/bin/env bash
# 增量更新：同步代码到 /opt/magi，按需装依赖，重启并自检。
#
#   sudo bash deploy/update.sh                     # 用脚本所在仓库
#   sudo bash deploy/update.sh /root/magi-src      # 用服务器上的目录
#   sudo bash deploy/update.sh /root/magi.tar.gz   # 用上传的压缩包
#
# 不动 nginx、不动 magi.env、不动数据库内容；数据库改动靠应用启动时迁移。
# 先在 /var/backups/magi 留一份快照再动手。

set -euo pipefail

APP_DIR=/opt/magi
SERVICE=magi
DB=/var/lib/magi/magi.db
BACKUP_DIR=/var/backups/magi
HEALTH=http://127.0.0.1:8000/healthz

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
die() { echo "错误：$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash deploy/update.sh"
[ -d "$APP_DIR" ] || die "$APP_DIR 不存在，请先跑 deploy/install.sh"

SRC="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TMP=""
cleanup() { [ -n "$TMP" ] && rm -rf "$TMP"; }
trap cleanup EXIT

if [ -f "$SRC" ]; then
    say "解包 $SRC"
    TMP="$(mktemp -d)"
    tar -xzf "$SRC" -C "$TMP"
    if [ -f "$TMP/pyproject.toml" ]; then
        SRC="$TMP"
    elif [ -f "$TMP/magi/pyproject.toml" ]; then
        SRC="$TMP/magi"
    else
        die "压缩包里找不到 pyproject.toml"
    fi
fi

[ -d "$SRC" ] || die "找不到 $SRC"
[ -f "$SRC/pyproject.toml" ] || die "$SRC 不是 MAGI 源码目录"

# 1. 备份数据库
if [ -f "$DB" ]; then
    mkdir -p "$BACKUP_DIR"
    STAMP="$(date +%Y%m%d-%H%M%S)"
    cp -a "$DB" "$BACKUP_DIR/magi-$STAMP.db"
    echo "已备份数据库：$BACKUP_DIR/magi-$STAMP.db"
    ls -1t "$BACKUP_DIR"/magi-*.db 2>/dev/null | tail -n +11 | xargs -r rm -f
    echo "（只保留最近 10 份）"
fi

# 2. 同步代码
say "同步代码到 $APP_DIR"
BEFORE_PYPROJECT="$(sha256sum "$APP_DIR/pyproject.toml" 2>/dev/null | awk '{print $1}')"
rsync -a --delete \
    --exclude='.venv' --exclude='data/' --exclude='.git/' \
    --exclude='__pycache__/' --exclude='*.pyc' --exclude='.env' \
    "$SRC"/ "$APP_DIR"/
chown -R magi:magi "$APP_DIR"

# 3. 依赖有变化才重装（大多数更新只是改代码，跳过 pip 能省几十秒）
AFTER_PYPROJECT="$(sha256sum "$APP_DIR/pyproject.toml" | awk '{print $1}')"
if [ "$BEFORE_PYPROJECT" != "$AFTER_PYPROJECT" ]; then
    say "依赖清单有变化，重装"
    PIP_OPTS="--quiet --timeout 60 --retries 5"
    if [ -n "${PIP_INDEX_URL:-}" ]; then
        PIP_OPTS="$PIP_OPTS -i $PIP_INDEX_URL"
    fi
    # shellcheck disable=SC2086
    "$APP_DIR/.venv/bin/pip" install $PIP_OPTS -e "$APP_DIR"
else
    echo "依赖清单未变，跳过 pip"
fi

# 4. 重启并自检
say "重启 $SERVICE"
systemctl restart "$SERVICE"

for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1; then
        echo "自检通过：$(curl -s --max-time 2 "$HEALTH")"
        VER="$(cd "$APP_DIR" && git log --oneline -1 2>/dev/null || echo '（非 git 部署）')"
        echo "更新完成，当前版本：$VER"
        exit 0
    fi
    sleep 1
done

echo "" >&2
echo "自检失败，最近 30 行日志：" >&2
journalctl -u "$SERVICE" -n 30 --no-pager >&2
echo "" >&2
echo "回滚：cp $BACKUP_DIR/magi-<时间戳>.db $DB && systemctl restart $SERVICE" >&2
exit 1
