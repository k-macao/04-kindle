#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Inspect a Kindle that is plugged in as a USB drive, and optionally install
# octopus004.koplugin straight into koreader/plugins (no SSH needed).
#
#   tools/kindle-usb.sh                 # auto-detect the mounted Kindle
#   tools/kindle-usb.sh /Volumes/Kindle # explicit mount point
#   tools/kindle-usb.sh --install       # copy the plugin over
#
# Exit KOReader on the device before plugging it in, otherwise the Kindle stays
# in "charging" mode and no drive shows up.

set -u

INSTALL=0
MOUNT=""

usage() {
    cat <<'EOF'
用法: tools/kindle-usb.sh [--install] [挂载点]

  --install   把仓库里的 octopus004.koplugin 复制到 <挂载点>/koreader/plugins/
  -h          显示本帮助

不给挂载点时自动在 /Volumes、/media、/run/media、/mnt 下找带 koreader 目录的盘。
实测路径例: macOS /Volumes/Kindle；Linux /media/$USER/Kindle；Windows 用 Git Bash
时是 /d 或 /e 之类（也可以直接传 "D:/" 这样的路径）。
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --install) INSTALL=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "未知选项: $1" >&2; usage >&2; exit 2 ;;
        *) MOUNT=$1 ;;
    esac
    shift
done

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PLUGIN_SRC="$REPO_ROOT/octopus004.koplugin"

if [ -z "$MOUNT" ]; then
    for base in /Volumes /media /run/media /mnt; do
        [ -d "$base" ] || continue
        for cand in "$base"/* "$base"/*/*; do
            if [ -d "$cand/koreader" ]; then
                MOUNT=$cand
                break 2
            fi
        done
    done
fi

if [ -z "$MOUNT" ]; then
    echo "找不到已挂载的 Kindle（没看到任何带 koreader 目录的盘）。" >&2
    echo "请在 Kindle 上先退出 KOReader，再插 USB；或用参数指定挂载点。" >&2
    exit 1
fi

# Windows paths like D:/ arrive here; strip the trailing slash.
MOUNT=${MOUNT%/}
K="$MOUNT/koreader"

if [ ! -d "$K" ]; then
    echo "错误: $K 不存在，这个盘不像 Kindle 的根目录（应该能看到 koreader/ 文件夹）。" >&2
    exit 1
fi

ok()   { printf '  [OK]   %s\n' "$1"; }
bad()  { printf '  [缺失] %s\n' "$1"; }
warn() { printf '  [注意] %s\n' "$1"; }
info() { printf '  [信息] %s\n' "$1"; }

echo "Kindle 挂载点: $MOUNT"
echo

echo "== KOReader =="
if [ -f "$K/git-rev" ]; then
    info "版本标记 (koreader/git-rev): $(head -n 1 "$K/git-rev" 2>/dev/null)"
else
    warn "没有 koreader/git-rev，老包或非官方打包；以 KOReader 内 帮助→关于 显示的版本为准"
fi

echo
echo "== SSH 依赖（菜单里没有 SSH 服务器时看这里）=="
for bin in dropbear dropbearkey; do
    if [ -f "$K/$bin" ]; then
        ok "koreader/$bin 存在"
    else
        bad "koreader/$bin 不存在 —— SSH.koplugin 会静默禁用自己，菜单里就不会出现 SSH 服务器"
    fi
done
if find "$K" -maxdepth 2 -name 'sftp-server*' -print -quit 2>/dev/null | grep -q .; then
    ok "找到 sftp-server（SFTP 可用）"
else
    warn "没找到 sftp-server：SFTP/新版 scp 可能不通，用 scp -O 或 tar|ssh"
fi

echo
echo "== 插件目录 =="
if [ -d "$K/plugins" ]; then
    ok "koreader/plugins 存在"
    for p in "$K"/plugins/*.koplugin; do
        [ -e "$p" ] || continue
        info "已安装: $(basename "$p")"
    done
else
    bad "koreader/plugins 不存在（KOReader 装得不对？）"
fi

echo
echo "== 本仓库插件与设置 =="
if [ ! -d "$PLUGIN_SRC" ]; then
    warn "脚本旁边没有 octopus004.koplugin（只做检查没关系；--install 需要它）"
fi
if [ -d "$K/plugins/octopus004.koplugin" ]; then
    ok "octopus004.koplugin 已安装"
    for f in main.lua wake.lua _meta.lua; do
        [ -f "$K/plugins/octopus004.koplugin/$f" ] || bad "  缺少 $f"
    done
else
    warn "octopus004.koplugin 还没装"
fi
if [ -f "$K/settings/octopus004.lua" ]; then
    ok "设置文件 settings/octopus004.lua 存在（重装插件不会丢设置）"
else
    info "还没有 settings/octopus004.lua（首次运行后生成）"
fi
if [ -f "$K/octopus004-report.txt" ]; then
    info "已有缓存报表 $(wc -c < "$K/octopus004-report.txt" | tr -d ' ') 字节"
fi
if [ -f "$K/settings/SSH/authorized_keys" ]; then
    info "SSH 公钥 $(grep -c . "$K/settings/SSH/authorized_keys" 2>/dev/null || echo 0) 行"
fi

if [ -f "$K/crash.log" ]; then
    echo
    echo "== crash.log 最后 15 行（可能含书名/路径，贴出来前先看一眼）=="
    tail -n 15 "$K/crash.log"
fi

if [ "$INSTALL" -eq 1 ]; then
    echo
    echo "== 安装插件 =="
    if [ ! -d "$PLUGIN_SRC" ]; then
        echo "错误: 找不到 $PLUGIN_SRC —— 把 octopus004.koplugin 文件夹和 tools/ 放在同一层目录里。" >&2
        exit 1
    fi
    if [ ! -d "$K/plugins" ]; then
        echo "错误: $K/plugins 不存在，先修好 KOReader 安装再来。" >&2
        exit 1
    fi
    target="$K/plugins/octopus004.koplugin"
    case "$target" in
        */koreader/plugins/octopus004.koplugin) ;;
        *) echo "错误: 目标路径异常 ($target)，中止。" >&2; exit 1 ;;
    esac
    if [ -d "$target" ]; then
        echo "  覆盖已有的 $target"
        rm -rf "$target"
    fi
    cp -R "$PLUGIN_SRC" "$K/plugins/" || {
        echo "错误: 复制失败（盘是只读的？换个 USB 线/口再试）。" >&2
        exit 1
    }
    ok "已复制到 koreader/plugins/octopus004.koplugin"
    ls -l "$target"
    cat <<'EOF'

完成。安全弹出后拔线，在 Kindle 上重新打开 KOReader：
  工具(Tools) → 004 Report → Report URL / Show report
EOF
else
    cat <<'EOF'

要直接装插件，加上 --install 再跑一次：
  tools/kindle-usb.sh --install
EOF
fi
