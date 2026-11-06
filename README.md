# 004 → Kindle（无需 Tesserae Cloud）

通过 GitHub Pages 发布 004 **构建后的报告正文**，KOReader 插件每 60 秒检查一次并自动翻页。两种显示方式：**纯文本**（设备原生排版，轻量）和**页面截图**（真实排版、图表和表格都在，见 [docs/kindle-graphic.md](docs/kindle-graphic.md)）。不依赖 Tesserae 服务端或 Cloud 账号。已有的 Tesserae 插件如启用了自动刷新，请先停止，以免两者抢占屏幕。

> 当前是可安装的初版，尚未在 Kindle Oasis 1 或真实 GitHub Pages 部署上做端到端验证。必须先把下面的生成步骤接入 **004 仓库实际的 Pages 发布流程**；本仓库改动不会自动修改 004。004 自身最短计划约 5 分钟，且 GitHub Actions 可能延迟；一分钟只指设备的检查/翻页间隔，不代表数据每分钟更新。

## 1. 生成并发布报告

需要 Python 3，标准库即可。把本仓库的 `kindle_report.py` 和 `viewer.html` 放在 004 仓库的同一目录中。在 004 的 Pages deploy job 中，**所有正文构建和注入步骤之后、上传 `_site` 之前**加入：

```sh
python3 kindle_report.py report.html --format text -o _site/kindle.txt
```

必须使用构建产物 `report.html`，不是 Git 中的原始模板。原有 Pages 任务发布 `_site` 后，设备默认读取 `https://k-macao.github.io/004/kindle.txt`。该地址根据 004 的 GitHub Pages 配置推定，文件发布前会返回 404；004 README 中还写有 `/03/`，请以实际部署为准。构建失败应阻止发布，避免误以为有新报告；转换器出错时不会覆盖本地旧输出。

也可用公开的 004 构建报告手动生成，再放到任何支持 HTTPS 的静态站点：

```sh
python3 kindle_report.py https://k-macao.github.io/004/ --format text -o kindle.txt
python3 -m unittest discover -s tests -v
```

不要将私密报告发布到公开 Pages。设备通过 HTTPS 请求，要求 KOReader 的 `ca-bundle.crt` 可用；重定向未启用，因此需填写最终的 HTTPS 地址。

### 字模式（EPUB，推荐）

截图再清晰也是图片：放大发虚、每轮刷新下几 MB、字号改不了。字模式把 004 正文清洗成
XHTML，再按它的写作套路重排（力量条目 → 标题 + 信号 + 分数；「归类 / 当次读数」→ 两列表格；
AI 解读 → 引用块；结论 → 结论框；数字加粗、涨跌带 ▲▼；章节带目录锚点），排版按页组织：
行情表格与章节导语（VIX 仪表、核心结论摘要等文字图表）装进卡片，数字统一加粗并加背景填充
突出，正文首行缩进、行距简洁，交给设备上现成的 KOReader 阅读器显示——文字矢量渲染，
字号/行距/边距全用 KOReader 自己的设置调，表格是真排版，文件只有几十 KB：

```sh
python tools/build_epub.py https://k-macao.github.io/004/ --out site   # 只用标准库
```

上传也可以用 `.github/workflows/refresh-epub.yml` 自动完成（每次定时运行（默认每 60 分钟）都会生成并推送一次；目录页第一行是本次更新时间，源数据时间超过 12 小时只给出警告，仍然推送）。
手动上传 `site/report.epub` + `site/epub.json` 时，插件里 **EPUB manifest URL** 指向
`site/epub.json`，**Display mode → EPUB (styled)**。每 60 秒只拉一次几百字节的清单：
没更新就翻页，`generated` 变了才重下 EPUB 并从头打开。详见
[docs/kindle-epub.md](docs/kindle-epub.md)。

### 图形界面模式（页面截图）

纯文本丢排版；要看到和网页一样的版式，用 `tools/render_report.py` 把发布页按设备分辨率截成
每页一张 PNG，并生成 `site/report.json` 清单：

```sh
pip install pillow playwright && python -m playwright install chromium
python tools/render_report.py --out site
```

默认输出是电子墨水友好的 **白底黑字、铺满屏宽**：渲染时注入 CSS 去掉主题色/阴影/窄栏，正文按
`--zoom 1.25` 放大、行距 1.5、表格黑线（`--zoom/--line-height/--margin` 可调），切片后转 8 位
灰阶（`--color` 保留彩色，`--theme auto` 沿用页面原配色）。插件显示时整屏一张图、不显示状态栏，
图片与屏幕尺寸一致就 1:1 绘制，字不缩放所以最锐利；状态改由点屏幕的菜单查看。

上传 `site/`（不要传 `site/full.png`），插件里 **Display mode → Page images**、**Image manifest
URL** 填 `site/report.json` 的地址即可。详步骤、机型参数、切页规则和限制见
[docs/kindle-graphic.md](docs/kindle-graphic.md)。

## 2. Kindle 安装与使用

1. 已越狱的 Kindle 安装并启动 KOReader。将 `octopus004.koplugin` 文件夹完整复制到 `koreader/plugins/`，重启 KOReader。部署方式见 [docs/kindle-ssh.md](docs/kindle-ssh.md)：USB 直连用 `tools/kindle-usb.sh`（Windows 用 `tools/kindle-usb.ps1`；`--install` / `-Install` 直接拷入，无需 SSH），联网用 `tools/deploy-plugin.sh`（KOReader 自带 SSH 服务，默认端口 2222，无需另装 usbnet）。
2. Tools → **004 Report** → 按显示模式填地址：**Report URL**（纯文本）、**EPUB manifest URL**（字模式）或 **Image manifest URL**（截图），再用 **Display mode** 选 text / epub / page images。选择 **Show report** 开始。
3. 每 60 秒检查一次报告并翻到下一页，末页回到首页。点击屏幕可手动翻页、立即刷新、查看状态或停止。停止会恢复原有屏保设置。
4. 有硬件唤醒时尝试使用 KOReader 唤醒管理器；否则保持设备唤醒并使用软件计时。是否支持取决于 Oasis 1 的 KOReader 构建。Wi-Fi 保持开启；一分钟联网和翻页会增加耗电。

请求失败时保留并轮播本地上次成功内容；文本模式顶部显示 `OFFLINE / CACHED`，图片模式整屏无状态栏，
失败会弹一次提示、详情见菜单 `Status`；状态菜单可查看错误和最近成功检查时间。成功检查时间不是源数据时间，报告开头的转换时间也不是源数据时间，数据新鲜度以报告正文中的行情/采集时间为准。KOReader 退出、关机、网络故障或系统休眠等可能中断刷新。屏幕上的内容可能是旧的，不能用于需要实时数据的决策。

## 内容范围

纯文本模式提取 `<main>` 中可见文本并按源顺序排版，表格转成文本；交互按钮、脚本、导航、图片、图表和链接目标不保留，只由网页后续 JavaScript 生成的内容也不会提取。要保留版式、表格和图表，请用图形界面模式（页面截图）：它按设备分辨率渲染真实页面，但同样不能交互。网页正文较长时，一分钟一页可能需要很久才能轮完；实际能否完整显示及中文字体效果需要在设备上验证。文本输出超过 2 MiB 会拒绝生成，避免设备资源耗尽。

### 正文全留，状态类文字不要

纯文本与 EPUB 两种字模式都按同一套规则过滤：**报告正文一律完整保留**（行情表、五大力量与分数、宏观/港股/大宗快讯的标题与摘要、AI 解读、49 个社区源的摘录与量化指标、AI 预测与历史结算、核心结论），只删掉「这台机器这次跑得怎么样」的状态类文字：

- 界面与运行提示：微信推送框（`微信推送 · …`、`点击下方按钮…`、`STATUS > …▌`）、筛选 chips（`49 SOURCE FEEDS`）、`抓取中` / `加载中` / `正在推送` 一类字样；
- 逐源抓取状态：`抓取状态：当次未获取（原因）…`、`当次实抓 · 端点 … · 36 ms · …`、`未获取原因：…`、`当次抓取成功，但时效窗口内没有可核验条目`、`… · 抓取核验 2026-11-06`、证据行 `2026-11-06T11:44:52Z · … 本次匹配 3 条，展示 3 条`、`未提供站内热度指标 · …`；
- 本次构建的汇总与缺口：`11/12 项同步成功 … · 抓取于 … · 核验日期 …`、`宏观快讯 301 条入库 · …`、`数据缺口：…`、`量化平台接口 2/11 可用 · … 9 个接口本次自动降级`、`窗口内无匹配快讯的小节：…`、`本次判定：…`；
- 产物缺失时的构建兜底占位：`今日未获取`、`待构建注入`、`运行 python3 …`、`恢复命令：…`。

「未获取」的源**保留源名**（`❄️ 02. 雪球网`），只去掉状态标签、原因与核验时间，49 源清单因此仍是全的。口径说明与方法论注释属于报告内容，**保留**：`口径：…`、`指标口径：…`、`▶ 词库极性与摘录说明：…`、`注：本节结论…`、`⚠️ 风险提示`、`扫描时间 / 行情日期 / 数据覆盖` 这类时间戳行（源数据时间还要靠它判新鲜度）。规则写在 `tools/build_epub.py` 与 `kindle_report.py` 的 `STATUS_*` 常量里，两边必须逐条一致，`tests/test_status_filter.py` 会比对；EPUB 清单 `site/epub.json` 的 `status_removed` 记录这次删掉多少块。图形界面模式是整页截图，页面上有什么就显示什么，**不做**这层过滤。

## 开发与许可证

`kindle_report.py` 保留 `--format html`（默认）供之前的网页渲染实验使用；设备插件使用 `--format text`。`octopus004.koplugin/wake.lua` 和相关调度思路来自 [dmellok/tesserae-koreader](https://github.com/dmellok/tesserae-koreader)，插件按 AGPL-3.0-or-later 发布，许可证见插件目录。上游插件无需安装或运行。
