#!/usr/bin/env python3
"""把 site/news*.xml 的每个对外地址挨个取一遍，报告状态、字节数与条目数。

    python tools/check_feed_urls.py                       # 全部地址，打一张表
    python tools/check_feed_urls.py --write site/feed-mirrors.json
    python tools/check_feed_urls.py --only raw            # 只测 raw 那几个
    python tools/check_feed_urls.py --url https://x/y.xml # 临时加一条

为什么要这个：同一份 feed 挂在几个互不相关的主机上（GitHub Pages、raw、Release 资产、
jsDelivr 各端点），设备上「內容下載失敗」时，得先分清是**地址坏了**还是**这台设备的线路/证书
不行**。所以除了状态码，还复现一遍 KOReader 新聞下載器自己的判定（`epubdownloadbackend.lua`）：

- `code` 是 4xx/5xx → 失败；
- 有 `content-length` 但**实际字节数不等** → 失败（"Incomplete content received"）；
- `Transfer-Encoding: chunked` → LuaSocket 会把分块头算进正文，等于喂了坏 XML；
- `Content-Encoding: gzip/br` → LuaSocket 不解压；
- 连接/握手超时（默认 10 秒、总时长 30 秒）→ 失败。

另外记下 TLS 签发者：旧 Kindle 的 CA 库认不出 Let's Encrypt 时，同一个文件换个签发者就能过。
结果可以写成 `site/feed-mirrors.json` 跟着仓库走——出问题时看这份文件比翻设备日志快。

测试里注入假 fetch，不联网。
"""

import argparse
import json
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

REPO = "k-macao/04-kindle"
USER_AGENT = "004-kindle-feed-check/1.0"
MAX_BYTES = 4 * 1024 * 1024

# 同一份 feed 的三种体积：完整版、精简版（给慢设备）、连通性测试版（几 KB）。
FILES = ("news.xml", "news-lite.xml", "news-test.xml")

# 顺序＝推荐给设备的尝试顺序：先 GitHub 自家（DigiCert），再 jsDelivr 的各端点。
HOSTS = (
    ("pages", "GitHub Pages", "https://k-macao.github.io/04-kindle/site/{file}"),
    ("raw", "raw.githubusercontent.com",
     f"https://raw.githubusercontent.com/{REPO}/main/site/{{file}}"),
    ("release", "GitHub Release 资产",
     f"https://github.com/{REPO}/releases/download/news-feed/{{file}}"),
    ("jsdelivr", "jsDelivr（Cloudflare）",
     f"https://cdn.jsdelivr.net/gh/{REPO}@main/site/{{file}}"),
    ("jsdelivr-fastly", "jsDelivr（Fastly）",
     f"https://fastly.jsdelivr.net/gh/{REPO}@main/site/{{file}}"),
    ("jsdelivr-gcore", "jsDelivr（Gcore）",
     f"https://gcore.jsdelivr.net/gh/{REPO}@main/site/{{file}}"),
    ("jsdelivr-testingcf", "jsDelivr（TestingCF）",
     f"https://testingcf.jsdelivr.net/gh/{REPO}@main/site/{{file}}"),
)


def feed_urls(files=FILES, hosts=HOSTS):
    """(key, 显示名, 地址)；key 形如 `raw/news.xml`，`--only raw` 能按前缀过滤。"""
    return [(f"{key}/{name}", f"{label} · {name}", template.format(file=name))
            for key, label, template in hosts for name in files]


def tls_issuer(url, timeout=10):
    """TLS 证书的签发机构；旧设备的 CA 库认不认这个，多半就看它。取不到就返回空串。"""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return ""
    try:
        context = ssl.create_default_context()
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=parts.hostname) as tls:
                cert = tls.getpeercert() or {}
    except (OSError, ValueError, ssl.SSLError):
        return ""
    for rdn in cert.get("issuer", ()):
        for key, value in rdn:
            if key == "organizationName":
                return value
    return ""


def http_get(url, timeout=15):
    """取回 (正文, 元信息)。元信息里带上 KOReader 判定需要的那几个响应头。"""
    started = time.time()
    request = Request(url, headers={"User-Agent": USER_AGENT,
                                    "Accept": "application/rss+xml, application/xml, "
                                              "text/xml;q=0.9, */*;q=0.5"})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_BYTES + 1)
        status = response.status
        headers = {key.lower(): value for key, value in response.headers.items()}
    if len(payload) > MAX_BYTES:
        raise ValueError("内容超过 4 MiB，LuaSocket 那边也扛不住")
    return payload, {
        "status": status,
        "headers": headers,
        "content_length": headers.get("content-length"),
        "content_encoding": headers.get("content-encoding", ""),
        "transfer_encoding": headers.get("transfer-encoding", ""),
        "content_type": headers.get("content-type", ""),
        "seconds": round(time.time() - started, 3),
        "tls_issuer": tls_issuer(url, timeout),
    }


def count_items(payload):
    """解析出条目数；根元素不是 rss/feed/RDF 就抛 ValueError（说明这个地址回的不是 feed）。

    只看「能不能解析」不够：404 页也是一段合法 HTML，ElementTree 照样能解析，只是条目数为 0
    ——那正是设备上「內容下載失敗」时最容易被误判成成功的情形。
    """
    root = ElementTree.fromstring(payload)
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag not in ("rss", "feed", "rdf"):
        raise ValueError(f"根元素是 <{tag}>，不是 rss/atom")
    channel = root.find("channel")
    if channel is not None:
        return len(channel.findall("item"))
    return len([node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "entry"])


def koreader_check(payload, meta):
    """按 KOReader 新聞下載器的判定复现一遍。

    设备上那句「內容下載失敗」看不出所以然，这里把它的四条判据逐条对一遍，直接给出原因。
    """
    notes = []
    length = meta.get("content_length")
    transfer = (meta.get("transfer_encoding") or "").lower()
    encoding = (meta.get("content_encoding") or "").lower()

    if "chunked" in transfer:
        notes.append("Transfer-Encoding: chunked（LuaSocket 会把分块头算进正文）")
    if encoding and encoding != "identity":
        notes.append(f"Content-Encoding: {encoding}（LuaSocket 不解压）")
    if length is not None:
        try:
            if int(length) != len(payload):
                notes.append(f"content-length {length} ≠ 实际 {len(payload)} 字节"
                             "（设备会报 Incomplete content received）")
        except (TypeError, ValueError):
            notes.append(f"content-length 不是数字：{length!r}")
    return {
        "ok": not notes,
        "notes": notes,
        "content_length": length,
        "bytes": len(payload),
        "content_encoding": encoding,
        "transfer_encoding": transfer,
        "content_type": meta.get("content_type", ""),
        "issuer": meta.get("tls_issuer", ""),
        "seconds": meta.get("seconds"),
    }


def check(url, fetch=None, timeout=15):
    """取一条地址，返回 {url, ok, status, bytes, items, error, koreader}。

    `fetch` 传 None 就用 `http_get`——CLI 一路传下来的是 None，别让 None 把默认值顶掉。
    """
    fetch = fetch or http_get
    meta = {}
    try:
        result = fetch(url, timeout)
    except HTTPError as exc:
        return {"url": url, "ok": False, "status": exc.code, "bytes": 0, "items": 0,
                "error": f"HTTP {exc.code}", "koreader": {"ok": False, "notes": [f"HTTP {exc.code}"]}}
    except (URLError, OSError, ValueError) as exc:
        return {"url": url, "ok": False, "status": 0, "bytes": 0, "items": 0,
                "error": str(exc), "koreader": {"ok": False, "notes": [str(exc)]}}
    if isinstance(result, tuple):
        payload, meta = result
    else:                                    # 测试里的假 fetch 只回正文
        payload = result
    try:
        items = count_items(payload)
    except (ElementTree.ParseError, ValueError) as exc:
        return {"url": url, "ok": False, "status": meta.get("status", 200),
                "bytes": len(payload), "items": 0, "error": f"不是 feed：{exc}",
                "koreader": koreader_check(payload, meta) | {"notes": [f"不是 feed：{exc}"]}}
    if not items:
        return {"url": url, "ok": False, "status": meta.get("status", 200),
                "bytes": len(payload), "items": 0,
                "error": "解析到 0 条（可能是空 feed 或错误页）",
                "koreader": koreader_check(payload, meta)}
    return {"url": url, "ok": True, "status": meta.get("status", 200),
            "bytes": len(payload), "items": items, "error": "",
            "koreader": koreader_check(payload, meta)}


def check_all(urls=None, fetch=None, timeout=15, log=None):
    fetch = fetch or http_get
    results = []
    for key, label, url in (urls if urls is not None else feed_urls()):
        result = check(url, fetch=fetch, timeout=timeout)
        result.update(key=key, label=label)
        results.append(result)
        if log:
            mark = "✅" if result["ok"] else "⚠️"
            device = result.get("koreader") or {}
            if result["ok"]:
                extra = []
                if device.get("issuer"):
                    extra.append(device["issuer"])
                if device.get("seconds"):
                    extra.append(f"{device['seconds']}s")
                detail = (f"{result['items']} 条 / {result['bytes'] / 1024:.1f} KiB"
                          + (f" / {' / '.join(extra)}" if extra else ""))
            else:
                detail = result["error"]
            log(f"  {mark} {label:34} {detail}")
            if result["ok"] and device and not device.get("ok", True):
                log(f"      ↳ 设备那边会失败：{'；'.join(device.get('notes') or [])}")
    return results


def summary(results):
    ok = [result for result in results if result["ok"]]
    return {"checked": len(results), "ok": len(ok),
            "urls": [result["url"] for result in ok]}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", default="", help="把结果写成 JSON（例如 site/feed-mirrors.json）")
    ap.add_argument("--url", action="append", default=[], help="额外测一条地址（可重复）")
    ap.add_argument("--only", nargs="*", default=None,
                    help="只测匹配这些 key 的地址（如 raw / jsdelivr / pages，按前缀）")
    ap.add_argument("--file", dest="files", nargs="*", default=None,
                    help="只测这些文件名（默认 news.xml news-lite.xml news-test.xml）")
    ap.add_argument("--timeout", type=int, default=15, help="单条超时秒数（默认 15）")
    return ap.parse_args(argv)


def main(argv=None, fetch=None):
    args = parse_args(argv)
    urls = feed_urls(args.files or FILES)
    if args.only:
        wanted = {key.lower() for key in args.only}
        urls = [item for item in urls
                if any(item[0].lower().startswith(token) for token in wanted)]
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
