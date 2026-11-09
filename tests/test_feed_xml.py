"""聚合 RSS：site/news.xml 的结构、转义、时间与守卫。

feed 是给别人家阅读器吃的，所以这里几乎每条测试都是「解析回来对不对」，
而不是「字符串里有没有某个片段」。
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_feed  # noqa: E402

NOW = datetime(2026, 11, 8, 0, 30, tzinfo=timezone.utc)   # 北京时间 08:30


def payload(count=12, source="CLS", group="flash"):
    items = []
    for index in range(count):
        items.append({
            "title": f"第 {index} 条财经新闻标题",
            "link": f"https://example.com/{index}",
            "published": f"2026-11-07T{index % 10:02d}:00:00Z",
            "summary": "一句摘要" if index % 2 else "",
            "source": source,
            "group": group,
            "lang": "zh",
        })
    return {"generated": "2026-11-07T23:00:00+00:00", "sources": [], "items": items}


def parse(text):
    return ElementTree.fromstring(text.encode("utf-8"))


class FeedStructureTest(unittest.TestCase):
    def build(self, data=None, **kwargs):
        folder = tempfile.mkdtemp()
        info, path = build_feed.build(data or payload(), Path(folder), now=NOW, **kwargs)
        return info, path, parse(path.read_text(encoding="utf-8"))

    def test_channel_metadata(self):
        _info, _path, root = self.build()
        channel = root.find("channel")
        self.assertEqual(root.get("version"), "2.0")
        self.assertEqual(channel.findtext("title"), build_feed.FEED_TITLE)
        self.assertEqual(channel.findtext("language"), "zh-cn")
        self.assertEqual(channel.findtext("ttl"), "60")
        self.assertTrue(channel.findtext("description"))
        self.assertTrue(channel.findtext("lastBuildDate"))

    def test_self_link_points_at_the_published_file(self):
        _info, _path, root = self.build()
        atom_link = root.find("channel/{http://www.w3.org/2005/Atom}link")
        self.assertIsNotNone(atom_link)
        self.assertEqual(atom_link.get("rel"), "self")
        self.assertTrue(atom_link.get("href").endswith("site/news.xml"))

    def test_every_item_is_rendered(self):
        info, _path, root = self.build()
        self.assertEqual(len(root.findall("./channel/item")), info["items"])
        self.assertEqual(info["items"], 12)

    def test_duplicate_article_links_are_emitted_once(self):
        data = payload(count=3)
        data["items"][1]["link"] = data["items"][0]["link"]
        data["items"][1]["title"] = "同一文章的另一个标题版本"
        info, _path, root = self.build(data, min_items=1)
        items = root.findall("./channel/item")
        guids = [item.findtext("guid") for item in items]
        self.assertEqual(info["items"], 2)
        self.assertEqual(len(guids), len(set(guids)))

    def test_items_are_newest_first(self):
        _info, _path, root = self.build()
        stamps = [parsedate_to_datetime(item.findtext("pubDate"))
                  for item in root.findall("./channel/item")]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_pubdate_is_rfc822_utc(self):
        _info, _path, root = self.build()
        raw = root.findall("./channel/item")[0].findtext("pubDate")
        self.assertTrue(raw.endswith("+0000"), raw)
        parsed = parsedate_to_datetime(raw)
        self.assertEqual(parsed.tzinfo.utcoffset(parsed).total_seconds(), 0)


class ItemContentTest(unittest.TestCase):
    def one(self, **fields):
        item = {"title": "标题一条", "link": "https://example.com/a",
                "published": "2026-11-07T10:00:00Z", "summary": "摘要一句",
                "source": "CLS", "group": "flash"}
        item.update(fields)
        data = {"generated": "2026-11-08T00:00:00+00:00", "sources": [], "items": [item]}
        folder = tempfile.mkdtemp()
        _info, path = build_feed.build(data, Path(folder), now=NOW, min_items=1)
        return parse(path.read_text(encoding="utf-8")).find("./channel/item")

    def test_source_name_and_site(self):
        item = self.one()
        source = item.find("source")
        self.assertEqual(source.text, "财联社 · 电报")
        self.assertEqual(source.get("url"), "https://cls.cn")

    def test_category_is_the_group_label(self):
        self.assertEqual(self.one().findtext("category"), "中文财经快讯")
        self.assertEqual(self.one(group="global").findtext("category"), "全球市场与要闻")

    def test_link_is_the_guid_when_present(self):
        item = self.one()
        guid = item.find("guid")
        self.assertEqual(guid.get("isPermaLink"), "true")
        self.assertEqual(guid.text, "https://example.com/a")
        self.assertEqual(item.findtext("link"), "https://example.com/a")

    def test_guid_falls_back_to_a_stable_urn(self):
        item = self.one(link="")
        guid = item.find("guid")
        self.assertEqual(guid.get("isPermaLink"), "false")
        self.assertTrue(guid.text.startswith("urn:004:news:"))
        # 同一条没链接的新闻，两次生成的指纹必须一样，否则读者端会重复。
        self.assertEqual(guid.text, self.one(link="").find("guid").text)

    def test_item_without_a_url_still_gets_the_source_homepage(self):
        """没有原文地址时 <link> 退回站点首页：KOReader 缺 <link> 会整轮停在中途。"""
        item = self.one(link="")
        self.assertEqual(item.findtext("link"), "https://cls.cn")
        self.assertEqual(item.find("guid").get("isPermaLink"), "false")

    def test_pseudo_link_is_not_published_as_a_link(self):
        """`jin10:index:…` 这种伪链接既不能当 <link>，也不能当 permalink 的 guid。"""
        item = self.one(link="jin10:index:20261107232605506800", source="JIN10")
        self.assertEqual(item.findtext("link"), "https://jin10.com")
        guid = item.find("guid")
        self.assertEqual(guid.get("isPermaLink"), "false")
        self.assertTrue(guid.text.startswith("urn:004:news:"))

    def test_every_item_always_has_an_absolute_link(self):
        data = payload(6)
        data["items"][0]["link"] = ""
        data["items"][1]["link"] = "jin10:index:1"
        del data["items"][2]["link"]
        folder = tempfile.mkdtemp()
        _info, path = build_feed.build(data, Path(folder), now=NOW, min_items=1)
        root = parse(path.read_text(encoding="utf-8"))
        for item in root.findall("./channel/item"):
            self.assertTrue(item.findtext("link", "").startswith("https://"),
                            item.findtext("title"))

    def test_newline_in_title_is_collapsed(self):
        item = self.one(title="上半句\n下半句")
        self.assertNotIn("\n", item.findtext("title"))
        self.assertEqual(item.findtext("title"), "上半句 下半句")

    def test_empty_summary_is_omitted(self):
        self.assertIsNone(self.one(summary="").find("description"))

    def test_summary_equal_to_title_is_omitted(self):
        self.assertIsNone(self.one(summary="标题一条").find("description"))

    def test_description_is_kept_when_it_adds_something(self):
        self.assertEqual(self.one().findtext("description"), "摘要一句")

    def test_analysis_is_appended_to_rss_body(self):
        chain = ("线索：政策公布；事实：文件列出新要求；推断：执行成本或变化；"
                 "影响：相关行业需留意；验证：跟踪配套细则。")
        item = self.one(analysis=chain)
        description = item.findtext("description")
        self.assertIn("AI 线索分析", description)
        self.assertEqual(description.count("<p>"), 7)  # 摘要、分析标题、五个线索环节
        for label in ("线索：", "事实：", "推断：", "影响：", "验证："):
            self.assertIn(f"<p>{label}", description)

    def test_lite_summary_limit_does_not_truncate_analysis(self):
        chain = ("线索：明确的政策线索；事实：公告已发布；推断：执行环节可能变化；"
                 "影响：行业参与者需观察；验证：等待实施细则。")
        data = payload(count=1)
        data["items"][0]["summary"] = "摘要内容" * 50
        data["items"][0]["analysis"] = chain
        folder = Path(tempfile.mkdtemp())
        _info, path = build_feed.build(data, folder, now=NOW, min_items=1, desc_limit=20)
        item = parse(path.read_text(encoding="utf-8")).find("./channel/item")
        description = item.findtext("description")
        self.assertIn("<p>" + "摘要内容" * 5 + "…</p>", description)
        flattened = description.replace("<p>", "").replace("</p>", "")
        self.assertIn(chain, flattened)


class ChinesePairingTest(unittest.TestCase):
    """英文条目必须中英并列：阅读器列表页只显示标题，译文不接在标题上就等于没翻。"""

    def build(self, data):
        folder = Path(tempfile.mkdtemp())
        info, path = build_feed.build(data, folder, now=NOW)
        return info, parse(path.read_text(encoding="utf-8"))

    def test_english_item_shows_both_languages(self):
        data = payload(12)
        data["items"][0].update({"title": "Fed cuts rates by 25bp", "lang": "en",
                                 "summary": "The Fed lowered rates for the second time.",
                                 "title_zh": "美联储降息 25 个基点",
                                 "summary_zh": "美联储第二次下调利率"})
        info, root = self.build(data)
        items = root.findall("./channel/item")
        item = next(node for node in items if "Fed cuts" in (node.findtext("title") or ""))
        self.assertIn("中文：美联储降息 25 个基点", item.findtext("title"))
        self.assertIn("中文：美联储第二次下调利率", item.findtext("description"))
        self.assertIn("原文：The Fed lowered rates", item.findtext("description"))
        self.assertEqual(info["zh"], 1)

    def test_chinese_item_is_untouched(self):
        info, root = self.build(payload(12))
        self.assertEqual(info["zh"], 0)
        for node in root.findall("./channel/item"):
            self.assertNotIn("｜", node.findtext("title"))

    def test_translated_summary_alone_is_used_when_no_english_summary(self):
        data = payload(12)
        data["items"][0].update({"title": "Fed cuts rates by 25bp", "lang": "en",
                                 "summary": "", "title_zh": "美联储降息",
                                 "summary_zh": "美联储下调利率"})
        _info, root = self.build(data)
        item = next(node for node in root.findall("./channel/item")
                    if "Fed cuts" in (node.findtext("title") or ""))
        self.assertEqual(item.findtext("description"), "中文：美联储下调利率")


class EscapingTest(unittest.TestCase):
    def build_one(self, **fields):
        item = {"title": "标题一条财经新闻", "link": "https://example.com/a?x=1&y=2",
                "published": "2026-11-07T10:00:00Z", "summary": "",
                "source": "CLS", "group": "flash"}
        item.update(fields)
        data = {"generated": "2026-11-08T00:00:00+00:00", "sources": [], "items": [item]}
        folder = tempfile.mkdtemp()
        _info, path = build_feed.build(data, Path(folder), now=NOW, min_items=1)
        return parse(path.read_text(encoding="utf-8")).find("./channel/item")

    def test_ampersands_and_angle_brackets_survive(self):
        item = self.build_one(title='AT&T 收购 <b>某公司</b> "报价" 上涨 5% & 更多')
        self.assertEqual(item.findtext("title"), 'AT&T 收购 <b>某公司</b> "报价" 上涨 5% & 更多')

    def test_query_string_in_link_is_escaped_not_broken(self):
        self.assertEqual(self.build_one().findtext("link"), "https://example.com/a?x=1&y=2")

    def test_control_characters_are_dropped(self):
        item = self.build_one(title="标题一条新闻\x07带\x0b控制符")
        text = item.findtext("title")
        # 控制符一个都不能留在 XML 里；正文文字本身保留（归一化可能把它换成空格）。
        self.assertNotIn("\x07", text)
        self.assertNotIn("\x0b", text)
        self.assertIn("控制符", text)

    def test_xml_declaration_and_chinese_encoding(self):
        folder = tempfile.mkdtemp()
        _info, path = build_feed.build(payload(), Path(folder), now=NOW)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="utf-8"?>'))
        self.assertIn("财经新闻", text)


class AnalysisGuardTest(unittest.TestCase):
    def item(self, analysis):
        data = payload(count=1)
        data["items"][0]["analysis"] = analysis
        folder = Path(tempfile.mkdtemp())
        _info, path = build_feed.build(data, folder, now=NOW, min_items=1)
        return parse(path.read_text(encoding="utf-8")).find("./channel/item")

    def test_new_paragraph_heading_passes_analysis_check(self):
        analysis = ("线索：市场成交额变化；事实：指数早盘收涨；推断：风险偏好可能改善；"
                    "影响：相关板块或受关注；验证：观察后续成交与行业数据。")
        item = self.item(analysis)
        self.assertTrue(build_feed.check_analysis_fields([item]))

    def test_analysis_check_rejects_missing_clue(self):
        item = self.item("线索：有线索，但遗漏其余环节。")
        with self.assertRaisesRegex(ValueError, "第 1 条缺少 AI 线索分析标签"):
            build_feed.check_analysis_fields([item])


class DeviceFieldGuardTest(unittest.TestCase):
    """设备侧硬要求：缺一条就整轮停在中途（KOReader 索引 nil 报错）。"""

    def fields(self, xml):
        return parse(xml).findall("./channel/item")

    def good(self):
        return ("<rss><channel><item><title>标题</title><link>https://a.example/1</link>"
                "<guid>g</guid></item></channel></rss>")

    def test_good_item_passes(self):
        self.assertTrue(build_feed.check_device_fields(self.fields(self.good())))

    def test_duplicate_guid_is_rejected(self):
        xml = self.good().replace("</channel>",
                                  "<item><title>另一条</title><link>https://a.example/2</link>"
                                  "<guid>g</guid></item></channel>")
        with self.assertRaisesRegex(ValueError, "<guid> 重复"):
            build_feed.check_device_fields(self.fields(xml))

    def test_missing_link_is_rejected(self):
        xml = "<rss><channel><item><title>标题</title><guid>g</guid></item></channel></rss>"
        with self.assertRaises(ValueError) as caught:
            build_feed.check_device_fields(self.fields(xml))
        self.assertIn("第 1 条", str(caught.exception))

    def test_relative_link_is_rejected(self):
        xml = ("<rss><channel><item><title>标题</title><link>jin10:index:1</link>"
               "<guid>g</guid></item></channel></rss>")
        with self.assertRaises(ValueError):
            build_feed.check_device_fields(self.fields(xml))

    def test_newline_in_title_is_rejected(self):
        xml = ("<rss><channel><item><title>上\n下</title><link>https://a.example/1</link>"
               "<guid>g</guid></item></channel></rss>")
        with self.assertRaises(ValueError) as caught:
            build_feed.check_device_fields(self.fields(xml))
        self.assertIn("换行", str(caught.exception))

    def test_missing_guid_is_rejected(self):
        xml = "<rss><channel><item><title>标题</title><link>https://a.example/1</link></item></channel></rss>"
        with self.assertRaises(ValueError):
            build_feed.check_device_fields(self.fields(xml))


class GuardTest(unittest.TestCase):
    def test_too_few_items_refuses_to_write(self):
        folder = Path(tempfile.mkdtemp())
        with self.assertRaises(ValueError):
            build_feed.build(payload(count=3), folder, min_items=10, now=NOW)
        self.assertFalse((folder / "news.xml").exists())

    def test_empty_payload_refuses_to_write(self):
        folder = Path(tempfile.mkdtemp())
        with self.assertRaises(ValueError):
            build_feed.build({"items": []}, folder, min_items=1, now=NOW)
        self.assertFalse((folder / "news.xml").exists())

    def test_max_items_is_respected(self):
        folder = tempfile.mkdtemp()
        info, _path = build_feed.build(payload(count=30), Path(folder), now=NOW,
                                       min_items=1, max_items=5)
        self.assertEqual(info["items"], 5)

    def test_info_lists_failed_sources_with_reason(self):
        data = payload(12)
        data["sources"] = [{"key": "CLS", "ok": True},
                           {"key": "COINDESK", "ok": False, "error": "HTTP 503: Service Unavailable"}]
        info, _path = build_feed.build(data, Path(tempfile.mkdtemp()), now=NOW)
        self.assertEqual(info["sources_ok"], 1)
        self.assertEqual(info["sources_total"], 2)
        self.assertEqual(info["sources_failed"][0][0], "COINDESK")
        self.assertIn("503", info["sources_failed"][0][1])

    def test_info_counts_sources(self):
        data = payload(count=6)
        data["items"].append({**data["items"][0], "title": "另一条来自财新的新闻",
                             "link": "https://e.com/x",
                              "source": "CAIXIN", "group": "depth"})
        folder = tempfile.mkdtemp()
        info, _path = build_feed.build(data, Path(folder), now=NOW, min_items=1)
        self.assertEqual(info["sources"], 2)


class JsdelivrTest(unittest.TestCase):
    def test_mirror_url(self):
        self.assertEqual(
            build_feed.jsdelivr_url("https://raw.githubusercontent.com/k-macao/04-kindle/main/site/news.xml"),
            "https://cdn.jsdelivr.net/gh/k-macao/04-kindle@main/site/news.xml")

    def test_unknown_host_has_no_mirror(self):
        self.assertEqual(build_feed.jsdelivr_url("https://example.com/news.xml"), "")


class EndToEndTest(unittest.TestCase):
    def test_cli_writes_a_readable_feed(self):
        folder = Path(tempfile.mkdtemp())
        data = folder / "news_data.json"
        data.write_text(json.dumps(payload(count=14), ensure_ascii=False), encoding="utf-8")
        code = build_feed.main(["--data", str(data), "--out", str(folder / "site"),
                                "--min-items", "5"])
        self.assertEqual(code, 0)
        root = parse((folder / "site" / "news.xml").read_text(encoding="utf-8"))
        self.assertEqual(len(root.findall("./channel/item")), 14)

    def test_cli_fails_without_news_data(self):
        folder = Path(tempfile.mkdtemp())
        self.assertEqual(build_feed.main(["--data", str(folder / "missing.json"),
                                          "--out", str(folder / "site")]), 1)


if __name__ == "__main__":
    unittest.main()
