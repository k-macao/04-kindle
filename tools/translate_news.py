#!/usr/bin/env python3
"""给英文条目补中文：把 news_data.json 里英文条目的标题/摘要翻成中文，就地写回。

    python tools/translate_news.py --data news_data.json --cache site/news_zh_cache.json
    python tools/translate_news.py --data news_data.json --dry-run
    python tools/translate_news.py --only JIN10 --max-items 5

日报和聚合 RSS 的其余环节都不联网（除了抓源），这一环是唯一额外要网的一步，所以：

- **只翻英文**：`lang=en` 的源，或者标题里一个汉字都没有、字母占一半以上（`looks_english`），
  才去翻。中文条目一个请求都不发。
- **三家免费接口按顺序回退**（都不需要密钥）：Google 的 `translate_a/single`、
  Lingva 镜像、MyMemory。哪家回的内容里没有汉字就算失败，换下一家。
- **结果落缓存** `site/news_zh_cache.json`：同一句英文只翻一次，第二天重复的稿子直接命中
  缓存，既省额度又让译文稳定（读者不会看到同一条新闻隔天换一种说法）。缓存的键是文本
  指纹，条目超过 `--cache-limit` 或放置超过 `--cache-days` 天就清掉。
- **翻不出来不算失败**：整个流程要能在翻译接口全挂的时候照常出日报，所以默认退出码 0；
  加 `--strict` 才在「一条都没翻成」时返回 3，工作流用 `continue-on-error` 接住它。

写回的字段：

    title_zh      标题的中文（原文仍在 title 里，渲染时中英并列）
    summary_zh    摘要的中文（只翻英文摘要，中文摘要不进这个字段）

统计写进 stdout，也写进 `news.json` 的 `zh_added` / `zh_missing`（见 tools/build_news.py）。
"""

import argparse
import hashlib
import html as html_mod
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

USER_AGENT = "004-kindle-news/1.0 (+https://github.com/k-macao/04-kindle)"
MAX_CHARS = 600          # 单次请求的正文上限，长摘要先截断再翻
CACHE_LIMIT = 6000
CACHE_DAYS = 120

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_RE = re.compile(r"[A-Za-z]")
SPACE_RE = re.compile(r"[\s\u3000]+")


# ---------------------------------------------------------------------------
# 判断这条要不要翻
# ---------------------------------------------------------------------------

def has_cjk(text):
    return bool(CJK_RE.search(str(text or "")))


def looks_english(text):
    """没有汉字、字母又占多数，就当英文。`GPT-5 发布` 因为有汉字，不算。"""
    text = str(text or "")
    if has_cjk(text):
        return False
    letters = len(LATIN_RE.findall(text))
    if letters < 8:
        return False
    visible = sum(1 for char in text if not char.isspace())
    return letters / max(visible, 1) >= 0.5


def needs_translation(item):
    """源标了 en，或者标题本身就是英文——两种都翻。"""
    if str(item.get("lang") or "").lower() == "en":
        return True
    return looks_english(item.get("title"))


# ---------------------------------------------------------------------------
# 三家免费接口
# ---------------------------------------------------------------------------

def _google_url(text):
    return ("https://translate.googleapis.com/translate_a/single"
            "?client=gtx&sl=en&tl=zh-CN&dt=t&q=" + quote(text))


def _google_parse(payload):
    data = json.loads(payload)
    return "".join(segment[0] for segment in (data[0] or []) if segment and segment[0])


def _lingva_url(text):
    return "https://lingva.garudalinux.org/api/v1/en/zh/" + quote(text)


def _lingva_parse(payload):
    return str(json.loads(payload).get("translation") or "")


def _mymemory_url(text):
    return ("https://api.mymemory.translated.net/get?q=" + quote(text)
            + "&langpair=en%7Czh-CN")


def _mymemory_parse(payload):
    data = json.loads(payload)
    if int(data.get("responseStatus") or 0) != 200:
        raise ValueError(f"MyMemory 状态 {data.get('responseStatus')}")
    text = str((data.get("responseData") or {}).get("translatedText") or "")
    # 额度用尽时它会把警告塞进 translatedText，别把警告当中文存起来。
    if "MYMEMORY WARNING" in text.upper() or "QUERY LENGTH LIMIT" in text.upper():
        raise ValueError("MyMemory 额度提示")
    return html_mod.unescape(text)


# (名字, 拼 URL, 解析)；顺序＝优先级。
PROVIDERS = (
    ("google", _google_url, _google_parse),
    ("lingva", _lingva_url, _lingva_parse),
    ("mymemory", _mymemory_url, _mymemory_parse),
)


def http_get(url, timeout=12):
    request = Request(url, headers={"User-Agent": USER_AGENT,
                                    "Accept": "application/json, text/plain;q=0.9"})
    with urlopen(request, timeout=timeout) as response:
        return response.read(256 * 1024)


def tidy(text):
    """翻译结果收拾干净：压缩空白，去掉接口偶尔回带的引号与多余冒号。"""
    text = SPACE_RE.sub(" ", str(text or "")).strip().strip("\"'“”").strip()
    if text.endswith("。"):
        text = text[:-1]
    return text


def translate(text, fetch=None, providers=PROVIDERS, timeout=12, log=None):
    """翻一条，返回 (中文, 用了哪家)；全挂返回 (None, 最后一条错误)。"""
    fetch = fetch or http_get
    text = SPACE_RE.sub(" ", str(text or "")).strip()[:MAX_CHARS]
    if not text:
        return None, "空文本"
    error = ""
    for name, url_of, parse in providers:
        try:
            payload = fetch(url_of(text), timeout)
            raw = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
            result = tidy(parse(raw))
        except (URLError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            error = f"{name}: {exc}"
            if log:
                log(f"    · {name} 不行（{exc}），换下一家")
            continue
        # 没有汉字就不是中文：接口可能原样回英文，别把它当中文写进日报。
        if not result or not has_cjk(result):
            error = f"{name}: 返回的内容不是中文"
            if log:
                log(f"    · {name} 回的不是中文，换下一家")
            continue
        return result, name
    return None, error or "没有可用的接口"


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------

def cache_key(text):
    return hashlib.sha1(SPACE_RE.sub(" ", str(text or "")).strip().encode("utf-8")).hexdigest()[:16]


def load_cache(path, now=None, days=CACHE_DAYS, limit=CACHE_LIMIT):
    """读缓存；过期条目与超量的旧条目直接丢掉，免得文件一直长。"""
    now = now or datetime.now(timezone.utc)
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    horizon = now - timedelta(days=days)
    entries = {}
    for key, value in (payload.get("entries") or {}).items():
        if not isinstance(value, dict):
            continue
        stamp = value.get("at") or ""
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment >= horizon and value.get("zh"):
            entries[str(key)] = {"zh": str(value["zh"]), "by": str(value.get("by") or ""),
                                 "at": stamp}
    if len(entries) > limit:
        ordered = sorted(entries.items(), key=lambda pair: pair[1].get("at") or "", reverse=True)
        entries = dict(ordered[:limit])
    return entries


def save_cache(path, entries, now=None, limit=CACHE_LIMIT):
    now = now or datetime.now(timezone.utc)
    if len(entries) > limit:
        ordered = sorted(entries.items(), key=lambda pair: pair[1].get("at") or "", reverse=True)
        entries = dict(ordered[:limit])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"updated": now.isoformat(timespec="seconds"),
                                  "count": len(entries), "entries": entries},
                                 ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return entries


# ---------------------------------------------------------------------------
# 批量处理
# ---------------------------------------------------------------------------

def translate_text(text, cache, fetch=None, providers=PROVIDERS, timeout=12,
                   delay=0.0, log=None):
    """带缓存的单条翻译，返回 (中文或 None, 来源：cache/接口名/None)。"""
    key = cache_key(text)
    hit = cache.get(key)
    if hit:
        return hit["zh"], "cache"
    if delay:
        time.sleep(delay)
    result, by = translate(text, fetch=fetch, providers=providers, timeout=timeout, log=log)
    if result:
        cache[key] = {"zh": result, "by": by,
                      "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return result, by


def translate_payload(payload, cache=None, fetch=None, max_items=80, providers=PROVIDERS,
                      timeout=12, delay=0.0, log=print, now=None):
    """给 payload 里的英文条目补 title_zh / summary_zh，返回统计字典。"""
    cache = {} if cache is None else cache
    now = now or datetime.now(timezone.utc)
    stats = {"english": 0, "translated": 0, "cached": 0, "failed": 0,
             "summary_translated": 0, "pending": 0}
    for item in payload.get("items") or []:
        if not needs_translation(item):
            continue
        if stats["english"] >= max_items:
            stats["pending"] += 1
            continue
        stats["english"] += 1
        title = str(item.get("title") or "")
        if title:
            zh, by = translate_text(title, cache, fetch=fetch, providers=providers,
                                    timeout=timeout, delay=delay, log=log)
            if zh:
                item["title_zh"] = zh
                if by == "cache":
                    stats["cached"] += 1
                else:
                    stats["translated"] += 1
            else:
                stats["failed"] += 1
        summary = str(item.get("summary") or "")
        # 摘要只在它本身是英文时才翻：中文摘要不需要，也不该被当成英文送去翻译。
        if summary and looks_english(summary) and summary != title:
            zh, by = translate_text(summary, cache, fetch=fetch, providers=providers,
                                    timeout=timeout, delay=delay, log=log)
            if zh:
                item["summary_zh"] = zh
                stats["summary_translated"] += 1
    stats["cache_size"] = len(cache)
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="news_data.json", help="fetch_news.py 的输出（就地更新）")
    ap.add_argument("--cache", default="site/news_zh_cache.json",
                    help="译文缓存（默认 site/news_zh_cache.json，跟着仓库走）")
    ap.add_argument("--only", nargs="*", default=None, help="只翻这几个源（key）")
    ap.add_argument("--max-items", type=int, default=80, help="最多翻多少条（默认 80）")
    ap.add_argument("--timeout", type=int, default=12, help="单请求超时秒数（默认 12）")
    ap.add_argument("--delay", type=float, default=0.3, help="每条之间歇几秒（默认 0.3）")
    ap.add_argument("--cache-limit", type=int, default=CACHE_LIMIT, help="缓存最多留多少条")
    ap.add_argument("--cache-days", type=float, default=CACHE_DAYS, help="缓存留多少天")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写回也不写缓存")
    ap.add_argument("--strict", action="store_true",
                    help="一条都没翻成时退出码 3（工作流用 continue-on-error 接）")
    return ap.parse_args(argv)


def main(argv=None, fetch=None):
    args = parse_args(argv)
    target = Path(args.data)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"读不到 {target}：{exc}", file=sys.stderr)
        return 2
    if args.only:
        wanted = {key.upper() for key in args.only}
        payload["items"] = [item for item in (payload.get("items") or [])
                            if str(item.get("source") or "").upper() in wanted]

    cache = load_cache(args.cache, days=args.cache_days, limit=args.cache_limit)
    stats = translate_payload(payload, cache=cache, fetch=fetch, max_items=args.max_items,
                              timeout=args.timeout, delay=args.delay,
                              log=lambda line: print(line))
    print(f"英文 {stats['english']} 条：新翻 {stats['translated']} 条，"
          f"命中缓存 {stats['cached']} 条，没翻成 {stats['failed']} 条，"
          f"另附摘要译文 {stats['summary_translated']} 条"
          + (f"，超出上限 {stats['pending']} 条" if stats["pending"] else ""))
    if args.dry_run:
        print("--dry-run：不写回、不写缓存")
        return 0
    if not args.only:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    save_cache(args.cache, cache, limit=args.cache_limit)
    print(f"缓存 {len(cache)} 条 → {args.cache}")
    if args.strict and stats["english"] and not (stats["translated"] or stats["cached"]):
        print("一条都没翻成：翻译接口可能全挂了", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
