#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Push octopus004.koplugin to a Kindle that is running KOReader's built-in SSH
# server (see docs/kindle-ssh.md). Uses `tar | ssh | tar`, so it needs neither
# rsync nor scp/sftp on the device.
#
#   tools/deploy-plugin.sh 192.168.1.23
#   tools/deploy-plugin.sh -p 2222 -u root -i ~/.ssh/id_ed25519 192.168.1.23
#
# Existing settings and the cached report live outside the plugin directory and
# are left untouched. Restart KOReader afterwards so the plugin is reloaded.

set -eu

PORT=2222
REMOTE_USER=root
IDENTITY=
REMOTE_PLUGINS=/mnt/us/koreader/plugins
PLUGIN=octopus004.koplugin

usage() {
    cat <<'EOF'
用法: tools/deploy-plugin.sh [选项] <Kindle IP 或主机名>

选项:
  -p PORT    SSH 端口（默认 2222，KOReader 内置服务）
  -u USER    远程用户（默认 root）
  -i FILE    私钥文件（透传给 ssh -i）
  -d DIR     设备上的插件目录（默认 /mnt/us/koreader/plugins）
  -h         显示本帮助

例:
  tools/deploy-plugin.sh 192.168.1.23
  tools/deploy-plugin.sh -p 22 -u root 192.168.1.23      # 走 USBNetwork 时
EOF
}

while getopts "hp:u:i:d:" opt; do
    case "$opt" in
        p) PORT=$OPTARG ;;
        u) REMOTE_USER=$OPTARG ;;
        i) IDENTITY=$OPTARG ;;
        d) REMOTE_PLUGINS=$OPTARG ;;
        h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
shift $((OPTIND - 1))

if [ "$#" -ne 1 ]; then
    usage >&2
    exit 2
fi

HOST=$1
TARGET="$REMOTE_USER@$HOST"

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -d "$REPO_ROOT/$PLUGIN" ]; then
    echo "错误: 找不到 $REPO_ROOT/$PLUGIN" >&2
    exit 1
fi

# Keep ssh's own diagnostics visible; fail fast instead of hanging forever.
set -- -p "$PORT" -o ConnectTimeout=10
if [ -n "$IDENTITY" ]; then
    set -- "$@" -i "$IDENTITY"
fi

echo "==> 检查 $HOST 上的 KOReader 目录"
if ! ssh "$@" "$TARGET" 'test -d /mnt/us/koreader'; then
    cat >&2 <<EOF
错误: 无法在 $HOST 上执行命令，或 /mnt/us/koreader 不存在。
请确认:
  - KOReader 正在运行，且 工具(Tools) → SSH server 已勾选
  - 端口正确（KOReader 默认 2222；USBNetwork 才是 22）
  - 电脑与 Kindle 在同一局域网，IP 没有变化（设备搜索框输入 ;711 可查看）
EOF
    exit 1
fi

echo "==> 上传 $PLUGIN 到 $TARGET:$REMOTE_PLUGINS"
(
    cd "$REPO_ROOT"
    tar czf - "$PLUGIN"
) | ssh "$@" "$TARGET" "mkdir -p '$REMOTE_PLUGINS' && tar xzf - -C '$REMOTE_PLUGINS'"

echo "==> 校验设备上的文件"
ssh "$@" "$TARGET" "ls -l '$REMOTE_PLUGINS/$PLUGIN'"

cat <<EOF
完成。重启 KOReader（菜单 → 退出，再从 KUAL 或书架打开）后:
  工具(Tools) → 004 Report → Report URL / Show report

提示: 调试时可用 ssh -p $PORT $TARGET 'tail -n 50 /mnt/us/koreader/crash.log'
EOF
