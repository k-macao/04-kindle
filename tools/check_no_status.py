#!/usr/bin/env python3
"""检查两种字模式产物里还有没有状态类文字、站点外壳与乱码（CI 与本地都能跑）。

    python tools/check_no_status.py                       # 默认 kindle.txt + site/report.epub
    python tools/check_no_status.py --text kindle.txt --epub site/report.epub

判定用的就是生成时那套规则（`kindle_report.is_status_text / is_chrome_text /
is_garbled`），所以「自检」与「过滤」不会各说各话。EPUB 是 XHTML，先按块边界拆成
文字行再逐行判：整段说明里出现「抓取状态」四个字不算残留（`逐源显示当次抓取状态
与条目发布日期…` 是内容），只有**整行**是状态文字才算；站壳（导航条、页脚、版权
与备案）和乱码同理，只认整行。

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

from kindle_report import is_chrome_text, is_garbled, is_status_text  # noqa: E402

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
    """整行还是状态类文字的（抓取中、STATUS >、核验日期……）。"""
    return [line for line in lines if is_status_text(line)]


def junk_residues(lines):
    """整行还是站点外壳或乱码的（导航条、页脚版权备案、编码坏掉的文字）。"""
    return [line for line in lines
            if is_chrome_text(line) or is_garbled(line)]


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
    junk = junk_residues(rows)
    if junk:
        errors.append(f"kindle.txt 里还有 {len(junk)} 行非正文内容（站点外壳/乱码），例如："
                      + " | ".join(line[:60] for line in junk[:3]))
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
    junk = junk_residues(book)
    if junk:
        errors.append(f"report.epub 里还有 {len(junk)} 行非正文内容（站点外壳/乱码），例如："
                      + " | ".join(line[:60] for line in junk[:3]))
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
                            ("generated", "data_time", "data_time_kind", "chars",
                             "sections", "status_removed", "body_removed")})
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
        shown = {**dict.fromkeys(("text_lines", "epub_lines", "epub_chars", "sections",
                                  "status_removed", "body_removed", "data_time",
                                  "data_time_kind"), "—"),
                 **summary}
        # 精度要跟着时间一起打出来：只有日期时 data_time 里的 00:00:00 是补出来的，
        # 光看时间分不清「真的是午夜」还是「正文只给到交易日」。
        if shown["data_time_kind"] not in ("—", None, ""):
            shown["data_time"] = f"{shown['data_time']}（{shown['data_time_kind']}）"
        print("纯文本 {text_lines} 行 ｜ EPUB {epub_lines} 行 / {epub_chars} 字符 ｜ "
              "章节 {sections} ｜ 本次删掉状态块 {status_removed} ｜ "
              "删掉非正文 {body_removed} ｜ 数据时间 {data_time}"
              .format(**shown))
    for error in errors:
        # CI 里用 workflow command 前缀，错误才会显示成 run 的注解。
        prefix = "::error title=正文自检::" if os.environ.get("GITHUB_ACTIONS") else ""
        print(prefix + error, file=sys.stderr)
    if errors:
        return 1
    print("正文自检通过：kindle.txt 与 report.epub 都没有整行状态文字、站点外壳或乱码，"
          "正文行数正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
