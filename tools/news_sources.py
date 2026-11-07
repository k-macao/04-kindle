#!/usr/bin/env python3
"""每日新闻订阅源的目录：走 RSSHub 路由的财经新闻源。

为什么自己建目录，而不是只读 004 页面上的 05 节：

- 004 页面每小时才刷一次，且 05 节只渲染「本次抓到的条目」，不标注走的是原生 RSS
  还是 RSSHub 路由；这里要的是**每天一份、只含新闻**的独立文档。
- RSSHub 是第三方公共服务，路由表由官方仓库 (DIYgod/RSSHub) 维护。下面每条路由的
  `path` 都按官方 `lib/routes/<namespace>/…` 里的 `route.path` 核对过；`route` 是
  拼上 namespace 后的完整路径（例如 `lib/routes/cls/telegraph.tsx` 的
  `path: '/telegraph/:category?'` → `/cls/telegraph`）。
- 想加源：优先选「站点没有原生 RSS、或原生 RSS 常被墙」的站点，把完整路由写进
  SOURCES，跑 `python tools/fetch_news.py --only KEY` 验一条；抓不到就说明路由或
  站点变了，换实例或换路由，别留一条永远失败的源。

字段说明：

    key     稳定的短标识（提交信息、日志、--only 都用它）
    name    设备上显示的名字
    group   GROUPS 里的分组键
    route   RSSHub 完整路由（以 / 开头，不含实例域名）
    lang    zh / en，仅用于排序与文档说明
    note    这条源是什么、为什么值得每天看
"""

# 分组顺序＝EPUB 目录顺序；每组一节。
# 标签不带序号：序号由 build_news.py 按「这天真的有稿的组」依次排，
# 免得某组空了以后目录里出现「一、… 三、…」的跳号。
GROUPS = [
    ("flash", "中文财经快讯"),
    ("depth", "中文财经深度"),
    ("global", "全球市场与加密"),
]
GROUP_LABELS = {key: label for key, label in GROUPS}

SOURCES = [
    # ---------------- 一、中文财经快讯 ----------------
    {
        "key": "CLS",
        "name": "财联社 · 电报",
        "group": "flash",
        "lang": "zh",
        "route": "/cls/telegraph",
        "note": "财联社电报（RSSHub lib/routes/cls/telegraph.tsx，path /telegraph/:category?），A 股盘中最重要的快讯源。",
    },
    {
        "key": "WSCN_LIVE",
        "name": "华尔街见闻 · 要闻快讯",
        "group": "flash",
        "lang": "zh",
        "route": "/wallstreetcn/live/global",
        "note": "wallstreetcn/live.tsx 的 path /live/:category?，category=global 是「要闻」频道。",
    },
    {
        "key": "JIN10",
        "name": "金十数据 · 快讯",
        "group": "flash",
        "lang": "zh",
        "route": "/jin10",
        "note": "jin10/index.ts 的 path /:important?，不带参数＝全部快讯；带 1 只要重要快讯。",
    },
    {
        "key": "STCN_KX",
        "name": "证券时报 · 快讯",
        "group": "flash",
        "lang": "zh",
        "route": "/stcn/article/list/kx",
        "note": "stcn/kx.ts 的 path /article/list/kx，证券时报快讯（A 股公告与监管动向）。",
    },
    {
        "key": "THS_NEWS",
        "name": "同花顺 · 实时财经新闻",
        "group": "flash",
        "lang": "zh",
        "route": "/10jqka/realtimenews",
        "note": "10jqka/realtimenews.ts 的 path /realtimenews/:tag?，同花顺 7×24 快讯。",
    },
    {
        "key": "YICAI_BRIEF",
        "name": "第一财经 · 简报",
        "group": "flash",
        "lang": "zh",
        "route": "/yicai/brief",
        "note": "yicai/brief.ts 的 path /brief，第一财经每日简报式短稿。",
    },
    {
        "key": "SINA_ROLL",
        "name": "新浪 · 股市滚动",
        "group": "flash",
        "lang": "zh",
        "route": "/sina/rollnews/2517",
        "note": "sina/rollnews.ts 的 path /rollnews/:lid?，lid=2517 是「股市」频道（表见路由说明）。",
    },
    # ---------------- 二、中文财经深度 ----------------
    {
        "key": "WSCN_NEWS",
        "name": "华尔街见闻 · 要闻文章",
        "group": "depth",
        "lang": "zh",
        "route": "/wallstreetcn/news/global",
        "note": "wallstreetcn/news.ts 的 path /news/:category?，比快讯长的成稿，适合夜读。",
    },
    {
        "key": "CAIXIN",
        "name": "财新 · 最新",
        "group": "depth",
        "lang": "zh",
        "route": "/caixin/latest",
        "note": "caixin/latest.ts 的 path /latest，财新网最新稿件（部分付费墙，只给摘要）。",
    },
    {
        "key": "GELONGHUI",
        "name": "格隆汇 · 推荐",
        "group": "depth",
        "lang": "zh",
        "route": "/gelonghui/home",
        "note": "gelonghui/home.ts 的 path /home/:tag?，默认 tag=web_home_page＝推荐。",
    },
    {
        "key": "YICAI_LATEST",
        "name": "第一财经 · 最新",
        "group": "depth",
        "lang": "zh",
        "route": "/yicai/latest",
        "note": "yicai/latest.ts 的 path /latest，第一财经最新成稿。",
    },
    {
        "key": "HK01",
        "name": "香港01 · 即时",
        "group": "depth",
        "lang": "zh",
        "route": "/hk01/latest",
        "note": "hk01/latest.ts 的 path /latest，港股与香港政经的即时报道。",
    },
    {
        "key": "CAIJING21",
        "name": "21 财经 · 热点",
        "group": "depth",
        "lang": "zh",
        "route": "/21caijing/channel/热点",
        "note": "21caijing/channel.ts 的 path /channel/:name{.+}?，name 是中文频道名（抓取时按 URL 编码）。",
    },
    # ---------------- 三、全球市场与加密 ----------------
    {
        "key": "REUTERS",
        "name": "路透社 · 商业财经",
        "group": "global",
        "lang": "en",
        "route": "/reuters/business",
        "note": "reuters/common.tsx 的 path /:category/:topic?，category=business。",
    },
    {
        "key": "BLOOMBERG",
        "name": "彭博社 · 市场",
        "group": "global",
        "lang": "en",
        "route": "/bloomberg/markets",
        "note": "bloomberg/index.ts 的 path /:site?，site=markets（站点表见路由说明）。",
    },
    {
        "key": "WSJ_ECON",
        "name": "WSJ · 经济",
        "group": "global",
        "lang": "en",
        "route": "/wsj/en-us/economy",
        "note": "wsj/news.ts 的 path /:lang/:category?，lang=en-us、category=economy。",
    },
    {
        "key": "COINDESK",
        "name": "CoinDesk · 最新",
        "group": "global",
        "lang": "en",
        "route": "/coindesk/news",
        "note": "coindesk/news.ts 的 path /news，加密市场的基准新闻源。",
    },
]

SOURCE_KEYS = {source["key"] for source in SOURCES}


def catalog_errors():
    """目录自检：键唯一、分组存在、路由像条完整路径。返回问题列表（空即通过）。"""
    problems = []
    seen = set()
    for source in SOURCES:
        key = source.get("key")
        if not key or key in seen:
            problems.append(f"源 key 重复或缺失：{key!r}")
        seen.add(key)
        if source.get("group") not in GROUP_LABELS:
            problems.append(f"{key}: 分组 {source.get('group')!r} 不在 GROUPS 里")
        route = source.get("route") or ""
        if not route.startswith("/") or " " in route or "//" in route:
            problems.append(f"{key}: 路由不像条完整路径：{route!r}")
        if not source.get("name"):
            problems.append(f"{key}: 缺少显示名")
    return problems


def group_label(key):
    return GROUP_LABELS.get(key, key)


def sources_in(group=None):
    """按目录顺序取源；给了 group 就只取那一组。"""
    return [source for source in SOURCES if group is None or source["group"] == group]
