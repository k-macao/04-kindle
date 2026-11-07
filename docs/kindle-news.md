# 每日新闻聚合 RSS（RSSHub）

每天 **07:00（北京时间）** 抓一轮 RSSHub（财经 / 政治 / 哲学思想），英文条目自动配中文，
合成**一个标准 RSS**：`site/news.xml`。任何阅读器（KOReader 的新闻下载器、NetNewsWire、
Feedly、Reeder…）直接订阅，不需要插件、不需要清单：

```
https://raw.githubusercontent.com/k-macao/04-kindle/main/site/news.xml      ← 先试这条
https://github.com/k-macao/04-kindle/releases/download/news-feed/news.xml   ← 再试这条（Release 资产）
https://k-macao.github.io/04-kindle/site/news.xml                           ← GitHub Pages
https://cdn.jsdelivr.net/gh/k-macao/04-kindle@main/site/news.xml            ← 被墙时用 jsDelivr
```

四条挂在不同主机家族、不同 CA 上，**设备上一条不通就换下一条**（顺序与理由见「2.1 多入口」）。

| 文件 | 内容 | 谁用 |
| --- | --- | --- |
| `site/news.xml` | **聚合 RSS**：所有源合成一个标准 feed，英文条目中英并列 | 阅读器订阅、KOReader 新闻下载器 |
| `site/news_seen.json` | 最近 7 天推过的标题指纹 | 抓取时跨天去重，不进 feed |
| `site/news_zh_cache.json` | 英文标题/摘要的中文译文缓存 | 重译同一句英文时直接命中，省额度、译文稳定 |

> 工作流**只生成 xml**。日报 EPUB / 清单 / 纯文本（`site/news.epub`、`site/news.json`、
> `site/news.txt`）不再自动产出，仓库里也不再保留——`tools/build_news.py` 还在，需要那本书时
> 手动跑（见下面「做那本 EPUB」）。

抓的是 [RSSHub](https://github.com/DIYgod/RSSHub) 的公开路由，**不重排 004 的报告页**：报告页每小时才刷，
而且只渲染当次抓到的条目，拿不到「这条新闻来自哪个源、原文在哪」。

## 1. 三条命令

```sh
python tools/fetch_news.py --out news_data.json               # 抓一轮，写到 news_data.json（不进 Git）
python tools/translate_news.py --data news_data.json \
       --cache site/news_zh_cache.json                        # 英文条目补中文（就地写回）
python tools/build_feed.py --data news_data.json --out site   # 聚合 RSS：site/news.xml
```

做那本 EPUB（可选，工作流不再做）：

```sh
python tools/build_news.py --data news_data.json --out site   # site/news.epub + news.json + news.txt
```

实例顺序＝优先级：**官方 `rsshub.app` 第一**，后面 `rsshub.rss.tips` / `rsshub.isrss.com` /
`rsshub.cups.moe` 兜底。抓取脚本开工前先探一次 `/healthz`：官方实例对机房 IP 回 403 时
（004 的 CI 记过这事）把它挪到队尾当兜底，日志写「探活失败（HTTP 403），排到队尾」。
所以**先接官方**和**官方挂了也能出报**两件事同时成立；`RSSHUB_BASE`（逗号分隔）可覆盖，
自建实例放最前面。

`news_data.json` 是中间产物：抓取和排版分开，排版可以离线反复调。`news_data.json` 里每条是
`{title, link, published, summary, source, group, lang}`（英译中之后多 `title_zh` / `summary_zh`），
`sources` 里记着每个源这次用的实例、抓到几条、失败原因，`instances_used` 记着哪个实例供了多少条
——出问题先看这里。

## 1.1 英文条目自动附中文

`translate_news.py` 是唯一另需联网的一步，规矩是：

- **只翻英文**：源标了 `lang: en`，或者标题里一个汉字都没有、字母占一半以上；
- **三家免费接口按顺序回退**：Google `translate_a/single` → Lingva 镜像 → MyMemory，
  回的字符串里没有汉字就算失败、换下一家；
- **结果落 `site/news_zh_cache.json`**：键是文本指纹，同一句英文只翻一次；条目超过 6000 条
  或放置超过 120 天自动清掉；
- **翻译接口全挂也照样出 feed**：这一步在工作流里 `continue-on-error: true`，feed 自检会打出
  「还有几条英文标题没配上中文」，下一轮补齐；
- 渲染时中英并列：RSS 标题写成 `English Title｜中文：译文`，摘要是「中文：… 原文：…」——
  阅读器的列表页只显示标题，译文不接在标题上等于没翻；EPUB（手动生成时）标题下多一行「中文：…」。

抓取时的三个时间约定：

- **单源时间窗**：源可以写 `max_age_hours`（政策、哲学类默认放宽到 3～14 天）；
- **未来时间**：超过 6 小时的时间戳一律按「生成时间」记（财新周刊的期号日期跑到 25 小时后，
  否则它会一直霸占「最新一条」、把排序带歪）；
- **跨天去重**：`news_seen.json` 记着最近 7 天推过的标题指纹。

`build_news.py`（可选，手动跑那本书时）的规矩：

- 一条新闻 = 标题 + `来源 · 北京时间` + 最多一句摘要。摘要是 RSS 的 `description` / `summary`
  去掉标签后的文字，和前一句相同就不重复出；
- 按目录分组顺序分节，**空组跳过、序号顺排**（所以某天没深度稿时不会出现「一、… 三、…」）；
- 少于 `--min-items`（默认 10）条、或者正文乱码占比超过 2% → 退出码 1，**不覆盖旧产物**：
  宁可设备上还是昨天的日报，也不推一份空书。

### 2.1 多入口：设备说「内容下载失败」时

KOReader 的新聞下載器只给一句「內容下載失敗」，它分不清是地址错了、文件坏了，还是这台设备
那条线路不通。所以同一份 feed 挂在几个互不相关的主机上，按下面顺序试：

| 顺序 | 入口 | 地址 | 说明 |
| --- | --- | --- | --- |
| 1 | raw | `https://raw.githubusercontent.com/k-macao/04-kindle/main/site/news.xml` | 立即生效；证书是老牌子（DigiCert），旧 Kindle 的 CA 库也认 |
| 2 | Release 资产 | `https://github.com/k-macao/04-kindle/releases/download/news-feed/news.xml` | 每轮自动重传，走 github.com；同样老证书，稳 |
| 3 | GitHub Pages | `https://k-macao.github.io/04-kindle/site/news.xml` | 推送后约 30–60 秒生效；证书是 Let's Encrypt，个别旧设备不认 |
| 4 | jsDelivr | `https://cdn.jsdelivr.net/gh/k-macao/04-kindle@main/site/news.xml` | CDN，`@main` 有十来分钟缓存；线路独立，常能绕开墙 |
| 5 | jsDelivr 备用端点 | `fastly.` / `gcore.` / `testingcf.jsdelivr.net`（路径同上） | 某个 CDN 节点不通时换 |

> 顺序是有讲究的：旧 Kindle（比如 Oasis 1）的 CA 证书库可能认不出 Let's Encrypt，
> 这种设备上 `github.io` 会报「內容下載失敗」，而 raw / Release 的证书通常没问题。
> 反过来，如果线路被墙，jsDelivr 更容易通。两条路都堵住的情况基本没有。

每轮跑完，工作流会把这几条**逐个取一遍**，结果写在 `site/feed-mirrors.json`
（`ok` / `total` / 每条的状态码、字节数、条目数、报错）。设备报错时先看这份表：

- 某条 `ok: false` + `404` → 地址真的没了（Pages 没开、Release 资产没上传），换别的入口；
- 全都 `ok: true`，设备还是失败 → 是**这台设备的网络/TLS**，不是 feed 的问题：换个入口
  （jsDelivr 常能绕开），或者在 KOReader 里用浏览器打开那个地址看报什么错；
- `status: 200` 但 `items: 0` / `不是 feed` → 那层回的是 404 页面（Pages 未构建完时常见），
  过一分钟再试。

### 聚合 RSS（`site/news.xml`）

`build_feed.py` 把 `news_data.json` 合成一份**标准 RSS 2.0**——不装插件、不用清单，任何阅读器都能订：

```
https://k-macao.github.io/04-kindle/site/news.xml
https://raw.githubusercontent.com/k-macao/04-kindle/main/site/news.xml
https://github.com/k-macao/04-kindle/releases/download/news-feed/news.xml
https://cdn.jsdelivr.net/gh/k-macao/04-kindle@main/site/news.xml
```

（仓库如果以后改了 Pages 域名，同一份文件也在 `site/news.xml`；各入口的区别见「2.1 多入口」。）

- 一个 `<item>`＝标题、原文链接、`pubDate`（RFC822/UTC）、分类、`<source url="站点">来源名</source>`、
  一句摘要；**英文条目标题后面接中文**（`English Title｜中文：译文`），摘要为「中文：… 原文：…」；
- `guid` 用原文链接（`isPermaLink="true"`），没有链接的用标题指纹 `urn:004:news:<hash>`——
  同一条新闻在读者端不会因为每天重发而重复；
- `lastBuildDate` 是本次生成时间，`ttl` = 60 分钟；
- 正文先删掉 XML 1.0 不允许的控制字符再转义，`&`、`<`、引号都不会破坏文件；
  生成后立刻用解析器自检，解析不过就不发布；
- 条目少于 `--min-items`（默认 10）时退出码 1，不覆盖上一份。

订阅方式：KOReader 的 **新闻下载器**（News downloader）里添加上面的地址；或者把它丢给
NetNewsWire / Feedly / Reeder / Inoreader 之类的阅读器。设备上如果 `raw.githubusercontent.com`
连不上，用 jsDelivr 那条，或者把仓库 Pages 打开后换成 gh.io 的地址。

## 2. 源目录

源写在 `tools/news_sources.py`，24 条，五个分组：

| 分组 | 源 | RSSHub 路由 |
| --- | --- | --- |
| 快讯 | 财联社·电报 | `/cls/telegraph` |
| | 华尔街见闻·要闻快讯 | `/wallstreetcn/live/global` |
| | 华尔街见闻·A 股快讯 | `/wallstreetcn/live/a-stock` |
| | 华尔街见闻·港股快讯 | `/wallstreetcn/live/hk-stock` |
| | 金十数据 | `/jin10` |
| | 同花顺·实时财经新闻 | `/10jqka/realtimenews` |
| | 第一财经·简报 | `/yicai/brief` |
| | 新浪·股市滚动 | `/sina/rollnews/2517` |
| 深度 | 华尔街见闻·要闻文章 | `/wallstreetcn/news/global` |
| | 财新·最新 | `/caixin/latest` |
| | 格隆汇·推荐 | `/gelonghui/home` |
| | 格隆汇·日热文 | `/gelonghui/hot-article` |
| | 第一财经·最新 | `/yicai/latest` |
| | 财新周刊 | `/caixin/weekly` |
| 全球 | 彭博社·市场 | `/bloomberg/markets` |
| | 彭博社·加密 | `/bloomberg/crypto` |
| | 彭博社·经济 | `/bloomberg/economics` |
| 政治 | 中国政府网·最新政策 | `/gov/zhengce/zuixin` |
| | 外交部·发言人表态 | `/gov/mfa/wjdt/fyrbt` |
| | 联合早报·国际 | `/zaobao/realtime/world` |
| | 彭博社·政治 | `/bloomberg/politics` |
| 思想 | Aeon·哲学 | `/aeon/category/philosophy` |
| | Psyche·Ideas | `/psyche/type/ideas` |
| | 爱思想·最近更新 | `/aisixiang/toplist/10` |

2026-11-07 首轮真实运行（128 条 / 12 个源可用）换掉了四条**在机房 IP 上取不到**的源，理由都写在
各自的 `note` 里：WSJ 经济标着 `antiCrawler`、路透商业被 Cloudflare 挡住、证券时报快讯与 21 财经热点
返回 0 条。替换挑的是同域名下已验证能用的路由（华尔街见闻 A 股、第一财经头条、格隆汇日热文）和一个
和华尔街见闻港股快讯。第二批运行又发现 雪球热帖 的 `requirePuppeteer: true`（公共实例没有
Playwright，必挂），一并换成港股快讯——**加源前先看路由的 `features`**：`requirePuppeteer`、
`requireConfig`、`antiCrawler` 这三种在公共实例上基本都取不到。

2026-11-07 第三轮（加了政治与思想两组源之后）清单里出现了新教训：

- **「解析出 N 条、时间窗内 0 条」不等于源坏了**。政策文件、哲学随笔本来就是周更甚至月更，
  全局 24 小时窗口把它们整条滤掉。现在每条源可以单独写 `max_age_hours`（中国政府网 168、
  外交部 72、Aeon/Psyche 336、爱思想 168），放宽后靠 `news_seen.json` 的标题指纹保证不重复推。
- **CoinDesk 连挂三轮**（超时 / 503），换成 AP News 自带的官方 RSS（`/apnews/rss/business`）——
  上游本身就提供 RSS 的源最不容易挂。
- 第一财经头条能解析出 30 条但全部在窗口外（接口回的是老稿），与「第一财经·最新」重复，删掉。
- 第四轮：香港01 即时的列表混着娱乐稿（抓到「璀璨之城大结局｜吳卓羲…」），换掉；AP News 商业
  在公共实例上回 503，也换掉。反复 503 的都是「要现抓很多页」的路由（即时新闻、Espresso），
  而同命名空间里较轻的路由正常（明报 pns 版、彭博各频道）——**换源优先挑「同命名空间里已经跑通
  的那条」**，比试新站点稳。
- 第五轮定下来：全球＝彭博经济；政治＝彭博政治 + 中文官方口径；深度＝财新周刊（明报这个命名空间
  在公共实例上不稳：即时版与经济版 503、观点版报 XML 语法错误，全组放弃）；思想＝Aeon、Psyche、
  爱思想·最近更新。爱思想的「推荐排行」（`toplist/11`）也试过：29 条全是常青旧文，14 天窗口内 0 条，
  不适合日报，删掉。

清单里的 `sources_failed` / `sources_error` / `instances_probe` 每轮都会记下失败源、具体报错
和实例探活结果——不用翻 CI 日志也能判断是 403、超时，还是真的没更新。

这些路由都按 RSSHub 仓库 `lib/routes/<namespace>/…` 里的 `route.path` 核对过（namespace 拼在路径前面）。
注意 **namespace 是目录名，不是 `namespace.ts` 里的 `name`**：注册代码按顶层目录拼路径
（`lib/registry-dev.ts` 的 `/${name}${key}`），`name` 只是文档上的显示名——21 财经是
`/21caijing/channel/热点`（目录），不是 `/21财经/…`（显示名）。
挑源的思路是**优先补 004 那 49 条没有的**，而且优先「站点本身没有原生 RSS、或原生 RSS 常被墙」的站。

每条源还带一个 `site`（站点域名），只用于「这条源抓不到时去问 RSSHub Radar 该换哪条路由」；
没有 RSSHub 路由、但站点自带 RSS 的，用 `feed` 直接写原生地址（`route` 与 `feed` 二选一，
目录自检会拦）。

加一条新源：

1. 在 RSSHub 仓库里找到路由（`lib/routes/<namespace>/<file>.ts` 的 `route.path`），拼上 namespace
   得到完整路径，例如 `caixin/latest.ts` 的 `path: '/latest'` → `/caixin/latest`；
2. 写进 `tools/news_sources.py` 的 `SOURCES`（`key` 唯一、`group` 用 `GROUPS` 里的键）；
3. 单源验一下：`python tools/fetch_news.py --only 新KEY --min-items 1 --no-seen`；
   抓不到就是路由或站点变了，别留一条永远失败的源。

`python -m unittest tests.test_news` 里的 `CatalogTest` 会检查 key 唯一、分组存在、路由像完整路径。

## 3. 找不到路由：用 RSSHub Radar

RSSHub Radar 是 RSSHub 官方的浏览器扩展，回答「这个站有没有 RSS、对应的 RSSHub 路由是什么」。
它的规则就是每条路由的 `radar` 字段，由实例的 API 汇总：

```
GET {实例}/api/radar/rules/{domain}     单个域名（推荐，省流量）
GET {实例}/api/radar/rules              全部规则
```

形状（`lib/api/radar/rules/utils.ts` 生成）：`{域名: {"_name": 站名, 子域|".": [{title, target, source, docs}]}}`，
`target` 就是能拼到实例后面的路由，`source` 是站点上的对应路径。

**手工找**

```sh
python tools/radar_discover.py https://www.hexun.com/
python tools/radar_discover.py https://www.hexun.com/ --emit-source   # 直接给可粘贴的源条目
python tools/radar_discover.py https://www.hexun.com/ --json          # 机器可读
python tools/radar_discover.py https://a.com/ --rules ./radar_rules.json   # 用本地快照，不打网络
```

顺序与扩展一致——**先规则、后原生**：

1. `www.hexun.com` → 先找 `hexun.com` 下的 `www` 子域规则，没有再退到 `"."`；
2. 没有规则就抓页面找 `<link rel="alternate" type="application/rss+xml|atom+xml|feed+json">`
   ——站点自己的 RSS。没有 RSSHub 路由的站，往往靠这一条，写进目录时用 `feed` 字段。

**自动旁注**：`fetch_news.py` 的某个源彻底失败时，会顺手问一次
`{第一个实例}/api/radar/rules/{该源的 site}`，把建议写进日志与 `news_data.json` 的
`sources[].radar`：

```
⚠️ WSCN_LIVE    华尔街见闻 · 要闻快讯     未取到  ...  HTTP Error 404
   Radar 建议：/wallstreetcn/live/:category?（实时快讯）
```

它**只给建议、不自动换源**：`target` 里带 `:param` 的要人定默认值，原生 feed 也要看一眼是不是
全站流。确认后改 `tools/news_sources.py`，跑 `python -m unittest tests.test_news` 即完成加源。
Radar 的 API 也不通时（比如实例整体挂了）这一行就不出现，抓取照常按失败处理。

## 4. 实例回退与限制

公共实例按顺序试，默认顺序（也可用 `RSSHUB_BASE` 环境变量覆盖，逗号分隔）：

```
https://rsshub.rss.tips, https://rsshub.isrss.com, https://rsshub.cups.moe, https://rsshub.app
```

- `rsshub.app` 对 GitHub Actions 的出口 IP 常直接回 403（004 的 CI 日志里出现过），所以排最后；
  自建实例建议放最前面。
- 同一个实例连续失败 `--trip-after`（默认 3）次就不再试它，免得 17 源 × N 实例的 403 风暴把一轮抓取拖死；
  某个源抓不到只影响那一条，不影响别的源。
- 单源单轮最多 `--per-source`（默认 12）条，只收最近 `--max-age-hours`（默认 24）小时内的；
  只有时间窗更短，才能保证一天一份、不把昨天的稿子再堆进来。
- 时间：RSS 的 `pubDate`（RFC822）和 Atom 的 `published` / `updated`（RFC3339）都认，RDF 认 `dc:date`；
  认不出时间的条目照样收，排在有时刻的后面。
- 部分上游本身要登录（401）或改版（404），这类源会持续出现在 `sources` 的 `error` 里——那是在提醒
  你换路由，不是程序坏了。

## 5. 去重

两层：

- **同一轮内**：标题去掉空白与标点后相同就算同一件事（多家转载常见）。保留**最早发布**的那条，
  避免转载时间把旧闻顶到最上面；
- **跨天**：`site/news_seen.json` 记着最近 7 天推过的标题指纹（sha1 前 12 位）。抓取时命中就跳过，
  所以昨天 20:00 的稿子不会在今天的日报里再出现一次。想重抓全量就加 `--no-seen`。

乱码标题（`锟斤拷`、`□□□`、`æ–‡` 这类）在抓取和排版两层都会被丢掉——沿用报告那边
`tools/build_epub.py` 的 `is_garbled` / `garbled_ratio`，不另立一套规则。

## 6. 自动发布

`.github/workflows/refresh-news.yml`（**只出 xml**）：

- `cron: "0 23 * * *"`（UTC）= 每天 07:00 北京时间；也可以手动 `workflow_dispatch`；
- 流程：跑测试 → `fetch_news.py` → `translate_news.py` → `build_feed.py` → feed 自检
  （xml 可解析、条目数达标、guid 不缺不重、`atom:link rel="self"` 对得上）→ 提交推送
  `site/news.xml` + `site/news_seen.json` + `site/news_zh_cache.json`；
- 任何一步失败就**不推送**：抓不到足够条目、xml 解析不过时，读者手上还是上一份 feed；
- 与小时级报告完全隔离（`concurrency: refresh-news`），报告失败不影响 feed，feed 失败也不动报告产物；
- 改了抓取/翻译/feed 的代码（`tools/fetch_news.py`、`news_sources.py`、`build_feed.py`、
  `radar_discover.py`、`translate_news.py`、本工作流）也会跑一次：这种运行带 `--no-seen`，
  按完整时间窗生成、也不写标题指纹，免得把当天还没到 07:00 的条目提前吃掉。

## 7. 订阅方式

- **KOReader**：工具 → **新闻下载器**（News downloader）→ 管理新聞源 → 新增，把地址粘进去；
  报「內容下載失敗」就按「2.1 多入口」换一条地址再试；
- **其它阅读器**：NetNewsWire / Feedly / Reeder / Inoreader 里直接加这个 feed 地址；
- **插件**：`News subscription` 模式读的是 `site/news.json`，工作流已不再生成它。想继续用那个模式，
  就手动跑 `build_news.py`（见「做那本 EPUB」）并自己提交 `site/news.epub`、`site/news.json`、
  `site/news.txt`，再在菜单 → **004 Report** → **News manifest URL** 填清单地址。

地址填 `raw.githubusercontent.com` 时，连不上会自动改试 jsDelivr 镜像；也可以在同一个框里写多个地址
（空格或逗号分隔），插件按顺序试。菜单 → **Check URLs (network)** 会逐个地址给结果，用来判断这台
设备当前哪条线路通。详见 README「地址连不上时」。

`Status` 里分别显示当前来源的清单地址、书名、是否打开，以及 `Data:` ——新闻那边是**最新一条新闻的
发布时间**（`data_time_kind: item`），比「文件生成时间」更能说明这份日报新不新。

## 8. 排错

| 现象 | 先看什么 |
| --- | --- |
| feed 没更新 | Actions 运行日志：`fetch_news.py` 每源一行 `✅ / ⚠️`，`build_feed.py` 末尾会列出失败源与报错 |
| 只剩几个源有稿 | 实例被限流/403：换 `RSSHUB_BASE`（自建实例最稳） |
| 某条源一直 404/401 | 看日志里的 `Radar 建议`；也可以 `python tools/radar_discover.py <站点域名>` 自己查 |
| 条目少得异常 | 是不是刚手动跑过一次？`site/news_seen.json` 把已推条目挡掉了，`--no-seen` 可重抓 |
| 阅读器一直不更新 | feed 地址是否用 `raw.githubusercontent.com`（连不上换 jsDelivr）；`channel/lastBuildDate` 是不是今天 |
| 有条目质量差 | 先看是哪条源：抓取只做粗筛（标题、乱码、时间窗），正文质量取决于上游 RSS |
