#!/usr/bin/env python3
"""Turn a *built* 004 report into a self-contained, paginated webpage."""
import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.request import Request, urlopen

MAX_BYTES = 20 * 1024 * 1024
BLOCKS = {'p', 'div', 'section', 'article', 'h1', 'h2', 'h3', 'h4', 'li', 'tr', 'br', 'hr', 'table'}
# <nav>/<aside>/<footer> 在 <main> 里出现时基本是站点的导航、侧栏与页脚
# （见 BODY_DROP_TAGS）；正文的小标题在 <header> 里，只拆壳、不丢内容。
SKIP = {'script', 'style', 'noscript', 'button', 'svg', 'nav', 'form', 'iframe',
        'template', 'select', 'textarea', 'aside', 'footer'}
VOID = {'br', 'hr', 'img', 'input', 'meta', 'link', 'source', 'wbr', 'area', 'embed', 'param', 'track'}

# ---------------------------------------------------------------------------
# 状态类文字：不要
# ---------------------------------------------------------------------------
#
# 正文「全部内容」都留，只删「这台机器这次跑得怎么样」的话：抓取/加载/推送状态、
# 端点与耗时、核验时间、缺口与降级计数、界面按钮与筛选 chips、构建兜底占位。
# 口径说明与方法论注释（「口径：…」「指标口径：…」「▶ 词库极性与摘录说明：…」
# 「注：…」「⚠️ 风险提示」）是报告内容，保留。
#
# 这四组规则与 tools/build_epub.py 里的同名常量必须逐条一致（本文件要单独拷进
# 004 仓库，所以不能 import）；tests/test_status_filter.py 会直接比对两边。

STATUS_DROP_CLASSES = frozenset({
    'push-wrap', 'push-title', 'push-tip', 'terminal-log-box', 'pixel-push-btn',
    'filter-row', 'filter-tab', 'filter-result', 'hud-stat', 'hud-stats',
    'live-state', 'loading', 'spinner', 'skeleton', 'status-bar', 'fetch-status',
})
STATUS_DROP_IDS = frozenset({'#pushStatus', '#pushBtn', '#filterResult'})
STATUS_CHIP_TEXT = frozenset({
    '未获取', '今日未获取', '抓取中', '加载中', '抓取失败', '同步中', '推送中',
})
STATUS_LINE_PATTERNS = (
    # —— 界面与运行状态：推送框、终端状态行、加载提示、筛选 chips ——
    r'^(?:STATUS|TIME CHECK|TRANSMITTING|SUCCESS|FAILED|LOADING|FETCHING)\s*[>＞]',
    r'^(?:OFFLINE|CACHED)(?:\s*/\s*(?:OFFLINE|CACHED))?$',
    r'\d+\s*SOURCE\s+FEEDS\s*$',
    r'^(?:抓取中|加载中|正在抓取|正在加载|正在推送|正在发送|正在构建|请稍候|请稍后)',
    r'(?:抓取中|加载中|同步中|推送中)[…\.。]?\s*$',
    r'^微信推送\s*[·・]',
    r'^点击下方按钮',
    r'^▶?\s*立即推送',
    # —— 逐源抓取状态、端点与耗时、核验时间 ——
    r'^抓取状态[：:]',
    r'^当次(?:未获取|抓取成功|抓取失败|实抓)',
    r'^未获取原因[：:]',
    r'^未提供站内热度指标',
    r'^热度未知\s*[·・]',
    r'^端点\s+https?://',
    r'[·・]\s*抓取核验\s*\d{4}-\d{2}-\d{2}\s*$',
    r'[·・]\s*抓取于\s*\d{4}-\d{2}-\d{2}',
    r'核验日期\s*\d{4}-\d{2}-\d{2}',
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}',
    r'^时间未知\s*[·・]',
    r'^发布时间未知\s*[·・]',
    r'本次匹配\s*\d+\s*条[，,]\s*展示\s*\d+\s*条\s*$',
    # —— 本次构建的汇总状态、缺口与降级 ——
    r'^\d+\s*/\s*\d+\s*(?:项同步成功|个社区实抓成功|源可用|源均未获取)',
    r'^[^\d]{0,12}\d+\s*/\s*\d+\s*可用\s*[·・]',
    r'\d+\s*个接口本次自动降级',
    r'^查看(?:原帖|原文|原页|更多)',
    r'\d+\s*条入库\s*[·・]',
    r'^数据缺口[：:]',
    r'^本次判定[：:]',
    r'^窗口内无匹配快讯的小节[：:]',
    r'^数据日期\s.*[·・]\s*采集状态',
    r'^采集状态\s',
    # —— 构建兜底占位：产物缺失时页面上那段「怎么把它跑出来」的运行提示 ——
    r'^今日未获取',
    r'[—-]\s*今日未获取\s*$',
    r'待构建注入',
    r'^运行\s*python3?\s',
    r'^⚠️\s*未读到',
    r'^本栏(?:不再回填|不回填)',
    r'未找到\s*[\w./-]+\.json',
    r'恢复命令[：:]',
)
STATUS_LINE_RES = tuple(re.compile(pattern) for pattern in STATUS_LINE_PATTERNS)


def is_status_text(line):
    """一行文字是不是「状态类文字」（抓取中、STATUS、核验时间……）。"""
    line = line.strip()
    if not line:
        return False
    if line in STATUS_CHIP_TEXT:
        return True
    return any(pattern.search(line) for pattern in STATUS_LINE_RES)


# 纯文本专属：源页面里这些状态是行内 <span>，抽成文字后粘在正文行尾
# （「…回复正文 未提供站内热度指标 · 2026-11-06T11:45:19Z」），只能切尾巴。
# EPUB 那边它们是独立节点，strip_status 直接整块删掉，不需要这条规则。
STATUS_TAIL_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r'\s*(?:未提供站内热度指标|热度未知)\s*[·・]\s*[^·・]{0,40}$',
))


def strip_status_suffix(line):
    """切掉行尾的状态尾巴：状态标签（「❄️ 02. 雪球网未获取」）与行内状态片段。"""
    for pattern in STATUS_TAIL_PATTERNS:
        line = pattern.sub('', line)
    for chip in STATUS_CHIP_TEXT:
        if line.endswith(chip) and len(line) > len(chip) + 3:
            return line[:-len(chip)].rstrip(' ·・|')
    return line


def filter_status_lines(lines):
    """只按状态类规则过滤，其余原样保留（顺序、空行都不动）。"""
    return [strip_status_suffix(line) for line in lines if not is_status_text(line)]


def filter_content_lines(lines):
    """逐行判定：状态类文字、站点外壳、乱码都不要；正文里的站壳分段顺手切掉。

    先整行判、再切分段：反过来的话，切掉页脚分段之后一行里剩下的站壳比例变小，
    「一半导航 + 一半正文」的首页抓取就会漏过去（EPUB 那条路径就是整行判的）。
    """
    kept = []
    for line in lines:
        if is_status_text(line):
            continue
        line = clean_text(strip_status_suffix(line))
        if not line or is_garbled(line) or is_chrome_text(line):
            continue
        line = trim_chrome_text(line)
        if not line or is_garbled(line) or is_chrome_text(line):
            continue
        kept.append(line)
    return kept


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


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.found = False
        self.depth = 0
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if not self.inside:
            if tag == 'main':
                self.inside = self.found = True
                self.depth = 1
            return
        if tag not in VOID:
            self.depth += 1
        if self.skip:
            if tag not in VOID:
                self.skip += 1
            return
        classes = set(attrs.get('class', '').split())
        element_id = attrs.get('id', '')
        role = '@' + attrs['role'].strip().lower() if attrs.get('role') else ''
        if (tag in SKIP or tag in BODY_DROP_TAGS
                or classes.intersection(STATUS_DROP_CLASSES)
                or classes.intersection(BODY_DROP_CLASSES)
                or ('#' + element_id) in STATUS_DROP_IDS
                or ('#' + element_id) in BODY_DROP_IDS
                or role in BODY_DROP_ROLES):
            if tag not in VOID:
                self.skip = 1
            return
        if tag in BLOCKS:
            self.parts.append('\n')
        elif tag in {'td', 'th'}:
            self.parts.append('  |  ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.inside or tag in VOID:
            return
        if self.skip:
            self.skip -= 1
        elif tag in BLOCKS:
            self.parts.append('\n')
        self.depth -= 1
        if self.depth == 0:
            self.inside = False

    def handle_data(self, data):
        if self.inside and not self.skip:
            self.parts.append(data)

    def unfiltered_lines(self):
        """按源顺序的可见文字行，还没过滤状态类文字。"""
        text = ''.join(self.parts).replace('\xa0', ' ')
        return [line for line in
                (re.sub(r'[ \t\r\f\v]+', ' ', raw).strip() for raw in text.splitlines())
                if line]

    def text(self):
        return '\n'.join(filter_content_lines(self.unfiltered_lines()))


def extract_text(source):
    parser = ReportParser()
    parser.feed(source)
    lines = parser.unfiltered_lines()
    # 占位符检查跑在过滤前：模板里的 {{FETCH_STATUS}} 正好落在会被删掉的状态块里，
    # 过滤后再查就查不出「拿的是仓库模板而不是构建产物」了。
    if not parser.found or not lines:
        raise ValueError('未找到非空的 <main> 报告正文')
    if re.search(r'\{\{[A-Z][A-Z0-9_]*\}\}', '\n'.join(lines)):
        raise ValueError('检测到未替换的模板占位符；请使用 004 构建后的报告，而不是仓库模板')
    text = '\n'.join(filter_content_lines(lines))
    if not text:
        raise ValueError('过滤状态文字、站壳与乱码后正文为空；请检查源页面是不是构建产物')
    # 过滤之后还剩这么多乱码，说明源页面本身的编码就坏了：不生成，别把垃圾推给设备。
    if garbled_ratio(text) >= GARBLED_DOC_RATIO:
        raise ValueError('正文里乱码过多（源页面编码可能有问题），拒绝生成')
    return text


def render(source, generated_at=None):
    text = extract_text(source)
    generated_at = generated_at or datetime.now(timezone.utc).isoformat(timespec='seconds')
    payload = json.dumps({'text': text, 'generated': generated_at}, ensure_ascii=True).replace('<', '\\u003c')
    template = Path(__file__).with_name('viewer.html').read_text(encoding='utf-8')
    return template.replace('/* REPORT_DATA */', payload)


def read_source(source):
    if source.startswith(('https://', 'http://')):
        req = Request(source, headers={'User-Agent': '004-kindle-report/1.0'})
        with urlopen(req, timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
    else:
        with open(source, 'rb') as f:
            raw = f.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('报告超过 20 MiB，拒绝处理')
    return decode_source(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='004 构建后的 report.html 本地路径或公开 URL')
    parser.add_argument('-o', '--output', required=True, help='生成文件路径')
    parser.add_argument('--format', choices=('html', 'text'), default='html', help='text 用于 KOReader 插件')
    args = parser.parse_args()
    try:
        source = read_source(args.source)
        if args.format == 'text':
            result = '章鱼 AI·舆情004\n转换时间（UTC）：' + datetime.now(timezone.utc).isoformat(timespec='seconds') + '\n数据时间以正文为准\n\n' + extract_text(source) + '\n'
            if len(result.encode('utf-8')) > 2 * 1024 * 1024:
                raise ValueError('正文超过设备端 2 MiB 限制')
        else:
            result = render(source)
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Do not destroy the last good output if parsing or writing fails.
        temporary = target.with_name(target.name + '.tmp')
        temporary.write_text(result, encoding='utf-8')
        temporary.replace(target)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f'生成失败：{exc}', file=sys.stderr)
        return 1
    print(f'已生成 {target}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
