#!/usr/bin/env python3
"""行情面板体检：data_time 落后就补跑一轮 refresh-stocks.yml。

GitHub Actions 的 cron 会延迟、负载高时直接丢弃——实测过 ``*/5`` 的调度 6 小时
一次都没被触发，面板就一直停在旧快照。refresh-stocks.yml 现在每 15 分钟抓一轮，
这个脚本每 10 分钟看一眼 ``site/stocks.json`` 的 ``data_time``：

- 盘中（北京时间周一至周五 09:15–15:05）且落后超过 ``--stale-minutes`` →
  ``gh workflow run refresh-stocks.yml`` 补一轮；
- 非盘中通常不补跑；但若上一个应有的交易日收盘快照缺失，允许工作流补抓一次；
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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def expected_session_date(now):
    """此刻至少应有哪一个工作日的收盘/最新快照（北京时间日期）。

    开盘前应有前一工作日，交易窗口开始后应有当天；周末退到周五。这里不猜法定
    节假日——若当天休市，补抓会拿到上一交易日且 data_hash 不变，不会产生假数据。
    """
    beijing = now.astimezone(timezone.utc) + BEIJING_OFFSET
    day = beijing.date()
    minutes = beijing.hour * 60 + beijing.minute
    if day.weekday() >= 5 or minutes < LOOP_START_MIN:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def catch_up_needed(path, now=None, log=print):
    """窗口外是否缺少最近应有交易日的数据；缺失/坏清单也应尝试补抓一次。"""
    now = now or datetime.now(timezone.utc)
    expected = expected_session_date(now)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log(f"面板清单读不到（{exc}）：需要补抓")
        return True
    stamp = parse_stamp(data.get("data_time"))
    if stamp is None:
        log("面板清单没有可用 data_time：需要补抓")
        return True
    actual = (stamp.astimezone(timezone.utc) + BEIJING_OFFSET).date()
    if actual < expected:
        log(f"面板数据日期 {actual}，应至少到 {expected}：需要补抓收盘快照")
        return True
    if actual > expected:
        # 同花顺信封 timestamp 是请求时刻：旧版在次日凌晨补抓时会把前一日收盘
        # 错写成「今天」的精确行情。未来日期不是覆盖得更好，而是必须纠正的坏时间。
        log(f"面板数据日期 {actual} 晚于此刻应有交易日 {expected}：需要重抓纠正时间")
        return True
    log(f"面板数据日期 {actual} 已覆盖应有交易日 {expected}，窗口外无需占用 runner")
    return False


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


DISPATCH_ATTEMPTS = 3


def dispatch(repo=None, sleep=time.sleep, log=print):
    """喊醒 refresh-stocks.yml。

    GitHub 的 dispatch 接口偶发 HTTP 500（实测过：交接那一棒连吃几次 500，
    结果 job 变红，而外层 cron 又不停把我们叫醒，滚动成一片失败）。所以这里
    带退避重试；**失败只记警告、不抛异常**——交接失败不该把整轮抓取标成失败，
    下一次 cron / watchdog 还会再叫醒。
    """
    cmd = ["gh", "workflow", "run", "refresh-stocks.yml", "--ref", "main"]
    if repo:
        cmd += ["--repo", repo]
    last = None
    for attempt in range(1, DISPATCH_ATTEMPTS + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    check=False)
        except OSError as exc:
            last = str(exc)
        else:
            if result.returncode == 0:
                if attempt > 1:
                    log(f"交接第 {attempt} 次成功")
                return True
            last = (result.stderr or result.stdout or "").strip().splitlines()
            last = last[-1] if last else f"exit {result.returncode}"
        if attempt < DISPATCH_ATTEMPTS:
            wait = 2 ** attempt
            log(f"交接失败（{last}），{wait} 秒后重试")
            sleep(wait)
    log(f"::warning::交接没成功（{last}）：不影响本轮数据，下一班 cron/体检会再叫醒")
    return False


def check(path, now=None, stale_minutes=STALE_MINUTES, repo=None, do_dispatch=False,
          log=print):
    """看一眼清单，必要时补跑；返回 ("fresh"|"outside"|"busy"|"dispatched"|"nostamp")。"""
    now = now or datetime.now(timezone.utc)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
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

    ``--check-session``   在交易时段内 → 退出码 0，否则 1；
    ``--needs-catch-up``  缺最近工作日快照 → 退出码 0，供迟到的工作流补抓一次；
    ``--handoff``         仍在交易时段 → ``workflow_dispatch`` 把下一棒交给自己，否则什么都不做。
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
    parser.add_argument("--needs-catch-up", action="store_true",
                        help="窗口外缺少最近工作日快照时返回 0，否则返回 1（工作流用）")
    args = parser.parse_args(argv)

    if args.needs_catch_up:
        now = parse_stamp(args.now) if args.now else None
        now = now or datetime.now(timezone.utc)
        return 0 if catch_up_needed(args.manifest, now=now) else 1

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
        # 交接成功与否都不影响退出码：这一棒的数据已经抓完并提交了，
        # 把 job 标红只会误导（真正要紧的是下一次能再被叫醒）。
        dispatch(args.repo)
        return 0

    now = parse_stamp(args.now) if args.now else None
    result = check(args.manifest, now=now, stale_minutes=args.stale_minutes,
                   repo=args.repo, do_dispatch=args.dispatch)
    return 0 if result != "nostamp" else 1


if __name__ == "__main__":
    sys.exit(main())
