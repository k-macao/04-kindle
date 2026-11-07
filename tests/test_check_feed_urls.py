"""对外地址核对：多入口 feed 的状态表。

设备上「内容下载失败」时，第一件事是分清「地址错了」还是「这台设备那条线路不通」。
这里注入假 fetch 测判断逻辑（HTTP 码、非 feed 内容、超时），不联网。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_feed_urls  # noqa: E402

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>feed</title>
<item><title>一条新闻标题</title><guid>a</guid><pubDate>Sat, 07 Nov 2026 15:00:00 +0000</pubDate></item>
<item><title>另一条新闻标题</title><guid>b</guid><pubDate>Sat, 07 Nov 2026 14:00:00 +0000</pubDate></item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>feed</title>
<entry><title>atom 一条</title><id>x</id></entry>
</feed>
"""


def fetch_of(mapping):
    calls = []

    def fetch(url, timeout=15):
        calls.append(url)
        for fragment, payload in mapping.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload.encode() if isinstance(payload, str) else payload
        raise HTTPError(url, 404, "Not Found", {}, None)
    fetch.calls = calls
    return fetch


class CountTest(unittest.TestCase):
    def test_rss_items(self):
        self.assertEqual(check_feed_urls.count_items(RSS.encode()), 2)

    def test_atom_entries(self):
        self.assertEqual(check_feed_urls.count_items(ATOM.encode()), 1)

    def test_html_is_not_a_feed(self):
        # GitHub Pages 的 404 页是一段合法 HTML，能解析但不是 feed——必须识别出来。
        with self.assertRaises(ValueError):
            check_feed_urls.count_items(b"<html><body>404</body></html>")

    def test_empty_rss_counts_as_zero(self):
        self.assertEqual(
            check_feed_urls.count_items(b"<rss version='2.0'><channel></channel></rss>"), 0)


class CheckTest(unittest.TestCase):
    def test_ok(self):
        result = check_feed_urls.check("https://a.example/news.xml",
                                       fetch=fetch_of({"a.example": RSS}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["items"], 2)
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(result["error"], "")

    def test_http_error_is_reported_with_code(self):
        result = check_feed_urls.check("https://a.example/news.xml", fetch=fetch_of({}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 404)
        self.assertIn("404", result["error"])

    def test_network_error(self):
        result = check_feed_urls.check(
            "https://a.example/news.xml",
            fetch=fetch_of({"a.example": URLError("timed out")}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 0)
        self.assertIn("timed out", result["error"])

    def test_empty_feed_is_not_ok(self):
        result = check_feed_urls.check(
            "https://a.example/news.xml",
            fetch=fetch_of({"a.example": "<rss version='2.0'><channel></channel></rss>"}))
        self.assertFalse(result["ok"])
        self.assertIn("0 条", result["error"])

    def test_html_200_is_not_counted_as_feed(self):
        result = check_feed_urls.check(
            "https://a.example/news.xml",
            fetch=fetch_of({"a.example": "<html>404</html>"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertIn("不是 feed", result["error"])


class DefaultFetchTest(unittest.TestCase):
    """CLI 一路传下来的 fetch 是 None，不能把默认的 http_get 顶掉（踩过：直接 TypeError）。"""

    def test_none_falls_back_to_module_http_get(self):
        calls = []

        def fake_http_get(url, timeout=15):
            calls.append(url)
            return RSS.encode(), {"status": 200, "content_length": str(len(RSS.encode()))}

        original = check_feed_urls.http_get
        try:
            check_feed_urls.http_get = fake_http_get
            result = check_feed_urls.check("https://a.example/news.xml", fetch=None)
            self.assertTrue(result["ok"])
            self.assertEqual(calls, ["https://a.example/news.xml"])
            results = check_feed_urls.check_all(
                (("one", "第一", "https://a.example/news.xml"),), fetch=None, log=None)
            self.assertEqual(len(results), 1)
        finally:
            check_feed_urls.http_get = original


class KoreaderRuleTest(unittest.TestCase):
    """复现 KOReader 新聞下載器（epubdownloadbackend.lua）的四条判据。"""

    def test_content_length_mismatch_is_flagged(self):
        result = check_feed_urls.koreader_check(
            RSS.encode(), {"content_length": str(len(RSS.encode()) + 20)})
        self.assertFalse(result["ok"])
        self.assertIn("content-length", " ".join(result["notes"]))

    def test_chunked_is_flagged(self):
        result = check_feed_urls.koreader_check(
            RSS.encode(), {"transfer_encoding": "chunked"})
        self.assertFalse(result["ok"])
        self.assertIn("chunked", " ".join(result["notes"]))

    def test_gzip_is_flagged(self):
        result = check_feed_urls.koreader_check(RSS.encode(), {"content_encoding": "gzip"})
        self.assertFalse(result["ok"])
        self.assertIn("gzip", " ".join(result["notes"]))

    def test_plain_response_passes(self):
        result = check_feed_urls.koreader_check(
            RSS.encode(), {"content_length": str(len(RSS.encode())),
                          "content_encoding": "identity",
                          "transfer_encoding": "", "tls_issuer": "DigiCert Inc"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["notes"], [])
        self.assertEqual(result["issuer"], "DigiCert Inc")


class UrlsTest(unittest.TestCase):
    def test_three_files_times_hosts(self):
        urls = check_feed_urls.feed_urls()
        self.assertEqual(len(urls), len(check_feed_urls.HOSTS) * len(check_feed_urls.FILES))
        keys = [key for key, _label, _url in urls]
        self.assertIn("raw/news-lite.xml", keys)
        self.assertIn("release/news-test.xml", keys)
        raw = [url for key, _label, url in urls if key == "raw/news-lite.xml"][0]
        self.assertTrue(raw.endswith("/site/news-lite.xml"))


class CheckAllTest(unittest.TestCase):
    def test_every_entry_is_checked_and_labelled(self):
        urls = (("one", "第一", "https://one.example/news.xml"),
                ("two", "第二", "https://two.example/news.xml"))
        results = check_feed_urls.check_all(
            urls, fetch=fetch_of({"one.example": RSS}), log=None)
        self.assertEqual([item["key"] for item in results], ["one", "two"])
        self.assertEqual([item["ok"] for item in results], [True, False])
        stats = check_feed_urls.summary(results)
        self.assertEqual((stats["ok"], stats["checked"]), (1, 2))
        self.assertEqual(stats["urls"], ["https://one.example/news.xml"])


class CliTest(unittest.TestCase):
    def test_writes_report(self):
        folder = Path(tempfile.mkdtemp())
        target = folder / "site" / "feed-mirrors.json"
        code = check_feed_urls.main(["--write", str(target), "--only", "one"],
                                    fetch=fetch_of({"one.example": RSS}))
        # --only one 在这份默认列表里没有匹配项；用 --url 补一条再跑。
        self.assertEqual(code, 2)
        code = check_feed_urls.main(["--write", str(target), "--only", "jsdelivr"],
                                    fetch=fetch_of({"jsdelivr.net": RSS}))
        self.assertEqual(code, 0)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["ok"], payload["total"])      # 四个 jsDelivr 端点全过
        self.assertTrue(all(item["ok"] for item in payload["results"]))
        self.assertEqual(payload["results"][0]["items"], 2)

    def test_failure_exits_one(self):
        code = check_feed_urls.main(["--only", "raw"], fetch=fetch_of({}))
        self.assertEqual(code, 1)

    def test_internal_error_still_writes_report(self):
        folder = Path(tempfile.mkdtemp())
        target = folder / "feed.json"
        original = check_feed_urls.check_all

        def boom(*args, **kwargs):
            raise RuntimeError("模拟崩溃")

        try:
            check_feed_urls.check_all = boom
            code = check_feed_urls.main(["--only", "raw", "--write", str(target)])
        finally:
            check_feed_urls.check_all = original
        self.assertEqual(code, 1)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["ok"], 0)
        self.assertEqual(payload["results"][0]["key"], "internal")
        self.assertIn("模拟崩溃", payload["results"][0]["error"])

    def test_custom_url_is_checked(self):
        folder = Path(tempfile.mkdtemp())
        target = folder / "feed.json"
        code = check_feed_urls.main(["--only", "pages", "--url", "https://x.example/f.xml",
                                     "--write", str(target)],
                                    fetch=fetch_of({"x.example": RSS}))
        self.assertEqual(code, 0)          # pages 那条 404，但自定那条成功：有活的就不算失败
        payload = json.loads(target.read_text(encoding="utf-8"))
        custom = [item for item in payload["results"] if item["label"] == "自定地址"]
        self.assertTrue(custom[0]["ok"])
