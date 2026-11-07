"""插件里的 net.lua：GitHub raw ↔ jsDelivr 镜像映射、多地址解析、错误翻译。

net.lua 是纯 Lua（不 require 任何 KOReader 模块），所以这里直接用 Lua 运行时 dofile 它。
本机没装 Lua 运行时（CI 默认没有）时整组跳过——和 test_render.py 依赖 Pillow 一样。
"""

import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "octopus004.koplugin"

try:
    from lupa import LuaRuntime
except ImportError:                     # pragma: no cover - 取决于本机环境
    LuaRuntime = None

RAW = "https://raw.githubusercontent.com/k-macao/04-kindle/main/site/epub.json"
JSD = "https://cdn.jsdelivr.net/gh/k-macao/04-kindle@main/site/epub.json"


@unittest.skipUnless(LuaRuntime, "需要 lupa 才能执行 Lua（pip install lupa）")
class NetLuaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lua = LuaRuntime(unpack_returned_tuples=True)
        source = (PLUGIN / "net.lua").read_text(encoding="utf-8")
        cls.net = cls.lua.execute(source)

    def mirrors(self, url):
        return list(self.net.mirrors(url).values())

    def test_raw_url_expands_to_pages_and_jsdelivr_mirrors(self):
        urls = self.mirrors(RAW)
        self.assertEqual(urls[0], RAW)
        # Pages 优先：和 004 的报告同一个域名（k-macao.github.io）。
        self.assertEqual(urls[1], "https://k-macao.github.io/04-kindle/site/epub.json")
        self.assertIn(JSD, urls)
        self.assertIn("https://fastly.jsdelivr.net/gh/k-macao/04-kindle@main/site/epub.json", urls)
        self.assertIn("https://gcore.jsdelivr.net/gh/k-macao/04-kindle@main/site/epub.json", urls)

    def test_mirror_list_is_capped(self):
        self.assertLessEqual(len(self.mirrors(RAW)), self.net.MAX_MIRRORS)

    def test_jsdelivr_url_falls_back_to_raw(self):
        urls = self.mirrors(JSD)
        self.assertEqual(urls[0], JSD)
        self.assertIn(RAW, urls)
        self.assertIn("https://k-macao.github.io/04-kindle/site/epub.json", urls)
        self.assertEqual(len(urls), len(set(urls)), "候选地址不要重复")

    def test_non_github_url_is_left_alone(self):
        url = "https://example.com/site/epub.json"
        self.assertEqual(self.mirrors(url), [url])

    def test_empty_url_has_no_candidates(self):
        self.assertEqual(self.mirrors(""), [])

    def test_github_parts_for_both_forms(self):
        raw = self.net.githubParts(RAW)
        self.assertEqual((raw["owner"], raw["repo"], raw["ref"], raw["path"]),
                         ("k-macao", "04-kindle", "main", "site/epub.json"))
        jsd = self.net.githubParts(JSD)
        self.assertEqual((jsd["owner"], jsd["repo"], jsd["ref"], jsd["path"]),
                         ("k-macao", "04-kindle", "main", "site/epub.json"))

    def test_pages_url_for_a_user_site_repo(self):
        # k-macao.github.io 这种用户站没有仓库名那一段。
        urls = self.mirrors("https://raw.githubusercontent.com/k-macao/k-macao.github.io/main/index.html")
        self.assertIn("https://k-macao.github.io/index.html", urls)

    def test_github_parts_ignores_query_and_other_hosts(self):
        parts = self.net.githubParts(RAW + "?v=123")
        self.assertEqual(parts["path"], "site/epub.json")
        self.assertIsNone(self.net.githubParts("https://k-macao.github.io/04-kindle/site/epub.json"))

    def test_split_urls_accepts_spaces_commas_and_newlines(self):
        text = (RAW + "  " + JSD + ",\nhttps://k-macao.github.io/x.json;https://example.com/y")
        self.assertEqual(list(self.net.splitURLs(text).values()),
                         [RAW, JSD, "https://k-macao.github.io/x.json", "https://example.com/y"])

    def test_split_urls_drops_non_https_and_empties(self):
        self.assertEqual(list(self.net.splitURLs("http://a/b https://c/d").values()),
                         ["https://c/d"])
        self.assertEqual(list(self.net.splitURLs("").values()), [])
        self.assertEqual(list(self.net.splitURLs(None).values()), [])

    def test_candidates_put_declared_urls_first_then_mirrors(self):
        urls = list(self.net.candidates(RAW + " " + "https://mirror.example/x.json").values())
        self.assertEqual(urls[0], RAW)
        self.assertEqual(urls[1], "https://mirror.example/x.json")
        self.assertIn(JSD, urls)
        self.assertLessEqual(len(urls), self.net.MAX_MIRRORS)

    def test_candidates_dedupe_and_cap(self):
        text = " ".join([RAW, RAW, JSD])
        urls = list(self.net.candidates(text).values())
        self.assertEqual(len(urls), len(set(urls)))
        self.assertLessEqual(len(urls), self.net.MAX_MIRRORS)
        self.assertEqual(list(self.net.candidates("").values()), [])

    def test_bust_touches_only_jsdelivr(self):
        self.assertEqual(self.net.bust(RAW, 600), RAW)
        busted = self.net.bust(JSD, 600)
        self.assertTrue(busted.startswith(JSD + "?v="), busted)
        # 同一分钟内同一个串，跨分钟就换，尽量绕开 jsDelivr 的分支缓存。
        self.assertEqual(self.net.bust(JSD, 600), self.net.bust(JSD, 619))
        self.assertNotEqual(self.net.bust(JSD, 600), self.net.bust(JSD, 660))

    def test_bust_replaces_existing_query(self):
        self.assertNotIn("?v=1&", self.net.bust(JSD + "?v=1", 600))

    def test_hint_translates_common_failures(self):
        cases = {
            "network failure": "镜像",
            "timeout": "超时",
            "HTTP 404": "URL",
            "HTTP 503": "稍后",
            "handshake failed": "证书",
        }
        for error, expected in cases.items():
            with self.subTest(error=error):
                self.assertIn(expected, self.net.hint(error))

    def test_hint_passes_unknown_errors_through(self):
        self.assertEqual(self.net.hint("something odd"), "something odd")
        self.assertTrue(self.net.hint("").strip())


if __name__ == "__main__":
    unittest.main()
