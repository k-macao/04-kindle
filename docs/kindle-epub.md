# 字模式：把 004 排成 EPUB 交给 KOReader 阅读器

截图模式（`render_report.py`）保真，但本质是图片：放大就发虚、每轮刷新要下几 MB、
字号改不了。**字模式**换一条路——把 004 的正文清洗成 XHTML，配一套电子墨水用的
CSS，打包成单文件 EPUB。插件把它下载到设备后交给 **KOReader 自己的阅读器**打开，
于是：

- 文字是矢量渲染，任何字号都锐利；
- KOReader 已有的字号、行距、页边距、字体、进度条、字典、目录全部可用；
- 一个文件几十 KB，刷新只重下一本书，不再有几 MB 图片；
- 标题分级、表格黑线、列表缩进都是真正的排版，不是等宽字堆。

```
004 构建页  →  build_epub.py  →  site/report.epub + site/epub.json
                                        ↓ 上传到仓库
       设备：插件拉 epub.json → generated 变了才下 report.epub → 打开阅读器
```

## 1. 生成

只依赖 Python 标准库，**不需要** Playwright / Pillow：

```sh
python tools/build_epub.py https://k-macao.github.io/004/ --out site
```

输出：

```
site/report.epub     单文件 EPUB 3（含 toc.ncx，兼容旧版 crengine）
site/epub.json       {"generated", "doc", "title", "chars", "bytes",
                      "sections", "data_time", "source"}
```

成功时打印 `完成：84 KiB，19342 字符，site/report.epub`。

手头有本地 HTML 也可以：`python tools/build_epub.py report.html --out site`
（相对链接会失效，公开页面抓取时会自动改成绝对地址）。

生成内容做了什么：

- 只取 `<main>` 里的正文，`nav`/`footer`/`button`/`script`/`svg` 等整段丢弃；
- 保留 `p / h1-h6 / ul / ol / li / table / thead / tbody / tr / td / th / b / strong / em / a / img / hr` 等；
- 删掉所有 `class`、`id`、内联 `style`、`on*` 事件属性——避免 004 的样式混进来；
- `a[href]`、`img[src]` 改成绝对地址；
- 注入的 CSS：白底黑字、正文行距 1.55、标题 1.55/1.3/1.14em、表格满宽 + 1px 纯黑
  实线 + 上下 0.2em/0.3em 内边距、表头跨页重复（`thead`）、行不断页（`tr`）。

### 内容重排：把 004 的写作套路变成版式

004 的正文是「一句话里塞标题、标签、分数、归类、读数」，直接排版会糊成一片。脚本按它
稳定的写法做了结构识别，纯文本没有的信息层级这里补回来：

| 原文形态 | 重排后 |
| --- | --- |
| `1. 全球风险偏好（…）重点 · 利空 · 100.0 分` | `<h3>` 小标题 + 独立信号行：`重点`、`利空` 两枚描边标签 + 放大的 `100.0` |
| `归类：市场盘面　当次读数：…` | 两列 **kv 表**（左列 5.5em 固定宽加粗，右列内容） |
| `扫描时间 … · 行情日期 … · 数据覆盖 99% · 口径：…` | 同一套 kv 表（`·` 分隔的元信息也识别），`口径` 允许长文本 |
| `▶ AI 总结解读：…；提取信息：…；规则判断：…` | 左边线引用块，按 `提取信息：` / `规则判断：` 拆成独立段落 |
| `◆ 三大关注面 · …` | 四级标题 |
| 一条力量的整段（标题 + 信号 + 指标 + 证据 + AI 解读） | 收进同一张**卡片**框 |
| `结论：空头占优…　合成分 −25.1 · 置信度 100%` | 双线描边的结论框，`合成分` / `置信度` 各自成行 |
| `结论` 之外的正文长句 | 普通段落，行距 1.55 |

### 数字与方向

- 所有数字包 `<span class="num">`：**加粗**；表格里的数字单元格右对齐并整格加粗；
- 带符号的涨跌前加 **▼ / ▲**（电子墨水没有颜色，符号比红绿更可靠），并把 ASCII 减号
  统一成 `−`；
- 日期与时间不加粗：`2026-11-02`、`10:09:05`、`10 月 30 日` 保持原样，避免整页全是黑块；
- 关键分数（`重点 · 利空 · 100.0 分` 里的分数、结论框里的合成分/置信度）额外放大约 1.15–1.3 倍。

### 表格

- 004 自己的表格（02 行情快照 12 行）保留并强化：表头跨页重复、`tr` 不断页、单元格
  1px 纯黑实线；
- 数字单元格识别为 `class="num"`：右对齐 + 加粗 + ▲▼；
- `—` 这类空值不当数字处理，保持左对齐、不加粗；
- kv 表与数据表样式分离（`table.kv` / `table.data`），不会互相污染。

### 卡片：一条细分 = 一张卡

每个「细分单元」都收进一个 1px 纯黑实线框里，视觉上自成一块，翻页时尽量不拦腰截断：

| 卡片 | 框住什么 | 样式 |
| --- | --- | --- |
| `div.card`（力量条目） | 一条力量的全部内容：`h3` 标题 + 信号行 + `table.kv` + 证据列表 + `▶ AI 总结解读`，**合成一张** | 2px 黑框，`page-break-inside: avoid` |
| `div.card.slim`（要点） | 以加粗词开头的列表（如「三大关注面」）拆成一行一张小卡 | 1px 黑框，行距更紧 |
| `table.data`（行情表） | 行情快照单独成块，不被任何卡吞掉 | 表头跨页重复，1px 网格 |
| `div.verdict`（结论） | 结论框保持独立双线框，不并入力量卡 | 原有样式 |

排版规则：

- 只用 **1px / 2px 实线 + 纯黑**，不上圆角、阴影、渐变（crengine 不支持，电子墨水也糊）；
- 卡与卡之间留 `0.9em` 上边距，标题（黑底横条）不进卡，保证层级不变；
- `page-break-inside: avoid` 让整张卡优先翻到下一页；若某张卡比一屏还高，引擎会自行忽略这条规则，
  真要强制就把它在该卡上改成 `auto`（`tools/build_epub.py` 里 `div.card` 的 CSS 段）。

### 章节与目录

- 标题带**实心底色横条**，和正文硬分隔：`h2` 章节标题是黑底白字并另起一页
  （第一个章节除外，避免空白页），`h3` 力量条目是浅灰底 + 左侧 4px 黑竖条，
  `h4` 小标题是更浅的灰底；
- 每个 `<h2>` 生成锚点（`01 /` → `#s01`）；
- 书首插一页 **目录**（`toc.xhtml`），`toc.ncx` 与 `nav.xhtml` 同步指向各章，KOReader 里
  的「目录」按钮可直接跳转。

## 2. 上传

把 `site/report.epub` 和 `site/epub.json` 传到 `k-macao/04-kindle` 的 `main`
（和之前传图片一样，拖 `site` 文件夹或单独选这两个文件都行）。传完验证：

```
https://raw.githubusercontent.com/k-macao/04-kindle/main/site/epub.json
```

能直接看到 JSON、`doc` 是 `report.epub` 就对了。

## 3. 设备端

插件 0.4.0 起有三档显示模式：`text` / `epub` / `page images`。

菜单：主菜单 → **工具 → 004 Report**

1. **EPUB manifest URL** → 填
   `https://raw.githubusercontent.com/k-macao/04-kindle/main/site/epub.json`；
2. **Display mode → EPUB (styled)**；
3. **Show report**。

也可以直接写 `F:\koreader\settings\octopus004.lua`（退出 KOReader 后再改）：

```lua
return {
    ["display"] = "doc",
    ["doc_url"] = "https://raw.githubusercontent.com/k-macao/04-kindle/main/site/epub.json",
}
```

首次会显示「正在下载」的提示，然后直接跳进阅读器打开这本报告，从第 1 页开始。

## 4. 刷新与翻页怎么走

- 每 60 秒拉一次 `epub.json`（几百字节）：
  - `generated` 没变 → **自动翻一页**（到最后一页后回到第 1 页）；
  - `generated` 变了 → 下载新 EPUB（默认上限 8 MiB），存成一个新的文件名
    `octopus004-report-<n>.epub`，然后重新打开——**新文件没有历史阅读进度，所以总是从
    第 1 页开始**，旧的自动删除。
- 刚打开的那一分钟不会翻页（留给你看首页），之后按 60 秒节奏走。
- 如果你手动打开了别的书，插件不会去翻它（只在当前文档是它自己下载的 EPUB 时才翻页）。
- 点屏幕不再有插件菜单，改用 KOReader 原生操作（左右点/滑动翻页，`Status` 之类在
  KOReader 菜单里）；插件自己的入口仍在 工具 → 004 Report。

## 4.5 自动刷新（GitHub Actions）

`.github/workflows/refresh-epub.yml` 会在仓库里定时跑一遍生成，你不用再手动执行、手动上传：

- **节奏**：cron 每 20 分钟叫醒一次，job 自己限流到 **200 分钟**一次
  （`env.INTERVAL_MINUTES`，想改成 60/360 直接改这里）；间隔不够时只跑几秒就跳过。
- **做什么**：抓 `https://k-macao.github.io/004/` → `tools/build_epub.py` → 只在
  `site/report.epub` / `site/epub.json` 真的变了才提交，提交信息带生成时间。
- **推送前检验源时间**：生成后、提交前，job 会读 `site/epub.json` 的 `data_time`
  （报告正文里的「扫描时间 / 行情日期」），距今超过 `env.MAX_SOURCE_AGE_HOURS`
  （默认 **12 小时**）或缺失时就跳过提交——「文件新」不等于「数据新」，旧数据
  不推给设备。手动触发的 `force` 只无视间隔限流，不会绕过这条新鲜度检查。
- **手动触发**：Actions → *Refresh 004 EPUB* → **Run workflow**，勾 `force` 可以无视间隔。
- **不会被 004 拖累**：004 自己没更新时，抓回来的内容一样，`git diff` 为空 → 不产生提交。
- **前提**：仓库 Settings → Actions → Workflow permissions 需为
  **Read and write permissions**（否则 `git push` 会被拒）。
- **体积**：EPUB 约 60–100 KiB，一次内容变化才提交一次；按 200 分钟节奏、004 每 2–3 小时
  更新算，一天约 0.5 MiB 历史。

设备端无需任何改动：`generated` 一变，插件下一次 60 秒轮询就会下载新版本并从头打开。

## 4.6 数据时间：别把「文件新」当成「数据新」

`epub.json` 里有三个时间，含义完全不同：

| 字段 | 含义 | 谁产生 |
| --- | --- | --- |
| `data_time` | 报告里的**行情时间**（页面上的「扫描时间 / 行情日期」），ISO-8601 UTC | 004 的数据源 |
| `generated` | 这份 EPUB **文件生成**时间 | `build_epub.py` |
| 设备 `Last check` | 插件拉清单的时间 | 插件 |

- 目录页会印一行：`数据时间：2026-11-02 10:09:05 UTC ｜ 文件生成：2026-11-05 10:28 UTC`；
- 插件 0.4.1 起，菜单 **Status** 增加 `Data: 2026-11-02 10:09:05 UTC`，超过 24 小时
  会自动追加 `(stale 3d)`，一眼就能看出「新的是文件，不是行情」；
- 004 的构建挂了（它自己的 Actions 在 `🧪 舆情层自检` 失败）时，`generated` 会变、
  `data_time` 不动 —— 这正是需要区分两者的场景。
- 自动刷新 job 在提交前也会用 `data_time` 做新鲜度闸门：超过 12 小时
  （`env.MAX_SOURCE_AGE_HOURS`）就不推送，见 §4.5。

## 5. 想调排版

不用改脚本——这些都在 KOReader 里调，插件不管：

- 字号 / 行距 / 字体：阅读界面 → 菜单 → **字体** / **行距**；
- 页边距：菜单 → **页面** → 边距；
- 主题/对比度：菜单 → **屏幕** → 夜间模式（想要白底黑字就保持关闭）。

需要**改内容风格**（比如表格线更细、标题更小）时改 `tools/build_epub.py` 里的
`STYLE_CSS` 再重新生成、上传即可。

## 6. 限制

- 单个 EPUB 上限 8 MiB（插件 `DOC_MAX`）；正文 HTML 超过 4 MiB 脚本会拒绝，
  因为老设备打开会卡。
- 抓的是**静态 HTML**：如果 004 改成前端渲染（正文由 JS 生成），`<main>` 会是空的，
  脚本会报「未找到非空的 `<main>` 报告正文」。这时改用截图模式，或让 004 侧在构建时
  把正文写进 HTML。
- 图表、图片如果依赖外链且域名不允许直连，设备上会是空白（正文和表格不受影响）。
- 阅读位置不会跨版本保留：每份新报告都是从第 1 页开始，这是刻意的。
- 回退：Display mode 随时切回 `text`（纯文本，插件自己画）或 `page images`（截图）。
