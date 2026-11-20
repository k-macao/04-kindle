# 同花顺行情面板（hithink-finance）

从同花顺官方 GitHub 仓库 [Hithink-Tech-Org/Financial-API](https://github.com/Hithink-Tech-Org/Financial-API)
的金融数据服务（hithink-finance）抓 A 股行情，排成四份产物：

| 产物 | 给谁看 | 刷新方式 |
| --- | --- | --- |
| `site/stocks.json` | 清单 + 数据（Kindle 插件与网页面板都读它） | 工作流每 15 分钟重写（+ 体检补跑） |
| `site/stocks.html` | 浏览器卡片仪表盘 | 每 7 分钟拉一次 `stocks.json` |
| `site/stocks.epub` | Kindle 字模式卡片版（交给阅读器排版） | 每 7 分钟查一次清单，变了才重下 |
| `site/stocks.txt` | Kindle 纯文本面板（EPUB 不可用时的回退） | 同上 |

面板是**一屏市场监视屏**，按信息密度从上到下分六段（最后一段是板块轮动 RRG）：

| 段 | 内容 |
| --- | --- |
| **市场概览** | 全 A 5584 只的涨跌家数（含一条涨/平/跌比例横条）、涨停/跌停数、平均涨跌幅、两市成交额、涨幅前五/跌幅前五 |
| **指数** | 13 个宽基：上证、深证成指、创业板指、沪深300、科创50、中证500、中证1000、上证50、中小100、上证180、创业板50、中证100、上证380 |
| **板块** | 91 个同花顺行业指数（一级/二级行业全覆盖），按涨跌幅排序；网页是密集小卡（涨跌幅带强度条），Kindle 是两栏文字 |
| **港股** | 恒生指数、国企指数、恒生科技指数 + 20 只龙头个股（走腾讯公开行情，见下） |
| **涨停池** | 名称、最新、涨跌幅、封板时间、原因、连板数、封单额（`--no-limit-up` 可关） |
| **板块轮动 · RRG** | 91 个板块在「相对强度 × 相对强度动量」四象限里的位置（领先/改善/滞后/疲软）、今日迁移、信号 A 名单、今日读数（`--no-rrg` 可关，见 §6） |

**不逐只列 5584 只个股**：明细只落在 `site/stocks.json`（`market` 字段是统计口径，
`indices` 是指数/板块明细），面板只画上面六段——Kindle 一屏能看完全市场温度，
最后一段板块轮动 RRG 见 §6。

### 港股为什么走腾讯

同花顺的金融数据服务**只有 A 股**：实测请求港股行情返回 `code=1002`
（`Invalid parameter format`），港股域只公开了新股（IPO 打新）三个接口，没有行情。
所以港股改走**腾讯公开行情** `https://qt.gtimg.cn/q=hkHSI,hkHSCEI,...`（GBK，
`~` 分隔的纯文本），在 `tools/fetch_hk.py` 里解析。

两边**不混算延迟**：清单里 `index_timestamp` 是 A 股/板块的快照时间，
`hk.data_timestamp` 是港股行情时间，`data_timestamp` 取两者较大值（港股收盘
比 A 股晚一小时）。文本与网页上的港股段都会单独标「行情 <时间>」。

迷你走势图的数据来自历史 K 线接口（`/api/a-share/prices/historical-batch` 与
`/api/a-share-index/prices/historical-batch`，参数 `interval=1d`、`adjust=forward`，
每批 ≤10 个代码），只取最近 20 个交易日收盘价，存进清单的 `history` 字段
（`{thscode: [[毫秒时间, 收盘], ...]}`）。要改天数用 `fetch_stocks.py --history-days N`
（`0` = 不取历史，面板上就没有迷你图）。

## 0. 先说清楚「一分钟」

- **服务端：一次触发，然后常驻**。GitHub Actions 的 cron 在这个仓库**极不靠谱**
  （实测：配了每 15 分钟，4.2 小时内 0 次；另有一次本该 07:48 跑，拖到 18:34
  收盘后才跑）。所以 `refresh-stocks.yml` 不依赖 cron：任意一次触发后，在同一个
  runner 里常驻——抓取窗口内（北京 09:00–15:30，周一到周五）每 15 分钟一轮，
  **窗口外长睡**（每 30 分钟醒一次），因此一次迟到几小时的 cron 也不会白跑，
  它会一直等到次日开盘自己开工；预算（约 5.5 小时）用完时若当天还有活，
  就 `workflow_dispatch` 把下一棒交给自己（`GITHUB_TOKEN` 可触发 dispatch，已实测）。
  连续 4 轮抓取失败（约 1 小时）就中止且不交接，避免空转。
  `refresh-stocks-watchdog.yml` 是额外保险：被叫醒时若盘中 `data_time` 落后 >25
  分钟，就补一枪。
- 判断「盘中」= 北京时间周一至周五 09:15–15:05；收盘后自循环自动停下，
  数据就停在收盘那一刻，这是正常的，面板会显示「已收盘」。

数据新鲜度以清单里的 `data_time`（快照数据时间）为准，不是生成时间；
收盘后快照时间不变，工作流按 `data_hash` 判断「行情没变就不提交」。

**面板自己会说实话**：纯文本/EPUB 抬头写「数据 2026-11-18 10:00:01 · 3 分钟前 ·
盘中」，网页版顶部徽章同步。但**「过期」只在「该有数据却没有」时才报**：

| 数据的时段 | 什么时候才算 STALE |
| --- | --- |
| 盘中 / 午休 | 超过 20 分钟没更新（这是真问题） |
| 已收盘 / 周末 | 超过 24 小时还没有新数据（跨过一个交易日） |
| 未开盘 | 同上 |

收盘后数据本来就不再变，所以凌晨或周末看面板会显示「已收盘（收盘后不再更新）」
而不是 STALE。早期版本是死比「现在 − 数据时间 > 20 分钟」，凌晨看必然报警，纯误报。
**午休（11:30–13:00）单列**：11:45 抓到的快照成交量跟 11:30 一样，标成「盘中」
会让人以为行情还在动。

看到 `未成交` / `--` 而不是 `0.00 手`，说明那一轮快照没有成交数据
（开盘前或接口抽风），不是成交为零。

**开盘前的空壳快照不会盖掉有效面板**：同花顺在开盘前会给「成交量全 0、个股没有
最新价」的快照（指数还带着前收盘点位）。`build_stocks.py` 会判定这种情况
（`empty_snapshot()`）：上一份面板有成交时直接报错退出、不写文件，工作流那一步
失败也就不会提交——设备继续显示上一份有成交的面板，等下一轮再更新。

## 1. API Key（GitHub 配置）

统一 API Key 在同花顺金融数据服务官网创建：<https://fuyao.aicubes.cn/admin/>。

放到仓库的 Actions Secret 里（**不要**写进代码、日志或 Git 提交）：

- Secret 名：**`THS_API_KEY`** 或 **`THS_KEY`**（官方名 `HITHINK_FINANCE_API_KEY` 也兼容）；
- 值：官网创建的统一 API Key。

工作流里已经接好了（`refresh-stocks.yml` 的「从同花顺抓一轮行情快照」步骤），
Key 缺失或接口报错时那一步直接失败、**不推送**，读者手上还是上一份面板。

本地跑时用环境变量（官方名优先，也兼容 `THS_API_KEY` / `THS_KEY` / `API_KEY`）：

```sh
export THS_API_KEY=…   # 或 THS_KEY=… / HITHINK_FINANCE_API_KEY=…
```

接口约定（见官方仓库 `docs/api/README.md`）：Base URL `https://fuyao.aicubes.cn`，
认证头 `X-api-key: <API_KEY>`，成功判断是 HTTP 200 **且**信封 `code == 0`。
这里只用五个接口：A 股快照、指数快照、涨停池、A 股历史 K 线、指数历史 K 线；
中文名用标的检索补，补过的缓存在 `site/stocks_names.json`。

## 2. 手动跑一轮（验证）

```sh
python tools/fetch_stocks.py --out stocks_data.json   # 抓取（含近 20 日 K 线）→ 中间产物（不进 Git）
python tools/build_stocks.py --data stocks_data.json --out site
python -m unittest discover -s tests                  # 含 tests/test_stocks.py
```

没 Key 时先看版式（演示数据，取自官方文档示例，面板上会标「演示数据」）：

```sh
python tools/build_stocks.py --demo --out site
```

只看某几个指数/板块：`--indices 000001.SH`；不要涨停池：`--no-limit-up`；
不要板块轮动：`--no-rrg`；
不要全市场概览（5584 只，最慢的一步）：`--no-market`；不取港股：`--no-hk`。

清单在 `tools/market_watchlist.py`：`INDICES`（13 宽基）、`SECTORS`（91 板块）、
`HK_INDICES`/`HK_STOCKS`（港股 3+20）。加一行 `(代码, 名字)` 即可，代码必须带后缀
（A 股 `000001.SH` / 板块 `881101.TI` / 港股 `hk00700`）。

**加代码前先确认它真的存在，名字也要对着目录抄**：同花顺对含非法代码的批量请求是**静默返回空**
（`code=0` 但 `item=[]`），不是报错——一个坏代码能让整批全丢，宽基指数曾经因此
13 个全灭。所以 `fetch_index_rows` 发现批次返回条数少于请求数，就逐个单取确认，
把真正坏的代码挑出来记进 `errors`，其余照常出面板。

板块判定也有坑：同花顺行业指数大多是 `881xxx/884xxx.TI`，但「有色金属」是
`000819.SH`——只按后缀判会把它排进【指数】段（宽基就变 14 个了），
所以先查清单里的板块代码集合，再退回后缀。

**现在的清单是拿目录逐条校对过的**（`catalog/ths-index-list?tag=industry`，
320 条，其中 881* 系列 90 个）。校对前那份清单里有 36 个板块的名字是错的
（代码对、名字错，比如 `881121.TI` 被写成「电力设备」，目录里是「半导体」），
盘面上等于挂错牌子——所以加代码时务必对着目录抄名字，别凭印象。

## 3. 本地实时（60 秒一轮）

```sh
HITHINK_FINANCE_API_KEY=… python tools/fetch_stocks.py --loop 60 --build
```

每 60 秒抓一轮并重排 `site/`，Ctrl+C 停止。适合开着电脑盯盘；Kindle/手机
打开 `site/stocks.html`（或部署后的 Pages 地址）就能看到每分钟的新数据。

## 4. Kindle 上看

插件新增 **Display mode → Stocks panel**，清单地址填 `site/stocks.json` 的
HTTPS 地址（和 EPUB/新闻一样，支持多地址与镜像自动切换）：

```
https://raw.githubusercontent.com/k-macao/04-kindle/main/site/stocks.json
```

每 7 分钟只拉一次清单：没更新就翻一页，`generated` 变了才重下 `stocks.epub`
（文件名带序号，所以阅读器每次从头打开），交给 KOReader 阅读器排版显示
——字体、行距、页边距都跟着你设备上现有的阅读设置走。

EPUB 版是一串表格：市场概览 → 指数 → 板块（按涨跌幅，标「涨 N 跌 M」）→
港股 → 涨停池 → 板块轮动 RRG。crengine 不认 flex/grid/SVG，所以 Kindle 上**只有表格和标题**，
网页版的涨跌比例条、强度条、迷你走势图在 Kindle 上换成文字/表头说明。
纯文本版（`stocks.txt`）更省：板块两栏并排（中文按 2 列宽对齐），一屏能扫完。

清单里没有 `epub` 字段、或者 EPUB 下载失败（非 HTTPS / 不是 zip）时，
自动回退到纯文本 `stocks.txt`，不会白屏。状态菜单（Status）会显示行情的
`data_time`（超过 24 小时没更新标 `stale`）、检查间隔（`every 7m`）和当前打开的
STOCKS 文件名。

## 5. 排错

- **工作流一跑就失败**：先看「从同花顺抓一轮行情快照」步骤的日志。
  `缺少同花顺 API Key` = Secret 没建或名字不对（必须是 `THS_API_KEY`、
  `THS_KEY` 或 `HITHINK_FINANCE_API_KEY`）；`code=2003` = Key 无效/无权限，去官网 Key 管理页重签；
  `code=4001` = 限流，脚本会自动退避重试 3 次。
- **面板是演示数据**：说明真实抓取还没跑成功过——`site/stocks.json` 里
  `"demo": true` 就是演示版，工作流跑通一轮会自动覆盖。
- **Kindle 上看到的是纯文本而不是卡片**：说明 EPUB 那条路没走通（清单里没有
  `epub`、地址不是 HTTPS、下载到的不是 zip）。插件会写一条
  `stocks epub failed, falling back to text` 到 `crash.log`，照着那条日志查。
- **Kindle 显示 OFFLINE / CACHED**：清单地址不通，用菜单里的
  `Check URLs (network)` 逐个试，换一条能通的地址（raw / Pages / jsDelivr）。
- **收盘后数据不变**：正常。快照时间停在收盘那一刻，`data_hash` 相同
  工作流就不提交；面板上看 `data_time` 就知道数据停在哪一刻。

## 6. 板块轮动 RRG（策略来自 GitHub）

面板最后一段：91 个同花顺行业板块按 **RRG（相对旋转图）** 分到四个象限，回答
「今天这个板块是刚开始转强、还是强了很久在熄火」——单看涨跌幅排序看不出来。

### 策略出处

- 代码：GitHub [hugo2046/QuantsPlaybook](https://github.com/hugo2046/QuantsPlaybook)
  （约 6.9k ★，「量化研究 · 券商金工研报复现」）里的
  `C-择时类/相对旋转图RRG行业轮动/src/factor_algo.py`（JdK 比率法 RRG）；
- 信号：同目录 `src/strategy.py` 的 `select_by_quadrant`（信号 A：只留「领先」象限，
  按距中心 (100, 100) 的距离取前 N，**不补足**）；
- 研报：西部证券 2026-05-25《指数化配置系列研究（6）：相对旋转图 RRG 框架下的行业研究》。

那份实现依赖 pandas / numpy，本仓库的面板产物是纯文件，所以
`tools/sector_rrg.py` 把公式原样搬成标准库版（纯函数，无 I/O），
连 warm-up 行数都照它的《RRG 算法说明》核过：

```
RS       = close / benchmark × 100
RS-Ratio = MA_smooth(100 × RS_t / RS_{t-ratio})              中枢 100（X 轴）
RS-Mom   = MA_smooth(100 × RS-Ratio_t / RS-Ratio_{t-mom})    中枢 100（Y 轴）

象限 1 领先 (>100, >100) · 2 改善 (<100, >100)
     3 滞后 (<100, <100) · 4 疲软 (>100, <100)
```

- **基准**按研报 §2.1 口径取「板块等权组合净值」（各板块日收益等权复合、首日 1.0），
  不是某只宽基；RS-Ratio 只依赖基准的收益比，对基准绝对水平尺度不变。
- **参数**先试研报口径 `220/60/20`（要 319 根板块日 K，约一年半）；
  拿不够就退「短窗 `60/20/5`」（89 根），并在面板上写明用的是哪一套
  ——**不同参数不混着比**。两根都不够时这一段整段不出现，只在 `errors` 里留一句。
- **今日**：历史 K 线通常不含今天（盘中），所以用快照的最新价覆盖/补上今天那一行，
  盘中 15 分钟一轮就能看到点位在四个象限里挪动。

### 怎么读

| 现象 | 含义 |
| --- | --- |
| 落在**领先**且轨迹继续朝右上 | 相对强度在趋势上、动量还在抬——轮动的主线 |
| 从**改善**进入**领先** | 拐点确认（面板的「今日迁移」会点名） |
| 在**疲软**但今日仍在涨 | RS 还高、动量已跌破 100：强势在衰减，追高要留一半清醒 |
| 在**滞后**但今日反弹 | 超跌修复，不是趋势反转 |

网页版把四象限画成一张灰度图（横轴 RS-Ratio、纵轴 RS-Momentum，细线是最近 6 个
交易日的轨迹，黑点是信号 A 名单）；Kindle 的纯文本/EPUB 只有「象限计数 + 今日读数 +
四个象限的清单 + 表格」，crengine 不认 SVG。

### 开关与成本

```sh
python tools/fetch_stocks.py --out stocks_data.json            # 默认就算 RRG
python tools/fetch_stocks.py --no-rrg                          # 不算（省 10 次批量请求）
python tools/fetch_stocks.py --rrg-days 340 --rrg-top-n 6      # 日 K 根数 / 信号 A 的 N
python tools/fetch_stocks.py --rrg-preset short                # 锁死参数档（report / short）
python tools/fetch_stocks.py --rrg-cache-minutes 60            # 板块长期日 K 的缓存时长
```

91 个板块的 340 根日 K 是 10 次批量请求（每批 ≤10 个代码），比迷你走势图的 20 根重
得多；所以工作流那个「同一 runner 常驻循环」里，长期日 K 按「当天 + 60 分钟 + 同一
批代码」缓存，每轮只用快照最新价覆盖今天那一行。接口抽风时沿用当天早些时候的缓存
（面板的 `errors` 会写一句），不会让整段消失，更不会拖垮别的段。

### 边界

- RRG 描述的是**相对**强弱的位置，不是绝对涨跌，也不是择时信号；研报的完整策略还
  包含成分股、扩散指标、月末调仓与回测，这里只复现了「象限 + 信号 A」这一层，
  **没有回测、没有收益承诺**，和面板其它段一样「仅供信息展示，不构成投资建议」。
- `today_pct` 来自快照（真实涨跌幅），RS-Ratio / RS-Mom 来自日 K 收盘价的比值；
  盘中最后一根是「未走完的日 K」，收盘后才定型。
- 参数不同、基准不同都会改变象限归属，跨面板比较前先看抬头写的是哪套参数。

## 7. 边界

- A 股/指数/板块走同花顺（官方服务只有 A 股）；港股走腾讯公开行情（美股没有）。
- 市场概览是**全 A 5584 只**的统计，一次要翻 3 页（`limit=2000`），约 5–10 秒；
  这是整轮里最慢的一步，可以用 `--no-market` 跳过。
- 快照是「最新成交价」级别的延迟行情，不是逐笔/Level-2；下单前请以券商行情为准。
- 面板只做信息展示，**不构成投资建议**（文本与页面底部都写了）。
