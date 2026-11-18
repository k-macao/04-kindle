#!/usr/bin/env python3
"""港股行情快照：腾讯 ``qt.gtimg.cn``（同花顺没有港股行情）。

**为什么不用同花顺**：官方文档 ``docs/api/hk-share`` 只有三个新股 IPO 接口，
``/api/meta/tickers/list`` 的 ``asset_type`` 枚举里也没有港股；实测
``/api/a-share-index/prices/snapshot?thscodes=HSI.HK`` 与 ``00700.HK`` 都返回
``1002 Invalid parameter format``。所以港股行情另找一个公开源。

**用的是什么**：腾讯的批量行情接口，一次请求多个代码，返回 GBK 编码的
``v_hk00700="100~腾讯控股~00700~372.800~376.000~...";`` 这种分号分隔文本。
字段位置（实测确认，2026-11）：

    0  市场标识      3  最新价        4  昨收          5  今开
    6  成交量(股)    30 时间(YYYY/MM/DD HH:MM:SS)      31 涨跌额
    32 涨跌幅(%)     33 最高          34 最低          37 成交额

指数与个股同格式，``f[1]`` 是中文名（指数叫「恒生指数」）。接口没有
``prev_price`` 之外的昨收字段差异，涨跌额/涨跌幅直接用它给的。

用法::

    python tools/fetch_hk.py --out hk_data.json
    python tools/fetch_hk.py --codes hk00700,hkHSI      # 只抓这几个

失败不抛异常：返回 ``{"rows": [...], "errors": [...]}``，缺的标的就不出现在面板上，
其它市场照常显示——单点故障不该让整块面板塌掉。
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_watchlist  # noqa: E402

BASE_URL = "https://qt.gtimg.cn/q="
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120 Safari/537.36")
MAX_ATTEMPTS = 3
# 字段下标（见模块 docstring）。
IDX_NAME, IDX_LAST, IDX_PREV, IDX_OPEN = 1, 3, 4, 5
IDX_VOLUME, IDX_TIME, IDX_CHANGE, IDX_RATIO = 6, 30, 31, 32
IDX_HIGH, IDX_LOW, IDX_TURNOVER = 33, 34, 37


def _default_fetch(url, headers, timeout):
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _num(text):
    """腾讯用空串/0 表示没有；转成 float 或 None。"""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value


def parse_quote_line(line):
    """一行 ``v_hk00700="100~腾讯控股~...";`` → 归一化后的 dict；解析不了返回 None。"""
    line = (line or "").strip()
    if not line.startswith("v_") or '="' not in line:
        return None
    head, _, payload = line.partition('="')
    thscode = head[2:].strip()
    fields = payload.rstrip('";').split("~")
    if not thscode or len(fields) <= IDX_TURNOVER:
        return None
    last = _num(fields[IDX_LAST])
    prev = _num(fields[IDX_PREV])
    change = _num(fields[IDX_CHANGE])
    ratio = _num(fields[IDX_RATIO])
    # 腾讯自己给的涨跌额/涨跌幅更可靠（前收在除权日会不一致），缺了才用最新-昨收补。
    if change is None and last is not None and prev:
        change = round(last - prev, 4)
    if ratio is None and change is not None and prev:
        ratio = round(change / prev * 100, 6)
    return {
        "thscode": thscode,
        "name": fields[IDX_NAME].strip(),
        "last_price": last,
        "prev_price": prev,
        "open_price": _num(fields[IDX_OPEN]),
        "high_price": _num(fields[IDX_HIGH]),
        "low_price": _num(fields[IDX_LOW]),
        "price_change": change,
        "price_change_ratio_pct": ratio,
        "volume": _num(fields[IDX_VOLUME]),
        "turnover": _num(fields[IDX_TURNOVER]),
        "quote_time": fields[IDX_TIME].strip(),
        "currency": "HKD",
    }


def parse_response(text):
    """整段响应 → [dict]。没数据的标的腾讯返回 ``v_hkXXXX="";``，直接跳过。"""
    rows = []
    for line in (text or "").split(";"):
        row = parse_quote_line(line)
        if row and row.get("last_price") is not None:
            rows.append(row)
    return rows


def quote_time_ms(text):
    """``2026/11/18 16:08:10``（香港时间）→ 毫秒时间戳；解析不了返回 None。"""
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
        except (TypeError, ValueError):
            continue
        # 港股时间 = UTC+8
        return int((dt.replace(tzinfo=timezone.utc).timestamp() - 8 * 3600) * 1000)
    return None


def fetch_payload(codes=None, indices=None, stocks=None, fetch=None, timeout=30,
                  sleep=time.sleep, base_url=BASE_URL):
    """抓一轮港股：指数 + 个股。返回 ``{"generated", "data_timestamp", "rows", "errors"}``。"""
    fetch = fetch or _default_fetch
    index_pairs = market_watchlist.HK_INDICES if indices is None else indices
    stock_pairs = market_watchlist.HK_STOCKS if stocks is None else stocks
    wanted = []
    if codes:
        names = {c: n for c, n in list(index_pairs) + list(stock_pairs)}
        wanted = [(c, names.get(c, c)) for c in codes]
    else:
        wanted = list(index_pairs) + list(stock_pairs)

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "data_timestamp": None,
        "rows": [],
        "errors": [],
    }
    if not wanted:
        return payload

    names = dict(wanted)
    url = base_url + ",".join(code for code, _ in wanted)
    headers = {"User-Agent": USER_AGENT, "Referer": "https://finance.qq.com/"}
    text, last_error = None, None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = fetch(url, headers, timeout)
        except HTTPError as exc:
            last_error = f"腾讯行情 HTTP {exc.code}"
            if exc.code < 500 or attempt == MAX_ATTEMPTS:
                break
            sleep(2 ** (attempt - 1))
            continue
        except (URLError, TimeoutError, OSError) as exc:
            last_error = f"连接腾讯行情失败：{exc}"
            if attempt == MAX_ATTEMPTS:
                break
            sleep(2 ** (attempt - 1))
            continue
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            text = raw.decode("gb18030", "replace")
        break

    if text is None:
        payload["errors"].append(last_error or "腾讯行情无响应")
        return payload

    rows = parse_response(text)
    for row in rows:
        # 腾讯不给中文名时用清单里的兜底名。
        if not row.get("name"):
            row["name"] = names.get(row["thscode"], row["thscode"])
        row["is_index"] = row["thscode"] in {c for c, _ in index_pairs}
        row["quote_ms"] = quote_time_ms(row.get("quote_time"))
    payload["rows"] = rows

    stamps = [r["quote_ms"] for r in rows if isinstance(r.get("quote_ms"), (int, float))]
    payload["data_timestamp"] = max(stamps) if stamps else None

    missing = [code for code, _ in wanted if code not in {r["thscode"] for r in rows}]
    if missing:
        payload["errors"].append(f"腾讯行情缺这些标的：{', '.join(missing)}")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="抓港股行情快照（腾讯公开接口）")
    parser.add_argument("--out", default="hk_data.json", help="中间产物路径（不进 Git）")
    parser.add_argument("--codes", default="", help="只抓这些代码（逗号分隔，如 hk00700,hkHSI）")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    payload = fetch_payload(codes=codes or None, timeout=args.timeout)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    count = len(payload["rows"])
    stamp = payload["data_timestamp"]
    when = (datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat()
            if stamp else "未知")
    print(f"港股 {count} 个 · 行情时间 {when} → {out}")
    for problem in payload["errors"]:
        print(f"  注意：{problem}", file=sys.stderr)
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())
