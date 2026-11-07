"""插件这半边的联网接线：镜像、多地址、失败说明。

KOReader 的模块在这台机器上加载不了，所以这里守住的是「结构」：清单、EPUB、图片清单、
正文都必须走 `urlCandidates` + `httpGetAny`，不能有哪条路径绕开镜像直接用单个地址。
真正的映射逻辑由 net.lua 的测试（tests/test_net_lua.py）覆盖。
"""

import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "octopus004.koplugin"
MAIN = (PLUGIN / "main.lua").read_text(encoding="utf-8")


class NetWiringTest(unittest.TestCase):
    def test_net_module_is_loaded(self):
        self.assertIn('local Net = dofile(dir .. "net.lua")', MAIN)
        self.assertTrue((PLUGIN / "net.lua").is_file())

    def test_candidates_come_from_net_module(self):
        self.assertIn("return Net.candidates(configured)", MAIN)

    def test_every_manifest_goes_through_candidates(self):
        for setting in ('self.settings.url', 'self.settings[spec.manifest]',
                        'self.settings.image_url'):
            with self.subTest(setting=setting):
                self.assertIn(f"self:urlCandidates({setting}", MAIN)
        # 清单与 EPUB 都走「依次尝试」，不再有单地址的直连。
        self.assertNotIn("self:httpGet(self.settings.doc_url", MAIN)
        self.assertNotIn("self:httpGet(self.settings[spec.manifest]", MAIN)
        self.assertNotIn("self:httpGet(self.settings.image_url", MAIN)
        self.assertIn("self:httpGetAny(self:urlCandidates(url), DOC_MAX)", MAIN)

    def test_doc_url_is_resolved_against_the_url_that_worked(self):
        self.assertIn("decoded.base = used", MAIN)
        self.assertIn("resolveURL(manifest.base or self.settings[spec.manifest]", MAIN)

    def test_multiple_urls_are_accepted_and_validated(self):
        self.assertIn('url:gmatch("[^%s,;]+")', MAIN)
        self.assertIn("Every URL must be HTTPS", MAIN)

    def test_failures_are_explained(self):
        self.assertIn("Net.hint(", MAIN)
        self.assertIn("function Report:diagnose()", MAIN)
        self.assertIn('text = "Check URLs (network)"', MAIN)

    def test_not_modified_is_terminal_not_a_reason_to_try_mirrors(self):
        self.assertIn("if code == 304 then", MAIN)
        self.assertIn("304 是「没变」", MAIN)

    def test_etag_is_only_reused_for_the_same_url(self):
        self.assertIn("self.settings.etag_url == first", MAIN)
        self.assertIn("self.settings.etag_url = used", MAIN)

    def test_error_hint_is_not_a_bare_code(self):
        # 旧设备上看到的只是 "network failure"：现在至少要带地址与一句人话。
        self.assertNotIn('return nil, "network failure"', MAIN)
        self.assertIn("另 %d 个地址也失败", MAIN)

    def test_version_was_bumped(self):
        meta = (PLUGIN / "_meta.lua").read_text(encoding="utf-8")
        version = re.search(r'version = "([\d.]+)"', meta).group(1)
        self.assertGreaterEqual(tuple(int(part) for part in version.split(".")), (0, 5, 1))


if __name__ == "__main__":
    unittest.main()
