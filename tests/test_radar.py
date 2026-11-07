"""RSSHub Radar：找不到路由时怎么发现源，以及抓取器怎么用这个发现。

规则形状取自 RSSHub 的 `lib/api/radar/rules/utils.ts`：
`{域名: {"_name": 站名, 子域|".": [{title, target, source, docs}]}}`。
测试全用假报文，不打网络。
"""

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fetch_news  # noqa: E402
import news_sources  # noqa: E402
import radar_discover  # noqa: E402

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)

# 真实 API 的形状：www 子域与裸域是两组规则，`_name` 是站点名。
RULES = {
    "wallstreetcn.com": {
        "_name": "华尔街见闻",
        "www": [{"title": "实时快讯", "target": "/wallstreetcn/live/:category?",
                 "source": ["/live/global", "/"],
                 "docs": "https://docs.rsshub.app/routes/wallstreetcn"}],
        ".": [{"title": "要闻", "target": "/wallstreetcn/news/global",
               "source": ["/news"]}],
    },
    "hexun.com": {
        "_name": "和讯网",
        ".": [{"title": "私募新闻", "target": "/hexun/pe/news", "source": ["/pe/news"]}],
    },
}

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Radar 找到的一条新闻</title><link>https://example.com/1</link>
<pubDate>Fri, 07 Nov 2026 01:00:00 +0800</pubDate></item>
</channel></rss>"""

PAGE = """<html><head>
<link rel="stylesheet" href="/a.css">
<link rel="alternate" type="application/rss+xml" title="全站 RSS" href="/feed.xml">
<link rel="alternate" type="application/atom+xml" title="Atom" href="https://blog.example.com/atom.xml">
<link rel="alternate" type="application/feed+json" title="JSON Feed" href="/feed.json">
<link rel="alternate" type="text/html" hreflang="en" href="/en/">
</head><body>hi</body></html>"""


def fake_fetch(pages: dict):
    """按 URL 片段返回报文；`pages` 的值可以是 bytes / str / dict（转 JSON）/ 异常。"""
    def fetch(url, timeout=15):
        for fragment, payload in pages.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, dict):
                    return json.dumps(payload).encode()
                return payload.encode() if isinstance(payload, str) else payload
        raise OSError("HTTP Error 404: Not Found")
    return fetch


class HostTest(unittest.TestCase):
    def test_registrable_domain_is_what_the_api_wants(self):
        self.assertEqual(radar_discover.registrable_domain("https://www.10jqka.com.cn/"),
                         "10jqka.com.cn")
        self.assertEqual(radar_discover.registrable_domain("https://www.wallstreetcn.com/"),
                         "wallstreetcn.com")

    def test_hostname_keeps_www_because_radar_keys_by_subdomain(self):
        self.assertEqual(radar_discover.hostname("https://www.caixin.com/latest"), "www.caixin.com")
        self.assertEqual(radar_discover.hostname("caixin.com"), "caixin.com")
        self.assertEqual(radar_discover.hostname("http://money.163.COM/a"), "money.163.com")
        self.assertEqual(radar_discover.hostname(""), "")

    def test_domain_candidates_from_specific_to_general(self):
        self.assertEqual(radar_discover.domain_candidates("a.b.caixin.com"),
                         ["a.b.caixin.com", "b.caixin.com", "caixin.com"])
        self.assertEqual(radar_discover.domain_candidates("caixin.com"), ["caixin.com"])

    def test_domain_candidates_stop_before_public_suffix(self):
        # `10jqka.com.cn` 是站点，不能退到 `com.cn`。
        self.assertNotIn("com.cn", radar_discover.domain_candidates("www.10jqka.com.cn"))
        self.assertEqual(radar_discover.domain_candidates("www.10jqka.com.cn")[-1], "10jqka.com.cn")
        self.assertNotIn("co.uk", radar_discover.domain_candidates("news.bbc.co.uk"))

    def test_subdomain_of(self):
        self.assertEqual(radar_discover.subdomain_of("www.caixin.com", "caixin.com"), "www")
        self.assertEqual(radar_discover.subdomain_of("caixin.com", "caixin.com"), ".")
        self.assertEqual(radar_discover.subdomain_of("elsewhere.com", "caixin.com"), ".")


class RadarRulesTest(unittest.TestCase):
    def test_www_subdomain_wins_over_bare_domain(self):
        routes = radar_discover.radar_routes(RULES, "https://www.wallstreetcn.com/")
        self.assertEqual([route["target"] for route in routes], ["/wallstreetcn/live/:category?"])
        self.assertEqual(routes[0]["subdomain"], "www")
        self.assertEqual(routes[0]["site_name"], "华尔街见闻")

    def test_bare_domain_uses_dot_rules(self):
        routes = radar_discover.radar_routes(RULES, "https://wallstreetcn.com/")
        self.assertEqual([route["target"] for route in routes], ["/wallstreetcn/news/global"])

    def test_unknown_subdomain_falls_back_to_dot(self):
        routes = radar_discover.radar_routes(RULES, "https://m.hexun.com/")
        self.assertEqual([route["target"] for route in routes], ["/hexun/pe/news"])

    def test_unknown_domain_returns_nothing(self):
        self.assertEqual(radar_discover.radar_routes(RULES, "https://nothing.example/"), [])

    def test_entry_fields_are_normalized(self):
        route = radar_discover.radar_routes(RULES, "https://www.wallstreetcn.com/")[0]
        self.assertEqual(route["source"], ["/live/global", "/"])
        self.assertIn("docs.rsshub.app", route["docs"])

    def test_domain_rules_accepts_both_api_shapes(self):
        node = RULES["hexun.com"]
        self.assertEqual(radar_discover.domain_rules(node, "hexun.com"), {"hexun.com": node})
        self.assertEqual(radar_discover.domain_rules({"hexun.com": node}, "hexun.com"),
                         {"hexun.com": node})
        self.assertEqual(radar_discover.domain_rules({}, "hexun.com"), {})


class NativeFeedTest(unittest.TestCase):
    def test_rss_atom_and_json_links_are_all_found(self):
        feeds = radar_discover.native_feeds(PAGE, "https://example.com/section/")
        self.assertEqual([feed["feed"] for feed in feeds],
                         ["https://example.com/feed.xml",
                          "https://blog.example.com/atom.xml",
                          "https://example.com/feed.json"])
        self.assertEqual(feeds[0]["title"], "全站 RSS")

    def test_plain_html_alternate_is_ignored(self):
        feeds = radar_discover.native_feeds(PAGE, "https://example.com/")
        self.assertNotIn("https://example.com/en/", [feed["feed"] for feed in feeds])

    def test_duplicate_links_are_collapsed(self):
        page = ('<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
                '<link rel="alternate" type="application/rss+xml" href="/feed.xml">')
        self.assertEqual(len(radar_discover.native_feeds(page, "https://example.com/")), 1)

    def test_page_without_feeds(self):
        self.assertEqual(radar_discover.native_feeds("<html><body>x</body></html>",
                                                    "https://example.com/"), [])


class DiscoverTest(unittest.TestCase):
    def test_rules_hit_reports_radar(self):
        result = radar_discover.discover("https://www.wallstreetcn.com/", rules=RULES,
                                        fetch=fake_fetch({}))
        self.assertEqual(result["method"], "radar")
        self.assertEqual(result["domain"], "wallstreetcn.com")
        self.assertEqual(result["routes"][0]["target"], "/wallstreetcn/live/:category?")

    def test_no_rules_falls_back_to_native_feed(self):
        result = radar_discover.discover(
            "https://no-rule.example/start",
            fetch=fake_fetch({"/api/radar/rules/": {}, "no-rule.example/start": PAGE}))
        self.assertEqual(result["method"], "native")
        self.assertEqual(result["feeds"][0]["feed"], "https://no-rule.example/feed.xml")

    def test_neither_rule_nor_feed(self):
        result = radar_discover.discover(
            "https://empty.example/",
            fetch=fake_fetch({"/api/radar/rules/": {}, "empty.example": "<html></html>"}))
        self.assertEqual(result["method"], "none")
        self.assertEqual(result["routes"], [])
        self.assertEqual(result["feeds"], [])

    def test_page_fetch_error_is_not_fatal(self):
        result = radar_discover.discover(
            "https://broken.example/",
            fetch=fake_fetch({"/api/radar/rules/": {}, "broken.example": OSError("boom")}))
        self.assertEqual(result["method"], "none")

    def test_single_domain_endpoint_shape(self):
        # 逐域名接口直接返回域名节点本身（没有域名那一层）。
        result = radar_discover.discover(
            "https://www.hexun.com/",
            fetch=fake_fetch({"/api/radar/rules/hexun.com": RULES["hexun.com"]}))
        self.assertEqual(result["method"], "radar")
        self.assertEqual(result["routes"][0]["target"], "/hexun/pe/news")


class SuggestTest(unittest.TestCase):
    def test_suggestion_text(self):
        suggestions = radar_discover.suggest_routes(
            "https://www.wallstreetcn.com/",
            fetch=fake_fetch({"/api/radar/rules/wallstreetcn.com": RULES["wallstreetcn.com"]}))
        self.assertEqual(suggestions, ["/wallstreetcn/live/:category?（实时快讯）"])

    def test_api_failure_is_silent(self):
        self.assertEqual(radar_discover.suggest_routes(
            "https://www.wallstreetcn.com/",
            fetch=fake_fetch({"/api/radar/": OSError("HTTP Error 403")})), [])

    def test_empty_body_is_silent(self):
        self.assertEqual(radar_discover.suggest_routes(
            "https://www.wallstreetcn.com/",
            fetch=fake_fetch({"/api/radar/": b""})), [])


class SnippetTest(unittest.TestCase):
    def test_route_snippet(self):
        result = radar_discover.discover("https://www.hexun.com/", rules=RULES, fetch=fake_fetch({}))
        snippet = radar_discover.source_snippet(result, key="HEXUN", group="depth")
        self.assertEqual(snippet["route"], "/hexun/pe/news")
        self.assertEqual(snippet["key"], "HEXUN")
        self.assertIn("Radar 发现", snippet["note"])
        self.assertNotIn("feed", snippet)

    def test_native_snippet(self):
        result = {"site": "https://no-rule.example/", "domain": "no-rule.example",
                  "routes": [], "feeds": [{"feed": "https://no-rule.example/feed.xml",
                                           "title": "全站 RSS", "type": "application/rss+xml"}]}
        snippet = radar_discover.source_snippet(result, key="X")
        self.assertEqual(snippet["feed"], "https://no-rule.example/feed.xml")
        self.assertNotIn("route", snippet)

    def test_nothing_found_gives_empty_snippet(self):
        self.assertEqual(radar_discover.source_snippet(
            {"site": "https://x/", "routes": [], "feeds": []}, key="X"), {})


class CatalogUsesRadarFieldsTest(unittest.TestCase):
    def test_every_source_declares_site_and_one_url_kind(self):
        for source in news_sources.SOURCES:
            with self.subTest(source=source["key"]):
                self.assertTrue(source.get("site"), "每条源要有 site，Radar 才能给建议")
                self.assertTrue(bool(source.get("route")) ^ bool(source.get("feed")),
                                "route 与 feed 必须二选一")

    def test_catalog_check_catches_both_url_kinds(self):
        original = news_sources.SOURCES
        try:
            news_sources.SOURCES = [{"key": "X", "name": "x", "group": "flash",
                                     "route": "/x", "feed": "https://x/feed.xml"}]
            problems = news_sources.catalog_errors()
            self.assertTrue(any("二选一" in problem for problem in problems), problems)
            news_sources.SOURCES = [{"key": "X", "name": "x", "group": "flash"}]
            problems = news_sources.catalog_errors()
            self.assertTrue(any("二选一" in problem for problem in problems), problems)
            news_sources.SOURCES = [{"key": "X", "name": "x", "group": "flash",
                                     "feed": "http://x/feed.xml"}]
            problems = news_sources.catalog_errors()
            self.assertTrue(any("HTTPS" in problem for problem in problems), problems)
        finally:
            news_sources.SOURCES = original


class FetcherUsesFeedAndRadarTest(unittest.TestCase):
    def test_native_feed_is_fetched_without_instance_prefix(self):
        calls = []
        source = {"key": "NATIVE", "name": "原生 RSS", "group": "depth", "lang": "zh",
                  "feed": "https://native.example/feed.xml", "site": "native.example"}

        def fetch(url, timeout=15):
            calls.append(url)
            return RSS

        items, reports = fetch_news.collect(
            sources=[source], instances=["https://rsshub.rss.tips"], fetch=fetch,
            now=NOW, max_age_hours=24, log=lambda *_: None)
        self.assertEqual(calls, ["https://native.example/feed.xml"])
        self.assertEqual(len(items), 1)
        self.assertEqual(reports[0]["via"], "feed")
        self.assertTrue(reports[0]["ok"])

    def test_failed_feed_does_not_trip_instances(self):
        source = {"key": "NATIVE", "name": "原生 RSS", "group": "depth", "lang": "zh",
                  "feed": "https://native.example/feed.xml", "site": "native.example"}
        items, reports = fetch_news.collect(
            sources=[source], instances=["https://rsshub.rss.tips"],
            fetch=fake_fetch({"native.example": OSError("HTTP Error 500")}),
            now=NOW, max_age_hours=24, log=lambda *_: None)
        self.assertEqual(items, [])
        self.assertFalse(reports[0]["ok"])
        self.assertIn("native feed", reports[0]["error"])

    def test_failure_asks_radar_for_a_replacement_route(self):
        source = {"key": "WSCN", "name": "华尔街见闻", "group": "flash",
                  "route": "/wallstreetcn/live/global", "site": "wallstreetcn.com"}
        items, reports = fetch_news.collect(
            sources=[source], instances=["https://rsshub.rss.tips"],
            fetch=fake_fetch({"/wallstreetcn/": OSError("HTTP Error 404"),
                              "/api/radar/rules/wallstreetcn.com": RULES["wallstreetcn.com"]}),
            now=NOW, max_age_hours=24, radar_base="https://rsshub.rss.tips",
            log=lambda *_: None)
        self.assertEqual(items, [])
        self.assertFalse(reports[0]["ok"])
        # 源里写的是裸域 `wallstreetcn.com`，Radar 的 `"."` 规则最贴近——www 子域归浏览器地址管。
        self.assertEqual(reports[0]["radar"], ["/wallstreetcn/news/global（要闻）"])

    def test_radar_is_off_by_default_and_when_no_site(self):
        source = {"key": "NOSITE", "name": "没有域名", "group": "flash",
                  "route": "/whatever/route"}
        _items, reports = fetch_news.collect(
            sources=[source], instances=["https://rsshub.rss.tips"],
            fetch=fake_fetch({"/whatever/": OSError("HTTP Error 404")}),
            now=NOW, max_age_hours=24, log=lambda *_: None)
        self.assertEqual(reports[0]["radar"], [])
        self.assertEqual(reports[0]["via"], "instance")


if __name__ == "__main__":
    unittest.main()
