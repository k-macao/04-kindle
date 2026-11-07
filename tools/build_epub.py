#!/usr/bin/env python3
"""把 004 发布页排成 EPUB，让 KOReader 用「字模式」显示真正的排版。

截图模式（render_report.py）保真但费流量、放大后发虚；这个脚本换一条路：
抓 004 的 <main> 正文，清洗成 XHTML，再按 004 自己的写作套路重新组织结构
（力量条目 → 标题 + 信号 + 分数；「归类 / 当次读数」→ 两列表格；AI 解读 →
左边线引用块；◆ 小标题 → 四级标题；章节加锚点与目录），并给排版加卡片与数据
强调：行情表格、章节导语（VIX 仪表、结论摘要等文字图表）装进卡片，数字统一
加粗并加背景填充突出，最后打包成单文件 EPUB。插件把 EPUB 下载到设备后用
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
import unicodedata
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

# 页面显示用的北京时间（固定 UTC+8，中国不实行夏令时）。
BEIJING = timezone(timedelta(hours=8))


def _beijing_text(moment):
    """Format an aware datetime as 'YYYY-MM-DD HH:MM:SS 北京时间'."""
    return moment.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"
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
# Dropped together with everything inside them. <aside> / <footer> 在 <main> 里
# 出现时基本是站点的侧栏与页脚（见 BODY_DROP_TAGS），正文的小标题在 <header> 里，
# 所以 header 只拆壳、不丢内容。
DROP = {
    "script", "style", "noscript", "template", "iframe", "button", "input",
    "select", "textarea", "svg", "canvas", "video", "audio", "object",
    "embed", "nav", "form", "aside", "footer",
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

    __slots__ = ("tag", "attrs", "children", "marks")

    def __init__(self, tag, attrs=None, children=None, marks=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = list(children or [])
        # 源页面的 class / #id 只作为「这块是什么」的判断依据（见 strip_status），
        # 不写进 attrs，所以不会带进 EPUB——排版仍然只由本文件的 CSS 决定。
        self.marks = set(marks or ())

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


def el(tag, children=None, marks=None, **attrs):
    return Node(tag, attrs, children if children is not None else [], marks)


# ---------------------------------------------------------------------------
# HTML -> tree
# ---------------------------------------------------------------------------
#
# 文字归一化 normalize() 在下面的「正文判定」里定义一次，两个转换器共用同一份实现。


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

    @staticmethod
    def _marks(attrs):
        """记住源页面的 class / #id / role，供 strip_status / strip_nonbody 认块用。"""
        marks = set()
        for name, value in attrs:
            if name == "class" and value:
                marks.update(value.split())
            elif name == "id" and value:
                marks.add("#" + value)
            elif name == "role" and value:
                # role="navigation" 之类的语义标注，和 class 一样只当判断依据。
                marks.add("@" + value.strip().lower())
        return marks

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
            node = el(tag, None, marks=self._marks(attrs), **self._attrs(tag, attrs))
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
# 状态类文字：整块丢弃
# ---------------------------------------------------------------------------
#
# 目标：正文「全部内容」都留（行情、力量条目、快讯标题与摘要、AI 解读、社区摘录、
# 预测、结论……），只删掉「这台机器这次跑得怎么样」的话——抓取/加载/推送状态、
# 端点与耗时、核验时间、缺口与降级计数、界面按钮与筛选 chips、构建兜底占位。
# 口径说明、方法论注释（「口径：…」「指标口径：…」「▶ 词库极性与摘录说明：…」
# 「注：…」「⚠️ 风险提示」）属于报告内容，保留。
#
# 下面三组规则与 kindle_report.py 里的同名常量必须逐条一致；
# tests/test_status_filter.py 会直接比对两个模块，改一处就得改两处。

# 源页面里这些 class / id 整块是界面或运行状态，连同子节点一起丢。
STATUS_DROP_CLASSES = frozenset({
    "push-wrap", "push-title", "push-tip", "terminal-log-box", "pixel-push-btn",
    "filter-row", "filter-tab", "filter-result", "hud-stat", "hud-stats",
    "live-state", "loading", "spinner", "skeleton", "status-bar", "fetch-status",
})
STATUS_DROP_IDS = frozenset({"#pushStatus", "#pushBtn", "#filterResult"})
# 整行/整块只有这几个字时，它是枚状态标签（例如社区卡片右上角的「未获取」）。
STATUS_CHIP_TEXT = frozenset({
    "未获取", "今日未获取", "抓取中", "加载中", "抓取失败", "同步中", "推送中",
})
# 行级规则：命中任意一条即丢掉整块。绝大多数以 ^ 锚定，避免误伤正文里
# 顺带提到「抓取」「降级」的说明句。
STATUS_LINE_PATTERNS = (
    # —— 界面与运行状态：推送框、终端状态行、加载提示、筛选 chips ——
    r"^(?:STATUS|TIME CHECK|TRANSMITTING|SUCCESS|FAILED|LOADING|FETCHING)\s*[>＞]",
    r"^(?:OFFLINE|CACHED)(?:\s*/\s*(?:OFFLINE|CACHED))?$",
    r"\d+\s*SOURCE\s+FEEDS\s*$",
    r"^(?:抓取中|加载中|正在抓取|正在加载|正在推送|正在发送|正在构建|请稍候|请稍后)",
    r"(?:抓取中|加载中|同步中|推送中)[…\.。]?\s*$",
    r"^微信推送\s*[·・]",
    r"^点击下方按钮",
    r"^▶?\s*立即推送",
    # —— 逐源抓取状态、端点与耗时、核验时间 ——
    r"^抓取状态[：:]",
    r"^当次(?:未获取|抓取成功|抓取失败|实抓)",
    r"^未获取原因[：:]",
    r"^未提供站内热度指标",
    r"^热度未知\s*[·・]",
    r"^端点\s+https?://",
    r"[·・]\s*抓取核验\s*\d{4}-\d{2}-\d{2}\s*$",
    r"[·・]\s*抓取于\s*\d{4}-\d{2}-\d{2}",
    r"核验日期\s*\d{4}-\d{2}-\d{2}",
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
    r"^时间未知\s*[·・]",
    r"^发布时间未知\s*[·・]",
    r"本次匹配\s*\d+\s*条[，,]\s*展示\s*\d+\s*条\s*$",
    # —— 本次构建的汇总状态、缺口与降级 ——
    r"^\d+\s*/\s*\d+\s*(?:项同步成功|个社区实抓成功|源可用|源均未获取)",
    r"^[^\d]{0,12}\d+\s*/\s*\d+\s*可用\s*[·・]",
    r"\d+\s*个接口本次自动降级",
    r"^查看(?:原帖|原文|原页|更多)",
    r"\d+\s*条入库\s*[·・]",
    r"^数据缺口[：:]",
    r"^本次判定[：:]",
    r"^窗口内无匹配快讯的小节[：:]",
    r"^数据日期\s.*[·・]\s*采集状态",
    r"^采集状态\s",
    # —— 构建兜底占位：产物缺失时页面上那段「怎么把它跑出来」的运行提示 ——
    r"^今日未获取",
    r"[—-]\s*今日未获取\s*$",
    r"待构建注入",
    r"^运行\s*python3?\s",
    r"^⚠️\s*未读到",
    r"^本栏(?:不再回填|不回填)",
    r"未找到\s*[\w./-]+\.json",
    r"恢复命令[：:]",
)
STATUS_LINE_RES = tuple(re.compile(pattern) for pattern in STATUS_LINE_PATTERNS)
# 内容被删空之后可以顺手丢掉的外壳；表格相关的一律保留，删了会让列错位。
STATUS_KEEP_WHEN_EMPTY = frozenset({
    "td", "th", "tr", "table", "thead", "tbody", "tfoot", "col", "colgroup",
    "caption", "img", "br", "hr", "main",
})


def is_status_text(text):
    """一行/一块文字是不是「状态类文字」（抓取中、STATUS、核验时间……）。"""
    text = normalize(text)
    if not text:
        return False
    if text in STATUS_CHIP_TEXT:
        return True
    return any(pattern.search(text) for pattern in STATUS_LINE_RES)


def _is_block(child):
    """块级子节点：它自己就是一块，不能和邻居的文字拼成一行来判。"""
    return isinstance(child, Node) and child.tag not in INLINE and child.tag != "br"


def _is_single_line(node):
    """这个节点是不是「一视觉行」（没有块级子节点、也没有 <br>）。

    只有单行节点才整块判定；跨行的容器交给 _runs 按行拆开判，否则一句状态文字
    会把同容器里的正文一起带走（真实页面里 03B 的采集概况就是这样：小标题、
    裸文本状态句、口径说明挤在同一个 div 里）。
    """
    return not any(_is_block(child) or (isinstance(child, Node) and child.tag == "br")
                   for child in node.children)


def _runs(children):
    """把子节点按块级元素与 <br> 切成行内片段，返回 [(start, end)] 索引区间。

    源页面里不少状态句没有自己的标签，直接是容器里的裸文本（还夹着 <strong>），
    所以除了「整块是元素」的情况，还得能按行内片段删。
    """
    runs = []
    start = end = None
    for index, child in enumerate(children):
        inline = not _is_block(child) and not (isinstance(child, Node) and child.tag == "br")
        if inline:
            if start is None:
                start = index
            end = index + 1
        elif start is not None:
            runs.append((start, end))
            start = end = None
    if start is not None:
        runs.append((start, end))
    return runs


def _run_text(run):
    return normalize("".join(child if isinstance(child, str) else child.text()
                             for child in run))


def strip_status(node):
    """就地删掉状态类文字，返回删除数量；正文一律不动。

    三层，自下而上：
    1. 递归清子节点；
    2. 元素级——class/#id 命中（推送框、筛选 chips）整块删；单行节点整块文字就是
       状态文字的（`抓取状态：…`、`当次实抓 · 端点 …`、chip「未获取」）整块删；
       内容被删空的外壳顺手丢掉，不留空白块；
    3. 行内片段级——容器里裸文本 + 行内元素混排出来的那一行，命中就只删这一行，
       同容器的正文（小标题、口径说明）原样留下。
    """
    dropped = 0
    for child in node.children:
        if isinstance(child, Node):
            dropped += strip_status(child)

    kept = []
    for child in node.children:
        if isinstance(child, Node):
            if child.marks & STATUS_DROP_CLASSES or child.marks & STATUS_DROP_IDS:
                dropped += 1
                continue
            if _is_single_line(child) and is_status_text(child.text()):
                dropped += 1
                continue
            if (child.tag not in STATUS_KEEP_WHEN_EMPTY and not normalize(child.text())
                    and not any(isinstance(item, Node) for item in child.children)):
                dropped += 1
                continue
        kept.append(child)

    result = []
    cursor = 0
    for start, end in _runs(kept):
        result.extend(kept[cursor:start])
        run = kept[start:end]
        if is_status_text(_run_text(run)):
            dropped += 1
            # 被删掉的这一行如果紧跟在 <br> 后面，那个换行也没意义了。
            if result and isinstance(result[-1], Node) and result[-1].tag == "br":
                result.pop()
        else:
            result.extend(run)
        cursor = end
    result.extend(kept[cursor:])
    node.children = result
    return dropped


# ---------------------------------------------------------------------------
# 正文判定：不是正文的不要，乱码不要
# ---------------------------------------------------------------------------
#
# 004 的社区摘要直接取自各站公开页的抓取文字，抓回来的东西常常不是文章正文：
# 导航条、「关于我们 / 网站声明 / 联系方式」、二维码与 App 下载、查看更多 /
# 加载更多、版权与 ICP 备案、编辑部地址…… 还有编码坏掉留下的乱码。这些都上不了
# 设备。判定只认「站点外壳」和「编码坏了」，不认「看起来像废话」——口径说明、
# 方法论注释、风险提示、「注：……」都属于报告正文，一律保留。
#
#   1. 结构：class / #id / role 命中站点外壳（导航、页脚、侧栏、评论、分享、广告、
#      cookie 条、分页……）→ 整块删；
#   2. 文字：一行/一段的站壳分段占比够高，或本来就不是句子、导航词却扎堆 → 整段删；
#   3. 行内：正文和页脚挤在同一段里时只切掉站壳那几节（trim_chrome_text），正文留着。
#
# 乱码同理，整段删，不留半句：替换符 U+FFFD、控制符、私用区、UTF-8 被当成单字节
# 解出来的双字节垃圾（Ã©、â€、æ–‡）、GBK 的「锟斤拷」、连片的高位 Latin-1 字母
# （GBK 中文被当 latin-1 读出来就是 ÐÂÀË²Æ¾­ 这样）。
#
# 这些常量与函数在 tools/build_epub.py 与 kindle_report.py 里各有一份、必须逐条一致：
# kindle_report.py 要单独拷进 004 仓库，不能 import 本仓库的模块。
# tests/test_body_filter.py 会直接比对两边的规则，改一处就得改两处。

# 源页面里这些 class / #id / role 整块是站点界面，连同子节点一起丢。
BODY_DROP_CLASSES = frozenset({
    # 导航、页头页脚、侧栏
    "nav", "navbar", "nav-bar", "navigation", "menu", "topbar", "top-bar",
    "site-nav", "global-nav", "header-nav", "main-nav", "breadcrumb", "breadcrumbs",
    "crumb", "site-header", "page-header", "site-footer", "page-footer", "footer",
    "global-footer", "sidebar", "side-bar", "aside", "widget-area", "related",
    "related-posts", "related-articles", "recommend", "recommended", "hot-list",
    "hot-news", "rank-list",
    # 关注、分享、订阅、登录、搜索、工具栏、分页、二维码
    "share", "share-bar", "share-buttons", "social", "social-links", "follow",
    "follow-us", "subscribe", "subscription", "newsletter", "login", "signin",
    "signup", "search-box", "searchbar", "site-search", "toolbar", "pagination",
    "pager", "page-nav", "back-to-top", "backtop", "qrcode", "qr-code",
    "app-download", "download-app", "app-bar", "cookie", "cookie-banner", "gdpr",
    # 评论、广告、弹窗
    "comments", "comment-list", "comment-form", "comment-box", "reply-form",
    "ad", "ads", "advert", "advertisement", "ad-slot", "ad-banner", "sponsor",
    "promo", "popup", "modal", "overlay",
    # 版权与备案
    "copyright", "copy-right", "site-info", "site-meta", "footer-info", "beian",
    "icp", "license",
})
BODY_DROP_IDS = frozenset({
    "#footer", "#header", "#nav", "#navbar", "#sidebar", "#comments",
    "#cookie-banner", "#back-to-top",
})
BODY_DROP_ROLES = frozenset({
    "@navigation", "@banner", "@contentinfo", "@complementary", "@search",
    "@menu", "@menubar", "@dialog",
})
# 这两个标签在 <main> 里出现时基本就是站点的页脚与侧栏（正文页眉里的小标题用
# <header>，不能一起丢）。
BODY_DROP_TAGS = frozenset({"aside", "footer"})

# 站壳字样：出现在短分段里基本可以认定那一段是站点界面，不是报告正文。
CHROME_TEXT_PATTERNS = (
    # —— 版权、备案、许可 ——
    r"版权所有", r"版權所有", r"版权声明", r"copyright", r"all rights reserved",
    r"icp\s*[备证]", r"备案号", r"增值电信业务经营许可证", r"网络文化经营许可证",
    r"广播电视节目制作经营许可证", r"违法和不良信息举报", r"举报电话",
    r"网上有害信息", r"算法备案",
    # —— 站点介绍与联系方式 ——
    r"关于我们", r"關於我們", r"网站声明", r"网站地图", r"网站导航", r"联系方式",
    r"联系我们", r"用户反馈", r"加入我们", r"友情链接", r"广告服务", r"广告合作",
    r"广告投放", r"隐私政策", r"用户协议", r"服务条款", r"使用条款", r"免责声明",
    r"^地址[：:]", r"^邮编[：:]", r"^邮箱[：:]", r"^电话[：:]", r"^传真[：:]",
    # —— 二维码与客户端 ——
    r"扫码(?:下载|关注|安装|进入|订阅)", r"扫描二维码", r"二维码", r"下载客户端",
    r"下载\s*app", r"app\s*下载", r"客户端下载", r"手机版", r"触屏版", r"电脑版",
    # —— 关注、分享与互动 ——
    r"关注我们", r"官方微博", r"微信公众号", r"微信扫一扫", r"分享到", r"分享至",
    r"收藏本页", r"打印本页", r"字号[：:]", r"返回顶部", r"返回首页", r"回到顶部",
    r"上一篇", r"下一篇", r"相关阅读", r"相关文章", r"热门推荐", r"猜你喜欢",
    r"更多精彩", r"查看更多", r"加载更多", r"展开全文", r"收起全文",
    r"我要评论", r"发表评论", r"网友评论", r"全部评论", r"评论区", r"责任编辑",
    r"站内搜索", r"登录后", r"注册登录",
    # —— 英文站壳 ——
    r"^home$", r"^about us$", r"^contact us$", r"^privacy(?: policy)?$",
    r"^terms(?: of (?:use|service))?$", r"^cookie(?: policy| settings)?$",
    r"^site ?map$", r"^subscribe$", r"^newsletter$", r"^(?:sign|log) ?in$",
    r"^sign ?up$", r"^register$", r"^search$", r"^read more$", r"^load more$",
    r"^share(?: this)?$", r"^comments?$", r"^advertisement$", r"^back to top$",
    r"^menu$", r"^skip to (?:main )?content$",
    # —— 日文站壳 ——
    r"ログイン", r"会員登録", r"トップページ", r"サイトマップ", r"利用規約",
    r"プライバシーポリシー", r"お問い合わせ", r"会社概要", r"ヘルプ", r"もっと見る",
    r"詳しく見る", r"前のページ", r"次のページ", r"マイページ", r"マイアカウント",
)
CHROME_TEXT_RES = tuple(re.compile(pattern, re.I) for pattern in CHROME_TEXT_PATTERNS)
# 整行强规则：版权、备案这类字样一出现，那一段几乎不可能是报告正文（正文里提到
# 「版权所有」是带逗号句号的句子，会被 PROSE_RE 挡掉，不会误删）。
CHROME_STRONG_RES = tuple(re.compile(pattern, re.I) for pattern in (
    r"版权所有", r"版權所有", r"版权声明", r"copyright", r"all rights reserved",
    r"icp\s*[备证]", r"备案号",
    r"[粤京沪浙苏闽川鲁豫湘鄂皖赣津渝冀晋辽吉黑桂琼贵云陕甘青宁新蒙藏]icp",
    r"增值电信业务经营许可证", r"网络文化经营许可证",
))

# 分段：竖线、中点这类结构分隔符永远切；空格只在紧挨汉字时切——英文正文按空格
# 切开会把 comments、home 这些词当成站壳，那是毁正文。
CHROME_SEGMENT_SPLIT = re.compile(
    r"[|｜·・•‧]+|\s{2,}|\s+(?=[\u3400-\u9fff])|(?<=[\u3400-\u9fff])\s+")
CHROME_SEGMENT_MAX = 30      # 站壳分段的长度上限：长句是正文，不是导航词
CHROME_DROP_RATIO = 0.7      # 站壳分段占到这么多字符，整段就不要了
NAV_TOKEN_CHROME_COUNT = 6   # 导航词扎堆到这个数、又不是句子 —— 整段是导航条
NAV_WORD_CHROME_COUNT = 3    # 英文导航词（Home / Login / About us…）扎堆到这个数

# 短分段命中即算站壳的词（都是「只可能出现在站点界面」的词）。
CHROME_TOKENS_CJK = frozenset({
    "首页", "网站首页", "登录", "注册", "更多", "查看更多", "加载更多", "收藏",
    "分享", "返回顶部", "返回首页", "关于我们", "联系我们", "网站地图", "帮助",
    "下载", "APP", "客户端", "字号", "二维码", "关注我们", "微信公众号",
    "官方微博", "展开全文", "收起全文", "上一篇", "下一篇", "热门推荐",
    "相关阅读", "相关文章", "猜你喜欢", "网友评论", "我要评论", "站内搜索",
    "ニュース", "掲示板", "投資信託", "ログイン", "マイページ", "サイトマップ",
    "会員登録",
})
# 导航条判定用的词表，比上面宽：只有在「一行里扎堆出现、又没有句子」时才用，
# 单个词出现在正文里（新闻、行情、数据……）不算。
NAV_TOKENS_CJK = CHROME_TOKENS_CJK | frozenset({
    "新闻", "资讯", "财经", "股票", "基金", "期货", "外汇", "港股", "美股", "行情",
    "数据", "直播", "视频", "专题", "活动", "全部", "评论", "搜索", "导航", "栏目",
    "频道", "排行", "热榜", "标签", "微博", "微信", "手机版", "电脑版", "电报",
    "话题", "盯盘", "投研", "看盘", "加红", "行业板块", "概念板块", "地域板块",
    "板块名称", "涨跌幅", "资金流入", "个股涨幅榜", "个股跌幅榜", "股票名称",
    "涨跌价", "上证指数", "深证成指", "创业板指", "中证500",
    "トップ", "検索", "銘柄", "株価", "為替", "ランキング", "新着", "決算",
    "企業", "速報", "前のページ", "次のページ",
})
NAV_TOKENS_WORD = frozenset({
    "home", "news", "markets", "login", "sign in", "sign up", "register", "search",
    "about us", "contact us", "privacy policy", "terms of service", "subscribe",
    "newsletter", "share", "comments", "advertisement", "menu", "site map",
    "read more", "load more", "back to top", "most popular", "related posts",
})
# 分段里带句读（逗号、顿号也算）的是句子：一句话里提到「用户反馈」不能当站壳删。
PROSE_RE = re.compile(r"[。！？；，、]")
# 句末标点：整行有句子收尾就不是导航条。英文的 . 只认「词尾」那种（后面是空格或
# 行尾），否则 "-.--" 这种空数据占位、"CO.," 这种缩写会被当成句子放过导航条。
PROSE_END_RE = re.compile(r"(?:[.!?](?=[\s\"'”’)\]]|$))|[。！？；]")
# 空数据占位：抓下来的骨架屏/行情占位（-.--、--、N/A）本身不是正文。
PLACEHOLDER_RE = re.compile(
    r"(?:-{1,2}\.--|-{2,}|—{2,}|n/?a|null|undefined|nan|暂无数据)", re.I)

# —— 乱码 ——
GARBLED_MARKERS = (
    "\ufffd", "锟斤拷", "烫烫烫", "屯屯屯", "ï¿½", "â€", "ã€", "å¤§", "æ–‡",
)
GARBLED_CHAR_RE = re.compile(
    "[\ufffd\ue000-\uf8ff\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")
# 缺字形方块（□□□）与替换符连着出现，是字体/编码坏了的典型样子。
GARBLED_SYMBOL_RE = re.compile(r"(?:□|\ufffd){2,}")
INVISIBLE_RE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")
GARBLED_RATIO = 0.12         # 一段里可疑字符占到这么多，整段当乱码
GARBLED_DOC_RATIO = 0.02     # 全文（过滤之后）还这么多，说明源页面编码有问题，拒绝生成
MOJIBAKE_LEAD = frozenset(chr(code) for code in range(0xC0, 0x100))
MOJIBAKE_TRAIL = frozenset(chr(code) for code in range(0xA0, 0xC0)) | frozenset(
    "€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ")
# 连片高位 Latin-1：GBK 中文被当 latin-1 解码时整片都是这类字符。
LATIN_RUN_RE = re.compile(
    "[\u00a0-\u00ff\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc"
    "\u2030\u2039\u203a\u20ac\u2122]{3,}")
CHARSET_RE = re.compile(rb"charset\s*=\s*[\"']?\s*([\w-]+)", re.I)


def normalize(text):
    return re.sub(r"[ \t\r\n\f\v]+", " ", text.replace("\xa0", " ")).strip()


def _is_suspicious_char(char):
    if char in "\t\n\r":
        return False
    return unicodedata.category(char) in ("Cc", "Co", "Cs", "Cn")


def _mojibake_pairs(text):
    """UTF-8 被当 latin-1/cp1252 解码时的双字节垃圾（Ã©、â€、æ–‡…）。"""
    return sum(1 for first, second in zip(text, text[1:])
               if first in MOJIBAKE_LEAD and second in MOJIBAKE_TRAIL)


def garbled_ratio(text):
    """一段文字里可疑字符的占比（0～1），乱码判定与自检都用它。"""
    text = text.strip()
    if not text:
        return 0.0
    bad = sum(1 for char in text if _is_suspicious_char(char))
    bad += 2 * _mojibake_pairs(text)
    bad += sum(len(run.group(0)) for run in LATIN_RUN_RE.finditer(text))
    return min(1.0, bad / len(text))


def is_garbled(text):
    """这一段是不是乱码：替换符/控制符/私用区、双字节垃圾、连片高位 Latin-1。"""
    text = text.strip()
    if not text:
        return False
    if any(marker in text for marker in GARBLED_MARKERS):
        return True
    if GARBLED_SYMBOL_RE.search(text):
        return True
    if len(text) < 6:
        return any(_is_suspicious_char(char) for char in text)
    return garbled_ratio(text) >= GARBLED_RATIO


def clean_text(text):
    """去掉不可见字符和零散坏字符；整段乱码由 is_garbled 判掉。"""
    return GARBLED_CHAR_RE.sub("", INVISIBLE_RE.sub("", text))


def _segment_core(segment):
    return segment.strip("。，、,.!！?？()（）[]【】「」『』<>《》:：;；\"'’‘“”-—_+|/\\ ")


def segment_is_chrome(segment):
    """一个分段（按空格/竖线/中点切出来的那点文字）是不是站点外壳。"""
    core = _segment_core(segment)
    if not core:
        return False
    if PROSE_RE.search(segment):
        return False
    if core in CHROME_TOKENS_CJK or core.lower() in NAV_TOKENS_WORD:
        return True
    if PLACEHOLDER_RE.fullmatch(core):
        return True
    return (len(core) <= CHROME_SEGMENT_MAX
            and any(pattern.search(core) for pattern in CHROME_TEXT_RES))


def chrome_ratio(text):
    """站壳分段占全部字符的比例（0～1），空格与分隔符不计。"""
    segments = [segment for segment in CHROME_SEGMENT_SPLIT.split(text) if segment.strip()]
    total = sum(len(segment) for segment in segments)
    if not total:
        return 0.0
    chrome = sum(len(segment) for segment in segments if segment_is_chrome(segment))
    return chrome / total


def nav_word_hits(text):
    """一行里命中了多少个英文导航词（按整词匹配，comments 不会匹配到 commentary）。"""
    lowered = text.lower()
    return sum(1 for token in NAV_TOKENS_WORD
               if re.search(r"(?<![a-z])" + re.escape(token) + r"(?![a-z])", lowered))


def nav_token_count(text):
    """一行里命中了多少个导航词（中英日都算，不同词计数）。"""
    hits = {token for token in NAV_TOKENS_CJK if token in text}
    return len(hits) + nav_word_hits(text)


def is_chrome_text(text, ratio=CHROME_DROP_RATIO):
    """一行/一段是不是站点外壳（导航条、页脚、版权与备案、按钮条……）。"""
    text = normalize(text)
    if not text:
        return False
    if chrome_ratio(text) >= ratio:
        return True
    # 版权/备案样板：没有句读的一整行，直接当站壳。
    if not PROSE_RE.search(text) and any(p.search(text) for p in CHROME_STRONG_RES):
        return True
    # 导航词扎堆、又完全没有句子（抓下来的导航条就是这样），整段当站壳。
    if not PROSE_END_RE.search(text):
        if nav_token_count(text) >= NAV_TOKEN_CHROME_COUNT:
            return True
        if nav_word_hits(text) >= NAV_WORD_CHROME_COUNT:
            return True
    return False


def _segment_spans(text):
    spans = []
    cursor = 0
    for match in CHROME_SEGMENT_SPLIT.finditer(text):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return spans


def trim_chrome_text(text):
    """切掉行内的站壳分段，正文分段原样留下（没有可切的就原样返回）。"""
    text = clean_text(text)
    kept = []
    cursor = 0
    for start, end in _segment_spans(text):
        if segment_is_chrome(text[start:end]):
            kept.append((cursor, start))
            cursor = end
    kept.append((cursor, len(text)))
    if len(kept) == 1:
        return text
    joined = "".join(text[start:end] for start, end in kept)
    return re.sub(r"[\s|｜·・•‧]+", " ", joined).strip()


def decode_source(raw):
    """把抓回来的字节解成文字：UTF-8 → 页面自己声明的编码 → 报错。

    宁可不生成，也不要把一页乱码推给设备，所以最后一步是抛 ValueError。
    """
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    match = CHARSET_RE.search(raw[:4096])
    if match:
        encoding = match.group(1).decode("ascii", "ignore")
        try:
            return raw.decode(encoding).lstrip("\ufeff")
        except (LookupError, UnicodeDecodeError):
            pass
    raise ValueError("源页面不是 UTF-8（声明的编码也解不出来），解出来只会是乱码，已拒绝处理")


def strip_nonbody(node):
    """就地删掉站点外壳与乱码，返回删除数量；正文一律不动。

    三层，自下而上，和 strip_status 同一套路子：
    1. 递归清子节点；
    2. 元素级——class/#id/role 命中站点外壳、<aside>/<footer> 容器、单行节点的
       文字整段是站壳或乱码 → 整块删；内容被删空的外壳顺手丢掉；
    3. 行内片段级——容器里的裸文本按行拆开判，正文与页脚挤在同一行时只切站壳
       那几节，同容器的正文（小标题、口径说明）原样留下。
    """
    dropped = 0
    for child in node.children:
        if isinstance(child, Node):
            dropped += strip_nonbody(child)

    kept = []
    for child in node.children:
        if isinstance(child, Node):
            if (child.marks & BODY_DROP_CLASSES or child.marks & BODY_DROP_IDS
                    or child.marks & BODY_DROP_ROLES or child.tag in BODY_DROP_TAGS):
                dropped += 1
                continue
            if _is_single_line(child):
                raw = child.text()
                text = clean_text(raw)
                # 没有文字的单行节点（<img>、<br>、空 <span>）交给下面的空壳检查，
                # 这里只判有字的块，否则图片会被当成「删空了」删掉。
                if text:
                    if is_garbled(text) or is_chrome_text(text):
                        dropped += 1
                        continue
                    # 只对「一视觉行」做行内切割：多行容器按行留白排版，
                    # 摊平成一行反而会把版式弄坏，交给下面的按行判定。
                    if "\n" not in raw:
                        trimmed = trim_chrome_text(text)
                        if not trimmed:
                            dropped += 1
                            continue
                        if normalize(trimmed) != normalize(raw):
                            # 只切掉站壳那几节；行内标签留不住，正文本身一个字没动。
                            child.children = [trimmed]
            # 空壳检查与状态过滤共用一套「删空了也别丢」的标签（表格相关、img、br…）。
            if (child.tag not in STATUS_KEEP_WHEN_EMPTY and not normalize(child.text())
                    and not any(isinstance(item, Node) for item in child.children)):
                dropped += 1
                continue
        kept.append(child)

    result = []
    cursor = 0
    for start, end in _runs(kept):
        result.extend(kept[cursor:start])
        run = kept[start:end]
        raw = "".join(child if isinstance(child, str) else child.text() for child in run)
        text = clean_text(normalize(raw))
        if not text:
            # 只有 <img>/<br>/空标签的片段：不是站壳也不是乱码，原样留下。
            result.extend(run)
        elif is_garbled(text) or is_chrome_text(text):
            dropped += 1
            if result and isinstance(result[-1], Node) and result[-1].tag == "br":
                result.pop()
        elif "\n" not in raw and (trimmed := trim_chrome_text(text)):
            # 这一行只切掉了站壳那几节：用切好的文字替掉整段行内节点。
            if normalize(trimmed) != normalize(text):
                result.append(trimmed)
            else:
                result.extend(run)
        elif "\n" not in raw:
            # 切完什么都不剩：整行都是站壳。
            dropped += 1
        else:
            result.extend(run)
        cursor = end
    result.extend(kept[cursor:])
    node.children = result
    return dropped


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


def iter_all(node):
    """Yield every node in the tree, including `node` itself."""
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from iter_all(child)


def wrap_data_cards(root):
    """行情快照等数据表格也装进卡片：文字之外，图表按卡片呈现。"""
    for parent in list(iter_all(root)):
        children = parent.children
        for index, child in enumerate(children):
            if (isinstance(child, Node) and child.tag == "table"
                    and child.attrs.get("class") == "data"):
                children[index] = el("div", [child], **{"class": "card chart"})


# Section intros end at the first structured block; kv tables belong to the
# intro (a meta line is still an intro, not a boundary).
LEAD_BOUNDARY = {"force-block", "card", "verdict", "ai"}


def _find_parent(root, target):
    for child in root.children:
        if child is target:
            return root
        if isinstance(child, Node):
            found = _find_parent(child, target)
            if found is not None:
                return found
    return None


def _is_structured(node):
    if not isinstance(node, Node):
        # Whitespace-only text nodes between blocks are formatting, not
        # content — they must not break a section's intro run.
        return bool(node.strip())
    if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote",
                    "figure"}:
        return True
    if node.tag == "table":
        return _class(node) != "kv"
    return _class(node) in LEAD_BOUNDARY


def wrap_section_leads(root):
    """每个章节开头（标题之后、第一个结构化块之前）的连续导语装进卡片。

    导语是 00 章的 VIX 仪表、07 章的结论摘要这类"文字图表"：两行以上才装卡，
    单行或已转成表格的元信息保持原样。
    """
    for section in list(iter_tags(root, "h2")):
        parent = _find_parent(root, section)
        if parent is None:
            continue
        children = parent.children
        try:
            index = children.index(section)
        except ValueError:
            continue
        run = []
        cursor = index + 1
        while cursor < len(children) and not _is_structured(children[cursor]):
            run.append(children[cursor])
            cursor += 1
        if len(run) >= 2:
            children[index + 1:index + 1 + len(run)] = [
                el("div", run, **{"class": "card lead"})]


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
    wrap_data_cards(root)
    wrap_section_leads(root)
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
    return decode_source(raw)


def build_document(source, base_url="", stats=None):
    """Return (body_xhtml, sections, data_time) for the report body.

    `stats`（可选 dict）会写入 {"status_removed": N}，供清单与日志说明这次
    删掉了多少状态块。
    """
    parser = MainTree(base_url)
    parser.feed(source)
    root = parser.tree()
    if not parser.found:
        raise ValueError("未找到非空的 <main> 报告正文")
    # 占位符检查与源数据时间都取自「过滤前」的正文：模板占位符可能正好落在被删的
    # 状态块里（{{FETCH_STATUS}}），而扫描时间也可能被更激进的过滤规则删掉——
    # 先取时间再删状态，新鲜度闸门才不会拿到空值。
    raw_text = normalize(root.text())
    if re.search(r"\{\{[A-Z][A-Z0-9_]*\}\}", raw_text):
        raise ValueError("检测到未替换的模板占位符；请使用 004 构建后的报告")
    data_time, data_time_kind = extract_data_time_detail(
        raw_text, datetime.now(timezone.utc))
    if stats is not None:
        # 精度要跟着时间一起走：只有日期时，目录页不能显示成精确到秒，
        # 新鲜度闸门也不能拿午夜当时刻去算「距今多少小时」。
        stats["data_time_kind"] = data_time_kind
        stats["status_removed"] = strip_status(root)
        stats["body_removed"] = strip_nonbody(root)
    else:
        strip_status(root)
        strip_nonbody(root)
    sections = transform_tree(root)
    body = serialize(root).strip()
    if not re.search(r"<[a-z]", body):
        raise ValueError("未找到非空的 <main> 报告正文")
    # 过滤之后还剩这么多乱码，说明源页面本身的编码就坏了：不推送，别再往下游传垃圾。
    if garbled_ratio(plain_text(body)) >= GARBLED_DOC_RATIO:
        raise ValueError("正文里乱码过多（源页面编码可能有问题），拒绝生成")
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("正文超过 4 MiB，设备端可能拒绝打开")
    return body, sections, data_time


DATA_TIME_RE = re.compile(r"扫描时间\s*(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})")
DATA_DATE_RE = re.compile(r"行情日期\s*(\d{4})-(\d{2})-(\d{2})")
# 004 在 2026-11-06 换了模板：正文里不再有「扫描时间 … · 数据覆盖 …」那一行，
# 只剩构建戳 {{TS_FULL}}，形如「◆ 行情快照 · 11 月 6 日 · 2026-11-06 22:57:20 UTC」。
# 旧模板里这两个值是同一个（扫描时间 == TS_FULL），所以构建戳可以直接当源数据时间用。
DATA_BUILD_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\s*UTC")


def extract_data_time_detail(text, now=None):
    """Return (iso_utc_or_None, kind) for the report's own timestamp.

    `kind` 说明这个时间从哪儿来，精度差别很大，显示和新鲜度判定都要分开对待：

      "scan"  —— 「扫描时间 2026-11-02 10:09:05」（旧模板，秒级）
      "build" —— 正文里的构建戳「2026-11-06 22:57:20 UTC」（新模板，秒级）
      "date"  —— 只有「行情日期 2026-11-02」（**日级，时刻未知**）
      None    —— 正文里没有任何可用时间

    文件 mtime 说明不了数据新鲜度，所以一律以正文里的时间为准。
    """
    match = DATA_TIME_RE.search(text)
    if match:
        year, month, day, hour, minute, second = match.groups()
        return f"{year}-{month}-{day}T{hour}:{minute}:{second}Z", "scan"
    stamps = DATA_BUILD_RE.findall(text)
    if stamps and now is not None:
        # 晚于「现在」的戳不是构建戳，是正文里引用的未来时间（例如某条快讯预告
        # 「2027-01-01 00:00:00 UTC 生效」）；留着它会让新鲜度闸门永远判「很新」。
        limit = now + timedelta(hours=1)
        kept = []
        for parts in stamps:
            moment = parse_data_time("{}-{}-{}T{}:{}:{}Z".format(*parts))
            if moment is not None and moment <= limit:
                kept.append(parts)
        stamps = kept
    if stamps:
        # 正文里可能有多个 UTC 戳（逐源抓取戳都早于整页构建戳），取最晚的那个：
        # 最晚的才是「这份报告什么时候做出来的」，也就是报告里最新证据的时间。
        year, month, day, hour, minute, second = max(stamps)
        return f"{year}-{month}-{day}T{hour}:{minute}:{second}Z", "build"
    match = DATA_DATE_RE.search(text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}T00:00:00Z", "date"
    return None, None


def extract_data_time(text, now=None):
    """Return the report's own timestamp as ISO-8601 UTC, or None.

    只给时间值；精度（秒级还是日级）见 `extract_data_time_detail`。
    """
    return extract_data_time_detail(text, now)[0]


def parse_data_time(value):
    """Parse an ISO-8601 timestamp (as stored in epub.json) into UTC."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def check_data_age(data_time, now=None, max_hours=12, kind=None):
    """Return (ok, reason): 源数据时间距今是否还在 max_hours（默认 12 小时）内。

    数据新鲜度以报告正文里的行情/采集时间（data_time）为准，不是文件生成时间：
    超过上限就不推给设备，避免「文件新、数据旧」。源时间缺失或无法解析时
    也视为不通过——无法确认新鲜度就不该推送。

    `kind` 是 `extract_data_time_detail` 给的精度："scan"/"build" 是秒级时刻，
    "date" 表示正文只给到交易日，此时 data_time 里的 00:00:00 是补出来的、
    不是真实时刻，不能拿它算年龄——否则报告当天晚上就会「距今 23 小时」误报过期
    （上游 2026-11-06 换模板去掉「扫描时间」行之后就撞上了这个）。日期精度改按
    当天 23:59:59 UTC 起算：当天算新鲜，跨天超过 max_hours 才判过期。
    """
    now = now or datetime.now(timezone.utc)
    max_hours = float(max_hours)
    then = parse_data_time(data_time)
    if then is None:
        return False, "报告里没有可用的源时间（扫描时间/构建戳/行情日期），无法确认新鲜度"
    label = "源时间"
    if kind == "date":
        then = then.replace(hour=23, minute=59, second=59)
        label = "源日期（正文只到日）"
    age_hours = max(0.0, (now - then).total_seconds() / 3600)
    if age_hours > max_hours:
        return False, f"{label} {data_time} 距今 {age_hours:.1f} 小时，超过 {max_hours:g} 小时上限"
    return True, f"{label} {data_time} 距今 {age_hours:.1f} 小时，未超过 {max_hours:g} 小时上限"


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
  line-height: 1.5;
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

p { margin: 0 0 0.6em; }
/* 中文段落首行缩进两字，整齐美观；表格、引用、信号行等不缩进。 */
p { text-indent: 2em; }
p.ai-line, p.verdict-line, p.signal, p.meta, p.ai-label { text-indent: 0; }
ul, ol { margin: 0.2em 0 0.6em 1.1em; padding: 0; }
li { margin: 0 0 0.25em; }

/* Data timestamp band on the TOC page. */
p.meta {
  font-size: 0.9em;
  border: 1px solid #000000;
  padding: 0.3em 0.4em;
  margin: 0 0 0.9em;
}

/* Numbers: bold everywhere, right aligned inside tables, with a grey fill
   so figures stand out from the prose (data emphasis). */
.num { font-weight: bold; background: #dcdcdc; }
td.num, th.num { text-align: right; font-weight: bold; background: #dcdcdc; }
span.date { font-weight: bold; white-space: nowrap; background: #efefef; }

/* "重点 · 利空 · 100.0 分" becomes its own signal line. */
p.signal { margin: 0 0 0.5em; font-size: 0.95em; }
span.tag {
  border: 1px solid #000000;
  padding: 0 0.25em;
  margin-right: 0.35em;
  font-size: 0.9em;
}
span.score {
  background: #000000;
  color: #ffffff;
  padding: 0 0.3em;
  font-size: 1.3em;
  font-weight: bold;
  margin-left: 0.2em;
}

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
  background: #efefef;
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
table.data th { font-weight: bold; background: #efefef; }

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

/* Section intro cards: VIX gauge, verdict summaries and other text charts. */
div.card.lead {
  margin: 0.5em 0 1em;
}
div.card.lead > p { margin: 0 0 0.45em; }
div.card.lead > table.kv { margin: 0.1em 0 0.5em; }
div.card.lead > p:last-child { margin-bottom: 0; }

/* Data tables (quotes, forecasts) get their own card. */
div.card.chart {
  margin: 0.4em 0 0.9em;
}
div.card.chart > table.data { margin: 0; }
div.card > p:last-child, div.card > table:last-child,
div.card > ul:last-child { margin-bottom: 0; }

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
               data_time=None, data_time_kind=None):
    """Write a self-contained EPUB 3 to `path`; returns the plain-text length."""
    sections = sections or []
    stamp = modified or datetime.now(timezone.utc)
    modified_text = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    identifier = "urn:uuid:" + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"004-kindle:{modified_text}:{len(body)}"))
    words = len(re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", body)))
    # 目录页第一行是本次运行（更新）时间；源数据时间（004 报告里的扫描时间，
    # 新模板下是页面构建戳）放在后面。页面上统一按北京时间（UTC+8）显示；
    # epub.json 与提交信息仍保持 UTC。
    meta = ["更新时间：" + _beijing_text(stamp)]
    if data_time:
        if data_time_kind == "date":
            # 正文只给到交易日时，00:00:00 是补出来的，换算成北京时间会变成
            # 「08:00:00」这种看着精确到秒的假时刻；如实写成日期。
            meta.append("源数据日期：" + data_time[:10])
        else:
            parsed = parse_data_time(data_time)
            meta.append("源数据时间：" + (_beijing_text(parsed) if parsed
                                      else data_time.replace("T", " ").replace("Z", " UTC")))
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
                    _opf_document(title, identifier, modified_text, words),
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", _nav_document(title, sections),
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.xhtml", _toc_document(title, sections, meta),
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", _ncx_document(title, identifier, modified_text, sections),
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
    ap.add_argument("--base-url", default=None,
                    help="源是本地文件时，用这个地址把相对链接/图片补成绝对地址"
                         "（例如 https://k-macao.github.io/004/）；也作为清单里的 source")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        source = read_source(args.source)
        # 只抓一次页面、两种产物共用同一份时，源是本地文件，相对地址要靠 --base-url 补。
        if args.base_url:
            base_url = args.base_url
        else:
            base_url = args.source if args.source.startswith(("https://", "http://")) else ""
        stats = {}
        body, sections, data_time = build_document(source, base_url, stats)
        out = Path(args.out)
        epub = out / "report.epub"
        # 同一个时间戳同时写入 EPUB 目录页、epub.json 与提交信息，三者保持一致。
        generated = datetime.now(timezone.utc).replace(microsecond=0)
        # 清单里的 source 记「这份内容来自哪个地址」：本地文件 + --base-url 时记那个地址，
        # 否则记的是本地路径，追溯不到源。
        source_label = args.base_url or args.source
        data_time_kind = stats.get("data_time_kind")
        words = write_epub(epub, body, title=args.title, source=source_label,
                           modified=generated, sections=sections, data_time=data_time,
                           data_time_kind=data_time_kind)
        manifest = {
            "generated": generated.isoformat(timespec="seconds"),
            "doc": "report.epub",
            "title": args.title,
            "chars": words,
            "bytes": epub.stat().st_size,
            "sections": len(sections),
            "data_time": data_time or "",
            # 源时间精度：scan=扫描时间行，build=页面构建戳，date=只到交易日。
            # 设备与新鲜度闸门据此决定怎么显示、怎么算年龄。
            "data_time_kind": data_time_kind or "",
            # 这次删掉多少个状态块（抓取状态、端点耗时、核验时间、推送框……）。
            "status_removed": stats.get("status_removed", 0),
            # 这次删掉多少块「不是正文」的东西（站点导航/页脚/版权备案、乱码段）。
            "body_removed": stats.get("body_removed", 0),
            "source": source_label,
        }
        (out / "epub.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    print(f"完成：{manifest['bytes'] / 1024:.0f} KiB，{manifest['chars']} 字符，"
          f"{manifest['sections']} 个章节，数据时间 {data_time or '未知'}，{epub}")
    print(f"状态类文字：已删除 {manifest['status_removed']} 块（正文全部保留）")
    print(f"非正文内容：已删除 {manifest['body_removed']} 块（站点导航/页脚/版权备案、乱码段）")
    print(f"清单：{out / 'epub.json'}")
    print("上传 site/report.epub 与 site/epub.json，插件 Display mode 选 EPUB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
