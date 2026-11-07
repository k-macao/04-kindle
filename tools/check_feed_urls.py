#!/usr/bin/env python3
"""把 site/news.xml 的每个对外地址挨个取一遍，报告状态、字节数与条目数。

    python tools/check_feed_urls.py                    # 取一遍，打成一张表
    python tools/check_feed_urls.py --write site/feed-mirrors.json

为什么需要这个：同一份 feed 挂在几个不同的主机家族上（GitHub Pages、raw、Release 资产、
jsDelivr 各端点），哪条线路在设备上通、哪条 404 了，只能一条条试。手工试太慢，所以：

- 列表写在 `FEED_URLS` 里，`--url` 可以临时加一条、`--only` 只测匹配到的几条；
- 每条记录 `status`（HTTP 状态）、`bytes`、`items`（解析出来的条目数）、`error`；
- jsDelivr 的 `@main` 有十来分钟缓存，条目数比线上少属正常，所以只报告不评判；
- 结果可以写成 `site/feed-mirrors.json` 跟着仓库走：出问题时看这份文件就知道
  「是地址错了，还是这台设备的网络不通」。

测试里注入假 fetch，不联网。
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

REPO = "k-macao/04-kindle"
USER_AGENT = "004-kindle-feed-check/1.0"
MAX_BYTES = 4 * 1024 * 1024

# 顺序＝推荐给设备的尝试顺序：先 GitHub 自家，再 jsDelivr 的各端点。
# 同一条 feed 挂在这几个互不相关的主机上，一条不通就换下一条。
FEED_URLS = (
    ("pages", "GitHub Pages",
     f"https://{REPO.split('/')[0]}.github.io/{REPO.split('/')[1]}/site/news.xml"),
    ("raw", "raw.githubusercontent.com",
     f"https://raw.githubusercontent.com/{REPO}/main/site/news.xml"),
    ("release", "GitHub Release 资产",
     f"https://github.com/{REPO}/releases/download/news-feed/news.xml"),
    ("jsdelivr", "jsDelivr（Cloudflare）",
     f"https://cdn.jsdelivr.net/gh/{REPO}@main/site/news.xml"),
    ("jsdelivr-fastly", "jsDelivr（Fastly）",
     f"https://fastly.jsdelivr.net/gh/{REPO}@main/site/news.xml"),
    ("jsdelivr-gcore", "jsDelivr（Gcore）",
     f"https://gcore.jsdelivr.net/gh/{REPO}@main/site/news.xml"),
    ("jsdelivr-testingcf", "jsDelivr（TestingCF）",
     f"https://testingcf.jsdelivr.net/gh/{REPO}@main/site/news.xml"),
)


def http_get(url, timeout=15):
    """取回正文；只要正文，不要 30x 之外的额外处理（urllib 自己跟重定向）。"""
    request = Request(url, headers={"User-Agent": USER_AGENT,
                                    "Accept": "application/rss+xml, application/xml, "
                                              "text/xml;q=0.9, */*;q=0.5"})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("内容超过 4 MiB")
    return payload


def count_items(payload):
    """解析出条目数；根元素不是 rss/feed/RDF 就抛 ValueError（说明这个地址回的不是 feed）。

    只看「能不能解析」不够：GitHub Pages 的 404 页是一段合法 HTML，ElementTree 照样能解析，
    只是条目数为 0——那正是设备上「内容下载失败」时最容易被误判成成功的情形。
    """
    root = ElementTree.fromstring(payload)
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag not in ("rss", "feed", "rdf"):
        raise ValueError(f"根元素是 <{tag}>，不是 rss/atom")
    channel = root.find("channel")
    if channel is not None:
        return len(channel.findall("item"))
    return len([node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "entry"])


def check(url, fetch=None, timeout=15):
    """取一条地址，返回 {url, ok, status, bytes, items, error}。

    `fetch` 传 None 就用 `http_get`——CLI 一路传下来的是 None，别让 None 把默认值顶掉。
    """
    fetch = fetch or http_get
    try:
        payload = fetch(url, timeout)
    except HTTPError as exc:
        return {"url": url, "ok": False, "status": exc.code, "bytes": 0, "items": 0,
                "error": f"HTTP {exc.code}"}
    except (URLError, OSError, ValueError) as exc:
        return {"url": url, "ok": False, "status": 0, "bytes": 0, "items": 0, "error": str(exc)}
    try:
        items = count_items(payload)
    except (ElementTree.ParseError, ValueError) as exc:
        return {"url": url, "ok": False, "status": 200, "bytes": len(payload), "items": 0,
                "error": f"不是 feed：{exc}"}
    if not items:
        return {"url": url, "ok": False, "status": 200, "bytes": len(payload), "items": 0,
                "error": "解析到 0 条（可能是空 feed 或错误页）"}
    return {"url": url, "ok": True, "status": 200, "bytes": len(payload), "items": items,
            "error": ""}


def check_all(urls=FEED_URLS, fetch=None, timeout=15, log=None):
    fetch = fetch or http_get
    results = []
    for key, label, url in urls:
        result = check(url, fetch=fetch, timeout=timeout)
        result.update(key=key, label=label)
        results.append(result)
        if log:
            mark = "✅" if result["ok"] else "⚠️"
            detail = (f"{result['items']} 条 / {result['bytes'] / 1024:.1f} KiB"
                      if result["ok"] else result["error"])
            log(f"  {mark} {label:22} {detail}")
    return results


def summary(results):
    ok = [result for result in results if result["ok"]]
    return {"checked": len(results), "ok": len(ok),
            "urls": [result["url"] for result in ok]}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", default="", help="把结果写成 JSON（例如 site/feed-mirrors.json）")
    ap.add_argument("--url", action="append", default=[], help="额外测一条地址（可重复）")
    ap.add_argument("--only", nargs="*", default=None, help="只测这些 key（见 FEED_URLS）")
    ap.add_argument("--timeout", type=int, default=15, help="单条超时秒数（默认 15）")
    return ap.parse_args(argv)


def main(argv=None, fetch=None):
    args = parse_args(argv)
    urls = list(FEED_URLS)
    if args.only:
        wanted = {key.lower() for key in args.only}
        urls = [item for item in urls if item[0].lower() in wanted]
        if not urls:
            print(f"没有匹配的 key：{', '.join(sorted(wanted))}", file=sys.stderr)
            return 2
    for extra in args.url:
        urls.append((f"custom-{len(urls)}", "自定地址", extra))

    print(f"核对 {len(urls)} 条地址…")
    try:
        results = check_all(urls, fetch=fetch, timeout=args.timeout, log=print)
    except Exception as exc:                      # noqa: BLE001  兜底：崩了也要留下可读结果
        # 脚本自己出错时也要写出一份 JSON：工作流只靠这个文件判断「哪条地址不可用」，
        # 没有文件的话那一步会连带失败，反而看不出真正原因。
        print(f"核对脚本出错：{type(exc).__name__}: {exc}", file=sys.stderr)
        results = [{"key": "internal", "label": "核对脚本", "url": "", "ok": False,
                    "status": 0, "bytes": 0, "items": 0,
                    "error": f"{type(exc).__name__}: {exc}"}]
    stats = summary(results)
    print(f"可用 {stats['ok']}/{stats['checked']} 条")
    if args.write:
        payload = {"checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "ok": stats["ok"], "total": stats["checked"], "results": results}
        target = Path(args.write)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        print(f"结果：{target}")
    return 0 if stats["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
