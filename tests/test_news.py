"""每日新闻订阅：RSSHub 抓取（解析 / 回退 / 去重 / 时间窗）与日报排版。

这里不打网络：抓取函数允许注入 fetch，测试用假报文跑完整流程。
"""

import json
import sys
from urllib.error import HTTPError
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_news  # noqa: E402
import fetch_news  # noqa: E402
import news_sources  # noqa: E402

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)   # 北京时间 11:00

RSS_TWO = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>财联社</title>
<item>
  <title>恒指收涨 1.2%，南向资金净买入 24 亿</title>
  <link>https://example.com/a</link>
  <pubDate>Fri, 07 Nov 2026 01:00:00 +0800</pubDate>
  <description><![CDATA[<p>港股今日收涨，<b>成交额</b>放大。</p>]]></description>
</item>
<item>
  <title>银行板块午后走强</title>
  <link>https://example.com/b</link>
  <pubDate>Thu, 06 Nov 2026 22:00:00 +0800</pubDate>
  <description>银行板块午后走强</description>
</item>
</channel></rss>
"""

ATOM_ONE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>CoinDesk</title>
<entry>
  <title>Fed holds rates steady</title>
  <link rel="alternate" href="https://example.com/fed"/>
  <updated>2026-11-07T00:30:00Z</updated>
  <summary>Policy unchanged</summary>
</entry>
</feed>
"""

RDF_ONE = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>格隆汇</title></channel>
<item>
  <title>格隆汇研究：港股估值修复</title>
  <link>https://example.com/rdf</link>
  <dc:date>2026-11-07T02:00:00+08:00</dc:date>
</item>
</rdf:RDF>
"""


def rss_with(titles, published="Fri, 07 Nov 2026 02:00:00 +0800"):
    items = "".join(
        f"<item><title>{title}</title><link>https://example.com/{index}</link>"
        f"<pubDate>{published}</pubDate></item>"
        for index, title in enumerate(titles))
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'


def source(key="CLS", group="flash", route="/cls/telegraph", lang="zh"):
    return {"key": key, "name": f"源 {key}", "group": group, "route": route, "lang": lang}


def feed_fetch(mapping, calls=None):
    """按 URL 片段返回报文；mapping 的键是路由，值为 bytes 或异常。"""
    def fetch(url, timeout=15):
        if calls is not None:
            calls.append(url)
        for fragment, payload in mapping.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload.encode() if isinstance(payload, str) else payload
        raise OSError("HTTP Error 404: Not Found")
    return fetch


class CatalogTest(unittest.TestCase):
    def test_catalog_passes_its_own_checks(self):
        self.assertEqual(news_sources.catalog_errors(), [])

    def test_every_group_has_sources(self):
        for key, _label in news_sources.GROUPS:
            with self.subTest(group=key):
                self.assertTrue(news_sources.sources_in(key))

    def test_routes_are_unique(self):
        routes = [item["route"] for item in news_sources.SOURCES]
        self.assertEqual(len(routes), len(set(routes)))


class ProbeTest(unittest.TestCase):
    """官方实例探活：403 就排到队尾兜底，不删；404 说明实例没实现 /healthz，按可用处理。"""

    @staticmethod
    def fetch(mapping):
        def fetch(url, timeout=8):
            for fragment, payload in mapping.items():
                if fragment in url:
                    if isinstance(payload, Exception):
                        raise payload
                    return payload.encode() if isinstance(payload, str) else payload
            raise HTTPError(url, 404, "Not Found", {}, None)
        return fetch

    def test_official_goes_last_when_forbidden(self):
        forbidden = HTTPError("https://rsshub.app/healthz", 403, "Forbidden", {}, None)
        order = fetch_news.probe_instances(
            ["https://rsshub.app", "https://mirror.example"],
            fetch=self.fetch({"rsshub.app/healthz": forbidden, "mirror.example": "OK"}),
            log=lambda *_: None)
        self.assertEqual(order, ["https://mirror.example", "https://rsshub.app"])

    def test_official_stays_first_when_healthy(self):
        order = fetch_news.probe_instances(
            ["https://rsshub.app", "https://mirror.example"],
            fetch=self.fetch({"rsshub.app/healthz": "OK", "mirror.example": "OK"}),
            log=lambda *_: None)
        self.assertEqual(order, ["https://rsshub.app", "https://mirror.example"])

    def test_missing_health_path_is_not_a_failure(self):
        ok, note = fetch_news.probe_instance("https://mirror.example", fetch=self.fetch({}))
        self.assertTrue(ok)
        self.assertIn("404", note)

    def test_offline_instance_moves_back(self):
        order = fetch_news.probe_instances(
            ["https://dead.example", "https://alive.example"],
            fetch=self.fetch({"dead.example/healthz": OSError("timed out"),
                              "alive.example": "OK"}),
            log=lambda *_: None)
        self.assertEqual(order, ["https://alive.example", "https://dead.example"])

    def test_collect_probes_only_when_asked(self):
        calls = []

        def fetch(url, timeout=8):
            calls.append(url)
            return RSS_TWO.encode()

        fetch_news.collect(sources=[source()],
                           instances=["https://a.example", "https://b.example"],
                           fetch=fetch, now=NOW, log=lambda *_: None)
        self.assertFalse(any(url.endswith("/healthz") for url in calls))

        calls.clear()
        fetch_news.collect(sources=[source()],
                           instances=["https://a.example", "https://b.example"],
                           fetch=fetch, now=NOW, log=lambda *_: None, probe=True)
        self.assertTrue(any(url.endswith("/healthz") for url in calls))


class ParseFeedTest(unittest.TestCase):
    def test_rss_items(self):
        items = fetch_news.parse_feed(RSS_TWO.encode(), source())
        self.assertEqual(len(items), 2)
        first, second = items
        self.assertEqual(first["title"], "恒指收涨 1.2%，南向资金净买入 24 亿")
        self.assertEqual(first["link"], "https://example.com/a")
        self.assertEqual(first["published"], "2026-11-06T17:00:00Z")
        # HTML 标签与实体都不该留在摘要里；摘要和标题一样就丢掉。
        self.assertIn("港股今日收涨", first["summary"])
        self.assertNotIn("<p>", first["summary"])
        self.assertEqual(second["summary"], "")

    def test_atom_entry_uses_href_and_iso_date(self):
        items = fetch_news.parse_feed(ATOM_ONE.encode(), source("COINDESK", "global"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://example.com/fed")
        self.assertEqual(items[0]["published"], "2026-11-07T00:30:00Z")
        self.assertEqual(items[0]["group"], "global")

    def test_rdf_dc_date(self):
        items = fetch_news.parse_feed(RDF_ONE.encode(), source("GELONGHUI", "depth"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["published"], "2026-11-06T18:00:00Z")

    def test_broken_xml_raises_value_error(self):
        with self.assertRaises(ValueError):
            fetch_news.parse_feed(b"<rss><channel><item>", source())

    def test_empty_title_is_skipped(self):
        payload = rss_with(["", "  ", "正常标题一条"]).encode()
        self.assertEqual([item["title"] for item in fetch_news.parse_feed(payload, source())],
                         ["正常标题一条"])

    def test_title_gets_unescaped_and_collapsed(self):
        payload = rss_with(["&amp;lt;b&amp;gt;标题&amp;lt;/b&amp;gt;   两格"]).encode()
        items = fetch_news.parse_feed(payload, source())
        self.assertEqual(items[0]["title"], "标题 两格")


class RouteUrlTest(unittest.TestCase):
    def test_plain_route(self):
        self.assertEqual(fetch_news.route_url("https://rsshub.app/", "/cls/telegraph"),
                         "https://rsshub.app/cls/telegraph")

    def test_chinese_route_is_percent_encoded(self):
        url = fetch_news.route_url("https://rsshub.app", "/21caijing/channel/热点")
        self.assertTrue(url.startswith("https://rsshub.app/21caijing/channel/"))
        self.assertNotIn("热", url)
        self.assertIn("%E7%83%AD", url)


class CollectTest(unittest.TestCase):
    def test_first_instance_403_falls_back_to_second(self):
        calls = []

        def fetch(url, timeout=15):
            calls.append(url)
            if "rsshub.app" in url:
                raise OSError("HTTP Error 403: Forbidden")
            return RSS_TWO.encode()

        items, reports = fetch_news.collect(
            sources=[source()],
            instances=["https://rsshub.app", "https://rsshub.rss.tips"],
            fetch=fetch, now=NOW, max_age_hours=24, log=lambda *_: None)
        self.assertEqual(len(items), 2)
        self.assertTrue(reports[0]["ok"])
        self.assertEqual(reports[0]["instance"], "https://rsshub.rss.tips")
        # 先试排在前面的实例，失败了才换下一个。
        self.assertIn("rsshub.app/cls/telegraph", calls[0])

    def test_instance_trips_after_three_failures(self):
        calls = []

        def fetch(url, timeout=15):
            calls.append(url)
            if "dead" in url:
                raise OSError("HTTP Error 502: Bad Gateway")
            # 每个源给两条互不相同的标题，免得被跨源去重折叠成一条。
            return rss_with([f"{url.rsplit('/', 1)[-1]} 的第 1 条新闻",
                             f"{url.rsplit('/', 1)[-1]} 的第 2 条新闻"]).encode()

        sources = [source(f"S{index}", route=f"/cls/telegraph{index}") for index in range(5)]
        items, reports = fetch_news.collect(
            sources=sources,
            instances=["https://dead.example", "https://alive.example"],
            fetch=fetch, now=NOW, max_age_hours=24, trip_after=3, log=lambda *_: None)
        dead_calls = [url for url in calls if "dead" in url]
        self.assertEqual(len(dead_calls), 3)          # 3 次后不再打这个实例
        self.assertEqual(len(items), 10)              # 5 源 × 2 条都从活实例取到
        self.assertTrue(all(report["ok"] for report in reports))

    def test_old_items_are_dropped(self):
        old = rss_with(["一年前的旧闻标题"], published="Sat, 07 Nov 2025 02:00:00 +0800")
        items, _reports = fetch_news.collect(
            sources=[source()], instances=["https://rsshub.app"],
            fetch=feed_fetch({"/cls/telegraph": old}), now=NOW,
            max_age_hours=24, log=lambda *_: None)
        self.assertEqual(items, [])

    def test_same_title_across_sources_is_kept_once_with_earlier_time(self):
        early = rss_with(["同一条消息"], published="Fri, 07 Nov 2026 01:00:00 +0800")
        late = rss_with(["同一条消息"], published="Fri, 07 Nov 2026 07:00:00 +0800")
        items, _reports = fetch_news.collect(
            sources=[source("A", route="/a"), source("B", route="/b")],
            instances=["https://rsshub.app"],
            fetch=feed_fetch({"/a": early, "/b": late}), now=NOW,
            max_age_hours=24, log=lambda *_: None)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["published"], "2026-11-06T17:00:00Z")
        self.assertEqual(items[0]["source"], "A")

    def test_per_source_cap(self):
        payload = rss_with([f"第 {index} 条新闻标题" for index in range(30)])
        items, _reports = fetch_news.collect(
            sources=[source()], instances=["https://rsshub.app"],
            fetch=feed_fetch({"/cls/telegraph": payload}), now=NOW,
            max_age_hours=24, per_source=5, log=lambda *_: None)
        self.assertEqual(len(items), 5)

    def test_garbled_title_is_dropped(self):
        payload = rss_with(["锟斤拷锟斤拷 财经", "正常的一条财经新闻标题"])
        items, _reports = fetch_news.collect(
            sources=[source()], instances=["https://rsshub.app"],
            fetch=feed_fetch({"/cls/telegraph": payload}), now=NOW,
            max_age_hours=24, log=lambda *_: None)
        self.assertEqual([item["title"] for item in items], ["正常的一条财经新闻标题"])

    def test_seen_titles_do_not_come_back(self):
        seen = {fetch_news.title_hash("恒指收涨 1.2%，南向资金净买入 24 亿"): "2026-11-06T17:00:00Z"}
        items, _reports = fetch_news.collect(
            sources=[source()], instances=["https://rsshub.app"],
            fetch=feed_fetch({"/cls/telegraph": RSS_TWO}), now=NOW,
            max_age_hours=24, seen=seen, log=lambda *_: None)
        self.assertEqual([item["title"] for item in items], ["银行板块午后走强"])

    def test_all_instances_dead_reports_error(self):
        items, reports = fetch_news.collect(
            sources=[source()], instances=["https://rsshub.app"],
            fetch=feed_fetch({"/cls/telegraph": OSError("HTTP Error 500")}),
            now=NOW, max_age_hours=24, log=lambda *_: None)
        self.assertEqual(items, [])
        self.assertFalse(reports[0]["ok"])
        self.assertIn("500", reports[0]["error"])

    def test_items_are_newest_first(self):
        payload = rss_with(["较早的一条新闻", "较新的一条新闻"],
                           published="Fri, 07 Nov 2026 02:00:00 +0800")
        items, _reports = fetch_news.collect(
            sources=[source()], instances=["https://rsshub.app"],
            fetch=feed_fetch({"/cls/telegraph": payload}), now=NOW,
            max_age_hours=24, log=lambda *_: None)
        self.assertEqual([item["title"] for item in items], ["较早的一条新闻", "较新的一条新闻"])


class SeenFileTest(unittest.TestCase):
    def test_round_trip_and_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "news_seen.json"
            items = [{"title": "一条要记住的新闻", "published": "2026-11-07T01:00:00Z"}]
            seen = fetch_news.save_seen(path, {}, items, now=NOW, days=7)
            self.assertEqual(len(seen), 1)
            loaded = fetch_news.load_seen(path, now=NOW, days=7)
            self.assertEqual(loaded, seen)
            # 8 天后再读，这条指纹已经过期，会被忘掉。
            later = NOW + timedelta(days=8)
            self.assertEqual(fetch_news.load_seen(path, now=later, days=7), {})

    def test_missing_file_is_empty(self):
        self.assertEqual(fetch_news.load_seen("/nonexistent/news_seen.json", now=NOW), {})

    def test_bad_json_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "news_seen.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(fetch_news.load_seen(path, now=NOW), {})


class PruneTest(unittest.TestCase):
    def test_drops_short_and_duplicate_titles_and_sorts(self):
        items = [
            {"title": "同一条新闻标题", "published": "2026-11-07T00:00:00Z", "group": "flash"},
            {"title": "同一条新闻 标题", "published": "2026-11-06T22:00:00Z", "group": "flash"},
            {"title": "短", "published": "2026-11-07T02:00:00Z", "group": "flash"},
            {"title": "更新的一条新闻标题", "published": "2026-11-07T01:00:00Z", "group": "flash"},
        ]
        kept = build_news.prune(items)
        self.assertEqual([item["title"] for item in kept],
                         ["更新的一条新闻标题", "同一条新闻标题"])

    def test_max_items(self):
        items = [{"title": f"新闻标题第 {index} 条", "published": "2026-11-07T00:00:00Z"}
                 for index in range(20)]
        self.assertEqual(len(build_news.prune(items, max_items=3)), 3)


class GroupedTest(unittest.TestCase):
    def test_empty_group_is_skipped_and_numbering_stays_consecutive(self):
        items = [{"title": "全球新闻一条", "group": "global", "published": ""},
                 {"title": "快讯一条新闻", "group": "flash", "published": ""}]
        groups = build_news.grouped(items)
        self.assertEqual([label for _key, label, _rows in groups],
                         ["一、中文财经快讯", "二、全球市场与加密"])


class BuildTest(unittest.TestCase):
    def payload(self, count=12):
        items = []
        for index in range(count):
            items.append({
                "title": f"第 {index} 条财经新闻标题",
                "link": f"https://example.com/{index}",
                "published": f"2026-11-07T{index % 10:02d}:00:00Z",
                "summary": "一句摘要" if index % 2 else "",
                "source": "CLS",
                "group": "flash",
                "lang": "zh",
            })
        items.append({"title": "全球市场的一条新闻", "link": "", "published": "2026-11-06T10:00:00Z",
                      "summary": "", "source": "COINDESK", "group": "global", "lang": "en"})
        return {"generated": "2026-11-07T03:00:00+00:00",
                "sources": [{"key": "CLS", "ok": True}, {"key": "COINDESK", "ok": False}],
                "items": items}

    def test_build_writes_three_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest, paths = build_news.build(self.payload(), Path(folder) / "site",
                                               min_items=5, now=NOW)
            self.assertEqual(manifest["items"], 13)
            self.assertEqual(manifest["sources_ok"], 1)
            self.assertEqual(manifest["sources_total"], 2)
            # 失败源也写进清单：清单是提交到仓库的，第二天能看出哪条源是常死。
            self.assertEqual(manifest["sources_failed"], ["COINDESK"])
            self.assertEqual(manifest["doc"], "news.epub")
            self.assertEqual(manifest["data_time"], "2026-11-07T09:00:00Z")
            self.assertEqual(manifest["data_time_kind"], "item")
            for path in paths.values():
                self.assertTrue(Path(path).is_file(), path)
            text = paths["text"].read_text(encoding="utf-8")
            self.assertIn("一、中文财经快讯", text)
            self.assertIn("二、全球市场与加密", text)
            self.assertIn("财联社 · 电报", text)
            # 摘要只出一次，标题不会被当成摘要重复一行。
            self.assertEqual(text.count("第 1 条财经新闻标题"), 1)

    def test_epub_has_sections_and_articles(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest, paths = build_news.build(self.payload(), Path(folder) / "site",
                                               min_items=5, now=NOW)
            with zipfile.ZipFile(paths["epub"]) as book:
                page = book.read("OEBPS/report.xhtml").decode("utf-8")
                nav = book.read("OEBPS/nav.xhtml").decode("utf-8")
                style = book.read("OEBPS/style.css").decode("utf-8")
            self.assertEqual(page.count("<article"), manifest["items"])
            self.assertEqual(page.count("<h2 "), manifest["sections"])
            self.assertIn('id="g-flash"', page)
            self.assertIn('href="report.xhtml#g-flash"', nav)
            self.assertIn("新闻订阅", page)
            self.assertIn("article", style)          # 新闻版式替换了报告 CSS
            self.assertIn(".meta", style)

    def test_english_items_carry_chinese(self):
        data = self.payload()
        data["items"][0].update({"lang": "en", "title": "Fed cuts rates by 25bp",
                                 "summary": "The Fed lowered rates.",
                                 "title_zh": "美联储降息 25 个基点",
                                 "summary_zh": "美联储下调利率。"})
        with tempfile.TemporaryDirectory() as folder:
            manifest, paths = build_news.build(data, Path(folder) / "site",
                                               min_items=5, now=NOW)
            text = paths["text"].read_text(encoding="utf-8")
            with zipfile.ZipFile(paths["epub"]) as book:
                page = book.read("OEBPS/report.xhtml").decode("utf-8")
        self.assertIn("美联储降息 25 个基点", text)
        self.assertIn("美联储降息 25 个基点", page)
        self.assertIn("原文：The Fed lowered rates.", text)
        self.assertEqual(manifest["english"], 2)      # 第二条源标 en
        self.assertEqual(manifest["zh_added"], 1)
        self.assertEqual(manifest["zh_missing"], 1)

    def test_too_few_items_refuses_to_build(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                build_news.build(self.payload(count=3), Path(folder) / "site", min_items=10, now=NOW)
            self.assertFalse((Path(folder) / "site" / "news.epub").exists())

    def test_empty_payload_refuses_to_build(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                build_news.build({"items": [], "sources": []}, Path(folder) / "site",
                                 min_items=1, now=NOW)

    def test_garbled_body_refuses_to_build(self):
        payload = self.payload()
        for item in payload["items"]:
            item["title"] = "锟斤拷" * 6 + "财经"
            item["summary"] = "锟斤拷" * 6
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                build_news.build(payload, Path(folder) / "site", min_items=5, now=NOW)


class ManifestForPluginTest(unittest.TestCase):
    def test_manifest_has_the_fields_the_plugin_reads(self):
        payload = {"generated": "2026-11-07T03:00:00+00:00",
                   "sources": [{"key": "CLS", "ok": True}],
                   "items": [{"title": f"第 {index} 条财经新闻标题",
                              "published": "2026-11-07T01:00:00Z",
                              "summary": "", "source": "CLS", "group": "flash"}
                             for index in range(10)]}
        with tempfile.TemporaryDirectory() as folder:
            manifest, _paths = build_news.build(payload, Path(folder) / "site", now=NOW)
            saved = json.loads((Path(folder) / "site" / "news.json").read_text(encoding="utf-8"))
        for key in ("generated", "doc", "data_time", "data_time_kind", "bytes"):
            self.assertTrue(saved.get(key), key)
        self.assertEqual(saved["generated"], manifest["generated"])


class PluginNewsModeTest(unittest.TestCase):
    """插件那半边没有 Lua 运行时可跑，这里只守住「两个书源共用一套流程」的结构。"""

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "octopus004.koplugin" / "main.lua").read_text(encoding="utf-8")

    def test_news_is_a_selectable_display_mode(self):
        self.assertIn("function Report:isNewsMode()", self.source)
        self.assertIn("function Report:isBookMode()", self.source)
        self.assertIn('text = "News subscription", callback = function() pick("news") end', self.source)
        self.assertIn('if mode == "news" then return "news" end', self.source)

    def test_both_books_share_the_download_pipeline(self):
        # 下载、打开、翻页、清理都走 spec，不能写死报告那套设置键。
        self.assertIn("function Report:refreshDoc(kind)", self.source)
        self.assertIn("self:refreshDoc(self:docKind())", self.source)
        for call in ("self:turnDocPage(spec)", "self:openDoc(spec, target)",
                     "self:pruneDocs(name, spec.prefix)", "self:docPath(spec, name)"):
            with self.subTest(call=call):
                self.assertIn(call, self.source)

    def test_report_paths_are_not_hard_coded(self):
        for leftover in ("self.settings.doc_file =", "self.settings.doc_sig =",
                         "self.settings.doc_seq =", "self.settings.doc_file_name =",
                         "self.settings.doc_opened_at =", "DOC_PREFIX .. "):
            with self.subTest(leftover=leftover):
                self.assertNotIn(leftover, self.source)

    def test_news_uses_its_own_cache_prefix_and_settings(self):
        self.assertIn('NEWS_PREFIX = "octopus004-news-"', self.source)
        self.assertIn('manifest = "news_url"', self.source)
        self.assertNotIn('NEWS_PREFIX = "octopus004-report-"', self.source)

    def test_status_does_not_fall_back_to_report_data_time(self):
        # `a and b or c` 在 b 为 nil 时会回落到 c，新闻模式就会显示报告的数据时间。
        self.assertIn("stamp_setting", self.source)
        self.assertNotIn("isBookMode() and self.settings[spec.data_time]", self.source)


if __name__ == "__main__":
    unittest.main()
