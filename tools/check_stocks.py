#!/usr/bin/env python3
"""产物自检：清单可解析、字段齐、条数、轮询间隔、EPUB 结构。

单独成一个脚本，是为了让 refresh-stocks.yml 的自循环里不用写嵌套 heredoc
（YAML 块里的 heredoc 缩进极易出错）。

    python tools/check_stocks.py site
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_stocks  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out = argv[0] if argv else "site"
    problems = build_stocks.check(out)
    if problems:
        print("；".join(problems), file=sys.stderr)
        return 1
    print(f"自检通过 · {out}/stocks.json + stocks.html + stocks.epub + stocks.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
