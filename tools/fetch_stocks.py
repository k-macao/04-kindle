#!/usr/bin/env python3
"""从同花顺官方 GitHub 仓库的金融数据服务（hithink-finance）抓 A 股行情快照。

    HITHINK_FINANCE_API_KEY=… python tools/fetch_stocks.py --out stocks_data.json
    python tools/fetch_stocks.py --codes 600519.SH,000001.SZ --no-limit-up
    HITHINK_FINANCE_API_KEY=… python tools/fetch_stocks.py --loop 60 --build

数据源是同花顺官方维护的 GitHub 仓库
[Hithink-Tech-Org/Financial-API](https://github.com/Hithink-Tech-Org/Financial-API)
（官网 https://fuyao.aicubes.cn/，文档在仓库 ``docs/api/``）。统一 API Key，
REST 接口约定（见仓库 ``docs/api/README.md``）：

- Base URL ``https://fuyao.aicubes.cn``（可用 ``HITHINK_FINANCE_BASE_URL`` 覆盖）；
- 认证头 ``X-api-key: <API_KEY>``，Key 只从环境变量读，绝不写进日志或产物；
- 成功判断是 HTTP 200 **且**响应信封 ``code == 0``，信封形如
  ``{code, message, request_id, data}``，HTTP 200 不代表业务成功；
- ``1xxx``（参数错）与 ``2xxx``（未认证/无权限）是调用方可修的错，不重试；
  ``4001``（限流）与 ``5xxx``（服务端/上游）退避重试，最多 3 次；
- 空值是 ``null``（未披露），不补零、不拿模拟数据顶。

这里只用三个接口：

- ``GET /api/a-share/prices/snapshot?thscodes=…`` —— 自选股最新价、涨跌幅、
  开高低收、成交量/额（一次请求带回全部自选股）；
- ``GET /api/a-share-index/prices/snapshot?thscodes=…`` —— 宽基指数快照；
- ``GET /api/a-share/special-data/limit-up-pool`` —— 当日涨停池（情绪面，
  失败不挡主行情，只记进 ``errors``）；
- ``GET /api/a-share/prices/historical-batch`` 与指数同名接口 —— 最近
  ``--history-days`` 根日 K 收盘价，给卡片上的迷你走势图（失败不挡快照）。

快照不返回中文名，名字用 ``GET /api/meta/tickers/search?q=<thscode>`` 逐个补，
补过的存进 ``site/stocks_names.json`` 缓存，下次不再问（和新闻译文缓存一个路子）。

输出 ``stocks_data.json`` 只是中间产物，不进 Git（和 ``news_data.json`` 一样）；
排版成 Kindle 文本 / 网页面板 / 清单的是 ``tools/build_stocks.py``。抓取只负责
「取回并归一化」，合成只负责「判断与输出」，改了模板不用重抓。

关于「一分钟更新」：GitHub Actions 的定时任务最短 5 分钟（见
``.github/workflows/refresh-stocks.yml``），服务端快照最快 5 分钟一轮；
Kindle 插件与网页面板每 60 秒检查一次清单，``generated`` 变了才刷新。
本地要真正的 60 秒一轮，用 ``--loop 60 --build`` 常驻跑。
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_watchlist  # noqa: E402
import stock_watchlist  # noqa: E402

DEFAULT_BASE_URL = "https://fuyao.aicubes.cn"
# 官方推荐的环境变量名（见 Financial-API README「获取统一 API Key」一节）；
# 兼容仓库 .env.example 里的 API_KEY，以及仓库 Secret 的 THS_API_KEY / THS_KEY。
API_KEY_ENV_VARS = ("HITHINK_FINANCE_API_KEY", "THS_API_KEY", "THS_KEY", "API_KEY")
# 可重试的业务码：限流与服务端/上游异常（见 docs/api/README.md 错误表）。
RETRYABLE_CODES = (4001, 5001, 5002, 5003)
MAX_ATTEMPTS = 3
USER_AGENT = "004-kindle/stocks/1.0"


class ThsError(RuntimeError):
    """同花顺接口返回了业务错误（code != 0）或网络失败。"""

    def __init__(self, message, code=None, request_id=None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id


def api_key_from_env(environ=None):
    """按官方名优先的顺序从环境变量取 API Key；没有就返回空串。"""
    env = os.environ if environ is None else environ
    for name in API_KEY_ENV_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def _num(value):
    """把接口的数字字段归一成 float；null / 非数字一律 None（不补零）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _default_fetch(url, headers, timeout):
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 固定官方域名
        return response.read()


class ThsClient:
    """同花顺金融数据服务 REST 客户端：只认信封 code==0，限流/5xxx 退避重试。"""

    def __init__(self, base_url=DEFAULT_BASE_URL, api_key="", fetch=None,
                 timeout=30, sleep=time.sleep, max_attempts=MAX_ATTEMPTS):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or ""
        self.fetch = fetch or _default_fetch
        self.timeout = timeout
        self.sleep = sleep
        self.max_attempts = max(1, max_attempts)

    def get(self, path, params=None):
        """GET 一个接口，返回信封里的 data；业务错误抛 ThsError（带 code）。"""
        query = urlencode(params or {})
        url = self.base_url + path + (("?" + query) if query else "")
        headers = {"X-api-key": self.api_key, "User-Agent": USER_AGENT,
                   "Accept": "application/json"}
        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw = self.fetch(url, headers, self.timeout)
            except HTTPError as exc:
                # HTTP 层 5xx 可重试；4xx 是调用方问题，不重试。
                last_error = ThsError(f"同花顺接口 HTTP {exc.code}")
                if exc.code < 500 or attempt == self.max_attempts:
                    raise last_error from exc
                self.sleep(2 ** (attempt - 1))
                continue
            except (URLError, TimeoutError, OSError) as exc:
                last_error = ThsError(f"连接同花顺接口失败：{exc}")
                if attempt == self.max_attempts:
                    raise last_error from exc
                self.sleep(2 ** (attempt - 1))
                continue
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ThsError(f"同花顺接口返回的不是有效 JSON：{exc}") from exc
            if not isinstance(envelope, dict) or "code" not in envelope:
                raise ThsError("同花顺接口响应缺少 code 信封")
            code = envelope.get("code")
            if code == 0:
                data = envelope.get("data")
                return data if isinstance(data, dict) else {}
            message = str(envelope.get("message") or "未知错误")
            request_id = envelope.get("request_id")
            last_error = ThsError(
                f"同花顺接口 code={code}：{message}"
                + (f"（request_id={request_id}）" if request_id else ""),
                code=code, request_id=request_id)
            # 1xxx/2xxx 是调用方可修的错，重试也没用；只有限流/5xxx 才退避重试。
            if code not in RETRYABLE_CODES or attempt == self.max_attempts:
                raise last_error
            self.sleep(2 ** (attempt - 1))
        raise last_error or ThsError("同花顺接口请求重试次数用尽")


def normalize_snapshot(item, fallback_name=""):
    """把一条快照记录归一成面板字段；缺字段留 None，不补零。"""
    if not isinstance(item, dict):
        return None
    thscode = str(item.get("thscode") or "").strip()
    if not thscode:
        return None
    name = str(item.get("name") or fallback_name or "").strip()
    return {
        "thscode": thscode,
        "ticker": str(item.get("ticker") or thscode.split(".")[0]),
        "name": name,
        "last_price": _num(item.get("last_price")),
        "price_change": _num(item.get("price_change")),
        "price_change_ratio_pct": _num(item.get("price_change_ratio_pct")),
        "open_price": _num(item.get("open_price")),
        "high_price": _num(item.get("high_price")),
        "low_price": _num(item.get("low_price")),
        "prev_price": _num(item.get("prev_price")),
        "volume": _num(item.get("volume")),
        "turnover": _num(item.get("turnover")),
    }


def normalize_limit_up(item):
    """涨停池一条：价格/涨幅/封板时间/原因/连板/封单额。"""
    if not isinstance(item, dict):
        return None
    thscode = str(item.get("thscode") or "").strip()
    if not thscode:
        return None
    return {
        "thscode": thscode,
        "name": str(item.get("name") or "").strip(),
        "last_price": _num(item.get("last_price")),
        "price_change_ratio_pct": _num(item.get("price_change_ratio_pct")),
        "limit_up_time": str(item.get("limit_up_time") or "").strip(),
        "limit_up_reason": str(item.get("limit_up_reason") or "").strip(),
        "continue_day_text": str(item.get("continue_day_text") or "").strip(),
        "seal_money": _num(item.get("seal_money")),
    }


def load_names_cache(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_names_cache(path, cache):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def resolve_names(client, codes, cache, fallback_names):
    """补中文名：缓存/兜底里有的直接用，缺的逐个 search，补到就写回缓存。"""
    names = {}
    missing = []
    for code in codes:
        name = (cache.get(code) or fallback_names.get(code) or "").strip()
        if name:
            names[code] = name
        else:
            missing.append(code)
    for code in missing:
        try:
            data = client.get("/api/meta/tickers/search",
                              {"q": code, "limit": 1})
        except ThsError:
            continue  # 名字补不上不挡行情，面板上就显示代码
        for entry in data.get("item") or []:
            if isinstance(entry, dict) and entry.get("thscode") == code:
                found = str(entry.get("name") or "").strip()
                if found:
                    names[code] = found
                    cache[code] = found
                break
    return names


def normalize_history_group(group, keep):
    """批量历史 K 线一组 → [[date_ms, close], ...]，只留最近 keep 根。"""
    if not isinstance(group, dict):
        return []
    bars = []
    for bar in group.get("item") or []:
        if not isinstance(bar, dict):
            continue
        date_ms = bar.get("date_ms")
        close = _num(bar.get("close_price"))
        if isinstance(date_ms, (int, float)) and close is not None:
            bars.append([int(date_ms), close])
    bars.sort(key=lambda pair: pair[0])
    return bars[-keep:] if keep and keep > 0 else bars


def fetch_history(client, path, codes, days, now_ms=None):
    """批量日 K（每批最多 10 个代码）→ {thscode: [[date_ms, close], ...]}。

    窗口按自然日往前推（周末/节假日接口只返回交易日，取回后裁到最近 days 根）。
    """
    if not codes or not days or days <= 0:
        return {}
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (days * 2 + 10) * 24 * 3600 * 1000
    history = {}
    for offset in range(0, len(codes), 10):
        chunk = codes[offset:offset + 10]
        data = client.get(path, {
            "thscodes": ",".join(chunk), "interval": "1d",
            "start": start_ms, "end": now_ms, "adjust": "forward"})
        for group in data.get("item") or []:
            if isinstance(group, dict) and group.get("thscode"):
                history[str(group["thscode"])] = normalize_history_group(group, days)
    return history


def fetch_payload(client, stocks, indices, limit_up_size=0, names_cache_path=None,
                  history_days=0):
    """抓一轮：指数快照 + 股票快照 +（可选）涨停池 +（可选）历史 K 线。"""
    generated = datetime.now(timezone.utc).isoformat()
    payload = {"generated": generated, "data_timestamp": None,
               "indices": [], "stocks": [], "limit_up": [], "history": {},
               "errors": []}

    fallback = {}
    for code, name in list(indices) + list(stocks):
        fallback[code] = name
    cache = load_names_cache(names_cache_path) if names_cache_path else {}

    def snapshot(path, pairs):
        codes = [code for code, _ in pairs]
        if not codes:
            return [], None
        data = client.get(path, {"thscodes": ",".join(codes)})
        names = resolve_names(client, codes, cache, fallback)
        rows = []
        for entry in data.get("item") or []:
            row = normalize_snapshot(entry, names.get(
                str((entry or {}).get("thscode") or ""), ""))
            if row:
                rows.append(row)
        return rows, data.get("timestamp")

    index_rows, index_ts = snapshot("/api/a-share-index/prices/snapshot", indices)
    stock_rows, stock_ts = snapshot("/api/a-share/prices/snapshot", stocks)
    # 指数保持自选顺序；股票按涨跌幅排（涨的在前），没有涨跌幅的沉底。
    order = {code: pos for pos, (code, _) in enumerate(indices)}
    index_rows.sort(key=lambda row: order.get(row["thscode"], 9999))
    stock_rows.sort(key=lambda row: (
        row["price_change_ratio_pct"] is None,
        -(row["price_change_ratio_pct"] or 0.0)))
    payload["indices"] = index_rows
    payload["stocks"] = stock_rows
    stamps = [ts for ts in (index_ts, stock_ts) if isinstance(ts, (int, float))]
    payload["data_timestamp"] = max(stamps) if stamps else None

    if limit_up_size and limit_up_size > 0:
        try:
            data = client.get("/api/a-share/special-data/limit-up-pool", {
                "page": 1, "size": limit_up_size,
                "sort_field": "limit_up_time", "sort_dir": "desc"})
            rows = [row for row in (normalize_limit_up(entry)
                                    for entry in data.get("item") or []) if row]
            payload["limit_up"] = rows
        except ThsError as exc:
            # 涨停池是情绪面，挂了不挡主行情——记下来，面板上如实写。
            payload["errors"].append(f"涨停池未获取：{exc}")

    if history_days and history_days > 0:
        # 历史 K 线只为卡片上的迷你走势图，挂了不挡快照。
        try:
            history = {}
            history.update(fetch_history(
                client, "/api/a-share-index/prices/historical-batch",
                [code for code, _ in indices], history_days))
            history.update(fetch_history(
                client, "/api/a-share/prices/historical-batch",
                [code for code, _ in stocks], history_days))
            payload["history"] = history
        except ThsError as exc:
            payload["errors"].append(f"历史 K 线未获取（无迷你走势图）：{exc}")

    if names_cache_path:
        save_names_cache(names_cache_path, cache)
    return payload


def parse_codes(text):
    """`600519.SH, 000001.SZ` → [(code, "")]，供 --codes/--indices 覆盖清单。"""
    pairs = []
    for piece in (text or "").split(","):
        code = piece.strip().upper()
        if code:
            pairs.append((code, ""))
    return pairs


def fetch_index_rows(client, codes, chunk=40, log=None):
    """批量取指数/板块快照，**把坏代码隔离出来**。

    实测：一批里只要有一个不存在的代码（如 ``932000.CSI``），整个批次就返回
    ``code=1002`` 和 0 条——宽基指数曾因此全灭。所以分批取，某批失败就逐个单取，
    把真正坏的代码挑出来，其余照常返回。
    """
    rows, bad, stamps = [], [], []
    codes = [c for c in dict.fromkeys(codes) if c]
    for start in range(0, len(codes), chunk):
        batch = codes[start:start + chunk]
        try:
            data = client.get("/api/a-share-index/prices/snapshot",
                              {"thscodes": ",".join(batch)})
            got = (data or {}).get("item") or []
            rows.extend(got)
            if isinstance((data or {}).get("timestamp"), (int, float)):
                stamps.append(data["timestamp"])
            missing = [c for c in batch if c not in {r.get("thscode") for r in got}]
            if missing:
                bad.extend(missing)
        except ThsError as exc:
            if log:
                log(f"批次 {batch[0]}…{batch[-1]} 失败（{exc}），逐个重试")
            for code in batch:
                try:
                    data = client.get("/api/a-share-index/prices/snapshot",
                                      {"thscodes": code})
                    got = (data or {}).get("item") or []
                    if got:
                        rows.extend(got)
                        if isinstance((data or {}).get("timestamp"), (int, float)):
                            stamps.append(data["timestamp"])
                    else:
                        bad.append(code)
                except ThsError:
                    bad.append(code)
    return rows, list(dict.fromkeys(bad)), (max(stamps) if stamps else None)


def fetch_full_market(client, page=None, log=None):
    """全 A 快照（分页取尽）→ (rows, total)。用于涨跌家数/成交额等市场概览。"""
    page = page or market_watchlist.FULL_MARKET_PAGE
    rows, total, offset = [], None, 0
    while True:
        try:
            data = client.get("/api/a-share/prices/snapshot",
                              {"limit": page, "offset": offset})
        except ThsError as exc:
            if log:
                log(f"全市场第 {offset} 页失败：{exc}")
            break
        total = data.get("total", total)
        chunk = data.get("item") or []
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
        if total and offset >= total:
            break
    return rows, total


def market_breadth(rows):
    """全市场统计：涨跌家数、涨跌停、成交额、平均涨跌幅、涨跌前 10。"""
    stats = {"count": len(rows), "up": 0, "down": 0, "flat": 0, "no_price": 0,
             "limit_up": 0, "limit_down": 0, "turnover": 0.0, "volume": 0.0,
             "avg_ratio": None, "top_gainers": [], "top_losers": []}
    ratios = []
    for row in rows:
        ratio = row.get("price_change_ratio_pct")
        price = row.get("last_price")
        stats["turnover"] += row.get("turnover") or 0.0
        stats["volume"] += row.get("volume") or 0.0
        if price in (None, 0) or ratio is None:
            stats["no_price"] += 1
            continue
        ratios.append(ratio)
        if ratio > 0:
            stats["up"] += 1
        elif ratio < 0:
            stats["down"] += 1
        else:
            stats["flat"] += 1
        # 涨跌停粗判：主板 10%、创业板/科创 20%、北交所 30%。
        code = str(row.get("thscode") or "")
        limit = 29.5 if code.endswith(".BJ") else (
            19.5 if (code.startswith("30") or code.startswith("68")) else 9.8)
        if ratio >= limit:
            stats["limit_up"] += 1
        elif ratio <= -limit:
            stats["limit_down"] += 1
    if ratios:
        stats["avg_ratio"] = sum(ratios) / len(ratios)
    ranked = [r for r in rows if r.get("price_change_ratio_pct") is not None
              and r.get("last_price")]
    ranked.sort(key=lambda r: -(r.get("price_change_ratio_pct") or 0))
    stats["top_gainers"] = [{"thscode": r.get("thscode"), "name": r.get("name") or "",
                             "ratio": r.get("price_change_ratio_pct"),
                             "last_price": r.get("last_price")} for r in ranked[:10]]
    stats["top_losers"] = [{"thscode": r.get("thscode"), "name": r.get("name") or "",
                            "ratio": r.get("price_change_ratio_pct"),
                            "last_price": r.get("last_price")} for r in ranked[-10:][::-1]]
    return stats


def enrich_index_rows(rows, pairs):
    """给指数/板块补中文名与顺序（接口不返回名字），并标记哪些是板块。"""
    names = {code: name for code, name in pairs}
    order = {code: pos for pos, (code, _) in enumerate(pairs)}
    out = []
    for row in rows:
        code = row.get("thscode")
        if not code:
            continue
        item = dict(row)
        item["name"] = names.get(code) or item.get("name") or code
        item["is_sector"] = code.endswith(".TI") or bool(item.get("sector"))
        out.append(item)
    out.sort(key=lambda r: order.get(r.get("thscode"), 9999))
    return out


def run_once(args, client):
    """抓一轮并写出 stocks_data.json。

    面板形态是「指数 + 板块 + 市场概览 + 港股」的一屏监视（**不再有自选股**）：
    ``indices`` 放宽基与行业/概念板块，``market`` 放全 A 统计（5584 只的涨跌家数、
    成交额、涨跌停、涨跌前 10），``hk`` 放港股（另走腾讯接口）。
    """
    pairs = list(market_watchlist.INDICES) + list(market_watchlist.SECTORS)
    if args.indices:
        wanted = parse_codes(args.indices)
        pairs = [(c, n) for c, n in pairs if c in wanted] or [(c, c) for c in wanted]
    codes = [c for c, _ in pairs]

    payload = {"generated": datetime.now(timezone.utc).isoformat(),
               "data_timestamp": None, "index_timestamp": None,
               "indices": [], "market": None, "hk": None,
               "limit_up": [], "errors": []}

    # 1) 指数 + 板块（坏代码隔离，某批失败不影响其余）
    rows, bad, index_ts = fetch_index_rows(
        client, codes, log=lambda m: payload["errors"].append(m))
    payload["index_timestamp"] = index_ts
    payload["indices"] = enrich_index_rows(rows, pairs)
    if bad:
        payload["errors"].append(f"这些指数/板块代码无效或接口没返回：{', '.join(bad)}")

    # 2) 全 A 市场概览（分页取尽；失败留 null，不挡指数）
    if not args.no_market:
        try:
            market_rows, total = fetch_full_market(
                client, log=lambda m: payload["errors"].append(m))
            if market_rows:
                stats = market_breadth(market_rows)
                stats["total"] = total or len(market_rows)
                payload["market"] = stats
        except ThsError as exc:
            payload["errors"].append(f"全市场快照失败：{exc}")

    # 3) 涨停池（情绪面，失败不挡主行情）
    if not args.no_limit_up and args.limit_up_size:
        try:
            data = client.get("/api/a-share/special-data/limit-up-pool", {
                "page": 1, "size": args.limit_up_size,
                "sort_field": "limit_up_time", "sort_dir": "desc"})
            payload["limit_up"] = [r for r in (
                normalize_limit_up(e) for e in data.get("item") or []) if r]
        except ThsError as exc:
            payload["errors"].append(f"涨停池失败：{exc}")

    # 4) 港股（腾讯源，独立失败，不拖垮 A 股）
    if not args.no_hk:
        try:
            import fetch_hk  # noqa: E402
            hk = fetch_hk.fetch_payload()
            payload["hk"] = hk
            payload["errors"].extend(hk.get("errors") or [])
        except Exception as exc:                    # noqa: BLE001
            payload["errors"].append(f"港股行情失败：{exc}")

    # 数据时间 = 各市场里最新的快照时间（港股收盘晚于 A 股，取 max 才不误报过期）
    stamps = [payload.get("index_timestamp")]
    hk_stamp = (payload.get("hk") or {}).get("data_timestamp")
    if isinstance(hk_stamp, (int, float)):
        stamps.append(hk_stamp)
    stamps = [s for s in stamps if isinstance(s, (int, float))]
    payload["data_timestamp"] = max(stamps) if stamps else None

    if not payload["indices"] and not payload["market"]:
        raise ThsError("指数和全市场快照一条都没返回，不生成空面板")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    market = payload.get("market") or {}
    hk = payload.get("hk") or {}
    print(f"已抓取 {len(payload['indices'])} 个指数/板块"
          f" / 全市场 {market.get('count', 0)} 只（涨 {market.get('up', 0)} 跌 {market.get('down', 0)}）"
          f" / 涨停 {len(payload['limit_up'])}"
          f" / 港股 {len(hk.get('rows') or [])} 个 → {out}")
    for error in payload["errors"]:
        print(f"警告：{error}", file=sys.stderr)
    if args.build:
        import build_stocks  # noqa: E402  本地 --loop 时顺手排版
        build_stocks.build(out, Path(args.out_dir))
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="从同花顺金融数据服务抓 A 股行情快照 → stocks_data.json")
    parser.add_argument("--out", default="stocks_data.json",
                        help="中间产物路径（不进 Git）")
    parser.add_argument("--out-dir", default="site",
                        help="--build 时的产物目录（默认 site）")
    parser.add_argument("--codes", default="",
                        help="只抓这些股票 thscode（逗号分隔），默认用自选清单")
    parser.add_argument("--indices", default="",
                        help="只抓这些指数 thscode（逗号分隔），默认用自选清单")
    parser.add_argument("--no-limit-up", action="store_true",
                        help="不取涨停池")
    parser.add_argument("--no-market", action="store_true",
                        help="不取全 A 市场概览（5584 只，省时间）")
    parser.add_argument("--no-hk", action="store_true",
                        help="不取港股（腾讯公开接口）")
    parser.add_argument("--limit-up-size", type=int,
                        default=stock_watchlist.LIMIT_UP_SIZE,
                        help="涨停池取前几名")
    parser.add_argument("--history-days", type=int, default=20,
                        help="卡片迷你走势图取最近几根日 K（0=不取）")
    parser.add_argument("--names-cache", default=stock_watchlist.NAMES_CACHE,
                        help="中文名缓存路径")
    parser.add_argument("--base-url", default=os.environ.get(
        "HITHINK_FINANCE_BASE_URL", DEFAULT_BASE_URL),
        help="同花顺金融数据服务 Base URL")
    parser.add_argument("--loop", type=float, default=0, metavar="SECONDS",
                        help="每隔 SECONDS 秒重复抓一轮（本地实时用，如 60）")
    parser.add_argument("--build", action="store_true",
                        help="每轮抓完顺手跑 build_stocks 排版")
    args = parser.parse_args(argv)

    api_key = api_key_from_env()
    if not api_key:
        print("缺少同花顺 API Key：请设置环境变量 HITHINK_FINANCE_API_KEY"
              "（或 THS_API_KEY / THS_KEY；GitHub Actions 里是仓库 Secret，"
              "见 docs/kindle-stocks.md）",
              file=sys.stderr)
        return 1
    client = ThsClient(base_url=args.base_url, api_key=api_key)
    try:
        if args.loop and args.loop > 0:
            print(f"每 {args.loop:g} 秒抓一轮（Ctrl+C 停止）")
            while True:
                run_once(args, client)
                time.sleep(args.loop)
        run_once(args, client)
    except ThsError as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
