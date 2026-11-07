# 每日新闻订阅（RSSHub）

每天 **07:00（北京时间）** 生成一本只含新闻的 EPUB，和小时级报告分开存放、分开刷新：

| 文件 | 内容 | 谁用 |
| --- | --- | --- |
| `site/news.epub` | 当天的新闻，按「快讯 / 深度 / 全球」分节 | 插件「News subscription」模式 |
| `site/news.json` | 清单：`generated`、`doc`、条目数、源可用数、最新一条的时间 | 插件每 60 秒只拉这个 |
| `site/news.txt` | 同一份内容的纯文本 | 文本模式或自己看 |
| `site/news_seen.json` | 最近 7 天推过的标题指纹 | 抓取时跨天去重，不进书 |

抓的是 [RSSHub](https://github.com/DIYgod/RSSHub) 的公开路由，**不重排 004 的报告页**：报告页每小时才刷，
而且只渲染当次抓到的条目，拿不到「这条新闻来自哪个源、原文在哪」。

## 1. 两条命令

```sh
python tools/fetch_news.py --out news_data.json     # 抓一轮，写到 news_data.json（不进 Git）
python tools/build_news.py --data news_data.json --out site
```

`news_data.json` 是中间产物：抓取和排版分开，排版可以离线反复调。`news_data.json` 里每条是
`{title, link, published, summary, source, group, lang}`，`sources` 里记着每个源这次用的实例、
抓到几条、失败原因——出问题先看这里。

`build_news.py` 的规矩：

- 一条新闻 = 标题 + `来源 · 北京时间` + 最多一句摘要。摘要是 RSS 的 `description` / `summary`
  去掉标签后的文字，和前一句相同就不重复出；
- 按目录分组顺序分节，**空组跳过、序号顺排**（所以某天没深度稿时不会出现「一、… 三、…」）；
- 少于 `--min-items`（默认 10）条、或者正文乱码占比超过 2% → 退出码 1，**不覆盖旧产物**：
  宁可设备上还是昨天的日报，也不推一份空书。

## 2. 源目录

源写在 `tools/news_sources.py`，17 条：

| 分组 | 源 | RSSHub 路由 |
| --- | --- | --- |
| 快讯 | 财联社·电报 | `/cls/telegraph` |
| | 华尔街见闻·要闻快讯 | `/wallstreetcn/live/global` |
| | 金十数据 | `/jin10` |
| | 证券时报·快讯 | `/stcn/article/list/kx` |
| | 同花顺·实时财经新闻 | `/10jqka/realtimenews` |
| | 第一财经·简报 | `/yicai/brief` |
| | 新浪·股市滚动 | `/sina/rollnews/2517` |
| 深度 | 华尔街见闻·要闻文章 | `/wallstreetcn/news/global` |
| | 财新·最新 | `/caixin/latest` |
| | 格隆汇·推荐 | `/gelonghui/home` |
| | 第一财经·最新 | `/yicai/latest` |
| | 香港01·即时 | `/hk01/latest` |
| | 21 财经·热点 | `/21caijing/channel/热点` |
| 全球 | 路透社·商业财经 | `/reuters/business` |
| | 彭博社·市场 | `/bloomberg/markets` |
| | WSJ·经济 | `/wsj/en-us/economy` |
| | CoinDesk·最新 | `/coindesk/news` |

这些路由都按 RSSHub 仓库 `lib/routes/<namespace>/…` 里的 `route.path` 核对过（namespace 拼在路径前面）。
挑源的思路是**优先补 004 那 49 条没有的**，而且优先「站点本身没有原生 RSS、或原生 RSS 常被墙」的站。

加一条新源：

1. 在 RSSHub 仓库里找到路由（`lib/routes/<namespace>/<file>.ts` 的 `route.path`），拼上 namespace
   得到完整路径，例如 `caixin/latest.ts` 的 `path: '/latest'` → `/caixin/latest`；
2. 写进 `tools/news_sources.py` 的 `SOURCES`（`key` 唯一、`group` 用 `GROUPS` 里的键）；
3. 单源验一下：`python tools/fetch_news.py --only 新KEY --min-items 1 --no-seen`；
   抓不到就是路由或站点变了，别留一条永远失败的源。

`python -m unittest tests.test_news` 里的 `CatalogTest` 会检查 key 唯一、分组存在、路由像完整路径。

## 3. 实例回退与限制

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

## 4. 去重

两层：

- **同一轮内**：标题去掉空白与标点后相同就算同一件事（多家转载常见）。保留**最早发布**的那条，
  避免转载时间把旧闻顶到最上面；
- **跨天**：`site/news_seen.json` 记着最近 7 天推过的标题指纹（sha1 前 12 位）。抓取时命中就跳过，
  所以昨天 20:00 的稿子不会在今天的日报里再出现一次。想重抓全量就加 `--no-seen`。

乱码标题（`锟斤拷`、`□□□`、`æ–‡` 这类）在抓取和排版两层都会被丢掉——沿用报告那边
`tools/build_epub.py` 的 `is_garbled` / `garbled_ratio`，不另立一套规则。

## 5. 自动发布

`.github/workflows/refresh-news.yml`：

- `cron: "0 23 * * *"`（UTC）= 每天 07:00 北京时间；也可以手动 `workflow_dispatch`；
- 流程：跑测试 → `fetch_news.py` → `build_news.py` → 产物自检（条目数、分节数、EPUB 可读、
  清单指向的文件存在）→ 提交推送 `site/news.epub`、`site/news.json`、`site/news.txt`、`site/news_seen.json`；
- 任何一步失败就**不推送**：抓不到足够的条目时旧日报保持原样，设备不会拿到空书；
- 与小时级报告完全隔离（`concurrency: refresh-news`），报告失败不影响新闻，新闻失败也不动报告产物。

## 6. 设备端

1. 菜单 → **004 Report** → **News manifest URL** 填 `site/news.json` 的地址
   （例如 `https://k-macao.github.io/04-kindle/site/news.json`，以实际发布地址为准）；
2. **Display mode → News subscription**；
3. **Show report** 开始。

之后和字模式一样：每 60 秒只拉一次几百字节的清单，`generated` 变了才重下 EPUB 并从头打开；
新的一天下的是新文件（`octopus004-news-<n>.epub`），旧文件自动清掉，报告那本
（`octopus004-report-<n>.epub`）互不影响。两份书各自记 `sig`，所以来回切模式不会重复下载。

`Status` 里分别显示当前来源的清单地址、书名、是否打开，以及 `Data:` ——新闻那边是**最新一条新闻的
发布时间**（`data_time_kind: item`），比「文件生成时间」更能说明这份日报新不新。

## 7. 排错

| 现象 | 先看什么 |
| --- | --- |
| 日报没更新 | Actions 运行日志：`fetch_news.py` 打印的每源一行 `✅ / ⚠️`；`sources` 里的 `error` |
| 只剩几个源有稿 | 实例被限流/403：换 `RSSHUB_BASE`（自建实例最稳） |
| 条目少得异常 | 是不是刚手动跑过一次？`site/news_seen.json` 把已推条目挡掉了，`--no-seen` 可重抓 |
| 设备一直不更新 | 清单 URL 是否指向 `site/news.json`；`Status` 里的 `generated` 与网页上的是否一致 |
| 书里出现站壳/乱码 | 先看是哪条源：`build_news.py` 只做粗筛（标题与摘要），正文质量取决于上游 RSS |
