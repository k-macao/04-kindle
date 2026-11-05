#!/usr/bin/env python3
"""把 004 发布页排成 EPUB，让 KOReader 用「字模式」显示真正的排版。

截图模式（render_report.py）保真但费流量、放大后发虚；这个脚本换一条路：
抓 004 的 <main> 正文，清洗成 XHTML，配一套电子墨水用的 CSS（白底黑字、
标题分级、表格黑线、行距 1.55），打包成单文件 EPUB。插件把 EPUB 下载到
设备后用 KOReader 阅读器打开，于是字号、行距、页边距、进度条、字典全部
由 KOReader 自己管，文字是矢量渲染，最锐利。

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
INLINE = {"b", "strong", "i", "em", "span", "a", "sup", "sub", "code", "small", "u", "mark", "time"}

# Attributes worth keeping; everything else (classes, ids, inline styles,
# event handlers) is stripped so our CSS is the only styling.
ATTRS = {
    "a": {"href"},
    "img": {"src", "alt"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}


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


class MainToXHTML(HTMLParser):
    """Re-emit the <main> subtree as well-formed XHTML."""

    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.inside = False
        self.found = False
        self.depth = 0
        self.drop = 0
        self.stack = []
        self.chunks = []

    # -- helpers ---------------------------------------------------------
    def _absolute(self, value):
        if not value:
            return value
        if self.base_url and not re.match(r"^([a-z]+:|//|#)", value, re.I):
            return urljoin(self.base_url, value)
        return value

    def _close_in(self, names, multi=False):
        """Close inline runs plus any open element named in `names`."""
        while self.stack and (self.stack[-1] in names or self.stack[-1] in INLINE):
            top = self.stack.pop()
            self.chunks.append(f"</{top}>")
            if top in names and not multi:
                break

    def _emit_start(self, tag, attrs):
        parts = []
        for name, value in attrs:
            allowed = ATTRS.get(tag)
            if not allowed or name not in allowed:
                continue
            if name in ("href", "src"):
                value = self._absolute(value)
            parts.append(f' {name}="{html.escape(value or "", quote=True)}"')
        if tag in VOID:
            self.chunks.append(f"<{tag}{''.join(parts)}/>")
        else:
            self.chunks.append(f"<{tag}{''.join(parts)}>")
            self.stack.append(tag)

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
            self._close_in({"p"})
        if tag in SIBLING_CLOSERS:
            names, multi = SIBLING_CLOSERS[tag]
            self._close_in(names, multi)
        if tag in KEEP:
            self._emit_start(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        if tag in DROP:
            return
        if self.inside and not self.drop and tag in KEEP:
            self._emit_start(tag, attrs)
            if tag not in VOID:
                self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.inside or tag in VOID:
            return
        if self.drop:
            self.drop -= 1
        elif tag in KEEP:
            # Close up to the matching tag so unbalanced HTML stays valid.
            if tag in self.stack:
                while self.stack:
                    open_tag = self.stack.pop()
                    self.chunks.append(f"</{open_tag}>")
                    if open_tag == tag:
                        break
        self.depth -= 1
        if self.depth == 0:
            self.inside = False

    def handle_data(self, data):
        if self.inside and not self.drop:
            self.chunks.append(html.escape(data, quote=False))

    # -- result ----------------------------------------------------------
    def xhtml(self):
        while self.stack:
            self.chunks.append(f"</{self.stack.pop()}>")
        return "".join(self.chunks)


def extract_main(source, base_url=""):
    """Return the report body as XHTML, or raise ValueError."""
    parser = MainToXHTML(base_url)
    parser.feed(source)
    body = parser.xhtml().strip()
    if not parser.found or not re.search(r"<[a-z]", body):
        raise ValueError("未找到非空的 <main> 报告正文")
    text = re.sub(r"<[^>]+>", "", body)
    if re.search(r"\{\{[A-Z][A-Z0-9_]*\}\}", text):
        raise ValueError("检测到未替换的模板占位符；请使用 004 构建后的报告")
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("正文超过 4 MiB，设备端可能拒绝打开")
    return body


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
p { margin: 0 0 0.55em; text-indent: 0; }
ul, ol { margin: 0.2em 0 0.6em 1.1em; padding: 0; }
li { margin: 0 0 0.25em; }
table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.4em 0 0.9em;
  font-size: 0.92em;
  page-break-inside: auto;
}
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th, td {
  border: 1px solid #000000;
  padding: 0.2em 0.3em;
  text-align: left;
  vertical-align: top;
}
th { font-weight: bold; }
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


def _nav_document(title):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">\n'
        f"<head><meta charset=\"utf-8\"/><title>{html.escape(title)}</title></head>\n"
        "<body><nav epub:type=\"toc\" id=\"toc\">"
        f"<h1>{html.escape(title)}</h1><ol>"
        '<li><a href="report.xhtml">正文</a></li>'
        "</ol></nav></body>\n</html>\n"
    )


def _ncx_document(title, identifier, modified):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="{identifier}"/></head>\n'
        f"<docTitle><text>{html.escape(title)}</text></docTitle>\n"
        "<navMap><navPoint id=\"nav1\" playOrder=\"1\">"
        f"<navLabel><text>正文</text></navLabel><content src=\"report.xhtml\"/>"
        "</navPoint></navMap>\n</ncx>\n"
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
        '    <meta name="cover" content="none"/>\n'
        "  </metadata>\n"
        "  <manifest>\n"
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        '    <item id="body" href="report.xhtml" media-type="application/xhtml+xml"/>\n'
        '    <item id="css" href="style.css" media-type="text/css"/>\n'
        "  </manifest>\n"
        '  <spine toc="ncx"><itemref idref="body"/></spine>\n'
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


def write_epub(path, body, title="004 报告", modified=None, source=""):
    """Write a self-contained EPUB 3 to `path`; returns the plain-text length."""
    stamp = datetime.now(timezone.utc)
    if modified is None:
        modified = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        modified = modified.strftime("%Y-%m-%dT%H:%M:%SZ")
    identifier = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"004-kindle:{modified}:{len(body)}"))
    words = len(re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", body)))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(temporary, "w") as zf:
        # The mimetype entry must come first and be stored uncompressed.
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf",
                    _opf_document(title, identifier, modified, words),
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", _nav_document(title), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", _ncx_document(title, identifier, modified),
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
        body = extract_main(source, base_url)
        out = Path(args.out)
        epub = out / "report.epub"
        words = write_epub(epub, body, title=args.title, source=args.source)
        manifest = {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "doc": "report.epub",
            "title": args.title,
            "chars": words,
            "bytes": epub.stat().st_size,
            "source": args.source,
        }
        (out / "epub.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    print(f"完成：{manifest['bytes'] / 1024:.0f} KiB，{manifest['chars']} 字符，{epub}")
    print(f"清单：{out / 'epub.json'}")
    print("上传 site/report.epub 与 site/epub.json，插件 Display mode 选 EPUB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
