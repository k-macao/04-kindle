#!/usr/bin/env python3
"""板块情绪 EW-SDM（情绪加权扩散动量）：单日横截面，纯标准库。

策略从哪来
----------
GitHub 仓库 `autumn-go/strategy-factor-dashboard`（「A 股策略因子平台」）里的
`src/engines/ews_engine.py`：

    final_score = momentum × emotion_diff × s_score

- momentum     板块内个股涨跌幅，按（成交额 + 封单额）加权平均；
- emotion_diff 板块内个股情绪权重，同样按（成交额 + 封单额）加权平均；
- s_score      以板块内最高连板数为自变量的 Sigmoid 连板杠杆。

那份实现读的是 SQLite 历史库，依赖 pandas。这里只保留**单日**那一截公式，
数据全部来自面板本轮已经抓到的东西：

- 全 A 快照（``price_change_ratio_pct`` 涨跌幅、``turnover`` 成交额）；
- 涨停池（``seal_money`` 封单额、``continue_day_text`` 连板文本）；
- 同花顺行业板块成分股（``ah_sector_members.json`` 的 7 天缓存）。

与原版的差异（都在面板上如实写明，不静默降级）
----------------------------------------------
1. **炸板负反馈未启用**：涨停池只给涨停股，没有炸板/跌停明细，所以
   ``is_broken`` 恒为 False，「炸板且跌超 5% → -1.0」这条规则不生效。
   这意味着 emotion_diff 恒 ≥ 0：final_score 的正负跟随 momentum；板块内既没有
   涨停、也没有涨超 3% 的股票时，emotion 为 0，final 就是 0（原版即如此，不是缺数据）。
2. **涨停池是前 N 只**：池外的涨停股按「涨幅 > 3% → 1.0」记，连板杠杆可能偏低。
   面板会把池大小与市场涨停数并排写出。
3. **只算「今天」**：原版的回测（EW-SDM 净值、与 RRG 的对比）需要多年历史，
   这里不做。

公式照原版逐行对应（``calculate_emotion_weight`` / ``calculate_s_score`` /
``compute_daily_factors``），测试里用手算值核过。
"""

from __future__ import annotations

import math
import re

SOURCE = {
    "repo": "autumn-go/strategy-factor-dashboard",
    "file": "src/engines/ews_engine.py",
    "name": "EW-SDM 情绪加权扩散动量（单日横截面）",
}

# 与原版 PARAMS 一致：趋势阈值 3%、炸板负反馈 -5%、Sigmoid 中心 3.5。
PARAMS = {
    "trend_threshold": 3.0,
    "broken_drop_threshold": -5.0,
    "sigmoid_offset": 3.5,
    "pct_clip": 30.0,       # 原版对涨跌幅做 ±30 截断（涨跌停 ±20%/±10%，留余量）
}

# 涨停池大小：要覆盖「市场涨停数」才能让连板杠杆有意义（实测约 60 只）。
DEFAULT_POOL_SIZE = 100

_STREAK_RE = re.compile(r"(\d+)\s*连板")


def parse_streak(text):
    """涨停池的 ``continue_day_text`` → 连板数。

    「首板」→ 1，「3连板」→ 3。池里出现但文本解析不了的，按首板算（池里的股票
    一定是涨停的，连板数至少是 1）。
    """
    value = str(text or "").strip()
    if not value:
        return 1
    if "首板" in value:
        return 1
    match = _STREAK_RE.search(value)
    if match:
        return max(1, int(match.group(1)))
    return 1


def emotion_weight(pct_chg, streak, is_broken=False):
    """个股情绪权重（原版 ``calculate_emotion_weight``）。

    1. 炸板且跌幅超 5% → -1.0（强负反馈；本面板暂无炸板数据，恒不触发）；
    2. 非涨停：涨幅 > 3% → 1.0，否则 0.0；
    3. 首板 → 1.5；
    4. 连板 n ≥ 2 → 1.0 + 0.5 n。
    """
    pct = float(pct_chg or 0.0)
    if is_broken and pct < PARAMS["broken_drop_threshold"]:
        return -1.0
    if not streak or streak <= 0:
        return 1.0 if pct > PARAMS["trend_threshold"] else 0.0
    if streak == 1:
        return 1.5
    return 1.0 + 0.5 * streak


def s_score(max_streak):
    """连板杠杆（原版 ``calculate_s_score``）：2 × Sigmoid(n − 3.5) + 1，范围 (1, 3)。"""
    if not max_streak or max_streak <= 0:
        return 1.0
    x = float(max_streak) - PARAMS["sigmoid_offset"]
    # 防溢出：x 很大时 exp(-x) 下溢为 0，结果趋近 3。
    sigmoid = 1.0 / (1.0 + math.exp(-x)) if x > -700 else 0.0
    return 2.0 * sigmoid + 1.0


def sector_factor(member_codes, quotes, limits, name=""):
    """单个板块的 EW-SDM 单日读数。

    - ``member_codes``：板块成分股代码（同花顺 thscode）；
    - ``quotes``：``{code: {"pct": 涨跌幅%, "amount": 成交额(元)}}``，只含快照里有的；
    - ``limits``：``{code: {"streak": 连板数, "seal": 封单额(元)}}``，只含涨停池里的。

    只统计**快照里有行情**的成分股（与原版 ``member_df`` 同口径）；没有行情的
    成分股计入 ``members`` 但不计入权重。
    """
    members = list(dict.fromkeys(member_codes or []))
    priced = [code for code in members if code in quotes]
    pct_clip = PARAMS["pct_clip"]

    weights = []
    weighted_pct = weighted_emotion = 0.0
    up_count = limit_count = 0
    max_streak = 0
    for code in priced:
        quote = quotes[code]
        pct = quote.get("pct")
        pct = 0.0 if pct is None else max(-pct_clip, min(pct_clip, float(pct)))
        amount = max(0.0, float(quote.get("amount") or 0.0))
        limit = limits.get(code) or {}
        streak = int(limit.get("streak") or 0)
        seal = max(0.0, float(limit.get("seal") or 0.0))
        weight = amount + seal
        emotion = emotion_weight(pct, streak, is_broken=False)

        weights.append(weight)
        weighted_pct += weight * pct
        weighted_emotion += weight * emotion
        if pct > 0:
            up_count += 1
        if streak > 0:
            limit_count += 1
        max_streak = max(max_streak, streak)

    total_weight = sum(weights)
    if total_weight > 0:
        momentum = weighted_pct / total_weight
        emotion_diff = weighted_emotion / total_weight
    else:
        momentum = 0.0
        emotion_diff = 0.0
    scale = s_score(max_streak)
    return {
        "name": name,
        "members": len(members),
        "priced": len(priced),
        "momentum": momentum,
        "emotion_diff": emotion_diff,
        "s_score": scale,
        "final_score": momentum * emotion_diff * scale,
        "up_count": up_count,
        "limit_up_count": limit_count,
        "max_streak": max_streak,
        "amount": sum(max(0.0, float(quotes[c].get("amount") or 0.0)) for c in priced),
    }


def quotes_from_market(rows, to_float):
    """全 A 快照原始行 → ``{code: {"pct", "amount"}}``。``to_float`` 由调用方传入（与面板其他字段同口径）。"""
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("thscode") or "").strip()
        if not code:
            continue
        out[code] = {"pct": to_float(row.get("price_change_ratio_pct")),
                     "amount": to_float(row.get("turnover"))}
    return out


def limits_from_pool(pool):
    """涨停池 → ``{code: {"streak", "seal"}}``。池外的股票不在字典里。"""
    out = {}
    for row in pool or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("thscode") or "").strip()
        if not code:
            continue
        out[code] = {"streak": parse_streak(row.get("continue_day_text")),
                     "seal": row.get("seal_money") or 0.0}
    return out


def build_section(sectors, members, quotes, limits, as_of="", pool_size=0,
                  market_limit_up=None, notes=None):
    """91 个板块 → 段落 dict（字段供清单、三端正文共用）。

    ``sectors`` 是 ``[(代码, 名称), ...]``；``members`` 是 ``{代码: [{"thscode", "name"}]}``。
    排序：有行情的按 final_score 从高到低；无行情的排最后、status=no_data。
    """
    rows = []
    for code, name in sectors:
        member_codes = [m.get("thscode") for m in (members.get(code) or [])
                        if isinstance(m, dict) and m.get("thscode")]
        stat = sector_factor(member_codes, quotes, limits, name=name)
        stat["code"] = code
        stat["status"] = "ok" if stat["priced"] else "no_data"
        rows.append(stat)
    rows.sort(key=lambda r: (r["status"] != "ok", -r["final_score"], r["code"]))

    # 覆盖率按「去重后的股票」算：一只股票可能同属多个行业板块。
    unique = {m.get("thscode") for code, _ in sectors
              for m in (members.get(code) or []) if isinstance(m, dict) and m.get("thscode")}
    coverage = {
        "sectors": len(rows),
        "sectors_ok": sum(1 for r in rows if r["status"] == "ok"),
        "members": len(unique),
        "priced": sum(1 for code in unique if code in quotes),
        "pool_size": int(pool_size or 0),
        "market_limit_up": int(market_limit_up or 0),
    }
    return {
        "source": dict(SOURCE),
        "as_of": as_of or "",
        "params": dict(PARAMS),
        "coverage": coverage,
        "notes": list(notes or []),
        "rows": rows,
    }


def build_notes(pool_size, market_limit_up, priced, members):
    """给面板的口径说明。每一条都对应一个真实的数据缺口。"""
    notes = ["炸板/跌停数据未接入：情绪权重不做炸板负反馈，emotion 恒 ≥ 0，"
             "final 的正负跟随 momentum；emotion 为 0 时 final 为 0"]
    if not pool_size and market_limit_up:
        notes.append("涨停池这轮没取到：连板数全部按 0 处理，情绪只看涨幅")
    elif market_limit_up and pool_size < market_limit_up:
        notes.append(f"涨停池只取前 {pool_size} 只，市场涨停 {market_limit_up} 只："
                     "池外涨停按涨幅记（>3% 记 1.0），连板杠杆可能偏低")
    if members:
        notes.append(f"成分股有快照行情 {priced}/{members} 只")
    return notes


def _fmt_pct(value):
    return f"{value:+.2f}%"


def render_text_lines(section, limit=6):
    """纯文本版段落。``limit`` 是强、弱各列几行（周末精简版传 3）。"""
    if not section:
        return []
    lines = ["【板块情绪 · EW-SDM】单日横截面"
             + (f"（{section['as_of']}）" if section.get("as_of") else "")]
    lines.append("  公式：分 = 涨跌幅 × 情绪 × 连板杠杆；涨跌幅与情绪按（成交额+封单额）加权")
    lines.append("  分为 0 = 板块内没有涨停、也没有涨超 3% 的股票（情绪为 0）")
    cov = section.get("coverage") or {}
    lines.append(f"  覆盖：{cov.get('sectors_ok', 0)}/{cov.get('sectors', 0)} 个板块有行情"
                 f" · 成分股 {cov.get('priced', 0)}/{cov.get('members', 0)} 只")
    for note in section.get("notes") or []:
        lines.append(f"  注：{note}")
    ok = [r for r in section.get("rows") or [] if r.get("status") == "ok"]
    if not ok:
        lines.append("  暂无可算板块（成分股或快照缺失）")
        return lines
    lines.append("  强：")
    for row in ok[:limit]:
        lines.append(_row_line(row))
    lines.append("  弱：")
    for row in ok[::-1][:limit]:
        lines.append(_row_line(row))
    return lines


def _row_line(row):
    streak = f"最高{row['max_streak']}板" if row.get("max_streak") else "无连板"
    return (f"    {row['name']} 分 {row['final_score']:+.2f}"
            f" · 涨跌 {_fmt_pct(row['momentum'])} · 情绪 {row['emotion_diff']:.2f}"
            f" · 涨停 {row['limit_up_count']} · {streak}")
