#!/usr/bin/env python3
"""把当天抓到的新闻合成一个聚合 RSS 2.0：site/news.xml。

    python tools/build_feed.py --data news_data.json --out site

同一个 `news_data.json` 已经生成了 EPUB / 清单 / 纯文本；这里再出一份**标准的 RSS**，
方便任何阅读器（KOReader 的新闻下载器、NetNewsWire、Feedly、Reeder…）直接订阅，
不用装插件、也不用管 EPUB 那一套清单。

约定：

- 一条新闻 = `<item>`：标题、原文链接、发布时间（RFC822）、分类、来源（RSS 2.0 的
  `<source url="站点">名称</source>`）和一句摘要；
- **英文条目中英并列**：`tools/translate_news.py` 补过 `title_zh` 的，标题写成
  `English Title｜中文标题`，摘要有译文时写「中文：… 原文：…」，读者不必点进原文；
- **每条新闻带 AI 线索分析**：`analysis` 字段按「线索→事实→推断→影响→验证」写入 RSS 正文；
- `guid` 用原文链接（`isPermaLink="true"`），没有链接的用标题指纹的
  `urn:004:news:<hash>`——同一个链接在读者端不会重复出现；
- `lastBuildDate` 是这次生成的时间，`ttl` 建议阅读器每小时回来看一次；
- 去掉 XML 1.0 不允许的控制字符再转义，正文里出现 `&`、`<`、引号都不会破坏文件；
- 生成后立刻用 XML 解析器自检一遍，解析不过就**不写文件**，免得发布一份坏 feed；
- 条目少于 `--min-items` 时退出码 1，不覆盖上一份——和日报的规矩一致。
"""

import argparse
import html as html_mod
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_news  # noqa: E402  去重/排序与日报共用同一套
import fetch_news  # noqa: E402  标题指纹
import news_sources  # noqa: E402

FEED_TITLE = "004 每日财经新闻聚合"
DEFAULT_SELF_URL = "https://raw.githubusercontent.com/k-macao/04-kindle/main/site/news.xml"
DEFAULT_SITE_URL = "https://github.com/k-macao/04-kindle"
TTL_MINUTES = 60
FEED_MAX_BYTES = 4 * 1024 * 1024

# XML 1.0 不允许的字符：控制符（\t \n \r 除外）、代理区、以及两个非字符码位。
INVALID_XML_RE = re.compile(
    "["
    "\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f"
    "\ud800-\udfff\ufffe\uffff"
    "]")


def xml_text(value):
    """转义成元素内容；控制字符先删掉，否则整份 feed 会解析失败。"""
    text = INVALID_XML_RE.sub("", str(value or ""))
    return escape(text)


def xml_attr(value):
    return escape(INVALID_XML_RE.sub("", str(value or "")), {'"': "&quot;"})


def _absolute(value):
    """只认 http(s) 绝对地址；`jin10:index:…` 这种「伪链接」一律当没有。"""
    text = str(value or "").strip()
    lowered = text.lower()
    return text if lowered.startswith(("http://", "https://")) else ""


def rfc822(moment):
    """RSS 只认 RFC 822 的时间，统一写 UTC。"""
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def jsdelivr_url(url):
    """同一个文件在 jsDelivr 上的地址（给「raw 不通」的网络用）；算不出来就返回空串。"""
    parts = str(url or "").split("/")
    if len(parts) >= 7 and parts[2] == "raw.githubusercontent.com":
        owner, repo, ref = parts[3], parts[4], parts[5]
        return f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{ref}/" + "/".join(parts[6:])
    return ""


def group_label(key):
    return news_sources.GROUP_LABELS.get(key, key or "")


def item_xml(item, names, sites, desc_limit=None, fallback_link=DEFAULT_SITE_URL):
    """一条新闻对应一个 <item>；缺什么就省什么，不写空标签。"""
    # 英文条目把中文接在标题后面：阅读器的列表页只显示标题，译文必须在这里才看得见。
    title_zh = build_news.normalize(str(item.get("title_zh") or ""))
    shown = f"{item['title']}｜中文：{title_zh}" if title_zh else item["title"]
    # 标题里绝不能留换行：设备端用 <title>…</title> 抠书名，跨行就抠不到（见 check_device_fields）。
    shown = " ".join(str(shown).split())
    title = xml_text(shown)
    key = item.get("source") or ""
    name = names.get(key) or key
    site = sites.get(key) or ""
    # 每条都必须有绝对的 http(s) <link>：KOReader 的新闻下载器对每条都要
    # `feed.link._attr`，缺 <link> 时它索引 nil 当场报错，整个 feed 停在中途
    # （「讀一半失敗」就是这么来的）。没有原文地址的条目退回站点首页，绝不写假地址。
    link = _absolute(item.get("link")) or _absolute("https://" + site if site else "") or fallback_link
    guid_value = _absolute(item.get("link")) or ("urn:004:news:" + fetch_news.title_hash(item["title"]))
    permalink = "true" if _absolute(item.get("link")) else "false"
    parts = ["    <item>",
             f"      <title>{title}</title>",
             f"      <link>{xml_text(link)}</link>",
             f'      <guid isPermaLink="{permalink}">{xml_text(guid_value)}</guid>']
    moment = fetch_news.parse_published(item.get("published") or "")
    stamp = rfc822(moment)
    if stamp:
        parts.append(f"      <pubDate>{stamp}</pubDate>")
    label = group_label(item.get("group"))
    if label:
        parts.append(f"      <category>{xml_text(label)}</category>")
    if name:
        source_open = (f'      <source url="{xml_attr("https://" + site)}">' if site
                       else "      <source>")
        parts.append(f"{source_open}{xml_text(name)}</source>")
    summary = build_news.normalize(str(item.get("summary") or ""))
    summary_zh = build_news.normalize(str(item.get("summary_zh") or ""))
    description = ""
    if summary_zh and summary_zh != title_zh:
        description = "中文：" + summary_zh
        if summary and summary != item["title"]:
            description += "\n原文：" + summary
    elif summary and summary != item["title"]:
        description = summary
    # 精简版只截 RSS 摘要，不截 AI 线索链；验证点不能因小文件模式而丢失。
    if description and desc_limit and len(description) > desc_limit:
        description = description[:desc_limit].rstrip() + "…"
    analysis = str(item.get("analysis") or "").strip()
    if analysis:
        # 把摘要和五个线索环节逐段排版成 HTML；外层 XML 转义后，阅读器会在解析
        # <description> 时还原这些段落标签，KOReader 打开条目时就不会挤成一整行。
        analysis = " ".join(line.strip() for line in analysis.splitlines() if line.strip())
        clue_parts = [part.strip() for part in re.split(
            r"(?=(?:线索|事实|推断|影响|验证)：)", analysis) if part.strip()]
        blocks = ([description] if description else []) + ["AI 线索分析"] + clue_parts
        html_description = "".join(
            f"<p>{html_mod.escape(block, quote=False)}</p>" for block in blocks)
        parts.append(f"      <description>{xml_text(html_description)}</description>")
    elif description and description != shown:
        parts.append(f"      <description>{xml_text(description)}</description>")
    parts.append("    </item>")
    return "\n".join(parts)


def render(items, now, title=FEED_TITLE, self_url=DEFAULT_SELF_URL, site_url=DEFAULT_SITE_URL,
           ttl=TTL_MINUTES, desc_limit=None):
    names = {source["key"]: source["name"] for source in news_sources.SOURCES}
    sites = {source["key"]: source.get("site") or "" for source in news_sources.SOURCES}
    stamp = rfc822(now)
    description = (f"每天 07:00（北京时间）从 RSSHub 抓取并合并的财经、政治与哲学思想新闻，"
                   f"英文条目附中文译文，本次 {len(items)} 条。")
    head = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{xml_text(title)}</title>",
        f"    <link>{xml_text(site_url)}</link>",
        f'    <atom:link href="{xml_attr(self_url)}" rel="self" type="application/rss+xml"/>',
        f"    <description>{xml_text(description)}</description>",
        "    <language>zh-cn</language>",
        f"    <lastBuildDate>{stamp}</lastBuildDate>",
        "    <generator>04-kindle tools/build_feed.py</generator>",
        f"    <ttl>{int(ttl)}</ttl>",
    ]
    body = [item_xml(item, names, sites, desc_limit=desc_limit, fallback_link=site_url)
            for item in items]
    return "\n".join(head + body + ["  </channel>", "</rss>", ""])


ANALYSIS_REQUIRED_LABELS = ("AI 线索分析", "线索：", "事实：", "推断：", "影响：", "验证：")


def check_analysis_fields(parsed_items):
    """确保发布的每条 RSS description 都包含标题与完整线索链。"""
    for index, item in enumerate(parsed_items, start=1):
        description = item.findtext("description") or ""
        missing = [label for label in ANALYSIS_REQUIRED_LABELS if label not in description]
        if missing:
            title = item.findtext("title") or "(无标题)"
            raise ValueError(f"第 {index} 条缺少 AI 线索分析标签 {', '.join(missing)}：{title[:40]}")
    return True


def check_device_fields(parsed_items):
    """设备侧（KOReader 的新闻下载器）读这份 feed 时的硬要求，缺一条就整轮停在中途。

    - 每条都要有 `<link>`，而且是 http(s) 绝对地址：它拿 `feed.link` 去拼相对链接，
      缺 `<link>` 会直接索引 nil 报错；
    - 标题不能为空、不能带换行：它从 `%1` 里抠 `<title>…</title>` 当 EPUB 的
      `<dc:title>`，抠不到就是 nil，`string.format` 当场抛错；
    - `guid` 必须有，阅读器靠它去重。
    """
    for index, item in enumerate(parsed_items, start=1):
        link = (item.findtext("link") or "").strip()
        if not _absolute(link):
            raise ValueError(f"第 {index} 条没有绝对的 <link>：{link!r}（设备会停在这里）")
        title = item.findtext("title") or ""
        if not title.strip():
            raise ValueError(f"第 {index} 条的 <title> 是空的")
        if "\n" in title or "\r" in title:
            raise ValueError(f"第 {index} 条的 <title> 里有换行")
        if not (item.findtext("guid") or "").strip():
            raise ValueError(f"第 {index} 条没有 <guid>")
    return True


def build(payload, out_dir, min_items=10, max_items=build_news.MAX_ITEMS, now=None,
          title=FEED_TITLE, self_url=DEFAULT_SELF_URL, site_url=DEFAULT_SITE_URL,
          filename="news.xml", desc_limit=None):
    """生成 site/news.xml，返回 (info, path)。条目不足或 XML 不自洽时抛 ValueError。"""
    now = now or datetime.now(timezone.utc)
    items = build_news.prune(payload.get("items") or [], max_items)
    if len(items) < min_items:
        raise ValueError(f"有效条目 {len(items)} 条，少于下限 {min_items}")
    xml = render(items, now, title=title, self_url=self_url, site_url=site_url,
                 desc_limit=desc_limit)
    if len(xml.encode("utf-8")) > FEED_MAX_BYTES:
        raise ValueError(f"feed 超过 {FEED_MAX_BYTES // 1024} KiB，阅读器可能拒收")

    # 写完之前先解析一遍：宁可这次不发布，也不要推一份坏 XML。
    try:
        root = ElementTree.fromstring(xml.encode("utf-8"))
    except ElementTree.ParseError as exc:
        raise ValueError(f"生成的 XML 不自洽：{exc}") from exc
    parsed_items = root.findall("./channel/item")
    if len(parsed_items) != len(items):
        raise ValueError(f"XML 条目数不符：写出 {len(parsed_items)}，应有 {len(items)}")
    check_device_fields(parsed_items)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / filename
    target.write_text(xml, encoding="utf-8")
    newest = fetch_news.parse_published(items[0].get("published") or "")
    reports = payload.get("sources") or []
    info = {
        "file": target.name,
        "items": len(items),
        "sources_ok": sum(1 for report in reports if report.get("ok")),
        "sources_total": len(reports),
        # 失败源连同报错一起返回：工作流只出 xml、不写 news.json，日志是唯一的诊断入口。
        "sources_failed": [(report.get("key"), (report.get("error") or "")[:120])
                           for report in reports if not report.get("ok")],
        "zh": sum(1 for item in items if build_news.normalize(str(item.get("title_zh") or ""))),
        "bytes": target.stat().st_size,
        "built": rfc822(now),
        "newest": rfc822(newest),
        "sources": len({item.get("source") for item in items if item.get("source")}),
    }
    return info, target


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="news_data.json", help="fetch_news.py 的输出（默认 news_data.json）")
    ap.add_argument("--out", default="site", help="输出目录（默认 site）")
    ap.add_argument("--min-items", type=int, default=10, help="少于这么多条就不生成（默认 10）")
    ap.add_argument("--max-items", type=int, default=build_news.MAX_ITEMS,
                    help=f"最多收几条（默认 {build_news.MAX_ITEMS}）")
    ap.add_argument("--title", default=FEED_TITLE, help="channel 标题")
    ap.add_argument("--self-url", default=DEFAULT_SELF_URL, help="这份 feed 自己的公开地址")
    ap.add_argument("--site-url", default=DEFAULT_SITE_URL, help="站点主页（channel/link）")
    ap.add_argument("--filename", default="news.xml",
                    help="输出文件名（默认 news.xml；精简版写 news-lite.xml）")
    ap.add_argument("--desc-limit", type=int, default=None,
                    help="摘要最多保留多少字（默认不限；精简版用 140）")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        payload = json.loads(Path(args.data).read_text(encoding="utf-8"))
        info, path = build(payload, args.out, min_items=args.min_items,
                           max_items=args.max_items, title=args.title,
                           self_url=args.self_url, site_url=args.site_url,
                           filename=args.filename, desc_limit=args.desc_limit)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    print(f"完成：聚合 RSS {info['items']} 条（其中 {info['zh']} 条附中文译文）/ "
          f"{info['sources']} 个源供稿 / {info['bytes'] / 1024:.0f} KiB → {path}")
    if info["sources_total"] and info["sources_failed"]:
        print(f"源：{info['sources_ok']}/{info['sources_total']} 可用，失败 "
              f"{len(info['sources_failed'])} 条：")
        for key, error in info["sources_failed"]:
            print(f"  · {key}: {error}")
    print(f"订阅地址：{args.self_url}")
    mirror = jsdelivr_url(args.self_url)
    if mirror:
        print(f"同文件的 jsDelivr 镜像（raw 不通时用）：{mirror}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
