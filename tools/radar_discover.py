#!/usr/bin/env python3
"""找不到 RSSHub 路由时，用 RSSHub Radar 的规则去找源。

RSSHub Radar 是 RSSHub 官方的浏览器扩展：打开一个站点，它告诉你「这个站有没有 RSS、
RSSHub 上对应的路由是什么」。它的规则不是另写的，而是 RSSHub 每条路由的 `radar` 字段，
由实例的 API 汇总成「域名 → 子域 → 规则」：

    GET {instance}/api/radar/rules                 全部规则
    GET {instance}/api/radar/rules/{domain}        单个域名（省流量，推荐）

响应形状（lib/api/radar/rules/utils.ts 生成）：

    {"wallstreetcn.com": {"_name": "华尔街见闻",
                          "www": [{"title": "实时快讯", "target": "/wallstreetcn/live/:category?",
                                   "source": ["/live/global", "/"],
                                   "docs": "https://docs.rsshub.app/routes/wallstreetcn"}],
                          ".":   [...]}}

`target` 就是可以拼到实例后面的路由（`:param` 是占位参数），`source` 是站点上的对应路径。

用法：

    python tools/radar_discover.py https://wallstreetcn.com/           # 查一个站
    python tools/radar_discover.py https://www.hexun.com/ --json       # 机器可读
    python tools/radar_discover.py https://www.hexun.com/ --emit-source # 直接给可粘贴的源条目

查找顺序（与扩展一致，**先规则、后原生**）：

1. Radar 规则：`www.hexun.com` → 先找 `hexun.com` 下的 `www` 子域规则，没有再退到 `"."`；
2. 没有规则时，抓这个页面找 `<link rel="alternate" type="application/rss+xml|atom+xml">`
   ——站点自己的原生 RSS。没有 RSSHub 路由的站，往往靠这一条。

发现结果只做提议，不会自动写进目录：路由带 `:param` 时要人工确认默认值，
原生 feed 也要看一眼是不是全站/频道流。确认后把 `--emit-source` 打出来的条目粘进
`tools/news_sources.py` 的 `SOURCES`，跑 `tests/test_news.py` 即完成加源。
"""

import argparse
import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

USER_AGENT = "004-kindle-radar/1.0"
DEFAULT_RADAR_BASE = "https://rsshub.app"
DEFAULT_RULES_PATH = ""
MAX_BYTES = 4 * 1024 * 1024

# 常见的多段公共后缀：域名候选不能退到这一层（`10jqka.com.cn` 是站，`com.cn` 不是）。
PUBLIC_SUFFIXES = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.hk", "com.tw", "com.mo", "com.sg", "com.au", "com.br",
    "co.uk", "co.jp", "co.kr", "co.nz", "co.in", "co.za",
}
FEED_TYPES = ("rss", "atom", "feed+json", "json")


# ---------------------------------------------------------------------------
# 域名
# ---------------------------------------------------------------------------


def hostname(url):
    """取出主机名，`www.` 保留——Radar 按子域分键（`www` 与 `"."` 是两组规则）。"""
    text = (url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "https://" + text
    return (urlsplit(text).hostname or "").lower()


def domain_candidates(host):
    """从最具体到最宽松的域名候选：`a.b.caixin.com` → 它自己、`b.caixin.com`、`caixin.com`。"""
    host = (host or "").lower().strip(".")
    if not host:
        return []
    labels = host.split(".")
    out = [host]
    for index in range(1, len(labels) - 1):
        suffix = ".".join(labels[index:])
        if suffix in PUBLIC_SUFFIXES or len(labels[index:]) < 2:
            continue
        out.append(suffix)
    return out


def subdomain_of(host, domain):
    """`www.caixin.com` 相对 `caixin.com` 的子域是 `www`；裸域是 `"."`（Radar 的写法）。"""
    if not host or host == domain:
        return "."
    if host.endswith("." + domain):
        return host[: -(len(domain) + 1)]
    return "."


# ---------------------------------------------------------------------------
# Radar 规则
# ---------------------------------------------------------------------------


def http_get(url, timeout=15):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("内容超过 4 MiB，拒绝处理")
    return payload


def load_rules(source, fetch=None, timeout=15):
    """读 Radar 规则：`source` 可以是 URL、本地文件，或已经是 dict。"""
    if isinstance(source, dict):
        return source
    if not source:
        return {}
    text = str(source)
    if text.startswith("http://") or text.startswith("https://"):
        payload = (fetch or http_get)(text, timeout)
    else:
        payload = Path(text).read_bytes()
    try:
        data = json.loads(payload)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def rules_url(domain, base=DEFAULT_RADAR_BASE):
    return f"{base.rstrip('/')}/api/radar/rules/{domain}"


def _entries(node, subdomain):
    """取一个域名节点下某个子域的规则，`"."` 作为兜底。"""
    if not isinstance(node, dict):
        return []
    entries = node.get(subdomain)
    if entries is None and subdomain != ".":
        entries = node.get(".")
    if entries is None:
        # 有些部署/版本会把规则直接摊在域名下（没有子域一层）。
        entries = [value for key, value in node.items() if key != "_name" and isinstance(value, list)]
    flat = []
    for entry in entries if isinstance(entries, list) else []:
        flat.extend(entry if isinstance(entry, list) else [entry])
    return [entry for entry in flat if isinstance(entry, dict)]


def domain_rules(payload, domain):
    """单域名接口返回的可能是域名节点本身，也可能仍然包一层域名——统一成 `{domain: node}`。"""
    if not isinstance(payload, dict) or not payload:
        return {}
    node = payload.get(domain)
    if isinstance(node, dict):
        return payload
    return {domain: payload}


def radar_routes(rules, site_url):
    """按 Radar 的匹配顺序找这个站点的路由；找不到返回空列表。"""
    host = hostname(site_url)
    if not host:
        return []
    for candidate in domain_candidates(host):
        node = (rules or {}).get(candidate)
        if not isinstance(node, dict):
            continue
        for query in dict.fromkeys([subdomain_of(host, candidate), "."]):
            entries = _entries(node, query)
            if entries:
                return [{"domain": candidate, "subdomain": query,
                         "site_name": node.get("_name") or "",
                         **normalize_entry(entry)} for entry in entries]
    return []


def normalize_entry(entry):
    target = entry.get("target") or ""
    sources = entry.get("source") or []
    if isinstance(sources, str):
        sources = [sources]
    return {
        "title": entry.get("title") or entry.get("name") or "",
        "target": target,
        "source": [str(item) for item in sources if item],
        "docs": entry.get("docs") or "",
    }


def load_domain_rules(domain, fetch=None, base=DEFAULT_RADAR_BASE, timeout=8):
    """只为一个域名问一次 API（省流量）；拿不到就返回空 dict。"""
    if not domain:
        return {}
    try:
        payload = load_rules(rules_url(domain, base), fetch=fetch, timeout=timeout)
    except (URLError, OSError, ValueError):
        return {}
    return domain_rules(payload, domain)


def registrable_domain(site_url):
    """API 要的是可注册域名（`wallstreetcn.com`），不是 `www.wallstreetcn.com`。"""
    return (domain_candidates(hostname(site_url)) or [""])[-1]


def suggest_routes(site_url, fetch=None, base=DEFAULT_RADAR_BASE, timeout=8):
    """抓取失败时的旁注：这个域名在 Radar 里对应哪条路由。异常一律当作「没有建议」。"""
    domain = registrable_domain(site_url) or None
    rules = load_domain_rules(domain, fetch=fetch, base=base, timeout=timeout)
    entries = radar_routes(rules, site_url)
    return [f"{entry['target']}（{entry['title']}）" for entry in entries if entry["target"]]


# ---------------------------------------------------------------------------
# 原生 RSS：Radar 在没有规则时的兜底检测
# ---------------------------------------------------------------------------


class _FeedLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "link":
            return
        attributes = {str(key).lower(): (value or "") for key, value in attrs}
        rel = attributes.get("rel", "").lower().split()
        kind = attributes.get("type", "").lower()
        if "alternate" not in rel or not any(token in kind for token in FEED_TYPES):
            return
        href = attributes.get("href", "").strip()
        if href:
            self.links.append({"href": href, "title": attributes.get("title", ""), "type": kind})


def native_feeds(page, page_url):
    """从 HTML 里挑出 `<link rel=alternate>` 声明的 RSS/Atom/JSON feed。"""
    parser = _FeedLinks()
    try:
        parser.feed(page.decode("utf-8", "replace") if isinstance(page, bytes) else str(page))
    except Exception:                      # HTMLParser 极少抛错，但别让它挡住发现流程
        return []
    seen, feeds = set(), []
    for link in parser.links:
        absolute = urljoin(page_url, link["href"])
        if absolute in seen:
            continue
        seen.add(absolute)
        feeds.append({"feed": absolute,
                      "title": link["title"] or urlsplit(absolute).path or absolute,
                      "type": link["type"]})
    return feeds


def discover(site_url, rules=None, fetch=None, base=DEFAULT_RADAR_BASE, timeout=15):
    """先查 Radar 规则，再找原生 RSS；返回 {site, host, domain, method, routes, feeds}。"""
    fetch = fetch or http_get
    host = hostname(site_url)
    domain = (domain_candidates(host) or [""])[-1]
    if rules is None:
        rules = load_domain_rules(domain, fetch=fetch, base=base, timeout=timeout)
    routes = radar_routes(rules, site_url)
    if routes:
        return {"site": site_url, "host": host, "domain": domain,
                "method": "radar", "routes": routes, "feeds": []}
    feeds = []
    try:
        page = fetch(site_url if "//" in site_url else "https://" + site_url, timeout)
        feeds = native_feeds(page, site_url if "//" in site_url else "https://" + site_url)
    except (URLError, OSError, ValueError):
        feeds = []
    return {"site": site_url, "host": host, "domain": domain,
            "method": "native" if feeds else "none", "routes": [], "feeds": feeds}


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def source_snippet(result, key, group="depth", lang="zh"):
    """把发现结果变成 `tools/news_sources.py` 里的一条（要人再改 name / group / note）。"""
    if result.get("routes"):
        route = result["routes"][0]
        return {"key": key, "name": route["title"] or result.get("domain") or key,
                "group": group, "lang": lang, "route": route["target"],
                "note": f"Radar 发现：{result['site']}（{route['title']}），source {', '.join(route['source']) or '—'}"}
    if result.get("feeds"):
        feed = result["feeds"][0]
        return {"key": key, "name": feed["title"] or result.get("domain") or key,
                "group": group, "lang": lang, "feed": feed["feed"],
                "note": f"Radar 发现：{result['site']} 的原生 RSS（没有 RSSHub 路由）"}
    return {}


def describe(result):
    lines = [f"{result['site']}  →  {result['host']}（域名 {result['domain']}）"]
    if result["method"] == "radar":
        lines.append("  Radar 规则命中：")
        for route in result["routes"]:
            lines.append(f"    target  {route['target']}")
            if route["title"]:
                lines.append(f"    title   {route['title']}")
            if route["source"]:
                lines.append(f"    source  {', '.join(route['source'])}")
            if route["docs"]:
                lines.append(f"    docs    {route['docs']}")
            if ":" in route["target"]:
                lines.append("    注意    路由里有 :param 占位，加源前先定下默认值")
    elif result["method"] == "native":
        lines.append("  没有 Radar 规则，但站点自带 RSS：")
        for feed in result["feeds"]:
            lines.append(f"    {feed['feed']}  ({feed['type']} · {feed['title']})")
    else:
        lines.append("  既没有 Radar 规则，页面里也没声明 RSS —— 这个站暂时没法接。")
    return "\n".join(lines)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sites", nargs="+", help="站点地址，如 https://www.hexun.com/")
    ap.add_argument("--rules", default=DEFAULT_RULES_PATH,
                    help="Radar 规则：实例 API 地址或本地 JSON（默认逐域名请求实例 API）")
    ap.add_argument("--base", default=DEFAULT_RADAR_BASE,
                    help=f"Radar API 所在实例（默认 {DEFAULT_RADAR_BASE}）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--emit-source", action="store_true", help="打印可粘贴的 SOURCES 条目")
    ap.add_argument("--group", default="depth", help="条目默认分组（默认 depth）")
    ap.add_argument("--timeout", type=int, default=15)
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    rules = None
    if args.rules:
        rules = load_rules(args.rules, timeout=args.timeout)
        if not rules:
            print(f"读不到 Radar 规则：{args.rules}", file=sys.stderr)
            return 1
    results = [discover(site, rules=rules, base=args.base, timeout=args.timeout) for site in args.sites]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for index, result in enumerate(results):
        print(describe(result))
        if args.emit_source:
            snippet = source_snippet(result, key=f"NEW{index + 1}", group=args.group)
            print("  可粘贴到 SOURCES：" if snippet else "  （无可粘贴条目）")
            if snippet:
                print("    " + json.dumps(snippet, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
