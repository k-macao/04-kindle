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
        self.assertEqual(self.one(group="global").findtext("category"), "全球市场与加密")

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

    def test_empty_summary_is_omitted(self):
        self.assertIsNone(self.one(summary="").find("description"))

    def test_summary_equal_to_title_is_omitted(self):
        self.assertIsNone(self.one(summary="标题一条").find("description"))

    def test_description_is_kept_when_it_adds_something(self):
        self.assertEqual(self.one().findtext("description"), "摘要一句")


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
