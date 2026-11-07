"""正文判定：抓回来的东西先判断，不是正文的不要，乱码不要。

删：站点导航条、「关于我们 / 网站声明 / 联系方式」、二维码与 App 下载、
查看更多 / 加载更多、版权与 ICP 备案、编辑部地址、缺字形方块与编码坏掉的乱码。
留：报告正文（行情、力量条目、快讯、AI 解读、社区摘录、结论）、口径说明与风险提示，
以及英文/日文正文里那些「长得像导航词」的普通句子。

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

# 财联社首页的抓取原文：导航条 + 空数据占位，一个字都不是文章正文。
CAILIAN = (
    "财联社A股24小时电报-上市公司动态-今日股市行情报道 关于我们 网站声明 联系方式 "
    "用户反馈 网站地图 帮助 首页 电报 话题 盯盘 VIP FM 投研 下载 全部 加红 公司 "
    "看盘 港美股 基金 提醒 电报持续更新中 日期 加载更多 上证指数 -.-- -.-- (-.--%) "
    "深证成指 -.-- -.-- (-.--%) 创业板指 -.-- -.-- (-.--%) 行业板块 概念板块 "
    "地域板块 板块名称 涨跌幅 资金流入 -- -- -- 个股涨幅榜 个股跌幅榜 股票名称 涨跌价"
)
# 开盘啦首页：几条真标题 + 大段页脚，整段算站壳。
KAIPANLA = (
    "最新资讯 查看更多 > NEW 算力用电需求持续增长，绿色电力板块逆势活跃 扫码下载 "
    "开盘啦 关注开盘啦 微信公众号 关注开盘啦 官方微博 copyright© KAIPANLA "
    "TECHNOLOGY CO., 深圳开盘啦网络科技有限公司 粤ICP备2026012599号-4 版权所有 ｜ "
    "联系方式： kaipanla@kaipanla.onaliyun.com ｜ 地址：深圳市福田区沙头街道"
)
# 正文 + 页脚挤在一行：只切页脚，正文留下。
MIXED = (
    "【格隆汇-财经资讯动态-股市行情】北京：支持企业面向基础硬件、基础软件等领域开展"
    "自主攻关 传台积电进军DRAM！ 台积电可能要做的，不是传统DRAM 查看更多"
)
# Elite Trader 的英文简介：按空格切分会把 comments 当导航词，那是毁正文。
ENGLISH_BODY = (
    "Options If you have questions, comments, or expertise related to options "
    "trading, this is the place for you."
)

PAGE = f"""<html><body><nav>外站菜单</nav><main>
<h2>01 / 每日全球全景扫描 (Daily Global Panorama Scan)</h2>
<p>扫描时间 2026-11-06 13:32:10 UTC · 行情日期 2026-11-06 · 数据覆盖 99%</p>
<nav>首页 新闻 资讯 财经 股票 基金 期货 外汇</nav>
<aside>侧栏推荐 · 热门推荐</aside>
<footer>© 2026 某某财经网 版权所有 粤ICP备12345678号-1</footer>
<div role="navigation">关于我们 | 网站声明 | 联系方式</div>
<div class="site-footer"><span>关注我们</span><span>微信公众号</span><span>扫码下载APP</span></div>
<div class="share-bar">分享到微信 收藏本页 打印本页</div>
<article>
  <div><span>21. 财联社 · 电报</span><span>中性</span></div>
  <p>{CAILIAN}</p>
  <p>▶ 极性判读：中性（词库净值 +0.00）。</p>
</article>
<article>
  <div><span>38. 开盘啦 · 情绪复盘</span></div>
  <p>{KAIPANLA}</p>
</article>
<p>{MIXED}</p>
<p>【老虎国际】自 2026 年 6 月 12 日起将暂停股票交易服务，详见官网公告。</p>
<p>{ENGLISH_BODY}</p>
<p>公司官网首页改版，导航更清楚，用户反馈也更快。</p>
<p>锟斤拷锟斤拷锟斤拷锟斤拷</p>
<p>æ–‡ç« æ ‡é¢˜ é¡µé¢</p>
<p>ÐÂÀË²Æ¾­Íø ÐÂÀË²Æ¾­</p>
<p>□□□□□□</p>
<p>正文里混进了一个替换符 的句子，其余部分正常可读。</p>
<p>恒指 24,481.86（−1.27%），恒生国企 8,274.10（−1.13%）。</p>
<p>⚠️ 风险提示：仅供参考，不构成投资建议。</p>
<p>口径：力量分 = 45% 幅度 + 30% 印证度 + 25% 时效。</p>
<table><tr><th>市场</th><th>最新</th></tr><tr><td>恒生指数</td><td>24,481.86</td></tr></table>
<img src="pic.png" alt="图"/>
</main><footer>页脚</footer></body></html>"""

# 站点外壳：两种产物里都不许出现。
CHROME_MARKERS = (
    "关于我们", "网站声明", "联系方式", "网站地图", "加载更多",
    "查看更多", "扫一扫", "扫码下载", "关注我们", "微信公众号", "官方微博",
    "copyright", "版权所有", "粤ICP备", "分享到", "收藏本页", "热门推荐",
    "侧栏推荐", "上证指数", "-.--", "外站菜单",
)
# 乱码：整段删掉，不留半句。
GARBLED_MARKERS = (
    "锟斤拷", "æ–‡", "ÐÂÀË", "□□□□",
)
# 正文：两种产物里都必须还在。
CONTENT_MARKERS = (
    "每日全球全景扫描", "扫描时间", "财联社 · 电报", "极性判读", "开盘啦",
    "格隆汇", "台积电进军DRAM", "自 2026 年 6 月 12 日起将暂停股票交易服务",
    "comments, or expertise related to options trading", "官网首页改版",
    "风险提示", "口径",
)


class EpubBodyFilterTest(unittest.TestCase):
    def setUp(self):
        self.stats = {}
        self.body, _, _ = be.build_document(PAGE, "", self.stats)

    def test_site_chrome_is_gone(self):
        for marker in CHROME_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.body)

    def test_garbled_blocks_are_gone(self):
        for marker in GARBLED_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.body)

    def test_report_content_survives(self):
        for marker in CONTENT_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.body)
        # 数字会被包进 <span class="num">，所以数字单独按纯文本查。
        self.assertIn("24,481.86", be.plain_text(self.body))

    def test_english_sentence_is_not_cut_word_by_word(self):
        # 按空格切分成「分段」时，comments / home 这些词不能当站壳切掉。
        self.assertIn("questions, comments, or expertise", self.body)

    def test_body_and_footer_on_one_line_only_lose_the_footer(self):
        self.assertIn("台积电进军DRAM", self.body)
        self.assertNotIn("查看更多", self.body)

    def test_stray_replacement_character_is_removed_but_the_line_stays(self):
        self.assertIn("正文里混进了一个替换符", self.body)
        self.assertNotIn("\ufffd", self.body)

    def test_images_are_not_dropped_as_empty_blocks(self):
        self.assertIn("<img", self.body)

    def test_article_headers_keep_their_titles(self):
        # <header> 里是文章标题，只拆壳不丢内容（只有 <aside>/<footer> 整块删）。
        body = be.extract_main(
            "<main><article><header><h3>21. 财联社 · 电报</h3></header>"
            "<p>恒指 24,481.86（−1.27%）。</p></article></main>")
        self.assertIn("财联社 · 电报", body)
        self.assertIn("24,481.86", be.plain_text(body))

    def test_text_mode_keeps_article_headers_too(self):
        text = k.extract_text(
            "<main><article><header><h3>21. 财联社 · 电报</h3></header>"
            "<p>恒指 24,481.86（−1.27%）。</p></article></main>")
        self.assertIn("财联社 · 电报", text)

    def test_emptied_containers_are_removed(self):
        self.assertNotIn("<p></p>", self.body)
        self.assertNotIn("<div></div>", self.body)

    def test_removed_count_is_reported(self):
        self.assertGreaterEqual(self.stats["body_removed"], 8)


class TextBodyFilterTest(unittest.TestCase):
    def setUp(self):
        self.text = k.extract_text(PAGE)

    def test_site_chrome_is_gone(self):
        for marker in CHROME_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.text)

    def test_garbled_blocks_are_gone(self):
        for marker in GARBLED_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.text)

    def test_report_content_survives(self):
        for marker in CONTENT_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.text)

    def test_mixed_line_keeps_its_body(self):
        line = next(line for line in self.text.splitlines() if "格隆汇" in line)
        self.assertIn("台积电进军DRAM", line)
        self.assertNotIn("查看更多", line)

    def test_english_sentence_is_not_cut_word_by_word(self):
        self.assertIn("questions, comments, or expertise", self.text)

    def test_line_filter_is_pure(self):
        lines = ["恒指 24,481.86", "关于我们 | 网站声明 | 联系方式", "锟斤拷锟斤拷"]
        kept = k.filter_content_lines(lines)
        self.assertEqual(kept, [lines[0]])


class ClassifierTest(unittest.TestCase):
    """判定函数本身：宁可漏判，也别把正文当站壳。"""

    CHROME = (
        "关于我们 | 网站声明 | 联系方式 | 用户反馈 | 网站地图",
        "首页 电报 话题 盯盘 VIP FM 投研 下载 全部 加红 公司 看盘 港美股",
        "© 2026 深圳某某网络科技有限公司 版权所有 粤ICP备2026012599号-4",
        "扫码下载APP 关注微信公众号 官方微博 查看更多 加载更多",
        CAILIAN,
        KAIPANLA,
        "Home About us Contact us Privacy policy Terms of service",
        "総合トップ ニュース 掲示板 投資信託 ログイン 会員登録 サイトマップ",
    )
    BODY = (
        "恒指 24,481.86（−1.27%），恒生国企 8,274.10（−1.13%）。",
        "⚠️ 风险提示：仅供参考，不构成投资建议。数据可能延迟，请核实最新数据。",
        "口径：力量分 = 45% 幅度 + 30% 印证度 + 25% 时效。",
        "▶ 词库极性与摘录说明：中性（词库净值 +0.00）。",
        "11 月 6 日 港股风向标｜恒指反弹受阻再度缩量　财联社11月6日讯 今日港股短线回落。",
        ENGLISH_BODY,
        "【老虎国际】自 2026 年 6 月 12 日起将暂停股票交易服务，详见官网公告。",
        "注：本节结论逐条对应当次实时行情。",
    )

    def test_chrome_lines_are_recognised(self):
        for line in self.CHROME:
            with self.subTest(line=line):
                self.assertTrue(k.is_chrome_text(line))

    def test_body_lines_are_never_chrome(self):
        for line in self.BODY:
            with self.subTest(line=line):
                self.assertFalse(k.is_chrome_text(line))
                self.assertFalse(k.is_garbled(line))

    def test_trimming_only_removes_chrome_segments(self):
        self.assertEqual(
            k.trim_chrome_text("恒指 24,481.86 · 查看更多"),
            "恒指 24,481.86")
        unchanged = "净情感 +0.07 · 负面占比 3.33%"
        self.assertEqual(k.trim_chrome_text(unchanged), unchanged)

    def test_garbled_samples(self):
        for line in ("锟斤拷锟斤拷", "æ–‡ç« ", "ÐÂÀË²Æ¾­Íø", "□□□□", "\ufffd\ufffd"):
            with self.subTest(line=line):
                self.assertTrue(k.is_garbled(line))

    def test_garbled_ratio_is_bounded(self):
        self.assertEqual(k.garbled_ratio(""), 0.0)
        self.assertLessEqual(k.garbled_ratio("锟斤拷"), 1.0)
        self.assertEqual(k.garbled_ratio("恒指 24,481.86"), 0.0)


class SourceDecodingTest(unittest.TestCase):
    """解码也是「乱码不要」的一部分：解不对就说清楚，别生成一页垃圾。"""

    def test_utf8_with_bom(self):
        self.assertEqual(k.decode_source("\ufeff<main>正文</main>".encode("utf-8")),
                         "<main>正文</main>")

    def test_declared_charset_is_honoured(self):
        raw = '<meta charset="gb18030"><main>恒指 24,481.86</main>'.encode("gb18030")
        self.assertIn("恒指 24,481.86", k.decode_source(raw))

    def test_undecodable_bytes_are_rejected(self):
        with self.assertRaises(ValueError):
            k.decode_source(b"\xff\xfe\x00garbage\x9d\x9e")

    def test_both_converters_decode_the_same_way(self):
        raw = '<meta charset="gb18030"><main>恒指</main>'.encode("gb18030")
        self.assertEqual(k.decode_source(raw), be.decode_source(raw))


class GarbledPageTest(unittest.TestCase):
    """整页都是乱码时宁可报错，也不要把一页垃圾推给设备。"""

    BROKEN = "<main>" + "".join(
        f"<p>ÐÂÀË²Æ¾­Íø ÐÂÀË²Æ¾­Íø {index}</p>" for index in range(10)) + "</main>"

    def test_epub_refuses_a_mojibake_page(self):
        with self.assertRaises(ValueError):
            be.build_document(self.BROKEN)

    def test_text_refuses_a_mojibake_page(self):
        with self.assertRaises(ValueError):
            k.extract_text(self.BROKEN)


class RulesStayInSyncTest(unittest.TestCase):
    """kindle_report.py 要单独拷进 004 仓库，规则只能复制——所以逐条比对。"""

    def test_constants_match(self):
        for name in ("BODY_DROP_CLASSES", "BODY_DROP_IDS", "BODY_DROP_ROLES",
                     "BODY_DROP_TAGS", "CHROME_TEXT_PATTERNS", "CHROME_TOKENS_CJK",
                     "NAV_TOKENS_CJK", "NAV_TOKENS_WORD", "GARBLED_MARKERS"):
            with self.subTest(name=name):
                self.assertEqual(set(getattr(be, name)), set(getattr(k, name)))
        for name in ("CHROME_DROP_RATIO", "NAV_TOKEN_CHROME_COUNT", "CHROME_SEGMENT_MAX",
                     "GARBLED_RATIO", "GARBLED_DOC_RATIO"):
            with self.subTest(name=name):
                self.assertEqual(getattr(be, name), getattr(k, name))

    def test_both_converters_agree_line_by_line(self):
        samples = ClassifierTest.CHROME + ClassifierTest.BODY + (
            "锟斤拷", "ÐÂÀË²Æ¾­Íø", "\ufffd", "11 月 6 日 恒指 24,481.86（▼−1.27%）",
            "最新资讯 查看更多 > NEW 算力用电需求持续增长",
            "分享到 收藏本页 打印本页",
        )
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(be.is_chrome_text(text), k.is_chrome_text(text))
                self.assertEqual(be.is_garbled(text), k.is_garbled(text))
                self.assertEqual(be.trim_chrome_text(text), k.trim_chrome_text(text))

    def test_aside_and_footer_are_dropped_by_the_epub_parser_too(self):
        self.assertLessEqual(set(be.BODY_DROP_TAGS), set(be.DROP))
        for tag in be.BODY_DROP_TAGS:
            with self.subTest(tag=tag):
                self.assertIn(tag, k.SKIP)


if __name__ == "__main__":
    unittest.main()
