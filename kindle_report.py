#!/usr/bin/env python3
"""Turn a *built* 004 report into a self-contained, paginated webpage."""
import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen

MAX_BYTES = 20 * 1024 * 1024
BLOCKS = {'p', 'div', 'section', 'article', 'h1', 'h2', 'h3', 'h4', 'li', 'tr', 'br', 'hr', 'table'}
SKIP = {'script', 'style', 'noscript', 'button', 'svg', 'nav'}
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
    """丢掉状态行，其余原样保留（顺序、空行都不动）。"""
    return [strip_status_suffix(line) for line in lines if not is_status_text(line)]


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
        if (tag in SKIP or classes.intersection(STATUS_DROP_CLASSES)
                or ('#' + element_id) in STATUS_DROP_IDS):
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
        return '\n'.join(line for line in filter_status_lines(self.unfiltered_lines()) if line)


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
    text = '\n'.join(line for line in filter_status_lines(lines) if line)
    if not text:
        raise ValueError('过滤状态类文字后正文为空；请检查源页面是不是构建产物')
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
    return raw.decode('utf-8-sig')


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
