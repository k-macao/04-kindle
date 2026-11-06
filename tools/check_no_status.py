#!/usr/bin/env python3
"""检查两种字模式产物里还有没有状态类文字（CI 与本地都能跑）。

    python tools/check_no_status.py                       # 默认 kindle.txt + site/report.epub
    python tools/check_no_status.py --text kindle.txt --epub site/report.epub

判定用的就是生成时那套规则（`kindle_report.is_status_text`），所以「自检」与「过滤」
不会各说各话。EPUB 是 XHTML，先按块边界拆成文字行再逐行判：整段说明里出现
「抓取状态」四个字不算残留（`逐源显示当次抓取状态与条目发布日期…` 是内容），
只有**整行**是状态文字才算。

顺带做一道「别把正文也删了」的下限检查：行数/字符数掉到阈值以下就报错，
避免过滤规则写宽了却没人发现。
"""

import argparse
import html
import json
import os
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from kindle_report import is_status_text  # noqa: E402

BLOCK_TAGS = r"(?:h[1-6]|p|li|tr|div|article|section|table|ul|ol|blockquote|figcaption)"
DEFAULT_MIN_LINES = 200
DEFAULT_MIN_CHARS = 20000


def xhtml_lines(xhtml):
    """把 XHTML 按块边界拆成文字行（表格单元用 | 分隔），和纯文本模式同口径。"""
    text = re.sub(rf"<{BLOCK_TAGS}[^>]*>", "\n", xhtml)
    text = re.sub(rf"</{BLOCK_TAGS}>", "\n", text)
    text = re.sub(r"<t[dh][^>]*>", " | ", text)
    text = re.sub(r"<[^>]+>", "", text)
    lines = (re.sub(r"\s+", " ", row).strip() for row in html.unescape(text).splitlines())
    return [line for line in lines if line]


def epub_lines(epub_path):
    with zipfile.ZipFile(epub_path) as zf:
        return xhtml_lines(zf.read("OEBPS/report.xhtml").decode("utf-8"))


def text_lines(text_path):
    lines = Path(text_path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def residues(lines):
    return [line for line in lines if is_status_text(line)]


def check(text_path, epub_path, manifest_path=None,
          min_lines=DEFAULT_MIN_LINES, min_chars=DEFAULT_MIN_CHARS):
    """返回 (errors, summary)；errors 为空即通过。"""
    errors = []
    summary = {}

    try:
        rows = text_lines(text_path)
    except OSError as exc:
        return [f"读不到纯文本产物 {text_path}：{exc}"], summary
    summary["text_lines"] = len(rows)
    leftovers = residues(rows)
    if leftovers:
        errors.append(f"kindle.txt 里还有 {len(leftovers)} 行状态类文字，例如："
                      + " | ".join(line[:60] for line in leftovers[:3]))
    if len(rows) < min_lines:
        errors.append(f"kindle.txt 只有 {len(rows)} 行，低于下限 {min_lines}——"
                      f"过滤规则可能把正文也删了")

    try:
        book = epub_lines(epub_path)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        errors.append(f"读不到 EPUB 正文 {epub_path}：{exc}")
        return errors, summary
    summary["epub_lines"] = len(book)
    summary["epub_chars"] = sum(len(line) for line in book)
    leftovers = residues(book)
    if leftovers:
        errors.append(f"report.epub 里还有 {len(leftovers)} 行状态类文字，例如："
                      + " | ".join(line[:60] for line in leftovers[:3]))
    if summary["epub_chars"] < min_chars:
        errors.append(f"report.epub 正文只有 {summary['epub_chars']} 字符，低于下限 "
                      f"{min_chars}——过滤规则可能把正文也删了")

    manifest = Path(manifest_path) if manifest_path else Path(epub_path).with_name("epub.json")
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"读不懂清单 {manifest}：{exc}")
        else:
            summary.update({key: data.get(key) for key in
                            ("generated", "data_time", "chars", "sections", "status_removed")})
    return errors, summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--text", default="kindle.txt", help="纯文本产物（默认 kindle.txt）")
    ap.add_argument("--epub", default="site/report.epub", help="EPUB 产物（默认 site/report.epub）")
    ap.add_argument("--manifest", default=None, help="清单（默认与 EPUB 同目录的 epub.json）")
    ap.add_argument("--min-lines", type=int, default=DEFAULT_MIN_LINES)
    ap.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS)
    args = ap.parse_args(argv)

    errors, summary = check(args.text, args.epub, args.manifest,
                            args.min_lines, args.min_chars)
    if summary:
        print("纯文本 {text_lines} 行 ｜ EPUB {epub_lines} 行 / {epub_chars} 字符 ｜ "
              "章节 {sections} ｜ 本次删掉状态块 {status_removed} ｜ 数据时间 {data_time}"
              .format(**{**dict.fromkeys(("text_lines", "epub_lines", "epub_chars",
                                          "sections", "status_removed", "data_time"), "—"),
                         **summary}))
    for error in errors:
        # CI 里用 workflow command 前缀，错误才会显示成 run 的注解。
        prefix = "::error title=状态类文字自检::" if os.environ.get("GITHUB_ACTIONS") else ""
        print(prefix + error, file=sys.stderr)
    if errors:
        return 1
    print("状态类文字已清空：kindle.txt 与 report.epub 都没有整行状态文字，正文行数正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
