#!/usr/bin/env python3
"""每日新闻订阅源的目录：走 RSSHub 路由的财经新闻源。

为什么自己建目录，而不是只读 004 页面上的 05 节：

- 004 页面每小时才刷一次，且 05 节只渲染「本次抓到的条目」，不标注走的是原生 RSS
  还是 RSSHub 路由；这里要的是**每天一份、只含新闻**的独立文档。
- RSSHub 是第三方公共服务，路由表由官方仓库 (DIYgod/RSSHub) 维护。下面每条路由的
  `path` 都按官方 `lib/routes/<namespace>/…` 里的 `route.path` 核对过；`route` 是
  拼上 namespace 后的完整路径（例如 `lib/routes/cls/telegraph.tsx` 的
  `path: '/telegraph/:category?'` → `/cls/telegraph`）。
- **namespace 是目录名，不是 `namespace.ts` 里的 `name`**：注册代码按顶层目录拼路径
  （`lib/registry-dev.ts` 的 `/${name}${key}`），`name` 只是文档上的显示名。所以
  21 财经是 `/21caijing/channel/热点`（目录），不是 `/21财经/…`（显示名）。
- 想加源：优先选「站点没有原生 RSS、或原生 RSS 常被墙」的站点，把完整路由写进
  SOURCES，跑 `python tools/fetch_news.py --only KEY` 验一条；抓不到就说明路由或
  站点变了，换实例或换路由，别留一条永远失败的源。
- **加源前先看路由的 `features`**：`requirePuppeteer: true`（如 `/xueqiu/hots`）在公共实例上
  必然失败，`requireConfig: true`（要 cookie/密钥）也一样；`antiCrawler: true`（如 WSJ）
  在机房 IP 上基本取不到。这三种都别写进来，除非你自建实例并且配好了对应依赖。

字段说明：

    key     稳定的短标识（提交信息、日志、--only 都用它）
    name    设备上显示的名字
    group   GROUPS 里的分组键
    route   RSSHub 完整路由（以 / 开头，不含实例域名）——与 feed 二选一
    feed    站点自己的原生 RSS 绝对地址（RSSHub 没有这条路由时才用）——与 route 二选一
    site    站点域名，只用于「这条源挂了，去问 Radar 该换哪个路由」（tools/radar_discover.py）
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
        "site": "cls.cn",
        "note": "财联社电报（RSSHub lib/routes/cls/telegraph.tsx，path /telegraph/:category?），A 股盘中最重要的快讯源。",
    },
    {
        "key": "WSCN_LIVE",
        "name": "华尔街见闻 · 要闻快讯",
        "group": "flash",
        "lang": "zh",
        "route": "/wallstreetcn/live/global",
        "site": "wallstreetcn.com",
        "note": "wallstreetcn/live.tsx 的 path /live/:category?，category=global 是「要闻」频道。",
    },
    {
        "key": "JIN10",
        "name": "金十数据 · 快讯",
        "group": "flash",
        "lang": "zh",
        "route": "/jin10",
        "site": "jin10.com",
        "note": "jin10/index.ts 的 path /:important?，不带参数＝全部快讯；带 1 只要重要快讯。",
    },
    {
        "key": "WSCN_A",
        "name": "华尔街见闻 · A 股快讯",
        "group": "flash",
        "lang": "zh",
        "route": "/wallstreetcn/live/a-stock",
        "site": "wallstreetcn.com",
        "note": "wallstreetcn/live.tsx 的 category=a-stock。原来这里是证券时报快讯（/stcn/article/list/kx），"
                "实测返回 0 条，换成同一命名空间里A股频道——同域名其它路由都正常。",
    },
    {
        "key": "THS_NEWS",
        "name": "同花顺 · 实时财经新闻",
        "group": "flash",
        "lang": "zh",
        "route": "/10jqka/realtimenews",
        "site": "10jqka.com.cn",
        "note": "10jqka/realtimenews.ts 的 path /realtimenews/:tag?，同花顺 7×24 快讯。",
    },
    {
        "key": "YICAI_BRIEF",
        "name": "第一财经 · 简报",
        "group": "flash",
        "lang": "zh",
        "route": "/yicai/brief",
        "site": "yicai.com",
        "note": "yicai/brief.ts 的 path /brief，第一财经每日简报式短稿。",
    },
    {
        "key": "SINA_ROLL",
        "name": "新浪 · 股市滚动",
        "group": "flash",
        "lang": "zh",
        "route": "/sina/rollnews/2517",
        "site": "sina.com.cn",
        "note": "sina/rollnews.ts 的 path /rollnews/:lid?，lid=2517 是「股市」频道（表见路由说明）。",
    },
    # ---------------- 二、中文财经深度 ----------------
    {
        "key": "WSCN_NEWS",
        "name": "华尔街见闻 · 要闻文章",
        "group": "depth",
        "lang": "zh",
        "route": "/wallstreetcn/news/global",
        "site": "wallstreetcn.com",
        "note": "wallstreetcn/news.ts 的 path /news/:category?，比快讯长的成稿，适合夜读。",
    },
    {
        "key": "CAIXIN",
        "name": "财新 · 最新",
        "group": "depth",
        "lang": "zh",
        "route": "/caixin/latest",
        "site": "caixin.com",
        "note": "caixin/latest.ts 的 path /latest，财新网最新稿件（部分付费墙，只给摘要）。",
    },
    {
        "key": "GELONGHUI",
        "name": "格隆汇 · 推荐",
        "group": "depth",
        "lang": "zh",
        "route": "/gelonghui/home",
        "site": "gelonghui.com",
        "note": "gelonghui/home.ts 的 path /home/:tag?，默认 tag=web_home_page＝推荐。",
    },
    {
        "key": "GELONGHUI_HOT",
        "name": "格隆汇 · 日热文",
        "group": "depth",
        "lang": "zh",
        "route": "/gelonghui/hot-article",
        "site": "gelonghui.com",
        "note": "gelonghui/hot-article.ts 的 path /hot-article/:type?，默认 day。原来这里是路透商业"
                "（/reuters/business），Cloudflare 挡机房 IP，换成同域名已验证的中文源。",
    },
    {
        "key": "YICAI_LATEST",
        "name": "第一财经 · 最新",
        "group": "depth",
        "lang": "zh",
        "route": "/yicai/latest",
        "site": "yicai.com",
        "note": "yicai/latest.ts 的 path /latest，第一财经最新成稿。",
    },
    {
        "key": "YICAI_HEAD",
        "name": "第一财经 · 头条",
        "group": "depth",
        "lang": "zh",
        "route": "/yicai/headline",
        "site": "yicai.com",
        "note": "yicai/headline.ts 的 path /headline。原来这里是 WSJ 经济（/wsj/en-us/economy），"
                "该路由标着 antiCrawler、机房 IP 必挂，换成同域名已验证可用的中文源。",
    },
    {
        "key": "HK01",
        "name": "香港01 · 即时",
        "group": "depth",
        "lang": "zh",
        "route": "/hk01/latest",
        "site": "hk01.com",
        "note": "hk01/latest.ts 的 path /latest，港股与香港政经的即时报道。",
    },
    {
        "key": "WSCN_HK",
        "name": "华尔街见闻 · 港股快讯",
        "group": "flash",
        "lang": "zh",
        "route": "/wallstreetcn/live/hk-stock",
        "site": "wallstreetcn.com",
        "note": "wallstreetcn/live.tsx 的 category=hk-stock，港股盘中快讯。原来这里是雪球热帖"
                "（/xueqiu/hots），那条路由标着 requirePuppeteer: true——公共实例没有 Playwright，"
                "必然取不到。",
    },
    # ---------------- 三、全球市场与加密 ----------------
    {
        "key": "BLOOMBERG_CRYPTO",
        "name": "彭博社 · 加密",
        "group": "global",
        "lang": "en",
        "route": "/bloomberg/crypto",
        "site": "bloomberg.com",
        "note": "bloomberg/index.ts 的 site=crypto。原来的路透商业在这里返回 0 条，而彭博这条线上"
                "已经证明可用（markets 每天满额）。",
    },
    {
        "key": "BLOOMBERG",
        "name": "彭博社 · 市场",
        "group": "global",
        "lang": "en",
        "route": "/bloomberg/markets",
        "site": "bloomberg.com",
        "note": "bloomberg/index.ts 的 path /:site?，site=markets（站点表见路由说明）。",
    },
    {
        "key": "COINDESK",
        "name": "CoinDesk · 最新",
        "group": "global",
        "lang": "en",
        "route": "/coindesk/news",
        "site": "coindesk.com",
        "note": "coindesk/news.ts 的 path /news。2026-11-07 首轮返回 0 条（上游多半拦机房 IP），"
                "先留着观察：清单里的 sources_failed 会记录它是否持续失败。",
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
        feed = source.get("feed") or ""
        if bool(route) == bool(feed):
            problems.append(f"{key}: route（RSSHub 路由）与 feed（原生 RSS）必须二选一")
        if route and (not route.startswith("/") or " " in route or "//" in route):
            problems.append(f"{key}: 路由不像条完整路径：{route!r}")
        if feed and not feed.startswith("https://"):
            problems.append(f"{key}: 原生 feed 必须是 HTTPS 绝对地址：{feed!r}")
        if not source.get("name"):
            problems.append(f"{key}: 缺少显示名")
    return problems


def group_label(key):
    return GROUP_LABELS.get(key, key)


def sources_in(group=None):
    """按目录顺序取源；给了 group 就只取那一组。"""
    return [source for source in SOURCES if group is None or source["group"] == group]
