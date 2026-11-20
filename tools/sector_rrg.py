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


def demo_section(trail=DEFAULT_TRAIL, rows=340):
    """造一段确定性的合成板块行情，走**真实**的 RRG 公式 → 面板演示段落。

    演示数据必须走真公式：否则算错的地方在 --demo 里看不出来，一上真实数据才发现。
    """
    # 从今天往回数「工作日」，再倒过来——最后一根必须是今天，
    # 否则面板上「数据日」会停在几个月前，演示看着像坏掉。
    cursor = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0,
                                                microsecond=0)
    days = []
    while len(days) < rows:
        if cursor.weekday() < 5:          # 只造工作日，和真实交易日历同形
            days.append(int(cursor.timestamp() * 1000))
        cursor -= timedelta(days=1)
    days.reverse()
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
