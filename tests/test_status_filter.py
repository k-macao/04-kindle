"""状态类文字过滤：正文全部内容都留，只删「这次跑得怎么样」的话。

删：抓取/加载/推送状态、终端状态行、端点与耗时、抓取核验时间、同步/降级/缺口计数、
筛选 chips、构建兜底占位。
留：行情与力量条目、快讯标题与摘要、AI 解读、社区摘录、预测、结论，以及口径说明与
方法论注释（「口径：…」「▶ 词库极性与摘录说明：…」「注：…」「⚠️ 风险提示」）。

两个转换器各带一份同样的规则：`tools/build_epub.py`（EPUB 字模式）与
`kindle_report.py`（纯文本，要单独拷进 004 仓库，不能 import 本仓库的模块）。
所以这里除了测行为，还逐条比对两边规则是否一致。
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import build_epub as be  # noqa: E402
import kindle_report as k  # noqa: E402

PAGE = """<html><body><nav>菜单</nav><main>
<h2>01 / 每日全球全景扫描 (Daily Global Panorama Scan)</h2>
<p>扫描时间 2026-11-06 13:32:10 UTC · 行情日期 2026-11-06 · 数据覆盖 99%</p>
<div class="push-wrap">
  <div class="push-title">微信推送 · 一对一专属单页完整直推 (Token 本人)</div>
  <p class="push-tip">点击下方按钮，将本报告推送给 Token 对应的微信本人。</p>
  <button class="pixel-push-btn" id="pushBtn">▶ 立即推送微信单页完整版</button>
  <div class="terminal-log-box" id="pushStatus">STATUS &gt; 一对一专属推送已就绪<span>▌</span></div>
</div>
<div class="filter-row">
  <button class="filter-tab">全部 49 平台</button>
  <span class="filter-result" id="filterResult"><i></i>49 SOURCE FEEDS</span>
</div>
<div class="pub-meta">11/12 项同步成功，恒生科技指数 暂缺（源不可达，已降级显示 —）·
  抓取于 2026-11-06 13:30:00 UTC · 核验日期 2026-11-06</div>
<div class="pub-meta">宏观快讯 301 条入库 · 时效窗口 7 天 · 抓取于 2026-11-06 13:32:10 UTC</div>
<div class="pub-meta">口径：力量分 = 45% 幅度 + 30% 印证度 + 25% 时效。</div>
<table class="quote-table"><tr><th>市场</th><th>最新</th></tr>
<tr><td>恒生指数</td><td>24,481.86</td></tr></table>
<article class="pub-card" data-verdict="unavailable">
  <div class="pub-card-head"><span class="pub-name">❄️ 02. 雪球网</span><span class="pub-chip">未获取</span></div>
  <p class="pub-quote"><strong>抓取状态：</strong>当次未获取（robots.txt 不允许抓取）——
    本栏仅展示实盘抓到的公开讨论片段。</p>
  <div class="pub-meta">当次未获取（robots.txt 不允许抓取）· 中文投资者社区 · 抓取核验 2026-11-06</div>
</article>
<article class="pub-card" data-verdict="neutral">
  <div class="pub-card-head"><span class="pub-name">🐮 01. 富途牛牛社区</span><span class="pub-chip">中性</span></div>
  <p class="pub-quote"><strong>公开页摘录（未核验为评论）：</strong>馬斯克調整得州工廠生產計劃</p>
  <ul class="pixel-list"><li>【标题】回复正文
    <span class="pub-meta">未提供站内热度指标 · 2026-11-06T11:45:19Z</span></li></ul>
  <div class="pub-verdict"><strong>▶ 词库极性与摘录说明：</strong>中性（词库净值 +0.00）。
    以上分析仅基于本次公开页摘录。</div>
  <div class="pub-meta">公开页实抓摘录（300 字 · 未核验为评论）· 中文行情社区 · 抓取核验 2026-11-06</div>
</article>
<article class="pub-card">
  <div class="pub-card-head"><span class="pub-name">7. MarketWatch 宏观</span><span class="pub-chip">原生 RSS</span></div>
  <p class="pub-quote">当次抓取成功，但时效窗口内没有可核验条目（不回填旧标题）。</p>
  <div class="pub-meta">当次实抓 · 端点 https://feeds.content.dowjones.io/rss · 36 ms · 道琼斯官方 RSS。</div>
</article>
<div class="pub-meta">数据缺口：RSS 情报源 45/49 源可用（91%），4 源未获取（按空值处理）</div>
<p>注：本节结论逐条对应当次实时行情。</p>
<p>⚠️ 风险提示：仅供参考，不构成投资建议。</p>
</main><footer>页脚</footer></body></html>"""

# 状态文字：EPUB 与纯文本两种输出里都不许出现。
STATUS_MARKERS = (
    "微信推送 ·", "点击下方按钮", "立即推送", "一对一专属推送已就绪", "SOURCE FEEDS",
    "项同步成功", "抓取于 2026", "核验日期", "条入库", "抓取状态", "当次未获取",
    "抓取核验 2026", "当次实抓", "端点 https://", "当次抓取成功", "数据缺口",
    "未提供站内热度指标",
)
# 正文：两种输出里都必须还在。
CONTENT_MARKERS = (
    "每日全球全景扫描", "恒生指数", "24,481.86", "馬斯克調整得州工廠生產計劃",
    "【标题】回复正文", "富途牛牛社区", "雪球网", "MarketWatch", "原生 RSS",
)
# 说明与注释属于内容（本次口径：只删运行状态），必须保留。
# 「口径：力量分 = 45% …」在 EPUB 里会变成 kv 表、数字还会被包进 span，
# 所以这里只断言不含数字与冒号的片段。
NOTE_MARKERS = (
    "力量分", "词库极性与摘录说明", "注：本节结论", "风险提示",
)


class EpubStatusFilterTest(unittest.TestCase):
    def setUp(self):
        self.stats = {}
        self.body, self.sections, self.data_time = be.build_document(PAGE, "", self.stats)

    def test_status_text_is_gone(self):
        for marker in STATUS_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.body)

    def test_report_content_survives(self):
        for marker in CONTENT_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.body)

    def test_methodology_notes_survive(self):
        for marker in NOTE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.body)

    def test_unavailable_source_keeps_only_its_name(self):
        # 「未获取」这枚状态标签删掉，源名留着（49 源清单仍是全的）。
        self.assertIn("雪球网", self.body)
        self.assertNotIn("未获取", self.body)
        # 研判标签不是状态，留着。
        self.assertIn("中性", self.body)

    def test_scan_time_line_is_kept(self):
        self.assertIn("扫描时间", self.body)
        self.assertEqual(self.data_time, "2026-11-06T13:32:10Z")

    def test_removed_count_is_reported(self):
        self.assertGreaterEqual(self.stats["status_removed"], 10)

    def test_emptied_containers_are_removed(self):
        body = be.extract_main(
            "<main><div><p>抓取状态：当次未获取（robots.txt 不允许抓取）</p></div>"
            "<p>正文</p></main>")
        self.assertIn("正文", body)
        self.assertNotIn("<div></div>", body)
        self.assertNotIn("抓取状态", body)

    def test_data_time_is_read_before_filtering(self):
        # 时间戳只出现在会被删掉的汇总状态行里，也必须先取到再删。
        _, _, data_time = be.build_document(
            "<main><div class=\"pub-meta\">11/12 项同步成功 · 扫描时间 2026-11-06 "
            "13:32:10 UTC · 核验日期 2026-11-06</div><p>正文</p></main>")
        self.assertEqual(data_time, "2026-11-06T13:32:10Z")

    def test_placeholder_check_runs_before_filtering(self):
        # 模板占位符常常正好落在状态块里（{{FETCH_STATUS}}），删完再查就查不出来了。
        with self.assertRaises(ValueError):
            be.build_document(
                '<main><div class="pub-meta">{{FETCH_STATUS}}</div><p>正文</p></main>')

    def test_source_classes_are_not_serialised(self):
        for marker in ("pub-meta", "push-wrap", "pub-chip", "filter-result"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.body)


class TextStatusFilterTest(unittest.TestCase):
    def setUp(self):
        self.text = k.extract_text(PAGE)
        self.lines = self.text.splitlines()

    def test_status_lines_are_gone(self):
        for marker in STATUS_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.text)

    def test_report_content_survives(self):
        for marker in CONTENT_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.text)

    def test_methodology_notes_survive(self):
        for marker in NOTE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.text)

    def test_status_chip_is_cut_from_the_source_name(self):
        # 纯文本里源名和「未获取」挤在同一行，只切尾巴，不整行删。
        self.assertIn("雪球网", self.text)
        self.assertNotIn("未获取", self.text)
        self.assertTrue(any(line.strip().endswith("雪球网") for line in self.lines))

    def test_inline_metric_tail_is_cut(self):
        line = next(line for line in self.lines if "【标题】回复正文" in line)
        self.assertNotIn("2026-11-06T11:45:19Z", line)

    def test_verdict_chip_is_not_touched(self):
        self.assertIn("中性", self.text)

    def test_placeholder_check_runs_before_filtering(self):
        with self.assertRaises(ValueError):
            k.extract_text('<main><div class="pub-meta">{{FETCH_STATUS}}</div><p>正文</p></main>')

    def test_all_status_page_does_not_produce_an_empty_report(self):
        with self.assertRaises(ValueError):
            k.extract_text('<main><p>STATUS &gt; 抓取中…</p><p>加载中</p></main>')

    def test_line_filter_is_pure(self):
        rows = ["恒指 24,481.86", "抓取状态：当次未获取（robots.txt 不允许抓取）", "口径：力量分 = 45% 幅度"]
        self.assertEqual(k.filter_status_lines(rows), [rows[0], rows[2]])


class RulesStayInSyncTest(unittest.TestCase):
    """kindle_report.py 要单独拷进 004 仓库，规则只能复制——所以逐条比对。"""

    def test_line_patterns_match(self):
        self.assertEqual(be.STATUS_LINE_PATTERNS, k.STATUS_LINE_PATTERNS)

    def test_drop_classes_match(self):
        self.assertEqual(set(be.STATUS_DROP_CLASSES), set(k.STATUS_DROP_CLASSES))

    def test_drop_ids_match(self):
        self.assertEqual(set(be.STATUS_DROP_IDS), set(k.STATUS_DROP_IDS))

    def test_chip_texts_match(self):
        self.assertEqual(set(be.STATUS_CHIP_TEXT), set(k.STATUS_CHIP_TEXT))

    def test_both_converters_agree_line_by_line(self):
        """同一页文字，两个转换器的判断必须一致。"""
        samples = [
            "STATUS > 一对一专属推送已就绪", "49 SOURCE FEEDS", "抓取中…", "加载中",
            "抓取状态： 当次未获取（robots.txt 不允许抓取）",
            "当次实抓 · 端点 https://x/rss · 36 ms · 官方 RSS。",
            "11/12 项同步成功 · 抓取于 2026-11-06 13:30:00 UTC",
            "宏观快讯 301 条入库 · 时效窗口 7 天",
            "数据缺口：RSS 情报源 45/49 源可用（91%）",
            "公开页实抓摘录（300 字 · 未核验为评论）· 中文行情社区 · 抓取核验 2026-11-06",
            "2026-11-06T11:44:52Z · RSSHub 第三方实例输出 · 本次匹配 3 条，展示 3 条",
            "量化平台接口 2/11 可用 · 9 个接口本次自动降级",
            "未获取", "今日未获取", "◆ 宏观快讯 — 今日未获取",
            "运行 python3 community_snapshot.py 并执行 python3 build_site.py",
            "恒指 24,481.86（−1.27%）",
            "扫描时间 2026-11-06 13:32:10 UTC · 行情日期 2026-11-06 · 数据覆盖 99%",
            "口径：力量分 = 45% 幅度 + 30% 印证度 + 25% 时效。",
            "▶ 词库极性与摘录说明：中性（词库净值 +0.00）。",
            "注：本节结论逐条对应当次实时行情。",
            "⚠️ 风险提示：仅供参考，不构成投资建议。",
            "评论仅为有限样本，按各站实际指标排序；未获取源不生成指标。",
            "缺口记账：3/49 个源当次未获取 —— 它们的沉默不算利好。",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(be.is_status_text(text), k.is_status_text(text))


if __name__ == "__main__":
    unittest.main()
