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
        if tag in SKIP or classes.intersection({'push-wrap', 'filter-row'}):
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

    def text(self):
        text = ''.join(self.parts).replace('\xa0', ' ')
        lines = [re.sub(r'[ \t\r\f\v]+', ' ', line).strip() for line in text.splitlines()]
        return '\n'.join(line for line in lines if line)


def extract_text(source):
    parser = ReportParser()
    parser.feed(source)
    text = parser.text()
    if not parser.found or not text:
        raise ValueError('未找到非空的 <main> 报告正文')
    if re.search(r'\{\{[A-Z][A-Z0-9_]*\}\}', text):
        raise ValueError('检测到未替换的模板占位符；请使用 004 构建后的报告，而不是仓库模板')
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
