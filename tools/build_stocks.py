#!/usr/bin/env python3
"""把 stocks_data.json 排成全量/轻量清单、日线 sidecar、Kindle 文本、网页与 EPUB。

    python tools/build_stocks.py --data stocks_data.json --out site
    python tools/build_stocks.py --demo --out site   # 没 Key 时先看版式

产物都在 ``site/`` 下，跟着仓库走：

- ``stocks.json`` —— 清单 + 数据二合一：``generated`` 是发布时间，``data_hash`` 是正文指纹
  （变了才重下正文），``doc`` 指向 ``stocks.txt``，``epub`` 指向 ``stocks.epub``，
  ``data_time`` 是快照的数据时间（不是生成时间），``data_hash`` 也供工作流判断
  「行情没变就不提交」；
- ``stocks-manifest.json`` —— Kindle 多镜像择优用的轻量清单，不携带全量行情；
- ``stocks-leaders.json`` —— A/H 龙头约 130 根完整日线，主清单只保留 26 周压缩趋势；
- ``stocks.txt`` —— Kindle 纯文本面板（电子墨水友好：白底黑字、▲▼ + 正负号，
  不靠颜色传信息）；
- ``stocks.html`` —— 浏览器卡片仪表盘：指数卡 + 自选股卡（带近 20 日迷你走势图）
  + 涨停表，每 ``POLL_SECONDS``（7 分钟）拉一次 ``stocks.json``，``generated`` 变了就重绘；
  拉不到就保留上一次的数据并标 OFFLINE；
- ``stocks.epub`` —— Kindle 字模式卡片版：同一套数据排成表格卡片，交给
  KOReader 阅读器显示（crengine 不认 flex/grid/SVG，所以迷你图只出现在网页面板）。

三端可读正文最后都多一段【板块轮动 · RRG】：数据由 ``tools/sector_rrg.py`` 算好放进
``stocks_data.json`` 的 ``sector_rrg`` 字段（策略来自 GitHub ``hugo2046/QuantsPlaybook``，
见那个模块的文档），这里只负责排版——网页除了表格还画一张灰度四象限图，
Kindle 纯文本/EPUB 只有文字与表格。

抓取只负责取回并归一化（``tools/fetch_stocks.py``），这里只负责判断与输出：
数据条数太少、清单解析不过都不写文件，不覆盖上一份。
"""

import argparse
import hashlib
import html as html_mod
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ah_leaders  # noqa: E402
import ew_sdm  # noqa: E402
import timing_quants  # noqa: E402
import jrglobal_radar  # noqa: E402
import sector_rrg  # noqa: E402

TITLE = "同花顺行情面板"
DATA_SCHEMA = 2  # v2：轻量清单 + A/H 龙头半年量价 sidecar
DISCLAIMER = "仅供信息展示，不构成投资建议"
BEIJING = timezone.utc  # 北京时间 = UTC+8 固定偏移，不用 tzdata
# 服务端抓取间隔（分钟）：与 .github/workflows/refresh-stocks.yml 的 cron 一致
# （交易时段每 15 分钟一轮）。以前写 5 分钟，但 GitHub 会延迟/丢弃 */5 的调度，
# 实际几小时才跑一次；现在 15 分钟 + refresh-stocks-watchdog.yml 补跑。
SERVER_MINUTES = 15
# 设备端检查间隔：和服务端节奏对齐，7 分钟一查不会漏掉新快照，
# 也不会把同一个 generated 反复拉（每拉一次都要唤醒设备）。
POLL_SECONDS = 420


def poll_text():
    """POLL_SECONDS → 面板上显示的「7分钟」/「90秒」（前面留一个空格由调用处决定）。"""
    if POLL_SECONDS % 60 == 0:
        return f"{POLL_SECONDS // 60}分钟"
    return f"{POLL_SECONDS}秒"


def beijing_text(epoch_ms, fmt="%Y-%m-%d %H:%M:%S"):
    """毫秒时间戳 → 北京时间文字；None 时返回空串。"""
    if not isinstance(epoch_ms, (int, float)):
        return ""
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    from datetime import timedelta
    return (moment + timedelta(hours=8)).strftime(fmt)


def now_beijing_text():
    from datetime import timedelta
    moment = datetime.now(timezone.utc) + timedelta(hours=8)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# 行情判活：开盘前后同花顺会给「空壳快照」（价/开高低/量全是 0 或 null）。
# 这类数据既不该显示成 0.00 手，也不该覆盖上一份有效面板。
STALE_MINUTES = 20    # data_time 落后超过这么多分钟就标 STALE
SESSION_START_MIN = 9 * 60 + 15   # 北京时间 09:15 之前算「未开盘」（09:15 集合竞价）
SESSION_END_MIN = 15 * 60 + 5     # 15:05 之后算「已收盘」
LUNCH_START_MIN = 11 * 60 + 30    # A 股午休 11:30–13:00
LUNCH_END_MIN = 13 * 60
# 收盘后数据停住是正常的：只有「跨过一个完整交易日还没新数据」才算真过期。
# 24 小时留点余量，覆盖「周末/长假后第一个交易日早盘还没抓到」这种情况。
TRADING_DAY_MINUTES = 24 * 60


def total_volume(payload):
    """整份快照的成交量合计（None 当 0）。全 0 基本就是「今天还没成交」。"""
    total = 0.0
    hk = (payload.get("hk") or {}).get("rows") or []
    for row in (payload.get("indices") or []) + (payload.get("stocks") or []) + hk:
        volume = row.get("volume")
        if isinstance(volume, (int, float)):
            total += volume
    return total


def priced_count(payload):
    """有真实最新价的条目数（0 与 None 都不算）。"""
    count = 0
    hk = (payload.get("hk") or {}).get("rows") or []
    for row in (payload.get("indices") or []) + (payload.get("stocks") or []) + hk:
        value = row.get("last_price")
        if isinstance(value, (int, float)) and value != 0:
            count += 1
    return count


def empty_snapshot(payload):
    """开盘前的空壳快照：成交量为 0，并且过半条目没有最新价。

    只看价格不行——开盘前指数还会带着「前收盘」那个点位；只看成交量又太严——
    停牌股本来就没有成交。两个条件同时成立才当空壳。
    """
    rows = len(payload.get("indices") or []) + len(payload.get("stocks") or [])
    rows += len(((payload.get("hk") or {}).get("rows")) or [])
    if rows == 0:
        return True
    no_volume = total_volume(payload) == 0
    # 全市场概览取到了成交额 → 这一轮显然不是空壳
    if ((payload.get("market") or {}).get("turnover") or 0) > 0:
        return False
    all_empty = priced_count(payload) == 0
    # 指数会带着「前收盘」那个点位（所以 priced_count > 0），个股却全是 null；
    # 两个条件任一成立，再加上「成交量全 0」，就是空壳。
    return no_volume and (all_empty or priced_count(payload) * 2 < rows)


def today_has_values(payload):
    """有没有「今天成交过」的迹象：有成交量，或者绝大多数条目都有最新价。"""
    return not empty_snapshot(payload)


def row_is_empty(row):
    """这一行是不是空壳：没有任何价格。"""
    return all(not isinstance(row.get(key), (int, float)) or row.get(key) == 0
               for key in ("last_price", "open_price", "high_price", "low_price"))


# A 股法定节假日休市日（北京时间 YYYY-MM-DD，只列周一至周五；周末本身已走 weekday>=5）。
# 为什么不用「数据超过一天没更新」来猜节假日：抓取一旦因为网络/配额停了一天，
# 工作日也会被误判成节假日而切到精简版——把故障藏成了「休市」。
CN_HOLIDAYS = frozenset({
    # 2026（元旦、春节、清明、劳动节、端午、中秋、国庆）
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    # 2027（元旦、春节、清明、劳动节、端午、中秋、国庆）
    "2027-01-01",
    "2027-02-05", "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11", "2027-02-12",
    "2027-04-05",
    "2027-05-03", "2027-05-04", "2027-05-05",
    "2027-06-09",
    "2027-09-15",
    "2027-10-01", "2027-10-04", "2027-10-05", "2027-10-06", "2027-10-07",
})


def session_label(data_ms):
    """北京时间 → 盘中 / 午休 / 未开盘 / 已收盘 / 周末休市 / 节假日休市；不知道就返回空串。

    午休单列出来：11:45 抓到的那份快照，成交量跟 11:30 一模一样，标成「盘中」
    会让人以为行情还在动。
    """
    if not isinstance(data_ms, (int, float)):
        return ""
    beijing_dt = datetime.fromtimestamp(data_ms / 1000, tz=timezone.utc) + timedelta(hours=8)
    minutes = beijing_dt.hour * 60 + beijing_dt.minute
    if beijing_dt.weekday() >= 5:
        return "周末休市"
    if beijing_dt.strftime("%Y-%m-%d") in CN_HOLIDAYS:
        return "节假日休市"
    if minutes < SESSION_START_MIN:
        return "未开盘"
    if minutes > SESSION_END_MIN:
        return "已收盘"
    if LUNCH_START_MIN <= minutes < LUNCH_END_MIN:
        return "午休"
    return "盘中"


def age_text(data_ms, now_ms=None):
    """快照到现在多久：'' / '3 分钟前' / '2 小时前' / '3 天前'（负数返回空串）。"""
    if not isinstance(data_ms, (int, float)):
        return ""
    now_ms = now_ms if isinstance(now_ms, (int, float)) else int(
        datetime.now(timezone.utc).timestamp() * 1000)
    seconds = (now_ms - data_ms) / 1000
    if seconds < 0:
        return ""
    if seconds < 90:
        return "刚刚"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} 分钟前"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.0f} 小时前"
    return f"{hours / 24:.0f} 天前"


def is_stale(data_ms, now_ms=None):
    """「该有数据却没有」才算过期。

    以前是简单比「现在 - 数据时间 > 20 分钟」，于是收盘后、周末、深夜全都会
    被标成 STALE——可那段时间行情本来就不动，这是误报（凌晨一点看面板，
    显示「STALE 超过 20 分钟没更新」只会让人以为抓取坏了）。

    现在的规则：
      · 盘中（含午休）：数据超过 STALE_MINUTES 就算过期（真该在更新）；
      · 未开盘：只要数据是**今天或更晚**的就不算过期（开盘前本来没新数据）；
      · 已收盘/周末：只要数据是**今天（或最近一个交易日）或更晚**就不算过期；
      · 跨过一个交易日还没数据 → 才算真的没跟上（is_stale 为 True）。
    """
    if not isinstance(data_ms, (int, float)):
        return False
    now_ms = now_ms if isinstance(now_ms, (int, float)) else int(
        datetime.now(timezone.utc).timestamp() * 1000)
    age_min = (now_ms - data_ms) / 60000
    if age_min <= STALE_MINUTES:
        return False
    label = session_label(data_ms)          # 数据「属于」哪个时段
    if label in ("盘中", "午休"):
        return True                         # 盘中还停在旧数据 = 真的没跟上
    # 未开盘 / 已收盘 / 周末 / 空：看数据是不是「这个交易日」的
    return age_min > TRADING_DAY_MINUTES


def status_line(payload, now_ms=None):
    """抬头那一行：数据时间 + 距今 + 盘中/未开盘/已收盘 + STALE 警告。"""
    data_ms = payload.get("data_timestamp")
    if not isinstance(data_ms, (int, float)):
        return "数据 无"
    stamp = beijing_text(data_ms)
    if payload.get("demo"):
        # 演示数据的时间是官方文档里的示例，标「多久以前」只会吓人。
        return f"数据 {stamp}（演示数据，非实时）"
    parts = [f"数据 {stamp}"]
    age = age_text(data_ms, now_ms)
    if age:
        parts.append(age)
    label = session_label(data_ms)
    if label:
        parts.append(label)
    text = " · ".join(parts)
    if is_stale(data_ms, now_ms):
        text += f" · STALE 超过 {STALE_MINUTES} 分钟没更新（定时任务可能被延迟，见 docs）"
    elif label in ("已收盘", "周末休市", "节假日休市"):
        # 收盘后数据本来就不再变，别让人以为「时间没跟上」。
        text += "（收盘后不再更新）"
    return text


# ---------------------------------------------------------------------------
# 周末精简版：不在交易、也没有新行情时，面板不该再把 91 个板块、13 个指数、
# 20 只涨停逐条排列一遍。周末翻 Kindle 的人要的是「上周怎么收的、周一先看什么」，
# 而不是把周五那张表再读第三遍。
#
# 规则（与用户确认过的口径）：
#   · 指数全留（13 行，是「往哪儿躲」的坐标系）；
#   · 板块只留抗跌前 5 / 杀跌前 5，其余换成一行「见 stocks.html」；
#   · 涨停池只留条数 + 题材分布（谁涨停了不必在周末背）；
#   · RRG / 横截面只留抬头 + 前两三句结论；
#   · JSON 不动（网页、插件、检查脚本读的是全量数据）。
# ---------------------------------------------------------------------------

BRIEF_SECTOR_ROWS = 5        # 抗跌前 N / 杀跌前 N

def brief_reason(payload=None, now_ms=None):
    """要不要出「周末精简版」，返回原因（空串＝完整版）。

    只在**周末**与**法定节假日**触发；工作日的盘前、盘中、午休、收盘后一律保留完整版
    （演示数据默认也走完整版，除非手动传 ``--brief``）。
    """
    if (payload or {}).get("demo"):
        return ""
    now_ms = (now_ms if isinstance(now_ms, (int, float))
              else int(datetime.now(timezone.utc).timestamp() * 1000))
    beijing = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) + timedelta(hours=8)
    if beijing.weekday() >= 5:
        return "周末休市"
    if beijing.strftime("%Y-%m-%d") in CN_HOLIDAYS:
        return "节假日休市"
    return ""


def _generated_ms(generated):
    """把 manifest 的 generated（UTC ISO 或北京时间串）转成毫秒时间戳；解析不出就用当前时间。"""
    if isinstance(generated, (int, float)):
        return int(generated)
    text = str(generated or "").strip()
    for fmt, is_utc in (("%Y-%m-%dT%H:%M:%SZ", True), ("%Y-%m-%d %H:%M:%S", False)):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            if not is_utc:
                dt = dt - timedelta(hours=8)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def brief_view(payload, now_ms=None, mode=None):
    """精简版的清单字段；返回 None 表示出完整版。

    :param mode: ``None`` 自动、``"brief"`` 强制精简、``"full"`` 强制完整
                 （节假日想手动切，或想看一眼完整版时用）。
    """
    if mode == "full":
        return None
    reason = "手动指定" if mode == "brief" else brief_reason(payload, now_ms)
    if not reason:
        return None
    data_ms = (payload or {}).get("data_timestamp")
    return {
        "mode": "brief",
        "reason": reason,
        "as_of": beijing_text(data_ms, "%Y-%m-%d") if isinstance(data_ms, (int, float)) else None,
        "sector_rows": BRIEF_SECTOR_ROWS,
        # 涨停题材分布：网页直接用这份，免得 JS 再抄一遍关键词表（口径只有一份）
        "themes": sector_rrg.limit_up_themes((payload or {}).get("limit_up") or []),
    }


def _wrap_cells(cells, width=46, indent="  "):
    """把若干短句用 ` · ` 连起来并按显示宽度折行；折行处不留下悬空的分隔符。"""
    lines, current = [], ""
    for cell in cells:
        candidate = f"{current} · {cell}" if current else cell
        if current and _display_width(indent + candidate) > width:
            lines.append(indent + current)
            current = cell
        else:
            current = candidate
    if current:
        lines.append(indent + current)
    return lines


def brief_banner(brief, sector_total=0):
    """精简版开头那一行：说清楚删了什么、完整版在哪。"""
    if not brief:
        return ""
    rest = max(0, sector_total - 2 * int(brief.get("sector_rows") or BRIEF_SECTOR_ROWS))
    tail = f"，其余 {rest} 个板块见 stocks.html" if rest else ""
    return (f"【周末精简版 · {brief.get('reason')}】指数全留；板块只列抗跌/杀跌各 "
            f"{brief.get('sector_rows') or BRIEF_SECTOR_ROWS} 个{tail}；"
            "涨停池只留条数与题材；分析段只留结论")


def limit_up_theme_text(limit_up, limit=4):
    """涨停池的题材分布：`半导体材料/封测/存储 11只 · 算力… 3只 · 其他 4只`。"""
    groups = sector_rrg.limit_up_themes(limit_up)
    if not groups:
        return ""
    cells = [f"{group['name']} {group['count']}" for group in groups[:limit]]
    rest = sum(group["count"] for group in groups[limit:])
    if rest:
        cells.append(f"{sector_rrg.THEME_OTHER} {rest}")
    return " · ".join(cells)


def fmt_price(value):
    return "--" if value is None else f"{value:,.2f}"


def fmt_pos(value):
    """价格类字段：0 与 None 都写 "--"。开盘前接口会给 0.0，写成 0.00 像真价格。"""
    if not isinstance(value, (int, float)) or value == 0:
        return "--"
    return f"{value:,.2f}"


def fmt_ratio(value):
    if value is None:
        return "--"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def fmt_change(value):
    if value is None:
        return ""
    arrow = "▲" if value > 0 else ("▼" if value < 0 else "·")
    return f"{arrow}{value:+,.2f}"


def fmt_volume(value):
    """成交量（股）→ 手：3098875 股 = 309.89 万手。"""
    if value is None:
        return "--"
    hands = value / 100.0
    if hands >= 10000:
        return f"{hands / 10000:.2f}万手"
    return f"{hands:,.2f}手"


def fmt_turnover(value):
    """成交额（元）→ 亿：3937375200 元 = 39.37亿。"""
    if value is None:
        return "--"
    yi = value / 1e8
    if abs(yi) >= 10000:
        return f"{yi / 10000:.2f}万亿"
    return f"{yi:.2f}亿"


def fmt_one(value):
    """RS-Ratio / RS-Momentum 这类无量纲指标：一位小数，缺值 --。"""
    if not isinstance(value, (int, float)):
        return "--"
    return f"{value:.1f}"


def fmt_seal(value):
    if value is None:
        return ""
    return f"封单{fmt_turnover(value)}"


def ah_leader_section(payload):
    section = payload.get("ah_leaders")
    return section if isinstance(section, dict) else {}


def _trend(values, width=13):
    """数字序列压成黑白字符趋势；Kindle 不依赖 SVG 也能看半年方向。"""
    values = [value for value in values if isinstance(value, (int, float))]
    if len(values) < 2:
        return "--"
    if len(values) > width:
        step = len(values) / width
        values = [values[min(len(values) - 1, int(i * step))] for i in range(width)]
    low, high = min(values), max(values)
    if high == low:
        return "▄" * len(values)
    blocks = "▁▂▃▄▅▆▇█"
    return "".join(blocks[min(7, int((value - low) / (high - low) * 7))]
                   for value in values)


def _weekly_trends(market):
    weekly = (market or {}).get("weekly") or []
    closes = [bar[ah_leaders.CLOSE] for bar in weekly
              if isinstance(bar, list) and len(bar) > ah_leaders.VOLUME
              and isinstance(bar[ah_leaders.CLOSE], (int, float))]
    volumes = [bar[ah_leaders.VOLUME] for bar in weekly
               if isinstance(bar, list) and len(bar) > ah_leaders.VOLUME
               and isinstance(bar[ah_leaders.VOLUME], (int, float))]
    return _trend(closes), _trend(volumes)


def _leader_market_text(label, market):
    metrics = (market or {}).get("metrics") or {}
    quote = (market or {}).get("quote") or {}
    price_trend, volume_trend = _weekly_trends(market)
    return (f"  {label} 现{fmt_pos(quote.get('last_price'))} "
            f"半年{fmt_ratio(metrics.get('return_6m_pct'))} "
            f"回撤{fmt_ratio(metrics.get('max_drawdown_pct'))} "
            f"20日均额{fmt_turnover(metrics.get('avg_turnover_20'))} "
            f"{metrics.get('signal') or '数据不足'}\n"
            f"    价{price_trend} 量{volume_trend}")


def _sector_codes():
    """清单里所有板块代码（含 000819.SH 这种不是 .TI 的行业指数）。"""
    global _SECTOR_CODES
    if _SECTOR_CODES is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            import market_watchlist
        except ImportError:                        # 只有单文件被拷走时才用后缀兜底
            _SECTOR_CODES = frozenset()
        else:
            _SECTOR_CODES = frozenset(code for code, _ in market_watchlist.SECTORS)
    return _SECTOR_CODES


_SECTOR_CODES = None


def is_sector_row(row):
    """这一行算「板块」还是「宽基指数」。

    同花顺行业指数大多是 ``881xxx/884xxx.TI``，但「有色金属」是 ``000819.SH``
    ——只按后缀判会把它当成宽基指数。所以先查清单里的板块代码。
    """
    if row.get("is_sector"):
        return True
    code = str(row.get("thscode") or "")
    if code in _sector_codes():
        return True
    return code.endswith(".TI")


def _display_width(text):
    """终端显示宽度：CJK 算 2 列，其余 1 列。"""
    width = 0
    for char in text or "":
        width += 2 if ord(char) > 0x2E80 else 1
    return width


def _pad_display(text, width):
    return text + " " * max(0, width - _display_width(text))


def _short_name(name, limit=12):
    """按显示宽度截断长名字，超出才加省略号。

    同花顺有「汽车服务及其他」「油气开采及服务」这类 7 字名，两个都被截成
    「汽车服务…」「油气开采…」时会撞名——盘面上分不清是哪个板块。
    """
    if _display_width(name) <= limit:
        return name
    out = ""
    for char in name:
        if _display_width(out + char + "…") > limit:
            break
        out += char
    return out + "…"


def index_line(row, compact=False):
    """一行指数：名字 + 最新 + 涨跌幅 +（涨跌额/成交额）。compact 用于分栏。"""
    name = row.get("name") or row.get("thscode") or ""
    if row_is_empty(row):
        return f"{name} 未成交"
    ratio = fmt_ratio(row.get("price_change_ratio_pct"))
    if compact:
        name = _short_name(name, 14)
        return f"{name} {fmt_pos(row.get('last_price'))} {ratio}"
    return (f"{name} {fmt_pos(row.get('last_price'))} {ratio} "
            f"{fmt_change(row.get('price_change'))}"
            f" · 额{fmt_turnover(row.get('turnover'))}").rstrip()


def rrg_section(payload):
    """取出可用的 RRG 段落；没有（或没算出板块）就返回空 dict。

    排版层一律走这个入口：字段缺了宁可整段不显示，也不要排出半截表格。
    """
    # 抓取产物里叫 sector_rrg，清单里叫 rrg——拿清单当输入重排时要都认
    for key in ("sector_rrg", "rrg"):
        section = payload.get(key)
        if isinstance(section, dict) and section.get("rows"):
            return section
    return {}


def volume_price_section(payload):
    """始终从最新板块快照生成量价结果，不再只把它当作 RRG 的失败占位。

    RRG 回答中期相对强弱，量价卡回答今天的钱往哪里走；两者可以同时存在。
    旧清单只有 ``cross`` 时也能直接升级为 ``volume_price``。
    """
    existing = None
    for key in ("sector_cross", "cross", "volume_price"):
        section = payload.get(key)
        if isinstance(section, dict) and section.get("counts"):
            existing = section
            break
    indices = payload.get("indices") or []
    data_ms = payload.get("data_timestamp")
    as_of = ((existing or {}).get("as_of")
             or (beijing_text(data_ms, "%Y-%m-%d") if isinstance(data_ms, (int, float)) else None))
    why = (existing or {}).get("why") or ""
    section, _notes = sector_rrg.build_cross_section(
        indices, market=payload.get("market"), indices=indices,
        limit_up=payload.get("limit_up"), as_of=as_of, why=why)
    return section or existing or {}


def cross_section(payload):
    """RRG 不可用时的兼容字段；实际内容与量价结果卡同源。"""
    if rrg_section(payload):
        return {}
    return volume_price_section(payload)


def final_sections(payload):
    """面板的量化段二选一：能算 RRG 就用 RRG，否则用单日横截面。

    两者**互斥**（同一位置只显示一个）：数据里万一都带着（比如拿旧清单重排），
    这里统一裁决成「RRG 优先」，清单、指纹、自检都走这一个入口，免得一边写
    RRG 一边还留着过期的横截面。
    """
    rrg = rrg_section(payload)
    if rrg:
        return rrg, {}
    return {}, cross_section(payload)


def radar_section(payload):
    """取出或根据当前盘面合成 Jovian Research（jrglobal.xyz/tools/）聪明钱雷达与 AI 数据智库段落。"""
    existing = payload.get("radar")
    snap = existing if isinstance(existing, dict) and existing.get("cn_gurus") else None
    return jrglobal_radar.cross_verify_panel(payload, snapshot=snap)


# 风险开关不是收益承诺，而是把「该不该开新仓」放在选板块之前。仓位上限是保守的
# 纪律模板，不知道用户资金、回撤承受力时绝不能输出满仓/梭哈式指令。
RISK_POSITION_CAP = {"attack": 60, "neutral": 30, "defense": 10}
OPPORTUNITY_LIMIT = 5


def _finite(value):
    """有限浮点数；bool 不是行情数字。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _percentile(value, values):
    """0–1 横截面百分位；同值取中间名次，避免代码顺序影响分数。"""
    value = _finite(value)
    ordered = sorted(v for v in (_finite(item) for item in values) if v is not None)
    if value is None or not ordered:
        return 0.0
    if len(ordered) == 1:
        return 0.5
    lower = sum(1 for item in ordered if item < value)
    equal = sum(1 for item in ordered if item == value)
    return (lower + (equal - 1) / 2.0) / (len(ordered) - 1)


def _trail_persistence(row):
    """RRG 最近轨迹中位于领先/改善象限的点数与总点数。"""
    quadrants = []
    for point in row.get("trail") or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        ratio, momentum = _finite(point[0]), _finite(point[1])
        if ratio is None or momentum is None:
            continue
        quadrant = sector_rrg.quadrant_of(ratio, momentum)
        if quadrant is not None:
            quadrants.append(quadrant)
    favourable = sum(1 for quadrant in quadrants if quadrant in (1, 2))
    return favourable, len(quadrants)


def _opportunity_pool(rrg, volume, risk):
    """从 RRG 持续性、相对强度、当日量价确认中选 3–5 个观察方向。

    只写「重点观察/等待确认/仅观察」，不把板块指数伪装成可直接成交的买入指令。
    RRG 缺失时退到单日抗跌榜，并明确标低置信度，避免把一天涨幅当趋势。
    """
    rows = list((rrg or {}).get("rows") or [])
    support = {row.get("name"): row for row in
               ((volume.get("intraday") or {}).get("true_support") or [])}
    fades = {row.get("name"): row for row in
             ((volume.get("intraday") or {}).get("fade_warning") or [])}
    opportunities = []

    if rows:
        ratio_values = [row.get("rs_ratio") for row in rows]
        momentum_values = [row.get("rs_mom") for row in rows]
        pct_values = [row.get("today_pct") for row in rows]
        for row in rows:
            quadrant = row.get("quadrant")
            if quadrant not in (1, 2):
                continue
            name = row.get("name") or row.get("thscode") or ""
            favourable, trail_total = _trail_persistence(row)
            persist_ratio = favourable / trail_total if trail_total else 0.0
            score = 30 if quadrant == 1 else 22
            score += 20 * _percentile(row.get("rs_ratio"), ratio_values)
            score += 15 * _percentile(row.get("rs_mom"), momentum_values)
            score += 15 * persist_ratio
            previous = row.get("prev_quadrant")
            if quadrant == 1 and previous not in (1, None):
                transition = "新进领先"
                score += 10
            elif quadrant == 1:
                transition = "保持领先"
                score += 6
            elif previous not in (2, None):
                transition = "新进改善"
                score += 5
            else:
                transition = "保持改善"
                score += 3
            score += 10 * _percentile(row.get("today_pct"), pct_values)

            reasons = [f"{row.get('quadrant_name') or ('领先' if quadrant == 1 else '改善')}象限",
                       transition]
            if trail_total:
                reasons.append(f"近{trail_total}期有{favourable}期位于领先/改善")
            if name in support:
                score += 8
                reasons.append("日内真承接")
            risks = []
            if name in fades:
                score -= 18
                risks.append(fades[name].get("tag") or "冲高回落")
            today_pct = _finite(row.get("today_pct"))
            if today_pct is not None and today_pct >= 4:
                score -= 10
                risks.append("当日涨幅偏大，避免追高")
            elif today_pct is not None and today_pct >= 2.5:
                score -= 5
                risks.append("短线升温，等待回落确认")

            if risk.get("mode") == "defense":
                status = "仅观察"
            elif name in fades:
                status = "等待确认"
            elif today_pct is not None and today_pct >= 3:
                status = "等待回落"
            elif quadrant == 2:
                status = "改善观察"
            else:
                status = "重点观察"
            invalidation = ("退出领先象限或 RS-Mom 跌破 100" if quadrant == 1
                            else "动量重新跌破 100 或转入滞后")
            opportunities.append({
                "thscode": row.get("thscode"),
                "name": name,
                "score": max(0, min(100, int(round(score)))),
                "status": status,
                "role": ("新进领先" if quadrant == 1 and previous not in (1, None)
                         else ("持续领先" if quadrant == 1 else "改善候选")),
                "quadrant": row.get("quadrant_name") or ("领先" if quadrant == 1 else "改善"),
                "today_pct": today_pct,
                "rs_ratio": _finite(row.get("rs_ratio")),
                "rs_mom": _finite(row.get("rs_mom")),
                "persistence": {"favourable": favourable, "samples": trail_total},
                "reasons": reasons[:4],
                "risks": risks,
                "invalidation": invalidation,
                "confidence": "中期+当日" if trail_total >= 3 else "中期",
            })
        opportunities.sort(key=lambda row: (-row["score"], row["name"]))
        # 机会池同时保留「已经领先」和「正在改善」：全取老主线会稳定但太迟，
        # 全取改善又容易抄底。3 个领先 + 2 个改善是观察结构，不是仓位分配。
        leaders = [row for row in opportunities if row.get("quadrant") == "领先"][:3]
        improving = [row for row in opportunities if row.get("quadrant") == "改善"][:2]
        selected = leaders + improving
        selected_codes = {row.get("thscode") for row in selected}
        for row in opportunities:
            if len(selected) >= OPPORTUNITY_LIMIT:
                break
            if row.get("thscode") not in selected_codes:
                selected.append(row)
                selected_codes.add(row.get("thscode"))
        opportunities = sorted(selected, key=lambda row: (-row["score"], row["name"]))
    else:
        # 没有长期日 K 时不装作知道趋势，只给单日相对抗跌线索。
        for index, row in enumerate((volume.get("winners") or [])[:OPPORTUNITY_LIMIT]):
            name = row.get("name") or row.get("thscode") or ""
            pct = _finite(row.get("today_pct"))
            opportunities.append({
                "thscode": row.get("thscode"), "name": name,
                "score": max(0, 50 - index * 4 + (6 if name in support else 0)
                             - (12 if name in fades else 0)),
                "status": "仅观察" if risk.get("mode") == "defense" else "等待历史确认",
                "role": "单日线索",
                "quadrant": "单日相对强势", "today_pct": pct,
                "rs_ratio": None, "rs_mom": None,
                "persistence": {"favourable": 0, "samples": 0},
                "reasons": ["单日相对抗跌", "缺少可用 RRG 历史"],
                "risks": ["一天涨跌不能证明趋势"],
                "invalidation": "下一交易日跌出相对强势前列",
                "confidence": "低",
            })
    for rank, row in enumerate(opportunities[:OPPORTUNITY_LIMIT], 1):
        row["rank"] = rank
    return opportunities[:OPPORTUNITY_LIMIT]


def decision_section(payload):
    """风险开关 + 板块机会池，供 JSON/TXT/HTML/EPUB 四端共用。"""
    volume = volume_price_section(payload)
    rrg = rrg_section(payload)
    sentiment = volume.get("sentiment") or {}
    sentiment_score = _finite(sentiment.get("score"))
    risk_data_ready = sentiment_score is not None
    sentiment_score = 0.0 if sentiment_score is None else sentiment_score

    counts = (rrg.get("counts") or {}) if rrg else {}
    total = sum(int(counts.get(key) or 0)
                for key in ("leading", "improving", "weakening", "lagging"))
    if total:
        trend_score = 100.0 * (
            (counts.get("leading") or 0)
            + 0.65 * (counts.get("improving") or 0)
            + 0.35 * (counts.get("weakening") or 0)) / total
        score = 0.70 * sentiment_score + 0.30 * trend_score
    else:
        trend_score = None
        score = sentiment_score

    market = volume.get("market") or {}
    limit_up, limit_down = market.get("limit_up"), market.get("limit_down")
    reasons = ([f"盘面情绪 {int(round(sentiment_score))}/100 · {sentiment.get('level') or '未知'}"]
               if risk_data_ready else ["当日风险数据不足，不允许据此开新风险"])
    if total:
        favourable = (counts.get("leading") or 0) + (counts.get("improving") or 0)
        reasons.append(f"RRG 领先/改善 {favourable}/{total}")
    else:
        reasons.append("RRG 历史不足，风险分只用当日盘面")
    up, down = market.get("up"), market.get("down")
    if isinstance(up, (int, float)) and isinstance(down, (int, float)):
        reasons.append(f"全 A 上涨/下跌 {int(up)}/{int(down)}")
    if (isinstance(limit_up, (int, float)) and isinstance(limit_down, (int, float))
            and limit_down >= max(10, limit_up * 1.5)):
        score -= 8
        reasons.append(f"跌停 {int(limit_down)} 显著多于涨停 {int(limit_up)}")
    fades = ((volume.get("intraday") or {}).get("fade_warning") or [])
    if len(fades) >= 3:
        score -= 5
        reasons.append(f"{len(fades)} 个板块出现派发警告")
    score = max(0, min(100, int(round(score))))

    if not risk_data_ready:
        mode, label, stance = "defense", "数据不足", "不开新风险，等待完整市场宽度与情绪数据"
        cap = 0
    elif score >= 65:
        mode, label, stance = "attack", "进攻", "可分批试仓，只参与最强方向"
        cap = RISK_POSITION_CAP[mode]
    elif score >= 45:
        mode, label, stance = "neutral", "中性", "只看最强方向，小仓等待确认"
        cap = RISK_POSITION_CAP[mode]
    else:
        mode, label, stance = "defense", "防守", "不开新方向，只观察并管理已有风险"
        cap = RISK_POSITION_CAP[mode]
    if (sentiment_score >= 75 or sentiment.get("level") == "过热") and mode == "attack":
        cap = min(cap, 45)
        stance = "盘面过热：保持进攻观察，但不追高"
        reasons.append("情绪过热，仓位上限主动下调")

    risk = {
        "mode": mode, "label": label, "score": score,
        "max_position_pct": cap, "stance": stance,
        "reasons": reasons[:5],
        "single_trade_risk_pct": 0.5,
        "note": "仓位是通用纪律模板，不是个性化投资建议",
    }
    return {
        "version": "risk-sector-v1",
        "as_of": volume.get("as_of") or rrg.get("as_of"),
        "risk": risk,
        "opportunities": _opportunity_pool(rrg, volume, risk),
        "method": "当日市场宽度/涨跌停/收盘位置 + RRG 相对强度与持续性",
        "note": "机会池是观察顺序，不是买入指令；板块指数不可直接成交",
    }


def _wrap_display(text, width=48, indent="  "):
    """按显示宽度折行（CJK 算 2 列），Kindle 上不会被截断。

    先在「·、，；。」后面断，再按宽度硬切——逐字折行会把「-0.06%」劈成
    「-0.06」+「%」，电子墨水上读起来像两行两个数。
    """
    # 一串「非分隔符 + 可选的句读/空格」= 一个不可再分的块：
    # 「半导体 -0.06%（」不会被拆开，标点留在块尾。
    tokens = re.findall(
        r"[^·、，；。：）\s]+[·、，；。：）]?\s*|[·、，；。：）]\s*", text or "")
    lines, current = [], ""
    for token in tokens:
        if not token:
            continue
        if _display_width(current + token) <= width:
            current += token
            continue
        if current:
            lines.append(indent + current.rstrip())
            current = ""
        while _display_width(token) > width:
            cut = ""
            for char in token:
                if _display_width(cut + char) > width:
                    break
                cut += char
            lines.append(indent + cut.rstrip())
            token = token[len(cut):]
        current = token
    if current.strip():
        lines.append(indent + current.rstrip())
    return lines


def rrg_quadrant_line(section, quadrant, limit=6):
    """一个象限的紧凑清单：名字 + 今日涨跌 + (RS-Ratio / RS-Mom)。"""
    rows = [row for row in section.get("rows") or []
            if row.get("quadrant") == quadrant]
    if not rows:
        return ""
    cells = [f"{row.get('name') or row.get('thscode')} {fmt_ratio(row.get('today_pct'))}"
             f"({row.get('rs_ratio'):.1f}/{row.get('rs_mom'):.1f})"
             for row in rows[:limit]]
    extra = f" 等 {len(rows)} 个" if len(rows) > limit else ""
    return f"{sector_rrg.QUADRANTS[quadrant]}({len(rows)}) " + " · ".join(cells) + extra


BRIEF_NARRATIVE_LINES = 2        # 精简版留几条结论（每条只留第一句）


def _first_sentences(items, limit=BRIEF_NARRATIVE_LINES):
    """取前几条结论，每条只留第一句——「只留两三句结论」不是「只留三段」。"""
    picked = []
    for text in list(items or [])[:limit]:
        sentence = str(text).split("。")[0].strip()
        if sentence:
            picked.append(sentence + "。")
    return picked


def render_decision_text(section, brief=None):
    """风险开关与机会池的纯文本版；仓位明确写成纪律上限而非收益承诺。"""
    risk = section.get("risk") or {}
    lines = [
        f"【风险开关】{risk.get('label') or '—'} {risk.get('score', '—')}/100"
        f" · 纪律仓位≤{risk.get('max_position_pct', 0)}%",
    ]
    lines.extend(_wrap_display(f"[行动] {risk.get('stance') or '仅观察'}",
                               width=46, indent="  "))
    reasons = risk.get("reasons") or []
    if reasons:
        lines.extend(_wrap_display("[依据] " + " · ".join(reasons[:3 if brief else 5]),
                                   width=46, indent="  "))
    lines.append(f"  [纪律] 单笔风险≤{risk.get('single_trade_risk_pct', 0.5):g}% · 仓位上限不是满仓目标")

    opportunities = section.get("opportunities") or []
    lines.append(f"【板块机会池】{len(opportunities)} 个 · 观察顺序，不是买入指令")
    for row in opportunities[:3 if brief else OPPORTUNITY_LIMIT]:
        pct = fmt_ratio(row.get("today_pct"))
        lines.append(
            f"  {row.get('rank')}. {row.get('name')} {row.get('score')}/100"
            f" · {row.get('role') or row.get('status')} · {row.get('status')} · 今日{pct}"
        )
        detail = " / ".join((row.get("reasons") or [])[:3])
        if row.get("risks"):
            detail += (" · 风险 " if detail else "风险 ") + "、".join(row["risks"][:2])
        if detail:
            lines.extend(_wrap_display(detail, width=44, indent="    "))
        if not brief:
            lines.extend(_wrap_display("失效：" + (row.get("invalidation") or "强势条件消失"),
                                       width=44, indent="    "))
    return lines


def render_rrg_text(section, brief=None):
    """RRG 量化段的纯文本版。

    ``brief``：只留抬头、象限分布与前 ``BRIEF_NARRATIVE_LINES`` 句结论，
    逐象限名单与出处折叠成一行提示。
    """
    rows = section.get("rows") or []
    counts = section.get("counts") or {}
    demo = "（演示）" if section.get("demo") else ""
    lines = [f"【板块轮动 · RRG】{len(rows)} 个板块 · {section.get('preset')}"
             f" · 基准 板块等权 · 数据 {section.get('as_of')}{demo}"]
    lines.append(f"  象限 领先 {counts.get('leading', 0)} · 改善 {counts.get('improving', 0)}"
                 f" · 滞后 {counts.get('lagging', 0)} · 疲软 {counts.get('weakening', 0)}"
                 " · 依次按轮动顺序（领先→疲软→滞后→改善）")
    signal = (section.get("signal") or {}).get("picks") or []
    if signal:
        cells = [f"{row.get('name')} {fmt_ratio(row.get('today_pct'))}" for row in signal[:6]]
        lines.extend(_wrap_display("[信号] " + " · ".join(cells), width=46, indent="  "))
    for quadrant in (1, 2, 4, 3):
        line = rrg_quadrant_line(section, quadrant, limit=3 if brief else 5)
        if line:
            lines.extend(_wrap_display(line, width=46, indent="  "))
    return lines


def render_cross_text(section, brief=None):
    """只输出结果卡：市场、量价、承接、派发、主线与强弱，不复述方法论。"""
    counts = section.get("counts") or {}
    stats = section.get("stats") or {}
    market = section.get("market") or {}
    sent = section.get("sentiment") or {}
    flow = section.get("money") or {}
    intra = section.get("intraday") or {}
    lines = [f"【结果 · 量价】{section.get('as_of') or '未知'}"]

    score = sent.get("score")
    market_result = (f"{sent.get('level') or '—'} {score}/100" if score is not None
                     else f"涨停{market.get('limit_up', 0)} / 跌停{market.get('limit_down', 0)}")
    lines.append(
        f"  [市场] {market_result} · 红{counts.get('up', 0)}/绿{counts.get('down', 0)}"
        f" · 中位{fmt_ratio(stats.get('median'))} · CSAD {stats.get('csad', 0):.2f}"
        + (f" · {sent.get('divergence')}" if sent.get("divergence") else "")
    )

    top_avg, tail_avg = flow.get("top_avg"), flow.get("tail_avg")
    if isinstance(top_avg, (int, float)) and isinstance(tail_avg, (int, float)):
        gap = top_avg - tail_avg
        verdict = ("量能无避风港" if abs(gap) < 0.20 else
                   ("资金偏向高成交板块" if gap > 0 else "高成交板块承压"))
    else:
        verdict = "量价数据不足"
    lines.append(
        f"  [量价] {verdict} · 成交{fmt_turnover(flow.get('total'))}"
        f" · {flow.get('top_name') or '—'}占比{flow.get('top_share', 0):.1f}%"
        f"/今日{fmt_ratio(flow.get('top_pct'))}"
        + (f" · 前{flow.get('size', 0)}均{fmt_ratio(top_avg)} / 后{flow.get('size', 0)}均{fmt_ratio(tail_avg)}"
           if isinstance(top_avg, (int, float)) and isinstance(tail_avg, (int, float)) else "")
    )

    support = intra.get("true_support") or []
    if support:
        cells = [
            f"{row.get('name')} {fmt_ratio(row.get('overnight_pct'))}→{fmt_ratio(row.get('intraday_pct'))}"
            f" CLV{row.get('clv', 0):.2f}"
            for row in support[:3]
        ]
        lines.extend(_wrap_display("[承接] " + " · ".join(cells), width=46, indent="  "))

    fades = intra.get("fade_warning") or []
    if fades:
        cells = [
            f"{row.get('name')} {row.get('tag') or '回落'} 日内{fmt_ratio(row.get('intraday_pct'))}"
            f" CLV{row.get('clv', 0):.2f}"
            for row in fades[: (3 if brief else 5)]
        ]
        lines.extend(_wrap_display("[派发] " + " · ".join(cells), width=46, indent="  "))

    themes = section.get("themes") or []
    if themes:
        lines.extend(_wrap_display(
            "[主线] " + " · ".join(f"{row.get('name')} {row.get('count', 0)}只" for row in themes[:4]),
            width=46, indent="  "))

    winners = section.get("winners") or []
    losers = section.get("losers") or []
    if winners or losers:
        strong = " ".join(f"{row.get('name')}{fmt_ratio(row.get('today_pct'))}" for row in winners[:3])
        weak = " ".join(f"{row.get('name')}{fmt_ratio(row.get('today_pct'))}" for row in losers[:3])
        lines.extend(_wrap_display(f"[强弱] 抗跌 {strong} · 领跌 {weak}", width=46, indent="  "))
    return lines


def data_hash(payload, view=None):
    """行情指纹：价格/涨跌幅/条数/历史收盘变了才变，供工作流跳过无变化的提交。

    ``view``（周末精简版）也算进去：周五收盘到周六零点行情一点没变，但版式要从
    完整版换成精简版——不进指纹的话这一步永远不会被提交，设备上就一直是完整版。
    """
    history = payload.get("history") or {}
    rrg_part, cross_part = final_sections(payload)
    volume_part = volume_price_section(payload)
    radar_part = radar_section(payload)
    decision_part = decision_section(payload)
    leaders_part = ah_leader_section(payload)
    mode = (view.get("mode") if isinstance(view, dict)
            else ("brief" if view == "brief" else "full"))
    core = {
        "schema": DATA_SCHEMA,
        "view": mode or "full",
        "indices": [(r.get("thscode"), r.get("last_price"),
                     r.get("price_change_ratio_pct")) for r in payload.get("indices") or []],
        # 全市场只留统计口径：5584 只逐只价格进 hash 会让收盘后每轮都变，反而刷空提交
        "market": [(payload.get("market") or {}).get(k) for k in
                   ("count", "up", "down", "limit_up", "limit_down", "turnover")],
        "hk": [(r.get("thscode"), r.get("last_price"), r.get("turnover"))
               for r in (payload.get("hk") or {}).get("rows") or []],
        "limit_up": [(r.get("thscode"), r.get("last_price")) for r in payload.get("limit_up") or []],
        "history": {code: [pair[1] for pair in bars]
                    for code, bars in sorted(history.items())},
        # RRG 只看「象限分布 + 信号 A 名单」：逐板块的 RS/Mom 每轮都在小数点后动，
        # 全塞进指纹会让收盘后也每 15 分钟提交一次，等于白刷。
        "rrg": [(rrg.get("as_of"), rrg.get("preset"), rrg.get("counts"),
                 [row.get("name") for row in
                  ((rrg.get("signal") or {}).get("picks") or [])])
                for rrg in [rrg_part] if rrg],
        "cross": [(cross.get("as_of"), cross.get("counts"),
                   [row.get("name") for row in (cross.get("winners") or [])[:3]],
                   [row.get("name") for row in (cross.get("losers") or [])[:3]],
                   [(item.get("name"), item.get("count"))
                    for item in (cross.get("themes") or [])[:3]])
                  for cross in [cross_part] if cross],
        "volume_price": [(volume.get("as_of"), volume.get("counts"), volume.get("stats"),
                          volume.get("money"), volume.get("intraday"), volume.get("themes"))
                         for volume in [volume_part] if volume],
        # 四工具快照也会独立更新；只看行情价格会把「持仓/AI/月报/组合已更新」误判成无变化。
        # stock_index 是由持仓与共识派生出的冗余索引，排除它以缩短 hash 输入。
        "radar": {key: value for key, value in radar_part.items() if key != "stock_index"},
        "decision": {
            "risk": [(decision_part.get("risk") or {}).get(key) for key in
                     ("mode", "score", "max_position_pct")],
            "opportunities": [(row.get("thscode"), row.get("score"), row.get("status"))
                              for row in decision_part.get("opportunities") or []],
        },
        "ah_leaders": [
            (row.get("sector_code"), row.get("status"), row.get("a_code"), row.get("h_code"),
             ((row.get("a") or {}).get("quote") or {}).get("last_price"),
             ((row.get("h") or {}).get("quote") or {}).get("last_price"),
             ((row.get("a") or {}).get("metrics") or {}).get("return_6m_pct"),
             ((row.get("h") or {}).get("metrics") or {}).get("return_6m_pct"),
             (((row.get("a") or {}).get("weekly") or [[None]])[-1][0]),
             (((row.get("h") or {}).get("weekly") or [[None]])[-1][0]))
            for row in leaders_part.get("rows") or []
        ],
        "data_timestamp": payload.get("data_timestamp"),
    }
    raw = json.dumps(core, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def manifest(payload, generated, view=None):
    rrg_part, cross_part = final_sections(payload)
    volume_part = volume_price_section(payload)
    decision_part = decision_section(payload)
    leaders_part = ah_leader_section(payload)
    if isinstance(view, dict):
        brief = view
    else:
        brief = brief_view(payload, now_ms=_generated_ms(generated), mode=view)
    data_ms = payload.get("data_timestamp")
    data_time = None
    if isinstance(data_ms, (int, float)):
        # 插件的 isoSeconds 只认 "...Z" 结尾（和 epub.json 的 data_time 同形）。
        data_time = datetime.fromtimestamp(
            data_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": DATA_SCHEMA,
        "generated": generated,
        "doc": "stocks.txt",
        "epub": "stocks.epub",
        "title": TITLE,
        # 数据时间两写：ISO 给插件显示，毫秒原值留着——这样拿 stocks.json
        # 本身当输入重排一次（--data site/stocks.json）也不会把数据时间弄丢。
        "data_timestamp": data_ms if isinstance(data_ms, (int, float)) else None,
        "data_time": data_time,
        "data_time_kind": "exact" if data_time else None,
        "data_hash": data_hash(payload, view=brief),
        "demo": bool(payload.get("demo")),
        # 版式：完整版是 null；周末/节假日是 {"mode": "brief", "reason": …}。
        # 网页与插件据此出精简版，JSON 里的数据仍是全量。
        "view": brief,
        "counts": {
            "indices": len(core_indices(payload)),
            "sectors": len(sector_indices(payload)),
            "market": (payload.get("market") or {}).get("count") or 0,
            "hk": len(((payload.get("hk") or {}).get("rows")) or []),
            "limit_up": len(payload.get("limit_up") or []),
            "rrg": len(rrg_part.get("rows") or []),
            "cross": (cross_part.get("counts") or {}).get("total") or 0,
            "volume_price": (volume_part.get("counts") or {}).get("total") or 0,
        },
        "indices": payload.get("indices") or [],
        "market": payload.get("market"),
        "hk": _hk_manifest(payload),
        "limit_up": payload.get("limit_up") or [],
        # 板块轮动整段进清单（网页要自己画四象限图；Kindle 只用文字与表格）
        "rrg": rrg_part or None,
        # 板块情绪 EW-SDM（单日横截面，见 tools/ew_sdm.py）；没算出来就是 null。
        "ew_sdm": payload.get("ew_sdm") or None,
        # 指数择时 ICU 均线 + QRS（见 tools/timing_quants.py）；没算出来就是 null。
        "timing": payload.get("timing") or None,
        # 日 K 不够时的兼容段；量价结果则无论有没有 RRG 都独立输出。
        "cross": cross_part or None,
        "volume_price": volume_part or None,
        # 风险开关与观察池和三端正文同源，设备只拉 JSON 也能直接读取。
        "decision": decision_part,
        # 每板块都保留一行；没有 A/H 同公司时 status=no_match，绝不硬凑。
        # 主清单只放 26 周压缩趋势；完整约 130 根日线在 sidecar，省 Kindle 流量。
        "ah_leaders": leaders_part or None,
        "leader_history": "stocks-leaders.json" if leaders_part.get("rows") else None,
        # Jovian Research (jrglobal.xyz/tools/) 聪明钱雷达 + AI 数据智库 + 组合实验室
        "radar": radar_section(payload),
        "history": payload.get("history") or {},
        "errors": payload.get("errors") or [],
    }


def compact_manifest(data):
    """给 Kindle 多镜像择优的小清单；避免每 7 分钟从 6 个源重复拉全量 JSON。"""
    keys = ("schema", "generated", "doc", "epub", "title", "data_time", "data_time_kind",
            "data_hash", "demo", "view", "counts", "leader_history")
    return {key: data.get(key) for key in keys if data.get(key) is not None}


def core_indices(payload):
    """宽基指数（不含行业/概念板块）。"""
    return [r for r in payload.get("indices") or [] if not is_sector_row(r)]


def sector_indices(payload):
    return [r for r in payload.get("indices") or [] if is_sector_row(r)]


def _hk_manifest(payload):
    """港股那块写进清单：带上 data_time（网页/插件要显示行情时间）。"""
    hk = payload.get("hk")
    if not isinstance(hk, dict):
        return None
    stamp = hk.get("data_timestamp")
    data_time = None
    if isinstance(stamp, (int, float)):
        data_time = datetime.fromtimestamp(
            stamp / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = dict(hk)
    out["data_timestamp"] = stamp if isinstance(stamp, (int, float)) else None
    out["data_time"] = data_time
    return out


def render_txt(payload, generated, brief=None):
    """Kindle 纯文本版：一屏卡片式监视器（市场概览 → 指数 → 板块 → 港股 → RRG）。

    不再有「自选股」：面板看的是全市场（指数/板块/涨跌家数/量价）+ 港股。

    ``brief`` 是「周末精简版」（见 brief_view）：指数全留、板块只留抗跌/杀跌各
    N 个、涨停池只留条数与题材、分析段只留结论。JSON 里的数据一条不少。
    """
    lines = [TITLE]
    demo = "【演示数据】" if payload.get("demo") else ""
    lines.append(f"生成 {generated} 北京时间 · {status_line(payload)}{demo}")
    lines.append(f"每{SERVER_MINUTES}分钟刷新 · 每{poll_text()}检查 · {DISCLAIMER}")
    if not today_has_values(payload):
        lines.append("注：这一版快照没有成交数据（开盘前/接口给空壳），下面显示 -- 与「未成交」")
    if brief:
        lines.append(brief_banner(brief, len(sector_indices(payload))))
    lines.append("")

    # 结果优先：先给「该不该开新风险」和少量观察方向，再给量价证据与 RRG。
    decision = decision_section(payload)
    lines.extend(render_decision_text(decision, brief=brief))
    lines.append("")
    volume = volume_price_section(payload)
    if volume:
        lines.extend(render_cross_text(volume, brief=brief))
        lines.append("")
    section = rrg_section(payload)
    if section:
        lines.extend(render_rrg_text(section, brief=brief))
        lines.append("")

    # 板块情绪 EW-SDM（单日横截面）：紧跟 RRG，周末精简版只留强弱各 3 行。
    ew_part = payload.get("ew_sdm")
    if ew_part:
        lines.extend(ew_sdm.render_text_lines(ew_part, limit=3 if brief else 6))
        lines.append("")

    # 指数择时 ICU 均线 + QRS：宽基指数日线算的，周末精简版只留 ICU 行。
    timing_part = payload.get("timing")
    if timing_part:
        lines.extend(timing_quants.render_text_lines(timing_part, brief=brief))
        lines.append("")

    # Jovian Research (jrglobal.xyz/tools/) 聪明钱雷达 + AI 数据智库 + 组合实验室
    radar = radar_section(payload)
    if radar:
        lines.extend(jrglobal_radar.render_radar_txt(radar, brief=brief))
        lines.append("")


    market = payload.get("market") or {}
    if market:
        lines.append(f"【市场概览】全 A · {market.get('count', 0)} 只")
        lines.append(f"  涨 {market.get('up', 0)} · 跌 {market.get('down', 0)}"
                     f" · 平 {market.get('flat', 0)} · 无价 {market.get('no_price', 0)}"
                     f" · 涨停 {market.get('limit_up', 0)}"
                     f" · 跌停 {market.get('limit_down', 0)}")
        avg = market.get("avg_ratio")
        lines.append(f"  平均涨跌 {fmt_ratio(avg) if avg is not None else '--'}"
                     f" · 成交额 {fmt_turnover(market.get('turnover'))}"
                     f" · 量 {fmt_volume(market.get('volume'))}")
        gainers = market.get("top_gainers") or []
        losers = market.get("top_losers") or []
        if gainers:
            lines.append("  涨前列 " + " ".join(
                f"{g.get('name') or g.get('thscode')}{fmt_ratio(g.get('ratio'))}"
                for g in gainers[:5]))
        if losers:
            lines.append("  跌前列 " + " ".join(
                f"{g.get('name') or g.get('thscode')}{fmt_ratio(g.get('ratio'))}"
                for g in losers[:5]))
        lines.append("")

    indices = payload.get("indices") or []
    broad = [r for r in indices if not is_sector_row(r)]
    sectors = sorted([r for r in indices if is_sector_row(r)],
                     key=lambda r: -((r.get("price_change_ratio_pct") or -999)))

    if broad:
        lines.append(f"【指数】{len(broad)}")
        for row in broad:
            lines.append(index_line(row))
        lines.append("")

    if sectors:
        up = sum(1 for r in sectors if (r.get("price_change_ratio_pct") or 0) > 0)
        if brief:
            # 周末只留两端：抗跌前 N（跌得最少）与杀跌前 N（跌得最狠）。
            rows = int(brief.get("sector_rows") or BRIEF_SECTOR_ROWS)
            head_rows = sectors[:rows]
            tail_rows = list(reversed(sectors[-rows:])) if len(sectors) > rows else []
            lines.append(f"【板块】{len(sectors)} · 涨 {up} 跌 {len(sectors) - up}"
                         f" · 精简：抗跌前 {rows} / 杀跌前 {rows}")
            for row in head_rows + tail_rows:
                lines.append(index_line(row))
            rest = len(sectors) - len(head_rows) - len(tail_rows)
            if rest > 0:
                lines.append(f"  其余 {rest} 个板块见 stocks.html（JSON 里是全量）")
            lines.append("")
        else:
            lines.append(f"【板块】{len(sectors)} · 涨 {up} 跌 {len(sectors) - up} · 按涨跌幅")
            cells = [index_line(r, compact=True) for r in sectors]
            col_w = max(_display_width(c) for c in cells) + 3
            for i in range(0, len(cells), 2):
                right = cells[i + 1] if i + 1 < len(cells) else ""
                lines.append((_pad_display(cells[i], col_w) + right).rstrip())
            lines.append("")

    leaders = ah_leader_section(payload)
    leader_rows = leaders.get("rows") or []
    if leader_rows:
        coverage = leaders.get("coverage") or {}
        lines.append(f"【A/H 龙头半年量价】板块 {coverage.get('sectors', len(leader_rows))}"
                     f" · 命中 {coverage.get('matched', 0)}"
                     f" · 无 A/H 同公司 {coverage.get('unmatched', 0)}")
        lines.append("规则：同一家公司同时有 A/H 股；板块成分内按 A 股近20日平均成交额选 1 只")
        for row in leader_rows:
            sector = row.get("sector_name") or row.get("sector_code") or ""
            if row.get("status") != "ok":
                lines.append(f"{sector}｜暂无可验证的 A/H 同公司成分股")
                continue
            h_code = str(row.get("h_code") or "").replace("hk", "")
            lines.append(f"{sector}｜{row.get('name') or row.get('a_code')} "
                         f"A{str(row.get('a_code') or '').split('.')[0]} / H{h_code}")
            lines.extend(_leader_market_text("A", row.get("a")).splitlines())
            lines.extend(_leader_market_text("H", row.get("h")).splitlines())
        leader_errors = leaders.get("errors") or []
        if leader_errors:
            lines.append(f"A/H 子源提示 {len(leader_errors)} 条：{leader_errors[0]}")
        lines.append("完整约130根 A/H 日线见 stocks-leaders.json；此处为最近26周压缩趋势。")
        lines.append("")

    hk = payload.get("hk") or {}
    hk_rows = hk.get("rows") or []
    if hk_rows:
        stamp = hk.get("data_timestamp")
        when = beijing_text(stamp) if isinstance(stamp, (int, float)) else "无"
        lines.append(f"【港股】{len(hk_rows)} 个 · 行情 {when} · 腾讯公开接口")
        ordered = ([r for r in hk_rows if r.get("is_index")]
                   + [r for r in hk_rows if not r.get("is_index")])
        for row in ordered:
            name = row.get("name") or row.get("thscode") or ""
            if row_is_empty(row):
                lines.append(f"{name} 未成交")
                continue
            traded = isinstance(row.get("volume"), (int, float)) and row.get("volume") != 0
            # 港股指数腾讯只给点位、不给成交额（给了也是成分合计，口径不同），
            # 所以指数行不显示成交额，别让「0.18亿」误导。
            tail = (f"额{fmt_turnover(row.get('turnover'))}"
                    if traded and row.get("turnover")
                    and not row.get("is_index") else "")
            lines.append((f"{name} {fmt_pos(row.get('last_price'))} "
                          f"{fmt_ratio(row.get('price_change_ratio_pct'))} "
                          f"{fmt_change(row.get('price_change'))} {tail}").rstrip())
        lines.append("")

    limit_up = payload.get("limit_up") or []
    if limit_up and brief:
        themes = limit_up_theme_text(limit_up)
        lines.append(f"【涨停池】{len(limit_up)} · 按题材（只数）")
        if themes:
            wrapped = _wrap_cells(themes.split(" · "), width=44, indent="  ")
            wrapped[0] = "  题材 " + wrapped[0].strip()
            lines.extend(wrapped)
        else:
            lines.append("  接口没给涨停原因，题材分布这一轮算不出")
        lines.append("")
    elif limit_up:
        lines.append(f"【涨停池】{len(limit_up)} · 按封板时间")
        for row in limit_up:
            name = row.get("name") or row.get("thscode") or ""
            extra = " ".join(part for part in (
                row.get("limit_up_time") or "",
                row.get("limit_up_reason") or "",
                row.get("continue_day_text") or "",
                fmt_seal(row.get("seal_money"))) if part)
            lines.append(f"{name} {fmt_pos(row.get('last_price'))} "
                         f"{fmt_ratio(row.get('price_change_ratio_pct'))} {extra}".rstrip())
        lines.append("")


    for error in payload.get("errors") or []:
        lines.append(f"注：{error}")
    lines.append(DISCLAIMER)
    return "\n".join(lines) + "\n"


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#fff;color:#000;
font-family:"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:16px}
header{border-bottom:3px solid #000;padding-bottom:8px}
h1{font-size:26px;margin:0 0 4px}
.meta{font-size:14px;color:#333}
.badge{display:inline-block;border:1px solid #000;padding:0 6px;font-size:13px;margin-left:6px}
.badge.stale{background:#000;color:#fff}
.stale-hint{display:none;border:2px dashed #000;padding:6px 10px;margin:10px 0;font-size:13px}
.muted{font-size:16px;font-weight:normal;color:#555}
section h2{font-size:19px;border-bottom:2px solid #000;padding-bottom:4px;margin:22px 0 10px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.cards.index{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.cards.sector{grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:6px}
.cards.hk{grid-template-columns:repeat(auto-fill,minmax(158px,1fr))}
.leader-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:9px}
.leader-card{border:2px solid #000;padding:8px;break-inside:avoid}
.leader-card h3{font-size:15px;margin:0 0 5px;border-bottom:1px solid #999;padding-bottom:3px}
.leader-pair{font-size:12px;color:#444;margin-bottom:5px}
.leader-markets{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.leader-market{border:1px solid #aaa;padding:5px;font-size:11.5px}
.leader-market .ticker{font-size:13px;font-weight:bold}
.leader-market svg{display:block;width:100%;height:58px;margin:3px 0}
.leader-market .metrics{line-height:1.45}
.result-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:9px;margin:10px 0}
.result-card{border:2px solid #000;background:#fff;padding:10px;min-height:132px}
.result-card .label{font-size:12px;color:#555;border-bottom:1px solid #bbb;padding-bottom:3px}
.result-card .value{font-size:23px;font-weight:bold;line-height:1.15;margin:7px 0 3px}
.result-card .sub{font-size:12px;color:#444;min-height:17px}
.result-card .metric{display:flex;justify-content:space-between;gap:8px;border-top:1px dotted #bbb;padding:4px 0;font-size:12.5px}
.result-card .metric span:last-child{text-align:right;font-weight:bold}
.result-card.compact{min-height:0}
.result-search{border:2px solid #000;padding:8px;margin:9px 0}
.result-search input{width:100%;border:0;border-bottom:1px solid #000;padding:5px 2px;font-size:14px;background:#fff}
.result-search input:focus{outline:0;border-bottom-width:2px}
.breadth{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));
gap:1px;background:#000;border:2px solid #000;margin:10px 0}
.breadth .cell{background:#fff;padding:6px 10px}
.breadth .k{font-size:11.5px;color:#555}
.breadth .v{font-size:21px;font-weight:bold;line-height:1.15}
.breadth .s{font-size:11.5px;color:#333}
.bar{display:flex;height:10px;border:1px solid #000;margin-top:5px}
.bar .u{background:#000}
.bar .d{background:#bbb}
.bar .f{background:#fff}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:8px}
.two .k{font-size:11.5px;color:#555;border-bottom:1px solid #000;padding-bottom:2px}
.rank{display:flex;justify-content:space-between;font-size:13px;padding:2px 0;
border-bottom:1px dotted #bbb}
.card.sector .head{padding:5px 7px}
.card.sector .name{font-size:13px}
.card.sector .body{padding:5px 7px}
.card.sector .price{font-size:17px}
.card.sector .ratio{font-size:12.5px;margin-bottom:0}
.card.sector .bar2{height:4px;background:#e6e6e6;margin-top:4px;border:1px solid #999}
.card.sector .bar2 i{display:block;height:100%;background:#000}
.card{border:2px solid #000;padding:0;background:#fff}
.card .head{display:flex;justify-content:space-between;align-items:baseline;
padding:8px 12px;border-bottom:1px solid #000;background:#f2f2f2}
.card .name{font-size:15px;font-weight:bold}
.card .code{font-size:12px;color:#555}
.card .body{padding:8px 12px}
.card .price{font-size:28px;font-weight:bold;line-height:1.1}
.card .ratio{font-size:16px;font-weight:bold;margin-bottom:6px}
.card table.kv{width:100%;border-collapse:collapse;font-size:12.5px}
.card table.kv td{padding:1px 0}
.card table.kv td:last-child{text-align:right}
.card .spark{margin-top:6px;border-top:1px dashed #999;padding-top:4px}
.card .spark svg{display:block;width:100%;height:36px}
.card .spark-label{font-size:11px;color:#555;display:flex;justify-content:space-between}
.up{font-weight:bold}.down{font-weight:bold}
.rrg-chart{border:2px solid #000;padding:6px;margin:10px 0 6px}
.rrg-chart svg{display:block;width:100%;height:auto}
.rrg-chart .grid{stroke:#000;stroke-width:1;stroke-dasharray:5 4}
.rrg-chart .trail{fill:none;stroke:#c9c9c9;stroke-width:1}
.rrg-chart .trail.hot{stroke:#000;stroke-width:1.6}
.rrg-chart .pt{fill:#777}
.rrg-chart .pt.hot{fill:#000}
.rrg-chart text{font-family:inherit;fill:#000}
.rrg-chart .lbl{font-size:12px}
.rrg-chart .qlabel{font-size:15px;font-weight:bold;fill:#555}
.rrg-chart .ax{font-size:11.5px;fill:#555}
.qcount .cell{background:#fff}
p.src{font-size:11.5px;color:#444;margin-top:8px;line-height:1.5}
.themes{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.themes .chip{border:1px solid #000;padding:2px 7px;font-size:12.5px}
.themes .chip b{font-size:14px}
footer{border-top:1px solid #000;margin-top:16px;padding-top:8px;font-size:13px;color:#333}
</style>
</head>
<body>
<div class="wrap">
<header>
<h1>__TITLE__<span class="badge" id="state">…</span></h1>
<div class="meta" id="meta">加载中…</div>
</header>
<div class="stale-hint" id="stale-hint">数据已过期，请先核对上方行情时间。</div>
<main id="main"></main>
<footer>__DISCLAIMER__ · 每 __POLL_TEXT__ 检查更新</footer>
</div>
<noscript><p>需要 JavaScript 才能每分钟刷新；Kindle 请用 stocks.txt 纯文本版或 stocks.epub 卡片版。</p></noscript>
<script>
'use strict';
var POLL = __POLL__;
// 界面文案用同一份间隔（POLL 是秒）：改成 7 分钟时两边不会说岔。
var POLL_TEXT = '__POLL_TEXT__';
var lastGenerated = null;
// 清单里的时间都是 ISO-8601 UTC（"...Z"），显示前换成北京时间（+8h）。
function bj(iso){
if(!iso)return '无';
var s=String(iso).replace('Z','');s=s.replace(' ','T');
var p=s.match(/^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2}):(\\d{2})$/);
if(!p)return String(iso);
var d=new Date(Date.UTC(+p[1],+p[2]-1,+p[3],+p[4],+p[5],+p[6])+8*3600*1000);
function z(n){return (n<10?'0':'')+n;}
return d.getUTCFullYear()+'-'+z(d.getUTCMonth()+1)+'-'+z(d.getUTCDate())+' '+
z(d.getUTCHours())+':'+z(d.getUTCMinutes())+':'+z(d.getUTCSeconds());
}
// 距今多久 / 盘中还是未开盘 / 是否过期：和纯文本面板同一套口径（STALE_MINUTES）。
function ageText(iso){
if(!iso)return '';
var s=String(iso).replace('Z','');
var p=s.match(/^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2}):(\\d{2})$/);
if(!p)return '';
var then=Date.UTC(+p[1],+p[2]-1,+p[3],+p[4],+p[5],+p[6]);
var sec=(Date.now()-then)/1000;
if(sec<0)return '';
if(sec<90)return '刚刚';
var min=sec/60;if(min<90)return Math.round(min)+' 分钟前';
var hr=min/60;if(hr<36)return Math.round(hr)+' 小时前';
return Math.round(hr/24)+' 天前';}
function sessionLabel(iso){
if(!iso)return '';
var s=String(iso).replace('Z','');
var p=s.match(/^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2}):(\\d{2})$/);
if(!p)return '';
var bjMin=((+p[4]+8)%24)*60+(+p[5]);
var d=new Date(Date.UTC(+p[1],+p[2]-1,+p[3],+p[4],+p[5],+p[6])+8*3600*1000);
if(d.getUTCDay()===0||d.getUTCDay()===6)return '周末休市';
if(bjMin<9*60+15)return '未开盘';
if(bjMin>15*60+5)return '已收盘';
if(bjMin>=11*60+30&&bjMin<13*60)return '午休';
return '盘中';}
function isStale(iso){
// 只有「该有数据却没有」才算过期：盘中落后 >20 分钟是真问题；
// 收盘后/周末/深夜数据本来就不动，不能标 STALE（否则凌晨看面板永远在报警）。
if(!iso)return false;
var s=String(iso).replace('Z','');
var p=s.match(/^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2}):(\\d{2})$/);
if(!p)return false;
var then=Date.UTC(+p[1],+p[2]-1,+p[3],+p[4],+p[5],+p[6]);
var age=(Date.now()-then)/60000;
if(age<=__STALE__)return false;
var label=sessionLabel(iso);
if(label==='盘中'||label==='午休')return true;
return age>24*60;}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function num(v,d){return (v==null||isNaN(v))?'--':Number(v).toLocaleString('zh-CN',
{minimumFractionDigits:d==null?2:d,maximumFractionDigits:d==null?2:d});}
function pos(v){return (typeof v==='number'&&v!==0)?num(v):'--';}
function ratio(v){if(v==null||isNaN(v))return '--';
return (v>0?'+':'')+Number(v).toFixed(2)+'%';}
function change(v){if(v==null||isNaN(v))return '';
var a=v>0?'▲':(v<0?'▼':'·');return a+(v>0?'+':'')+Number(v).toFixed(2);}
function cls(v){return v>0?'up':(v<0?'down':'');}
function vol(v){if(v==null)return '--';var h=v/100;
return h>=10000?(h/10000).toFixed(2)+'万手':h.toLocaleString('zh-CN',
{minimumFractionDigits:2,maximumFractionDigits:2})+'手';}
function money(v){if(v==null)return '--';var y=v/1e8;
return Math.abs(y)>=10000?(y/10000).toFixed(2)+'万亿':y.toFixed(2)+'亿';}
function spark(closes){
if(!closes||closes.length<2)return '';
var pts=closes.filter(function(v){return typeof v==='number';});
if(pts.length<2)return '';
var lo=Math.min.apply(null,pts),hi=Math.max.apply(null,pts),span=(hi-lo)||1;
var w=140,h=32,step=w/(pts.length-1),coords=[];
pts.forEach(function(v,i){coords.push((i*step).toFixed(1)+','+
(h-2-((v-lo)/span)*(h-4)).toFixed(1));});
return '<svg width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" '+
'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'+
'<polyline points="'+coords.join(' ')+'" fill="none" stroke="#000" '+
'stroke-width="1.5"/></svg>';}
function closesOf(d,code){
var bars=(d.history||{})[code]||[];
return bars.map(function(p){return p[1];});}
function rangeOf(closes){
var pts=closes.filter(function(v){return typeof v==='number';});
if(!pts.length)return null;
return {first:pts[0],last:pts[pts.length-1],
lo:Math.min.apply(null,pts),hi:Math.max.apply(null,pts)};}
function rowEmpty(r){
var keys=['last_price','open_price','high_price','low_price'];
for(var i=0;i<keys.length;i++){var v=r[keys[i]];
if(typeof v==='number'&&v!==0)return false;}
return true;}
function traded(r){return typeof r.volume==='number'&&r.volume!==0;}
function card(r,d,small){
var closes=closesOf(d,r.thscode);
var s=spark(closes),rg=rangeOf(closes);
var empty=rowEmpty(r);
var kv='';
if(empty){
kv='<table class="kv"><tr><td>状态</td><td>未成交（开盘前或接口未返回）</td></tr></table>';
}else if(!small){
kv='<table class="kv"><tr><td>今开</td><td>'+num(r.open_price)+'</td></tr>'+
'<tr><td>最高</td><td>'+num(r.high_price)+'</td></tr>'+
'<tr><td>最低</td><td>'+num(r.low_price)+'</td></tr>'+
(traded(r)?'<tr><td>成交量</td><td>'+vol(r.volume)+'</td></tr>'+
'<tr><td>成交额</td><td>'+money(r.turnover)+'</td></tr>':'<tr><td>成交</td><td>未成交</td></tr>')+'</table>';
}
var sparkHtml='';
if(s){
sparkHtml='<div class="spark">'+s+'<div class="spark-label"><span>近'+
closes.length+'日 '+num(rg.first)+'→'+num(rg.last)+'</span>'+
'<span>低 '+num(rg.lo)+' 高 '+num(rg.hi)+'</span></div></div>';
}
return '<div class="card"><div class="head"><span class="name">'+
esc(r.name||r.thscode)+'</span><span class="code">'+esc(r.thscode||'')+
'</span></div><div class="body"><div class="price">'+
(empty?'<span class="muted">未成交</span>':num(r.last_price))+
'</div><div class="ratio '+cls(r.price_change_ratio_pct)+'">'+
(empty?'':ratio(r.price_change_ratio_pct)+' '+change(r.price_change))+'</div>'+
kv+sparkHtml+'</div></div>';}
function breadthBar(m){
var total=(m.up||0)+(m.down||0)+(m.flat||0);
if(!total)return '';
function w(n){return (100*(n||0)/total).toFixed(2)+'%';}
return '<div class="bar"><span class="u" style="width:'+w(m.up)+'"></span>'+
'<span class="f" style="width:'+w(m.flat)+'"></span>'+
'<span class="d" style="width:'+w(m.down)+'"></span></div>';}
function breadth(m){
if(!m)return '';
var cells=[
['上涨 / 下跌','<span class="up">'+(m.up||0)+'</span> / <span class="down">'+(m.down||0)+'</span>',
'共 '+(m.count||0)+' 只 · 平盘 '+(m.flat||0)],
['涨停 / 跌停','<span class="up">'+(m.limit_up||0)+'</span> / <span class="down">'+(m.limit_down||0)+'</span>',
'无价 '+(m.no_price||0)],
['平均涨跌',ratio(m.avg_ratio),'全 A 算术平均'],
['成交额',money(m.turnover),'量 '+vol(m.volume)]];
var h='<div class="breadth">';
cells.forEach(function(c){h+='<div class="cell"><div class="k">'+c[0]+
'</div><div class="v">'+c[1]+'</div><div class="s">'+c[2]+'</div></div>';});
h+='</div>'+breadthBar(m);
var up=(m.top_gainers||[]),down=(m.top_losers||[]);
if(up.length||down.length){
h+='<div class="two"><div><div class="k">涨幅前五</div>';
up.slice(0,5).forEach(function(g){h+='<div class="rank"><span>'+
esc(g.name||g.thscode)+'</span><b class="up">'+ratio(g.ratio)+'</b></div>';});
h+='</div><div><div class="k">跌幅前五</div>';
down.slice(0,5).forEach(function(g){h+='<div class="rank"><span>'+
esc(g.name||g.thscode)+'</span><b class="down">'+ratio(g.ratio)+'</b></div>';});
h+='</div></div>';}
return h;}
function sectorCard(r){
var empty=rowEmpty(r),rc=r.price_change_ratio_pct||0;
var w=Math.min(100,Math.abs(rc)*10).toFixed(0);
return '<div class="card sector"><div class="head"><span class="name">'+
esc(r.name||r.thscode)+'</span></div><div class="body">'+
'<div class="price '+(empty?'muted':cls(rc))+'">'+
(empty?'未成交':num(r.last_price))+'</div>'+
'<div class="ratio '+cls(rc)+'">'+(empty?'':ratio(r.price_change_ratio_pct))+'</div>'+
'<div class="s">额 '+money(r.turnover)+'</div>'+
'<div class="bar2"><i style="width:'+w+'%"></i></div></div></div>';}
function hkCard(r){
var empty=rowEmpty(r);
return '<div class="card"><div class="head"><span class="name">'+
esc(r.name||r.thscode)+'</span><span class="code">'+
esc(String(r.thscode||'').replace(/^hk/,''))+'</span></div><div class="body">'+
'<div class="price '+(empty?'muted':cls(r.price_change_ratio_pct))+'">'+
(empty?'未成交':num(r.last_price))+'</div>'+
'<div class="ratio '+cls(r.price_change_ratio_pct)+'">'+
(empty?'':ratio(r.price_change_ratio_pct)+' '+change(r.price_change))+'</div>'+
'<table class="kv"><tr><td>成交额</td><td>'+money(r.turnover)+'</td></tr>'+
'<tr><td>成交量</td><td>'+vol(r.volume)+'</td></tr></table></div></div>';}
function weeklyChart(market){
var bars=(market&&market.weekly)||[];
if(bars.length<2)return '<div class="muted">半年 K 线不足</div>';
var closes=bars.map(function(b){return Number(b[4]);});
var volumes=bars.map(function(b){return Number(b[5]||0);});
var lo=Math.min.apply(null,closes),hi=Math.max.apply(null,closes),span=(hi-lo)||1;
var vmax=Math.max.apply(null,volumes)||1,w=145,h=58,priceH=36,step=w/Math.max(1,bars.length-1);
var points=closes.map(function(v,i){return (i*step).toFixed(1)+','+
(2+(hi-v)/span*(priceH-5)).toFixed(1);}).join(' ');
var bw=Math.max(1,w/bars.length-1),svg='<svg viewBox="0 0 '+w+' '+h+'" aria-label="26周量价趋势">';
volumes.forEach(function(v,i){var bh=(v/vmax)*16;svg+='<rect x="'+(i*w/bars.length).toFixed(1)+
'" y="'+(h-bh).toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+bh.toFixed(1)+'" fill="#bbb"/>';});
svg+='<line x1="0" y1="'+priceH+'" x2="'+w+'" y2="'+priceH+'" stroke="#999" stroke-width="0.5"/>'+
'<polyline points="'+points+'" fill="none" stroke="#000" stroke-width="1.8"/></svg>';
return svg;}
function ahMarket(label,code,market){
var m=(market&&market.metrics)||{},q=(market&&market.quote)||{};
return '<div class="leader-market"><div class="ticker">'+esc(label+' '+code)+' · '+pos(q.last_price)+'</div>'+
weeklyChart(market)+'<div class="metrics">半年 '+ratio(m.return_6m_pct)+' · 回撤 '+ratio(m.max_drawdown_pct)+'<br>'+
'20日均额 '+money(m.avg_turnover_20)+' · '+esc(m.signal||'数据不足')+'</div></div>';}
function ahLeaderSection(g){
if(!g||!g.rows||!g.rows.length)return '';
var c=g.coverage||{},h='<section><h2>A/H 龙头半年量价<span class="muted"> 板块 '+
(c.sectors||g.rows.length)+' · 命中 '+(c.matched||0)+' · 无同公司 '+(c.unmatched||0)+'</span></h2>'+
'<p class="src">同一家公司同时有 A/H 股；在板块当前成分内按 A 股近20日平均成交额动态选 1 只。'+
'线=周收盘，柱=周成交量；完整约130根日线见 stocks-leaders.json。</p><div class="leader-grid">';
if(g.errors&&g.errors.length)h+='<p class="src">A/H 子源提示 '+g.errors.length+' 条：'+esc(g.errors[0])+'</p>';
g.rows.forEach(function(r){
h+='<div class="leader-card"><h3>'+esc(r.sector_name||r.sector_code)+'</h3>';
if(r.status!=='ok'){h+='<div class="muted">暂无可验证的 A/H 同公司成分股</div></div>';return;}
var ac=String(r.a_code||'').split('.')[0],hc=String(r.h_code||'').replace(/^hk/,'');
h+='<div class="leader-pair">'+esc(r.name||r.a_code)+' · A'+esc(ac)+' / H'+esc(hc)+
' · '+esc(r.selection_basis||'')+'</div><div class="leader-markets">'+
ahMarket('A',ac,r.a)+ahMarket('H',hc,r.h)+'</div></div>';});
return h+'</div></section>';}
function limitRows(rows){
var h='<table class="data"><tr><th>名称</th><th>最新</th><th>涨跌幅</th><th>封板</th>'+
'<th>原因</th><th>连板</th><th>封单额</th></tr>';
rows.forEach(function(r){h+='<tr><td>'+esc(r.name||r.thscode)+'</td>'+
'<td>'+num(r.last_price)+'</td>'+
'<td class="'+cls(r.price_change_ratio_pct)+'">'+ratio(r.price_change_ratio_pct)+'</td>'+
'<td>'+esc(r.limit_up_time||'')+'</td><td>'+esc(r.limit_up_reason||'')+'</td>'+
'<td>'+esc(r.continue_day_text||'')+'</td><td>'+money(r.seal_money)+'</td></tr>';});
return h+'</table>';}
function num1(v){
return (typeof v==='number')?(Math.round(v*10)/10).toFixed(1):'--';}
// RRG 四象限图：横轴 RS-Ratio、纵轴 RS-Momentum，十字中枢 (100,100)。
// 电子墨水只有黑/灰：领先象限淡灰底 + 黑点，其余为灰点；轨迹是被压成两维的
// 最近几个交易日路径——点朝右上走=继续转强，朝右下走=强弩之末。
function rrgChart(g){
var rows=g.rows||[];
if(!rows.length)return '';
var minx=100,maxx=100,miny=100,maxy=100;
rows.forEach(function(r){
minx=Math.min(minx,r.rs_ratio);maxx=Math.max(maxx,r.rs_ratio);
miny=Math.min(miny,r.rs_mom);maxy=Math.max(maxy,r.rs_mom);});
minx=Math.min(minx,99);maxx=Math.max(maxx,101);
miny=Math.min(miny,99);maxy=Math.max(maxy,101);
var spanx=Math.max(maxx-minx,2),spany=Math.max(maxy-miny,2);
minx-=spanx*0.10;maxx+=spanx*0.10;miny-=spany*0.14;maxy+=spany*0.14;
var W=720,H=400,P=30;
function X(v){return P+(v-minx)/(maxx-minx)*(W-2*P);}
function Y(v){return H-P-(v-miny)/(maxy-miny)*(H-2*P);}
var hot={};
((g.signal||{}).picks||[]).forEach(function(p){hot[p.thscode]=1;});
var h='<div class="rrg-chart"><svg viewBox="0 0 '+W+' '+H+'">';
h+='<rect x="'+X(100).toFixed(1)+'" y="'+Y(maxy).toFixed(1)+'" width="'+
(X(maxx)-X(100)).toFixed(1)+'" height="'+(Y(100)-Y(maxy)).toFixed(1)+'" fill="#f0f0f0"/>';
h+='<line class="grid" x1="'+X(100).toFixed(1)+'" y1="'+P+'" x2="'+X(100).toFixed(1)+'" y2="'+(H-P)+'"/>';
h+='<line class="grid" x1="'+P+'" y1="'+Y(100).toFixed(1)+'" x2="'+(W-P)+'" y2="'+Y(100).toFixed(1)+'"/>';
[[1,'领先',W-P-6,P+16,'end'],[2,'改善',P+6,P+16,'start'],
[3,'滞后',P+6,H-P-6,'start'],[4,'疲软',W-P-6,H-P-6,'end']].forEach(function(q){
h+='<text class="qlabel" x="'+q[2]+'" y="'+q[3]+'" text-anchor="'+q[4]+'">'+q[1]+'</text>';});
rows.forEach(function(r){
var t=r.trail||[];
if(t.length<2)return;
var pts=t.map(function(p){return X(p[0]).toFixed(1)+','+Y(p[1]).toFixed(1);}).join(' ');
h+='<polyline class="trail'+(hot[r.thscode]?' hot':'')+'" points="'+pts+'"/>';});
rows.forEach(function(r){
h+='<circle class="pt'+(hot[r.thscode]?' hot':'')+'" cx="'+X(r.rs_ratio).toFixed(1)+
'" cy="'+Y(r.rs_mom).toFixed(1)+'" r="'+(hot[r.thscode]?4:2.6)+'"><title>'+
esc(r.name)+' '+esc(r.quadrant_name)+' · RS '+num1(r.rs_ratio)+
' / 动量 '+num1(r.rs_mom)+'</title></circle>';});
// 标注：信号 A + 每个象限离中心最远的那个（91 个全标会糊成一团）
var labelled={},labels=[];
rows.forEach(function(r){if(hot[r.thscode]&&!labelled[r.thscode]){labelled[r.thscode]=1;labels.push(r);}});
[1,2,3,4].forEach(function(q){
for(var i=0;i<rows.length;i++){if(rows[i].quadrant===q&&!labelled[rows[i].thscode]){
labelled[rows[i].thscode]=1;labels.push(rows[i]);break;}}});
labels.forEach(function(r){
var x=X(r.rs_ratio),y=Y(r.rs_mom),right=x<W*0.62;
h+='<text class="lbl" x="'+(right?x+7:x-7).toFixed(1)+'" y="'+(y+4).toFixed(1)+
'" text-anchor="'+(right?'start':'end')+'">'+esc(r.name)+'</text>';});
h+='<text class="ax" x="'+P+'" y="'+(H-10)+'">← 相对强度偏弱 · RS-Ratio 中枢 100 · 偏强 →</text>';
h+='<text class="ax" x="'+(W-P)+'" y="'+(H-10)+'" text-anchor="end">↑ 动量上升</text>';
h+='</svg></div>';
return h;}
function resultCard(label,value,sub,rows,tone){
var h='<div class="result-card"><div class="label">'+esc(label)+'</div>'+
'<div class="value '+esc(tone||'')+'">'+esc(value||'--')+'</div>'+
'<div class="sub">'+esc(sub||'')+'</div>';
(rows||[]).forEach(function(row){
h+='<div class="metric"><span>'+esc(row[0])+'</span><span class="'+esc(row[2]||'')+'">'+esc(row[1])+'</span></div>';});
return h+'</div>';}
function decisionSection(d,brief){
if(!d||!d.risk)return '';
var risk=d.risk||{},ops=d.opportunities||[];
var h='<section class="result-section"><h2>风险开关与板块机会池<span class="muted"> '+esc(d.as_of||'')+'</span></h2>';
var reasonRows=(risk.reasons||[]).slice(0,brief?3:5).map(function(x){return ['依据',x];});
h+='<div class="result-grid">';
h+=resultCard('风险开关',(risk.label||'--')+' '+(risk.score==null?'--':risk.score)+'/100',
'纪律仓位 ≤ '+(risk.max_position_pct||0)+'% · '+(risk.stance||''),reasonRows,
risk.mode==='defense'?'down':(risk.mode==='attack'?'up':''));
ops.slice(0,brief?3:5).forEach(function(o){
var rows=[['状态',o.status||'仅观察'],['今日',ratio(o.today_pct)],
['失效',o.invalidation||'强势条件消失']];
if(o.risks&&o.risks.length)rows.push(['风险',o.risks.join('、'),'down']);
h+=resultCard('#'+o.rank+' · '+o.name,(o.score||0)+'/100 · '+(o.role||o.quadrant||''),
(o.reasons||[]).slice(0,3).join(' · '),rows,o.status==='仅观察'?'down':'');});
h+='</div><p class="src">机会池是观察顺序，不是买入指令；仓位为通用纪律上限，单笔风险模板 ≤ '+
Number(risk.single_trade_risk_pct||0.5).toFixed(1)+'%。</p></section>';
return h;}
function volumeVerdict(flow){
var a=flow.top_avg,b=flow.tail_avg;
if(typeof a!=='number'||typeof b!=='number')return '量价数据不足';
var gap=a-b;
if(Math.abs(gap)<0.20)return '量能无避风港';
return gap>0?'资金偏向高成交板块':'高成交板块承压';}
// 结果卡：只给读数、量价结论、真承接和派发预警，不复述计算方法。
function crossSection(g,brief){
if(!g||!g.counts)return '';
var c=g.counts||{},st=g.stats||{},mk=g.market||{},sent=g.sentiment||{};
var flow=g.money||{},intra=g.intraday||{},support=intra.true_support||[],fade=intra.fade_warning||[];
var winners=g.winners||[],losers=g.losers||[],themes=g.themes||[];
var score=(typeof sent.score==='number')?sent.score:null;
var h='<section class="result-section"><h2>结果 · 量价分析<span class="muted"> '+esc(g.as_of||'')+'</span></h2>';
h+='<div class="result-grid">';
h+=resultCard('市场状态',score===null?'--':(score+'/100 · '+(sent.level||'')),sent.divergence||'',[
['板块红 / 绿',(c.up||0)+' / '+(c.down||0)],
['中位 / 平均',ratio(st.median)+' / '+ratio(st.mean)],
['涨停 / 跌停',(mk.limit_up||0)+' / '+(mk.limit_down||0)],
['CSAD',((typeof st.csad==='number')?st.csad.toFixed(2):'--')+(st.herding?' · 羊群':'')]
],score!==null&&score<40?'down':'');
var flowRows=(flow.top||[]).slice(0,brief?3:5).map(function(r){
return [r.name+' · '+Number(r.share||0).toFixed(1)+'%',ratio(r.today_pct),cls(r.today_pct)];});
h+=resultCard('量价结论',volumeVerdict(flow),'成交 '+money(flow.total)+' · '+(flow.top_name||'--')+'占 '+Number(flow.top_share||0).toFixed(1)+'%',flowRows,'');
var supportRows=support.slice(0,brief?2:3).map(function(r){
return [r.name+' · '+(r.tag||'承接'),'隔夜 '+ratio(r.overnight_pct)+' → 日内 '+ratio(r.intraday_pct)+' · CLV '+num1(r.clv),cls(r.intraday_pct)];});
h+=resultCard('真承接',support.length?support.map(function(r){return r.name;}).slice(0,2).join(' / '):'未发现','低开走强、收盘靠近高位',supportRows,'up');
var fadeRows=fade.slice(0,brief?2:4).map(function(r){
return [r.name+' · '+(r.tag||'回落'),'日内 '+ratio(r.intraday_pct)+' · CLV '+num1(r.clv),cls(r.intraday_pct)];});
h+=resultCard('派发预警',fade.length?fade.map(function(r){return r.name;}).slice(0,2).join(' / '):'未发现','高开低走或放量长上影',fadeRows,'down');
var strengthRows=[];
winners.slice(0,3).forEach(function(r){strengthRows.push(['抗跌 · '+r.name,ratio(r.today_pct),cls(r.today_pct)]);});
losers.slice(0,3).forEach(function(r){strengthRows.push(['领跌 · '+r.name,ratio(r.today_pct),cls(r.today_pct)]);});
h+=resultCard('强弱结果',winners.length?winners[0].name:'--',winners.length?('最强 '+ratio(winners[0].today_pct)):'',strengthRows, winners.length?cls(winners[0].today_pct):'');
var themeRows=themes.slice(1,brief?3:5).map(function(t){return [t.name,(t.count||0)+' 只'];});
var lead=themes[0]||{};
h+=resultCard('赚钱主线',lead.name||'--',lead.count?((lead.count||0)+' 只 · '+(lead.examples||[]).join('、')):'',themeRows,'up');
h+='</div></section>';
return h;}

function timingSection(t){
var rows=(t&&t.rows)||[];
if(!rows.length)return '';
var h='<section><h2>指数择时 · ICU 均线 / RSRS(QRS)<span class="muted"> '+rows.length+' 个'+(t.as_of?' · '+esc(t.as_of):'')+'</span></h2>';
h+='<p class="meta">ICU(5)：收盘上穿买、下穿卖。QRS：上穿 +0.7 开、下穿 -0.7 平。</p>';
h+='<table class="card"><tr><th>指数</th><th>收盘</th><th>ICU</th><th>状态</th><th>QRS</th><th>状态</th></tr>';
rows.forEach(function(r){
var cross=r.icu_last_cross?(r.icu_last_cross.side+r.icu_last_cross.bars_ago+'日前'):'近期无穿越';
h+='<tr><td>'+esc(r.name)+'</td><td>'+num(r.close,2)+'</td><td>'+num(r.icu,2)+'</td><td>'+esc(r.icu_state||'--')+'<br><span class="muted">'+esc(cross)+'</span></td><td>'+(r.qrs==null?'--':(r.qrs>0?'+':'')+num(r.qrs,2))+'</td><td>'+esc(r.qrs_state||'--')+'</td></tr>';});
h+='</table>';
(t.notes||[]).forEach(function(n){h+='<p class="meta">'+esc(n)+'</p>';});
return h+'</section>';}

function ewSection(e){
var cov=e.coverage||{};
var ok=(e.rows||[]).filter(function(r){return r.status==='ok';});
if(!ok.length)return '';
var byScore=ok.slice().sort(function(a,b){return b.final_score-a.final_score;});
var strong=byScore.slice(0,6),weak=byScore.slice().reverse().slice(0,6);
function line(r){var st=r.max_streak?('最高'+r.max_streak+'板'):'无连板';
return '<tr><td>'+esc(r.name)+'</td><td>'+(r.final_score>0?'+':'')+num(r.final_score,2)+'</td><td>'+(r.momentum>0?'+':'')+num(r.momentum,2)+'%</td><td>'+num(r.emotion_diff,2)+'</td><td>'+(r.limit_up_count||0)+' · '+st+'</td></tr>';}
var head='<tr><th>板块</th><th>分</th><th>涨跌</th><th>情绪</th><th>涨停 · 连板</th></tr>';
var h='<section><h2>板块情绪 · EW-SDM<span class="muted"> 共 '+ok.length+' 个'+(e.as_of?' · '+esc(e.as_of):'')+'</span></h2>';
h+='<p class="meta">分 = 涨跌幅 × 情绪 × 连板杠杆；分为 0 = 板块内没有涨停、也没有涨超 3% 的股票。覆盖 '+(cov.sectors_ok||0)+'/'+(cov.sectors||0)+' 个板块，成分股 '+(cov.priced||0)+'/'+(cov.members||0)+' 只。</p>';
h+='<h3>强</h3><table class="card">'+head+strong.map(line).join('')+'</table>';
h+='<h3>弱</h3><table class="card">'+head+weak.map(line).join('')+'</table>';
(e.notes||[]).forEach(function(n){h+='<p class="meta">'+esc(n)+'</p>';});
return h+'</section>';}

function rrgSection(g,brief){
var rows=g.rows||[];
if(!rows.length)return '';
var c=g.counts||{},names=['领先','改善','滞后','疲软'];
var keys=['leading','improving','lagging','weakening'];
var h='<section><h2>板块轮动结果<span class="muted"> '+rows.length+' 个 · '+esc(g.as_of||'')+'</span></h2>';
h+=rrgChart(g);
h+='<div class="breadth qcount">';
names.forEach(function(name,i){
h+='<div class="cell"><div class="k">'+name+'</div><div class="v">'+(c[keys[i]]||0)+'</div></div>';});
h+='</div>';
var picks=(g.signal||{}).picks||[];
if(picks.length){
var pickRows=picks.slice(0,6).map(function(r){return [r.name,ratio(r.today_pct),cls(r.today_pct)];});
h+='<div class="result-grid">'+resultCard('领先信号',picks[0].name,(g.signal||{}).name||'',pickRows,'up')+'</div>';}
return h+'</section>';}

function radarSection(r,brief){
if(!r)return '';
window.__RADAR_INDEX__=r.stock_index||[];
var h='<section class="result-section"><h2>结果数据<span class="muted"> '+esc(r.report_period||'')+'</span></h2><div class="result-grid">';
var matches=r.panel_matches||[],cons=r.cn_top1_consensus||[];
var matchRows=matches.slice(0,brief?2:4).map(function(m){
return [m.name+' · '+m.panel_source,ratio(m.today_pct),cls(m.today_pct)];});
if(!matchRows.length)matchRows=cons.slice(0,3).map(function(c){return [c.name,c.top1_managers+' 人第一重仓'];});
h+=resultCard('聪明钱命中',matches.length?matches[0].name:(cons[0]?cons[0].name:'--'),matches.length?matches[0].holders_summary:'暂无当日涨幅榜命中',matchRows,'');
var consRows=cons.slice(0,brief?3:5).map(function(c){return [c.name+' · '+c.sector,c.top1_managers+' 人'];});
h+=resultCard('机构共识',cons[0]?cons[0].name:'--',cons[0]?(cons[0].top1_managers+' 人第一重仓'):'',consRows,'');
var ai=r.ai_data||{},kr=ai.korea_export||{},wsts=ai.global_semi_wsts||{},tw=ai.taiwan_semi_2026_10||[];
var aiRows=[
['韩国半导体出口',ratio(kr.semi_yoy_pct)],
['DRAM / NAND',ratio(kr.dram_yoy_pct)+' / '+ratio(kr.nand_yoy_pct)],
['WSTS 全球月销',ratio(wsts.global_yoy_pct)]
];
tw.slice(0,2).forEach(function(x){aiRows.push([x.name.split(' ')[0],ratio(x.yoy_pct)]);});
h+=resultCard('半导体景气',typeof kr.semi_yoy_pct==='number'?ratio(kr.semi_yoy_pct):'--',(kr.month||'')+' · $'+num(kr.semi_export_usd_b,2)+'B',aiRows,'up');
var ocpi=ai.compute_cost_ocpi||{},gpus=ocpi.gpus||[],ramp=ai.enterprise_adoption_ramp||{};
var computeRows=gpus.slice(0,3).map(function(x){return [x.gpu,'OCPI '+num(x.ocpi,2)];});
computeRows.push(['企业 AI 采用率',ratio(ramp.overall_pct)]);
h+=resultCard('算力与采用',gpus[0]?(gpus[0].gpu+' · '+num(gpus[0].ocpi,2)):'--',ocpi.date||'',computeRows,'');
var bench=r.portfolio_benchmarks||[];
var benchRows=bench.slice(0,brief?3:5).map(function(b){
var v=(b.return_1y_pct!==null&&b.return_1y_pct!==undefined)?('1Y '+ratio(b.return_1y_pct)):('6M '+ratio(b.return_6m_pct));
return [String(b.name||'').replace(/^.*（|）$/g,''),v,((b.return_1y_pct||b.return_6m_pct||0)>=0?'up':'down')];});
h+=resultCard('组合收益',bench[0]?ratio(bench[0].return_1y_pct):'--',bench[0]?String(bench[0].name||'').replace(/^.*（|）$/g,''):'',benchRows,bench[0]&&bench[0].return_1y_pct>=0?'up':'down');
var macro=r.macro_data||{},macroRows=(macro.coverage||[]).map(function(x){return [x.name,Number(x.count||0).toLocaleString()+String(x.unit||'')];});
h+=resultCard('宏观覆盖',macroRows[0]?macroRows[0][1]:'--',macro.refresh||'',macroRows,'');
h+='</div><div class="result-search"><input id="radar-q" type="text" placeholder="输入股票 / 代码 / 基金经理，查看持仓结果" oninput="window.filterRadarStock(this.value)"><div id="radar-results"></div></div>';
return h+'</section>';}

window.filterRadarStock=function(q){
var box=document.getElementById('radar-results');
if(!box)return;
var kw=String(q||'').trim();
if(!kw){box.innerHTML='';return;}
var ku=kw.toUpperCase(),list=window.__RADAR_INDEX__||[];
var hits=list.filter(function(item){
var mgrs=(item.holders||[]).map(function(h){return h.manager||'';}).join(' ');
return String(item.code||'').toUpperCase().indexOf(ku)>=0||String(item.name||'').indexOf(kw)>=0||String(item.sector||'').indexOf(kw)>=0||mgrs.indexOf(kw)>=0;});
if(!hits.length){box.innerHTML='<div class="result-grid">'+resultCard('查询结果','无匹配',kw,[],'')+'</div>';return;}
box.innerHTML='<div class="result-grid">'+hits.slice(0,6).map(function(item){
var rows=(item.holders||[]).slice(0,5).map(function(h){
var value=h.value_yi?(Number(h.value_yi).toFixed(1)+'亿 · '+(h.action||'持有')):(h.action||'持有');
return [h.manager,value];});
return resultCard('持仓结果',item.name,(item.code||'')+' · '+(item.sector||''),rows,'');
}).join('')+'</div>';};

function render(d){
var h='';
var brief=!!(d.view&&d.view.mode==='brief');
// 结果置顶：先决定能不能开新风险，再看量价证据和行情明细。
if(d.decision&&d.decision.risk)h+=decisionSection(d.decision,brief);
if(d.volume_price&&d.volume_price.counts)h+=crossSection(d.volume_price,brief);
else if(d.cross&&d.cross.counts)h+=crossSection(d.cross,brief);
if(d.radar)h+=radarSection(d.radar,brief);
if(d.market){h+='<section><h2>市场概览 · 全 A</h2>'+breadth(d.market)+'</section>';}
var idx=(d.indices||[]),broad=[],sectors=[];
idx.forEach(function(r){
if(r.is_sector||/\.TI$/.test(String(r.thscode||'')))sectors.push(r);else broad.push(r);});
if(broad.length){h+='<section><h2>指数（'+broad.length+'）</h2><div class="cards index">';
broad.forEach(function(r){h+=card(r,d,true);});h+='</div></section>';}
if(sectors.length){
var sorted=sectors.slice().sort(function(a,b){
return (b.price_change_ratio_pct||-999)-(a.price_change_ratio_pct||-999);});
if(brief){
var n=(d.view&&d.view.sector_rows)||5;
var ends=sorted.slice(0,n).concat(sorted.slice(-n));
h+='<section><h2>板块（'+sectors.length+'）<span class="muted"> 精简：抗跌前 '+n+
' / 杀跌前 '+n+'</span></h2><div class="cards sector">';
ends.forEach(function(r){h+=sectorCard(r);});h+='</div><p class="src">其余 '+
Math.max(0,sectors.length-ends.length)+' 个板块见完整版面板（数据在 stocks.json 里是全量）。</p>';
h+='</section>';}
else{
h+='<section><h2>板块（'+sectors.length+'）· 按涨跌幅</h2><div class="cards sector">';
sorted.forEach(function(r){h+=sectorCard(r);});h+='</div></section>';}}
if(d.ah_leaders&&d.ah_leaders.rows)h+=ahLeaderSection(d.ah_leaders);
var hkRows=((d.hk||{}).rows||[]);
if(hkRows.length){
var hi=[],hs=[];
hkRows.forEach(function(r){if(r.is_index)hi.push(r);else hs.push(r);});
h+='<section><h2>港股（'+hkRows.length+'）<span class="muted"> 行情 '+
bj((d.hk||{}).data_time)+'</span></h2><div class="cards hk">';
hi.concat(hs).forEach(function(r){h+=hkCard(r);});h+='</div></section>';}
if(d.limit_up&&d.limit_up.length){
if(brief){
var themes=(d.view&&d.view.themes)||[];
h+='<section><h2>涨停池（'+d.limit_up.length+'）· 按题材</h2><div class="themes">';
themes.forEach(function(t){h+='<span class="chip">'+esc(t.name)+' <b>'+t.count+'</b></span>';});
h+='</div><p class="src">周末精简版：逐只票的封板时间/原因/连板见完整版面板。</p></section>';}
else h+='<section><h2>涨停池（'+d.limit_up.length+
'）· 按封板时间</h2>'+limitRows(d.limit_up)+'</section>';}
// RRG 是量价结果之外的中期轮动补充；没有日 K 时不再重复输出同一份横截面。
if(d.rrg&&d.rrg.rows&&d.rrg.rows.length)h+=rrgSection(d.rrg,brief);
if(d.timing&&d.timing.rows&&d.timing.rows.length)h+=timingSection(d.timing);
if(d.ew_sdm&&d.ew_sdm.rows&&d.ew_sdm.rows.length)h+=ewSection(d.ew_sdm);
(d.errors||[]).forEach(function(e){h+='<p>注：'+esc(e)+'</p>';});
document.getElementById('main').innerHTML=h||'<p>暂无数据</p>';
var demo=d.demo?'【演示数据】':'';
var briefTag=brief?'【周末精简版 · '+((d.view||{}).reason||'')+'】':'';
var stale=isStale(d.data_time);
document.getElementById('meta').textContent='生成 '+bj(d.generated)+'（北京时间）· 数据时间 '+
bj(d.data_time)+(ageText(d.data_time)?' · '+ageText(d.data_time):'')+
(sessionLabel(d.data_time)?' · '+sessionLabel(d.data_time):'')+(demo?' '+demo:'')+
(briefTag?' '+briefTag:'')+
' · 每'+POLL_TEXT+'检查一次';
var badge=document.getElementById('state');
badge.textContent=stale?'STALE':'ONLINE';
badge.className=stale?'badge stale':'badge';
var hint=document.getElementById('stale-hint');
if(hint)hint.style.display=stale?'block':'none';
}
function load(){
fetch('stocks.json?t='+Date.now(),{cache:'no-store'}).then(function(r){
if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(d){
if(d.generated===lastGenerated)return;lastGenerated=d.generated;render(d);
}).catch(function(){document.getElementById('state').textContent='OFFLINE / CACHED';});}
load();setInterval(load,POLL*1000);
</script>
</body>
</html>
"""


def render_html():
    return (HTML_TEMPLATE.replace("__TITLE__", TITLE)
            .replace("__DISCLAIMER__", DISCLAIMER)
            .replace("__POLL_TEXT__", poll_text())
            .replace("__STALE__", str(STALE_MINUTES))
            .replace("__POLL__", str(POLL_SECONDS)))


# ---------------------------------------------------------------------------
# EPUB（Kindle 字模式卡片版）：crengine 不认 flex/grid/SVG，这里用表格排卡片，
# 迷你走势图只出现在网页面板；EPUB 里给 20 日收盘的起止与区间高低。
# ---------------------------------------------------------------------------

EPUB_CSS = """body{font-family:sans-serif;line-height:1.5}
h1{font-size:1.4em;border-bottom:2px solid #000;padding-bottom:4px}
h2{font-size:1.1em;font-weight:normal;border-bottom:1px solid #000;padding-bottom:2px;margin-top:1.2em}
.meta{font-size:0.85em}
table.card{border:1px solid #000;width:100%;border-collapse:collapse;margin:8px 0}
table.card td{padding:3px 6px;vertical-align:top}
table.card .big{font-size:1.3em}
table.data{border-collapse:collapse;width:100%}
table.data th,table.data td{border:1px solid #999;padding:3px 5px;font-size:0.9em}
.up,.down{font-weight:normal}
"""

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def _epub_escape(text):
    return html_mod.escape(str(text if text is not None else ""), quote=True)


def render_epub_xhtml(payload, generated_bj, brief=None):
    """EPUB 章节 XHTML：市场概览 + 宽基 + 板块 + 港股 + 涨停（crengine 友好）。

    只用表格和标题，不用 flex/grid/SVG —— Kindle 的 crengine 不支持。

    ``brief`` 是「周末精简版」：板块表只留抗跌/杀跌各 N 行、涨停池只留题材分布、
    分析段只留结论（与纯文本版同一口径，见 brief_view）。
    """
    esc = _epub_escape
    demo = "【演示数据】" if payload.get("demo") else ""
    parts = [f"<h1>{esc(TITLE)}{esc(demo)}</h1>",
             f'<p class="meta">生成 {esc(generated_bj)} 北京时间 · {esc(status_line(payload))}</p>',
             f'<p class="meta">{esc(DISCLAIMER)}</p>']
    if brief:
        parts.append('<p class="meta">'
                     f'{esc(brief_banner(brief, len(sector_indices(payload))))}</p>')
    if not today_has_values(payload):
        parts.append('<p class="meta">注：这一版快照没有成交数据（开盘前/接口给空壳），'
                     '下面显示 -- 与「未成交」，下一轮再看。</p>')

    decision = decision_section(payload)
    risk = decision.get("risk") or {}
    parts.append(f"<h2>风险开关与板块机会池（{esc(decision.get('as_of') or '—')}）</h2>")
    parts.append('<table class="card"><tr><th>风险开关</th><th>纪律上限</th></tr>')
    parts.append(
        f"<tr><td>{esc(risk.get('label') or '—')} {esc(risk.get('score'))}/100</td>"
        f"<td>总仓≤{risk.get('max_position_pct', 0)}% · 单笔风险≤"
        f"{esc(risk.get('single_trade_risk_pct', 0.5))}%</td></tr>"
    )
    parts.append(f"<tr><td colspan=\"2\">{esc(risk.get('stance') or '仅观察')}</td></tr></table>")
    opportunities = decision.get("opportunities") or []
    if opportunities:
        parts.append('<table class="data"><tr><th>#</th><th>板块</th><th>分数</th>'
                     '<th>状态</th><th>今日</th><th>失效条件</th></tr>')
        for row in opportunities[:3 if brief else OPPORTUNITY_LIMIT]:
            parts.append(
                f"<tr><td>{row.get('rank')}</td><td>{esc(row.get('name'))}"
                f" · {esc(row.get('role') or '')}</td>"
                f"<td>{row.get('score')}/100</td><td>{esc(row.get('status'))}</td>"
                f"<td>{esc(fmt_ratio(row.get('today_pct')))}</td>"
                f"<td>{esc(row.get('invalidation'))}</td></tr>"
            )
        parts.append("</table>")
    parts.append('<p class="meta">机会池是观察顺序，不是买入指令；仓位是通用纪律模板。</p>')

    volume = volume_price_section(payload)
    if volume:
        counts = volume.get("counts") or {}
        stats = volume.get("stats") or {}
        market = volume.get("market") or {}
        sent = volume.get("sentiment") or {}
        flow = volume.get("money") or {}
        intra = volume.get("intraday") or {}
        parts.append(f"<h2>结果 · 量价分析（{esc(volume.get('as_of') or '—')}）</h2>")
        parts.append('<table class="card"><tr><th>结果卡</th><th>读数</th></tr>')
        parts.append(
            f"<tr><td>市场状态</td><td>{esc(sent.get('level') or '—')} "
            f"{esc(sent.get('score'))}/100 · 红{counts.get('up', 0)}/绿{counts.get('down', 0)}"
            f" · 中位{esc(fmt_ratio(stats.get('median')))} · CSAD {esc(fmt_one(stats.get('csad')))}</td></tr>"
        )
        top_avg, tail_avg = flow.get("top_avg"), flow.get("tail_avg")
        if isinstance(top_avg, (int, float)) and isinstance(tail_avg, (int, float)):
            gap = top_avg - tail_avg
            verdict = ("量能无避风港" if abs(gap) < 0.20 else
                       ("资金偏向高成交板块" if gap > 0 else "高成交板块承压"))
        else:
            verdict = "量价数据不足"
        parts.append(
            f"<tr><td>量价结论</td><td>{esc(verdict)} · 成交{esc(fmt_turnover(flow.get('total')))}"
            f" · {esc(flow.get('top_name') or '—')}占{flow.get('top_share', 0):.1f}%"
            f" / 今日{esc(fmt_ratio(flow.get('top_pct')))}</td></tr>"
        )
        parts.append(
            f"<tr><td>涨停 / 跌停</td><td>{market.get('limit_up', 0)} / {market.get('limit_down', 0)}"
            f" · {esc(sent.get('divergence') or '')}</td></tr>"
        )
        parts.append("</table>")

        support = intra.get("true_support") or []
        fades = intra.get("fade_warning") or []
        if support or fades:
            parts.append('<table class="card"><tr><th>量价信号</th><th>隔夜 → 日内 / CLV</th></tr>')
            for row in support[:3]:
                parts.append(
                    f"<tr><td>承接 · {esc(row.get('name'))}</td>"
                    f"<td>{esc(fmt_ratio(row.get('overnight_pct')))} → "
                    f"{esc(fmt_ratio(row.get('intraday_pct')))} / {esc(fmt_one(row.get('clv')))}</td></tr>"
                )
            for row in fades[: (3 if brief else 5)]:
                parts.append(
                    f"<tr><td>派发 · {esc(row.get('name'))}</td>"
                    f"<td>{esc(row.get('tag') or '')} · 日内{esc(fmt_ratio(row.get('intraday_pct')))}"
                    f" / {esc(fmt_one(row.get('clv')))}</td></tr>"
                )
            parts.append("</table>")

        themes = volume.get("themes") or []
        if themes:
            parts.append('<table class="card"><tr><th>赚钱主线</th><th>数量</th></tr>')
            for row in themes[:4]:
                parts.append(
                    f"<tr><td>{esc(row.get('name'))}</td><td>{row.get('count', 0)} 只</td></tr>"
                )
            parts.append("</table>")


    radar = radar_section(payload)
    if radar:
        parts.extend(jrglobal_radar.render_radar_epub(radar, brief=bool(brief), esc=esc))


    market = payload.get("market") or {}
    if market:
        parts.append("<h2>市场概览 · 全 A</h2>")
        parts.append('<table class="data">'
                     "<tr><th>上涨</th><th>下跌</th><th>平盘</th><th>涨停</th>"
                     "<th>跌停</th><th>平均涨跌</th><th>成交额</th></tr>"
                     f"<tr><td>{market.get('up', 0)}</td><td>{market.get('down', 0)}</td>"
                     f"<td>{market.get('flat', 0)}</td><td>{market.get('limit_up', 0)}</td>"
                     f"<td>{market.get('limit_down', 0)}</td>"
                     f"<td>{esc(fmt_ratio(market.get('avg_ratio')))}</td>"
                     f"<td>{esc(fmt_turnover(market.get('turnover')))}</td></tr></table>")
        parts.append(f'<p class="meta">共 {market.get("count", 0)} 只 · '
                     f'无价 {market.get("no_price", 0)} · '
                     f'成交量 {esc(fmt_volume(market.get("volume")))}</p>')
        gainers = (market.get("top_gainers") or [])[:5]
        losers = (market.get("top_losers") or [])[:5]
        if gainers or losers:
            parts.append('<table class="data"><tr><th>涨幅前列</th><th>涨跌幅</th>'
                         "<th>跌幅前列</th><th>涨跌幅</th></tr>")
            for i in range(max(len(gainers), len(losers))):
                g = gainers[i] if i < len(gainers) else {}
                d = losers[i] if i < len(losers) else {}
                parts.append(
                    "<tr>"
                    f"<td>{esc(g.get('name') or g.get('thscode') or '')}</td>"
                    f"<td>{esc(fmt_ratio(g.get('ratio')))}</td>"
                    f"<td>{esc(d.get('name') or d.get('thscode') or '')}</td>"
                    f"<td>{esc(fmt_ratio(d.get('ratio')))}</td></tr>")
            parts.append("</table>")

    def index_table(rows, title):
        parts.append(f"<h2>{esc(title)}（{len(rows)}）</h2>")
        parts.append('<table class="data"><tr><th>名称</th><th>最新</th>'
                     "<th>涨跌幅</th><th>涨跌</th><th>成交额</th></tr>")
        for row in rows:
            if row_is_empty(row):
                parts.append(f"<tr><td>{esc(row.get('name') or row.get('thscode'))}</td>"
                             '<td colspan="4">未成交</td></tr>')
                continue
            parts.append(
                f"<tr><td>{esc(row.get('name') or row.get('thscode'))}</td>"
                f"<td>{esc(fmt_price(row.get('last_price')))}</td>"
                f"<td>{esc(fmt_ratio(row.get('price_change_ratio_pct')))}</td>"
                f"<td>{esc(fmt_change(row.get('price_change')))}</td>"
                f"<td>{esc(fmt_turnover(row.get('turnover')))}</td></tr>")
        parts.append("</table>")

    indices = payload.get("indices") or []
    broad = [r for r in indices if not is_sector_row(r)]
    sectors = sorted([r for r in indices if is_sector_row(r)],
                     key=lambda r: -((r.get("price_change_ratio_pct") or -999)))
    if broad:
        index_table(broad, "指数")
    if sectors:
        up = sum(1 for r in sectors if (r.get("price_change_ratio_pct") or 0) > 0)
        if brief:
            rows = int(brief.get("sector_rows") or BRIEF_SECTOR_ROWS)
            head_rows = sectors[:rows]
            tail_rows = list(reversed(sectors[-rows:])) if len(sectors) > rows else []
            index_table(head_rows + tail_rows,
                        f"板块 · 涨 {up} 跌 {len(sectors) - up}"
                        f" · 精简：抗跌前 {rows} / 杀跌前 {rows}")
            rest = len(sectors) - len(head_rows) - len(tail_rows)
            if rest > 0:
                parts.append(f'<p class="meta">其余 {rest} 个板块见网页面板 stocks.html'
                             "（数据在 stocks.json 里是全量）</p>")
        else:
            index_table(sectors, f"板块 · 涨 {up} 跌 {len(sectors) - up}")

    leaders = ah_leader_section(payload)
    leader_rows = leaders.get("rows") or []
    if leader_rows:
        coverage = leaders.get("coverage") or {}
        parts.append(f"<h2>A/H 龙头半年量价（命中 {coverage.get('matched', 0)} / "
                     f"板块 {coverage.get('sectors', len(leader_rows))}）</h2>")
        parts.append('<p class="meta">同一家公司同时有 A/H 股；板块成分内按 A 股近20日'
                     '平均成交额选 1 只。价/量为最近26周压缩趋势，完整日线见 '
                     'stocks-leaders.json。</p>')
        leader_errors = leaders.get("errors") or []
        if leader_errors:
            parts.append(f'<p class="meta">A/H 子源提示 {len(leader_errors)} 条：'
                         f'{esc(leader_errors[0])}</p>')
        parts.append('<table class="data"><tr><th>板块 / 龙头</th><th>A 半年</th>'
                     '<th>H 半年</th><th>量价状态</th></tr>')
        for row in leader_rows:
            sector = row.get("sector_name") or row.get("sector_code") or ""
            if row.get("status") != "ok":
                parts.append(f'<tr><td>{esc(sector)}</td>'
                             '<td colspan="3">暂无可验证的 A/H 同公司成分股</td></tr>')
                continue
            am = (row.get("a") or {}).get("metrics") or {}
            hm = (row.get("h") or {}).get("metrics") or {}
            ac = str(row.get("a_code") or "").split(".")[0]
            hc = str(row.get("h_code") or "").replace("hk", "")
            ap, av = _weekly_trends(row.get("a"))
            hp, hv = _weekly_trends(row.get("h"))
            parts.append(
                f'<tr><td>{esc(sector)} · {esc(row.get("name"))}<br/>'
                f'A{esc(ac)} / H{esc(hc)}</td>'
                f'<td>{esc(fmt_ratio(am.get("return_6m_pct")))}<br/>'
                f'回撤 {esc(fmt_ratio(am.get("max_drawdown_pct")))}</td>'
                f'<td>{esc(fmt_ratio(hm.get("return_6m_pct")))}<br/>'
                f'回撤 {esc(fmt_ratio(hm.get("max_drawdown_pct")))}</td>'
                f'<td>A {esc(am.get("signal") or "数据不足")}<br/>'
                f'H {esc(hm.get("signal") or "数据不足")}</td></tr>'
                f'<tr><td colspan="4">A 价{esc(ap)} 量{esc(av)}<br/>'
                f'H 价{esc(hp)} 量{esc(hv)}</td></tr>'
            )
        parts.append("</table>")

    hk = payload.get("hk") or {}
    hk_rows = hk.get("rows") or []
    if hk_rows:
        stamp = hk.get("data_timestamp")
        when = beijing_text(stamp) if isinstance(stamp, (int, float)) else "无"
        parts.append(f"<h2>港股（{len(hk_rows)}）</h2>")
        parts.append(f'<p class="meta">行情 {esc(when)} · 腾讯公开行情接口 · 币种 HKD</p>')
        parts.append('<table class="data"><tr><th>名称</th><th>最新</th>'
                     "<th>涨跌幅</th><th>成交额</th></tr>")
        ordered = ([r for r in hk_rows if r.get("is_index")]
                   + [r for r in hk_rows if not r.get("is_index")])
        for row in ordered:
            if row_is_empty(row):
                parts.append(f"<tr><td>{esc(row.get('name') or row.get('thscode'))}</td>"
                             '<td colspan="3">未成交</td></tr>')
                continue
            parts.append(
                f"<tr><td>{esc(row.get('name') or row.get('thscode'))}</td>"
                f"<td>{esc(fmt_price(row.get('last_price')))}</td>"
                f"<td>{esc(fmt_ratio(row.get('price_change_ratio_pct')))}</td>"
                f"<td>{esc(fmt_turnover(row.get('turnover')))}</td></tr>")
        parts.append("</table>")

    limit_up = payload.get("limit_up") or []
    if limit_up and brief:
        parts.append(f"<h2>涨停池（{len(limit_up)}）· 按题材</h2>")
        themes = limit_up_theme_text(limit_up)
        parts.append(f"<p>{esc('题材 ' + themes) if themes else '接口没给涨停原因，题材分布这一轮算不出'}</p>")
    elif limit_up:
        parts.append(f"<h2>涨停池（{len(limit_up)}）</h2>")
        parts.append('<table class="data"><tr><th>名称</th><th>最新</th><th>涨跌幅</th>'
                     "<th>封板</th><th>原因</th><th>连板</th><th>封单额</th></tr>")
        for row in limit_up:
            parts.append(
                f"<tr><td>{esc(row.get('name') or row.get('thscode'))}</td>"
                f"<td>{esc(fmt_price(row.get('last_price')))}</td>"
                f"<td>{esc(fmt_ratio(row.get('price_change_ratio_pct')))}</td>"
                f"<td>{esc(row.get('limit_up_time') or '')}</td>"
                f"<td>{esc(row.get('limit_up_reason') or '')}</td>"
                f"<td>{esc(row.get('continue_day_text') or '')}</td>"
                f"<td>{esc(fmt_seal(row.get('seal_money')))}</td></tr>")
        parts.append("</table>")

    section = rrg_section(payload)
    if section:
        counts = section.get("counts") or {}
        parts.append(f"<h2>板块轮动结果（{len(section.get('rows') or [])}）</h2>")
        parts.append('<table class="card"><tr><th>象限</th><th>数量</th></tr>')
        for key, label in (("leading", "领先"), ("improving", "改善"),
                           ("weakening", "疲软"), ("lagging", "滞后")):
            parts.append(f"<tr><td>{label}</td><td>{counts.get(key, 0)}</td></tr>")
        parts.append("</table>")
        signal = (section.get("signal") or {}).get("picks") or []
        if signal:
            parts.append('<table class="card"><tr><th>信号</th><th>今日</th></tr>')
            for row in signal[:6]:
                parts.append(
                    f"<tr><td>{esc(row.get('name'))}</td>"
                    f"<td>{esc(fmt_ratio(row.get('today_pct')))}</td></tr>"
                )
            parts.append("</table>")

    # 指数择时：ICU 与 QRS 各一张小表（与纯文本同口径）。
    timing_part = payload.get("timing")
    if timing_part and timing_part.get("rows"):
        parts.append("<h2>指数择时 · ICU 均线 / RSRS(QRS)</h2>")
        parts.append('<table class="card"><tr><th>指数</th><th>收盘</th><th>ICU(5)</th>'
                     '<th>ICU 状态</th><th>QRS</th><th>QRS 状态</th></tr>')
        for row in timing_part["rows"]:
            close = row.get("close")
            icu = row.get("icu")
            qrs = row.get("qrs")
            parts.append(
                f"<tr><td>{esc(row.get('name'))}</td>"
                f"<td>{'--' if close is None else f'{close:.2f}'}</td>"
                f"<td>{'--' if icu is None else f'{icu:.2f}'}</td>"
                f"<td>{esc(row.get('icu_state') or '--')}</td>"
                f"<td>{'--' if qrs is None else f'{qrs:+.2f}'}</td>"
                f"<td>{esc(row.get('qrs_state') or '--')}</td></tr>")
        parts.append("</table>")
        for note in timing_part.get("notes") or []:
            parts.append(f'<p class="meta">{esc(note)}</p>')

    # 板块情绪 EW-SDM：强 / 弱各取 6 个（与纯文本同口径），口径说明放在表下。
    ew_part = payload.get("ew_sdm")
    ok_rows = [r for r in (ew_part or {}).get("rows") or [] if r.get("status") == "ok"]
    if ok_rows:
        parts.append(f"<h2>板块情绪 · EW-SDM（共 {len(ok_rows)} 个板块）</h2>")
        parts.append('<table class="card"><tr><th>板块</th><th>分</th><th>涨跌</th>'
                     '<th>情绪</th><th>连板</th></tr>')
        shown = ok_rows[:6] + [r for r in ok_rows[::-1][:6] if r not in ok_rows[:6]]
        for row in shown:
            parts.append(
                f"<tr><td>{esc(row.get('name'))}</td>"
                f"<td>{row.get('final_score', 0):+.2f}</td>"
                f"<td>{esc(fmt_ratio(row.get('momentum')))}</td>"
                f"<td>{row.get('emotion_diff', 0):.2f}</td>"
                f"<td>{row.get('max_streak') or '--'}</td></tr>")
        parts.append("</table>")
        for note in ew_part.get("notes") or []:
            parts.append(f'<p class="meta">{esc(note)}</p>')


    for error in payload.get("errors") or []:
        parts.append(f'<p class="meta">注：{esc(error)}</p>')
    parts.append(f"<p>{esc(DISCLAIMER)}</p>")
    body = "\n".join(parts)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">'
            f"<head><title>{esc(TITLE)}</title>"
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>'
            f"<body>{body}</body></html>\n")


def write_epub(path, xhtml, title=TITLE):
    """写一个自包含的 EPUB 3（mimetype 必须第一且不压缩）。"""
    import uuid
    import zipfile

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    identifier = "urn:uuid:" + str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"004-kindle:stocks:{hashlib.sha256(xhtml.encode('utf-8')).hexdigest()}"))
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">
    <dc:identifier id="BookID">{identifier}</dc:identifier>
    <dc:title>{_epub_escape(title)}</dc:title>
    <dc:language>zh-CN</dc:language>
    <meta property="dcterms:modified">{stamp}</meta>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="style" href="style.css" media-type="text/css"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"><itemref idref="chapter"/></spine>
</package>
"""
    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN">
<head><title>{_epub_escape(title)}</title></head>
<body><nav epub:type="toc"><h1>{_epub_escape(title)}</h1>
<ol><li><a href="chapter.xhtml">{_epub_escape(title)}</a></li></ol></nav></body></html>
"""
    ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{identifier}"/></head>
  <docTitle><text>{_epub_escape(title)}</text></docTitle>
  <navMap><navPoint id="n1" playOrder="1"><navLabel><text>{_epub_escape(title)}</text></navLabel>
  <content src="chapter.xhtml"/></navPoint></navMap>
</ncx>
"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(temporary, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML,
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/style.css", EPUB_CSS, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/chapter.xhtml", xhtml, compress_type=zipfile.ZIP_DEFLATED)
    temporary.replace(path)


def check(out_dir):
    """产物自检：清单可解析、字段齐、文本非空、面板带定时轮询。"""
    out = Path(out_dir)
    problems = []
    manifest_path = out / "stocks.json"
    if not manifest_path.is_file():
        return ["缺少 stocks.json"]
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"stocks.json 解析失败：{exc}"]
    for key in ("schema", "generated", "doc", "title", "data_hash", "counts"):
        if not data.get(key):
            problems.append(f"stocks.json 缺少 {key}")
    compact_path = out / "stocks-manifest.json"
    if not compact_path.is_file():
        problems.append("缺少 stocks-manifest.json")
    else:
        try:
            compact = json.loads(compact_path.read_text(encoding="utf-8"))
            for key in ("schema", "generated", "doc", "epub", "data_time", "data_hash"):
                if compact.get(key) != data.get(key):
                    problems.append(f"stocks-manifest.json 的 {key} 与主清单不一致")
            if "indices" in compact or "ah_leaders" in compact:
                problems.append("stocks-manifest.json 不应携带全量行情")
        except ValueError as exc:
            problems.append(f"stocks-manifest.json 解析失败：{exc}")
    if data.get("doc") != "stocks.txt":
        problems.append("stocks.json 的 doc 不是 stocks.txt")
    counts = data.get("counts") or {}
    # 宽基 / 板块 / 全市场 / 港股，任何一块有东西就不算空面板
    # （以前只认 indices + stocks，只有板块的产物会被误判成空）
    if not any(counts.get(key) for key in ("indices", "sectors", "market", "hk")):
        problems.append("stocks.json 里指数、板块、全市场、港股都是空的")
    txt_path = out / "stocks.txt"
    if not txt_path.is_file() or txt_path.stat().st_size < 100:
        problems.append("stocks.txt 缺失或过小")
    elif TITLE not in txt_path.read_text(encoding="utf-8"):
        problems.append("stocks.txt 里没有面板标题")
    html_path = out / "stocks.html"
    if not html_path.is_file():
        problems.append("缺少 stocks.html")
    else:
        page = html_path.read_text(encoding="utf-8")
        if "stocks.json" not in page or f"var POLL = {POLL_SECONDS}" not in page:
            problems.append(f"stocks.html 没有每 {poll_text()}拉 stocks.json 的轮询")
    cross = data.get("cross") or {}
    volume = data.get("volume_price") or cross
    if volume.get("counts"):
        page = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
        if "结果 · 量价分析" not in page or "function crossSection" not in page:
            problems.append("清单里有量价结果，但 stocks.html 没有结果卡")
        if txt_path.is_file() and "【结果 · 量价】" not in txt_path.read_text(encoding="utf-8"):
            problems.append("清单里有量价结果，但 stocks.txt 没有结果卡")
    decision = data.get("decision") or {}
    risk = decision.get("risk") or {}
    if risk.get("mode") not in ("attack", "neutral", "defense"):
        problems.append("stocks.json 的风险开关缺失或 mode 无效")
    if not isinstance(risk.get("score"), (int, float)) or not 0 <= risk.get("score", -1) <= 100:
        problems.append("stocks.json 的风险分不在 0–100")
    if not isinstance(risk.get("max_position_pct"), (int, float)) \
            or not 0 <= risk.get("max_position_pct", -1) <= 100:
        problems.append("stocks.json 的纪律仓位上限无效")
    opportunities = decision.get("opportunities") or []
    if len(opportunities) > OPPORTUNITY_LIMIT:
        problems.append(f"stocks.json 的板块机会池超过 {OPPORTUNITY_LIMIT} 个")
    if opportunities and [row.get("rank") for row in opportunities] != list(range(1, len(opportunities) + 1)):
        problems.append("stocks.json 的板块机会池排名不连续")
    text_body = txt_path.read_text(encoding="utf-8") if txt_path.is_file() else ""
    if "【风险开关】" not in text_body or "【板块机会池】" not in text_body:
        problems.append("stocks.txt 没有风险开关或板块机会池")
    page = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
    if "function decisionSection" not in page or "d.decision" not in page:
        problems.append("stocks.html 没有风险开关或板块机会池")
    leaders = data.get("ah_leaders") or {}
    leader_rows = leaders.get("rows") or []
    if leader_rows:
        coverage = leaders.get("coverage") or {}
        if len(leader_rows) != coverage.get("sectors"):
            problems.append("A/H 龙头行数与板块覆盖数不一致")
        if any(row.get("status") not in ("ok", "no_match") for row in leader_rows):
            problems.append("A/H 龙头存在未知状态")
        if "function ahLeaderSection" not in page or "weeklyChart" not in page:
            problems.append("stocks.html 没有 A/H 龙头周量价图")
        if "【A/H 龙头半年量价】" not in text_body:
            problems.append("stocks.txt 没有 A/H 龙头半年量价段")
        sidecar_path = out / str(data.get("leader_history") or "stocks-leaders.json")
        if not sidecar_path.is_file():
            problems.append("清单有 A/H 龙头但缺少 stocks-leaders.json")
        else:
            try:
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                if not isinstance(sidecar.get("companies"), dict):
                    problems.append("stocks-leaders.json 缺少 companies")
            except ValueError as exc:
                problems.append(f"stocks-leaders.json 解析失败：{exc}")
    if cross.get("counts") and (data.get("rrg") or {}).get("rows"):
        # 两者互斥：量化分析段只显示一个，清单里不该同时有两段
        problems.append("清单里 rrg 与 cross 同时存在")
    rrg = data.get("rrg") or {}
    if rrg.get("rows"):
        if not rrg.get("narrative"):
            problems.append("stocks.json 的 rrg 没有叙述文字")
        page = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
        if "板块轮动结果" not in page:
            problems.append("清单里有 rrg，但 stocks.html 没有板块轮动结果")
        if txt_path.is_file() and "板块轮动 · RRG" not in txt_path.read_text(encoding="utf-8"):
            problems.append("清单里有 rrg，但 stocks.txt 没有板块轮动段")
    ew = data.get("ew_sdm") or {}
    if ew.get("rows"):
        page = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
        if "板块情绪 · EW-SDM" not in page:
            problems.append("清单里有 ew_sdm，但 stocks.html 没有板块情绪段")
        if txt_path.is_file() and "板块情绪 · EW-SDM" not in txt_path.read_text(encoding="utf-8"):
            problems.append("清单里有 ew_sdm，但 stocks.txt 没有板块情绪段")
    timing = data.get("timing") or {}
    if timing.get("rows"):
        page = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
        if "指数择时" not in page:
            problems.append("清单里有 timing，但 stocks.html 没有指数择时")
        if txt_path.is_file() and "指数择时" not in txt_path.read_text(encoding="utf-8"):
            problems.append("清单里有 timing，但 stocks.txt 没有指数择时段")
    view = data.get("view") or {}
    if view.get("mode") == "brief" and txt_path.is_file():
        if "【周末精简版" not in txt_path.read_text(encoding="utf-8"):
            problems.append("清单标了周末精简版，但 stocks.txt 没有精简横幅")
    epub_path = out / "stocks.epub"
    if not epub_path.is_file():
        problems.append("缺少 stocks.epub")
    else:
        import zipfile
        try:
            with zipfile.ZipFile(epub_path) as zf:
                names = zf.namelist()
                if not names or names[0] != "mimetype":
                    problems.append("stocks.epub 的 mimetype 不是第一项")
                if "OEBPS/chapter.xhtml" not in names:
                    problems.append("stocks.epub 里没有章节")
                chapter = zf.read("OEBPS/chapter.xhtml").decode("utf-8")
                if TITLE not in chapter:
                    problems.append("stocks.epub 章节里没有面板标题")
                if "风险开关与板块机会池" not in chapter:
                    problems.append("stocks.epub 章节里没有风险开关或板块机会池")
                if leader_rows and "A/H 龙头半年量价" not in chapter:
                    problems.append("stocks.epub 章节里没有 A/H 龙头半年量价")
        except zipfile.BadZipFile as exc:
            problems.append(f"stocks.epub 不是有效 EPUB：{exc}")
    return problems


# 演示数据：取自同花顺官方文档的响应示例，只为没 Key 时先看版式。
# 真实数据由工作流抓取后覆盖；面板与清单都标了「演示数据」。
DEMO_PAYLOAD = {
    "demo": True,
    "data_timestamp": 1784275991000,
    "indices": [
        {"thscode": "000001.SH", "ticker": "000001", "name": "上证指数",
         "last_price": 3665.94, "price_change": 16.79,
         "price_change_ratio_pct": 0.46, "open_price": 3650.12,
         "high_price": 3670.0, "low_price": 3648.5, "prev_price": 3649.15,
         "volume": 51234567800.0, "turnover": 1567890000000.0},
        {"thscode": "399006.SZ", "ticker": "399006", "name": "创业板指",
         "last_price": 2478.94, "price_change": 13.86,
         "price_change_ratio_pct": 0.56, "open_price": 2468.0,
         "high_price": 2485.0, "low_price": 2465.0, "prev_price": 2465.08,
         "volume": 23456789000.0, "turnover": 987650000000.0},
        {"thscode": "881101.TI", "ticker": "881101", "name": "种植业与林业",
         "last_price": 3732.1, "price_change": -56.6,
         "price_change_ratio_pct": -1.49, "volume": 2345678900.0,
         "turnover": 3456789000.0, "is_sector": True},
        {"thscode": "881121.TI", "ticker": "881121", "name": "半导体",
         "last_price": 4521.7, "price_change": 152.3,
         "price_change_ratio_pct": 3.49, "volume": 8765432100.0,
         "turnover": 234567890000.0, "is_sector": True},
    ],
    "market": {
        "count": 5584, "up": 2861, "down": 2413, "flat": 126, "no_price": 184,
        "limit_up": 58, "limit_down": 3, "turnover": 1712345678900.0,
        "volume": 123456789000.0, "avg_ratio": 0.32,
        "top_gainers": [
            {"thscode": "300750.SZ", "name": "宁德时代", "ratio": 10.02,
             "last_price": 268.4},
            {"thscode": "002594.SZ", "name": "比亚迪", "ratio": 9.98,
             "last_price": 312.5}],
        "top_losers": [
            {"thscode": "600036.SH", "name": "招商银行", "ratio": -9.87,
             "last_price": 32.1}],
    },
    "hk": {
        "generated": "2026-11-17T07:00:00+00:00",
        "data_timestamp": 1784275991000,
        "rows": [
            {"thscode": "hkHSI", "name": "恒生指数", "last_price": 23294.75,
             "price_change": -214.6, "price_change_ratio_pct": -0.91,
             "volume": 1234567890.0, "turnover": 87654321000.0, "is_index": True},
            {"thscode": "hkHSTECH", "name": "恒生科技", "last_price": 5218.4,
             "price_change": -61.2, "price_change_ratio_pct": -1.16,
             "volume": 987654321.0, "turnover": 45678901234.0, "is_index": True},
            {"thscode": "hk00700", "name": "腾讯控股", "last_price": 372.8,
             "price_change": -3.2, "price_change_ratio_pct": -0.85,
             "volume": 21345678.0, "turnover": 7890123456.0, "is_index": False},
        ],
        "errors": [],
    },
    "limit_up": [
        {"thscode": "603986.SH", "ticker": "603986", "name": "兆易创新",
         "last_price": 118.23, "price_change_ratio_pct": 10.0008,
         "limit_up_time": "09:34", "limit_up_reason": "存储芯片",
         "continue_day_text": "2连板", "seal_money": 123456789.12},
    ],
    "history": {
        "000001.SH": [[1784275991000 - (19 - i) * 86400000,
                       3600.0 + i * 3.4] for i in range(20)],
    },
    "errors": [],
}


def _demo_indices(payload):
    """演示版的指数/板块卡：补齐 RRG 里那些板块，价格用同一份合成收盘价。"""
    rows = list(payload.get("indices") or [])
    have = {row.get("thscode") for row in rows}
    for row in (payload.get("sector_rrg") or {}).get("rows") or []:
        if row.get("thscode") in have:
            continue
        close = row.get("close")
        rows.append({
            "thscode": row.get("thscode"), "ticker": str(row.get("thscode")).split(".")[0],
            "name": row.get("name"), "last_price": close,
            "price_change_ratio_pct": row.get("today_pct"),
            "turnover": round(close * 1e8) if isinstance(close, (int, float)) else None,
            "is_sector": True,
        })
    return rows


def _previous_payload(out_dir):
    """上一份 site/stocks.json（没有就 None）。清单本身就是「清单 + 数据」，
    所以能直接拿它判断上一份是不是空壳。"""
    path = Path(out_dir) / "stocks.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build(data_path=None, out_dir="site", demo=False, demo_cross=False, view=None):
    """读 stocks_data.json（或演示数据）→ 写四份产物，返回清单。"""
    if demo_cross:
        # 演示「没有板块日 K 时」的版式：同一批板块，但不给 RRG，走单日横截面
        payload = dict(DEMO_PAYLOAD)
        demo_rrg = sector_rrg.demo_section()
        payload["indices"] = _demo_indices({"sector_rrg": demo_rrg,
                                            "indices": payload["indices"]})
        section, _notes = sector_rrg.build_cross_section(
            payload["indices"], market=payload.get("market"),
            indices=payload["indices"], limit_up=payload.get("limit_up"),
            as_of=(datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d"),
            why="演示：这一轮没有板块日 K（短窗 RRG 要 89 根）")
        payload["sector_cross"] = section or {}
        demo = True
    if not demo_cross and demo:
        payload = dict(DEMO_PAYLOAD)
        # 演示数据也走真公式：RRG 段落由 sector_rrg.demo_section() 现算，
        # 板块卡片用同一批板块，版式不会「上面 2 个、下面 14 个」。
        payload["sector_rrg"] = sector_rrg.demo_section()
        payload["indices"] = _demo_indices(payload)
    if demo or demo_cross:
        # 演示数据的时间也用「现在」：段落写的是今天，抬头却停在几个月前，
        # 演示看着像坏掉（真实数据的时间永远来自接口快照）。
        demo_now = int(datetime.now(timezone.utc).timestamp() * 1000)
        payload["data_timestamp"] = demo_now
        if isinstance(payload.get("hk"), dict):
            payload["hk"] = dict(payload["hk"], data_timestamp=demo_now)
    else:
        if not data_path:
            raise ValueError("缺少 --data（或用 --demo 先看版式）")
        payload = json.loads(Path(data_path).read_text(encoding="utf-8"))
    total = (len(payload.get("indices") or [])
             + len(payload.get("stocks") or [])
             + len(((payload.get("hk") or {}).get("rows")) or []))
    if total == 0:
        raise ValueError("行情一条都没有，不生成空面板")
    # 开盘前/接口抽风时会给「空壳快照」（成交量全 0、大多没有最新价）。这种东西
    # 不该盖掉上一份有成交的面板：宁可让设备继续看上一份，也不要把 0.00 手刷上去。
    if not demo and empty_snapshot(payload):
        previous = _previous_payload(out_dir)
        if previous and not empty_snapshot(previous):
            raise ValueError(
                "这一轮快照没有任何成交数据（空壳），不覆盖上一份有效面板"
                "（上一份数据时间 " + (beijing_text(previous.get("data_timestamp")) or "未知")
                + " 北京时间）；等有成交的下一轮再写")
    # A/H 的腾讯/成分股链路是独立子源：一轮临时失败不能把上一份几十个已验证
    # 龙头刷成 91 行“暂无”。覆盖骤降且本轮有错误时沿用上一段，正文明确标缓存。
    previous = _previous_payload(out_dir)
    old_leaders = (previous or {}).get("ah_leaders") or {}
    new_leaders = ah_leader_section(payload)
    old_matched = (old_leaders.get("coverage") or {}).get("matched") or 0
    new_matched = (new_leaders.get("coverage") or {}).get("matched") or 0
    def complete_leaders(section):
        return sum(
            row.get("status") == "ok"
            and (((row.get("a") or {}).get("metrics") or {}).get("bars") or 0) >= 20
            and (((row.get("h") or {}).get("metrics") or {}).get("bars") or 0) >= 20
            for row in (section.get("rows") or []))
    old_complete = complete_leaders(old_leaders)
    new_complete = complete_leaders(new_leaders)
    leader_failed = any("A/H 龙头段未生成" in str(error)
                        for error in payload.get("errors") or [])
    leader_degraded = (
        bool(new_leaders.get("errors")) and (
            (old_matched > 0 and new_matched < old_matched * 0.5)
            or (old_complete > 0 and new_complete < old_complete * 0.5)))
    if old_leaders.get("rows") and (leader_failed or leader_degraded):
        payload["ah_leaders"] = old_leaders
        payload.pop("ah_leader_history", None)  # 保留磁盘上与旧段一致的完整日线
        payload.setdefault("errors", []).append(
            "A/H 龙头本轮子源失败或覆盖骤降，沿用上一份已验证半年量价缓存")

    # 清单里的 generated 与 epub.json 同形（ISO-8601 UTC），只做签名用；
    # 文本面板上显示北京时间文字。
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 周末/节假日出精简版（见 brief_view）；演示数据默认走完整版，
    # 免得周六看演示时以为板块表本来就只有 10 行。
    effective_mode = "full" if (demo or demo_cross) and view is None else view
    brief = brief_view(payload, mode=effective_mode)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = manifest(payload, generated, view=(brief if brief is not None else effective_mode))
    # 完整半年日线单独落盘：Kindle 的主清单只带 26 周压缩趋势，避免每 7 分钟
    # 重拉所有 130 根；网页/研究脚本需要逐日 OHLCV 时再读这个 sidecar。
    if "ah_leader_history" in payload:
        sidecar = {
            "generated": generated, "data_time": data.get("data_time"),
            "window": "约130个交易日，前复权；字段见每家公司 fields",
            "selection": (ah_leader_section(payload).get("selection") or ""),
            "companies": payload.get("ah_leader_history") or {},
        }
        (out / "stocks-leaders.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8")
    (out / "stocks.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "stocks-manifest.json").write_text(
        json.dumps(compact_manifest(data), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (out / "stocks.txt").write_text(
        render_txt(payload, now_beijing_text(), brief=brief), encoding="utf-8")
    (out / "stocks.html").write_text(render_html(), encoding="utf-8")
    write_epub(out / "stocks.epub",
               render_epub_xhtml(payload, now_beijing_text(), brief=brief))
    problems = check(out)
    if problems:
        raise ValueError("产物自检不过：" + "；".join(problems))
    counts = data["counts"]
    print(f"已生成 {out}/stocks.json、stocks-manifest.json、stocks.txt、stocks.html、stocks.epub"
          f"（指数 {counts['indices']} / 板块 {counts['sectors']}"
          f" / 全市场 {counts['market']} / 港股 {counts['hk']}"
          f" / 涨停 {counts['limit_up']}"
          + (f" / A/H龙头 {(data.get('ah_leaders') or {}).get('coverage', {}).get('matched', 0)}"
             if (data.get("ah_leaders") or {}).get("rows") else "")
          + (f" / RRG {counts['rrg']}" if counts.get("rrg") else "")
          + (f" / 横截面 {counts['cross']}" if counts.get("cross") else "")
          + (f" / 周末精简版（{brief['reason']}）" if brief else "")
          + f"，hash {data['data_hash']}）")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description="stocks_data.json → 面板三件套")
    parser.add_argument("--data", default="stocks_data.json",
                        help="fetch_stocks 的中间产物")
    parser.add_argument("--out", default="site", help="产物目录（默认 site）")
    parser.add_argument("--demo", action="store_true",
                        help="用演示数据排版（没 Key 时先看版式）")
    parser.add_argument("--demo-cross", action="store_true",
                        help="演示「没有板块日 K」时的单日横截面版式")
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--brief", dest="view", action="store_const", const="brief",
                      help="强制出周末精简版（板块只留两端、分析只留结论）")
    view.add_argument("--full", dest="view", action="store_const", const="full",
                      help="强制出完整版（节假日想翻全表时用）")
    args = parser.parse_args(argv)
    try:
        build(args.data, args.out, demo=args.demo, demo_cross=args.demo_cross,
              view=args.view)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"排版失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
