import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import build_epub as be

SAMPLE = """<html><body><nav>menu</nav><main>
<h2>今日要闻</h2><script>evil()</script>
<p>正文 <b>加粗</b> <a href="/x">链接</a></p>
<table><thead><tr><th>项目</th><th>值</th></tr></thead>
<tbody><tr><td>热度</td><td>92</td></tr></tbody></table>
<button>按钮</button><img src="pic.png" class="thumb" onerror="x()"/></main>
<footer>foot</footer></body></html>"""


class ExtractTest(unittest.TestCase):
    def test_only_main_survives(self):
        body = be.extract_main(SAMPLE, "https://k-macao.github.io/004/")
        self.assertIn("<h2>", body)
        self.assertIn("<table>", body)
        self.assertNotIn("evil", body)
        self.assertNotIn("按钮", body)
        self.assertNotIn("menu", body)
        self.assertNotIn("foot", body)

    def test_links_and_images_become_absolute(self):
        body = be.extract_main(SAMPLE, "https://k-macao.github.io/004/")
        self.assertIn('href="https://k-macao.github.io/x"', body)
        self.assertIn('src="https://k-macao.github.io/004/pic.png"', body)

    def test_unsafe_attributes_are_stripped(self):
        body = be.extract_main(SAMPLE, "https://k-macao.github.io/004/")
        self.assertNotIn("class=", body)
        self.assertNotIn("onerror", body)

    def test_unclosed_paragraph_is_closed_before_blocks(self):
        body = be.extract_main("<main><p>开头<ul><li>一</li></ul>")
        self.assertRegex(body, r"<p>开头\s*</p><ul>")

    def test_table_cells_and_rows_close_each_other(self):
        body = be.extract_main("<main><table><tr><td>a<td>b<tr><td>c</table></main>")
        self.assertEqual(body.count("<tr>"), 2)
        self.assertNotIn("<td>a<td>", body)

    def test_placeholders_are_rejected(self):
        with self.assertRaises(ValueError):
            be.extract_main("<main><p>{{TITLE}}</p></main>")

    def test_missing_main_is_rejected(self):
        with self.assertRaises(ValueError):
            be.extract_main("<html><body><p>no main</p></body></html>")


class EpubTest(unittest.TestCase):
    def _build(self, tmp):
        body = be.extract_main(SAMPLE, "")
        path = Path(tmp) / "site" / "report.epub"
        be.write_epub(path, body, title="004 报告")
        return path

    def test_mimetype_is_first_and_stored(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._build(td)
            with zipfile.ZipFile(path) as zf:
                first = zf.infolist()[0]
                self.assertEqual(first.filename, "mimetype")
                self.assertEqual(first.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(zf.read("mimetype"), b"application/epub+zip")

    def test_package_has_required_parts(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._build(td)
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                for required in ("META-INF/container.xml", "OEBPS/content.opf",
                                 "OEBPS/nav.xhtml", "OEBPS/toc.ncx",
                                 "OEBPS/style.css", "OEBPS/report.xhtml"):
                    self.assertIn(required, names)
                opf = zf.read("OEBPS/content.opf").decode("utf-8")
                self.assertIn("report.xhtml", opf)
                self.assertIn("dcterms:modified", opf)
                css = zf.read("OEBPS/style.css").decode("utf-8")
                self.assertIn("border: 1px solid #000000", css)
                self.assertIn("line-height: 1.55", css)
                body = zf.read("OEBPS/report.xhtml").decode("utf-8")
                self.assertIn("<table>", body)
                self.assertIn("今日要闻", body)

    def test_every_xml_part_is_well_formed(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._build(td)
            with zipfile.ZipFile(path) as zf:
                for name in ("META-INF/container.xml", "OEBPS/content.opf",
                             "OEBPS/nav.xhtml", "OEBPS/toc.ncx", "OEBPS/report.xhtml"):
                    with self.subTest(part=name):
                        ET.fromstring(zf.read(name))  # raises on malformed XML

    def test_manifest_written_by_main(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "report.html"
            src.write_text(SAMPLE, encoding="utf-8")
            out = Path(td) / "site"
            self.assertEqual(be.main([str(src), "--out", str(out)]), 0)
            data = json.loads((out / "epub.json").read_text(encoding="utf-8"))
            self.assertEqual(data["doc"], "report.epub")
            self.assertGreater(data["chars"], 0)
            self.assertGreater(data["bytes"], 500)
            self.assertTrue((out / "report.epub").is_file())


if __name__ == "__main__":
    unittest.main()
