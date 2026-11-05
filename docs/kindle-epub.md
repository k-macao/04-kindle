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
site/epub.json       {"generated", "doc", "title", "chars", "bytes", "source"}
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
