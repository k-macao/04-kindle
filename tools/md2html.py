#!/usr/bin/env python3
"""把 fetch_page 转录的 004 分块 markdown 拼成 build_epub.py 可消费的 HTML。

004 发布页（k-macao.github.io/004/）在沙箱里只能通过 fetch_page 工具读取，
工具按字符数把页面切成多个分块，且分块边界可能落在行中间。本脚本负责：

1. 按块级标记识别分块边界，把落在行中间的断点直接拼接（补回被切掉的字符）；
2. 反转义 fetch_page 的 markdown 转义（\\[ \\] \\* \\_ \\| \\. \\- \\> \\# \\+）；
3. 把转录文本转成 build_epub.py 认识的 HTML 结构：
   - `## X`            -> <h2>X</h2>（章节）
   - `◆ X`             -> <p>◆ X</p>（transform_block 会转成 <h4 class="sub">）
   - `N. name 重点·利空·score 分` -> <p>（transform_block 转成 force-block 卡片）
   - 其它 `N. name`    -> <ol><li>name</li></ol>（05 章的 RSS 源名）
   - `| a | b |` 表格  -> <table>（style_tables 加 class="data"，数值单元格加粗）
   - `- item`          -> <ul><li>item</li></ul>
   - 其余行            -> <p>
   - `**bold**` / `[t](u)` / `_em_` 在行内转换

用法：

    python tools/md2html.py [源目录] [输出文件]
    python tools/md2html.py /home/user/004src report-built.html

输出：report-built.html（<main> 包裹的正文，供 tools/build_epub.py 使用）。
"""

import html
import re
import sys
from pathlib import Path

# 分块拼接：下一分块首行是这些标记时视为新块，否则视为行中续接（直接拼接）。
BLOCK_START = re.compile(
    r"^(#|◆|- |\*\*|>|\||当次实抓|⚠️|STATUS|OPENING|<|\d{1,2}\\\.)"
)

# 与 build_epub.py 的 FORCE_RE 保持一致：力量条目行。
FORCE_RE = re.compile(
    r"^(\d{1,2})[.．、]\s*(.+?)\s*(重点|次要|噪音)\s*[·・]\s*"
    r"(利好|利空|中性)\s*[·・]\s*([\d.]+)\s*分$"
)

# 表格分隔行：| --- | :---: | ---: |
SEP_CELL = re.compile(r"^:?-{2,}:?$")


def join_chunks(texts):
    """把分块拼成完整文本；行中断裂直接拼接，块级起始用空行分隔。"""
    out = texts[0].rstrip()
    for nxt in texts[1:]:
        nxt = nxt.lstrip()
        if BLOCK_START.match(nxt):
            out += "\n\n" + nxt
        else:
            out += nxt
    return out


def unescape(text):
    """反转义 fetch_page 的 markdown 转义。"""
    for esc, ch in ((r"\[", "["), (r"\]", "]"), (r"\*", "*"), (r"\_", "_"),
                    (r"\|", "|"), (r"\.", "."), (r"\-", "-"), (r"\>", ">"),
                    (r"\#", "#"), (r"\+", "+")):
        text = text.replace(esc, ch)
    return text


def inline(text):
    """行内 markdown -> HTML。先保护链接/加粗/斜体为占位符，再转义剩余文本。"""
    stashed = []

    def keep(frag):
        stashed.append(frag)
        return f"\x00{len(stashed) - 1}\x00"

    # 链接 [t](u) —— URL 里的 & 转成 &amp;
    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: keep(
            f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>'),
        text)
    # 加粗 **t**
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: keep(f"<strong>{m.group(1)}</strong>"), text)
    # 斜体 *t*（不与加粗冲突）
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", lambda m: keep(f"<em>{m.group(1)}</em>"), text)
    # 斜体 _t_
    text = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", lambda m: keep(f"<em>{m.group(1)}</em>"), text)
    # 转义剩余文本里的 < > &（占位符不受影响）
    text = html.escape(text, quote=False)
    for index, frag in enumerate(stashed):
        text = text.replace(f"\x00{index}\x00", frag)
    return text


def table_html(rows):
    """markdown 表格 -> <table>。先保护单元格里的 \\| 再按 | 切分。"""
    cells = []
    for row in rows:
        safe = row.strip().strip("|").replace(r"\|", "\x01")
        cells.append([c.strip().replace("\x01", "|") for c in safe.split("|")])
    head, body = cells[0], cells[1:]
    # 去掉分隔行
    if body and all(SEP_CELL.match(c) or not c for c in body[0]):
        body = body[1:]
    th = "".join(f"<th>{inline(c)}</th>" for c in head)
    trs = "".join(
        "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>"
        for row in body)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def convert(text):
    """整篇 markdown -> HTML 块列表。"""
    out = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("## "):
            out.append(f"<h2>{inline(stripped[3:])}</h2>")
            i += 1
        elif stripped.startswith("### "):
            out.append(f"<h3>{inline(stripped[4:])}</h3>")
            i += 1
        elif stripped.startswith("#### "):
            out.append(f"<h4>{inline(stripped[5:])}</h4>")
            i += 1
        elif stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            out.append(table_html(rows))
        elif stripped.startswith("- "):
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:])
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(it)}</li>" for it in items)
                       + "</ul>")
        elif re.match(r"^\d{1,2}\.\s", stripped):
            # 力量条目行 -> <p>（transform_block 转 force-block）；
            # 其余编号行（05 章 RSS 源名）-> <ol><li>
            if FORCE_RE.match(stripped):
                out.append(f"<p>{inline(stripped)}</p>")
            else:
                name = re.sub(r"^\d{1,2}\.\s*", "", stripped)
                out.append(f"<ol><li>{inline(name)}</li></ol>")
            i += 1
        else:
            out.append(f"<p>{inline(stripped)}</p>")
            i += 1
    return "\n".join(out)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    src_dir = Path(argv[0]) if argv else Path("/home/user/004src")
    out_path = Path(argv[1]) if len(argv) > 1 else Path("report-built.html")
    chunks = sorted(src_dir.glob("chunk-*.md"))
    if not chunks:
        print(f"未找到分块：{src_dir}/chunk-*.md", file=sys.stderr)
        return 1
    text = join_chunks([c.read_text(encoding="utf-8") for c in chunks])
    text = unescape(text)
    body = convert(text)
    out_path.write_text(
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
        "<title>004 报告</title></head><body>\n<main>\n" + body + "\n</main>\n"
        "</body></html>\n", encoding="utf-8")
    print(f"完成：{len(chunks)} 个分块，{len(text)} 字符，{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
