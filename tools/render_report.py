#!/usr/bin/env python3
"""Render the published 004 page into Kindle-sized page images.

The KOReader plugin has two display modes: the plain-text report
(kindle_report.py) and this one, which shows the real page as a stack of
screenshots, so tables, badges and layout survive.

Output layout (everything relative to --out):

    report.json          manifest consumed by the plugin
    pages/000.png ...    one image per screen, exactly WIDTHxHEIGHT

Host the folder anywhere HTTPS reachable without redirects (raw.githubusercontent
of a public repo is the simplest) and point the plugin's "Image manifest URL" at
report.json.

Rendering needs Playwright (headless Chromium):

    pip install playwright
    python -m playwright install chromium

Slicing needs Pillow:

    pip install pillow

If you already have a full-page screenshot, skip the browser entirely:

    python tools/render_report.py --from-png full.png --out site
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Oasis 1 (KOA1) is 1072x1448; adjust per device if needed.
DEFAULT_WIDTH = 1072
DEFAULT_HEIGHT = 1448
# Look this far above a page boundary for a blank row to cut on.
CUT_WINDOW = 90
# Rows whose ink is below this fraction of a page row count as blank.
BLANK_ROW_LIMIT = 0.004
# Keep a little whitespace after the last line of content.
BOTTOM_MARGIN = 16


def _require_pillow():
    try:
        from PIL import Image  # noqa: F401
    except ImportError:  # pragma: no cover - environment dependent
        sys.exit(
            "缺少 Pillow：先运行 `pip install pillow`，"
            "或用 --from-png 提供已经截好的整页图片。"
        )
    from PIL import Image

    return Image


def background_color(img):
    """Most common colour along the page border, i.e. the paper colour."""
    w, h = img.size
    pixels = []
    for x in range(0, w, max(1, w // 40)):
        pixels.append(img.getpixel((x, 0)))
        pixels.append(img.getpixel((x, h - 1)))
    for y in range(0, h, max(1, h // 40)):
        pixels.append(img.getpixel((0, y)))
        pixels.append(img.getpixel((w - 1, y)))
    counts = {}
    for px in pixels:
        if isinstance(px, tuple):
            px = px[:3]
        counts[px] = counts.get(px, 0) + 1
    return max(counts, key=counts.get)


def _resample(Image, name):
    """Pillow 10 moved the resampling constants under Image.Resampling."""
    resampling = getattr(Image, "Resampling", Image)
    return getattr(resampling, name)


def ink_by_row(img, bg=None):
    """Per-row ink amount in 0..1 (fraction of non-background pixels)."""
    Image = _require_pillow()
    if bg is None:
        bg = background_color(img)
    rgb = img.convert("RGB")
    diff = Image.new("RGB", rgb.size, bg)
    from PIL import ImageChops

    delta = ImageChops.difference(rgb, diff).convert("L")
    mask = delta.point(lambda v: 255 if v > 18 else 0)
    # Collapse to one pixel per row: the mean is the fraction of ink pixels.
    column = mask.resize((1, rgb.size[1]), _resample(Image, "BOX"))
    return [column.getpixel((0, y)) / 255.0 for y in range(rgb.size[1])]


def content_bottom(ink, limit=BLANK_ROW_LIMIT):
    """Index one past the last row that still has visible content."""
    for y in range(len(ink) - 1, -1, -1):
        if ink[y] > limit:
            return y + 1
    return 0


def cut_points(ink, page_height, total_height, window=CUT_WINDOW):
    """Pick page boundaries, preferring blank rows so nothing is cut in half."""
    cuts = []
    start = 0
    while total_height - start > page_height:
        target = start + page_height
        low = max(start + max(1, page_height // 2), target - window)
        if low > target:
            low = target
        best = None
        for y in range(low, target + 1):
            score = ink[y]
            if best is None or score <= best[1]:
                best = (y, score)
        cut = best[0] if best else target
        cuts.append(cut)
        start = cut
    cuts.append(total_height)
    return cuts


def build_pages(img, page_height, width=None, window=CUT_WINDOW, margin=BOTTOM_MARGIN,
                limit=BLANK_ROW_LIMIT):
    """Slice a tall screenshot into page-sized images, padding the last one."""
    Image = _require_pillow()
    if width is None:
        width = img.size[0]
    ink = ink_by_row(img)
    bottom = content_bottom(ink, limit)
    if bottom == 0:
        raise ValueError("整张图都是空白，检查截图是否成功")
    bottom = min(img.size[1], bottom + margin)
    ink = ink[:bottom]
    cuts = cut_points(ink, page_height, bottom, window)
    bg = background_color(img)
    pages = []
    start = 0
    for end in cuts:
        page = img.crop((0, start, width, min(end, bottom)))
        if page.size[1] < page_height:
            canvas = Image.new(img.mode, (width, page_height), bg)
            canvas.paste(page, (0, 0))
            page = canvas
        pages.append(page)
        start = end
    return pages


def render_with_playwright(url, width, height, wait_seconds, scroll):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "缺少 Playwright。用下面两条命令装好再跑（或用 --from-png 提供截图）：\n"
            "    pip install playwright\n"
            "    python -m playwright install chromium"
        )
    import io

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(url, wait_until="load", timeout=60000)
        if scroll:
            # Trigger lazy-loaded sections, then return to the top.
            page.evaluate(
                "() => { let y = 0; const step = () => { window.scrollTo(0, y); y += 600;"
                " if (y < document.body.scrollHeight) setTimeout(step, 30); }; step(); }"
            )
        page.wait_for_timeout(int(wait_seconds * 1000))
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        data = page.screenshot(full_page=True)
        browser.close()
    Image = _require_pillow()
    return Image.open(io.BytesIO(data))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--url", default="https://k-macao.github.io/004/",
                     help="要渲染的公开页面（默认 004 发布页）")
    src.add_argument("--from-png", help="已有的整页截图，跳过浏览器")
    ap.add_argument("--out", required=True, help="输出目录，例如 site")
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="页宽（默认 KOA1 1072）")
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="页高（默认 KOA1 1448）")
    ap.add_argument("--wait", type=float, default=3.0, help="渲染后等待秒数（默认 3）")
    ap.add_argument("--max-pages", type=int, default=60,
                    help="页数上限，超过就报错（默认 60，与插件一致）")
    ap.add_argument("--keep-full", action="store_true", help="保留整页截图 site/full.png")
    ap.add_argument("--no-scroll", action="store_true", help="渲染前不做滚动触发")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    Image = _require_pillow()

    if args.from_png:
        img = Image.open(args.from_png).convert("RGB")
        if img.size[0] != args.width:
            ratio = args.width / img.size[0]
            print(f"警告：截图宽 {img.size[0]}px，缩放到 {args.width}px（比例 {ratio:.3f}）")
            img = img.resize((args.width, max(1, round(img.size[1] * ratio))), _resample(Image, "LANCZOS"))
        source = args.from_png
    else:
        print(f"渲染 {args.url} …")
        img = render_with_playwright(args.url, args.width, args.height,
                                     args.wait, not args.no_scroll)
        source = args.url

    if img.size[0] != args.width:
        img = img.resize((args.width, max(1, round(img.size[1] * args.width / img.size[0]))),
                         _resample(Image, "LANCZOS"))

    pages = build_pages(img, args.height, width=args.width)
    if len(pages) > args.max_pages:
        sys.exit(f"需要 {len(pages)} 页，超过上限 {args.max_pages}；"
                 f"可减小 --width 或放宽 --max-pages。")

    out = Path(args.out)
    page_dir = out / "pages"
    page_dir.mkdir(parents=True, exist_ok=True)
    # Drop stale pages from a previous render.
    for old in page_dir.glob("page-*.png"):
        old.unlink()

    total = 0
    for index, page in enumerate(pages):
        target = page_dir / f"page-{index:03d}.png"
        page.save(target, optimize=True)
        total += target.stat().st_size

    if args.keep_full or args.from_png is None:
        img.save(out / "full.png", optimize=True)

    manifest = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "width": args.width,
        "height": args.height,
        "count": len(pages),
        "pages": [f"pages/page-{i:03d}.png" for i in range(len(pages))],
    }
    (out / "report.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    size = f"{total / 1024 / 1024:.1f} MiB" if total >= 1024 * 1024 else f"{total / 1024:.0f} KiB"
    print(f"完成：{len(pages)} 页，共 {size}，页面 {args.width}x{args.height}")
    print(f"清单：{out / 'report.json'}")
    print("上传后把插件 Display mode 设为 page images，并把 Image manifest URL 指向 report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
