# 同花顺行情面板（hithink-finance）

从同花顺官方 GitHub 仓库 [Hithink-Tech-Org/Financial-API](https://github.com/Hithink-Tech-Org/Financial-API)
的金融数据服务（hithink-finance）抓 A 股行情快照，排成三份产物：

| 产物 | 给谁看 | 刷新方式 |
| --- | --- | --- |
| `site/stocks.json` | 清单 + 数据（Kindle 插件与网页面板都读它） | 工作流每 5 分钟重写 |
| `site/stocks.txt` | Kindle 纯文本面板 | 清单 `generated` 变了才重下 |
| `site/stocks.html` | 浏览器数据面板 | 每 60 秒拉一次 `stocks.json` |

面板内容：宽基指数（上证/深证/创业板/沪深300/科创50/中证500/中证1000）+ 自选股
（默认约 40 只行业龙头，按涨跌幅排序）+ 当日涨停池（前 20，按封板时间）。
每只股票显示最新价、涨跌幅、涨跌额、开/高/低、成交量、成交额。

## 0. 先说清楚「一分钟」

- **服务端最快 5 分钟一轮**：GitHub Actions 的定时任务最短 5 分钟，做不到 1 分钟。
  工作流在 A 股交易时段（北京时间 09:00–15:55，周一到周五）每 5 分钟抓一轮，
  见 `.github/workflows/refresh-stocks.yml`。
- **设备端每 60 秒检查一次**：Kindle 插件与网页面板都每 60 秒拉一次清单，
  `generated` 变了才刷新——和 EPUB/新闻同一套规矩。
- **本地要真正的 60 秒一轮**：`python tools/fetch_stocks.py --loop 60 --build`
  常驻跑（见第 3 节）。

数据新鲜度以清单里的 `data_time`（快照数据时间）为准，不是生成时间；
收盘后快照时间不变，工作流按 `data_hash` 判断「行情没变就不提交」。

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
这里只用三个接口：A 股快照、指数快照、涨停池；中文名用标的检索补，
补过的缓存在 `site/stocks_names.json`。

## 2. 手动跑一轮（验证）

```sh
python tools/fetch_stocks.py --out stocks_data.json   # 抓取 → 中间产物（不进 Git）
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

每 60 秒只拉一次几百字节的清单：没更新就翻页，`generated` 变了才重下
`stocks.txt` 并从头显示。状态菜单（Status）会显示行情的 `data_time`，
超过 24 小时没更新会标 `stale`。

## 5. 排错

- **工作流一跑就失败**：先看「从同花顺抓一轮行情快照」步骤的日志。
  `缺少同花顺 API Key` = Secret 没建或名字不对（必须是 `THS_API_KEY`、
  `THS_KEY` 或 `HITHINK_FINANCE_API_KEY`）；`code=2003` = Key 无效/无权限，去官网 Key 管理页重签；
  `code=4001` = 限流，脚本会自动退避重试 3 次。
- **面板是演示数据**：说明真实抓取还没跑成功过——`site/stocks.json` 里
  `"demo": true` 就是演示版，工作流跑通一轮会自动覆盖。
- **Kindle 显示 OFFLINE / CACHED**：清单地址不通，用菜单里的
  `Check URLs (network)` 逐个试，换一条能通的地址（raw / Pages / jsDelivr）。
- **收盘后数据不变**：正常。快照时间停在收盘那一刻，`data_hash` 相同
  工作流就不提交；面板上看 `data_time` 就知道数据停在哪一刻。

## 6. 边界

- 只覆盖 A 股（官方服务是 A 股金融数据服务）；港股/美股不在这个接口范围内。
- 快照是「最新成交价」级别的延迟行情，不是逐笔/Level-2；下单前请以券商行情为准。
- 面板只做信息展示，**不构成投资建议**（文本与页面底部都写了）。
