#!/usr/bin/env python3
"""行情面板体检：data_time 落后就补跑一轮 refresh-stocks.yml。

GitHub Actions 的 cron 会延迟、负载高时直接丢弃——实测过 ``*/5`` 的调度 6 小时
一次都没被触发，面板就一直停在旧快照。refresh-stocks.yml 现在每 15 分钟抓一轮，
这个脚本每 10 分钟看一眼 ``site/stocks.json`` 的 ``data_time``：

- 盘中（北京时间周一至周五 09:15–15:05）且落后超过 ``--stale-minutes`` →
  ``gh workflow run refresh-stocks.yml`` 补一轮；
- 非盘中（收盘后、开盘前、周末）→ 什么都不做，数据本来就停在收盘那一刻；
- 已经有一轮在排队/运行 → 不重复补跑。

只做「叫醒」：抓取、排版、提交、判重都在 refresh-stocks.yml 里，逻辑只有一份。

用法（本地先看会做什么，不加 --dispatch）::

    python tools/stocks_watchdog.py --manifest site/stocks.json
    python tools/stocks_watchdog.py --dispatch        # CI 里用，落后就补跑

退出码：0 = 正常（新鲜/非盘中/已补跑/已有在跑），1 = 清单读不了。
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BEIJING_OFFSET = timedelta(hours=8)
STALE_MINUTES = 25
SESSION_START_MIN = 9 * 60 + 15   # 北京时间 09:15 集合竞价开始算「盘中」
SESSION_END_MIN = 15 * 60 + 5     # 15:05 之后算收盘
ACTIVE_STATES = ("queued", "in_progress", "waiting", "pending")


def parse_stamp(text):
    """ISO-8601 UTC（"2026-11-18T02:00:01Z"）→ aware datetime；解析不了返回 None。"""
    if not isinstance(text, str) or not text.endswith("Z"):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# 自循环的「可以开始抓了」窗口：比 in_session 早一点、晚一点都行（集合竞价与
# 收盘后的最终快照都值得抓一次）。在这里放宽，是为了避免「cron 恰好在 09:03
# 触发、自循环一看 09:15 未到就退出」这种白跑。
LOOP_START_MIN = 9 * 60 + 0     # 北京 09:00 起算「该干活了」
LOOP_END_MIN = 15 * 60 + 30     # 15:30 之后不再抓（收盘 15:00 + 收尾快照）


def in_loop_window(now):
    """自循环该不该在这个时刻抓一轮（周一到周五 09:00–15:30 北京时间）。"""
    beijing = now.astimezone(timezone.utc) + BEIJING_OFFSET
    minutes = beijing.hour * 60 + beijing.minute
    return beijing.weekday() < 5 and LOOP_START_MIN <= minutes <= LOOP_END_MIN


def seconds_until_window(now, cap=None):
    """距下一个「该干活」的时刻还有多少秒（已在该窗口内返回 0）。

    cap 给了就最多返回 cap（让调用方可以「睡一大觉再看一眼」）。
    """
    if in_loop_window(now):
        return 0
    beijing = now.astimezone(timezone.utc) + BEIJING_OFFSET
    for day in range(0, 8):
        target = (beijing + timedelta(days=day)).replace(
            hour=LOOP_START_MIN // 60, minute=LOOP_START_MIN % 60,
            second=0, microsecond=0)
        if target <= beijing or target.weekday() >= 5:
            continue
        delta = (target - beijing).total_seconds()
        return int(min(delta, cap) if cap else delta)
    return int(cap or 3600)


def in_session(now):
    """北京时间是否在交易时段（周一到周五 09:15–15:05）。"""
    beijing = now.astimezone(timezone.utc) + BEIJING_OFFSET
    minutes = beijing.hour * 60 + beijing.minute
    return beijing.weekday() < 5 and SESSION_START_MIN <= minutes <= SESSION_END_MIN


def minutes_behind(stamp, now):
    if stamp is None:
        return None
    return (now - stamp).total_seconds() / 60


def running_jobs(repo=None):
    """refresh-stocks.yml 有没有排队/在跑的；查不到就返回 []（宁可补跑也别卡住）。"""
    cmd = ["gh", "run", "list", "--workflow=refresh-stocks.yml", "--limit", "5",
           "--json", "status,createdAt"]
    if repo:
        cmd += ["--repo", repo]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError:
        return []
    if result.returncode != 0:
        return []
    try:
        runs = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    return [run for run in runs if run.get("status") in ACTIVE_STATES]


def dispatch(repo=None):
    cmd = ["gh", "workflow", "run", "refresh-stocks.yml", "--ref", "main"]
    if repo:
        cmd += ["--repo", repo]
    subprocess.run(cmd, check=True)


def check(path, now=None, stale_minutes=STALE_MINUTES, repo=None, do_dispatch=False,
          log=print):
    """看一眼清单，必要时补跑；返回 ("fresh"|"outside"|"busy"|"dispatched"|"nostamp")。"""
    now = now or datetime.now(timezone.utc)
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"读不到 {path}：{exc}")

    stamp = parse_stamp(data.get("data_time"))
    if stamp is None:
        log("清单里没有可用的 data_time，跳过")
        return "nostamp"

    age = minutes_behind(stamp, now)
    log(f"data_time {data.get('data_time')} 距今 {age:.0f} 分钟（阈值 {stale_minutes}）")
    if not in_session(now):
        log("非交易时段：不补跑（收盘后数据停在收盘那一刻是正常的）")
        return "outside"
    if age <= stale_minutes:
        log("还算新鲜，不动")
        return "fresh"
    busy = running_jobs(repo)
    if busy:
        log(f"已经有一轮在排队/运行（{busy[0].get('status')}），这次不补")
        return "busy"
    if not do_dispatch:
        log(f"落后 {age:.0f} 分钟，本该补跑一轮（--dispatch 才会真的触发）")
        return "dispatched"
    log(f"落后 {age:.0f} 分钟：补跑一轮 refresh-stocks.yml")
    dispatch(repo)
    return "dispatched"


def main(argv=None):
    """命令行入口。

    除了体检，还给 refresh-stocks.yml 的自循环两个小开关：

    ``--check-session``  在交易时段内 → 退出码 0，否则 1（bash 里 `if ...; then` 就能用）；
    ``--handoff``        仍在交易时段 → ``workflow_dispatch`` 把下一棒交给自己，否则什么都不做。
    """
    parser = argparse.ArgumentParser(description="行情面板体检：落后就补跑一轮")
    parser.add_argument("--manifest", default="site/stocks.json",
                        help="清单路径（默认 site/stocks.json）")
    parser.add_argument("--stale-minutes", type=float, default=STALE_MINUTES,
                        help=f"落后多少分钟算过期（默认 {STALE_MINUTES}）")
    parser.add_argument("--now", default=None,
                        help="假装现在是这个时间（ISO-8601 UTC，测试用）")
    parser.add_argument("--repo", default=None, help="仓库 owner/name（默认当前目录）")
    parser.add_argument("--dispatch", action="store_true",
                        help="真的触发 refresh-stocks.yml（不加只打印会做什么）")
    parser.add_argument("--check-session", action="store_true",
                        help="只在交易时段内返回 0（自循环用）")
    parser.add_argument("--handoff", action="store_true",
                        help="还有活要干（今天还没收盘）就 workflow_dispatch 交接下一棒给自己")
    parser.add_argument("--in-window", action="store_true",
                        help="只在「该抓一轮」的窗口内返回 0（自循环用）")
    parser.add_argument("--sleep-until-window", type=int, default=None,
                        metavar="CAP",
                        help="距下一个该干活的时刻还有多少秒（最多 CAP 秒）")
    args = parser.parse_args(argv)

    if args.in_window:
        now = parse_stamp(args.now) if args.now else None
        now = now or datetime.now(timezone.utc)
        ok = in_loop_window(now)
        print("抓取窗口内" if ok else "窗口外")
        return 0 if ok else 1

    if args.sleep_until_window is not None:
        now = parse_stamp(args.now) if args.now else None
        now = now or datetime.now(timezone.utc)
        print(seconds_until_window(now, cap=args.sleep_until_window))
        return 0

    if args.check_session:
        now = parse_stamp(args.now) if args.now else None
        now = now or datetime.now(timezone.utc)
        print("盘中" if in_session(now) else "非盘中")
        return 0 if in_session(now) else 1

    if args.handoff:
        now = parse_stamp(args.now) if args.now else None
        now = now or datetime.now(timezone.utc)
        if not in_loop_window(now):
            # 收盘了（或周末）：不再交接，让 runner 释放；下一次开盘靠 cron /
            # watchdog / 手动叫醒。
            print("已过今天的抓取窗口：不交接（等下一次开盘的触发）")
            return 0
        print("窗口还没结束：交接下一棒给 refresh-stocks.yml")
        dispatch(args.repo)
        return 0

    now = parse_stamp(args.now) if args.now else None
    result = check(args.manifest, now=now, stale_minutes=args.stale_minutes,
                   repo=args.repo, do_dispatch=args.dispatch)
    return 0 if result != "nostamp" else 1


if __name__ == "__main__":
    sys.exit(main())
