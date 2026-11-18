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

    def test_beijing_is_defined_before_refresh(self):
        # Lua 的 local 只在声明点之后可见：finish() 在 refresh 里调 beijing()，
        # 定义在 refresh 之后就会被当成全局 nil，设备上直接闪退
        # （crash.log: attempt to call global 'beijing'）。
        lines = MAIN.splitlines()

        def find(needle):
            return next(i for i, line in enumerate(lines) if needle in line)

        self.assertLess(find("local function beijing"),
                        find("function Report:refresh("))
        self.assertLess(find("local function utcEpoch"),
                        find("local function isoSeconds"))

    def test_stocks_epub_goes_through_the_reader_pipeline(self):
        # 行情卡片版：清单里有 epub 就下载成书交给阅读器，失败才回退纯文本。
        self.assertIn("function Report:refreshStocksEpub(manifest, sig)", MAIN)
        self.assertIn("pcall(function() self:refreshStocksEpub(manifest, sig) end)", MAIN)
        self.assertIn("refreshStocks 回退", MAIN)
        self.assertIn("self:httpGetAny(self:urlCandidates(url), DOC_MAX)", MAIN)
        self.assertIn("self:pruneDocs(name, spec.prefix)", MAIN)
        # EPUB 与纯文本各自记签名，回退时不会把对方的缓存判成「没变」。
        self.assertIn('sig = "stocks_epub_sig"', MAIN)
        self.assertIn("self.settings.stocks_sig = sig", MAIN)

    def test_stocks_epub_prefix_does_not_collide_with_text_cache(self):
        # pruneDocs 按前缀删旧文件：卡片版前缀不能是纯文本缓存的超串，
        # 否则清旧 EPUB 时会把回退用的 octopus004-stocks.txt 一起删掉。
        prefix = re.search(r'local STOCKS_PREFIX = "([^"]+)"', MAIN).group(1)
        cache = re.search(r'local STOCKS_CACHE = "([^"]+)"', MAIN).group(1)
        self.assertFalse(cache.startswith(prefix), (prefix, cache))
        # 前缀也是 local：必须定义在 DOC_KINDS 之前才能被引用。
        self.assertLess(MAIN.index("local STOCKS_PREFIX"),
                        MAIN.index("local DOC_KINDS"))

    def test_stocks_mode_reopens_cached_card_without_network(self):
        # 已经有下载好的卡片版时，Show report 应该立刻交给阅读器，
        # 不能先显示纯文本、等下一轮 60 秒才变成卡片。
        start = MAIN[MAIN.index("function Report:start()"):]
        start = start[:start.index("\nend\n")]
        self.assertIn("self:openDoc(spec, current)", start)
        self.assertIn("lfs.attributes(current, \"mode\") == \"file\"", start)

    def test_stocks_mode_keeps_reader_awake(self):
        self.assertIn("function Report:usesReader()", MAIN)
        self.assertIn("not interactive and not self:usesReader()", MAIN)

    def test_check_interval_is_seven_minutes(self):
        # 设备端检查间隔 7 分钟：INTERVAL 是秒，界面文案由 intervalText() 生成，
        # 不能把「60s」写死在标题里。
        interval = int(re.search(r"local INTERVAL = (\d+)", MAIN).group(1))
        self.assertEqual(interval, 420)
        self.assertIn("local function intervalText()", MAIN)
        self.assertIn('string.format("%dm", INTERVAL / 60)', MAIN)
        self.assertIn("intervalText()", MAIN)
        self.assertNotIn('"004  |  60s  |  tap for menu"', MAIN)

    def test_auto_start_is_off_by_default(self):
        # 「自动阅读预设」默认取消：autostart 默认 false，且启动时的自动开始
        # 要求同时满足 autostart 与上次是跑着的（autostart_resume）。
        self.assertIn("if self.settings.autostart == nil then", MAIN)
        self.assertIn("self.settings.autostart = self.settings.enabled == true", MAIN)
        init = MAIN[MAIN.index("function Report:init()"):]
        init = init[:init.index("\nend\n")]
        self.assertIn("if self.settings.autostart and self.settings.autostart_resume", init)
        self.assertIn("and not session_started then", init)
        # 手动 Stop 之后即便 Auto-start 开着也不再自动开。
        stop = MAIN[MAIN.index("function Report:stop(silent)"):]
        stop = stop[:stop.index("\nend\n")]
        self.assertIn("self.settings.autostart_resume = nil", stop)

    def test_auto_start_touches_no_reader_presets(self):
        # 启动时只让 autoturn 停住，不改用户存下来的自动翻页预设/间隔。
        self.assertIn("function Report:cancelAutoTurn()", MAIN)
        body = MAIN[MAIN.index("function Report:cancelAutoTurn()"):]
        body = body[:body.index("\nend\n")]
        self.assertIn("auto_turn:stop()", body)
        for forbidden in ("autoturn_enabled", "autoturn_interval",
                          "G_reader_settings:saveSetting(\"autoturn", "delSetting(\"autoturn"):
            self.assertNotIn(forbidden, MAIN)

    def test_menu_has_auto_start_toggle(self):
        self.assertIn('"Auto-start on launch: "', MAIN)
        self.assertIn("function Report:toggleAutostart()", MAIN)

    def test_auto_start_cannot_reopen_the_same_doc_forever(self):
        # 死循环根因：init→start→openDoc→showReader→init 里，只看设置里的
        # autostart_resume 会一遍遍重开。现在有两道闸：进程内标记 session_started，
        # 以及「自动开书只做一次」（session_opened_doc / auto_opened_doc）。
        self.assertIn("local session_started = false", MAIN)
        self.assertIn("local session_opened_doc = false", MAIN)
        init = MAIN[MAIN.index("function Report:init()"):]
        init = init[:init.index("\nend\n")]
        self.assertIn("not session_started", init)
        self.assertIn("if self.settings.auto_opened_doc then session_opened_doc = true end", init)
        self.assertIn("if self.settings.auto_opened_doc then", MAIN)  # 或写回设置
        start = MAIN[MAIN.index("function Report:start()"):]
        start = start[:start.index("\n    self:refresh(true)")]
        self.assertIn("session_started = true", start)
        self.assertIn("and not session_opened_doc then", start)

    def test_open_doc_skips_when_already_open_and_records_marker(self):
        body = MAIN[MAIN.index("function Report:openDoc(spec, path)"):]
        body = body[:body.index("\nend\n")]
        self.assertIn('local current = self.ui and self.ui.document and self.ui.document.file', body)
        self.assertIn("if current and current == path then", body)
        self.assertIn("session_opened_doc = true", body)
        self.assertIn("patchSettings({ auto_opened_doc = true })", body)
        # 自动那一路上不重置标记，否则又能循环
        start = MAIN[MAIN.index("function Report:start()"):]
        start = start[:start.index("\n    self:refresh(true)")]
        self.assertIn("if self.interactive_start then", start)
        self.assertIn("session_opened_doc = false", start)

    def test_emergency_stop_is_a_file_level_kill_switch(self):
        self.assertIn("local function emergencyStopped()", MAIN)
        self.assertIn("data.emergency_stop == true", MAIN)
        init = MAIN[MAIN.index("function Report:init()"):]
        init = init[:init.index("\nend\n")]
        self.assertIn("if self.settings.emergency_stop then", init)
        self.assertIn("the plugin stays idle", init)
        self.assertIn("function Report:toggleEmergencyStop()", MAIN)
        self.assertIn('"Stop everything (emergency): "', MAIN)

    def test_session_widget_keeps_timers_alive_across_readerui_swap(self):
        # 后台壳：不显示但能收 onResume/onSuspend；ReaderUI 被换掉后能重新挂上，
        # 并且把菜单挂到当前宿主上，否则被 EPUB 模式困住时够不着 Stop。
        self.assertIn('local Session = WidgetContainer:extend{ name = "octopus004-session"', MAIN)
        self.assertIn("function Report:ensureSession()", MAIN)
        self.assertIn("function Report:attachToHost()", MAIN)
        start = MAIN[MAIN.index("function Report:start()"):]
        start = start[:start.index("\n    self:refresh(true)")]
        self.assertIn("self:ensureSession()", start)
        finish = MAIN[MAIN.index("        self.next_due = os.time() + INTERVAL"):]
        finish = finish[:finish.index("\n    end\n")]
        self.assertIn("if self.settings.enabled then self:ensureSession() end", finish)

    def test_stop_is_reachable_and_writes_its_reason(self):
        body = MAIN[MAIN.index("function Report:stop(silent)"):]
        body = body[:body.index("\nend\n")]
        self.assertIn("self.settings.autostart_resume = nil", body)
        self.assertIn("session_started = true", body)
        self.assertIn('"004: stopped', body)

    def test_version_was_bumped(self):
        meta = (PLUGIN / "_meta.lua").read_text(encoding="utf-8")
        version = re.search(r'version = "([\d.]+)"', meta).group(1)
        self.assertGreaterEqual(tuple(int(part) for part in version.split(".")), (0, 5, 1))


if __name__ == "__main__":
    unittest.main()
