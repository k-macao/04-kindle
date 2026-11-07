#!/usr/bin/env python3
"""源站直连：跳过所有第三方聚合器，直接抓站点自己给网页用的接口，合成一份 RSS。

为什么要这一层：RSSHub 再好也是**单一项目、单点依赖**——它挂了、路由改名了、或者机房出口
IP 被源站 403 了，整条链就断。下面这几个接口是站点自己页面在用的公开接口（RSSHub 的对应
路由抓的也是它们），我们直接取，等于把「多备用方案」的第一跳放回源站本身。

约定：

- 每个抓取器签名统一为 `抓取器(source, fetch, timeout, arg) -> bytes`，`arg` 是
  `news_sources.SOURCES` 里 `direct = "名字:参数"` 冒号后面那段；
- 返回的是**一份 RSS 2.0 字节流**，不是解析好的条目。这样后面 parse_feed、时间窗、去重、
  中英配对、排版一条都不用改，直连源和 RSSHub 源走完全相同的下游逻辑；
- 接口返回的字段名按上游实际报文写（金十 `data[].data.content`、财联社 `data.roll_data[]`、
  华尔街见闻 `data.items[]`），字段缺失就跳过这一条，不猜；
- 时间统一转成 UTC：接口给的是 epoch 秒就按秒，给的是没带时区的本地时间就按 +08:00。

这几个接口随时可能加签名、改字段。抓取器抛异常没关系——`fetch_news.collect()` 会把它当成
「这一层失败」，接着走 RSSHub 官方、镜像、原生 RSS、RSS-Bridge，日志里留一行原因。
"""

import hashlib
import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import quote_plus
from xml.sax.saxutils import escape

BEIJING = timezone(timedelta(hours=8))
# 每个接口一次抓多少条；后面还有 per_source 截断，这里给足再让下游按时间窗筛。
LIMIT = 40
INVALID_XML_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[\s\u3000]+")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _supports_headers(fetch):
    """注入的 fetch 可能是老签名 fetch(url, timeout)；有 headers 参数就带上。"""
    try:
        return "headers" in inspect.signature(fetch).parameters
    except (TypeError, ValueError):
        return False


def _get(fetch, url, timeout, headers=None):
    if headers and _supports_headers(fetch):
        return fetch(url, timeout, headers=headers)
    return fetch(url, timeout)


def _json(payload):
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    try:
        return json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"接口没回 JSON：{exc}") from exc


def _text(value):
    """接口里的 HTML 摘要压成一行纯文本；标题、摘要都过这一道。"""
    text = TAG_RE.sub(" ", str(value or ""))
    return SPACE_RE.sub(" ", text).strip()


def _moment(value):
    """接口时间 → UTC datetime。认 epoch 秒/毫秒、带时区的 ISO、以及「本地时间当 +08:00」。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number > 1e11:          # 毫秒
            number /= 1000
        try:
            return datetime.fromtimestamp(number, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)   # 站点页面显示的就是北京时间
    return moment.astimezone(timezone.utc)


def _xml(value):
    return escape(INVALID_XML_RE.sub("", str(value or "")))


def _rss(title, link, items):
    """把归一化后的条目拼成一份 RSS 2.0（中文标题、UTC 时间、绝对链接）。"""
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<rss version="2.0">',
             "  <channel>",
             f"    <title>{_xml(title)}</title>",
             f"    <link>{_xml(link)}</link>",
             "    <description>源站直连（不经过任何聚合器）</description>",
             "    <language>zh-cn</language>"]
    for item in items:
        lines.append("    <item>")
        lines.append(f"      <title>{_xml(item['title'])}</title>")
        if item.get("link"):
            lines.append(f"      <link>{_xml(item['link'])}</link>")
        guid = item.get("guid") or item.get("link") or item["title"]
        permalink = "true" if str(guid).startswith("http") else "false"
        lines.append(f'      <guid isPermaLink="{permalink}">{_xml(guid)}</guid>')
        if item.get("moment") is not None:
            stamp = format_datetime(item["moment"].astimezone(timezone.utc))
            lines.append(f"      <pubDate>{stamp}</pubDate>")
        if item.get("description"):
            lines.append(f"      <description>{_xml(item['description'])}</description>")
        lines.append("    </item>")
    lines.extend(["  </channel>", "</rss>", ""])
    return "\n".join(lines).encode("utf-8")


def _clean_link(value):
    text = str(value or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else ""


# ---------------------------------------------------------------------------
# 各家接口
# ---------------------------------------------------------------------------

def jin10(source, fetch, timeout=15, arg=""):
    """金十快讯：GET flash-api.jin10.com/get_flash_list（RSSHub /jin10 抓的就是它）。

    报文：{"data": [{"id", "time", "type", "important", "data": {"content", "pic", "link",
    "vip_title"}}]}，type == 1 是广告/图片卡，跳过。
    """
    header = {"x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0", "Accept": "application/json"}
    payload = _get(fetch, "https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1",
                   timeout, headers=header)
    rows = _json(payload).get("data") or []
    items = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") == 1:
            continue
        body = row.get("data") or {}
        content = _text(body.get("content"))
        title = _text(body.get("vip_title"))
        match = re.match(r"^【(.+?)】\s*(.*)$", content, re.S)
        if match:
            title = title or _text(match.group(1))
            content = _text(match.group(2))
        title = title or content[:60]
        if not title:
            continue
        ident = str(row.get("id") or "").strip()
        link = _clean_link(body.get("link")) or _clean_link(
            f"https://flash.jin10.com/detail/{ident}" if ident else "")
        items.append({"title": title, "link": link, "description": content,
                      "guid": f"jin10:index:{ident}" if ident else link,
                      "moment": _moment(row.get("time"))})
        if len(items) >= LIMIT:
            break
    if not items:
        raise ValueError("金十接口没有可用的快讯")
    return _rss("金十数据 · 快讯（直连）", "https://www.jin10.com/", items)


def cls_telegraph(source, fetch, timeout=15, arg=""):
    """财联社电报：GET www.cls.cn/api/cache，参数要按官方算法签名。

    sign = md5(sha1(按 key 排序后的查询串))，与 RSSHub lib/routes/cls/utils.ts 一致。
    报文：{"data": {"roll_data": [{"id", "title", "content", "ctime", "shareurl", "images"}]}}
    """
    params = {"appName": "CailianpressWeb", "name": "telegraph", "os": "web", "sv": "8.7.9"}
    query = "&".join(f"{quote_plus(key)}={quote_plus(value)}"
                     for key, value in sorted(params.items()))
    sign = hashlib.md5(hashlib.sha1(query.encode()).hexdigest().encode()).hexdigest()
    url = f"https://www.cls.cn/api/cache?{query}&sign={sign}"
    rows = ((_json(_get(fetch, url, timeout, headers={"Accept": "application/json"}))
             .get("data") or {}).get("roll_data") or [])
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = _text(row.get("content"))
        title = _text(row.get("title")) or content[:60]
        if not title:
            continue
        ident = str(row.get("id") or "").strip()
        link = _clean_link(row.get("shareurl")) or _clean_link(
            f"https://www.cls.cn/detail/{ident}" if ident else "")
        items.append({"title": title, "link": link, "description": content,
                      "guid": link or f"cls:{ident}", "moment": _moment(row.get("ctime"))})
        if len(items) >= LIMIT:
            break
    if not items:
        raise ValueError("财联社接口没有可用的电报")
    return _rss("财联社 · 电报（直连）", "https://www.cls.cn/telegraph", items)


def wallstreetcn_live(source, fetch, timeout=15, arg=""):
    """华尔街见闻快讯：GET api-one.wallstcn.com/apiv1/content/lives。

    `arg` 是频道：global（要闻）/ a-stock（A 股）/ us-stock / hk-stock / forex / commodity /
    financing，与 RSSHub /wallstreetcn/live/:category 的取值一致。
    报文：{"data": {"items": [{"uri", "title", "content", "content_text", "display_time",
    "score", "images"}]}}，display_time 是 epoch 秒。
    """
    channel = (arg or "global").strip() or "global"
    url = (f"https://api-one.wallstcn.com/apiv1/content/lives"
           f"?channel={quote_plus(channel)}-channel&limit={LIMIT}")
    rows = ((_json(_get(fetch, url, timeout, headers={"Accept": "application/json"}))
             .get("data") or {}).get("items") or [])
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = _text(row.get("content") or row.get("content_text"))
        title = _text(row.get("title")) or content[:60]
        if not title:
            continue
        link = _clean_link(row.get("uri"))
        items.append({"title": title, "link": link, "description": content,
                      "guid": link or title, "moment": _moment(row.get("display_time"))})
        if len(items) >= LIMIT:
            break
    if not items:
        raise ValueError(f"华尔街见闻接口（{channel}）没有可用的快讯")
    return _rss(f"华尔街见闻 · {channel}（直连）", "https://wallstreetcn.com/live", items)


DIRECT = {
    "jin10": jin10,
    "cls": cls_telegraph,
    "wallstreetcn": wallstreetcn_live,
}


def names():
    """支持的直连抓取器名字，给目录自检用。"""
    return tuple(sorted(DIRECT))


def split(spec):
    """`"wallstreetcn:a-stock"` → ("wallstreetcn", "a-stock")。"""
    name, _, arg = str(spec or "").partition(":")
    return name.strip(), arg.strip()


def fetch_direct(spec, source, fetch, timeout=15):
    name, arg = split(spec)
    handler = DIRECT.get(name)
    if handler is None:
        raise ValueError(f"未知的直连抓取器：{spec!r}（可选 {', '.join(names())}）")
    return handler(source, fetch, timeout, arg)
