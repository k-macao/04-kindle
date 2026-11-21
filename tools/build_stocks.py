#!/usr/bin/env python3
"""把 stocks_data.json 排成四份产物：清单、Kindle 纯文本、网页面板、EPUB 卡片版。

    python tools/build_stocks.py --data stocks_data.json --out site
    python tools/build_stocks.py --demo --out site   # 没 Key 时先看版式

四份产物（都在 ``site/`` 下，跟着仓库走）：

- ``stocks.json`` —— 清单 + 数据二合一：``generated`` 是 Kindle 插件的签名
  （变了才重下正文），``doc`` 指向 ``stocks.txt``，``epub`` 指向 ``stocks.epub``，
  ``data_time`` 是快照的数据时间（不是生成时间），``data_hash`` 供工作流判断
  「行情没变就不提交」；
- ``stocks.txt`` —— Kindle 纯文本面板（电子墨水友好：白底黑字、▲▼ + 正负号，
  不靠颜色传信息）；
- ``stocks.html`` —— 浏览器卡片仪表盘：指数卡 + 自选股卡（带近 20 日迷你走势图）
  + 涨停表，每 ``POLL_SECONDS``（7 分钟）拉一次 ``stocks.json``，``generated`` 变了就重绘；
  拉不到就保留上一次的数据并标 OFFLINE；
- ``stocks.epub`` —— Kindle 字模式卡片版：同一套数据排成表格卡片，交给
  KOReader 阅读器显示（crengine 不认 flex/grid/SVG，所以迷你图只出现在网页面板）。

四份产物最后都多一段【板块轮动 · RRG】：数据由 ``tools/sector_rrg.py`` 算好放进
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

import sector_rrg  # noqa: E402

TITLE = "同花顺行情面板"
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


def cross_section(payload):
    """取出可用的单日横截面段落（RRG 的降级口径）；没有就现算（仍不够则空 dict）。

    旧清单（或只抓了快照、没写 ``sector_cross`` 的文件）里已经有 91 个板块和涨停池，
    重排时直接现算一份，免得面板最后一段空着。
    """
    for key in ("sector_cross", "cross"):
        section = payload.get(key)
        if isinstance(section, dict) and section.get("counts"):
            return section
    if rrg_section(payload):
        return {}
    indices = payload.get("indices") or []
    data_ms = payload.get("data_timestamp")
    as_of = beijing_text(data_ms, "%Y-%m-%d") if isinstance(data_ms, (int, float)) else None
    section, _notes = sector_rrg.build_cross_section(
        indices, market=payload.get("market"), indices=indices,
        limit_up=payload.get("limit_up"), as_of=as_of)
    return section or {}


def final_sections(payload):
    """面板最后一段二选一：能算 RRG 就用 RRG，否则用单日横截面。

    两者**互斥**（同一位置只显示一个）：数据里万一都带着（比如拿旧清单重排），
    这里统一裁决成「RRG 优先」，清单、指纹、自检都走这一个入口，免得一边写
    RRG 一边还留着过期的横截面。
    """
    rrg = rrg_section(payload)
    if rrg:
        return rrg, {}
    return {}, cross_section(payload)


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


def render_rrg_text(section, brief=None):
    """RRG 段落的纯文本版（Kindle 面板最后一段）。

    ``brief``：只留抬头、象限分布与前 ``BRIEF_NARRATIVE_LINES`` 句结论，
    逐象限名单与出处折叠成一行提示。
    """
    rows = section.get("rows") or []
    counts = section.get("counts") or {}
    source = section.get("source") or {}
    demo = "（演示）" if section.get("demo") else ""
    lines = [f"【板块轮动 · RRG】{len(rows)} 个板块 · {section.get('preset')}"
             f" · 基准 板块等权 · 数据 {section.get('as_of')}{demo}"]
    lines.append(f"  象限 领先 {counts.get('leading', 0)} · 改善 {counts.get('improving', 0)}"
                 f" · 滞后 {counts.get('lagging', 0)} · 疲软 {counts.get('weakening', 0)}"
                 " · 依次按轮动顺序（领先→疲软→滞后→改善）")
    narrative = section.get("narrative") or []
    if brief:
        for text in _first_sentences(narrative):
            lines.extend(_wrap_display(text, width=46, indent="  "))
        lines.append(f"  逐象限名单（{len(rows)} 个板块）与信号名单见 stocks.html")
        lines.append(f"  策略 GitHub {source.get('repo')} · 研报口径「仅信息展示，不构成投资建议」")
        return lines
    for text in narrative:
        lines.extend(_wrap_display(text, width=46, indent="  "))
    for quadrant in (1, 2, 4, 3):
        line = rrg_quadrant_line(section, quadrant)
        if line:
            lines.extend(_wrap_display(line, width=46, indent="  "))
    lines.append(f"  策略 GitHub {source.get('repo')} · {source.get('file')}")
    lines.append(f"  研报 {source.get('report')}（仅信息展示，不构成投资建议）")
    return lines


def render_cross_text(section, brief=None):
    """横截面段落的纯文本版（没有日 K 时顶替 RRG 那一段）。

    ``brief``：抬头 + 中位数/离散度 + 前几句结论 + 抗跌/杀跌各 3 个，
    分布条、成交额占比与题材分布都省掉（周末看这些翻不出新东西）。
    """
    counts = section.get("counts") or {}
    stats = section.get("stats") or {}
    market = section.get("market") or {}
    lines = [f"【板块横截面】{counts.get('total', 0)} 个板块 · 单日收盘口径"
             f" · 数据 {section.get('as_of') or '未知'}"]
    head = (f"  红 {counts.get('up', 0)} · 绿 {counts.get('down', 0)}"
            f" · 平 {counts.get('flat', 0)}"
            f" · 中位数 {fmt_ratio(stats.get('median'))}"
            f" · 离散度 {stats.get('std', 0):.2f}")
    # 全 A 的涨跌家数/涨跌停在下面的读数里已经讲了，抬头不重复（Kindle 一屏很贵）
    lines.append(head)

    if brief:
        # 抗跌/杀跌前 5 在上面【板块】段已经列过，题材分布在【涨停池】段也列过；
        # 这里只留「为什么这么走」的两三句结论（优先挑带 `→` 的判断句）。
        raw = list(section.get("narrative") or [])
        verdicts = [item for item in raw if "→" in str(item)] or raw
        for text in _first_sentences(verdicts, limit=3):
            lines.extend(_wrap_display(text, width=46, indent="  "))
        if section.get("why"):
            lines.extend(_wrap_display(f"为什么是横截面：{section['why']}。",
                                       width=46, indent="  "))
        lines.append("  分布条、成交额占比与完整读数见 stocks.html")
        return lines

    bins = [f"{item['label']} {item['count']}" for item in section.get("bins") or []
            if item.get("count")]
    if bins:
        lines.extend(_wrap_display("分布 " + " · ".join(bins), width=46, indent="  "))

    for text in section.get("narrative") or []:
        lines.extend(_wrap_display(text, width=46, indent="  "))

    def name_list(rows, limit=5):
        return " ".join(f"{row.get('name')}{fmt_ratio(row.get('today_pct'))}"
                        for row in rows[:limit])

    winners = section.get("winners") or []
    losers = section.get("losers") or []
    if winners:
        lines.extend(_wrap_display(
            f"{section.get('winners_label') or '领涨'} " + name_list(winners),
            width=46, indent="  "))
    if losers:
        lines.extend(_wrap_display(
            f"{section.get('losers_label') or '领跌'} " + name_list(losers),
            width=46, indent="  "))

    money = section.get("money") or {}
    top = money.get("top") or []
    if top:
        cells = [f"{row['name']} {row['share']:.1f}%({fmt_ratio(row['today_pct'])})"
                 for row in top[:6] if row.get("share") is not None]
        if cells:
            lines.extend(_wrap_display("成交额占比 " + " · ".join(cells),
                                       width=46, indent="  "))

    themes = section.get("themes") or []
    if themes:
        cells = [f"{item['name']} {item['count']}只" for item in themes[:4]
                 if item.get("count")]
        if cells:
            lines.extend(_wrap_display("涨停题材 " + " · ".join(cells),
                                       width=46, indent="  "))
    if section.get("why"):
        lines.extend(_wrap_display(
            "为什么是横截面：" + str(section["why"]).rstrip("。"),
            width=46, indent="  "))
    lines.append("  口径：单日快照的横截面统计，不是 RRG（RRG 要 89 根以上板块日 K）")
    return lines


def data_hash(payload, view=None):
    """行情指纹：价格/涨跌幅/条数/历史收盘变了才变，供工作流跳过无变化的提交。

    ``view``（周末精简版）也算进去：周五收盘到周六零点行情一点没变，但版式要从
    完整版换成精简版——不进指纹的话这一步永远不会被提交，设备上就一直是完整版。
    """
    history = payload.get("history") or {}
    rrg_part, cross_part = final_sections(payload)
    mode = (view.get("mode") if isinstance(view, dict)
            else ("brief" if view == "brief" else "full"))
    core = {
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
        "data_timestamp": payload.get("data_timestamp"),
    }
    raw = json.dumps(core, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def manifest(payload, generated, view=None):
    rrg_part, cross_part = final_sections(payload)
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
        },
        "indices": payload.get("indices") or [],
        "market": payload.get("market"),
        "hk": _hk_manifest(payload),
        "limit_up": payload.get("limit_up") or [],
        # 板块轮动整段进清单（网页要自己画四象限图；Kindle 只用文字与表格）
        "rrg": rrg_part or None,
        # 日 K 不够时的降级段（两者互斥，同一位置只显示一个）
        "cross": cross_part or None,
        "history": payload.get("history") or {},
        "errors": payload.get("errors") or [],
    }


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

    # 最后一段：板块轮动 RRG；日 K 不够时换成单日横截面（见 tools/sector_rrg.py）。
    # 周末精简版只留抬头与前两三句结论——象限清单/分布表在周末翻不出新东西。
    section = rrg_section(payload)
    if section:
        lines.extend(render_rrg_text(section, brief=brief))
        lines.append("")
    else:
        cross = cross_section(payload)
        if cross:
            lines.extend(render_cross_text(cross, brief=brief))
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
.rrg-chart .legend{font-size:12px;color:#333;border-top:1px dashed #999;padding-top:4px;margin-top:4px}
.qcount .cell{background:#fff}
.rrg-read p{margin:5px 0;font-size:13.5px;line-height:1.5}
table.rrg-table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
table.rrg-table th{text-align:left;border-bottom:2px solid #000;padding:4px 6px;font-size:12.5px}
table.rrg-table td{border-bottom:1px solid #ddd;padding:3px 6px;white-space:nowrap}
.qchip{display:inline-block;border:1px solid #000;padding:0 5px;font-size:12px}
.qchip.q1{background:#000;color:#fff}
.qchip.q4{border-style:dashed}
.prev-q{color:#666}
p.src{font-size:11.5px;color:#444;margin-top:8px;line-height:1.5}
.bins{border:2px solid #000;padding:6px 8px;margin:8px 0}
.bins .row{display:flex;align-items:center;gap:8px;font-size:12.5px;line-height:1.9}
.bins .k{width:86px;text-align:right;color:#333}
.bins .b{flex:1;background:#f2f2f2;border:1px solid #999;height:12px}
.bins .b i{display:block;height:100%;background:#000}
.bins .n{width:30px;text-align:right;font-weight:bold}
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
<div class="stale-hint" id="stale-hint">数据超过 __STALE__ 分钟没更新：GitHub Actions 的定时任务被延迟或丢弃了，
面板上的数字可能不是最新的。工作流已是 15 分钟一轮 + 体检补跑（refresh-stocks-watchdog.yml），
仍不更新就看 GitHub Actions 的运行记录，详见 docs/kindle-stocks.md。</div>
<main id="main"></main>
<footer>__DISCLAIMER__ · A 股/指数/板块：同花顺金融数据服务（hithink-finance）· 港股：腾讯公开行情接口 ·
面板每 __POLL_TEXT__ 拉一次 stocks.json，generated 变了才重绘。</footer>
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
function qchip(q,name){
return '<span class="qchip q'+q+'">'+esc(name)+'</span>';}
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
// 记下真实取值范围（图例里写出来，读者才知道这张图被放大到多少倍）
var dataRange=minx.toFixed(1)+'–'+maxx.toFixed(1)+' / '+miny.toFixed(1)+'–'+maxy.toFixed(1);
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
h+='</svg><div class="legend">黑点＝'+esc((g.signal||{}).name||'信号 A')+
'；细线＝最近 '+((rows[0].trail||[]).length||0)+' 个交易日在四象限里的路径；'+
'灰底＝领先象限 · 虚线十字＝中枢 100 · 本图放大范围 '+esc(dataRange)+
'（RS-Ratio / RS-Momentum）。</div></div>';
return h;}
function rrgTable(g){
var h='<table class="data rrg-table"><tr><th>板块</th><th>今日</th><th>RS-Ratio</th>'+
'<th>RS-Mom</th><th>象限</th><th>前一日</th></tr>';
(g.rows||[]).forEach(function(r){
h+='<tr><td>'+esc(r.name||r.thscode)+'</td>'+
'<td class="'+cls(r.today_pct)+'">'+ratio(r.today_pct)+'</td>'+
'<td>'+num1(r.rs_ratio)+'</td><td>'+num1(r.rs_mom)+'</td>'+
'<td>'+qchip(r.quadrant,r.quadrant_name)+'</td>'+
'<td class="prev-q">'+(r.prev_quadrant?
esc(['','领先','改善','滞后','疲软'][r.prev_quadrant]):'--')+'</td></tr>';});
return h+'</table>';}
// 单日横截面（RRG 的降级口径）：没有日 K 时至少把「普涨/普跌、量价、题材」
// 三件事讲清楚，而不是让面板最后一段整块消失。
function crossSection(g,brief){
if(!g||!g.counts)return '';
var NARR=brief?3:999;
var c=g.counts||{},st=g.stats||{},mk=g.market||{},list=[],i;
var maxBin=1;
(g.bins||[]).forEach(function(b){if(b.count>maxBin)maxBin=b.count;});
var h='<section><h2>板块横截面<span class="muted"> '+(c.total||0)+' 个板块 · 单日收盘口径 · 数据 '+
esc(g.as_of||'')+'</span></h2>';
h+='<div class="breadth qcount">';
h+='<div class="cell"><div class="k">红 / 绿</div><div class="v"><span class="up">'+(c.up||0)+
'</span> / <span class="down">'+(c.down||0)+'</span></div><div class="s">板块中位数 '+
ratio(st.median)+'</div></div>';
h+='<div class="cell"><div class="k">离散度</div><div class="v">'+
((typeof st.std==='number')?st.std.toFixed(2):'--')+'</div><div class="s">标准差（越大越像轮动）</div></div>';
h+='<div class="cell"><div class="k">全 A 涨 / 跌</div><div class="v"><span class="up">'+
(mk.up||0)+'</span> / <span class="down">'+(mk.down||0)+'</span></div><div class="s">平均 '+
ratio(mk.avg_ratio)+'</div></div>';
h+='<div class="cell"><div class="k">涨停 / 跌停</div><div class="v"><span class="up">'+
(mk.limit_up||0)+'</span> / <span class="down">'+(mk.limit_down||0)+'</span></div><div class="s">全市场情绪面</div></div>';
h+='</div>';
if((g.bins||[]).length){
h+='<div class="bins">';
(g.bins||[]).forEach(function(b){
var w=maxBin?Math.round(100*b.count/maxBin):0;
h+='<div class="row"><span class="k">'+esc(b.label)+'</span><span class="b">'+
'<i style="width:'+w+'%"></i></span><span class="n">'+b.count+'</span></div>';});
h+='</div>';}
h+='<div class="rrg-read">'+(g.narrative||[]).slice(0,NARR).map(function(t){return '<p>'+esc(t)+'</p>';}).join('')+'</div>';
if(brief){
h+='<p class="src">周末精简版：涨跌分布条、成交额占比与涨停题材见完整版面板（刷新页面即恢复）。</p>';
return h+'</section>';}
function sideBlock(title,rows){
if(!rows||!rows.length)return '';
var out='<div><div class="k">'+esc(title)+'</div>';
rows.forEach(function(r){
out+='<div class="rank"><span>'+esc(r.name)+'</span><b class="'+cls(r.today_pct)+'">'+
ratio(r.today_pct)+'</b></div>';});
return out+'</div>';}
if((g.winners||[]).length||(g.losers||[]).length){
h+='<div class="two">'+sideBlock(g.winners_label||'领涨',g.winners)+
sideBlock(g.losers_label||'领跌',g.losers)+'</div>';}
var money=g.money||{};
if((money.top||[]).length){
h+='<table class="data rrg-table"><tr><th>成交额前列</th><th>占两市成交</th><th>今日</th></tr>';
(money.top||[]).slice(0,8).forEach(function(r){
h+='<tr><td>'+esc(r.name)+'</td><td>'+((typeof r.share==='number')?r.share.toFixed(1)+'%':'--')+
'</td><td class="'+cls(r.today_pct)+'">'+ratio(r.today_pct)+'</td></tr>';});
h+='</table>';}
if((g.themes||[]).length){
h+='<div class="themes">';
(g.themes||[]).forEach(function(t){
h+='<span class="chip">'+esc(t.name)+' <b>'+t.count+'</b>'+
(t.examples&&t.examples.length?' · '+esc(t.examples.join('、')):'')+'</span>';});
h+='</div>';}
h+='<p class="src">口径：单日快照的横截面统计（涨跌分布 / 量价 / 涨停题材），'+
'<b>不是 RRG</b>——RRG 要 89 根以上板块日 K，日 K 够了面板会自动换回四象限。'+
(g.why?'<br>这一轮：'+esc(g.why)+'。':'')+'</p>';
return h+'</section>';}
function rrgSection(g,brief){
var rows=g.rows||[];
if(!rows.length)return '';
var c=g.counts||{},src=g.source||{},names=['领先','改善','滞后','疲软'];
var keys=['leading','improving','lagging','weakening'];
var defs=['RS>100 且 动量>100','RS<100 且 动量>100','RS<100 且 动量<100','RS>100 且 动量<100'];
var h='<section><h2>板块轮动 · RRG<span class="muted"> '+rows.length+' 个板块 · '+
esc(g.preset||'')+' · 数据 '+esc(g.as_of||'')+'</span></h2>';
h+=rrgChart(g);
h+='<div class="breadth qcount">';
names.forEach(function(name,i){
h+='<div class="cell"><div class="k">'+name+'</div><div class="v">'+(c[keys[i]]||0)+
'</div><div class="s">'+defs[i]+'</div></div>';});
h+='</div>';
h+='<div class="rrg-read">'+(g.narrative||[]).slice(0,brief?3:999).map(function(t){
return '<p>'+esc(t)+'</p>';}).join('')+'</div>';
if(!brief)h+=rrgTable(g);
h+='<p class="src">基准 '+esc(g.benchmark||'')+' · '+esc(String((g.params||{}).lookback_ratio||''))+
'/'+esc(String((g.params||{}).lookback_mom||''))+'/'+esc(String((g.params||{}).smooth_window||''))+
' 参数 · '+esc(String(g.samples||0))+' 根日 K<br>策略：GitHub '+esc(src.repo||'')+' · '+
esc(src.file||'')+'（'+esc(src.signal||'')+'）<br>研报：'+esc(src.report||'')+'</p>';
return h+'</section>';}
function render(d){
var h='';
var brief=!!(d.view&&d.view.mode==='brief');
// 一屏监视：市场概览 → 宽基 → 板块 → 港股 → 涨停池 → 板块轮动 RRG（或降级的横截面）
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
// 最后一段：板块轮动 RRG（策略来自 GitHub hugo2046/QuantsPlaybook）；
// 板块日 K 不够时退到单日横截面（两者互斥，只显示一个）
if(d.rrg&&d.rrg.rows&&d.rrg.rows.length)h+=rrgSection(d.rrg,brief);
else if(d.cross&&d.cross.counts)h+=crossSection(d.cross,brief);
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
h2{font-size:1.1em;border-bottom:1px solid #000;padding-bottom:2px;margin-top:1.2em}
.meta{font-size:0.85em}
table.card{border:1px solid #000;width:100%;border-collapse:collapse;margin:8px 0}
table.card td{padding:3px 6px;vertical-align:top}
table.card .big{font-size:1.3em;font-weight:bold}
table.data{border-collapse:collapse;width:100%}
table.data th,table.data td{border:1px solid #999;padding:3px 5px;font-size:0.9em}
.up{font-weight:bold}.down{font-weight:bold}
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
        parts.append('<p class="meta"><b>'
                     f'{esc(brief_banner(brief, len(sector_indices(payload))))}</b></p>')
    if not today_has_values(payload):
        parts.append('<p class="meta">注：这一版快照没有成交数据（开盘前/接口给空壳），'
                     '下面显示 -- 与「未成交」，下一轮再看。</p>')

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
        source = section.get("source") or {}
        demo_tag = "（演示）" if section.get("demo") else ""
        parts.append(f"<h2>板块轮动 · RRG（{len(section['rows'])}）{esc(demo_tag)}</h2>")
        parts.append(f'<p class="meta">{esc(section.get("preset") or "")} · 基准 '
                     f'{esc(section.get("benchmark") or "")} · 数据 '
                     f'{esc(str(section.get("as_of") or ""))} · 象限 领先 '
                     f'{counts.get("leading", 0)} · 改善 {counts.get("improving", 0)}'
                     f' · 滞后 {counts.get("lagging", 0)} · 疲软 '
                     f'{counts.get("weakening", 0)}</p>')
        narrative = section.get("narrative") or []
        for text in (_first_sentences(narrative) if brief else narrative):
            parts.append(f"<p>{esc(text)}</p>")
        if brief:
            parts.append(f'<p class="meta">逐板块的 RS-Ratio / RS-Mom 与信号名单见网页面板 '
                         f"stocks.html（{len(section['rows'])} 个板块）</p>")
        else:
            parts.append('<table class="data"><tr><th>板块</th><th>今日</th>'
                         "<th>RS-Ratio</th><th>RS-Mom</th><th>象限</th></tr>")
            for row in section["rows"]:
                parts.append(
                    f"<tr><td>{esc(row.get('name') or row.get('thscode'))}</td>"
                    f"<td>{esc(fmt_ratio(row.get('today_pct')))}</td>"
                    f"<td>{esc(fmt_one(row.get('rs_ratio')))}</td>"
                    f"<td>{esc(fmt_one(row.get('rs_mom')))}</td>"
                    f"<td>{esc(row.get('quadrant_name') or '')}</td></tr>")
            parts.append("</table>")
        parts.append(f'<p class="meta">策略：GitHub {esc(source.get("repo") or "")} · '
                     f'{esc(source.get("file") or "")}<br>研报 {esc(source.get("report") or "")}</p>')
    else:
        cross = cross_section(payload)
        if cross:
            counts = cross.get("counts") or {}
            stats = cross.get("stats") or {}
            parts.append(f"<h2>板块横截面（{counts.get('total', 0)}）</h2>")
            parts.append(f'<p class="meta">单日收盘口径 · 数据 {esc(str(cross.get("as_of") or ""))}'
                         f' · 红 {counts.get("up", 0)} / 绿 {counts.get("down", 0)}'
                         f' · 中位数 {esc(fmt_ratio(stats.get("median")))}</p>')
            narrative = cross.get("narrative") or []
            for text in (_first_sentences(narrative) if brief else narrative):
                parts.append(f"<p>{esc(text)}</p>")
            winners = cross.get("winners") or []
            losers = cross.get("losers") or []
            if brief:
                winners, losers = winners[:3], losers[:3]
            if winners or losers:
                parts.append('<table class="data"><tr><th>' +
                             esc(cross.get("winners_label") or "领涨") +
                             "</th><th>涨跌幅</th><th>" +
                             esc(cross.get("losers_label") or "领跌") +
                             "</th><th>涨跌幅</th></tr>")
                for index in range(max(len(winners), len(losers))):
                    left = winners[index] if index < len(winners) else {}
                    right = losers[index] if index < len(losers) else {}
                    parts.append(
                        f"<tr><td>{esc(left.get('name') or '')}</td>"
                        f"<td>{esc(fmt_ratio(left.get('today_pct')))}</td>"
                        f"<td>{esc(right.get('name') or '')}</td>"
                        f"<td>{esc(fmt_ratio(right.get('today_pct')))}</td></tr>")
                parts.append("</table>")
            if cross.get("why"):
                parts.append(f'<p class="meta">为什么是横截面：{esc(str(cross["why"]))}</p>')

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
    for key in ("generated", "doc", "title", "data_hash", "counts"):
        if not data.get(key):
            problems.append(f"stocks.json 缺少 {key}")
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
    if cross.get("counts"):
        page = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
        if "板块横截面" not in page or "function crossSection" not in page:
            problems.append("清单里有 cross，但 stocks.html 没有横截面段")
        if txt_path.is_file() and "【板块横截面】" not in txt_path.read_text(encoding="utf-8"):
            problems.append("清单里有 cross，但 stocks.txt 没有横截面段")
    if cross.get("counts") and (data.get("rrg") or {}).get("rows"):
        # 两者互斥：面板最后一段只显示一个，清单里不该同时有两段
        problems.append("清单里 rrg 与 cross 同时存在")
    rrg = data.get("rrg") or {}
    if rrg.get("rows"):
        if not rrg.get("narrative"):
            problems.append("stocks.json 的 rrg 没有叙述文字")
        page = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
        if "板块轮动 · RRG" not in page:
            problems.append("清单里有 rrg，但 stocks.html 没有板块轮动段")
        if txt_path.is_file() and "板块轮动 · RRG" not in txt_path.read_text(encoding="utf-8"):
            problems.append("清单里有 rrg，但 stocks.txt 没有板块轮动段")
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
    # 清单里的 generated 与 epub.json 同形（ISO-8601 UTC），只做签名用；
    # 文本面板上显示北京时间文字。
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 周末/节假日出精简版（见 brief_view）；演示数据默认走完整版，
    # 免得周六看演示时以为板块表本来就只有 10 行。
    brief = brief_view(payload, mode=("full" if (demo or demo_cross) and view is None else view))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = manifest(payload, generated, view=brief)
    (out / "stocks.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "stocks.txt").write_text(
        render_txt(payload, now_beijing_text(), brief=brief), encoding="utf-8")
    (out / "stocks.html").write_text(render_html(), encoding="utf-8")
    write_epub(out / "stocks.epub",
               render_epub_xhtml(payload, now_beijing_text(), brief=brief))
    problems = check(out)
    if problems:
        raise ValueError("产物自检不过：" + "；".join(problems))
    counts = data["counts"]
    print(f"已生成 {out}/stocks.json、stocks.txt、stocks.html、stocks.epub"
          f"（指数 {counts['indices']} / 板块 {counts['sectors']}"
          f" / 全市场 {counts['market']} / 港股 {counts['hk']}"
          f" / 涨停 {counts['limit_up']}"
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
