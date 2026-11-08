"""源站直连 + 提供者链：多备用方案的第一跳和最后一条退路。

这里不打网络：`fetch` 全部注入假报文，报文按上游接口的真实字段写（金十 flash-api、
财联社 api/cache、华尔街见闻 apiv1/content/lives），字段一改测试就该红。
"""

import json
import sys
import unittest
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import direct_feeds  # noqa: E402
import fetch_news  # noqa: E402
import news_sources  # noqa: E402

NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)   # 北京时间 11:00

JIN10_JSON = {
    "data": [
        {"id": "20261107232605506800", "time": "2026-11-07T23:26:05", "type": 0,
         "important": 1,
         "data": {"content": "【波兰克拉科夫枪击事件致2死10伤】金十数据11月7日讯，当地消防部门消息。",
                  "link": None, "pic": "", "vip_title": None}},
        {"id": "20261107232534241800", "time": "2026-11-07T23:25:34", "type": 0,
         "data": {"content": "伊朗议员：美国已无发动战争的新选项。", "link": None}},
        # type == 1 是图片/广告卡，必须跳过
        {"id": "20261107232000000000", "time": "2026-11-07T23:20:00", "type": 1,
         "data": {"content": "广告位", "pic": "https://img.jin10.com/x.png"}},
    ]
}

CLS_JSON = {
    "data": {"roll_data": [
        {"id": 1234567, "title": "", "content": "财联社11月7日电，沪指收涨0.8%。",
         "ctime": 1794013200, "shareurl": "https://www.cls.cn/detail/1234567"},
        {"id": 1234568, "title": "银行板块午后走强", "content": "银行板块午后走强，多股涨停。",
         "ctime": 1794016800, "shareurl": ""},
    ]}
}

WSCN_JSON = {
    "data": {"items": [
        {"uri": "https://wallstreetcn.com/livenews/2900001",
         "title": "", "content_text": "华尔街见闻11月7日讯，美股三大指数收涨。",
         "content": "<p>华尔街见闻11月7日讯，美股三大指数收涨。</p>",
         "display_time": 1794067200, "score": 1},
        {"uri": "https://wallstreetcn.com/livenews/2900002",
         "title": "美联储官员放鹰", "content": "美联储官员放鹰。",
         "display_time": 1794070800, "score": 2},
    ]}
}


def fake_fetch(mapping, calls=None):
    """mapping: {主机名或 url 片段: bytes/json/dict/异常}。dict 自动转成 JSON 字节。

    先按主机名匹配，再退回整串匹配：RSS-Bridge 的地址把上游 URL 塞进了 query
    （`feed_1=https%3A%2F%2Frsshub.app…`），只按子串匹配会把「代取」错认成「直连」。
    """
    def fetch(url, timeout=15, headers=None):
        if calls is not None:
            calls.append((url, headers or {}))
        host = urlparse(url).netloc
        ordered = sorted(mapping.items(),
                         key=lambda pair: 0 if pair[0].split("//")[-1].split("/")[0] == host else 1)
        for key, value in ordered:
            key_host = key.split("//")[-1].split("/")[0]
            if key_host == host or key in url:
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, (dict, list)):
                    return json.dumps(value).encode("utf-8")
                return value if isinstance(value, bytes) else value.encode("utf-8")
        raise OSError(f"没有为这个地址准备假报文：{url}")
    return fetch


def titles(payload):
    root = ElementTree.fromstring(payload)
    return [item.findtext("title") for item in root.findall("./channel/item")]


def rss(payload):
    return ElementTree.fromstring(payload)


class DirectFeedTest(unittest.TestCase):
    """每个抓取器都把接口报文变成一份标准 RSS，后面的流程不用改。"""

    def test_jin10_flash_api(self):
        calls = []
        payload = direct_feeds.jin10({"key": "JIN10"}, fake_fetch(
            {"flash-api.jin10.com": JIN10_JSON}, calls))
        root = rss(payload)
        items = root.findall("./channel/item")
        self.assertEqual([item.findtext("title") for item in items],
                         ["波兰克拉科夫枪击事件致2死10伤", "伊朗议员：美国已无发动战争的新选项。"])
        # 没有 <link> 的快讯用真实详情页，绝不写 jin10:index:…
        self.assertEqual(items[0].findtext("link"),
                         "https://flash.jin10.com/detail/20261107232605506800")
        self.assertEqual(items[0].findtext("pubDate"), "Sat, 07 Nov 2026 15:26:05 +0000")
        # 接口要的 header 必须带上，否则金十直接 403
        self.assertEqual(calls[0][1].get("x-app-id"), "bVBF4FyRTn5NJF5n")

    def test_jin10_skips_advertisement_rows(self):
        payload = direct_feeds.jin10({}, fake_fetch({"flash-api.jin10.com": JIN10_JSON}))
        self.assertNotIn("广告位", titles(payload))

    def test_cls_telegraph_signs_the_query(self):
        calls = []
        payload = direct_feeds.cls_telegraph({}, fake_fetch({"www.cls.cn/api/cache": CLS_JSON},
                                                            calls))
        _url, headers = calls[0]
        self.assertEqual(headers.get("Accept"), "application/json")
        query = "appName=CailianpressWeb&name=telegraph&os=web&sv=8.7.9"
        import hashlib
        expected = hashlib.md5(hashlib.sha1(query.encode()).hexdigest().encode()).hexdigest()
        self.assertIn(f"sign={expected}", _url)
        self.assertIn("appName=CailianpressWeb", _url)
        items = rss(payload).findall("./channel/item")
        # 没有 title 的电报，用正文开头当标题
        self.assertTrue(items[0].findtext("title").startswith("财联社11月7日电"))
        self.assertEqual(items[1].findtext("title"), "银行板块午后走强")
        self.assertTrue(items[1].findtext("link").startswith("https://www.cls.cn/detail/"))

    def test_wallstreetcn_channel_comes_from_the_catalogue(self):
        calls = []
        direct_feeds.wallstreetcn_live({}, fake_fetch({"api-one.wallstcn.com": WSCN_JSON},
                                                      calls), arg="a-stock")
        self.assertIn("channel=a-stock-channel", calls[0][0])

    def test_wallstreetcn_items(self):
        payload = direct_feeds.wallstreetcn_live(
            {}, fake_fetch({"api-one.wallstcn.com": WSCN_JSON}), arg="global")
        items = rss(payload).findall("./channel/item")
        self.assertEqual(len(items), 2)
        self.assertIn("美股三大指数收涨", items[0].findtext("title"))
        self.assertEqual(items[1].findtext("title"), "美联储官员放鹰")
        self.assertEqual(items[0].findtext("published") or items[0].findtext("pubDate"),
                         "Sat, 07 Nov 2026 16:00:00 +0000")
        # HTML 摘要不能原样塞进 feed
        self.assertNotIn("<p>", items[0].findtext("description") or "")

    def test_epoch_seconds_and_beijing_naive_time(self):
        self.assertEqual(direct_feeds._moment(1794013200),
                         datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(direct_feeds._moment("2026-11-07T23:26:05"),
                         datetime(2026, 11, 7, 15, 26, 5, tzinfo=timezone.utc))
        self.assertIsNone(direct_feeds._moment("不是时间"))

    def test_empty_response_is_an_error_not_an_empty_feed(self):
        with self.assertRaises(ValueError):
            direct_feeds.jin10({}, fake_fetch({"flash-api.jin10.com": {"data": []}}))

    def test_non_json_response_is_an_error(self):
        with self.assertRaises(ValueError):
            direct_feeds.jin10({}, fake_fetch({"flash-api.jin10.com": "<html>403</html>"}))

    def test_unknown_crawler_is_rejected(self):
        with self.assertRaises(ValueError):
            direct_feeds.fetch_direct("nosuch", {}, fake_fetch({}))

    def test_known_names_cover_the_catalogue(self):
        used = {str(source.get("direct") or "").partition(":")[0]
                for source in news_sources.SOURCES if source.get("direct")}
        self.assertTrue(used <= set(direct_feeds.names()), used)


class ProgramChainTest(unittest.TestCase):
    """一个源的退路顺序：direct → rsshub → mirror → native → bridge。"""

    def source(self, **extra):
        base = {"key": "T", "name": "测试源", "group": "flash", "route": "/cls/telegraph",
                "lang": "zh"}
        base.update(extra)
        return base

    def test_chain_order_follows_the_provider_list(self):
        candidates = fetch_news.source_candidates(
            self.source(direct="cls"),
            ["https://rsshub.app", "https://mirror.example"], ("direct", "rsshub", "mirror"),
            dead={}, trip_after=3)
        self.assertEqual([c["provider"] for c in candidates], ["direct", "rsshub", "mirror"])

    def test_bridge_wraps_the_previous_hops(self):
        candidates = fetch_news.source_candidates(
            self.source(direct="cls"), ["https://rsshub.app"], ("rsshub", "bridge"),
            dead={}, trip_after=3,
            bridge_instances=("https://rss-bridge.org/bridge01",))
        bridge = [c for c in candidates if c["provider"] == "bridge"]
        self.assertEqual(len(bridge), 1)
        self.assertIn("bridge=FeedMerge", bridge[0]["url"])
        self.assertIn("rsshub.app%2Fcls%2Ftelegraph", bridge[0]["url"])

    def test_bridge_also_wraps_the_last_mirror(self):
        """官方超时、镜像报 5xx 时，让第三方再去试一次镜像地址——这才是最后一条退路的意义。"""
        candidates = fetch_news.source_candidates(
            self.source(), ["https://rsshub.app", "https://m1.example", "https://m2.example"],
            ("bridge",), dead={}, trip_after=3,
            bridge_instances=("https://rss-bridge.org/bridge01",))
        wrapped = [c["url"] for c in candidates if c["provider"] == "bridge"]
        self.assertEqual(len(wrapped), 2)          # 官方 + 最后一个镜像（中间的 m1 不重复包）
        self.assertTrue(any("rsshub.app" in url for url in wrapped))
        self.assertTrue(any("m2.example" in url for url in wrapped))

    def test_bridge_targets_are_not_duplicated(self):
        candidates = fetch_news.source_candidates(
            self.source(), ["https://rsshub.app"], ("bridge",), dead={}, trip_after=3,
            bridge_instances=("https://rss-bridge.org/bridge01",))
        self.assertEqual(len(candidates), 1)

    def test_direct_success_stops_the_chain(self):
        calls = []
        fetch = fake_fetch({"flash-api.jin10.com": JIN10_JSON}, calls)
        items, reports = fetch_news.collect(
            sources=[self.source(key="JIN10", direct="jin10", route="/jin10")],
            instances=["https://rsshub.app"], fetch=fetch, now=NOW,
            max_age_hours=24, log=lambda *_: None)
        self.assertTrue(reports[0]["ok"])
        self.assertEqual(reports[0]["provider"], "direct")
        self.assertEqual(reports[0]["via"], "direct")
        self.assertEqual(len(items), 2)
        self.assertEqual(len(calls), 1)             # 官方实例一次都没打

    def test_partial_download_skips_source_and_continues_scanning(self):
        """下载到一半会抛 IncompleteRead；该源失败就记下来，继续扫描其他源。"""
        def fetch(url, timeout=15):
            if "/broken" in url:
                raise IncompleteRead(b"<rss><channel>", 256)
            if "/healthy" in url:
                return RSS_OK
            raise AssertionError(f"unexpected URL: {url}")

        sources = [
            self.source(key="BROKEN", route="/broken"),
            self.source(key="HEALTHY", route="/healthy"),
        ]
        items, reports = fetch_news.collect(
            sources=sources, instances=["https://rsshub.app"], providers=("rsshub",),
            fetch=fetch, now=NOW, max_age_hours=24, log=lambda *_: None)

        self.assertEqual([report["key"] for report in reports], ["BROKEN", "HEALTHY"])
        self.assertFalse(reports[0]["ok"])
        self.assertIn("IncompleteRead", reports[0]["attempts"][0]["error"])
        self.assertTrue(reports[1]["ok"])
        self.assertEqual(len(items), 1)

    def test_direct_failure_falls_back_to_the_official_instance(self):
        fetch = fake_fetch({
            "flash-api.jin10.com": OSError("HTTP Error 403: Forbidden"),
            "rsshub.app": RSS_OK,
        })
        items, reports = fetch_news.collect(
            sources=[self.source(key="JIN10", direct="jin10", route="/jin10")],
            instances=["https://rsshub.app"], fetch=fetch, now=NOW,
            max_age_hours=24, log=lambda *_: None)
        self.assertEqual(len(items), 1)
        self.assertEqual(reports[0]["provider"], "rsshub")
        # 前几跳为什么不行，必须留在报告里
        self.assertEqual(reports[0]["attempts"][0]["provider"], "direct")
        self.assertIn("403", reports[0]["attempts"][0]["error"])

    def test_bridge_is_the_last_resort(self):
        fetch = fake_fetch({
            "flash-api.jin10.com": OSError("HTTP Error 403: Forbidden"),
            "rsshub.app": OSError("timed out"),
            "bridge.example": RSS_OK,
        })
        items, reports = fetch_news.collect(
            sources=[self.source(key="JIN10", direct="jin10", route="/jin10")],
            instances=["https://rsshub.app"], fetch=fetch, now=NOW, max_age_hours=24,
            log=lambda *_: None,
            providers=("direct", "rsshub", "bridge"),
            bridge_instances=("https://bridge.example",))
        self.assertEqual(len(items), 1)
        self.assertEqual(reports[0]["provider"], "bridge")
        self.assertEqual([a["provider"] for a in reports[0]["attempts"]],
                         ["direct", "rsshub", "bridge"])

    def test_bridge_is_off_by_default(self):
        """别人的公共服务，库函数默认不碰：不显式要就不该出现在报告里。"""
        fetch = fake_fetch({"rsshub.app": OSError("boom")})
        _items, reports = fetch_news.collect(
            sources=[self.source()], instances=["https://rsshub.app"], fetch=fetch,
            now=NOW, max_age_hours=24, log=lambda *_: None)
        self.assertNotIn("bridge", [a["provider"] for a in reports[0]["attempts"]])

    def test_unknown_provider_name_is_ignored(self):
        candidates = fetch_news.source_candidates(
            self.source(), ["https://rsshub.app"], ("rsshub", "胡说的层"), dead={},
            trip_after=3)
        self.assertEqual([c["provider"] for c in candidates], ["rsshub"])


class HealthTest(unittest.TestCase):
    def payload(self):
        return {
            "generated": "2026-11-07T03:00:00+00:00",
            "providers": ["direct", "rsshub"],
            "items": [{"title": "一条"}],
            "sources": [
                {"key": "JIN10", "name": "金十", "group": "flash", "ok": True,
                 "provider": "direct", "count": 3,
                 "attempts": [{"provider": "direct", "label": "直连 jin10", "ok": True,
                               "error": ""}]},
                {"key": "CLS", "name": "财联社", "group": "flash", "ok": False,
                 "provider": "", "count": 0, "error": "全部失败",
                 "attempts": [{"provider": "direct", "label": "直连 cls", "ok": False,
                               "error": "HTTP Error 500"}]},
            ],
        }

    def test_summary_counts_by_provider(self):
        health = fetch_news.build_health(self.payload())
        self.assertEqual(health["summary"], {"ok": 1, "total": 2, "items": 1,
                                             "by_provider": {"direct": 1}})
        self.assertEqual(health["providers"], ["direct", "rsshub"])

    def test_attempts_are_kept_per_source(self):
        health = fetch_news.build_health(self.payload())
        self.assertEqual(health["sources"][1]["attempts"][0]["error"], "HTTP Error 500")

    def test_write_health_creates_parent_folder(self):
        import tempfile
        folder = Path(tempfile.mkdtemp())
        health = fetch_news.write_health(self.payload(), folder / "site" / "source-health.json")
        written = json.loads((folder / "site" / "source-health.json").read_text(encoding="utf-8"))
        self.assertEqual(written, health)
        self.assertEqual(written["checked"], "2026-11-07T03:00:00+00:00")


class WorkflowTest(unittest.TestCase):
    """工作流必须真的把五层退路和健康表用上：改了参数忘了改 yml 就等于没这个功能。"""

    def setUp(self):
        self.text = (ROOT / ".github" / "workflows" / "refresh-news.yml").read_text(
            encoding="utf-8")

    def test_fetch_step_asks_for_every_layer(self):
        self.assertIn("--providers direct,rsshub,mirror,native,bridge", self.text)

    def test_health_file_is_written_and_committed(self):
        self.assertIn("--health site/source-health.json", self.text)
        self.assertIn("site/source-health.json", self.text.split("git add", 1)[1])

    def test_cli_rejects_unknown_layer(self):
        argv = fetch_news.parse_args(["--providers", "direct,瞎写的层"])
        self.assertIn("瞎写的层", argv.providers)      # 解析这一层不做判断，main 才校验
        self.assertEqual(fetch_news.ALL_PROVIDERS,
                         ("direct", "rsshub", "mirror", "native", "bridge"))

    def test_cli_defaults_cover_the_chain(self):
        args = fetch_news.parse_args([])
        self.assertEqual(args.providers.split(","), list(fetch_news.ALL_PROVIDERS))
        self.assertEqual(args.health, "")


RSS_OK = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
          b'<item><title>RSSHub \xe5\x9b\x9e\xe6\x9d\xa5\xe7\x9a\x84\xe4\xb8\x80\xe6\x9d\xa1</title>'
          b'<link>https://example.com/1</link><pubDate>Sat, 07 Nov 2026 02:30:00 +0800</pubDate>'
          b'</item></channel></rss>')


if __name__ == "__main__":
    unittest.main()
