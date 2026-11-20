#!/usr/bin/env python3
"""在 CI 里跑一遍 unittest，并把失败的用例写成 GitHub 注解。

为什么需要这一步
----------------
「跑测试」红了，日志却未必拿得到：本地 `gh run view --log-failed` 会直接从
`results-receiver.actions.githubusercontent.com` 收到 EOF，页面上也只剩一句
`Process completed with exit code 1`——不知道是哪个用例挂的，只能靠猜。

把失败的用例名 + 断言首行做成 `::error` 注解之后，`gh api
repos/…/check-runs/<id>/annotations` 就能直接读到「哪个文件、哪个用例、为什么」，
不用翻日志。本地跑（没有 GITHUB_ACTIONS）时注解前缀不加，输出与直接跑 unittest 一样。

用法
----
    python tools/ci_unittest.py              # = python -m unittest discover -s tests
    python tools/ci_unittest.py -v           # 多带一个 -v 给 unittest
    python tools/ci_unittest.py --start tests --pattern "test_*.py"
"""

import argparse
import os
import re
import subprocess
import sys

# unittest 的失败抬头两种写法：
#   FAIL: test_x (test_mod.Class.test_x)          ← 普通用例
#   FAIL: test_x (test_mod.Class) (key='值')      ← 带 subTest
# 也有 `FAIL: test_x (unittest.loader._FailedTest.test_x)` 这种导入失败。
HEADER = re.compile(r"^(?P<kind>FAIL|ERROR): (?P<case>\S+)(?: \((?P<where>[^)]*)\))?")
# 断言或异常的最后一行：缩进没有、以「异常类: 」开头。
TAIL = re.compile(r"^(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception|Failure|Warning)?): ?(?P<msg>.*)$")
MESSAGE_LIMIT = 400


def parse_failures(output):
    """从 unittest 的 stderr/stdout 里取出 [(用例名, 位置, 原因), ...]。"""
    failures = []
    lines = output.splitlines()
    for index, line in enumerate(lines):
        match = HEADER.match(line.strip())
        if not match:
            continue
        case = match.group("case")
        where = (match.group("where") or "").strip()
        message = _tail_message(lines[index + 1:])
        failures.append((case, where, message))
    return failures


def _tail_message(rest):
    """取这段 traceback 里最后一句「异常类: 说明」。"""
    message = ""
    for line in rest:
        stripped = line.strip()
        if not stripped:
            continue
        if HEADER.match(stripped) or stripped.startswith("=" * 20):
            break                       # 下一个用例开始了
        if line[:1].isspace():          # 缩进的是源码行/上下文，不是结论
            continue
        found = TAIL.match(stripped)
        if found and found.group("msg"):
            message = stripped
    return message[:MESSAGE_LIMIT]


def _test_path(where, case, start):
    """把用例归属的模块名换成仓库里的文件路径（注解要指到文件）。"""
    module = ""
    parts = where.split(".") if where else []
    if parts and parts[0].startswith("test_"):
        module = parts[0]
    if not module:
        module = case.split(".")[0] if case.startswith("test_") else ""
    if module:
        return f"{start}/{module}.py"
    return f"{start}/"


def annotation_lines(failures, start="tests"):
    """生成 `::error` 注解：一条失败一个，别把整段 traceback 塞进去。"""
    lines = []
    for case, where, message in failures:
        path = _test_path(where, case, start)
        name = f"{where}::{case}" if where and not where.endswith(case) else (where or case)
        text = f"{name} — {message}" if message else name
        lines.append(f"::error file={path},title=测试失败::{text}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="tests", help="discover 的起始目录")
    parser.add_argument("--pattern", default="test_*.py", help="discover 的文件匹配")
    parser.add_argument("--verbose", "-v", action="store_true", help="传给 unittest -v")
    parser.add_argument("--top", default="", help="discover 的顶层目录（默认当前目录）")
    args = parser.parse_args(argv)

    command = [sys.executable, "-m", "unittest", "discover", "-s", args.start]
    if args.top:
        command += ["-t", args.top]
    command += ["-p", args.pattern]
    if args.verbose:
        command.append("-v")

    # 输出原样透传（人在页面上看日志时还是熟悉的 unittest 样子），同时留一份解析。
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    sys.stdout.write(proc.stdout)
    sys.stdout.flush()

    failures = parse_failures(proc.stdout)
    if proc.returncode and not failures:
        # 一个用例都没解析出来（比如进程直接崩了）：至少把退出码与尾行报上去。
        tail = " | ".join(line.strip() for line in proc.stdout.splitlines()[-3:] if line.strip())
        print(f"::error title=测试失败::unittest 退出码 {proc.returncode}，"
              f"没有解析到失败用例；日志尾：{tail[:MESSAGE_LIMIT]}", file=sys.stderr)
        return proc.returncode

    if failures:
        print(f"共 {len(failures)} 个失败用例：", file=sys.stderr)
        for case, where, message in failures:
            label = f"{where}::{case}" if where else case
            print(f"  ✗ {label}" + (f" — {message}" if message else ""), file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS"):
            for line in annotation_lines(failures, args.start):
                # 注解走 stdout：GitHub 只从流程输出里认 `::error` 工作流命令。
                print(line)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
