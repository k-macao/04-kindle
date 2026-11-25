#!/usr/bin/env python3
"""每个同花顺行业板块的 A/H 同公司龙头与半年量价摘要。

这里不联网，只做四件事：

1. 把 A 股板块成分与 A/H 代码对求交集；
2. 按 A 股最近 20 个交易日平均成交额动态选龙头；
3. 从约 130 根日 K 计算半年收益、最大回撤、量能变化与价量状态；
4. 把日线压成最多 26 根周线，供网页 / Kindle 显示，完整日线另存 sidecar。

没有 A/H 同时上市成分股的板块必须保留一行 ``status=no_match``，不能拿普通 A 股
或两家不同公司硬凑成 “AH 龙头”。
"""

from datetime import datetime, timezone

# 统一 K 线紧凑格式，减少 stocks-leaders.json 体积：
# [date_ms, open, high, low, close, volume, turnover]
DATE, OPEN, HIGH, LOW, CLOSE, VOLUME, TURNOVER = range(7)
BAR_FIELDS = ("date_ms", "open", "high", "low", "close", "volume", "turnover")


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def compact_bar(date_ms, open_price, high_price, low_price, close_price,
                volume=None, turnover=None):
    """构造一根紧凑 K 线；日期或收盘无效时返回 None。"""
    if not isinstance(date_ms, (int, float)) or _number(close_price) is None:
        return None
    return [int(date_ms), _number(open_price), _number(high_price), _number(low_price),
            _number(close_price), _number(volume), _number(turnover)]


def normalize_name(name):
    """用于腾讯 AH 榜单名称与 A 股成分名称的保守匹配。

    只去证券简称里常见的装饰（空白、星号、H 股后缀）；不去“集团/股份”，避免把
    不同公司误合并。静态代码对仍是主映射，名称匹配只补新上市的 A/H 公司。
    """
    text = str(name or "").strip().upper()
    for token in (" ", "　", "*", "-W", "－W", "-SW", "－SW"):
        text = text.replace(token, "")
    return text


def average_turnover(bars, days=20):
    values = [bar[TURNOVER] for bar in (bars or [])[-days:]
              if len(bar) > TURNOVER and _number(bar[TURNOVER]) is not None
              and bar[TURNOVER] > 0]
    return sum(values) / len(values) if values else None


def _average(values):
    values = [value for value in values if _number(value) is not None]
    return sum(values) / len(values) if values else None


def metrics(bars):
    """半年日线 → 量价摘要。少于两根也返回明确的 ``bars`` 数，不伪造收益。"""
    bars = [bar for bar in (bars or [])
            if isinstance(bar, list) and len(bar) > TURNOVER
            and _number(bar[CLOSE]) is not None]
    closes = [bar[CLOSE] for bar in bars]
    result = {
        "bars": len(bars), "return_6m_pct": None, "max_drawdown_pct": None,
        "return_20d_pct": None, "avg_turnover_20": average_turnover(bars, 20),
        "volume_ratio_20": None, "signal": "数据不足",
    }
    if len(closes) >= 2 and closes[0]:
        result["return_6m_pct"] = (closes[-1] / closes[0] - 1) * 100
        peak = closes[0]
        worst = 0.0
        for close in closes:
            peak = max(peak, close)
            if peak:
                worst = min(worst, (close / peak - 1) * 100)
        result["max_drawdown_pct"] = worst
    if len(closes) >= 21 and closes[-21]:
        result["return_20d_pct"] = (closes[-1] / closes[-21] - 1) * 100

    recent_volume = _average([bar[VOLUME] for bar in bars[-20:]])
    previous_volume = _average([bar[VOLUME] for bar in bars[-40:-20]])
    if recent_volume is not None and previous_volume and previous_volume > 0:
        result["volume_ratio_20"] = recent_volume / previous_volume

    price = result["return_20d_pct"]
    volume_ratio = result["volume_ratio_20"]
    if price is not None and volume_ratio is not None:
        if price > 2 and volume_ratio >= 1.10:
            result["signal"] = "价升量增"
        elif price > 2 and volume_ratio <= 0.90:
            result["signal"] = "价升量缩"
        elif price < -2 and volume_ratio >= 1.10:
            result["signal"] = "价跌量增"
        elif price < -2 and volume_ratio <= 0.90:
            result["signal"] = "价跌量缩"
        else:
            result["signal"] = "量价平稳"
    return result


def weekly_bars(bars, limit=26):
    """日线按 ISO 周压成周 OHLCV；成交量/额求和，最多保留最近 ``limit`` 周。"""
    groups = []
    current_key = None
    for bar in bars or []:
        if not isinstance(bar, list) or len(bar) <= TURNOVER or _number(bar[CLOSE]) is None:
            continue
        try:
            day = datetime.fromtimestamp(bar[DATE] / 1000, tz=timezone.utc)
        except (OSError, OverflowError, TypeError, ValueError):
            continue
        iso = day.isocalendar()
        key = (iso.year, iso.week)
        if key != current_key:
            groups.append(list(bar))
            current_key = key
            continue
        week = groups[-1]
        week[DATE] = bar[DATE]
        highs = [value for value in (week[HIGH], bar[HIGH]) if _number(value) is not None]
        lows = [value for value in (week[LOW], bar[LOW]) if _number(value) is not None]
        week[HIGH] = max(highs) if highs else None
        week[LOW] = min(lows) if lows else None
        week[CLOSE] = bar[CLOSE]
        for pos in (VOLUME, TURNOVER):
            values = [value for value in (week[pos], bar[pos]) if _number(value) is not None]
            week[pos] = sum(values) if values else None
    return groups[-limit:] if limit and limit > 0 else groups


def select_sector_leaders(sectors, members, pair_map, histories, market_rows=None):
    """为每个板块选一个 A/H 同公司龙头；每个板块始终返回一行。

    ``members`` 是 ``{sector_code: [{thscode,name}, ...]}``；``pair_map`` 是
    ``{a_code: h_code}``。唯一排序口径是 20 个完整交易日的平均成交额；历史不足时
    不拿单日成交额替代，以免把不同口径伪装成同一套“龙头”排名。
    """
    market = {row.get("thscode"): row for row in (market_rows or []) if row.get("thscode")}
    rows = []
    for sector_code, sector_name in sectors:
        candidates = []
        for member in members.get(sector_code) or []:
            a_code = str(member.get("thscode") or "")
            h_code = pair_map.get(a_code)
            if not h_code:
                continue
            bars = histories.get(a_code) or []
            turnovers = [bar[TURNOVER] for bar in bars[-20:]
                         if isinstance(bar, list) and len(bar) > TURNOVER
                         and _number(bar[TURNOVER]) is not None and bar[TURNOVER] > 0]
            # “近 20 日平均成交额”是用户指定的唯一排名口径。少一天也不拿当日额
            # 偷换概念；数据源暂缺时由构建器沿用上一份已验证龙头。
            if len(turnovers) < 20:
                continue
            avg = sum(turnovers) / len(turnovers)
            candidates.append((avg, a_code, h_code,
                               member.get("name") or (market.get(a_code) or {}).get("name") or a_code))
        if not candidates:
            has_pair = any(pair_map.get(str(member.get("thscode") or ""))
                           for member in members.get(sector_code) or [])
            rows.append({
                "sector_code": sector_code, "sector_name": sector_name,
                "status": "no_match",
                "reason": ("A/H 候选缺少完整 20 日成交额，暂不选"
                           if has_pair else "当前成分股中没有可验证的 A/H 同公司"),
            })
            continue
        candidates.sort(key=lambda item: (-item[0], item[1]))
        score, a_code, h_code, name = candidates[0]
        rows.append({
            "sector_code": sector_code, "sector_name": sector_name, "status": "ok",
            "name": name, "a_code": a_code, "h_code": h_code,
            "selection_basis": "A股近20日平均成交额",
            "selection_turnover": score,
        })
    return rows


def build_section(sectors, selected_rows, a_history, h_history,
                  market_rows=None, hk_rows=None, errors=None):
    """把选择结果、A/H 当前行情、半年指标与 26 周趋势合成面板段落。"""
    a_quotes = {row.get("thscode"): row for row in (market_rows or []) if row.get("thscode")}
    h_quotes = {row.get("thscode"): row for row in (hk_rows or []) if row.get("thscode")}
    rows, companies = [], set()
    for selected in selected_rows:
        row = dict(selected)
        if row.get("status") == "ok":
            a_code, h_code = row.get("a_code"), row.get("h_code")
            a_bars, h_bars = a_history.get(a_code) or [], h_history.get(h_code) or []
            row["a"] = {
                "quote": a_quotes.get(a_code), "metrics": metrics(a_bars),
                "weekly": weekly_bars(a_bars),
            }
            row["h"] = {
                "quote": h_quotes.get(h_code), "metrics": metrics(h_bars),
                "weekly": weekly_bars(h_bars),
            }
            if not a_bars or not h_bars:
                row["history_note"] = "A/H 至少一边半年 K 线不完整"
            companies.add((a_code, h_code))
        rows.append(row)
    matched = sum(row.get("status") == "ok" for row in rows)
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "selection": "板块当前成分股中的 A/H 同公司；按 A 股近 20 日平均成交额动态选 1 只",
        "history_window": "约 130 个交易日；网页/Kindle 压缩为最近 26 周",
        "coverage": {
            "sectors": len(sectors), "matched": matched,
            "unmatched": len(sectors) - matched, "companies": len(companies),
        },
        "rows": rows, "errors": list(errors or []),
    }


def sidecar_companies(section, a_history, h_history):
    """只保留被选中公司的完整 A/H 日线，避免把所有候选都写进产物。"""
    result = {}
    for row in (section or {}).get("rows") or []:
        if row.get("status") != "ok":
            continue
        a_code, h_code = row.get("a_code"), row.get("h_code")
        if a_code in result:
            continue
        result[a_code] = {
            "name": row.get("name"), "a_code": a_code, "h_code": h_code,
            "fields": list(BAR_FIELDS),
            "a": a_history.get(a_code) or [], "h": h_history.get(h_code) or [],
        }
    return result
