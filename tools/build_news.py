#!/usr/bin/env python3
"""把 fetch_news.py 抓到的新闻排成每日订阅文档：EPUB + 清单 + 纯文本。

    python tools/build_news.py                      # 读 news_data.json，输出到 site/
    python tools/build_news.py --data news_data.json --out site --min-items 10

产出：

    site/news.epub   单文件 EPUB 3，按「快讯 / 深度 / 全球」分节，条目＝标题 + 来源与
                     北京时间 + 一句摘要
    site/news.json   清单：{"generated", "doc", "title", "chars", "bytes", "sections",
                            "items", "sources_ok", "sources_total", "sources_failed",
                            "data_time", ...}
    news.txt         同一份内容的纯文本（设备文本模式可以直接读它）

清单里的 `doc` 与 `generated` 是插件那套 EPUB 流程要的字段：`generated` 变了才会重下，
`doc` 是相对清单 URL 的 EPUB 路径。`data_time` 取最新一条新闻的时间（kind=item），
比「文件生成时间」更能说明这份日报新不新。

一条新闻都没有、条目少于 `--min-items`、或者正文里乱码过多时**退出码 1 且不覆盖旧产物**，
免得把空日报推给设备。
"""

import argparse
import html as html_mod
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import news_sources  # noqa: E402
from build_epub import (BEIJING, garbled_ratio, normalize, parse_data_time,  # noqa: E402
                        write_epub)

TITLE = "004 新闻订阅"
MAX_ITEMS = 150
CN_NUMERALS = "一二三四五六七八九十"
GARBLED_DOC_RATIO = 0.02

NEWS_STYLE_CSS = """/* 004 每日新闻订阅 - 电子墨水版式 */
html, body {
  background: #ffffff;
  color: #000000;
  margin: 0;
  padding: 0;
}
body {
  font-size: 1em;
  line-height: 1.55;
  padding: 0.6em 0.7em 1.2em;
}
h1 {
  font-size: 1.5em;
  margin: 0 0 0.2em;
}
h2 {
  font-size: 1.15em;
  margin: 1.4em 0 0.5em;
  padding-bottom: 0.25em;
  border-bottom: 2px solid #000000;
  page-break-after: avoid;
}
article {
  margin: 0 0 0.85em;
  padding: 0 0 0.6em;
  border-bottom: 1px dotted #999999;
}
h3 {
  font-size: 1.02em;
  line-height: 1.45;
  margin: 0 0 0.2em;
}
p.meta {
  font-size: 0.78em;
  color: #444444;
  margin: 0 0 0.25em;
}
p.sum {
  font-size: 0.9em;
  margin: 0;
}
p.lead {
  font-size: 0.82em;
  color: #333333;
  margin: 0 0 0.8em;
}
"""


def beijing_stamp(value):
    """把 ISO 时间转成「11-07 06:12」这种北京时间，认不出就原样给。"""
    moment = parse_data_time(value)
    if moment is None:
        return (value or "").replace("T", " ").replace("Z", " UTC")
    return moment.astimezone(BEIJING).strftime("%m-%d %H:%M")


def load_items(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload.get("items") or []
    reports = payload.get("sources") or []
    return payload, items, reports


def prune(items, max_items=MAX_ITEMS):
    """去掉空标题、乱码标题与重复标题，按时间新→旧排序，再截到上限。"""
    def sort_key(item):
        moment = parse_data_time(item.get("published"))
        return moment.timestamp() if moment else 0.0

    seen = set()
    kept = []
    for item in sorted(items, key=sort_key, reverse=True):
        title = normalize(str(item.get("title") or ""))
        if len(title) < 4:
            continue
        identity = re.sub(r"[\s\W_]+", "", title).lower()
        if identity in seen:
            continue
        seen.add(identity)
        kept.append({**item, "title": title})
        if len(kept) >= max_items:
            break
    return kept


def grouped(items):
    """按目录里的分组顺序分组，空组不出现；序号按实际出现的组顺排。"""
    buckets = {key: [] for key, _label in news_sources.GROUPS}
    for item in items:
        group = item.get("group") or "flash"
        buckets.setdefault(group, []).append(item)
    rows = [(key, label, buckets.get(key) or []) for key, label in news_sources.GROUPS
            if buckets.get(key)]
    numbered = []
    for index, (key, label, items_in_group) in enumerate(rows):
        number = CN_NUMERALS[index] if index < len(CN_NUMERALS) else str(index + 1)
        numbered.append((key, f"{number}、{label}", items_in_group))
    return numbered


def render_body(items, generated_text, sources_ok, sources_total):
    """新闻正文的 XHTML 片段：一节一个分组，一条新闻一个 article。"""
    parts = [f"<h1>{html_mod.escape(TITLE)}</h1>",
             f'<p class="lead">{html_mod.escape(generated_text)}'
             f'　共 {len(items)} 条 · {sources_ok}/{sources_total} 个源可用。'
             "每天 07:00（北京时间）更新。</p>"]
    for key, label, rows in grouped(items):
        parts.append(f'<h2 id="g-{key}">{html_mod.escape(label)}</h2>')
        for item in rows:
            stamp = beijing_stamp(item.get("published"))
            source = html_mod.escape(str(item.get("source_name") or item.get("source") or ""))
            meta = " · ".join(part for part in (source, stamp) if part)
            parts.append('<article class="news">')
            parts.append(f"<h3>{html_mod.escape(item['title'])}</h3>")
            if meta:
                parts.append(f'<p class="meta">{meta}</p>')
            summary = normalize(str(item.get("summary") or ""))
            if summary and summary != item["title"]:
                parts.append(f'<p class="sum">{html_mod.escape(summary)}</p>')
            parts.append("</article>")
    return "\n".join(parts)


def render_text(items, generated_text, sources_ok, sources_total):
    lines = [TITLE, generated_text,
             f"共 {len(items)} 条 · {sources_ok}/{sources_total} 个源可用", ""]
    for _key, label, rows in grouped(items):
        lines.append(label)
        for item in rows:
            stamp = beijing_stamp(item.get("published"))
            source = str(item.get("source_name") or item.get("source") or "")
            lines.append(f"[{stamp}] {item['title']}　{source}".rstrip())
            summary = normalize(str(item.get("summary") or ""))
            if summary and summary != item["title"]:
                lines.append("　" + summary)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build(payload, out_dir, min_items=10, max_items=MAX_ITEMS, now=None):
    """生成三个产物，返回 (manifest, paths)；条目不足时抛 ValueError。"""
    now = now or datetime.now(timezone.utc)
    raw_items = payload.get("items") or []
    reports = payload.get("sources") or []
    items = prune(raw_items, max_items)
    if len(items) < min_items:
        raise ValueError(f"有效条目 {len(items)} 条，少于下限 {min_items}")

    # 名字用目录里的中文名（抓取时只记了 key，渲染前补上）。
    names = {source["key"]: source["name"] for source in news_sources.SOURCES}
    for item in items:
        item["source_name"] = names.get(item.get("source") or "", item.get("source") or "")

    sources_ok = sum(1 for report in reports if report.get("ok"))
    sources_total = len(reports) or len(news_sources.SOURCES)
    stamp = now.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")
    generated_text = f"{stamp} 北京时间生成"
    body = render_body(items, generated_text, sources_ok, sources_total)
    text = render_text(items, generated_text, sources_ok, sources_total)

    if garbled_ratio(re.sub(r"<[^>]+>", " ", body)) >= GARBLED_DOC_RATIO:
        raise ValueError("新闻正文里乱码过多（源内容编码可能有问题），拒绝生成")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sections = [(f"g-{key}", label) for key, label, _rows in grouped(items)]
    data_time = items[0].get("published") or ""
    epub = out / "news.epub"
    words = write_epub(epub, body, title=TITLE, modified=now, source="RSSHub",
                       sections=sections, data_time=data_time or None,
                       data_time_kind="item" if data_time else None,
                       style=NEWS_STYLE_CSS)
    failed = [report.get("key") for report in reports if not report.get("ok")]
    manifest = {
        "generated": now.isoformat(timespec="seconds"),
        "doc": "news.epub",
        "title": TITLE,
        "chars": words,
        "bytes": epub.stat().st_size,
        "sections": len(sections),
        "items": len(items),
        "sources_ok": sources_ok,
        "sources_total": sources_total,
        # 哪些源这轮没取到：写进提交过的清单里，第二天就能看出是偶发还是常死。
        "sources_failed": failed,
        "data_time": data_time,
        "data_time_kind": "item" if data_time else "",
        "source": "RSSHub",
    }
    (out / "news.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    text_path = out / "news.txt"
    text_path.write_text(text, encoding="utf-8")
    return manifest, {"epub": epub, "manifest": out / "news.json", "text": text_path}


def write_failure_manifest(payload, out_dir, reason):
    """抓不到东西时也留个说明，方便排查；不覆盖已有 EPUB。"""
    manifest = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "doc": "", "title": TITLE, "items": 0, "error": reason}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "news.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="news_data.json", help="fetch_news.py 的输出（默认 news_data.json）")
    ap.add_argument("--out", default="site", help="输出目录（默认 site）")
    ap.add_argument("--min-items", type=int, default=10, help="少于这么多条就不生成（默认 10）")
    ap.add_argument("--max-items", type=int, default=MAX_ITEMS, help=f"最多收几条（默认 {MAX_ITEMS}）")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        payload, _items, _reports = load_items(args.data)
        manifest, paths = build(payload, args.out, args.min_items, args.max_items)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    print(f"完成：{manifest['items']} 条 / {manifest['sources_ok']} 个源，"
          f"{manifest['bytes'] / 1024:.0f} KiB，数据时间 {manifest['data_time'] or '未知'}，"
          f"{paths['epub']}")
    print(f"清单：{paths['manifest']}　纯文本：{paths['text']}")
    print("插件里 Display mode 选「News subscription」，新闻清单 URL 指向 site/news.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
