#!/usr/bin/env python3
"""指数择时：ICU 均线 + RSRS/QRS 信号，纯标准库。

策略从哪来
----------
GitHub 仓库 `hugo2046/QuantsPlaybook`（研报复现）两个目录：

- `C-择时类/ICU均线/src/icu_ma.py`：收盘价的滚动「重复中位数（Siegel 1982）」稳健回归，
  取窗口末端拟合值作为 ICU 均线；择时规则见 `ICU_MA.ipynb`：收盘价上穿 ICU 买入，
  下穿卖出（研报：中泰证券《均线才是绝对收益利器——ICU 均线下的择时策略》）。
- `SignalMaker/qrs.py`（`QRSCreator`）与 `C-择时类/QRS择时信号/QRS.ipynb`：
  用最低价 → 最高价的滚动线性回归斜率 β（= σ_high / σ_low × ρ），
  对 M 日 β 做 Z 值标准化，再乘以相关系数的 n 次方作为惩罚项；
  信号上穿 +0.7 开仓、下穿 -0.7 平仓（研报：中金证券《量化择时系列（1）》）。
  默认参数 N=18、M=600、n=2，惩罚项按滚动均值归一（`adjust_regulation=True`）。

这份模块是标准库版本：仓库里的原实现依赖 numpy / pandas / scipy，面板只用标准库。
数值对照（开发时在 900 根模拟日线上跑过，测试里锁的是其中一段小样本的参考值）：
ICU 与 scipy 的 `siegelslopes(method="hierarchical")` 滚动结果逐点一致（最大误差 0）；
QRS 与 `QRSCreator.fit(..., adjust_regulation=True/False)` 逐点一致（最大误差约 1e-14）。

适用范围
--------
只用**指数日线**（收盘价、最高价、最低价）。面板的 13 个宽基指数能拿到这些字段；
QRS 需要至少 N+M-1 = 617 根日线，所以抓取时要比 RRG 更长的历史。
"""

from __future__ import annotations

import math

SOURCE = {
    "icu": {
        "repo": "hugo2046/QuantsPlaybook",
        "file": "C-择时类/ICU均线/src/icu_ma.py",
        "rule": "C-择时类/ICU均线/ICU_MA.ipynb（收盘价上穿买入、下穿卖出）",
        "report": "中泰证券 2023-04《“均线”才是绝对收益利器——ICU 均线下的择时策略》",
    },
    "qrs": {
        "repo": "hugo2046/QuantsPlaybook",
        "file": "SignalMaker/qrs.py（QRSCreator）",
        "rule": "C-择时类/QRS择时信号/QRS.ipynb（±0.7 阈值，N=18、M=600、n=2）",
        "report": "中金证券 2021-01《量化择时系列（1）：金融工程视角下的技术择时艺术》",
    },
}

ICU_WINDOW = 5                    # 与仓库 bt_strategy 的默认 periods 一致
QRS_PARAMS = {"regression_window": 18, "zscore_window": 600, "n": 2,
              "upper": 0.7, "lower": -0.7}
QRS_MIN_BARS = QRS_PARAMS["regression_window"] + QRS_PARAMS["zscore_window"] - 1


def _median(values):
    data = sorted(values)
    if not data:
        raise ValueError("median of empty sequence")
    mid = len(data) // 2
    if len(data) % 2:
        return data[mid]
    return (data[mid - 1] + data[mid]) / 2.0


def siegel_fit(y):
    """重复中位数回归（hierarchical）：x = 0..n-1，返回 (intercept, slope)。

    对每个点 i 取它到其余各点连线的斜率中位数 → 再取这些中位数的中位数作为斜率；
    截距为 median(y_i − slope·x_i)。与 scipy.stats.siegelslopes 的 hierarchical 口径一致。
    """
    n = len(y)
    if n < 2:
        raise ValueError("siegel_fit needs at least 2 points")
    per_point = []
    for i in range(n):
        slopes = [(y[j] - y[i]) / (j - i) for j in range(n) if j != i]
        per_point.append(_median(slopes))
    slope = _median(per_point)
    intercept = _median([y[i] - slope * i for i in range(n)])
    return intercept, slope


def icu_ma(closes, window=ICU_WINDOW):
    """ICU 均线序列（与输入等长，前 window-1 个为 None）。

    窗口末端的值 = intercept + slope · (window − 1)，与 icu_ma.py 的 `siegelslopes_ma` 一致。
    """
    out = [None] * len(closes)
    for t in range(window - 1, len(closes)):
        chunk = closes[t - window + 1: t + 1]
        if any(v is None for v in chunk):
            continue
        intercept, slope = siegel_fit(chunk)
        out[t] = intercept + slope * (window - 1)
    return out


def _pearson(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _pstd(values):
    n = len(values)
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)  # ddof=0，与 numpy 默认一致


def qrs_series(highs, lows, regression_window=18, zscore_window=600, n=2,
               adjust_regulation=True):
    """QRS 信号序列（与 `QRSCreator.fit(..., adjust_regulation=True)` 同口径）。

    返回与输入等长的列表，前面不够的位置为 None。
    步骤：
    1. β_t = σ(high)/σ(low) × ρ(low, high)，窗口 N；
    2. z_t = (β_t − mean_M) / std_M（最近 M 个 β，ddof=0）；
    3. 惩罚项 r_t = ρ_t^n；adjust 时除以 r 的 N 日滚动均值；
    4. signal_t = z_t × r_t（adjust 时为 z_t × r_t / mean_N(r)）。
    """
    if len(highs) != len(lows):
        raise ValueError("highs and lows must have the same length")
    total = len(lows)
    nw, mw = regression_window, zscore_window
    beta = [None] * total
    corr = [None] * total
    for t in range(nw - 1, total):
        lo = lows[t - nw + 1: t + 1]
        hi = highs[t - nw + 1: t + 1]
        if any(v is None for v in lo + hi):
            continue
        rho = _pearson(lo, hi)
        s_lo = _pstd(lo)
        if rho is None or s_lo <= 0:
            continue
        corr[t] = rho
        beta[t] = _pstd(hi) / s_lo * rho

    reg = [None if c is None else c ** n for c in corr]
    out = [None] * total
    for t in range(nw - 1 + mw - 1, total):
        window = beta[t - mw + 1: t + 1]
        if any(b is None for b in window) or beta[t] is None:
            continue
        mean = sum(window) / mw
        sd = _pstd(window)
        if sd <= 0:
            continue
        z = (beta[t] - mean) / sd
        r = reg[t]
        if r is None:
            continue
        if adjust_regulation:
            recent = reg[t - nw + 1: t + 1]
            if any(v is None for v in recent):
                continue
            avg = sum(recent) / nw
            if avg == 0:
                continue
            r = r / avg
        out[t] = z * r
    return out


def cross_events(series, upper, lower):
    """信号穿越阈值的事件，与 backtrader 的 CrossUp / CrossDown 同口径。

    上穿 upper：前一根 ≤ upper 且当根 > upper → 开仓；
    下穿 lower：前一根 ≥ lower 且当根 < lower → 平仓。
    返回 ``[(index, "open"|"close")]``，按持仓状态过滤后（开仓只在空仓时，平仓只在持仓时）。
    """
    events = []
    position = 0
    for t in range(1, len(series)):
        prev, cur = series[t - 1], series[t]
        if prev is None or cur is None:
            continue
        if position == 0 and prev <= upper < cur:
            position = 1
            events.append((t, "open"))
        elif position == 1 and prev >= lower > cur:
            position = 0
            events.append((t, "close"))
    return events, position


def icu_cross_events(closes, icu):
    """收盘价与 ICU 均线的穿越：上穿买入、下穿卖出（同 bt.CrossOver 的符号）。"""
    events = []
    position = 0
    for t in range(1, len(closes)):
        if None in (closes[t], closes[t - 1], icu[t], icu[t - 1]):
            continue
        prev_diff = closes[t - 1] - icu[t - 1]
        diff = closes[t] - icu[t]
        if position == 0 and prev_diff <= 0 < diff:
            position = 1
            events.append((t, "open"))
        elif position == 1 and prev_diff >= 0 > diff:
            position = 0
            events.append((t, "close"))
    return events, position


def _last_valid(values):
    for idx in range(len(values) - 1, -1, -1):
        if values[idx] is not None:
            return idx
    return None


def build_row(code, name, dates, highs, lows, closes):
    """一个指数的择时读数：ICU 状态 + QRS 状态。缺字段时对应项为 None，并给原因。

    ``dates`` 是毫秒时间戳列表（与其它序列等长）。
    """
    row = {"code": code, "name": name, "close": None, "icu": None, "icu_side": None,
           "icu_last_cross": None, "icu_state": None, "qrs": None, "qrs_state": None,
           "qrs_last_event": None, "bars": len(closes), "notes": []}
    if closes and closes[-1] is not None:
        row["close"] = closes[-1]
        row["date_ms"] = dates[-1] if dates else None

    if len(closes) >= ICU_WINDOW and all(c is not None for c in closes[-ICU_WINDOW:]):
        icu = icu_ma(closes, ICU_WINDOW)
        last = _last_valid(icu)
        if last is not None:
            row["icu"] = icu[last]
            row["icu_side"] = "上方" if closes[last] > icu[last] else "下方"
            events, state = icu_cross_events(closes, icu)
            row["icu_state"] = "持有" if state else "空仓"
            if events:
                idx, kind = events[-1]
                row["icu_last_cross"] = {"side": "上穿" if kind == "open" else "下穿",
                                         "date_ms": dates[idx] if dates else None,
                                         "bars_ago": len(closes) - 1 - idx}
    else:
        row["notes"].append("收盘价不足或缺失，ICU 未算")

    if highs is None or lows is None:
        row["notes"].append("没有最高/最低价，QRS 未算")
    elif len(lows) < QRS_MIN_BARS:
        row["notes"].append(f"日线 {len(lows)} 根，QRS 需要 {QRS_MIN_BARS} 根")
    else:
        signal = qrs_series(highs, lows, QRS_PARAMS["regression_window"],
                            QRS_PARAMS["zscore_window"], QRS_PARAMS["n"], True)
        last = _last_valid(signal)
        if last is not None:
            row["qrs"] = signal[last]
            events, state = cross_events(signal, QRS_PARAMS["upper"], QRS_PARAMS["lower"])
            row["qrs_state"] = "持有" if state else "空仓"
            if events:
                idx, kind = events[-1]
                row["qrs_last_event"] = {"side": "开仓" if kind == "open" else "平仓",
                                         "date_ms": dates[idx] if dates else None,
                                         "bars_ago": len(lows) - 1 - idx}
    return row


def build_section(indices, history, as_of="", notes=None):
    """宽基指数 → 择时段落。``history``：``{代码: [[毫秒, 最高, 最低, 收盘], ...]}``（时间升序）。

    没有历史的指数仍然输出一行（收盘为空，notes 写明原因），不静默丢行。
    """
    rows = []
    for code, name in indices:
        bars = history.get(code) or []
        dates = [b[0] for b in bars]
        highs = [b[1] for b in bars]
        lows = [b[2] for b in bars]
        closes = [b[3] for b in bars]
        if not bars:
            row = build_row(code, name, [], None, None, [])
            row["notes"] = ["指数日线没取到"]
        else:
            row = build_row(code, name, dates, highs, lows, closes)
        rows.append(row)
    ok = [r for r in rows if r.get("close") is not None]
    qrs_ok = [r for r in rows if r.get("qrs") is not None]
    return {
        "source": SOURCE,
        "params": {"icu_window": ICU_WINDOW, **QRS_PARAMS},
        "as_of": as_of or "",
        "coverage": {"indices": len(rows), "with_close": len(ok), "with_qrs": len(qrs_ok)},
        "notes": list(notes or []),
        "rows": rows,
    }


def render_text_lines(section, brief=False):
    """纯文本版择时段。每个指数两行：ICU 一行、QRS 一行（周末精简版只留 ICU 行）。"""
    if not section or not section.get("rows"):
        return []
    head = "【指数择时 · ICU 均线 / RSRS(QRS)】"
    if section.get("as_of"):
        head += f"（{section['as_of']}）"
    lines = [head,
             f"  规则：收盘上穿 ICU({ICU_WINDOW}) 买、下穿卖；QRS(N={QRS_PARAMS['regression_window']}、"
             f"M={QRS_PARAMS['zscore_window']}、n={QRS_PARAMS['n']}) 上穿 +{QRS_PARAMS['upper']} 开、"
             f"下穿 {QRS_PARAMS['lower']} 平"]
    for note in section.get("notes") or []:
        lines.append(f"  注：{note}")
    for row in section["rows"]:
        if row.get("close") is None:
            lines.append(f"  {row['name']}：{'；'.join(row.get('notes') or ['无数据'])}")
            continue
        icu = row.get("icu")
        cross = row.get("icu_last_cross")
        cross_text = (f"{cross['side']}{cross['bars_ago']}日前" if cross else "近期无穿越")
        if icu is None:
            lines.append(f"  {row['name']} 收 {row['close']:.2f} · ICU 未算")
        else:
            # ICU 是 5 点 Siegel 拟合，奇数窗口时线常穿过某一根收盘价（约 1/5），显示「贴线」免得误读成「下方」
            side = "贴线" if abs(row["close"] - icu) < 1e-9 else row["icu_side"]
            lines.append(f"  {row['name']} 收 {row['close']:.2f} · ICU {icu:.2f}（{side}）"
                         f" · {row['icu_state']}（{cross_text}）")
        if not brief:
            if row.get("qrs") is not None:
                ev = row.get("qrs_last_event")
                ev_text = (f"{ev['side']}{ev['bars_ago']}日前" if ev else "近期无穿越")
                lines.append(f"      QRS {row['qrs']:+.2f} · {row['qrs_state']}（{ev_text}）")
            elif row.get("notes"):
                lines.append(f"      QRS：{'；'.join(row['notes'])}")
    return lines
