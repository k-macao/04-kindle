import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

try:
    from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    HAVE_PIL = True
except ImportError:  # Pillow is only needed for the image renderer
    HAVE_PIL = False

import render_report as rr


def fake_page(width=400, height=4000, line_height=20, gap=40, first=100, ink=None):
    """White page with black text-like bars; `ink` limits where bars are drawn."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    end = height if ink is None else ink
    y = first
    while y + line_height < end:
        draw.rectangle((20, y, width - 20, y + line_height), fill=(0, 0, 0))
        y += line_height + gap
    return img


class ThemeTest(unittest.TestCase):
    """The device is e-ink: force black ink on white paper, nothing else."""

    def test_light_theme_is_black_on_white(self):
        css = rr.THEME_CSS
        self.assertIn("#ffffff", css)
        self.assertIn("#000000", css)
        self.assertIn("color-scheme: light", css)
        # Coloured backgrounds would turn into dithered grey mush.
        self.assertIn("background-image: none", css)

    def test_bleed_css_removes_narrow_column(self):
        self.assertIn("max-width: none", rr.BLEED_CSS)
        self.assertIn("padding: 0", rr.BLEED_CSS)

    def test_defaults_are_light_gray_full_bleed(self):
        args = rr.parse_args(["--out", "site"])
        self.assertEqual(args.theme, "light")
        self.assertFalse(args.color)
        self.assertFalse(args.no_bleed)


@unittest.skipUnless(HAVE_PIL, "需要 Pillow 才能测试图片切片")
class SliceTest(unittest.TestCase):
    def test_pages_have_exact_height(self):
        img = fake_page()
        pages = rr.build_pages(img, page_height=1000, width=400)
        self.assertGreater(len(pages), 2)
        for page in pages:
            self.assertEqual(page.size, (400, 1000))

    def test_cut_points_land_on_blank_rows(self):
        img = fake_page()
        ink = rr.ink_by_row(img)
        bottom = rr.content_bottom(ink) + rr.BOTTOM_MARGIN
        cuts = rr.cut_points(ink[:bottom], 1000, bottom)
        self.assertGreater(len(cuts), 2)
        for cut in cuts[:-1]:
            # Nothing is sliced in half: the boundary row has no ink.
            self.assertEqual(ink[cut], 0.0, f"第 {cut} 行切断了内容")

    def test_blank_tail_is_cropped(self):
        img = fake_page(height=3000, ink=1500)
        ink = rr.ink_by_row(img)
        bottom = rr.content_bottom(ink)
        self.assertLess(bottom, 1600)
        self.assertGreater(bottom, 1400)
        pages = rr.build_pages(img, page_height=1000, width=400)
        self.assertEqual(len(pages), 2)

    def test_content_bottom_on_blank_image(self):
        blank = Image.new("RGB", (100, 500), (255, 255, 255))
        self.assertEqual(rr.content_bottom(rr.ink_by_row(blank)), 0)
        with self.assertRaises(ValueError):
            rr.build_pages(blank, page_height=200)

    def test_short_content_becomes_one_padded_page(self):
        img = fake_page(height=600, ink=500)
        pages = rr.build_pages(img, page_height=1000, width=400)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].size, (400, 1000))

    def test_manifest_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "full.png"
            fake_page(height=2600).save(src)
            out = Path(tmp) / "site"
            code = rr.main(["--from-png", str(src), "--out", str(out),
                            "--width", "400", "--height", "1000"])
            self.assertEqual(code, 0)
            manifest = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["width"], 400)
            self.assertEqual(manifest["height"], 1000)
            self.assertEqual(manifest["count"], len(manifest["pages"]))
            self.assertGreaterEqual(manifest["count"], 2)
            for rel in manifest["pages"]:
                page = out / rel
                self.assertTrue(page.is_file(), rel)
                self.assertEqual(Image.open(page).size, (400, 1000))

    def test_page_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "full.png"
            fake_page(height=5000).save(src)
            out = Path(tmp) / "site"
            with self.assertRaises(SystemExit):
                rr.main(["--from-png", str(src), "--out", str(out),
                         "--width", "400", "--height", "400", "--max-pages", "2"])


if __name__ == "__main__":
    unittest.main()

    def test_cli_writes_grayscale_pages(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "full.png"
            fake_page(width=400, height=2200).save(src)
            out = Path(td) / "site"
            rr.main(["--from-png", str(src), "--out", str(out),
                     "--width", "400", "--height", "1000"])
            manifest = json.loads((out / "report.json").read_text(encoding="utf-8"))
            page = Image.open(out / manifest["pages"][0])
            self.assertEqual(page.mode, "L")
            self.assertEqual(page.size, (400, 1000))

    def test_color_flag_keeps_rgb(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "full.png"
            fake_page(width=400, height=2200).save(src)
            out = Path(td) / "site"
            rr.main(["--from-png", str(src), "--out", str(out), "--color",
                     "--width", "400", "--height", "1000"])
            manifest = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(Image.open(out / manifest["pages"][0]).mode, "RGB")
