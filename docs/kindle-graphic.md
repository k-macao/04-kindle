# 图形界面模式：把 004 发布页按屏幕尺寸渲染成图片

纯文本模式（`kindle_report.py` + `kindle.txt`）只保留正文文字，表格变成一行行字、徽章和图
表全丢。这个模式改成**把真实页面按设备分辨率截图**，插件按页显示，60 秒轮询刷新——设备上
看到的是和网页一样的排版，代价是不能交互、页数多时刷新变慢。

整条链路只在**本仓库**里完成，不需要改 004 的任何代码或 CI。

## 原理

```
004 发布页 (https://k-macao.github.io/004/)
   │  tools/render_report.py  （本机或 CI，headless Chromium 截图 + Pillow 切片）
   ▼
site/report.json + site/pages/page-000.png … page-NNN.png
   │  上传到本仓库 main（或任何 HTTPS 静态位置，要求不跳转）
   ▼
octopus004.koplugin（Display mode = page images）
   │  每 60 秒拉 report.json，generated 变了才重新下载图片
   ▼
Kindle 屏幕：一页一张图，自动翻页
```

清单格式（`report.json`，插件按它工作）：

```json
{
  "generated": "2026-11-05T02:11:00+00:00",
  "source": "https://k-macao.github.io/004/",
  "width": 1072,
  "height": 1448,
  "count": 12,
  "pages": ["pages/page-000.png", "pages/page-001.png"]
}
```

`pages` 可以是相对路径（相对 `report.json` 的地址解析），也可以是完整的 HTTPS 地址。

## 1. 生成（本机 Windows / macOS / Linux）

一次性依赖：

```sh
pip install pillow playwright
python -m playwright install chromium
```

渲染 + 切片：

```sh
python tools/render_report.py --out site
```

默认参数就是 Kindle Oasis 1：`--width 1072 --height 1448`，源页面
`https://k-macao.github.io/004/`。输出：

```
site/report.json
site/pages/page-000.png …     每张正好 1072×1448
site/full.png                 整页截图，仅用于排查，别上传（很大）
```

常用选项：

| 选项 | 作用 |
| --- | --- |
| `--url URL` | 换源页面（默认 004 发布页） |
| `--width / --height` | 设备分辨率；KOA1/PW3/PW4 = 1072×1448，KOA2/3 = 1264×1680，PW5 = 1236×1648，基础款 = 600×800。以 `;711` 或 KOReader 帮助→关于 为准 |
| `--from-png full.png` | 已有整页截图，跳过浏览器（截图宽度不等于 `--width` 会自动缩放并提示） |
| `--max-pages N` | 页数上限，默认 60（与插件一致），超了直接报错 |
| `--wait 5` | 渲染后等待秒数，页面加载慢时加大 |
| `--keep-full` | 显式保留 `site/full.png` |

切页规则：脚本会找**空白行**下刀，尽量不切断文字行；末尾的空白会被裁掉，最后一页不够高
时用纸色补齐，所以每张图都是同样尺寸，插件不需要缩放计算。

没有 Playwright 也不影响使用：用浏览器开发者工具（F12 → 设备工具栏 → **Capture full size
screenshot**）或任意长截图工具存一张 `full.png`，再跑：

```sh
python tools/render_report.py --from-png full.png --out site
```

只用 Pillow（切片）也能出图。

## 2. 上传

最省事：GitHub 网页 → 本仓库 → **Add file → Upload files**，把 `site` 整个目录拖进去
（保持 `site/report.json` 与 `site/pages/…` 的相对结构），提交到 `main`。命令行同理：

```sh
git add site/report.json site/pages
git commit -m "更新 004 页面截图"
git push
```

上传后确认这个地址能直接打开（不跳转、纯 JSON）：

```
https://raw.githubusercontent.com/k-macao/04-kindle/main/site/report.json
```

> 不要上传 `site/full.png`（十几 MB，且没用到）。仓库里已把它 ignore。

## 3. 设备端

### 方式 A：菜单里改

1. 工具 → **004 Report** → **Display mode** → **Page images**；
2. 同一个菜单 → **Image manifest URL** → 填上面的 `report.json` 地址；
3. **Show report**。

### 方式 B：直接写设置（不想在墨水屏上打长网址）

退出 KOReader，插 USB，编辑 `F:\koreader\settings\octopus004.lua`：

```lua
return {
    ["display"] = "image",
    ["image_url"] = "https://raw.githubusercontent.com/k-macao/04-kindle/main/site/report.json",
}
```

UTF-8 无 BOM，拔线后重新打开 KOReader → 004 Report → Show report。原有 `enabled` 等设置不必
手写，插件会补默认值。

启动时会显示 `004 | loading 3/12`，下完切到第 1 页；顶部状态行 `004 | ONLINE | 时间 | tap`。
点屏幕弹出菜单（下一页 / 立即刷新 / 显示模式 / 状态 / 停止）。

## 4. 更新

改了 004 之后重新跑一次第 1 步、覆盖 `site/` 里的文件即可。设备每 60 秒拉一次
`report.json`，发现 `generated` 变了才重新下载所有图片；`generated` 没变就继续翻页。

## 限制与注意事项

- **页数是真实成本**：004 页面很长，按 1072×1448 切可能有十几到几十页，每页 200–600 KB，
  一轮刷新要下几 MB。慢网/弱信号时可以调大 `--width/--height`（等于缩小字、减少页数），
  或在 004 侧用 `?section=` 之类的入口只截一部分。
- **插件硬上限**：单页 4 MiB、总 64 MiB、最多 60 页；`render_report.py` 默认也按 60 页报错。
- **静态快照**：截图里没有交互，也没有网页动画；数字新鲜度看页面上的行情日期。
- **HTTPS 与证书**：和文本模式一样，要求 `koreader/ca-bundle.crt` 存在，且地址不能 302
  （raw.githubusercontent 是直连的，gist 的 `/raw` 短地址会跳转，别用）。
- **缓存**：图片在 `/mnt/us/koreader/octopus004-pages/`，清单签名记在
  `/mnt/us/koreader/settings/octopus004.lua`；离线时继续播本地已有的页。
- **回退**：把 Display mode 切回 **text**、Report URL 填 `kindle.txt` 的地址即可，两套缓存
  互不干扰。
