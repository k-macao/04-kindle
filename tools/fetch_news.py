#!/usr/bin/env python3
"""每天从 RSSHub 路由抓一轮财经新闻，归一化成 news_data.json。

    python tools/fetch_news.py --out news_data.json
    python tools/fetch_news.py --only CLS JIN10 --max-age-hours 24
    RSSHUB_BASE="https://rsshub.app,https://rsshub.rss.tips" python tools/fetch_news.py

为什么单独抓，而不是复用 004 的报告页：报告页每小时才刷、只渲染当次抓到的条目，
而且不区分原生 RSS 与 RSSHub 路由。这里要的是**只含新闻**的每日订阅文档，所以直接
按 tools/news_sources.py 里的路由去 RSSHub 实例取。

实例与回退：

- **先接官方实例 `rsshub.app`**，再挂一串公共镜像按顺序回退；`RSSHUB_BASE` 环境变量
  （逗号分隔）可覆盖，自建实例优先填最前面。
- 开工前对官方实例探一次活（`/healthz`）：它今天对机房 IP 回 403，就别让 23 个源
  轮流撞 403，直接把官方实例挪到队尾当兜底，日志里写清楚为什么（`probe_instances`）。
- 同一实例连续失败达 `--trip-after` 次就不再试它，避免 49 源 × N 实例的 403 风暴
  把一轮抓取拖死。
- 单源失败不影响其它源；全部失败或有效条目少于 `--min-items` 时退出码 1，
  让工作流别推送一份空日报。
- 某个源彻底失败时，顺手用 **RSSHub Radar** 问一次「这个域名该用哪条路由」
  （`{实例}/api/radar/rules/{domain}`，见 tools/radar_discover.py），把建议写进日志与
  `sources[].radar`。只给建议、不自动换源：换哪条要人确认。

源可以走 RSSHub 路由（`route`），也可以直接是站点原生 RSS（`feed`，RSSHub 没有这条路由时）。

输出（news_data.json）只是中间产物，不进 Git：

    {"generated": "...", "items": [{title, link, published, summary, source, group, lang}],
     "sources": [{key, name, group, route, ok, count, instance, error}]}
"""

import argparse
import hashlib
import html as html_mod
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))

import direct_feeds  # noqa: E402  源站直连：多备用方案的第一跳
import news_sources  # noqa: E402
import radar_discover  # noqa: E402  源挂了就去问 Radar 该换哪个路由
from build_epub import is_garbled  # noqa: E402  乱码标题不进日报

# 顺序＝优先级：官方实例第一，然后是 004 里探活过的公共镜像。
# rsshub.app 对机房出口 IP 有时回 403（004 的 CI 记录过），遇到就用 probe_instances
# 把它挪到最后当兜底——「先接官方」和「官方挂了也能出报」两件事不冲突。
OFFICIAL_INSTANCE = "https://rsshub.app"
DEFAULT_INSTANCES = (
    OFFICIAL_INSTANCE,
    "https://rsshub.rss.tips",
    "https://rsshub.isrss.com",
    "https://rsshub.cups.moe",
)
# 每个源按这个顺序试，任何一层成功就停。顺序＝从「离源站最近」到「绕得最远」：
#   direct   源站自己的接口（tools/direct_feeds.py），不经过任何第三方
#   rsshub   RSSHub 官方实例
#   mirror   RSSHub 公共镜像
#   native   站点自己的原生 RSS
#   bridge   RSS-Bridge 公共实例代取（第三方代码库，GitHub 出口 IP 被源站挡时的第三条路）
# bridge 默认不进链：它是别人的公共服务，少打一次是一次；命令行/工作流显式要才加。
DEFAULT_PROVIDERS = ("direct", "rsshub", "mirror", "native")
ALL_PROVIDERS = ("direct", "rsshub", "mirror", "native", "bridge")
# RSS-Bridge 一次代取几条：FeedMerge 每条 feed 最多给 10 条，这里只求「有稿」不求全。
VIA_BY_PROVIDER = {"native": "feed", "direct": "direct", "bridge": "bridge"}
BRIDGE_LIMIT = 20
BRIDGE_MAX_INSTANCES = 2
BRIDGE_MAX_TARGETS = 3

USER_AGENT = "004-kindle-news/1.0"
# 有些路由把日期解析成未来（财新周刊的期号日期会跑到 25 小时后），这类条目会霸占
# 「最新一条」的位置、把整份 feed 的排序带歪。超过这个容差就按「生成时间」记。
FUTURE_TOLERANCE_HOURS = 6
MAX_BYTES = 4 * 1024 * 1024
SUMMARY_MAX = 220
TITLE_MAX = 160
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[\s\u3000]+")


# ---------------------------------------------------------------------------
# 取回与解析
# ---------------------------------------------------------------------------


HEALTH_PATH = "/healthz"


def probe_instance(instance, fetch=None, timeout=8):
    """探一次实例：/healthz 能取回东西就算活。返回 (可用?, 说明)。

    只有 403/429 与网络错误才算「不可用」——404 之类说明这个实例没实现 /healthz，
    不能据此判定它取不到 feed；探活只是省几个必然失败的请求，不是硬门槛。
    """
    fetch = fetch or http_get
    try:
        fetch(instance.rstrip("/") + HEALTH_PATH, timeout)
    except HTTPError as exc:                     # HTTPError 是 URLError 的子类，先接住
        if exc.code in (401, 403, 429):
            return False, f"HTTP {exc.code}"
        return True, f"HTTP {exc.code}（探活路径不可用，按可用处理）"
    except (URLError, OSError, ValueError) as exc:
        return False, str(exc)
    return True, ""


def probe_instances(instances, fetch=None, timeout=8, log=print, notes=None):
    """探活并把不通的实例挪到队尾（不删，留着兜底）。返回新的实例顺序。

    `notes` 给了就把每个实例的探活结果写进去（进 news_data.json → news.json，
    没有日志也能看出官方实例今天通没通）。
    """
    fetch = fetch or http_get
    alive, dead = [], []
    for instance in instances:
        ok, note = probe_instance(instance, fetch=fetch, timeout=timeout)
        if notes is not None:
            notes[instance] = {"ok": ok, "note": note}
        if ok:
            alive.append(instance)
            if note:
                log(f"  · 实例 {instance} 探活：{note}")
        else:
            dead.append(instance)
            log(f"  · 实例 {instance} 探活失败（{note}），排到队尾兜底")
    return alive + dead


def route_url(instance, route):
    """把实例域名和路由拼成 URL；中文频道名（如 21 财经的「热点」）按 UTF-8 编码。"""
    return instance.rstrip("/") + quote(route, safe="/:@!$&'()*+,;=-._~")


def bridge_url(instance, feed_url, limit=BRIDGE_LIMIT):
    """把一条 feed 地址交给 RSS-Bridge 的 FeedMerge 代取（它只做转发，不改内容）。

    这就是「第三条路」：RSSHub 全挂、源站又不给 GitHub 机房 IP 开门时，让第三方机房去取。
    """
    query = urlencode({"action": "display", "bridge": "FeedMerge",
                       "feed_1": feed_url, "limit": limit, "format": "Atom"})
    return instance.rstrip("/") + "/?" + query


def source_candidates(source, instances, providers, dead, trip_after,
                      bridge_instances=()):
    """一个源的所有退路，按 `providers` 的顺序排好。

    每一跳是 {provider, label, url, instance, direct}：direct 那跳没有 URL（由抓取器自己拼）。
    """
    route = source.get("route") or ""
    feed = source.get("feed") or ""
    route_urls = [(instance, route_url(instance, route)) for instance in instances] if route else []
    candidates = []
    for provider in providers:
        if provider == "direct":
            spec = source.get("direct") or ""
            if spec:
                candidates.append({"provider": "direct", "label": f"直连 {spec}",
                                   "url": "", "instance": "", "direct": spec})
        elif provider == "rsshub":
            if route_urls:
                instance, url = route_urls[0]
                # 官方实例连续失败够多次也会暂停：49 个源轮流撞 403 只会把一轮抓取拖死。
                if dead.get(instance, 0) < trip_after:
                    candidates.append({"provider": "rsshub", "label": instance,
                                       "url": url, "instance": instance, "direct": ""})
        elif provider == "mirror":
            for instance, url in route_urls[1:]:
                if dead.get(instance, 0) < trip_after:
                    candidates.append({"provider": "mirror", "label": instance,
                                       "url": url, "instance": instance, "direct": ""})
        elif provider == "native":
            if feed:
                candidates.append({"provider": "native", "label": "native feed",
                                   "url": feed, "instance": "", "direct": ""})
        elif provider == "bridge":
            # 让 RSS-Bridge 代取哪几条：原生 feed、官方实例的路由、以及**最后一个镜像**的路由。
            # 最后那条最要紧——某一轮里官方超时、镜像报 5xx 的时候，让第三方机房再去试一次
            # 镜像地址常常就成了；只包官方地址等于把这条退路浪费掉。
            targets = []
            for url in (feed,
                        *(url for _i, url in route_urls[:1]),
                        *(url for _i, url in route_urls[-1:])):
                if url and url not in targets:
                    targets.append(url)
            for base in list(bridge_instances)[:BRIDGE_MAX_INSTANCES]:
                for target in targets[:BRIDGE_MAX_TARGETS]:
                    label = base.replace("https://", "")
                    candidates.append({"provider": "bridge", "label": f"RSS-Bridge {label}",
                                       "url": bridge_url(base, target), "instance": "",
                                       "direct": ""})
    return candidates


def http_get(url, timeout=15, headers=None):
    merged = {"User-Agent": USER_AGENT,
              "Accept": "application/rss+xml, application/atom+xml, "
                        "application/xml;q=0.9, text/xml;q=0.8"}
    merged.update(headers or {})
    request = Request(url, headers=merged)
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("内容超过 4 MiB，拒绝处理")
    return payload


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(element):
    return html_mod.unescape("".join(element.itertext())) if element is not None else ""


def clean_text(value, limit=None):
    """去掉标签与不可见字符，压缩空白；截断时按字符数（中文标题够用）。"""
    text = TAG_RE.sub(" ", str(value or ""))
    text = html_mod.unescape(text).replace("\xa0", " ")
    text = "".join(char for char in text
                   if not unicodedata.category(char) in ("Cc", "Cf") or char in "\t\n")
    text = SPACE_RE.sub(" ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def parse_published(value):
    """RSS 的 RFC822 与 Atom 的 RFC3339 都认；认不出就返回 None。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        moment = None
    if moment is None:
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _first_link(entry):
    """Atom 的 link 在属性里，RSS 的在文本里；优先 rel=alternate。"""
    fallback = ""
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = child.get("href") or ""
        rel = (child.get("rel") or "alternate").lower()
        if href and rel == "alternate":
            return href.strip()
        fallback = fallback or href.strip() or (child.text or "").strip()
    return fallback


URL_PREFIXES = ("http://", "https://")

# 没有 <link> 的条目只按 guid 造地址：认得的 guid 形状就补真实详情页，认不得就留空。
GUID_URL_RULES = (
    (re.compile(r"^jin10:index:(\d+)$"), "https://flash.jin10.com/detail/{}"),
)


def _is_url(value):
    return str(value or "").strip().lower().startswith(URL_PREFIXES)


def _link_from_guid(guid):
    """金十快讯这类没有 <link> 的源，guid 里就藏着详情页 id。"""
    text = str(guid or "").strip()
    if not text:
        return ""
    if _is_url(text):
        return text
    for pattern, template in GUID_URL_RULES:
        found = pattern.match(text)
        if found:
            return template.format(found.group(1))
    return ""


def _child_text(entry, *names):
    for child in entry:
        if _local(child.tag) in names:
            text = _text(child).strip()
            if text:
                return text
    return ""


def parse_feed(payload, source=None):
    """把 RSS 2.0 / Atom / RDF 报文解析成条目列表（不带时间窗与去重）。"""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ValueError(f"XML 解析失败：{exc}") from exc
    entries = [node for node in root.iter() if _local(node.tag) in ("item", "entry")]
    items = []
    for entry in entries:
        title = clean_text(_child_text(entry, "title"), TITLE_MAX)
        if not title:
            continue
        published = parse_published(_child_text(entry, "pubDate", "published",
                                               "updated", "date", "issued"))
        summary = clean_text(_child_text(entry, "description", "summary",
                                         "content", "encoded"), SUMMARY_MAX)
        if summary and summary.lower() == title.lower():
            summary = ""
        # link 只放真的能用浏览器打开的地址；金十快讯只有 guid，就从 guid 里还原详情页。
        # 注意千万别把 `jin10:index:…` 这种非 URL 写进 <link>：KOReader 的新闻下载器
        # 会拿它当 base_url 去拼相对链接，而缺 <link> 的条目更糟——它直接索引 nil 报错，
        # 整个 feed 的下载就停在那儿（表现就是「讀一半失敗」）。
        link = _first_link(entry).strip()
        if not _is_url(link):
            link = _link_from_guid(_child_text(entry, "guid", "id"))
        items.append({
            "title": title,
            "link": link,
            "published": published.isoformat().replace("+00:00", "Z") if published else "",
            "summary": summary,
            "source": (source or {}).get("key", ""),
            "group": (source or {}).get("group", ""),
            "lang": (source or {}).get("lang", "zh"),
            "_moment": published,
        })
    return items


# ---------------------------------------------------------------------------
# 抓一轮
# ---------------------------------------------------------------------------


def _dedupe_key(title):
    """同一件事常被多家转载：去掉空白与标点后比较，标题相同就算重复。"""
    return re.sub(r"[\s\W_]+", "", title).lower()


def title_hash(title):
    """标题的稳定短指纹，用来跨天记住「这条已经推过了」。"""
    return hashlib.sha1(_dedupe_key(title).encode("utf-8")).hexdigest()[:12]


def load_seen(path, now=None, days=7):
    """读上次推送过的标题指纹；超过 days 天的条目直接忘掉（只防相邻几天重复）。"""
    now = now or datetime.now(timezone.utc)
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    horizon = now - timedelta(days=days)
    seen = {}
    for key, stamp in (payload.get("seen") or {}).items():
        moment = parse_published(str(stamp))
        if moment is not None and moment >= horizon:
            seen[str(key)] = moment.isoformat().replace("+00:00", "Z")
    return seen


def save_seen(path, seen, items, now=None, days=7):
    """把这一轮推送出去的标题也记上，顺手清掉过期的。"""
    now = now or datetime.now(timezone.utc)
    horizon = now - timedelta(days=days)
    merged = dict(seen)
    for item in items:
        merged[title_hash(item["title"])] = item.get("published") or now.isoformat().replace("+00:00", "Z")
    keep = {key: stamp for key, stamp in merged.items()
            if (parse_published(stamp) or now) >= horizon}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"updated": now.isoformat(timespec="seconds"), "seen": keep},
                                 ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return keep


def collect(sources=None, instances=None, timeout=15, max_age_hours=24,
            per_source=12, trip_after=3, now=None, fetch=None, log=print, seen=None,
            radar_base=None, probe=False, probe_notes=None, providers=None,
            bridge_instances=None):
    """抓一轮，返回 (items, source_reports)。fetch(url, timeout) 可注入，便于离线测试。

    `seen` 是之前推过的标题指纹（见 load_seen）：命中的条目不再进今天的日报，
    否则 24 小时时间窗会把昨天的同一批稿子再推一遍。

    `radar_base` 给了就在**某个源彻底失败时**去问 RSSHub Radar「这个域名该用哪条路由」，
    结果只写进日志和报告的 `radar` 字段，不会自动换源——发现的事要人确认。

    `probe` 为真时先探活各实例，把 403 的（如机房 IP 下的 rsshub.app）挪到队尾。
    默认关（库函数保持「给什么实例就用什么」），命令行默认开。

    `providers` 是每个源的退路顺序（见 DEFAULT_PROVIDERS）。库函数默认只走前四跳；
    显式传 "bridge" 才用 RSS-Bridge 公共实例代取——那是别人的公共服务，按需再打。
    """
    fetch = fetch or http_get
    now = now or datetime.now(timezone.utc)
    instances = list(instances or DEFAULT_INSTANCES)
    if probe and len(instances) > 1:
        instances = probe_instances(instances, fetch=fetch, timeout=min(timeout, 8), log=log,
                                    notes=probe_notes)
    sources = list(sources if sources is not None else news_sources.SOURCES)
    chain = tuple(provider for provider in (providers or DEFAULT_PROVIDERS)
                  if provider in ALL_PROVIDERS)
    bridge_hosts = tuple(bridge_instances or news_sources.RSS_BRIDGE_INSTANCES)
    seen_titles = dict(seen or {})
    known = {}
    reports = []
    dead = {}          # instance -> 连续失败次数

    for source in sources:
        key = source["key"]
        feed = source.get("feed") or ""
        # 单源时间窗：政策、哲学类更新稀疏，全局 24 小时会把它们整条滤掉。
        source_horizon = now - timedelta(hours=float(source.get("max_age_hours")
                                                     or max_age_hours))
        report = {"key": key, "name": source["name"], "group": source["group"],
                  "route": source.get("route") or "", "feed": feed,
                  "direct": source.get("direct") or "", "ok": False, "count": 0,
                  "instance": "", "via": "feed" if feed else "instance", "error": "",
                  "provider": "", "url": "", "radar": [],
                  # 每一跳试了什么、为什么不行：源挂了的时候，这一个字段就能说清该补哪一层。
                  "attempts": []}
        errors = []
        candidates = source_candidates(source, instances, chain, dead, trip_after,
                                       bridge_hosts)
        for candidate in candidates:
            instance = candidate["instance"]
            label = candidate["label"]
            try:
                if candidate["direct"]:
                    payload = direct_feeds.fetch_direct(candidate["direct"], source, fetch,
                                                        timeout)
                else:
                    payload = fetch(candidate["url"], timeout)
                items = parse_feed(payload, source)
            except (URLError, OSError, ValueError, ElementTree.ParseError) as exc:
                errors.append(f"{candidate['provider']}/{label}: {exc}")
                report["attempts"].append({"provider": candidate["provider"],
                                           "label": label, "ok": False, "error": str(exc)[:160]})
                if instance:
                    dead[instance] = dead.get(instance, 0) + 1
                continue
            if instance:
                dead[instance] = 0
            future_limit = now + timedelta(hours=FUTURE_TOLERANCE_HOURS)
            fresh = []
            clamped = 0
            for item in items:
                moment = item.pop("_moment")
                if moment is not None and moment < source_horizon:
                    continue
                if is_garbled(item["title"]):
                    continue
                if moment is not None and moment > future_limit:
                    # 时间在未来的条目：内容留着，时间按「现在」记，别让它排到最前面。
                    moment = now
                    item["published"] = now.isoformat().replace("+00:00", "Z")
                    clamped += 1
                item["published"] = item["published"] or ""
                item["_moment"] = moment
                fresh.append(item)
            fresh.sort(key=lambda item: item["_moment"] or now, reverse=True)
            fresh = fresh[:per_source]
            if not fresh:
                span = (f"{float(source.get('max_age_hours') or max_age_hours) / 24:.0f} 天"
                        if source.get("max_age_hours") else f"{max_age_hours:g} 小时")
                errors.append(f"{candidate['provider']}/{label}: 解析出 {len(items)} 条，"
                              f"{span}窗口内 0 条")
                report["attempts"].append({"provider": candidate["provider"], "label": label,
                                           "ok": False,
                                           "error": f"解析出 {len(items)} 条，窗口内 0 条"})
                continue
            taken = 0
            for item in fresh:
                moment = item.pop("_moment")
                identity = _dedupe_key(item["title"])
                if title_hash(item["title"]) in seen_titles:
                    continue
                if identity in known:
                    # 同一件事保留最早发布的那条（转载时间晚，别把旧闻顶上来）。
                    old = known[identity]
                    if old["published"] and moment and old["_moment"] and moment < old["_moment"]:
                        item["_moment"] = moment
                        known[identity] = item
                    continue
                item["_moment"] = moment
                known[identity] = item
                taken += 1
            report.update(ok=True, count=taken, instance=instance or "",
                          # via 保留旧口径（feed / instance / direct / bridge），
                          # provider 才是「哪一层」——老日志和下游脚本都还在读 via。
                          via=VIA_BY_PROVIDER.get(candidate["provider"], "instance"),
                          provider=candidate["provider"], url=candidate["url"])
            report["attempts"].append({"provider": candidate["provider"], "label": label,
                                       "ok": True, "error": ""})
            if clamped:
                report["clamped"] = clamped
                log(f"    · {key}：{clamped} 条时间在未来的条目，已按生成时间记")
            note = "" if taken else "（时间窗内都是已推过或重复的）"
            log(f"  ✅ {key:12} {source['name']:18} {taken:2} 条{note}  ({label})")
            if len(report["attempts"]) > 1:
                # 降级成功也说清楚：前几跳各自为什么不行，别让「换了一层」这件事只藏在代码里。
                log("    ↳ 退路：" + "；".join(f"{a['provider']}={a['error'][:60]}"
                                              for a in report["attempts"][:-1]))
            break
        else:
            report["error"] = "；".join(errors[:3]) or "所有实例都跳过（连续失败过多）"
            hint = ""
            if radar_base and source.get("site"):
                report["radar"] = radar_discover.suggest_routes(
                    source["site"], fetch=fetch, base=radar_base, timeout=min(timeout, 8))
                if report["radar"]:
                    hint = "　Radar 建议：" + " / ".join(report["radar"])
            log(f"  ⚠️ {key:12} {source['name']:18} 未取到  {report['error']}{hint}")
        reports.append(report)

    items = sorted(known.values(), key=lambda item: item["_moment"] or now, reverse=True)
    for item in items:
        item.pop("_moment", None)
    return items, reports


def fetch_news(sources=None, instances=None, timeout=15, max_age_hours=24,
               per_source=12, trip_after=3, now=None, log=print, fetch=None, seen=None,
               radar_base=None, probe=False, providers=None, bridge_instances=None):
    """collect() 的便捷包装：返回可写盘的 JSON 结构。

    `fetch` 可注入（测试里用假报文），默认真联网。
    """
    now = now or datetime.now(timezone.utc)
    probes = {}
    items, reports = collect(sources=sources, instances=instances, timeout=timeout,
                             max_age_hours=max_age_hours, per_source=per_source,
                             trip_after=trip_after, now=now, log=log, fetch=fetch,
                             seen=seen, radar_base=radar_base, probe=probe,
                             probe_notes=probes, providers=providers,
                             bridge_instances=bridge_instances)
    # 哪个实例真的供了稿：写进清单，一眼能看出官方实例这轮通没通。
    used = {}
    for report in reports:
        if report.get("ok") and report.get("instance"):
            used[report["instance"]] = used.get(report["instance"], 0) + report.get("count", 0)
    return {
        "generated": now.isoformat(timespec="seconds"),
        "providers": list(providers or DEFAULT_PROVIDERS),
        "instances": list(instances or DEFAULT_INSTANCES),
        "instances_used": used,
        "instances_probe": probes,
        "sources": reports,
        "items": items,
    }


def build_health(payload, providers=None):
    """把这一轮「每个源在哪一层活下来的」整理成一张表。

    源挂了的时候，报错信息通常只说明最后一跳；这张表把每一跳都留着，下次该补哪一层一目了然。
    """
    sources = payload.get("sources") or []
    ok = [report for report in sources if report.get("ok")]
    by_provider = {}
    for report in ok:
        name = report.get("provider") or "unknown"
        by_provider[name] = by_provider.get(name, 0) + 1
    return {
        "checked": payload.get("generated", ""),
        "providers": list(providers or payload.get("providers") or DEFAULT_PROVIDERS),
        "summary": {"ok": len(ok), "total": len(sources),
                    "items": len(payload.get("items") or []),
                    "by_provider": by_provider},
        "sources": [{"key": report.get("key"), "name": report.get("name"),
                     "group": report.get("group"), "ok": bool(report.get("ok")),
                     "provider": report.get("provider") or "",
                     "count": report.get("count", 0),
                     "error": report.get("error") or "",
                     "attempts": report.get("attempts") or []}
                    for report in sources],
    }


def write_health(payload, path, providers=None):
    """写 site/source-health.json（进 Git；工作流把它贴在步骤摘要里）。"""
    health = build_health(payload, providers=providers)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return health


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="news_data.json", help="输出 JSON（默认 news_data.json）")
    ap.add_argument("--only", nargs="*", default=None,
                    help="只抓这几个源（key，见 tools/news_sources.py）")
    ap.add_argument("--instances", default=None,
                    help="RSSHub 实例，逗号分隔（默认读 RSSHUB_BASE，再不济用内置一串）")
    ap.add_argument("--timeout", type=int, default=15, help="单请求超时秒数（默认 15）")
    ap.add_argument("--max-age-hours", type=float, default=24,
                    help="只收最近这么多小时的条目（默认 24，正好一天一轮）")
    ap.add_argument("--per-source", type=int, default=12,
                    help="单源最多收几条（默认 12）")
    ap.add_argument("--trip-after", type=int, default=3,
                    help="同一实例连续失败几次就不再试（默认 3）")
    ap.add_argument("--min-items", type=int, default=10,
                    help="少于这么多条就算失败，退出码 1（默认 10）")
    ap.add_argument("--seen", default="site/news_seen.json",
                    help="已推送标题指纹（默认 site/news_seen.json）")
    ap.add_argument("--seen-days", type=float, default=7,
                    help="指纹记多久（默认 7 天）")
    ap.add_argument("--no-seen", action="store_true", help="不读也不写指纹（重抓全量）")
    ap.add_argument("--radar-base", default=None,
                    help="RSSHub Radar 的 API 实例（默认用第一个抓取实例）")
    ap.add_argument("--no-radar", action="store_true",
                    help="源失败时不去问 Radar 该换哪条路由")
    ap.add_argument("--no-probe", action="store_true",
                    help="不先探活实例（默认为官方实例 rsshub.app 探一次，403 就排到队尾）")
    ap.add_argument("--providers", default=",".join(ALL_PROVIDERS),
                    help="每个源的退路顺序，逗号分隔（默认 " + ",".join(ALL_PROVIDERS) +
                         "）：direct 源站直连、rsshub 官方实例、mirror 镜像、"
                         "native 原生 RSS、bridge RSS-Bridge 代取")
    ap.add_argument("--bridge-instances", default=None,
                    help="RSS-Bridge 公共实例，逗号分隔（默认用 news_sources 里内置的三个）")
    ap.add_argument("--health", default="",
                    help="把「每个源在哪一层活下来」写成 JSON（工作流打卡 site/source-health.json）")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    problems = news_sources.catalog_errors()
    if problems:
        for problem in problems:
            print(f"目录有问题：{problem}", file=sys.stderr)
        return 2
    sources = news_sources.SOURCES
    if args.only:
        wanted = {key.upper() for key in args.only}
        sources = [source for source in sources if source["key"].upper() in wanted]
        if not sources:
            print(f"没有匹配的源：{', '.join(sorted(wanted))}", file=sys.stderr)
            return 2
    raw_instances = args.instances
    if raw_instances is None:
        import os
        raw_instances = os.environ.get("RSSHUB_BASE", "")
    instances = [part.strip() for part in str(raw_instances).split(",") if part.strip()]
    instances = instances or list(DEFAULT_INSTANCES)

    providers = [part.strip() for part in str(args.providers).split(",") if part.strip()]
    unknown = [name for name in providers if name not in ALL_PROVIDERS]
    if unknown or not providers:
        print(f"--providers 里有不认识的一层：{', '.join(unknown) or '(空)'}；"
              f"可选 {', '.join(ALL_PROVIDERS)}", file=sys.stderr)
        return 2
    bridge_instances = [part.strip() for part in str(args.bridge_instances or "").split(",")
                        if part.strip()] or None
    radar_base = None if args.no_radar else (args.radar_base or instances[0])
    seen = {} if args.no_seen else load_seen(args.seen, days=args.seen_days)
    print(f"抓取 {len(sources)} 个源（退路 {' → '.join(providers)}；实例 {len(instances)} 个，"
          f"已推过 {len(seen)} 条不再收）…")
    if instances and instances[0].rstrip("/") == OFFICIAL_INSTANCE:
        print(f"官方实例优先：{OFFICIAL_INSTANCE}（探活失败会自动回退到镜像）")
    payload = fetch_news(sources=sources, instances=instances, timeout=args.timeout,
                         max_age_hours=args.max_age_hours, per_source=args.per_source,
                         trip_after=args.trip_after, seen=seen, radar_base=radar_base,
                         probe=not args.no_probe, providers=providers,
                         bridge_instances=bridge_instances)
    ok_sources = sum(1 for report in payload["sources"] if report["ok"])
    total = len(payload["items"])
    target = Path(args.out)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(f"完成：{total} 条 / {ok_sources} 个源可用 → {target}")
    if args.health:
        health = write_health(payload, args.health, providers=providers)
        summary = health["summary"]
        by_provider = " / ".join(f"{name} {count}" for name, count in
                                 sorted(summary["by_provider"].items(), key=lambda pair: -pair[1]))
        print(f"源健康表 → {args.health}（{summary['ok']}/{summary['total']} 可用"
              + (f"；{by_provider}" if by_provider else "") + "）")
    used = payload.get("instances_used") or {}
    if used:
        print("实例供稿：" + " / ".join(f"{name} {count} 条"
                                       for name, count in sorted(used.items(),
                                                                 key=lambda pair: -pair[1])))
    suggestions = [report for report in payload["sources"] if report.get("radar")]
    if suggestions:
        print("Radar 给了几条替换建议（确认后改 tools/news_sources.py）：")
        for report in suggestions:
            print(f"  · {report['key']}（{report['name']}）→ {' / '.join(report['radar'])}")
    if not args.no_seen and total:
        save_seen(args.seen, seen, payload["items"], days=args.seen_days)
    if total < args.min_items:
        print(f"有效条目 {total} 条，少于下限 {args.min_items}：这次不生成日报", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
