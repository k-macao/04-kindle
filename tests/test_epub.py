import datetime as dt
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
<h2>01 / 每日全球全景扫描 (Daily Global Panorama Scan)</h2>
<p>扫描时间 2026-11-02 10:09:05 UTC · 行情日期 2026-11-02 · 数据覆盖 99%</p>
<p>1. 全球风险偏好（美股三大指数对港股的隔夜映射）重点 · 利空 · 100.0 分</p>
<p>归类：市场盘面　当次读数：美股三指均值 −1.92%，三指同向</p>
<p>◆ 三大关注面 · 宏观事件 / 板块轮动</p>
<p>▶ AI 总结解读：美股三指均值 −1.92%；提取信息：标普 7,580.07（−1.82%）；规则判断：重点，方向利空（100.0 分）。</p>
<p>结论：空头占优 · 多单应减仓或对冲　合成分 −25.1 · 置信度 100%</p>
<table><thead><tr><th>市场</th><th>最新</th><th>涨跌幅</th></tr></thead>
<tbody><tr><td>恒生指数</td><td>23,393.32</td><td>+0.42%</td></tr>
<tr><td>恒生科技</td><td>—</td><td>—</td></tr></tbody></table>
<script>evil()</script><button>按钮</button>
<img src="pic.png" class="thumb" onerror="x()"/></main>
<footer>foot</footer></body></html>"""


class ExtractTest(unittest.TestCase):
    def test_only_main_survives(self):
        body = be.extract_main(SAMPLE, "https://k-macao.github.io/004/")
        self.assertIn("<h2", body)
        self.assertIn("<table", body)
        self.assertNotIn("evil", body)
        self.assertNotIn("按钮", body)
        self.assertNotIn("menu", body)
        self.assertNotIn("foot", body)

    def test_links_and_images_become_absolute(self):
        body = be.extract_main(SAMPLE, "https://k-macao.github.io/004/")
        self.assertIn('src="https://k-macao.github.io/004/pic.png"', body)

    def test_source_attributes_are_stripped(self):
        body = be.extract_main(SAMPLE, "https://k-macao.github.io/004/")
        self.assertNotIn("thumb", body)
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


class StructureTest(unittest.TestCase):
    """004's own writing patterns are turned into real layout blocks."""

    def setUp(self):
        self.body = be.extract_main(SAMPLE, "https://k-macao.github.io/004/")

    def test_force_entry_becomes_heading_plus_signal_line(self):
        self.assertIn('<h3 class="force">1　全球风险偏好', self.body)
        self.assertIn('<p class="signal">', self.body)
        self.assertIn('<span class="tag">重点</span>', self.body)
        self.assertIn('<span class="tag">利空</span>', self.body)
        self.assertIn('<span class="score">100.0</span>', self.body)

    def test_field_value_pairs_become_a_table(self):
        self.assertIn('<table class="kv">', self.body)
        self.assertIn("<th>归类</th><td>市场盘面</td>", self.body)
        self.assertIn("<th>当次读数</th><td>美股三指均值", self.body)

    def test_ai_commentary_is_split_into_labelled_paragraphs(self):
        self.assertIn('<div class="ai">', self.body)
        self.assertIn('<p class="ai-label">▶ AI 总结解读</p>', self.body)
        self.assertIn('<span class="label">提取信息：</span>', self.body)
        self.assertIn('<span class="label">规则判断：</span>', self.body)

    def test_diamond_subhead_becomes_h4(self):
        self.assertIn('<h4 class="sub">三大关注面', self.body)

    def test_verdict_is_boxed_with_its_figures(self):
        self.assertIn('<div class="verdict">', self.body)
        self.assertIn("<strong>空头占优", self.body)
        self.assertIn("合成分：", self.body)
        self.assertIn("置信度：", self.body)

    def test_numbers_are_emphasised_and_direction_marked(self):
        self.assertIn('<span class="num">▼−1.92%</span>', self.body)
        self.assertIn('<td class="num">▲+0.42%</td>', self.body)
        self.assertIn('<span class="num">7,580.07</span>', self.body)

    def test_dates_and_times_stay_plain(self):
        body = be.extract_main("<main><p>扫描时间 2026-11-02 10:09:05 UTC，10 月 30 日收盘</p></main>")
        self.assertNotIn("num", body)

    def test_meta_line_with_middle_dots_becomes_a_table(self):
        body = be.extract_main(
            "<main><p>扫描时间 2026-11-02 10:09:05 UTC · 行情日期 2026-11-02 · 数据覆盖 99%</p></main>")
        self.assertIn('<table class="kv">', body)
        self.assertIn("<th>扫描时间</th>", body)
        self.assertIn("<th>数据覆盖</th>", body)

    def test_dates_are_not_mistaken_for_negative_numbers(self):
        body = be.extract_main("<main><p>2026-11-02 收盘</p></main>")
        self.assertNotIn("▼", body)

    def test_data_table_marks_numeric_cells(self):
        self.assertIn('<table class="data">', self.body)
        self.assertIn('<td class="num">23,393.32</td>', self.body)
        self.assertNotIn('<td class="num">—</td>', self.body)

    def test_sections_get_anchors_and_ids(self):
        _, sections, _ = be.build_document(SAMPLE)
        self.assertEqual(sections[0][0], "s01")
        self.assertIn('id="s01"', self.body)

    def test_reprocessing_output_does_not_nest_spans(self):
        again = be.extract_main("<main>" + self.body + "</main>")
        self.assertNotIn('<span class="num"><span class="num">', again)


CARD_SAMPLE = """<main>
<h2>01 / 每日全球全景扫描</h2>
<h4>◆ 推动股价的 5 大力量</h4>
<p>1. 全球风险偏好（美股三指映射）重点 · 利空 · 100.0 分</p>
<p>归类：市场盘面　当次读数：美股三指均值 −1.92%</p>
<ul><li>[10 月 30 日] 标普 7,580.07（−1.82%）</li></ul>
<p>▶ AI 总结解读：三指同向；提取信息：标普；规则判断：重点。</p>
<p>2. 大宗商品与供应链重点 · 利好 · 100.0 分</p>
<p>归类：宏观事件　当次读数：油价均值 −4.39%</p>
<table><thead><tr><th>市场</th><th>最新</th></tr></thead>
<tbody><tr><td>WTI 原油</td><td>99.62</td></tr></tbody></table>
<h4>◆ 三大关注面</h4>
<ul>
<li><strong>宏观事件（利空）</strong>　4 项证据</li>
<li><strong>板块轮动（利空）</strong>　1 项证据</li>
</ul>
</main>"""


class CardTest(unittest.TestCase):
    def setUp(self):
        self.body = be.extract_main(CARD_SAMPLE)

    def _card_span(self, start):
        end = self.body.find("</div>", start)
        return self.body[start:end]

    def test_entry_and_its_blocks_live_in_one_card(self):
        start = self.body.index('<div class="card">')
        card = self._card_span(start)
        for marker in ('<h3 class="force">', 'class="signal"', '<table class="kv">',
                       '<div class="ai">'):
            self.assertIn(marker, card)

    def test_each_entry_gets_its_own_card(self):
        self.assertEqual(self.body.count('<div class="card">'), 2)

    def test_market_table_is_not_swallowed_by_a_card(self):
        first = self.body.index('<div class="card">')
        start = self.body.index('<div class="card">', first + 1)
        card = self._card_span(start)
        self.assertNotIn('<table><thead>', card)
        self.assertIn('<table class="data">', self.body)

    def test_bold_lead_bullets_become_slim_cards(self):
        self.assertEqual(self.body.count('class="card slim"'), 2)
        self.assertNotIn("<li><strong>宏观事件", self.body)

    def test_card_css(self):
        css = be.STYLE_CSS
        self.assertIn("div.card {", css)
        self.assertIn("border: 2px solid #000000", css)
        self.assertIn("page-break-inside: avoid", css)
        self.assertIn("div.card.slim {", css)
        self.assertIn("border-width: 1px", css)


class ChartCardTest(unittest.TestCase):
    """数据表格装进卡片；章节导语装进卡片。"""

    def test_data_table_becomes_a_card_chart(self):
        body = be.extract_main(
            "<main><h2>02 / 行情快照</h2><table><thead><tr><th>市场</th>"
            "<th>最新</th></tr></thead><tbody><tr><td>恒指</td>"
            "<td>24,681.51</td></tr></tbody></table></main>")
        self.assertIn('<div class="card chart">', body)
        self.assertIn('<table class="data">', body)
        # 数据表格仍是独立卡片，不与其它卡片粘连
        self.assertNotIn('<div class="card chart"><div class="card">', body)

    def test_kv_table_stays_out_of_chart_cards(self):
        body = be.extract_main(
            "<main><h2>01 / 全景</h2><p>扫描时间 2026-11-02 10:09:05 UTC</p></main>")
        self.assertNotIn('<div class="card chart">', body)

    def test_section_preamble_becomes_a_lead_card(self):
        body = be.extract_main(
            "<main><h2>00 / 每日 VIX</h2><p>CBOE VIX 28.18 +11.57 / +69.66%</p>"
            "<p>今日判读： 风险定价升温 · 恐慌定价升温</p>"
            "<p>期权市场正在提高未来波动与下行保险的定价</p></main>")
        self.assertIn('<div class="card lead">', body)
        self.assertIn('<h2 id="s00" class="section first">00 / 每日 VIX</h2>',
                      body)

    def test_single_meta_line_is_not_carded(self):
        body = be.extract_main(
            "<main><h2>01 / 全景</h2><p>扫描时间 2026-11-02 10:09:05 UTC</p>"
            "<p>1. 全球风险偏好 重点 · 利空 · 100.0 分</p></main>")
        self.assertNotIn('<div class="card lead">', body)

    def test_lead_runs_stop_at_structured_blocks(self):
        body = be.extract_main(
            "<main><h2>00 / VIX</h2><p>CBOE VIX 28.18</p><p>今日判读：升温</p>"
            "<p>◆ 仪表</p><p>仪表细节</p></main>")
        self.assertIn('<div class="card lead">', body)
        self.assertIn("<h4 class=\"sub\">仪表</h4>", body)

    def test_whitespace_text_nodes_do_not_break_lead_runs(self):
        # 真实报告里块与块之间有换行文本节点；它们只是排版，不能截断导语。
        body = be.extract_main(
            "<main><h2>00 / VIX</h2>\n<p>CBOE VIX 28.18</p>\n"
            "<p>今日判读：升温</p>\n<p>口径：当前 28.18</p>\n</main>")
        self.assertIn('<div class="card lead">', body)


class DataFillTest(unittest.TestCase):
    """数据突出：数字加粗 + 背景填充。"""

    def test_numbers_get_a_background_fill(self):
        css = be.STYLE_CSS
        self.assertIn(".num { font-weight: bold; background: #dcdcdc; }", css)
        self.assertIn("td.num, th.num { text-align: right; font-weight: bold; background: #dcdcdc; }",
                      css)

    def test_score_is_a_black_badge(self):
        css = be.STYLE_CSS
        self.assertIn("span.score {", css)
        self.assertIn("background: #000000", css)
        self.assertIn("color: #ffffff", css)

    def test_table_values_get_a_fill(self):
        css = be.STYLE_CSS
        self.assertIn("table.kv td {", css)
        self.assertIn("background: #efefef", css)
        self.assertIn("table.data th { font-weight: bold; background: #efefef; }", css)

    def test_lead_and_chart_card_css(self):
        css = be.STYLE_CSS
        self.assertIn("div.card.lead {", css)
        self.assertIn("div.card.chart {", css)
        self.assertIn("div.card.chart > table.data { margin: 0; }", css)

    def test_prose_paragraphs_are_indented(self):
        css = be.STYLE_CSS
        self.assertIn("text-indent: 2em", css)
        self.assertIn(
            "p.ai-line, p.verdict-line, p.signal, p.meta, p.ai-label { text-indent: 0; }",
            css)

    def test_line_height_is_concise(self):
        css = be.STYLE_CSS
        self.assertIn("line-height: 1.5", css)


class HeadingBandTest(unittest.TestCase):
    def setUp(self):
        self.body = be.extract_main(SAMPLE)

    def test_sections_are_banded_and_start_a_new_page(self):
        css = be.STYLE_CSS
        self.assertIn("h2.section {", css)
        self.assertIn("background: #000000", css)
        self.assertIn("color: #ffffff", css)
        self.assertIn("page-break-before: always", css)
        # The very first section must not create a blank page.
        self.assertIn("h2.section.first { page-break-before: auto; }", css)

    def test_sub_headings_have_their_own_grey_bands(self):
        css = be.STYLE_CSS
        self.assertIn("h3.force {", css)
        self.assertIn("background: #dcdcdc", css)
        self.assertIn("border-left: 4px solid #000000", css)
        self.assertIn("h4.sub {", css)
        self.assertIn("background: #efefef", css)

    def test_first_heading_carries_the_first_class(self):
        self.assertIn('class="section first"', self.body)
        self.assertEqual(self.body.count('class="section first"'), 1)


class DataTimeTest(unittest.TestCase):
    def test_scan_time_becomes_iso_utc(self):
        body, _, data = be.build_document(SAMPLE)
        self.assertEqual(data, "2026-11-02T10:09:05Z")

    def test_date_only_falls_back_to_midnight(self):
        _, _, data = be.build_document("<main><p>行情日期 2026-11-02</p></main>")
        self.assertEqual(data, "2026-11-02T00:00:00Z")

    def test_missing_timestamp_is_empty(self):
        _, _, data = be.build_document("<main><p>纯文本</p></main>")
        self.assertIsNone(data)


class FreshnessGateTest(unittest.TestCase):
    """推送前的源时间闸门：源数据超过 12 小时就不推。"""

    NOW = dt.datetime(2026, 11, 5, 23, 30, tzinfo=dt.timezone.utc)

    def test_fresh_source_passes(self):
        ok, reason = be.check_data_age("2026-11-05T23:00:00Z", self.NOW, 12)
        self.assertTrue(ok)
        self.assertIn("未超过 12 小时上限", reason)

    def test_exactly_at_limit_passes(self):
        ok, _ = be.check_data_age("2026-11-05T11:30:00Z", self.NOW, 12)
        self.assertTrue(ok)

    def test_one_minute_over_limit_is_blocked(self):
        ok, reason = be.check_data_age("2026-11-05T11:29:00Z", self.NOW, 12)
        self.assertFalse(ok)
        self.assertIn("超过 12 小时上限", reason)

    def test_missing_source_time_is_blocked(self):
        ok, reason = be.check_data_age(None, self.NOW, 12)
        self.assertFalse(ok)
        self.assertIn("源时间", reason)

    def test_unparseable_source_time_is_blocked(self):
        ok, _ = be.check_data_age("昨天下午", self.NOW, 12)
        self.assertFalse(ok)

    def test_offset_timestamp_is_accepted(self):
        ok, _ = be.check_data_age("2026-11-05T23:00:00+00:00", self.NOW, 12)
        self.assertTrue(ok)

    def test_future_source_time_is_not_an_error(self):
        ok, _ = be.check_data_age("2026-11-06T00:00:00Z", self.NOW, 12)
        self.assertTrue(ok)

    def test_default_limit_is_twelve_hours(self):
        ok, reason = be.check_data_age("2026-11-05T11:29:00Z", self.NOW)
        self.assertFalse(ok)
        self.assertIn("12", reason)


class EpubTest(unittest.TestCase):
    def _build(self, tmp):
        body, sections, data_time = be.build_document(SAMPLE)
        path = Path(tmp) / "site" / "report.epub"
        be.write_epub(path, body, title="004 报告", sections=sections,
                      data_time=data_time)
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
                                 "OEBPS/nav.xhtml", "OEBPS/toc.ncx", "OEBPS/toc.xhtml",
                                 "OEBPS/style.css", "OEBPS/report.xhtml"):
                    self.assertIn(required, names)
                opf = zf.read("OEBPS/content.opf").decode("utf-8")
                self.assertIn("report.xhtml", opf)
                self.assertIn("dcterms:modified", opf)
                css = zf.read("OEBPS/style.css").decode("utf-8")
                for rule in ("border: 1px solid #000000", "line-height: 1.5",
                             "table.kv", "div.verdict", ".num"):
                    self.assertIn(rule, css)
                body = zf.read("OEBPS/report.xhtml").decode("utf-8")
                self.assertIn("<table", body)
                self.assertIn("每日全球全景扫描", body)

    def test_toc_links_to_sections(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._build(td)
            with zipfile.ZipFile(path) as zf:
                toc = zf.read("OEBPS/toc.xhtml").decode("utf-8")
                self.assertIn('href="report.xhtml#s01"', toc)
                self.assertIn('class="meta"', toc)
                # 源数据 10:09:05 UTC 显示为北京时间 18:09:05。
                self.assertIn("源数据时间：2026-11-02 18:09:05 北京时间", toc)
                # 第一行是本次更新时间（运行时间），源数据时间排在后面。
                self.assertRegex(toc, r"更新时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} 北京时间")
                self.assertLess(toc.index("更新时间："), toc.index("源数据时间："))
                nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
                self.assertIn('href="report.xhtml#s01"', nav)
                ncx = zf.read("OEBPS/toc.ncx").decode("utf-8")
                self.assertIn('src="report.xhtml#s01"', ncx)

    def test_every_xml_part_is_well_formed(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._build(td)
            with zipfile.ZipFile(path) as zf:
                for name in ("META-INF/container.xml", "OEBPS/content.opf",
                             "OEBPS/nav.xhtml", "OEBPS/toc.ncx", "OEBPS/toc.xhtml",
                             "OEBPS/report.xhtml"):
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
            self.assertEqual(data["sections"], 1)
            self.assertEqual(data["data_time"], "2026-11-02T10:09:05Z")
            self.assertGreater(data["chars"], 0)
            self.assertGreater(data["bytes"], 500)
            self.assertTrue((out / "report.epub").is_file())


if __name__ == "__main__":
    unittest.main()
