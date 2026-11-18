# 同花顺行情面板（hithink-finance）

从同花顺官方 GitHub 仓库 [Hithink-Tech-Org/Financial-API](https://github.com/Hithink-Tech-Org/Financial-API)
的金融数据服务（hithink-finance）抓 A 股行情，排成四份产物：

| 产物 | 给谁看 | 刷新方式 |
| --- | --- | --- |
| `site/stocks.json` | 清单 + 数据（Kindle 插件与网页面板都读它） | 工作流每 15 分钟重写（+ 体检补跑） |
| `site/stocks.html` | 浏览器卡片仪表盘 | 每 7 分钟拉一次 `stocks.json` |
| `site/stocks.epub` | Kindle 字模式卡片版（交给阅读器排版） | 每 7 分钟查一次清单，变了才重下 |
| `site/stocks.txt` | Kindle 纯文本面板（EPUB 不可用时的回退） | 同上 |

面板是**表格 + 图形 + 卡片**三层：

- **指数卡**（小卡）：最新点位、涨跌幅、近 20 日迷你走势图；
- **自选股卡**（大卡）：最新价、涨跌额/涨跌幅、开/高/低、成交量、成交额，
  底部一条 SVG 迷你走势图（近 20 个交易日收盘连成的折线）；
- **涨停表格**：名称、最新、涨跌幅、封板时间、原因、连板数、封单额。

面板内容：宽基指数（上证/深证/创业板/沪深300/科创50/中证500/中证1000）+ 自选股
（默认约 40 只行业龙头，按涨跌幅排序）+ 当日涨停池（前 20，按封板时间）。

迷你走势图的数据来自历史 K 线接口（`/api/a-share/prices/historical-batch` 与
`/api/a-share-index/prices/historical-batch`，参数 `interval=1d`、`adjust=forward`，
每批 ≤10 个代码），只取最近 20 个交易日收盘价，存进清单的 `history` 字段
（`{thscode: [[毫秒时间, 收盘], ...]}`）。要改天数用 `fetch_stocks.py --history-days N`
（`0` = 不取历史，面板上就没有迷你图）。

## 0. 先说清楚「一分钟」

- **服务端：一次触发，自己跑满交易时段**。GitHub Actions 的 cron 在这个仓库被
  **严重限流**（实测：配了 `3,18,33,48` 每 15 分钟，合并后 4.2 小时内 0 次触发；
  连每小时任务的真实间隔都是 4–10 小时）。所以 `refresh-stocks.yml` 不依赖 cron，
  而是：任意一次触发（cron / push / 手动）后，在**同一个 runner 里每 15 分钟**重跑
  抓取+排版+提交，跑满约 5.5 小时的作业预算，收尾时如果还在交易时段就
  `workflow_dispatch` 把下一棒交给自己（`GITHUB_TOKEN` 触发 dispatch 已实测可用）。
  连续 4 轮抓取失败（约 1 小时）就中止且不交接，避免空转。
  `refresh-stocks-watchdog.yml` 只是额外保险：它被叫醒时若发现 `data_time`
  落后 >25 分钟，就 `workflow_dispatch` 补一枪。
- 判断「盘中」= 北京时间周一至周五 09:15–15:05；收盘后自循环自动停下，
  数据就停在收盘那一刻，这是正常的，面板会显示「已收盘」。

数据新鲜度以清单里的 `data_time`（快照数据时间）为准，不是生成时间；
收盘后快照时间不变，工作流按 `data_hash` 判断「行情没变就不提交」。

**面板自己会说实话**：纯文本/EPUB 抬头写「数据 2026-11-18 10:00:01 · 3 分钟前 ·
盘中」，超过 20 分钟会变成 `STALE 超过 20 分钟没更新（定时任务可能被延迟）`；
网页版顶部状态徽章会从 `ONLINE` 变 `STALE`，并展开一条提示条。看到 `未成交` /
`--` 而不是 `0.00 手`，说明那一轮快照没有成交数据（开盘前或接口抽风），不是成交为零。

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

只看某几只：`--codes 600519.SH,000001.SZ`；只看某几个指数：`--indices 000001.SH`；
不要涨停池：`--no-limit-up`。自选清单在 `tools/stock_watchlist.py`，加股票
往 `STOCKS` 里加一行 `(thscode, 名字)` 即可（代码必须带交易所后缀，如 `600519.SH`）。

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

EPUB 卡片版长这样：每只股票一张表格卡片（名称/代码、大字最新价、涨跌幅、
开高低、成交/成交额），下面一行文字走势「近20日收盘 1,200.00→1,276.00
（区间 1,200.00–1,276.00）」。crengine 不认 flex/grid/SVG，所以 Kindle 上
迷你图改成这行文字，真正的折线图只在网页版/浏览器里有。

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

## 6. 边界

- 只覆盖 A 股（官方服务是 A 股金融数据服务）；港股/美股不在这个接口范围内。
- 快照是「最新成交价」级别的延迟行情，不是逐笔/Level-2；下单前请以券商行情为准。
- 面板只做信息展示，**不构成投资建议**（文本与页面底部都写了）。
