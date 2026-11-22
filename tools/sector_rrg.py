#!/usr/bin/env python3
"""A 股板块轮动：RRG（相对旋转图）——把 GitHub 上的券商研报复现搬成纯标准库。

策略从哪来
----------
GitHub 仓库 `hugo2046/QuantsPlaybook`（约 6.9k ★，「量化研究 · 券商金工研报复现」）里的
`C-择时类/相对旋转图RRG行业轮动`：

- `src/factor_algo.py` —— JdK 比率法 RRG（Julius de Kempenaer 原版口径，中枢 100），
  研报为西部证券 2026-05-25《指数化配置系列研究（6）：相对旋转图 RRG 框架下的行业研究》；
- `src/strategy.py` —— 三个选板块信号，这里复现**信号 A**（`select_by_quadrant`）：
  只保留「领先」象限，按距中心 (100, 100) 的欧氏距离取前 N，不足不补。

那份实现依赖 pandas / numpy；本仓库的面板产物只用标准库（Kindle 那头是纯文件），
所以这里把三个公式原样搬成 list 版：不是「随便找了个动量指标」，而是**逐行对应**
`factor_algo.py` 的 `_smooth_ratio_shift` + 三层递推，连 warm-up 行数都照它的
《RRG 算法说明》核对过（见 `MIN_ROWS` 与 tests/test_sector_rrg.py）。

为什么用它看「今天 A 股板块」
----------------------------
91 个同花顺行业指数原来只有「今日涨跌幅」一个视角：涨得多的排在前面，看不出
是「刚开始转强」还是「强了很久正在熄火」。RRG 用两个正交指标补上这件事：

    指标            含义                            中枢
    RS-Ratio (X)    相对强度的趋势                  100
    RS-Momentum (Y) 相对强度自身的变化率（拐点在先） 100

象限：1 领先(>100,>100) · 2 改善(<100,>100) · 3 滞后(<100,<100) · 4 疲软(>100,<100)。
「改善」是刚起来、「疲软」是刚熄火，这两个才是今天盘面真正在变的地方。

算法（与 factor_algo.py 一一对应）
---------------------------------
    RS       = close / benchmark × 100
    RS-Ratio = MA_smooth(100 × RS_t / RS_{t-ratio})              中枢 100
    RS-Mom   = MA_smooth(100 × RS-Ratio_t / RS-Ratio_{t-mom})    中枢 100

基准按研报 §2.1 口径取**板块等权组合净值**（各板块日收益等权复合，首日 = 1.0），
不是某只宽基。RS-Ratio 只依赖基准的收益比，对基准绝对水平尺度不变。

warm-up 行数（照《RRG 算法说明》的「第二铁律」抄，写错会静默全 None）：

    RS-Ratio : lookback_ratio + smooth − 1
    RS-Mom   : lookback_ratio + lookback_mom + 2 × (smooth − 1)
    长度下界 : lookback_ratio + lookback_mom + 2 × (smooth − 1) + 1

研报默认 220/60/20 要 319 根日 K（约一年半）。同花顺历史 K 线给不够时自动退到
「短窗 60/20/5」（89 根），并在面板上写明用的是哪套参数——**不同参数不混着比**。

这里只有纯函数 + 一个 `build_section()`：取数和排版分别在 `fetch_stocks.py` /
`build_stocks.py`，改模板不用重抓数据。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

# 策略出处：面板底部要照抄，别让读者以为这些数字是本站发明的。
SOURCE = {
    "repo": "hugo2046/QuantsPlaybook",
    "file": "C-择时类/相对旋转图RRG行业轮动/src/factor_algo.py",
    "signal": "C-择时类/相对旋转图RRG行业轮动/src/strategy.py · select_by_quadrant",
    "report": ("西部证券 2026-05-25《指数化配置系列研究（6）："
               "相对旋转图 RRG 框架下的行业研究》"),
}

CENTER = 100.0
# 术语按研报正用语：领先 / 改善 / 滞后 / 疲软（旧译「领涨/走强/领跌/走弱」不要用）。
QUADRANTS = {1: "领先", 2: "改善", 3: "滞后", 4: "疲软"}
QUADRANTS_EN = {1: "leading", 2: "improving", 3: "lagging", 4: "weakening"}
# 象限顺序就是轮动顺序：领先 → 疲软 → 滞后 → 改善 → 领先（顺时针）。
QUADRANT_ORDER = (1, 4, 3, 2)

# 参数档位：先试研报口径，历史 K 线不够再退短窗（并在产物里写清楚）。
PRESETS = (
    {"key": "report", "label": "研报口径 220/60/20",
     "lookback_ratio": 220, "lookback_mom": 60, "smooth_window": 20},
    {"key": "short", "label": "短窗 60/20/5",
     "lookback_ratio": 60, "lookback_mom": 20, "smooth_window": 5},
)
DEFAULT_TRAIL = 6      # 轨迹点数（网页上的尾巴），6 根够看出方向
DEFAULT_TOP_N = 6      # 信号 A 的 top_n（研报默认 6）


def preset_by_key(key):
    """按 key 取参数档；key 为空返回 None（交给 build_section 自动挑）。"""
    for preset in PRESETS:
        if preset["key"] == key:
            return dict(preset)
    return None


def min_rows(preset):
    """这套参数至少要多少根日 K 才不是全 None（见模块文档的「长度下界」）。"""
    return (preset["lookback_ratio"] + preset["lookback_mom"]
            + 2 * (preset["smooth_window"] - 1) + 1)


# --------------------------------------------------------------------------
# 数据对齐
# --------------------------------------------------------------------------

def beijing_day(ms):
    """毫秒时间戳 → 北京时间的交易日文字（YYYY-MM-DD）。"""
    try:
        moment = datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return ""
    return (moment + timedelta(hours=8)).strftime("%Y-%m-%d")


def _close(value):
    """归一成正的 float；0 / null / 非数字一律 None（价格必须严格为正）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def align_history(history, today_prices=None, today_day=None):
    """`{code: [[毫秒, 收盘], ...]}` → (交易日列表, {code: [收盘 | None]})。

    三件事（对应研报的「调用者职责」，原实现交给 pandas）：

    1. **日历对齐**：取所有板块交易日的并集，缺的那天用前值填充（ffill）——
       某个板块停牌/接口漏一天，不该让它整列错位一格；
    2. **今日拼接**：历史 K 线通常不含今天（盘中）或已含今天但收盘价还在动，
       所以用快照的最新价覆盖 `today_day` 那一行；没有今天的行就补一行；
    3. **头部留 None**：ffill 救不了「序列开头就缺」，那几天保持 None，
       warm-up 由公式自己吃掉。
    """
    raw = {}
    days = set()
    for code, bars in (history or {}).items():
        row = {}
        for bar in bars or []:
            if not isinstance(bar, (list, tuple)) or len(bar) < 2:
                continue
            day = beijing_day(bar[0])
            close = _close(bar[1])
            if day and close:
                row[day] = close
        if row:
            raw[str(code)] = row
            days.update(row)

    if today_day:
        prices = {str(code): _close(price)
                  for code, price in (today_prices or {}).items()}
        filled = False
        for code, row in raw.items():
            price = prices.get(code)
            if price:
                row[today_day] = price
                filled = True
        if filled or any(today_day > max(row) for row in raw.values() if row):
            days.add(today_day)

    ordered = sorted(days)
    aligned = {}
    for code, row in raw.items():
        last, values = None, []
        for day in ordered:
            value = row.get(day)
            if value is None:
                value = last
            else:
                last = value
            values.append(value)
        aligned[code] = values
    return ordered, aligned


# --------------------------------------------------------------------------
# 公式：与 factor_algo.py 同形
# --------------------------------------------------------------------------

def equal_weight_benchmark(closes):
    """板块等权组合净值：各板块日收益等权平均后累乘（首日 = 1.0）。

    研报 §2.1 的口径是「行业等权**组合**」，不是「行业指数的点位均值」——
    后者会被点位高的行业主导，不是等权。某板块某天没价（未上市/停牌）就当日
    截面剔除，靠 `mean(skipnan)` 的等价写法处理。
    """
    series = [values for values in closes.values() if values]
    n = len(series[0]) if series else 0
    nav = [1.0] * n
    for i in range(1, n):
        returns = []
        for values in series:
            prev, cur = values[i - 1], values[i]
            if prev and cur and prev > 0:
                returns.append(cur / prev - 1.0)
        step = sum(returns) / len(returns) if returns else 0.0
        nav[i] = nav[i - 1] * (1.0 + step)
    return nav


def compute_rs(closes, benchmark):
    """RS = price / benchmark × 100（宽表版：`{code: [...]}` 进、`{code: [...]}` 出）。

    任一侧缺值 → None。基准必须是正的（等权净值首日 1.0，恒正）。
    """
    out = {}
    for code, price in (closes or {}).items():
        out[code] = [None if close is None or not base else close / base * 100.0
                     for close, base in zip(price, benchmark)]
    return out


def smooth_ratio_shift(values, lookback, smooth):
    """共享算子：`MA_smooth(100 × x_t / x_{t-lookback})`。

    `compute_rs_ratio` / `compute_rs_momentum` 都套这一个算子（原实现抽成
    `_smooth_ratio_shift`），保证公式只落地一处。

    pandas 的 `rolling(window).mean()` 默认 min_periods=window：窗口里有一个
    None 就整体 None。这里照样，**不偷偷用可用点凑平均**——否则 warm-up 期间
    会冒出一堆看起来正常的假值。
    """
    if lookback < 1 or smooth < 1:
        raise ValueError("lookback 与 smooth 都必须是正整数")
    n = len(values)
    ratios = [None] * n
    for i in range(lookback, n):
        base, cur = values[i - lookback], values[i]
        if base and cur:
            ratios[i] = 100.0 * cur / base
    out = [None] * n
    for i in range(smooth - 1, n):
        window = ratios[i - smooth + 1:i + 1]
        if any(value is None for value in window):
            continue
        out[i] = sum(window) / smooth
    return out


def compute_rs_ratio(closes, benchmark, lookback_ratio=220, smooth_window=20):
    """RS-Ratio（X 轴）：相对强度的长期趋势，中枢 100。"""
    need = lookback_ratio + smooth_window
    if len(benchmark) < need:
        raise ValueError(
            f"数据长度不足：RS-Ratio 需 >= {need} 行"
            f"（lookback_ratio={lookback_ratio} + smooth_window={smooth_window}），"
            f"实得 {len(benchmark)}")
    rs = compute_rs(closes, benchmark)
    return {code: smooth_ratio_shift(values, lookback_ratio, smooth_window)
            for code, values in rs.items()}


def compute_rrg(closes, benchmark, lookback_ratio=220, lookback_mom=60,
                smooth_window=20):
    """一次算 (RS-Ratio, RS-Momentum)，复用中间的 RS-Ratio（推荐入口）。

    长度下界照《RRG 算法说明》§3：
    `lookback_ratio + lookback_mom + 2 × (smooth − 1) + 1`。
    momentum 经过**两次** shift+rolling，所以是 `2 × (smooth − 1)` 而不是
    `smooth`——原实现曾经写成 `+ smooth`，300–318 行输入会静默全 NaN。
    """
    need = (lookback_ratio + lookback_mom + 2 * (smooth_window - 1) + 1)
    if len(benchmark) < need:
        raise ValueError(
            "数据长度不足：RS-Momentum 需 >= "
            f"{need} 行（lookback_ratio={lookback_ratio} + "
            f"lookback_mom={lookback_mom} + 2×(smooth_window={smooth_window}−1) + 1），"
            f"实得 {len(benchmark)}")
    ratio_all = compute_rs_ratio(closes, benchmark, lookback_ratio, smooth_window)
    momentum_all = {code: smooth_ratio_shift(values, lookback_mom, smooth_window)
                    for code, values in ratio_all.items()}
    return ratio_all, momentum_all


def quadrant_of(rs_ratio, rs_mom):
    """(RS-Ratio, RS-Momentum) → 1 领先 / 2 改善 / 3 滞后 / 4 疲软；十字上返回 None。

    严格不等号，落在中心轴上（恰为 100）不强行归边——和原实现的
    `classify_quadrant` 同语义。
    """
    if rs_ratio is None or rs_mom is None:
        return None
    if rs_ratio == CENTER or rs_mom == CENTER:
        return None
    if rs_ratio > CENTER:
        return 1 if rs_mom > CENTER else 4
    return 2 if rs_mom > CENTER else 3


def distance_from_center(rs_ratio, rs_mom):
    """到 (100, 100) 的欧氏距离——信号 A 用它排「同象限里谁更强」。"""
    if rs_ratio is None or rs_mom is None:
        return None
    return math.hypot(rs_ratio - CENTER, rs_mom - CENTER)


# --------------------------------------------------------------------------
# 面板段落
# --------------------------------------------------------------------------

def _round(value, digits=2):
    return None if value is None else round(value, digits)


def _pick_preset(days, preset_key=None):
    """挑参数档：指定了就用指定的；否则研报口径 → 短窗，都不够返回 (None, 原因)。"""
    if preset_key:
        preset = preset_by_key(preset_key)
        if not preset:
            return None, [f"未知的 RRG 参数档：{preset_key}"]
        if days < min_rows(preset):
            return None, [f"{preset['label']} 需要至少 {min_rows(preset)} 根板块日 K，"
                          f"只拿到 {days} 根"]
        return preset, []
    for preset in PRESETS:
        if days >= min_rows(preset):
            return dict(preset), []
    return None, [f"板块日 K 只有 {days} 根，短窗参数也要 {min_rows(PRESETS[-1])} 根，"
                  "这一轮不做 RRG"]


def build_section(history, sectors, today_prices=None, today_day=None,
                  today_pct=None, top_n=DEFAULT_TOP_N, trail=DEFAULT_TRAIL,
                  preset_key=None):
    """板块历史 K 线 → 面板用的 RRG 段落。

    :param history: `{thscode: [[毫秒, 收盘], ...]}`（同花顺 historical-batch 归一后的样子）
    :param sectors: `[(thscode, 名字), ...]`，决定顺序与显示名（接口不返回板块名）
    :param today_prices: `{thscode: 快照最新价}`，用来把今天这一行接上（盘中必须）
    :param today_day: 今天的交易日文字（北京时间的 `YYYY-MM-DD`）
    :param today_pct: `{thscode: 涨跌幅%}`，面板展示「今日」那一列
    :returns: `(section, notes)`；数据不够时 `(None, [原因...])`，**不抛异常**——
        板块轮动挂了不该拖垮整块行情面板
    """
    pairs = [(str(code), str(name or code)) for code, name in sectors if code]
    usable = {code: history.get(code) for code, _ in pairs
              if history.get(code)}
    if len(usable) < 3:
        return None, [f"能取到历史 K 线的板块只有 {len(usable)} 个（至少 3 个才算得动 RRG）"]

    days, closes = align_history(usable, today_prices, today_day)
    preset, notes = _pick_preset(len(days), preset_key)
    if not preset:
        return None, notes

    params = {key: preset[key] for key in
              ("lookback_ratio", "lookback_mom", "smooth_window")}
    benchmark = equal_weight_benchmark(closes)
    try:
        ratio_all, mom_all = compute_rrg(closes, benchmark, **params)
    except ValueError as exc:
        return None, [str(exc)]

    last = len(days) - 1
    prev = last - 1
    pct = {str(code): value for code, value in (today_pct or {}).items()}
    rows = []
    for code, name in pairs:
        series = closes.get(code)
        if not series:
            continue
        rs_ratio, rs_mom = ratio_all[code][last], mom_all[code][last]
        quadrant = quadrant_of(rs_ratio, rs_mom)
        if quadrant is None:
            continue
        prev_quadrant = None
        if prev >= 0:
            prev_quadrant = quadrant_of(ratio_all[code][prev], mom_all[code][prev])
        points = []
        for index in range(max(0, last - trail + 1), last + 1):
            if ratio_all[code][index] is None or mom_all[code][index] is None:
                continue
            points.append([round(ratio_all[code][index], 2),
                           round(mom_all[code][index], 2)])
        rows.append({
            "thscode": code,
            "name": name,
            "rs_ratio": _round(rs_ratio),
            "rs_mom": _round(rs_mom),
            "distance": _round(distance_from_center(rs_ratio, rs_mom)),
            "quadrant": quadrant,
            "quadrant_name": QUADRANTS[quadrant],
            "prev_quadrant": prev_quadrant,
            "today_pct": _round(_numeric(pct.get(code)), 2),
            "close": _round(series[last], 2),
            "trail": points,
        })
    if not rows:
        return None, ["所有板块的 RRG 指标都是空值（warm-up 没走完），这一轮不出板块轮动"]

    # 象限内按离中心远近排：同一象限里离得越远越「纯」，也因此是信号 A 的次序。
    rows.sort(key=lambda row: (QUADRANT_ORDER.index(row["quadrant"]),
                               -(row["distance"] or 0.0)))
    counts = {QUADRANTS_EN[q]: 0 for q in QUADRANT_ORDER}
    counts_prev = {QUADRANTS_EN[q]: 0 for q in QUADRANT_ORDER}
    for row in rows:
        counts[QUADRANTS_EN[row["quadrant"]]] += 1
        if row["prev_quadrant"]:
            counts_prev[QUADRANTS_EN[row["prev_quadrant"]]] += 1

    moves = _moves(rows)
    # 信号 A（研报 §2.1）：只留领先象限，按距中心距离取前 top_n，**不补足**。
    picks = [row for row in rows if row["quadrant"] == 1][:max(1, top_n)]

    section = {
        "model": "RRG 相对旋转图（JdK 比率法）",
        "source": dict(SOURCE),
        "preset": preset["label"],
        "preset_key": preset["key"],
        "params": params,
        "benchmark": "板块等权组合净值（研报 §2.1 口径）",
        "samples": len(days),
        "as_of": days[-1] if days else None,
        "prev_as_of": days[prev] if prev >= 0 else None,
        "top_n": top_n,
        "rows": rows,
        "counts": counts,
        "counts_prev": counts_prev,
        "moves": moves,
        "signal": {"name": "信号 A · 领先象限距中心前 "
                           + str(len(picks) or top_n),
                   "quadrants": [1],
                   "picks": [{"thscode": row["thscode"], "name": row["name"],
                              "today_pct": row["today_pct"],
                              "rs_ratio": row["rs_ratio"], "rs_mom": row["rs_mom"]}
                             for row in picks]},
    }
    section["narrative"] = _narrative(section)
    return section, notes


def _moves(rows):
    """象限迁移：今天和前一交易日比，谁进了/出了「领先」（轮动最早的信号）。"""
    return {
        "into_leading": [_name(row) for row in rows
                         if row["quadrant"] == 1 and row["prev_quadrant"] not in (1, None)],
        "out_of_leading": [_name(row) for row in rows
                           if row["quadrant"] != 1 and row["prev_quadrant"] == 1],
        "into_improving": [_name(row) for row in rows
                           if row["quadrant"] == 2 and row["prev_quadrant"] not in (2, None)],
    }


def _numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _name(row):
    return row.get("name") or row.get("thscode") or ""


def _pct_text(value):
    if value is None:
        return "--"
    return f"{value:+.2f}%"


def _join_names(rows, limit=5):
    names = [_name(row) for row in rows[:limit]]
    more = len(rows) - len(names)
    text = "、".join(names)
    return text + (f" 等 {len(rows)} 个" if more > 0 else "")


def _narrative(section):
    """「今日读数」：把象限分布、迁移、信号 A 与今日涨跌对起来讲人话。

    只描述**发生了什么**（位置、迁移、今日涨跌），不写「买入/卖出」——
    面板底部本来就有「不构成投资建议」。
    """
    rows = section["rows"]
    counts = section["counts"]
    prev = section["counts_prev"]
    lines = []

    by_quadrant = {q: [row for row in rows if row["quadrant"] == q]
                   for q in QUADRANT_ORDER}
    head = (f"今日 {len(rows)} 个板块：领先 {counts['leading']} · "
            f"改善 {counts['improving']} · 滞后 {counts['lagging']} · "
            f"疲软 {counts['weakening']}")
    if sum(prev.values()) > 0:
        head += (f"（前一交易日 领先 {prev['leading']} · 改善 {prev['improving']} · "
                 f"滞后 {prev['lagging']} · 疲软 {prev['weakening']}）")
    lines.append(head + "。")

    moves = section["moves"]
    if moves["into_leading"] or moves["out_of_leading"]:
        parts = []
        if moves["into_leading"]:
            parts.append("新进领先 " + "、".join(moves["into_leading"][:5]))
        if moves["out_of_leading"]:
            parts.append("退出领先 " + "、".join(moves["out_of_leading"][:5]))
        lines.append("；".join(parts) + "——象限迁移比涨跌幅更早一步。")
    elif moves["into_improving"]:
        lines.append("新进改善：" + "、".join(moves["into_improving"][:5])
                     + "（相对强度开始回暖，但还没站上中枢）。")

    picks = section["signal"]["picks"]
    if picks:
        lines.append(
            "信号 A（领先象限里离中心最远的前 "
            f"{len(picks)} 个）："
            + "、".join(f"{row['name']} {_pct_text(row['today_pct'])}"
                        f"（RS {row['rs_ratio']:.1f} / 动量 {row['rs_mom']:.1f}）"
                        for row in picks)
            + "。")
    elif by_quadrant[1]:
        lines.append("领先象限今天只有 " + str(len(by_quadrant[1])) + " 个板块，"
                     "信号 A 的数量不足。")
    else:
        lines.append("今天没有板块站在领先象限（RS-Ratio 与动量同时在 100 以上），"
                     "轮动整体在中枢以下。")

    # 今日涨跌幅与 RRG 位置是否互相印证（只比有涨跌幅的）
    priced = [row for row in rows if row["today_pct"] is not None]
    if priced:
        winners = sorted(priced, key=lambda row: -row["today_pct"])[:5]
        aligned = [row for row in winners if row["quadrant"] in (1, 2)]
        if winners:
            text = ("今日涨幅前五：" + "、".join(
                f"{row['name']} {_pct_text(row['today_pct'])}" for row in winners)
                + f" ——其中 {len(aligned)} 个落在领先/改善象限"
                + ("，涨势有轮动承接。" if len(aligned) >= 3 else
                   "，多数是中枢以下的反弹，先当短线看。"))
            lines.append(text)

    weakening = by_quadrant[4]
    if weakening:
        lines.append("疲软象限（RS-Ratio > 100 但动量已跌破中枢，强势在衰减）："
                     + _join_names(weakening, 5)
                     + ("；今日仍在涨的："
                        + "、".join(row["name"] for row in weakening
                                    if (row["today_pct"] or 0) > 0) + "，追高要留一半清醒。"
                        if any((row["today_pct"] or 0) > 0 for row in weakening) else "。"))

    lagging = by_quadrant[3]
    if lagging and priced:
        weakest = sorted([row for row in lagging if row["today_pct"] is not None],
                         key=lambda row: row["today_pct"])[:3]
        if weakest:
            down = [row for row in weakest if row["today_pct"] < 0]
            lines.append("滞后象限里今日最弱：" + "、".join(
                f"{row['name']} {_pct_text(row['today_pct'])}" for row in weakest)
                + ("（相对强度与动量都在中枢下方）。" if down else
                   "——位置最差但今天在反弹，属于超跌修复，不是趋势反转。"))
    return lines


# --------------------------------------------------------------------------
# 演示数据（没有 API Key / 盘中抓不到时先看版式）
# --------------------------------------------------------------------------

# (代码, 名字, 日漂移%, 波动幅度%, 周期)：漂移控制在 ±0.05%/日（一年约 ±12%），
# 幅度/周期各不相同，四个象限都会有人——只求版式像真的，不求像某一天的盘面。
DEMO_SECTORS = (
    ("881121.TI", "半导体", 0.05, 0.60, 34),
    ("881122.TI", "光学光电子", 0.04, 0.50, 41),
    ("881270.TI", "元件", 0.03, 0.45, 53),
    ("881281.TI", "电池", 0.02, 0.45, 61),
    ("881279.TI", "光伏设备", 0.01, 0.35, 47),
    ("881155.TI", "银行", 0.00, 0.20, 71),
    ("881157.TI", "证券", -0.01, 0.45, 29),
    ("881166.TI", "军工装备", 0.02, 0.40, 37),
    ("881273.TI", "白酒", -0.02, 0.25, 67),
    ("881140.TI", "化学制药", -0.03, 0.30, 43),
    ("881145.TI", "电力", -0.03, 0.25, 59),
    ("881153.TI", "房地产", -0.04, 0.55, 31),
    ("881101.TI", "种植业与林业", -0.05, 0.45, 57),
    ("881105.TI", "煤炭开采加工", -0.05, 0.35, 73),
)


def beijing_today(now=None):
    """北京时间的「今天」。

    面板与清单里的数据日一律按北京日期写，所以这里不能用 UTC 日期：UTC 16:00
    之后北京已经是次日，两者会差一天（CI 就是在这个窗口里红的）。
    """
    moment = now or datetime.now(timezone.utc)
    return (moment + timedelta(hours=8)).date()


def demo_days(rows=340, now=None):
    """演示用的交易日序列：从北京今天往回数工作日，最后一根落在最近的工作日。

    周末没有行情：周六/周日跑演示时最后一根是周五。合成价格与日期无关（只按
    步号算），所以日期只影响「数据日」这个标签。
    """
    cursor = beijing_today(now)
    days = []
    while len(days) < rows:
        if cursor.weekday() < 5:          # 只造工作日，和真实交易日历同形
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    return days


def demo_section(trail=DEFAULT_TRAIL, rows=340, now=None):
    """造一段确定性的合成板块行情，走**真实**的 RRG 公式 → 面板演示段落。

    演示数据必须走真公式：否则算错的地方在 --demo 里看不出来，一上真实数据才发现。
    """
    # 从今天往回数「工作日」，再倒过来——最后一根必须是最近的工作日，
    # 否则面板上「数据日」会停在几个月前，演示看着像坏掉。
    days = [int(datetime(day.year, day.month, day.day, 8, 0,
                         tzinfo=timezone.utc).timestamp() * 1000)
            for day in demo_days(rows, now=now)]
    history, today_pct = {}, {}
    for idx, (code, _name_, drift, amp, period) in enumerate(DEMO_SECTORS):
        price, closes = 100.0, []
        for step in range(len(days)):
            ret = drift / 100.0 + (amp / 100.0) * math.sin(
                2 * math.pi * step / period + idx * 0.7)
            price *= 1.0 + ret
            closes.append(round(price, 4))
        history[code] = [[day, close] for day, close in zip(days, closes)]
        today_pct[code] = round((closes[-1] / closes[-2] - 1.0) * 100.0, 2)
    section, _notes = build_section(
        history, [(code, name) for code, name, *_rest in DEMO_SECTORS],
        today_pct=today_pct, trail=trail)
    if section:
        section["demo"] = True
    return section or {}


# --------------------------------------------------------------------------
# 降级口径：单日横截面（日 K 不够、算不出 RRG 时）
# --------------------------------------------------------------------------

# 涨跌幅分布的分档：从右（涨）往左（跌）排，面板上画成一条横条图。
CROSS_BINS = (
    ("≥ +2%", 2.0, None),
    ("+1 ~ +2%", 1.0, 2.0),
    ("0 ~ +1%", 0.0, 1.0),
    ("-1 ~ 0", -1.0, 0.0),
    ("-2 ~ -1%", -2.0, -1.0),
    ("-3 ~ -2%", -3.0, -2.0),
    ("-4 ~ -3%", -4.0, -3.0),
    ("≤ -4%", None, -4.0),
)

# 涨停原因的粗分类：同花顺的 limit_up_reason 是一串「+」连起来的题材词，
# 这里只做「按关键词归堆」的粗颗粒聚合（顺序即优先级，先命中先算）。
THEME_KEYWORDS = (
    ("半导体材料/封测/存储", ("存储", "光刻", "封装", "封测", "电子化学品", "湿电子",
                              "集成电路", "高纯铟", "晶圆", "半导体", "芯片")),
    ("算力/液冷/数据中心", ("算力", "液冷", "数据中心", "IDC", "服务器", "光模块")),
    ("机器人", ("机器人", "人形")),
    ("新能源/电力设备", ("光伏", "储能", "锂", "电池", "风电", "逆变", "变压器",
                         "输配电", "氢能", "充电")),
    ("军工", ("军工", "航空", "航天", "导弹")),
    ("医药", ("医药", "创新药", "疫苗", "医疗", "中药")),
    ("并购重组/国资", ("收购", "重组", "预重整", "国资", "资产出售", "股份回购", "回购")),
)
THEME_OTHER = "其他"
CROSS_TOP = 5


def _bin_of(pct):
    for label, low, high in CROSS_BINS:
        if low is not None and pct < low:
            continue
        if high is not None and pct >= high:
            continue
        return label
    return CROSS_BINS[-1][0]


def _theme_of(reason):
    text = str(reason or "")
    for name, keywords in THEME_KEYWORDS:
        if any(word in text for word in keywords):
            return name
    return THEME_OTHER


def build_cross_section(rows, market=None, indices=None, limit_up=None,
                        as_of=None, why=""):
    """单日收盘的板块横截面读数——RRG 算不出来时的降级口径。

    :param rows: 指数/板块快照（只用 ``is_sector`` 的行）
    :param market: 全 A 统计（涨跌家数、涨跌停、成交额），来自快照的 ``market``
    :param indices: 宽基指数行（只用来算「大盘 vs 小盘」的分化）
    :param limit_up: 涨停池（用来聚合赚钱效应集中在哪些题材）
    :param as_of: 数据日（北京时间 ``YYYY-MM-DD``）
    :param why: 为什么退到横截面（面板上如实写出来，读者才知道 RRG 去哪了）
    :returns: `(section, notes)`；板块太少时 `(None, [原因])`

    这里**不编造 RRG 象限**：单日快照只有「今天涨多少、量多大」，
    没有时间轴就做不出相对强度。所以这里只回答三个问题——
    今天是普涨/普跌还是轮动、量价是否互相印证、赚钱效应集中在哪里。
    """
    sectors = []
    for row in rows or []:
        if not row.get("is_sector"):
            continue
        pct = _numeric(row.get("price_change_ratio_pct"))
        if pct is None:
            continue
        item = {
            "thscode": row.get("thscode"),
            "name": row.get("name") or row.get("thscode") or "",
            "today_pct": round(pct, 2),
            "turnover": _numeric(row.get("turnover")),
        }
        item.update(_ohlc_metrics(row))
        sectors.append(item)
    if len(sectors) < 5:
        return None, [f"有涨跌幅的板块只有 {len(sectors)} 个，样本太少不做横截面"]

    pcts = [row["today_pct"] for row in sectors]
    total = len(pcts)
    mean_val = sum(pcts) / total
    csad_val = sum(abs(p - mean_val) for p in pcts) / total
    counts = {"total": total, "up": sum(1 for p in pcts if p > 0),
              "down": sum(1 for p in pcts if p < 0),
              "flat": sum(1 for p in pcts if p == 0)}
    stats = {
        "median": round(_median(pcts), 2),
        "mean": round(mean_val, 2),
        # 总体标准差（这就是这一天的「离散度」）：普跌日它很小，
        # 结构性行情它才变大——比单看涨跌幅更能说明是轮动还是整体减仓。
        "std": round(math.sqrt(sum((p - mean_val) ** 2 for p in pcts) / total), 2),
        # 国泰君安 CCK 羊群效应模型（hugo2046/QuantsPlaybook）：
        # 横截面绝对偏差 CSAD 相对于 |mean| 偏小时，说明资金在无差别同向交易（羊群踩踏/抢筹）。
        "csad": round(csad_val, 2),
        "herding": bool(abs(mean_val) >= 1.8 and csad_val / max(abs(mean_val), 0.01) < 0.45),
    }
    # 全 A 的涨跌家数与板块口径**分开放**：一个数是 5584 只个股、一个是 91 个行业指数，
    # 混在同一个 up/down 里会写出「91 个板块：涨 649」这种错话。
    breadth = {}
    for key in ("count", "up", "down", "flat", "limit_up", "limit_down",
                "avg_ratio", "turnover"):
        if market and market.get(key) is not None:
            breadth[key] = market.get(key)

    ranked = sorted(sectors, key=lambda row: -row["today_pct"])
    winners = ranked[:CROSS_TOP]
    losers = ranked[-CROSS_TOP:][::-1]

    bins = []
    for label, _low, _high in CROSS_BINS:
        bins.append({"label": label,
                     "count": sum(1 for p in pcts if _bin_of(p) == label)})

    money = _cross_money(sectors, market)
    spread = _index_spread(indices)
    themes = _cross_themes(limit_up)
    intraday = _cross_intraday(sectors, indices, market)
    limit_up_struct = _limit_up_structure(limit_up)
    sentiment = _cross_sentiment(breadth, counts, indices, sectors, limit_up_struct)

    section = {
        "mode": "cross_section",
        "model": "板块横截面（单日收盘）",
        "as_of": as_of,
        "why": why or "",
        "counts": counts,
        "market": breadth,
        "stats": stats,
        "bins": bins,
        "winners": winners,
        "losers": losers,
        # 全线飘绿（或只有一两个红的）时前五名其实也在亏，标题不能还写「领涨」
        "winners_label": ("领涨" if winners and winners[0]["today_pct"] > 0
                          and counts["up"] > 2 else "相对抗跌"),
        "losers_label": ("领跌" if losers and losers[-1]["today_pct"] < 0
                         else "相对最弱"),
        "money": money,
        "spread": spread,
        "themes": themes,
        "intraday": intraday,
        "limit_up_structure": limit_up_struct,
        "sentiment": sentiment,
    }
    section["narrative"] = _cross_narrative(section)
    return section, []


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _cross_money(sectors, market):
    """量价：钱去了哪儿、有没有用。没有历史量，只能看「今天量 × 今天涨跌」。"""
    priced = [row for row in sectors if row.get("turnover")]
    if not priced:
        return {}
    priced.sort(key=lambda row: -row["turnover"])
    total = (market or {}).get("turnover") or sum(row["turnover"] for row in priced)
    # 前后各取多少只：91 个板块时取 20，板块少的时候按三分之一缩（否则两组会重叠，
    # 「前 20 平均 vs 后 20 平均」就成了自己跟自己比）。
    size = max(3, min(20, len(priced) // 3))
    head = priced[:10]
    top_group = priced[:size]
    tail_group = priced[-size:]
    return {
        "total": _round(total, 0),
        "size": size,
        "top": [{"name": row["name"], "today_pct": row["today_pct"],
                 "turnover": _round(row["turnover"], 0),
                 "share": _round(row["turnover"] / total * 100, 1) if total else None}
                for row in head],
        "top_avg": _round(sum(r["today_pct"] for r in top_group) / len(top_group), 2),
        "tail_avg": _round(sum(r["today_pct"] for r in tail_group) / len(tail_group), 2),
        "top_share": _round(priced[0]["turnover"] / total * 100, 1) if total else None,
        "top_name": priced[0]["name"],
        "top_pct": priced[0]["today_pct"],
    }


def _index_spread(indices):
    """大盘 vs 小盘：权重（沪深300/上证50）与小盘（中证1000/创业板指）的分化。"""
    named = {row.get("name"): row for row in indices or [] if not row.get("is_sector")}
    pairs = (("沪深300", "中证1000"), ("上证50", "创业板指"), ("沪深300", "创业板指"))
    for big, small in pairs:
        left, right = named.get(big), named.get(small)
        if not left or not right:
            continue
        big_pct = _numeric(left.get("price_change_ratio_pct"))
        small_pct = _numeric(right.get("price_change_ratio_pct"))
        if big_pct is None or small_pct is None:
            continue
        return {"big": big, "big_pct": round(big_pct, 2),
                "small": small, "small_pct": round(small_pct, 2),
                "spread": round(big_pct - small_pct, 2)}
    return {}


def _ohlc_metrics(row):
    """拆分单根日 K 的隔夜跳空、日内实体、收盘分位（CLV）与振幅。

    依据开源证券/华西证券「日内分时截面动量（APM）」与 QuantsPlaybook「隔夜日间网络因子」：
    A 股隔夜收益主要由散户/隔夜消息驱动（易反转），日内实体与收盘分位由机构交易驱动（具动量）。
    """
    prev = _numeric((row or {}).get("prev_price"))
    op = _numeric((row or {}).get("open_price"))
    hi = _numeric((row or {}).get("high_price"))
    lo = _numeric((row or {}).get("low_price"))
    cl = _numeric((row or {}).get("last_price"))
    if not (prev and op and hi and lo and cl) or prev <= 0 or op <= 0 or hi < lo:
        return {}
    overnight = (op - prev) / prev * 100.0
    intraday = (cl - op) / op * 100.0
    amp = (hi - lo) / prev * 100.0
    clv = (cl - lo) / (hi - lo) if hi > lo else 0.5
    return {
        "overnight_pct": round(overnight, 2),
        "intraday_pct": round(intraday, 2),
        "amplitude_pct": round(amp, 2),
        "clv": round(max(0.0, min(1.0, clv)), 2),
    }


def _cross_intraday(sectors, indices, market):
    """提取盘中真承接（低开高走 + 高 CLV）与冲高派发（放量长上影 / 高开低走）。"""
    with_ohlc = [s for s in sectors or [] if "intraday_pct" in s and "clv" in s]
    if len(with_ohlc) < 3:
        return {}
    total_turnover = (market or {}).get("turnover") or sum(s.get("turnover") or 0 for s in with_ohlc)

    def _entry(s, tag=""):
        share = (round(s["turnover"] / total_turnover * 100.0, 1)
                 if total_turnover and s.get("turnover") else None)
        out = {
            "name": s["name"],
            "today_pct": s["today_pct"],
            "overnight_pct": s["overnight_pct"],
            "intraday_pct": s["intraday_pct"],
            "clv": s["clv"],
            "amplitude_pct": s.get("amplitude_pct"),
            "share": share,
        }
        if tag:
            out["tag"] = tag
        return out

    # 盘中真承接：日内收阳（>= +0.15%）且收盘在全天振幅上半区（CLV >= 0.55），剔除冲高回落留长上影的假阳线
    support_candidates = [
        _entry(s, "低开走阳" if s["overnight_pct"] < -0.1 else "日内走强")
        for s in with_ohlc
        if s["intraday_pct"] >= 0.15 and s["clv"] >= 0.55
    ]
    support_candidates.sort(key=lambda r: (-r["intraday_pct"], -r["clv"]))

    # 冲高回落 / 高开派发：
    # 1) 成交占比 >= 5% 的热门板块却收出长上影（CLV <= 0.35 且振幅 >= 2.0%）
    # 2) 隔夜平/高开（>= -0.15%）但日内实体杀跌 <= -2.0% 且 CLV <= 0.20
    fade_candidates = []
    for s in with_ohlc:
        item = _entry(s)
        share = item.get("share") or 0.0
        if share >= 5.0 and s["clv"] <= 0.35 and (s.get("amplitude_pct") or 0) >= 2.0:
            item["tag"] = "放量长上影"
            fade_candidates.append(item)
        elif s["overnight_pct"] >= -0.15 and s["intraday_pct"] <= -2.0 and s["clv"] <= 0.20:
            item["tag"] = "高开低走派发"
            fade_candidates.append(item)
    fade_candidates.sort(key=lambda r: (-(r.get("share") or 0), r["intraday_pct"]))

    broad_list = []
    wanted = ("科创50", "创业板指", "沪深300", "上证50", "中证1000", "上证指数")
    by_name = {r.get("name"): r for r in indices or [] if not r.get("is_sector")}
    for name in wanted:
        row = by_name.get(name)
        if not row:
            continue
        m = _ohlc_metrics(row)
        if m:
            broad_list.append({
                "name": name,
                "today_pct": round(_numeric(row.get("price_change_ratio_pct")) or 0.0, 2),
                **m,
            })

    return {
        "true_support": support_candidates[:5],
        "fade_warning": fade_candidates[:5],
        "broad": broad_list,
    }


def _parse_board_height(text):
    """从同花顺 ``continue_day_text``（如 ``3连板``、``4天3板``、``首板``）提取连板高度。"""
    raw = str(text or "").strip()
    if not raw or "首板" in raw:
        return 1
    import re as _re
    m = _re.search(r"(\d+)\s*连板", raw)
    if m:
        return max(1, int(m.group(1)))
    m = _re.search(r"\d+\s*天\s*(\d+)\s*板", raw)
    if m:
        return max(1, int(m.group(1)))
    return 1


def _limit_up_structure(limit_up):
    """涨停池微观结构：连板高度梯队、早盘封板占比、封单金额强度。"""
    rows = list(limit_up or [])
    if not rows:
        return {}
    max_board = 1
    max_board_name = ""
    multi_board_count = 0
    early_count = 0
    afternoon_count = 0
    seals = []
    for r in rows:
        b = _parse_board_height(r.get("continue_day_text"))
        if b >= 2:
            multi_board_count += 1
        if b > max_board or (b == max_board and not max_board_name):
            max_board = b
            max_board_name = str(r.get("name") or r.get("thscode") or "").replace(" ", "")
        t = str(r.get("limit_up_time") or "").strip()
        if t:
            if t <= "10:00:00":
                early_count += 1
            elif t >= "13:00:00":
                afternoon_count += 1
        seal = _numeric(r.get("seal_money"))
        if seal is not None and seal >= 0:
            seals.append(seal)

    total = len(rows)
    total_seal_yi = round(sum(seals) / 1e8, 2) if seals else None
    median_seal_wan = round(_median(seals) / 1e4, 0) if seals else None
    return {
        "total": total,
        "max_board": max_board,
        "max_board_name": max_board_name,
        "multi_board_count": multi_board_count,
        "first_board_count": total - multi_board_count,
        "early_count": early_count,
        "afternoon_count": afternoon_count,
        "total_seal_yi": total_seal_yi,
        "median_seal_wan": median_seal_wan,
    }


def _cross_sentiment(breadth, sector_counts, indices, sectors, limit_up_struct):
    """合成 0–100 盘面情绪温度计（参考潮汐 Tide 五分量与涨跌停情绪择时模型）。

    五个分量（满分 100）：
      1. 宽度分 (0–25)：全 A 上涨占比 up / (up + down)
      2. 极值分 (0–25)：涨停 / (涨停 + 跌停)
      3. 日内承接分 (0–20)：宽基指数（或板块）的平均收盘分位 CLV
      4. 连板梯队分 (0–15)：最高连板高度 × 2.5 + 连板家数加分
      5. 封板质量分 (0–15)：10:00 前早盘封板占比 + 封单中位数强度
    """
    up = (breadth or {}).get("up")
    down = (breadth or {}).get("down")
    if isinstance(up, (int, float)) and isinstance(down, (int, float)) and (up + down) > 0:
        breadth_score = round(25.0 * up / (up + down), 1)
    else:
        s_up = (sector_counts or {}).get("up") or 0
        s_tot = max(1, (sector_counts or {}).get("total") or 1)
        breadth_score = round(25.0 * s_up / s_tot, 1)

    lu = (breadth or {}).get("limit_up")
    ld = (breadth or {}).get("limit_down")
    if isinstance(lu, (int, float)) and isinstance(ld, (int, float)) and (lu + ld) > 0:
        extreme_score = round(25.0 * lu / (lu + ld), 1)
    else:
        extreme_score = round(breadth_score, 1)

    clvs = []
    for r in indices or []:
        if not r.get("is_sector"):
            m = _ohlc_metrics(r)
            if "clv" in m:
                clvs.append(m["clv"])
    if not clvs:
        clvs = [s["clv"] for s in sectors or [] if "clv" in s]
    avg_clv = sum(clvs) / len(clvs) if clvs else 0.5
    support_score = round(20.0 * avg_clv, 1)

    if limit_up_struct and limit_up_struct.get("total"):
        mb = limit_up_struct.get("max_board") or 1
        mc = limit_up_struct.get("multi_board_count") or 0
        ladder_score = min(15.0, round(mb * 2.5 + min(5.0, mc * 1.0), 1))
        early_ratio = (limit_up_struct.get("early_count") or 0) / max(1, limit_up_struct["total"])
        med_wan = limit_up_struct.get("median_seal_wan") or 0.0
        seal_score = min(15.0, round(10.0 * early_ratio + min(5.0, (med_wan / 1000.0) * 5.0), 1))
    else:
        ladder_score = round(7.5 * (breadth_score / 12.5), 1)
        seal_score = round(7.5 * (extreme_score / 12.5), 1)
        ladder_score = max(0.0, min(15.0, ladder_score))
        seal_score = max(0.0, min(15.0, seal_score))

    score = int(round(breadth_score + extreme_score + support_score + ladder_score + seal_score))
    score = max(0, min(100, score))
    if score < 25:
        level = "冰点"
    elif score < 40:
        level = "防守"
    elif score < 60:
        level = "中性"
    elif score < 75:
        level = "活跃"
    else:
        level = "过热"

    divergence = ""
    if (breadth_score + extreme_score) <= 12.0 and support_score >= 12.0:
        divergence = "指数托底、个股冰点"
    elif (breadth_score + extreme_score) >= 35.0 and support_score <= 8.0:
        divergence = "个股活跃、权重冲高回落"

    return {
        "score": score,
        "level": level,
        "divergence": divergence,
        "components": {
            "breadth": breadth_score,
            "extremes": extreme_score,
            "support": support_score,
            "ladder": ladder_score,
            "seal": seal_score,
        },
    }


def limit_up_themes(limit_up, limit=None):
    """涨停题材聚合（对外入口）：``[{"name","count","examples"}, …]``，按条数降序。

    横截面段落与「周末精简版」的涨停池都用它：周末只写「题材分布」，
    不再把 20 只票逐只列一遍。
    """
    ordered = _cross_themes(limit_up)
    return ordered if limit is None else ordered[:limit]


def _cross_themes(limit_up):
    """涨停题材聚合：赚钱效应集中在哪条链上（粗分类，只做线索）。"""
    groups = {}
    for row in limit_up or []:
        name = _theme_of(row.get("limit_up_reason"))
        group = groups.setdefault(name, {"name": name, "count": 0, "examples": []})
        group["count"] += 1
        stock = row.get("name") or row.get("thscode")
        if stock and len(group["examples"]) < 3:
            group["examples"].append(str(stock).replace(" ", ""))
    ordered = sorted(groups.values(), key=lambda item: -item["count"])
    return ordered


def _cross_narrative(section):
    """横截面读数：只讲当天能证明的事（普跌/普涨、量价、题材），不假装有象限。"""
    counts = section.get("counts") or {}
    stats = section.get("stats") or {}
    total = counts.get("total") or 0
    lines = []
    if not total:
        return lines
    red, green = counts.get("up", 0), counts.get("down", 0)
    median = stats.get("median")
    std = stats.get("std")
    breadth = section.get("market") or {}
    head = (f"{total} 个板块：红 {red} · 绿 {green} · 平 {counts.get('flat', 0)}，"
            f"中位数 {_pct_text(median)}、离散度（标准差）{std:.2f}")
    avg = breadth.get("avg_ratio")
    if avg is not None and median is not None:
        if abs(median - avg) < 0.3:
            head += f"；全 A 平均 {_pct_text(avg)}，板块与个股几乎同步——跌（涨）是系统性的"
        elif median > avg:
            head += f"；全 A 平均 {_pct_text(avg)}，行业指数比个股抗跌"
        else:
            head += f"；全 A 平均 {_pct_text(avg)}，行业层面比个股更弱"
    lines.append(head + "。")

    limit_up, limit_down = breadth.get("limit_up"), breadth.get("limit_down")
    if limit_down and limit_up is not None and limit_down >= max(limit_up * 1.5, 10):
        lines.append(f"涨停 {limit_up} / 跌停 {limit_down}：跌停是涨停的 "
                     f"{limit_down / max(limit_up, 1):.1f} 倍，杀跌的尾部比赚钱的尾部粗，"
                     "情绪面还在恶化。")
    elif limit_up and limit_down is not None and limit_up >= max(limit_down * 1.5, 20):
        lines.append(f"涨停 {limit_up} / 跌停 {limit_down}：赚钱效应明显强于亏钱效应。")

    if green and green / total >= 0.8:
        lines.append("九成板块翻绿、离散度还不到 1.5 → 这是整体减仓式的普跌，"
                     "不是板块间搬家；这种日子里「谁抗跌」多是仓位效应，别当成风格切换。")
    elif red and red / total >= 0.8:
        lines.append("九成板块翻红、离散度不大 → 是普涨（风险偏好整体回暖），"
                     "不是某条主线的独立行情。")
    elif std is not None and std >= 2.0:
        lines.append("板块之间拉得很开（离散度 ≥ 2）→ 有钱在做板块间切换，"
                     "这种日子才值得看谁在「刚转强」。")
    else:
        lines.append("涨跌参半、离散度不大 → 轮动不明显的震荡日。")

    spread = section.get("spread") or {}
    if spread:
        tone = "权重抗跌、小盘杀跌" if spread["spread"] > 0 else "小盘活、权重拖后腿"
        lines.append(f"{spread['big']} {_pct_text(spread['big_pct'])} vs "
                     f"{spread['small']} {_pct_text(spread['small_pct'])}"
                     f"（差 {spread['spread']:+.2f} 个百分点）→ {tone}。")

    money = section.get("money") or {}
    if money:
        gap = (money["top_avg"] or 0) - (money["tail_avg"] or 0)
        if gap >= 0.5:
            verdict = "钱留在成交活跃的板块里，量价互相印证"
        elif gap <= -0.5:
            verdict = "成交越活跃跌得越多，量是出货不是承接"
        else:
            verdict = "成交额前列与末列涨跌几乎一样，量没有提供避风港"
        size = money.get("size") or 20
        lines.append(f"成交额前 {size} 平均 {_pct_text(money['top_avg'])}、"
                     f"后 {size} 平均 {_pct_text(money['tail_avg'])}——{verdict}；"
                     f"最大的一块是{money['top_name']}，占两市成交 "
                     f"{money['top_share']:.1f}%，今日 {_pct_text(money['top_pct'])}。")

    themes = section.get("themes") or []
    if themes:
        best = themes[0]
        text = (f"涨停 {sum(t['count'] for t in themes)} 只里，"
                f"{best['count']} 只集中在「{best['name']}」")
        if best.get("examples"):
            text += "（" + "、".join(best["examples"]) + "）"
        others = [t for t in themes[1:] if t["count"] >= 2]
        if others:
            text += "；其次 " + "、".join(f"{t['name']} {t['count']} 只" for t in others[:2])
        lines.append(text + "——赚钱效应就看这几条链。")

    intraday = section.get("intraday") or {}
    support_list = intraday.get("true_support") or []
    fade_list = intraday.get("fade_warning") or []
    if support_list or fade_list:
        parts = []
        if support_list:
            picked = "、".join(
                f"{r['name']}（隔夜 {_pct_text(r['overnight_pct'])} → 日内 {_pct_text(r['intraday_pct'])}，分位 {r['clv']:.2f}）"
                for r in support_list[:3]
            )
            parts.append("盘中低开走阳真承接：" + picked)
        if fade_list:
            faded = "、".join(
                f"{r['name']}（{r.get('tag') or '冲高回落'}，日内 {_pct_text(r['intraday_pct'])}，分位 {r['clv']:.2f}）"
                for r in fade_list[:3]
            )
            parts.append("冲高回落/派发预警：" + faded)
        lines.append("隔夜 vs 日内（APM 日内动量拆分）：" + "；".join(parts) + "。")

    sentiment = section.get("sentiment") or {}
    lu_struct = section.get("limit_up_structure") or {}
    if sentiment.get("score") is not None:
        comp = sentiment.get("components") or {}
        div = f" · {sentiment['divergence']}" if sentiment.get("divergence") else ""
        sent_line = (
            f"情绪温度 {sentiment['score']}/100（{sentiment.get('level')}{div}："
            f"宽度 {comp.get('breadth', 0):.0f}/25 · 极值 {comp.get('extremes', 0):.0f}/25 · "
            f"日内承接 {comp.get('support', 0):.0f}/20 · 连板 {comp.get('ladder', 0):.0f}/15 · "
            f"封板 {comp.get('seal', 0):.0f}/15）"
        )
        if stats.get("herding"):
            sent_line += f"；CCK 横截面绝对偏差 CSAD {stats.get('csad'):.2f}，呈同向羊群交易"
        if lu_struct.get("total"):
            sent_line += (
                f"；最高 {lu_struct['max_board']} 连板"
                + (f"（{lu_struct['max_board_name']}）" if lu_struct.get("max_board_name") else "")
                + f"，连板 {lu_struct['multi_board_count']} / 首板 {lu_struct['first_board_count']}"
                + (f"，封单合计 {lu_struct['total_seal_yi']:.2f} 亿" if lu_struct.get("total_seal_yi") is not None else "")
            )
        lines.append(sent_line + "。")

    return lines
