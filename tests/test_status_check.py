"""tools/check_no_status.py：产物自检必须按「整行」判，别把说明句当残留。"""

import contextlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import build_epub as be  # noqa: E402
import check_no_status as cs  # noqa: E402
import kindle_report as k  # noqa: E402

CLEAN_BODY = (
    '<h2 id="s01" class="section">01 / 每日全球全景扫描</h2>'
    "<p>恒指 24,481.86（−1.27%）</p>"
    # 说明句里出现「抓取状态」四个字是内容，不算残留——CI 曾经就是被这个误判挡住。
    "<p>逐源显示当次抓取状态与条目发布日期；抓不到的源标「未获取」。</p>"
)
DIRTY_BODY = CLEAN_BODY + "<p>抓取状态：当次未获取（robots.txt 不允许抓取）</p>"


def write_epub(path, body):
    be.write_epub(path, body, title="004 报告", sections=[("s01", "01 / 全景")],
                  data_time="2026-11-06T13:32:10Z")


class XhtmlLinesTest(unittest.TestCase):
    def test_block_boundaries_become_lines(self):
        lines = cs.xhtml_lines("<div>甲</div><p>乙</p><table><tr><td>丙</td><td>丁</td></tr></table>")
        # 每个单元前都加 | 分隔，行首那个也留着——和纯文本模式的表格渲染同口径。
        self.assertEqual(lines, ["甲", "乙", "| 丙 | 丁"])

    def test_note_sentence_is_not_a_residue(self):
        lines = cs.xhtml_lines(CLEAN_BODY)
        self.assertTrue(any("抓取状态" in line for line in lines))
        self.assertEqual(cs.residues(lines), [])

    def test_whole_status_line_is_a_residue(self):
        lines = cs.xhtml_lines(DIRTY_BODY)
        self.assertEqual(len(cs.residues(lines)), 1)
        self.assertIn("抓取状态：", cs.residues(lines)[0])


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.epub = self.dir / "report.epub"
        self.text = self.dir / "kindle.txt"
        write_epub(self.epub, CLEAN_BODY)
        self.text.write_text("\n".join(cs.xhtml_lines(CLEAN_BODY) * 80), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, **kwargs):
        argv = ["--text", str(self.text), "--epub", str(self.epub),
                "--min-lines", "10", "--min-chars", "10"]
        for key, value in kwargs.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        return cs.main(argv)

    def test_clean_artifacts_pass(self):
        self.assertEqual(self._run(), 0)

    def test_status_line_in_epub_fails(self):
        write_epub(self.epub, DIRTY_BODY)
        self.assertEqual(self._run(), 1)

    def test_status_line_in_text_fails(self):
        self.text.write_text("恒指 24,481.86\n抓取状态：当次未获取（robots.txt 不允许抓取）\n",
                             encoding="utf-8")
        self.assertEqual(self._run(), 1)

    def test_too_few_lines_fails(self):
        self.text.write_text("恒指 24,481.86\n", encoding="utf-8")
        self.assertEqual(self._run(min_lines=200), 1)

    def test_too_few_chars_fails(self):
        self.assertEqual(self._run(min_chars=100000), 1)

    def test_missing_files_fail(self):
        self.assertEqual(cs.main(["--text", str(self.dir / "nope.txt"),
                                  "--epub", str(self.dir / "nope.epub"),
                                  "--min-lines", "1", "--min-chars", "1"]), 1)

    def test_manifest_numbers_are_reported(self):
        manifest = self.dir / "epub.json"
        manifest.write_text('{"generated": "2026-11-06T15:00:00+00:00", "sections": 9,'
                            ' "status_removed": 154, "data_time": "2026-11-06T13:32:10Z"}',
                            encoding="utf-8")
        errors, summary = cs.check(self.text, self.epub, manifest, 10, 10)
        self.assertEqual(errors, [])
        self.assertEqual(summary["status_removed"], 154)
        self.assertEqual(summary["sections"], 9)


class GeneratedArtifactsTest(unittest.TestCase):
    """用真的生成器跑一遍：过滤后的产物必须过自检。"""

    def test_filtered_output_passes_self_check(self):
        page = (
            "<html><body><main>"
            "<h2>01 / 每日全球全景扫描</h2>"
            "<p>扫描时间 2026-11-06 13:32:10 UTC · 行情日期 2026-11-06</p>"
            '<div class="push-wrap"><div>微信推送 · 一对一专属单页完整直推</div>'
            '<div class="terminal-log-box" id="pushStatus">STATUS &gt; 已就绪</div></div>'
            "<p>恒指 24,481.86（−1.27%）</p>"
            '<div class="pub-meta">11/12 项同步成功 · 抓取于 2026-11-06 13:30:00 UTC</div>'
            "<article><div><span>❄️ 02. 雪球网</span><span>未获取</span></div>"
            "<p>抓取状态：当次未获取（robots.txt 不允许抓取）</p>"
            '<div>当次未获取（robots.txt 不允许抓取）· 中文投资者社区 · 抓取核验 2026-11-06</div>'
            "</article>"
            "<p>口径：力量分 = 45% 幅度 + 30% 印证度。</p>"
            "<h2>07 / 核心结论</h2><p>空头占优</p>"
            "</main></body></html>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            src = out / "report.html"
            src.write_text(page, encoding="utf-8")
            # EPUB 走 build_epub 的 CLI，纯文本走 kindle_report 的 CLI：
            # 两个产物都是「生成器真实输出」，不是手工拼的。
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(be.main([str(src), "--out", str(out)]), 0)
            argv = sys.argv
            sys.argv = ["kindle_report.py", str(src), "--format", "text",
                        "-o", str(out / "kindle.txt")]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(k.main(), 0)
            finally:
                sys.argv = argv
            body = zipfile.ZipFile(out / "report.epub").read("OEBPS/report.xhtml").decode("utf-8")
            self.assertNotIn("抓取状态", body)
            self.assertIn("雪球网", body)
            self.assertIn("口径", body)
            self.assertEqual(cs.main(["--text", str(out / "kindle.txt"),
                                      "--epub", str(out / "report.epub"),
                                      "--min-lines", "3", "--min-chars", "3"]), 0)


if __name__ == "__main__":
    unittest.main()
