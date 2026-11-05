#!/usr/bin/env python3
"""把 004 发布页排成 EPUB，让 KOReader 用「字模式」显示真正的排版。

截图模式（render_report.py）保真但费流量、放大后发虚；这个脚本换一条路：
抓 004 的 <main> 正文，清洗成 XHTML，再按 004 自己的写作套路重新组织结构
（力量条目 → 标题 + 信号 + 分数；「归类 / 当次读数」→ 两列表格；AI 解读 →
左边线引用块；◆ 小标题 → 四级标题；章节加锚点与目录），数字统一加粗突出，
表格保留并强化，最后打包成单文件 EPUB。插件把 EPUB 下载到设备后用
KOReader 阅读器打开，字号、行距、页边距全由 KOReader 自己管。

用法：

    python tools/build_epub.py https://k-macao.github.io/004/ --out site
    python tools/build_epub.py report.html --out site      # 本地文件也行

输出：

    site/report.epub    单文件 EPUB 3（含 toc.ncx，兼容旧版 crengine）
    site/epub.json      变更清单：{"generated", "doc", "bytes", "chars", ...}

把这两个文件上传到仓库，插件里 Display mode 选 EPUB、EPUB manifest URL 指
向 epub.json 即可。只依赖标准库，不需要 Playwright / Pillow。
"""

import argparse
import html
import json
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

MAX_BYTES = 20 * 1024 * 1024

# Tags that survive into the EPUB. Anything else is either dropped (with its
# content) or unwrapped (children kept), so unknown markup can never break
# the XHTML.
KEEP = {
    "p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot",
    "tr", "td", "th", "caption", "colgroup", "col", "strong", "b", "em", "i",
    "span", "a", "br", "hr", "sup", "sub", "code", "pre", "blockquote",
    "img", "figure", "figcaption",
}
VOID = {"br", "hr", "img", "col"}
# Dropped together with everything inside them.
DROP = {
    "script", "style", "noscript", "template", "iframe", "button", "input",
    "select", "textarea", "svg", "canvas", "video", "audio", "object",
    "embed", "nav", "form",
}
# Elements that implicitly close an open <p> (HTML's "closes a p element"
# list); without this an unclosed <p> would swallow the rest of the report.
P_CLOSERS = {
    "address", "article", "aside", "blockquote", "div", "dl", "fieldset",
    "figcaption", "figure", "footer", "header", "hgroup", "hr", "main",
    "menu", "nav", "ol", "p", "pre", "section", "table", "ul",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
# Elements that implicitly close their siblings (HTML tree construction).
SIBLING_CLOSERS = {
    "li": ({"li"}, False),
    "dt": ({"dt", "dd"}, False),
    "dd": ({"dt", "dd"}, False),
    "td": ({"td", "th"}, False),
    "th": ({"td", "th"}, False),
    "tr": ({"tr", "td", "th"}, True),
    "thead": ({"tr", "td", "th", "thead", "tbody", "tfoot"}, True),
    "tbody": ({"tr", "td", "th", "thead", "tbody", "tfoot"}, True),
    "tfoot": ({"tr", "td", "th", "thead", "tbody", "tfoot"}, True),
}
INLINE = {"b", "strong", "i", "em", "span", "a", "sup", "sub", "code", "small",
          "u", "mark", "time"}
# Attributes worth keeping; everything else (classes, ids, inline styles,
# event handlers) is stripped so our CSS is the only styling.
ATTRS = {
    "a": {"href"},
    "img": {"src", "alt"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}

# ---------------------------------------------------------------------------
# Node tree
# ---------------------------------------------------------------------------


class Node:
    """Minimal element node; text children are plain strings."""

    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs=None, children=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = list(children or [])

    def text(self):
        parts = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return "".join(parts)

    def add(self, *children):
        for child in children:
            if isinstance(child, str) and self.children and isinstance(self.children[-1], str):
                self.children[-1] += child
            elif child is not None:
                self.children.append(child)
        return self


def el(tag, children=None, **attrs):
    return Node(tag, attrs, children if children is not None else [])


def normalize(text):
    return re.sub(r"[ \t\r\n\f\v]+", " ", text.replace("\xa0", " ")).strip()


# ---------------------------------------------------------------------------
# HTML -> tree
# ---------------------------------------------------------------------------


class MainTree(HTMLParser):
    """Parse the <main> subtree into Node objects, dropping unsafe markup."""

    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.root = el("main")
        self.stack = [self.root]
        self.inside = False
        self.found = False
        self.depth = 0
        self.drop = 0

    # -- helpers ---------------------------------------------------------
    def _absolute(self, value):
        if not value:
            return value
        if self.base_url and not re.match(r"^([a-z]+:|//|#)", value, re.I):
            return urljoin(self.base_url, value)
        return value

    def _attrs(self, tag, attrs):
        allowed = ATTRS.get(tag)
        out = {}
        for name, value in attrs:
            if not allowed or name not in allowed:
                continue
            if name in ("href", "src"):
                value = self._absolute(value)
            out[name] = value or ""
        return out

    def _pop_to(self, names, multi=False):
        while len(self.stack) > 1:
            top = self.stack[-1].tag
            if top in names or top in INLINE:
                self.stack.pop()
                if top in names and not multi:
                    break
            else:
                break

    # -- HTMLParser hooks ------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if not self.inside:
            if tag == "main":
                self.inside = self.found = True
                self.depth = 1
            return
        if tag not in VOID:
            self.depth += 1
        if self.drop:
            if tag not in VOID:
                self.drop += 1
            return
        if tag in DROP:
            if tag not in VOID:
                self.drop = 1
            return
        if tag in P_CLOSERS:
            self._pop_to({"p"})
        if tag in SIBLING_CLOSERS:
            names, multi = SIBLING_CLOSERS[tag]
            self._pop_to(names, multi)
        if tag in KEEP:
            node = el(tag, None, **self._attrs(tag, attrs))
            self.stack[-1].children.append(node)
            if tag not in VOID:
                self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.inside or tag in VOID:
            return
        if self.drop:
            self.drop -= 1
        elif tag in KEEP:
            for index in range(len(self.stack) - 1, 0, -1):
                if self.stack[index].tag == tag:
                    del self.stack[index:]
                    break
        self.depth -= 1
        if self.depth == 0:
            self.inside = False

    def handle_data(self, data):
        if self.inside and not self.drop:
            self.stack[-1].add(data)

    def tree(self):
        return self.root


# ---------------------------------------------------------------------------
# Numbers: emphasise every figure, mark up/down
# ---------------------------------------------------------------------------

NUM_RE = re.compile(r"[-+−]?\d[\d,]*(?:\.\d+)?%?")
SIGN_PREFIX = ("", " ", "\u3000", "（", "(", "：", ":", "·", "、", "/", "｜", "|")
DOWN = "−-"
UP = "+"


def emphasize(text):
    """Wrap every number in <span class="num">, prefixing ▲/▼ on signed ones."""
    out = []
    pos = 0
    for match in NUM_RE.finditer(text):
        token = match.group(0).replace("-", "−")
        prev = text[match.start() - 1] if match.start() else ""
        if prev and (prev.isdigit() or prev in "-−./·"):
            continue
        rest = text[match.end():]
        # Dates and clock times are noise when bolded: 2026-11-02, 10:09:05,
        # "10 月 30 日" all stay in plain text.
        if prev in (":", "："):
            continue
        if rest[:1] in ("-", "−", "/") and rest[1:2].isdigit():
            continue
        if re.match(r"\s*(月|日|年|时|秒)", rest):
            continue
        if rest[:1] in (":", "：") and re.match(r"\d{2}", rest[1:3]):
            continue
        out.append(html.escape(text[pos:match.start()], quote=False))
        mark = ""
        if token[0] in DOWN and (prev == "" or prev in SIGN_PREFIX):
            mark = "▼"
        elif token[0] in UP and (prev == "" or prev in SIGN_PREFIX):
            mark = "▲"
        out.append(f'<span class="num">{mark}{html.escape(token, quote=False)}</span>')
        pos = match.end()
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


# ---------------------------------------------------------------------------
# Structure recognition
# ---------------------------------------------------------------------------

FORCE_RE = re.compile(
    r"^(\d{1,2})[.．、]\s*(.+?)\s*(重点|次要|噪音)\s*[·・]\s*"
    r"(利好|利空|中性)\s*[·・]\s*([\d.]+)\s*分$"
)
SUBHEAD_RE = re.compile(r"^◆\s*(.+)$")
AI_RE = re.compile(r"^▶\s*([^：:]{1,16})[：:]\s*(.+)$")
VERDICT_RE = re.compile(r"^结论[：:]\s*(.+)$")
KV_RE = re.compile(r"^([^：:\s]{1,12})[：:]\s*(.+)$")
QUOTE_RE = re.compile(r"^[\[［]\s*(\d{1,2}\s*月\s*\d{1,2}\s*日)\s*[\]］]\s*(.+)$")

# Labels that are really "field: value" pairs, not prose.
KV_LABELS = {
    "归类", "当次读数", "力量分", "合成分", "置信度", "调整项", "数据缺口",
    "行情日期", "扫描时间", "数据覆盖", "最新", "涨跌", "涨跌幅", "情感",
    "实体级情感得分", "事件", "相关性", "新颖度", "方向", "置信度同步下调",
    "口径",
}
KV_VALUE_MAX = 110
# "口径" is a definition, not a figure: let it wrap inside the cell.
KV_VALUE_MAX_BY_LABEL = {"口径": 400}
KV_SEPARATORS = re.compile(r"[\u3000]+|\s*[·・]\s*|\s{2,}")
KV_SPACE_PAIR = re.compile(r"^([^\s：:]{2,8})\s+(.+)$")


def kv_pair(part):
    """Return (label, value) when `part` is a known field/value pair."""
    part = part.strip()
    for pattern in (KV_RE, KV_SPACE_PAIR):
        match = pattern.match(part)
        if not match:
            continue
        label, value = match.group(1).strip(), match.group(2).strip()
        if label not in KV_LABELS:
            continue
        if len(value) > KV_VALUE_MAX_BY_LABEL.get(label, KV_VALUE_MAX):
            continue
        return label, value
    return None


def is_kv_line(text):
    parts = [p for p in KV_SEPARATORS.split(text) if p.strip()]
    if len(parts) < 1:
        return False
    return all(kv_pair(part) for part in parts)


def kv_table(text):
    rows = []
    for part in KV_SEPARATORS.split(text):
        pair = kv_pair(part)
        if pair:
            rows.append(el("tr", [el("th", [pair[0]]), el("td", [pair[1]])]))
    return el("table", rows, **{"class": "kv"})


def force_block(match):
    index, name, weight, direction, score = match.groups()
    signal = el("p", [
        el("span", [weight], **{"class": "tag"}),
        el("span", [direction], **{"class": "tag"}),
        el("span", [score], **{"class": "score"}),
        " 分",
    ], **{"class": "signal"})
    return el("div", [
        el("h3", [f"{index}　{name.strip()}"], **{"class": "force"}),
        signal,
    ], **{"class": "force-block"})


def subhead_block(match):
    return el("h4", [match.group(1).strip()], **{"class": "sub"})


VERDICT_PAIR = re.compile(r"^([^\s\d·]{1,8})\s*[：:]?\s*(.+)$")


def verdict_block(match):
    body = match.group(1).strip()
    chunks = [c.strip() for c in re.split(r"[\u3000]|\s{2,}", body) if c.strip()]
    head = chunks[0] if chunks else body
    rows = [el("p", [el("strong", [head])], **{"class": "verdict-line"})]
    for chunk in chunks[1:]:
        for part in [p.strip() for p in chunk.split("·") if p.strip()]:
            pair = VERDICT_PAIR.match(part)
            if pair:
                rows.append(el("p", [
                    el("span", [pair.group(1).strip() + "："], **{"class": "label"}),
                    pair.group(2).strip(),
                ], **{"class": "verdict-line"}))
            else:
                rows.append(el("p", [part], **{"class": "verdict-line"}))
    return el("div", rows, **{"class": "verdict"})


def ai_block(match):
    label, body = match.group(1).strip(), match.group(2).strip()
    children = [el("p", ["▶ " + label], **{"class": "ai-label"})]
    # "…；提取信息：…；规则判断：…" -> one paragraph per logical part.
    for chunk in re.split(r"；\s*(?=提取信息[：:]|规则判断[：:]|结论[：:])", body):
        chunk = chunk.strip()
        if not chunk:
            continue
        sub = re.match(r"^(提取信息|规则判断|结论)[：:]\s*(.+)$", chunk)
        if sub:
            children.append(el("p", [
                el("span", [sub.group(1) + "："], **{"class": "label"}),
                sub.group(2),
            ], **{"class": "ai-line"}))
        else:
            children.append(el("p", [chunk], **{"class": "ai-line"}))
    return el("div", children, **{"class": "ai"})


def quote_line(match):
    date, rest = match.group(1), match.group(2).strip()
    return Node("__quote__", {"date": date}, [rest])


def split_lines(children):
    """Split a block's children on <br> so each visual line can be matched."""
    lines, current = [], []
    for child in children:
        if isinstance(child, Node) and child.tag == "br":
            lines.append(current)
            current = []
        else:
            current.append(child)
    lines.append(current)
    return lines


def transform_block(node):
    """Return a list of replacement nodes for one <p>/<li>."""
    groups = split_lines(node.children)
    out = []
    for group in groups:
        text = normalize("".join(c if isinstance(c, str) else c.text() for c in group))
        if not text:
            out.append(Node(node.tag, node.attrs, group))
            continue
        match = FORCE_RE.match(text)
        if match:
            out.append(force_block(match))
            continue
        if SUBHEAD_RE.match(text):
            out.append(subhead_block(SUBHEAD_RE.match(text)))
            continue
        if VERDICT_RE.match(text):
            out.append(verdict_block(VERDICT_RE.match(text)))
            continue
        match = AI_RE.match(text)
        if match:
            out.append(ai_block(match))
            continue
        if is_kv_line(text):
            out.append(kv_table(text))
            continue
        match = QUOTE_RE.match(text)
        if match:
            date, rest = match.group(1), match.group(2)
            out.append(Node(node.tag, node.attrs, [
                el("span", [f"{date}　"], **{"class": "date"}),
                rest,
            ]))
            continue
        out.append(Node(node.tag, node.attrs, group))
    return out


NUMERIC_CELL = re.compile(r"[-+−]?[\d,]+(?:\.\d+)?%?")


def mark_direction(text):
    if text[:1] in ("−", "-"):
        return "▼" + text
    if text[:1] == "+":
        return "▲" + text
    return text


CARD_BOUNDARY = {"h1", "h2", "h3", "h4", "h5", "h6"}
CARD_CONTAINERS = {"main", "div", "section", "article"}


def _class(node):
    return node.attrs.get("class", "") if isinstance(node, Node) else ""


def _starts_new_card(node):
    if not isinstance(node, Node):
        return False
    if node.tag in CARD_BOUNDARY:
        return True
    if node.tag == "table":
        # kv tables are part of the entry; a data table is a section of its own.
        return _class(node) != "kv"
    return _class(node) in ("force-block", "card", "verdict")


def _wrap_force_cards(children):
    """Wrap one 力量条目 and everything under it in a single card box."""
    out = []
    index = 0
    while index < len(children):
        node = children[index]
        if _class(node) != "force-block":
            out.append(node)
            index += 1
            continue
        card = el("div", list(node.children), **{"class": "card"})
        index += 1
        while index < len(children) and not _starts_new_card(children[index]):
            card.children.append(children[index])
            index += 1
        out.append(card)
    return out


def _lead_is_bold(node):
    if not isinstance(node, Node) or node.tag != "li":
        return False
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                return False
            continue
        return child.tag in ("strong", "b")
    return False


def _cards_from_lists(children):
    """A bullet list whose items lead with a bold label reads better as cards."""
    out = []
    for node in children:
        if (isinstance(node, Node) and node.tag == "ul"
                and len([c for c in node.children if isinstance(c, Node)]) >= 2
                and all(_lead_is_bold(c) for c in node.children if isinstance(c, Node))):
            for item in node.children:
                if isinstance(item, Node):
                    out.append(el("div", list(item.children), **{"class": "card slim"}))
            continue
        out.append(node)
    return out


def group_cards(root):
    for child in root.children:
        if isinstance(child, Node) and child.tag != "table":
            group_cards(child)
    if root.tag not in CARD_CONTAINERS or "card" in _class(root):
        return
    root.children = _cards_from_lists(_wrap_force_cards(root.children))


def style_tables(root):
    for table in iter_tags(root, "table"):
        if table.attrs.get("class") == "kv":
            continue
        table.attrs.setdefault("class", "data")
        for cell in iter_tags(table, "th"):
            cell.attrs.setdefault("scope", "col")
        for cell in iter_tags(table, "td"):
            text = normalize(cell.text())
            if text and NUMERIC_CELL.fullmatch(text):
                cell.attrs["class"] = "num"
                # No colour on e-ink: ▲/▼ carries the direction.
                cell.children = [mark_direction(text)]


def iter_tags(node, tag):
    for child in node.children:
        if isinstance(child, Node):
            if child.tag == tag:
                yield child
            yield from iter_tags(child, tag)


def anchor_sections(root):
    """Give each <h2> an id and return the (id, title) list for the TOC."""
    sections = []
    for index, heading in enumerate(iter_tags(root, "h2"), 1):
        title = normalize(heading.text())
        match = re.match(r"^(\d{2})\s*[/／]", title)
        anchor = "s" + (match.group(1) if match else f"{index:02d}")
        heading.attrs["id"] = anchor
        heading.attrs["class"] = "section first" if index == 1 else "section"
        if title:
            sections.append((anchor, title))
    return sections


def transform_tree(root):
    def walk(node):
        children = []
        for child in node.children:
            if not isinstance(child, Node):
                children.append(child)
                continue
            if child.tag in ("p", "li") and child.children:
                children.extend(transform_block(child))
            else:
                walk(child)
                children.append(child)
        node.children = children

    walk(root)
    group_cards(root)
    style_tables(root)
    return anchor_sections(root)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

# Numbers inside these are already styled, and headings look wrong with a bold
# figure, so we leave their text alone.
ESCAPE_SKIP = {"pre", "code", "h1", "h2", "h3", "h4", "h5", "h6"}
STYLE_SKIP = {"num", "score", "tag", "label", "date"}


def serialize(node, emphasis=True):
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(emphasize(child) if emphasis else html.escape(child, quote=False))
            continue
        if child.tag == "__quote__":
            continue
        attrs = "".join(
            f' {name}="{html.escape(value, quote=True)}"'
            for name, value in child.attrs.items()
        )
        if child.tag in VOID:
            parts.append(f"<{child.tag}{attrs}/>")
        else:
            keep_plain = child.tag in ESCAPE_SKIP or child.attrs.get("class") in STYLE_SKIP
            inner = serialize(child, emphasis and not keep_plain)
            parts.append(f"<{child.tag}{attrs}>{inner}</{child.tag}>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


def read_source(source):
    if source.startswith(("https://", "http://")):
        request = Request(source, headers={"User-Agent": "004-kindle-epub/1.0"})
        with urlopen(request, timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
    else:
        with open(source, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("页面超过 20 MiB，拒绝处理")
    return raw.decode("utf-8-sig")


def build_document(source, base_url=""):
    """Return (body_xhtml, sections, data_time) for the report body."""
    parser = MainTree(base_url)
    parser.feed(source)
    root = parser.tree()
    if not parser.found:
        raise ValueError("未找到非空的 <main> 报告正文")
    sections = transform_tree(root)
    body = serialize(root).strip()
    if not re.search(r"<[a-z]", body):
        raise ValueError("未找到非空的 <main> 报告正文")
    text = re.sub(r"<[^>]+>", "", body)
    if re.search(r"\{\{[A-Z][A-Z0-9_]*\}\}", text):
        raise ValueError("检测到未替换的模板占位符；请使用 004 构建后的报告")
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("正文超过 4 MiB，设备端可能拒绝打开")
    return body, sections, extract_data_time(plain_text(body))


DATA_TIME_RE = re.compile(r"扫描时间\s*(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})")
DATA_DATE_RE = re.compile(r"行情日期\s*(\d{4})-(\d{2})-(\d{2})")


def extract_data_time(text):
    """Return the report's own timestamp as ISO-8601 UTC, or None.

    004 states it twice: "扫描时间 2026-11-02 10:09:05 UTC" (when the data was
    read) and "行情日期 2026-11-02". The file's mtime says nothing about data
    freshness, so we keep them apart.
    """
    match = DATA_TIME_RE.search(text)
    if match:
        year, month, day, hour, minute, second = match.groups()
        return f"{year}-{month}-{day}T{hour}:{minute}:{second}Z"
    match = DATA_DATE_RE.search(text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}T00:00:00Z"
    return None


def plain_text(body_xhtml):
    text = re.sub(r"<[^>]+>", " ", body_xhtml)
    return re.sub(r"\s+", " ", html.unescape(text))


def extract_main(source, base_url=""):
    return build_document(source, base_url)[0]


# ---------------------------------------------------------------------------
# EPUB packaging
# ---------------------------------------------------------------------------

STYLE_CSS = """/* 004 report - tuned for e-ink / crengine */
html, body {
  background: #ffffff;
  color: #000000;
  margin: 0;
  padding: 0;
}
body {
  font-size: 1em;
  line-height: 1.55;
  text-align: left;
  hyphens: none;
}
h1, h2, h3, h4, h5, h6 {
  color: #000000;
  line-height: 1.25;
  margin: 0.7em 0 0.3em;
  page-break-after: avoid;
  text-align: left;
}
h1 { font-size: 1.55em; }
h2 { font-size: 1.3em; }
h3 { font-size: 1.14em; }
h4, h5, h6 { font-size: 1.02em; }

/* Headings carry a filled band so they never blend into the prose.
   E-ink renders solid black/grey crisply (no anti-aliased colour fringes). */
h1 {
  background: #000000;
  color: #ffffff;
  font-size: 1.5em;
  padding: 0.3em 0.4em;
  margin: 0 0 0.8em;
}
h2.section {
  background: #000000;
  color: #ffffff;
  font-size: 1.22em;
  padding: 0.35em 0.4em;
  margin: 1em 0 0.7em;
  border-top: 0;
  page-break-before: always;
}
/* The first section stays on the same page as the top of the book. */
h2.section.first { page-break-before: auto; }
h3.force {
  background: #dcdcdc;
  color: #000000;
  font-size: 1.1em;
  border-left: 4px solid #000000;
  padding: 0.25em 0.4em;
  margin: 1.1em 0 0.35em;
}
h4.sub {
  background: #efefef;
  color: #000000;
  font-size: 1.02em;
  padding: 0.2em 0.4em;
  margin: 0.9em 0 0.35em;
}

p { margin: 0 0 0.55em; text-indent: 0; }
ul, ol { margin: 0.2em 0 0.6em 1.1em; padding: 0; }
li { margin: 0 0 0.25em; }

/* Data timestamp band on the TOC page. */
p.meta {
  font-size: 0.9em;
  border: 1px solid #000000;
  padding: 0.3em 0.4em;
  margin: 0 0 0.9em;
}

/* Numbers: bold everywhere, right aligned inside tables. */
.num { font-weight: bold; }
td.num, th.num { text-align: right; font-weight: bold; }
span.date { font-weight: bold; white-space: nowrap; }

/* "重点 · 利空 · 100.0 分" becomes its own signal line. */
p.signal { margin: 0 0 0.5em; font-size: 0.95em; }
span.tag {
  border: 1px solid #000000;
  padding: 0 0.25em;
  margin-right: 0.35em;
  font-size: 0.9em;
}
span.score { font-size: 1.3em; font-weight: bold; margin-left: 0.2em; }

/* Field/value pairs are tables, not sentences. */
table.kv {
  width: 100%;
  border-collapse: collapse;
  margin: 0.2em 0 0.7em;
  font-size: 0.95em;
}
table.kv th {
  width: 5.5em;
  white-space: nowrap;
  border: 1px solid #000000;
  font-weight: bold;
  text-align: left;
  padding: 0.2em 0.3em;
}
table.kv td {
  border: 1px solid #000000;
  padding: 0.2em 0.3em;
  vertical-align: top;
}

/* Market data table. */
table.data {
  width: 100%;
  border-collapse: collapse;
  margin: 0.4em 0 0.9em;
  font-size: 0.92em;
}
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
table.data th, table.data td {
  border: 1px solid #000000;
  padding: 0.2em 0.3em;
  text-align: left;
  vertical-align: top;
}
table.data th { font-weight: bold; }

/* Cards: one 力量条目 = one bordered box, sub-blocks stacked inside. */
div.card {
  border: 2px solid #000000;
  padding: 0.35em 0.45em 0.45em;
  margin: 0.9em 0 1.15em;
  page-break-inside: avoid;
}
div.card > h3.force {
  margin: 0.05em 0 0.5em;
  font-size: 1.06em;
}
div.card > p.signal { margin: 0 0 0.45em; }
div.card > table.kv { margin: 0.1em 0 0.5em; }
div.card > ul { margin: 0.2em 0 0.3em 1em; }
div.card > div.ai {
  margin: 0.45em 0 0.15em;
  padding: 0.1em 0 0.1em 0.5em;
}
/* Bullet lists that lead with a bold label become one small card per item. */
div.card.slim {
  border-width: 1px;
  padding: 0.25em 0.4em;
  margin: 0.55em 0 0.6em;
}

/* AI commentary: boxed with a left rule, a notch smaller than body text. */
div.ai {
  border-left: 3px solid #000000;
  padding: 0.1em 0 0.1em 0.55em;
  margin: 0.45em 0 0.8em;
  font-size: 0.95em;
}
div.ai p { margin: 0 0 0.3em; }
p.ai-label { font-weight: bold; margin: 0 0 0.25em; }
span.label { font-weight: bold; }

/* Final verdict: boxed, figures enlarged. */
div.verdict {
  border: 2px solid #000000;
  padding: 0.45em 0.55em;
  margin: 0.7em 0 0.9em;
}
p.verdict-line { margin: 0 0 0.3em; }
div.verdict p:first-child { font-size: 1.06em; }
div.verdict .num { font-size: 1.15em; }

img { max-width: 100%; }
a { color: #000000; text-decoration: none; }
hr { border: 0; border-top: 1px solid #000000; margin: 0.9em 0; }
blockquote { margin: 0.5em 0 0.7em 0.8em; }
code, pre { font-family: monospace; font-size: 0.9em; }
"""


def _xhtml_document(body, title):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
        f"<head><meta charset=\"utf-8\"/><title>{html.escape(title)}</title>"
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f'<body><div class="report">{body}</div></body>\n'
        "</html>\n"
    )


def _toc_document(title, sections, meta=""):
    items = "".join(
        f'<li><a href="report.xhtml#{html.escape(anchor)}">{html.escape(heading)}</a></li>'
        for anchor, heading in sections
    ) or '<li><a href="report.xhtml">正文</a></li>'
    meta_html = f'<p class="meta">{meta}</p>' if meta else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">\n'
        f"<head><meta charset=\"utf-8\"/><title>目录</title>"
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f"<body><h1>目录</h1>{meta_html}<ul class=\"toc\">{items}</ul></body>\n</html>\n"
    )


def _nav_document(title, sections):
    items = "".join(
        f'<li><a href="report.xhtml#{html.escape(anchor)}">{html.escape(heading)}</a></li>'
        for anchor, heading in sections
    ) or '<li><a href="report.xhtml">正文</a></li>'
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">\n'
        f"<head><meta charset=\"utf-8\"/><title>{html.escape(title)}</title></head>\n"
        "<body><nav epub:type=\"toc\" id=\"toc\">"
        f"<h1>{html.escape(title)}</h1><ol>"
        '<li><a href="toc.xhtml">目录</a></li>'
        f"{items}</ol></nav></body>\n</html>\n"
    )


def _ncx_document(title, identifier, modified, sections):
    points = []
    for index, (anchor, heading) in enumerate(sections, 1):
        points.append(
            f'<navPoint id="nav{index}" playOrder="{index}">'
            f"<navLabel><text>{html.escape(heading)}</text></navLabel>"
            f'<content src="report.xhtml#{html.escape(anchor)}"/></navPoint>'
        )
    if not points:
        points.append(
            '<navPoint id="nav1" playOrder="1"><navLabel><text>正文</text></navLabel>'
            '<content src="report.xhtml"/></navPoint>'
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="{identifier}"/></head>\n'
        f"<docTitle><text>{html.escape(title)}</text></docTitle>\n"
        f"<navMap>{''.join(points)}</navMap>\n</ncx>\n"
    )


def _opf_document(title, identifier, modified, words):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:identifier id=\"bookid\">{identifier}</dc:identifier>\n"
        f"    <dc:title>{html.escape(title)}</dc:title>\n"
        "    <dc:language>zh-CN</dc:language>\n"
        "    <dc:creator>004-kindle</dc:creator>\n"
        f"    <dc:description>章鱼 AI·舆情 004 报告（{words} 字符）</dc:description>\n"
        f"    <meta property=\"dcterms:modified\">{modified}</meta>\n"
        "  </metadata>\n"
        "  <manifest>\n"
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '    <item id="toc" href="toc.xhtml" media-type="application/xhtml+xml"/>\n'
        '    <item id="body" href="report.xhtml" media-type="application/xhtml+xml"/>\n'
        '    <item id="css" href="style.css" media-type="text/css"/>\n'
        "  </manifest>\n"
        '  <spine toc="ncx"><itemref idref="toc"/><itemref idref="body"/></spine>\n'
        "</package>\n"
    )


CONTAINER_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    "  <rootfiles>\n"
    '    <rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/>\n'
    "  </rootfiles>\n"
    "</container>\n"
)


def write_epub(path, body, title="004 报告", modified=None, source="", sections=None,
               data_time=None):
    """Write a self-contained EPUB 3 to `path`; returns the plain-text length."""
    sections = sections or []
    stamp = datetime.now(timezone.utc)
    modified = (modified or stamp).strftime("%Y-%m-%dT%H:%M:%SZ")
    identifier = "urn:uuid:" + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"004-kindle:{modified}:{len(body)}"))
    words = len(re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", body)))
    built = stamp.strftime("%Y-%m-%d %H:%M UTC")
    meta = []
    if data_time:
        meta.append("数据时间：" + data_time.replace("T", " ").replace("Z", " UTC"))
    meta.append("文件生成：" + built)
    meta = " ｜ ".join(meta)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(temporary, "w") as zf:
        # The mimetype entry must come first and be stored uncompressed.
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML,
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf",
                    _opf_document(title, identifier, modified, words),
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", _nav_document(title, sections),
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.xhtml", _toc_document(title, sections, meta),
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", _ncx_document(title, identifier, modified, sections),
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/style.css", STYLE_CSS, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/report.xhtml", _xhtml_document(body, title),
                    compress_type=zipfile.ZIP_DEFLATED)
    temporary.replace(path)
    return words


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", help="004 构建后的 report.html 本地路径或公开 URL")
    ap.add_argument("--out", required=True, help="输出目录，例如 site")
    ap.add_argument("--title", default="004 报告", help="书名（默认 004 报告）")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        source = read_source(args.source)
        base_url = args.source if args.source.startswith(("https://", "http://")) else ""
        body, sections, data_time = build_document(source, base_url)
        out = Path(args.out)
        epub = out / "report.epub"
        words = write_epub(epub, body, title=args.title, source=args.source,
                           sections=sections, data_time=data_time)
        manifest = {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "doc": "report.epub",
            "title": args.title,
            "chars": words,
            "bytes": epub.stat().st_size,
            "sections": len(sections),
            "data_time": data_time or "",
            "source": args.source,
        }
        (out / "epub.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    print(f"完成：{manifest['bytes'] / 1024:.0f} KiB，{manifest['chars']} 字符，"
          f"{manifest['sections']} 个章节，数据时间 {data_time or '未知'}，{epub}")
    print(f"清单：{out / 'epub.json'}")
    print("上传 site/report.epub 与 site/epub.json，插件 Display mode 选 EPUB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
