"""同花顺行情面板：信封解析、重试、归一化、排版与自检。

这里不打网络：ThsClient 允许注入 fetch，测试用同花顺官方文档里的响应示例
（docs/api/a-share/prices.md 等）跑完整流程。
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_stocks  # noqa: E402
import fetch_stocks
import stocks_watchdog  # noqa: E402

# 官方文档示例：行情快照（prices.md）与涨停池（limit-up-data.md）的响应。
SNAPSHOT = {
    "code": 0, "message": "success", "request_id": "r1",
    "data": {"timestamp": 1784275991000, "total": 2, "item": [
        {"thscode": "600519.SH", "ticker": "600519", "volume": 3098875,
         "turnover": 3937375200, "last_price": 1277.8, "price_change": 21.8,
         "price_change_ratio_pct": 1.735669, "open_price": 1252.08,
         "high_price": 1282, "low_price": 1250.21, "prev_price": 1256},
        {"thscode": "000001.SZ", "ticker": "000001", "volume": 45678900,
         "turnover": 512340000, "last_price": 11.23, "price_change": -0.12,
         "price_change_ratio_pct": -1.057, "open_price": 11.35,
         "high_price": 11.4, "low_price": 11.2, "prev_price": 11.35},
    ]},
}

LIMIT_UP = {
    "code": 0, "message": "success", "request_id": "r2",
    "data": {"timestamp": 1748102400000,
             "pagination": {"total": 126, "pages": 3, "size": 50, "page": 1},
             "item": [
                 {"thscode": "603986.SH", "ticker": "603986", "name": "兆易创新",
                  "is_st": False, "is_new": False, "last_price": 118.23,
                  "price_change_ratio_pct": 10.0008, "limit_up_time": "09:34",
                  "limit_up_reason": "存储芯片", "continue_day_text": "2连板",
                  "continue_day_cnt": 2, "seal_money": 123456789.12,
                  "max_seal_money": 234567890.12},
             ]},
}

SEARCH = {
    "code": 0, "message": "success", "request_id": "r3",
    "data": {"timestamp": 1716105600000, "item": [
        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
         "exchange": "SH", "asset_type": "a-share", "currency": "CNY",
         "list_date": "2001-08-27", "end_date": None,
         "last_trade_date": None, "last_delivery_date": None},
    ]},
}


def envelope_bytes(payload):
    return json.dumps(payload).encode("utf-8")


class FakeFetch:
    """按 URL 路由返回假报文，并记下调用次数（验重试）。"""

    def __init__(self, routes):
        self.routes = routes  # path 子串 -> bytes 或 callable(url)->bytes
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        for needle, body in self.routes.items():
            if needle in url:
                return body(url) if callable(body) else body
        raise AssertionError(f"意外的请求：{url}")


def make_client(fetch, **kwargs):
    kwargs.setdefault("sleep", lambda seconds: None)
    return fetch_stocks.ThsClient(base_url="https://example.test",
                                  api_key="test-key", fetch=fetch, **kwargs)


class ApiKeyTest(unittest.TestCase):
    def test_official_name_wins(self):
        env = {"HITHINK_FINANCE_API_KEY": "a", "THS_API_KEY": "b", "API_KEY": "c"}
        self.assertEqual(fetch_stocks.api_key_from_env(env), "a")

    def test_fallbacks_and_blank(self):
        self.assertEqual(fetch_stocks.api_key_from_env({"THS_API_KEY": "b"}), "b")
        self.assertEqual(fetch_stocks.api_key_from_env({"THS_KEY": "k"}), "k")
        self.assertEqual(fetch_stocks.api_key_from_env({"API_KEY": "c"}), "c")
        self.assertEqual(fetch_stocks.api_key_from_env({}), "")
        self.assertEqual(fetch_stocks.api_key_from_env(
            {"HITHINK_FINANCE_API_KEY": "  "}), "")


class ClientTest(unittest.TestCase):
    def test_success_returns_data(self):
        fetch = FakeFetch({"snapshot": envelope_bytes(SNAPSHOT)})
        client = make_client(fetch)
        data = client.get("/api/a-share/prices/snapshot",
                          {"thscodes": "600519.SH"})
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(fetch.calls), 1)

    def test_business_error_is_not_retried(self):
        # 2003 无权限：调用方可修的错，重试也没用。
        bad = {"code": 2003, "message": "Key 无效", "request_id": "rx", "data": None}
        fetch = FakeFetch({"snapshot": envelope_bytes(bad)})
        client = make_client(fetch)
        with self.assertRaises(fetch_stocks.ThsError) as ctx:
            client.get("/api/a-share/prices/snapshot")
        self.assertEqual(ctx.exception.code, 2003)
        self.assertIn("rx", str(ctx.exception))
        self.assertEqual(len(fetch.calls), 1)

    def test_rate_limit_is_retried_then_succeeds(self):
        bodies = [envelope_bytes({"code": 4001, "message": "限流", "data": None}),
                  envelope_bytes(SNAPSHOT)]
        fetch = FakeFetch({"snapshot": lambda url: bodies.pop(0)})
        client = make_client(fetch)
        data = client.get("/api/a-share/prices/snapshot")
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(fetch.calls), 2)

    def test_http_500_is_retried(self):
        state = {"n": 0}

        def flaky(url):
            state["n"] += 1
            if state["n"] == 1:
                raise HTTPError(url, 500, "server error", {}, None)
            return envelope_bytes(SNAPSHOT)

        fetch = FakeFetch({"snapshot": flaky})
        client = make_client(fetch)
        self.assertEqual(client.get("/api/a-share/prices/snapshot")["total"], 2)
        self.assertEqual(len(fetch.calls), 2)

    def test_invalid_json_and_missing_code_raise(self):
        fetch = FakeFetch({"snapshot": b"not json"})
        with self.assertRaises(fetch_stocks.ThsError):
            make_client(fetch).get("/api/a-share/prices/snapshot")
        fetch = FakeFetch({"snapshot": envelope_bytes({"data": {}})})
        with self.assertRaises(fetch_stocks.ThsError):
            make_client(fetch).get("/api/a-share/prices/snapshot")


class NormalizeTest(unittest.TestCase):
    def test_snapshot_keeps_nulls_without_zero_fill(self):
        row = fetch_stocks.normalize_snapshot(
            {"thscode": "600519.SH", "last_price": None,
             "price_change_ratio_pct": "1.5"}, "贵州茅台")
        self.assertIsNone(row["last_price"])
        self.assertEqual(row["price_change_ratio_pct"], 1.5)
        self.assertEqual(row["name"], "贵州茅台")

    def test_snapshot_without_code_is_dropped(self):
        self.assertIsNone(fetch_stocks.normalize_snapshot({"last_price": 1}))
        self.assertIsNone(fetch_stocks.normalize_snapshot(None))

    def test_limit_up_normalize(self):
        row = fetch_stocks.normalize_limit_up(LIMIT_UP["data"]["item"][0])
        self.assertEqual(row["name"], "兆易创新")
        self.assertEqual(row["continue_day_text"], "2连板")
        self.assertEqual(row["seal_money"], 123456789.12)


class FetchPayloadTest(unittest.TestCase):
    def _client(self):
        fetch = FakeFetch({
            "a-share-index/prices/snapshot": envelope_bytes(SNAPSHOT),
            "a-share/prices/snapshot": envelope_bytes(SNAPSHOT),
            "limit-up-pool": envelope_bytes(LIMIT_UP),
            "tickers/search": envelope_bytes(SEARCH),
        })
        return make_client(fetch)

    def test_full_round_sorts_and_resolves_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / "names.json")
            payload = fetch_stocks.fetch_payload(
                self._client(),
                stocks=[("600519.SH", ""), ("000001.SZ", "")],
                indices=[("000001.SH", "上证指数")],
                limit_up_size=20, names_cache_path=cache)
            self.assertEqual(len(payload["stocks"]), 2)
            # 按涨跌幅排：茅台 +1.74% 在平安银行 -1.06% 之前。
            self.assertEqual(payload["stocks"][0]["thscode"], "600519.SH")
            self.assertEqual(payload["stocks"][0]["name"], "贵州茅台")
            self.assertEqual(payload["data_timestamp"], 1784275991000)
            self.assertEqual(len(payload["limit_up"]), 1)
            self.assertEqual(payload["errors"], [])
            # 名字补过就进了缓存。
            saved = json.loads(Path(cache).read_text(encoding="utf-8"))
            self.assertEqual(saved.get("600519.SH"), "贵州茅台")

    def test_limit_up_failure_does_not_block_snapshot(self):
        fetch = FakeFetch({
            "a-share-index/prices/snapshot": envelope_bytes(SNAPSHOT),
            "a-share/prices/snapshot": envelope_bytes(SNAPSHOT),
            "limit-up-pool": envelope_bytes(
                {"code": 3002, "message": "数据尚未准备", "data": None}),
            "tickers/search": envelope_bytes(SEARCH),
        })
        payload = fetch_stocks.fetch_payload(
            make_client(fetch), stocks=[("600519.SH", "")], indices=[],
            limit_up_size=20)
        self.assertEqual(len(payload["stocks"]), 2)
        self.assertEqual(payload["limit_up"], [])
        self.assertEqual(len(payload["errors"]), 1)
        self.assertIn("涨停池", payload["errors"][0])

    def test_history_batch_is_chunked_and_trimmed(self):
        # 每批最多 10 个代码；接口按日返回，取回后只留最近 N 根。
        bars = [{"date_ms": 1784000000000 + i * 86400000, "close_price": 10.0 + i}
                for i in range(30)]
        fetch = FakeFetch({"/prices/historical-batch": envelope_bytes(
            {"code": 0, "data": {"item": [{"thscode": "600519.SH", "item": bars}]}})})
        history = fetch_stocks.fetch_history(
            make_client(fetch), "/api/a-share/prices/historical-batch",
            ["600519.SH", "000001.SZ"], days=20, now_ms=1900000000000)
        self.assertEqual(list(history), ["600519.SH"])
        self.assertEqual(len(history["600519.SH"]), 20)
        # 裁掉的是最早的那几根，时间轴保持升序。
        self.assertEqual(history["600519.SH"][-1][1], 39.0)
        self.assertLess(history["600519.SH"][0][0], history["600519.SH"][-1][0])
        self.assertEqual(len(fetch.calls), 1)

    def test_history_days_zero_skips_requests(self):
        fetch = FakeFetch({})
        self.assertEqual(fetch_stocks.fetch_history(
            make_client(fetch), "/api/a-share/prices/historical-batch",
            ["600519.SH"], days=0), {})
        self.assertEqual(fetch.calls, [])

    def test_history_failure_is_an_error_not_a_crash(self):
        fetch = FakeFetch({
            "a-share-index/prices/snapshot": envelope_bytes(SNAPSHOT),
            "a-share/prices/snapshot": envelope_bytes(SNAPSHOT),
            "historical-batch": envelope_bytes(
                {"code": 3002, "message": "数据尚未准备", "data": None}),
            "tickers/search": envelope_bytes(SEARCH),
        })
        payload = fetch_stocks.fetch_payload(
            make_client(fetch), stocks=[("600519.SH", "")], indices=[],
            limit_up_size=0, history_days=20)
        self.assertEqual(payload["history"], {})
        self.assertEqual(len(payload["stocks"]), 2)
        self.assertTrue(any("历史" in error for error in payload["errors"]))


class BuildTest(unittest.TestCase):
    def _payload(self):
        return {
            "generated": "2026-11-17T07:00:00+00:00",
            "data_timestamp": 1784275991000,
            "indices": [
                {"thscode": "000001.SH", "name": "上证指数",
                 "last_price": 3665.94, "price_change": 16.79,
                 "price_change_ratio_pct": 0.46, "open_price": 3650.12,
                 "high_price": 3670.0, "low_price": 3648.5,
                 "prev_price": 3649.15, "volume": 51234567800.0,
                 "turnover": 1567890000000.0},
                {"thscode": "881101.TI", "name": "种植业与林业",
                 "last_price": 3732.1, "price_change": -56.6,
                 "price_change_ratio_pct": -1.49, "volume": 2345678900.0,
                 "turnover": 3456789000.0, "is_sector": True},
            ],
            "market": {
                "count": 5584, "up": 2861, "down": 2413, "flat": 126,
                "no_price": 184, "limit_up": 58, "limit_down": 3,
                "turnover": 1712345678900.0, "volume": 123456789000.0,
                "avg_ratio": 0.32,
                "top_gainers": [{"thscode": "300750.SZ", "name": "宁德时代",
                                 "ratio": 10.02, "last_price": 268.4}],
                "top_losers": [{"thscode": "600036.SH", "name": "招商银行",
                                "ratio": -9.87, "last_price": 32.1}],
            },
            "hk": {
                "data_timestamp": 1784275991000,
                "rows": [
                    {"thscode": "hkHSI", "name": "恒生指数", "last_price": 23294.75,
                     "price_change": -214.6, "price_change_ratio_pct": -0.91,
                     "volume": 1234567890.0, "turnover": 87654321000.0,
                     "is_index": True},
                    {"thscode": "hk00700", "name": "腾讯控股", "last_price": 372.8,
                     "price_change": -3.2, "price_change_ratio_pct": -0.85,
                     "volume": 21345678.0, "turnover": 7890123456.0,
                     "is_index": False},
                ],
                "errors": [],
            },
            "limit_up": [{"thscode": "603986.SH", "name": "兆易创新",
                          "last_price": 118.23, "price_change_ratio_pct": 10.0008,
                          "limit_up_time": "09:34", "limit_up_reason": "存储芯片",
                          "continue_day_text": "2连板", "seal_money": 123456789.12}],
            "errors": [],
        }

    def test_manifest_and_text(self):
        payload = self._payload()
        data = build_stocks.manifest(payload, "2026-11-17 15:00:00")
        self.assertEqual(data["doc"], "stocks.txt")
        self.assertEqual(data["data_time_kind"], "exact")
        self.assertEqual(data["counts"], {"indices": 1, "sectors": 1, "market": 5584,
                                          "hk": 2, "limit_up": 1})
        self.assertEqual(data["market"]["up"], 2861)
        self.assertEqual(data["hk"]["data_time"], "2026-07-17T08:13:11Z")
        text = build_stocks.render_txt(payload, "2026-11-17 15:00:00")
        self.assertIn("同花顺行情面板", text)
        # 一屏监视的四段都在，且不再有「自选股」
        self.assertIn("【市场概览】", text)
        self.assertIn("【指数】", text)
        self.assertIn("【板块】", text)
        self.assertIn("【港股】", text)
        self.assertNotIn("【自选股】", text)
        self.assertIn("上证指数 3,665.94 +0.46%", text)
        self.assertIn("腾讯控股 372.80 -0.85%", text)
        self.assertIn("兆易创新", text)
        self.assertIn("不构成投资建议", text)

    def test_data_hash_stable_and_sensitive(self):
        payload = self._payload()
        first = build_stocks.data_hash(payload)
        self.assertEqual(first, build_stocks.data_hash(dict(payload)))
        changed = self._payload()
        changed["indices"][0]["last_price"] = 3700.0
        self.assertNotEqual(first, build_stocks.data_hash(changed))
        # 全市场涨跌家数变了也要换 hash
        changed = self._payload()
        changed["market"]["up"] = 3000
        self.assertNotEqual(first, build_stocks.data_hash(changed))
        # 港股行情变了同样要换
        changed = self._payload()
        changed["hk"]["rows"][1]["last_price"] = 380.0
        self.assertNotEqual(first, build_stocks.data_hash(changed))

    def test_build_writes_four_files_and_passes_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "stocks_data.json"
            data_path.write_text(json.dumps(self._payload()), encoding="utf-8")
            out = Path(tmp) / "site"
            build_stocks.build(str(data_path), str(out))
            for name in ("stocks.json", "stocks.txt", "stocks.html", "stocks.epub"):
                self.assertTrue((out / name).is_file(), name)
            self.assertEqual(build_stocks.check(str(out)), [])
            page = (out / "stocks.html").read_text(encoding="utf-8")
            self.assertIn("stocks.json", page)
            self.assertIn(f"var POLL = {build_stocks.POLL_SECONDS}", page)
            # 文案与轮询间隔同源：改间隔时不会只改一半（7 分钟 = 420 秒）。
            self.assertIn(f"POLL_TEXT = '{build_stocks.poll_text()}'", page)
            self.assertIn(f"setInterval(load,POLL*1000)", page)
            # 一屏卡片（宽基/板块/港股）+ 市场概览 + 涨停表都由页面里的 JS 现画
            self.assertIn('class="cards index"', page)
            self.assertIn('class="cards sector"', page)
            self.assertIn('class="cards hk"', page)
            self.assertIn("function breadth(", page)
            self.assertIn("function sectorCard(", page)
            self.assertIn("function hkCard(", page)
            self.assertIn("function spark(", page)
            self.assertIn("function limitRows(", page)
            self.assertIn("腾讯公开行情接口", page)

    def test_epub_is_valid_zip_with_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            build_stocks.build(out_dir=str(out), demo=True)
            import zipfile
            with zipfile.ZipFile(out / "stocks.epub") as zf:
                names = zf.namelist()
                self.assertEqual(names[0], "mimetype")
                self.assertEqual(zf.read("mimetype"), b"application/epub+zip")
                chapter = zf.read("OEBPS/chapter.xhtml").decode("utf-8")
            self.assertIn("同花顺行情面板", chapter)
            self.assertIn("市场概览", chapter)
            self.assertIn("恒生指数", chapter)
            self.assertIn("种植业与林业", chapter)
            self.assertIn("<table", chapter)
            self.assertIn("腾讯公开行情接口", chapter)
            # crengine 不认 flex/grid，EPUB 里不能出现
            self.assertNotIn("display:grid", chapter)
            self.assertNotIn("display: grid", chapter)
            self.assertNotIn("<svg", chapter)

    def test_manifest_lists_epub_and_history(self):
        payload = self._payload()
        payload["history"] = {"600519.SH": [[1784000000000, 1250.0]]}
        data = build_stocks.manifest(payload, "2026-11-17 15:00:00")
        self.assertEqual(data["epub"], "stocks.epub")
        self.assertIn("600519.SH", data["history"])

    def test_history_changes_data_hash(self):
        payload = self._payload()
        payload["history"] = {"600519.SH": [[1784000000000, 1250.0]]}
        first = build_stocks.data_hash(payload)
        payload["history"]["600519.SH"][0][1] = 1251.0
        self.assertNotEqual(first, build_stocks.data_hash(payload))

    def test_epub_has_no_flex_grid_or_svg(self):
        # crengine（Kindle）不认 flex/grid/SVG，EPUB 只能靠表格排版。
        xhtml = build_stocks.render_epub_xhtml(build_stocks.DEMO_PAYLOAD,
                                               "2026-11-17 15:00:00")
        for banned in ("display:grid", "display: grid", "display:flex",
                       "display: flex", "<svg", "<polyline"):
            self.assertNotIn(banned, xhtml)

    def test_manifest_can_be_rebuilt_from_itself(self):
        # site/stocks.json 本身就是「清单 + 数据」：拿它当 --data 重排一次
        # （只改版式时很方便）不能把数据时间弄丢。
        data = build_stocks.manifest(self._payload(), "2026-11-17T07:00:00Z")
        again = build_stocks.manifest(data, "2026-11-17T08:00:00Z")
        self.assertEqual(again["data_time"], data["data_time"])
        self.assertEqual(again["counts"], data["counts"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            data_path = Path(tmp) / "stocks.json"
            data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            build_stocks.build(str(data_path), str(out))
            rebuilt = json.loads((out / "stocks.json").read_text(encoding="utf-8"))
        self.assertEqual(rebuilt["data_time"], data["data_time"])

    def test_build_rejects_empty_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "stocks_data.json"
            data_path.write_text(json.dumps(
                {"indices": [], "stocks": [], "limit_up": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                build_stocks.build(str(data_path), str(Path(tmp) / "site"))

    def test_demo_build_is_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            data = build_stocks.build(out_dir=str(out), demo=True)
            self.assertTrue(data["demo"])
            text = (out / "stocks.txt").read_text(encoding="utf-8")
            self.assertIn("演示数据", text)

    def test_device_poll_is_seven_minutes(self):
        # 设备端（网页面板 + Kindle 插件）都是 7 分钟检查一次；服务端仍是 5 分钟一轮。
        self.assertEqual(build_stocks.POLL_SECONDS, 420)
        self.assertEqual(build_stocks.poll_text(), "7分钟")
        text = build_stocks.render_txt(self._payload(), "2026-11-17 15:00:00")
        self.assertIn("每7分钟检查", text)
        # 服务端节奏跟 refresh-stocks.yml 的 cron 一致（15 分钟一轮 + watchdog 补跑）
        self.assertIn(f"每{build_stocks.SERVER_MINUTES}分钟刷新", text)
        self.assertEqual(build_stocks.SERVER_MINUTES, 15)

    def test_status_line_says_age_and_session(self):
        # 「时间为什么不是最新」曾经看不出来：现在抬头写距今多久 + 盘中/未开盘/已收盘。
        payload = self._payload()          # data_timestamp = 1784275991000
        data_ms = payload["data_timestamp"]
        line = build_stocks.status_line(payload, now_ms=data_ms + 3 * 60000)
        self.assertIn("数据 2026-07-17 16:13:11", line)
        self.assertIn("3 分钟前", line)
        self.assertIn("已收盘", line)
        self.assertNotIn("STALE", line)
        # 这份 payload 是 16:13（已收盘），收盘后不再更新，不该报 STALE
        # （否则凌晨看面板永远在报警）。盘中落后的 STALE 由下一条测试覆盖。
        closed = build_stocks.status_line(payload, now_ms=data_ms + 40 * 60000)
        self.assertNotIn("STALE", closed)
        self.assertIn("收盘后不再更新", closed)
        # 演示数据不标「多久以前」，免得看着像实时数据坏了
        demo = dict(payload, demo=True)
        self.assertIn("演示数据", build_stocks.status_line(demo, now_ms=data_ms))

    def test_age_and_session_helpers(self):
        base = 1794962959000            # 2026-11-18 08:49:19 北京时间
        self.assertEqual(build_stocks.age_text(base, base + 30000), "刚刚")
        self.assertEqual(build_stocks.age_text(base, base + 25 * 60000), "25 分钟前")
        self.assertEqual(build_stocks.age_text(base, base + 3 * 3600000), "3 小时前")
        self.assertEqual(build_stocks.age_text(base, base - 60000), "")
        self.assertEqual(build_stocks.session_label(base), "未开盘")
        self.assertEqual(build_stocks.session_label(base + 90 * 60000), "盘中")   # 10:19
        # 午休单列：11:45 抓到的那份成交量跟 11:30 一样，标「盘中」会误导
        self.assertEqual(build_stocks.session_label(base + 176 * 60000), "午休")   # 11:45
        self.assertEqual(build_stocks.session_label(base + 251 * 60000), "盘中")   # 13:00
        self.assertEqual(build_stocks.session_label(base + 7 * 3600000), "已收盘")
        # base 是 08:49（未开盘）：这时段行情本来不动，所以「老 21 分钟」不算过期；
        # 真过期判定在 test_stale_only_when_data_should_be_moving 里覆盖。
        self.assertFalse(build_stocks.is_stale(base, base + 5 * 60000))
        self.assertFalse(build_stocks.is_stale(base, base + 21 * 60000))

    def test_stale_only_when_data_should_be_moving(self):
        # 收盘后/深夜看面板不该报 STALE —— 行情本来就不动。以前是死比
        # 「现在 - 数据时间 > 20 分钟」，凌晨一点看必然报警，纯误报。
        import datetime as _dt

        def ms(h, m, day=18):
            """北京时间 → 毫秒时间戳（h 是北京整点，减 8 得 UTC）。"""
            return int((_dt.datetime(2026, 11, day, h, m, tzinfo=_dt.timezone.utc)
                        - _dt.timedelta(hours=8)).timestamp() * 1000)

        close = ms(15, 10)                   # 收盘后的一份数据
        self.assertEqual(build_stocks.session_label(close), "已收盘")
        self.assertFalse(build_stocks.is_stale(close, ms(18, 10)), "收盘后 3 小时不算过期")
        self.assertFalse(build_stocks.is_stale(close, ms(1, 10, day=19)), "次日凌晨不算过期")
        self.assertTrue(build_stocks.is_stale(close, ms(16, 10, day=19)), "跨交易日仍无新数据才算过期")
        # 盘中落后依然要报
        live = ms(10, 0)
        self.assertTrue(build_stocks.is_stale(live, ms(10, 40)))
        # 抬头在收盘后说清楚「收盘后不再更新」，不要只写「已收盘」让人以为卡住了
        text = build_stocks.status_line({"data_timestamp": close}, now_ms=ms(18, 10))
        self.assertIn("收盘后不再更新", text)
        self.assertNotIn("STALE", text)
        # 午休同理：11:45 的数据在 11:50 看不算过期（行情停了），但 12:40 就算
        lunch = ms(11, 45)
        self.assertEqual(build_stocks.session_label(lunch), "午休")
        self.assertFalse(build_stocks.is_stale(lunch, ms(11, 50)))

    def test_empty_snapshot_is_detected(self):
        # 开盘前的空壳：成交量全 0、只有前收盘那个点位；不能显示成「0.00 手」，
        # 也不能覆盖上一份有成交的面板。
        # 真实形态：指数带着前收盘点位（个别有价），个股全是 null，成交量全 0。
        shell = {"data_timestamp": 1794962959000,
                 "indices": [{"thscode": "000001.SH", "name": "上证指数",
                              "last_price": 3584.56, "open_price": 0.0, "high_price": 0.0,
                              "low_price": 0.0, "volume": 0.0, "turnover": 0.0}],
                 "stocks": [{"thscode": "601398.SH", "name": "工商银行",
                             "last_price": None, "open_price": None, "volume": 0.0},
                            {"thscode": "601939.SH", "name": "建设银行",
                             "last_price": None, "open_price": None, "volume": 0.0},
                            {"thscode": "600036.SH", "name": "招商银行",
                             "last_price": None, "open_price": None, "volume": 0.0}]}
        self.assertTrue(build_stocks.empty_snapshot(shell))
        self.assertFalse(build_stocks.today_has_values(shell))
        self.assertTrue(build_stocks.row_is_empty(shell["stocks"][0]))
        self.assertFalse(build_stocks.row_is_empty(shell["indices"][0]))
        live = self._payload()
        self.assertFalse(build_stocks.empty_snapshot(live))
        text = build_stocks.render_txt(shell, "2026-11-18 08:49:26")
        self.assertIn("未成交", text)
        self.assertNotIn("成交 0.00 手", text)
        self.assertNotIn("成交额 0.00 亿", text)
        # 有价但没成交的行：开盘价写 "--" 而不是 0.00
        self.assertIn("上证指数 3,584.56", text)
        self.assertEqual(build_stocks.fmt_pos(0.0), "--")
        self.assertEqual(build_stocks.fmt_pos(None), "--")
        self.assertEqual(build_stocks.fmt_pos(12.3), "12.30")

    def test_empty_snapshot_does_not_overwrite_a_live_panel(self):
        shell = {"data_timestamp": 1794962959000,
                 "indices": [{"thscode": "000001.SH", "name": "上证指数",
                              "last_price": None, "volume": 0.0}],
                 "stocks": [{"thscode": "601398.SH", "name": "工商银行",
                             "last_price": None, "volume": 0.0}]}
        self.assertTrue(build_stocks.empty_snapshot(shell))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            out.mkdir()
            live = build_stocks.manifest(self._payload(), "2026-11-17T07:00:00Z")
            (out / "stocks.json").write_text(json.dumps(live), encoding="utf-8")
            data_path = Path(tmp) / "in.json"
            data_path.write_text(json.dumps(shell), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                build_stocks.build(str(data_path), str(out))
            self.assertIn("空壳", str(ctx.exception))
            # 上一份有效面板原样保留
            kept = json.loads((out / "stocks.json").read_text(encoding="utf-8"))
            self.assertEqual(kept["data_timestamp"], live["data_timestamp"])

    def test_empty_snapshot_still_writes_when_there_is_nothing_better(self):
        shell = {"data_timestamp": 1794962959000,
                 "indices": [{"thscode": "000001.SH", "name": "上证指数",
                              "last_price": None, "volume": 0.0}],
                 "stocks": [{"thscode": "601398.SH", "name": "工商银行",
                             "last_price": None, "volume": 0.0}]}
        self.assertTrue(build_stocks.empty_snapshot(shell))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            data_path = Path(tmp) / "in.json"
            data_path.write_text(json.dumps(shell), encoding="utf-8")
            build_stocks.build(str(data_path), str(out))
            self.assertTrue((out / "stocks.txt").is_file())

    def test_watchdog_policy(self):
        # cron 会被丢弃：watchdog 在盘中且落后时补跑，非盘中/新鲜/已在跑都不动。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stocks.json"
            path.write_text(json.dumps({"data_time": "2026-11-18T01:15:00Z"}),
                            encoding="utf-8")
            dispatched = []
            logs = []
            originals = stocks_watchdog.running_jobs, stocks_watchdog.dispatch
            stocks_watchdog.running_jobs = lambda repo=None: []
            stocks_watchdog.dispatch = lambda repo=None: dispatched.append(True)
            try:
                # 盘中（北京 10:00）落后 85 分钟 → 补跑
                result = stocks_watchdog.check(
                    path, now=stocks_watchdog.parse_stamp("2026-11-18T02:30:00Z"),
                    repo=None, do_dispatch=True, log=logs.append)
                self.assertEqual(result, "dispatched")
                self.assertEqual(len(dispatched), 1)
                # 新鲜（09:20，落后 5 分钟）→ 不动
                self.assertEqual(stocks_watchdog.check(
                    path, now=stocks_watchdog.parse_stamp("2026-11-18T01:20:00Z"),
                    do_dispatch=True, log=logs.append), "fresh")
                # 收盘后 → 不动
                self.assertEqual(stocks_watchdog.check(
                    path, now=stocks_watchdog.parse_stamp("2026-11-18T09:30:00Z"),
                    do_dispatch=True, log=logs.append), "outside")
                # 已有一轮在跑 → 不重复补
                stocks_watchdog.running_jobs = lambda repo=None: [{"status": "in_progress"}]
                self.assertEqual(stocks_watchdog.check(
                    path, now=stocks_watchdog.parse_stamp("2026-11-18T02:30:00Z"),
                    do_dispatch=True, log=logs.append), "busy")
                self.assertEqual(len(dispatched), 1)
            finally:
                stocks_watchdog.running_jobs, stocks_watchdog.dispatch = originals

    def test_dispatch_survives_github_500(self):
        """dispatch 接口偶发 HTTP 500，不能让它把交接那一步整个搞挂。

        实测过：交接连吃几次 500 → job 变红 → 外层 cron 又把我们叫醒，
        滚动成一片失败；而数据其实早就抓完提交了。
        """
        calls = []

        class Failing:
            returncode = 1
            stdout = ""
            stderr = ("could not create workflow dispatch event: HTTP 500 "
                      "(https://api.github.com/.../dispatches)")

        class Working:
            returncode = 0
            stdout = ""
            stderr = ""

        original = stocks_watchdog.subprocess.run
        logs = []
        try:
            # 一直 500：重试 DISPATCH_ATTEMPTS 次，返回 False 而不是抛异常
            stocks_watchdog.subprocess.run = lambda cmd, **kw: (
                calls.append(cmd), Failing())[1]
            self.assertFalse(stocks_watchdog.dispatch(
                sleep=lambda seconds: None, log=logs.append))
            self.assertEqual(len(calls), stocks_watchdog.DISPATCH_ATTEMPTS)
            self.assertTrue(any("警告" in line or "::warning::" in line
                                for line in logs))

            # 前两次 500、第三次成功：返回 True
            calls.clear()
            answers = [Failing(), Failing(), Working()]
            stocks_watchdog.subprocess.run = lambda cmd, **kw: (
                calls.append(cmd), answers.pop(0))[1]
            self.assertTrue(stocks_watchdog.dispatch(
                sleep=lambda seconds: None, log=logs.append))
            self.assertEqual(len(calls), 3)
        finally:
            stocks_watchdog.subprocess.run = original

    def test_handoff_exits_zero_when_dispatch_fails(self):
        """交接失败不该让 job 标红（数据已经提交，标红只会误导）。"""
        original = stocks_watchdog.dispatch
        stocks_watchdog.dispatch = lambda repo=None: False
        try:
            # 北京周一 10:00，在抓取窗口内 → 会尝试交接
            now = "2026-11-18T02:00:00Z"
            code = stocks_watchdog.main(["--handoff", "--now", now])
            self.assertEqual(code, 0)
        finally:
            stocks_watchdog.dispatch = original

    def test_watchdog_session_window(self):
        parse = stocks_watchdog.parse_stamp
        self.assertTrue(stocks_watchdog.in_session(parse("2026-11-18T01:16:00Z")))   # 09:16 北京
        self.assertFalse(stocks_watchdog.in_session(parse("2026-11-18T01:10:00Z")))  # 09:10
        self.assertFalse(stocks_watchdog.in_session(parse("2026-11-18T07:10:00Z")))  # 15:10
        self.assertFalse(stocks_watchdog.in_session(parse("2026-11-21T02:00:00Z")))  # 周六
        self.assertTrue(stocks_watchdog.in_session(parse("2026-11-18T07:04:00Z")))   # 15:04

    def test_workflow_schedule_matches_the_text_panel(self):
        # 面板上写的「每 N 分钟刷新」必须和工作流 cron 一致，别再出现
        # 「写着 5 分钟、实际几小时没跑」。
        import re
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "refresh-stocks.yml").read_text(encoding="utf-8")
        cron = re.search(r'cron: "([^"]+)"', workflow).group(1)
        minutes = cron.split()[0]
        # 15 分钟一轮：分钟列表写成 3,18,33,48（错开整点），而不是 */5 这种会被丢弃的
        self.assertNotIn("*/5", cron)
        values = [int(part) for part in minutes.split(",")]
        self.assertEqual(len(values), 4)
        self.assertEqual(build_stocks.SERVER_MINUTES, 15)
        self.assertEqual(sorted(values), [3, 18, 33, 48])
        # watchdog 得存在，并且用同一个阈值口径
        watchdog = (root / ".github" / "workflows" / "refresh-stocks-watchdog.yml").read_text(encoding="utf-8")
        self.assertIn("tools/stocks_watchdog.py", watchdog)
        self.assertIn("--stale-minutes", watchdog)
        self.assertIn("actions: write", watchdog)

    def test_watchdog_check_session_and_handoff(self):
        # refresh-stocks.yml 的自循环靠这两个开关判断「还要不要继续」。
        parse = stocks_watchdog.parse_stamp
        session = stocks_watchdog.main(["--check-session", "--now", "2026-11-18T02:00:00Z"])
        self.assertEqual(session, 0)          # 北京 10:00，盘中
        closed = stocks_watchdog.main(["--check-session", "--now", "2026-11-18T09:00:00Z"])
        self.assertEqual(closed, 1)           # 北京 17:00，收盘

        dispatched = []
        original = stocks_watchdog.dispatch
        stocks_watchdog.dispatch = lambda repo=None: dispatched.append(True)
        try:
            # 盘中交接 → 真的 dispatch
            self.assertEqual(stocks_watchdog.main(
                ["--handoff", "--now", "2026-11-18T02:00:00Z"]), 0)
            self.assertEqual(len(dispatched), 1)
            # 收盘后交接 → 什么都不做，自循环到此为止
            self.assertEqual(stocks_watchdog.main(
                ["--handoff", "--now", "2026-11-18T09:00:00Z"]), 0)
            self.assertEqual(len(dispatched), 1)
        finally:
            stocks_watchdog.dispatch = original

    def test_loop_window_and_nap(self):
        # 常驻循环的「该不该干活」窗口：北京 09:00–15:30，周一到周五。
        # 比 in_session（09:15–15:05）宽，且**窗口外不退出**——cron 会迟到几小时，
        # 一次迟到的触发应该能睡到次日开盘自己开工。
        parse = stocks_watchdog.parse_stamp
        self.assertTrue(stocks_watchdog.in_loop_window(parse("2026-11-18T01:00:00Z")))   # 09:00
        self.assertFalse(stocks_watchdog.in_loop_window(parse("2026-11-18T00:59:00Z")))  # 08:59
        self.assertTrue(stocks_watchdog.in_loop_window(parse("2026-11-18T07:30:00Z")))   # 15:30
        self.assertFalse(stocks_watchdog.in_loop_window(parse("2026-11-18T07:31:00Z")))  # 15:31
        self.assertFalse(stocks_watchdog.in_loop_window(parse("2026-11-21T02:00:00Z")))  # 周六
        # 窗口内立刻回来
        self.assertEqual(stocks_watchdog.seconds_until_window(parse("2026-11-18T02:00:00Z")), 0)
        # 北京 01:37（周四凌晨）→ 距当日 09:00 约 7.4 小时
        nap = stocks_watchdog.seconds_until_window(parse("2026-11-18T17:37:00Z"))
        self.assertAlmostEqual(nap / 3600, 7.38, places=1)
        # 给 cap 就别睡太久（避免一次 sleep 卡死作业）
        self.assertEqual(stocks_watchdog.seconds_until_window(
            parse("2026-11-18T17:37:00Z"), cap=1800), 1800)
        # 周五收盘后 → 跳过周末，指向下周一 09:00（北京）
        friday = parse("2026-11-20T09:00:00Z")          # 北京 11-20 17:00 周五
        n = stocks_watchdog.seconds_until_window(friday)
        self.assertAlmostEqual(n / 3600, 64.0, delta=0.1)   # 周五17:00 → 周一09:00 = 64h

    def test_self_loop_guards(self):
        # 单个 step 里连续跑 6 小时，必须自己会停：无 Key/接口挂了不能空转，
        # 连续失败 4 轮就中止并且不交接下一棒。
        #
        # 这里不用 PyYAML：CI 的 runner 上没装，import yaml 会让整步失败
        # （#57 合并后 refresh-stocks 就是因此在「跑测试」挂掉的）。按缩进切步骤文本，
        # 断言照样够用，且零第三方依赖。
        root = Path(__file__).resolve().parents[1]
        text = (root / ".github" / "workflows" / "refresh-stocks.yml").read_text(encoding="utf-8")
        self.assertNotIn("\t", text, "YAML 里不能有 Tab")
        minutes = re.findall(r"timeout-minutes:\s*(\d+)", text)
        self.assertTrue(minutes, "没写 timeout-minutes")
        self.assertLessEqual(int(minutes[0]), 360)                     # 作业上限 6 小时
        # 交接下一棒要用 actions: write
        self.assertRegex(text, r"permissions:\s*\n\s*contents:\s*write\s*\n\s*actions:\s*write")
        steps = re.split(r"\n      - name: ", text)
        loop = [s for s in steps if s.startswith("常驻自循环")][0]
        # 窗口外不能退出（cron 会迟到几小时），要长睡等开盘
        self.assertIn("--in-window", loop)
        self.assertIn("--sleep-until-window", loop)
        self.assertNotIn("--check-session", loop, "常驻循环不该用「盘中就退出」的旧判断")
        self.assertIn("deadline", loop)                               # 有预算上限
        self.assertIn("fails", loop)                                  # 连续失败计数
        self.assertIn("ABORT_HANDOFF=1", loop)                        # 中止标记
        handoff = [s for s in steps if s.startswith("交接下一棒")][0]
        self.assertIn("--handoff", handoff)
        self.assertIn("ABORT_HANDOFF", handoff)                       # 中止后不交接

    def test_formatters(self):
        self.assertEqual(build_stocks.fmt_volume(3098875.0), "3.10万手")
        self.assertEqual(build_stocks.fmt_turnover(3937375200.0), "39.37亿")
        self.assertEqual(build_stocks.fmt_ratio(None), "--")
        self.assertEqual(build_stocks.fmt_change(21.8), "▲+21.80")


if __name__ == "__main__":
    unittest.main()


# ── 全市场概览 / 指数板块 / 港股（腾讯源）──────────────���────────────────────

INDEX_SNAPSHOT = {
    "code": 0, "message": "success", "request_id": "r4",
    "data": {"timestamp": 1784275991000, "total": 2, "item": [
        {"thscode": "000001.SH", "last_price": 3665.94,
         "price_change_ratio_pct": 0.46, "volume": 51234567800,
         "turnover": 1567890000000},
        {"thscode": "881101.TI", "last_price": 3732.1,
         "price_change_ratio_pct": -1.49, "volume": 2345678900,
         "turnover": 3456789000},
    ]},
}


class IndexRowsTest(unittest.TestCase):
    """批量取指数时，一个坏代码会让整批返回 0 条——必须隔离出来。"""

    def test_bad_code_is_isolated_without_losing_others(self):
        def fetch(url, headers, timeout):
            codes = url.split("thscodes=")[1].split("&")[0]
            codes = [c for c in codes.replace("%2C", ",").split(",") if c]
            if "881157.TI" in codes:
                # 整批被坏代码带崩
                return json.dumps({"code": 0, "data": {"total": 0, "item": []}}).encode()
            item = [r for r in INDEX_SNAPSHOT["data"]["item"] if r["thscode"] in codes]
            return json.dumps({"code": 0, "data": {
                "timestamp": 1784275991000, "total": len(item),
                "item": item}}).encode()

        client = make_client(fetch)
        rows, bad, stamp = fetch_stocks.fetch_index_rows(
            client, ["000001.SH", "881101.TI", "881157.TI"])
        self.assertEqual([r["thscode"] for r in rows], ["000001.SH", "881101.TI"])
        self.assertEqual(bad, ["881157.TI"])
        self.assertEqual(stamp, 1784275991000)

    def test_enrich_adds_names_and_marks_sectors(self):
        rows = [{"thscode": "881101.TI", "last_price": 1.0},
                {"thscode": "000001.SH", "last_price": 2.0}]
        pairs = [("000001.SH", "上证指数"), ("881101.TI", "种植业与林业")]
        out = fetch_stocks.enrich_index_rows(rows, pairs)
        self.assertEqual([r["thscode"] for r in out], ["000001.SH", "881101.TI"])
        self.assertEqual(out[0]["name"], "上证指数")
        self.assertFalse(out[0]["is_sector"])
        self.assertEqual(out[1]["name"], "种植业与林业")
        self.assertTrue(out[1]["is_sector"])


class MarketBreadthTest(unittest.TestCase):
    def _rows(self):
        return [
            {"thscode": "600001.SH", "name": "主板涨", "last_price": 11.0,
             "price_change_ratio_pct": 10.0, "volume": 100, "turnover": 1000},
            {"thscode": "600002.SH", "name": "主板跌停", "last_price": 9.0,
             "price_change_ratio_pct": -10.0, "volume": 100, "turnover": 1000},
            {"thscode": "300001.SZ", "name": "创业板涨", "last_price": 20.0,
             "price_change_ratio_pct": 19.9, "volume": 100, "turnover": 1000},
            {"thscode": "300002.SZ", "name": "创业板平", "last_price": 20.0,
             "price_change_ratio_pct": 0.0, "volume": 100, "turnover": 1000},
            {"thscode": "830001.BJ", "name": "北交所", "last_price": 5.0,
             "price_change_ratio_pct": 29.6, "volume": 100, "turnover": 1000},
            {"thscode": "600003.SH", "name": "未开盘", "last_price": None,
             "price_change_ratio_pct": None, "volume": 0, "turnover": 0},
        ]

    def test_counts_and_limits(self):
        stats = fetch_stocks.market_breadth(self._rows())
        self.assertEqual(stats["count"], 6)
        self.assertEqual(stats["up"], 3)
        self.assertEqual(stats["down"], 1)
        self.assertEqual(stats["flat"], 1)
        self.assertEqual(stats["no_price"], 1)
        # 主板 10%、创业板/科创 20%、北交所 30% 三档阈值
        self.assertEqual(stats["limit_up"], 3)
        self.assertEqual(stats["limit_down"], 1)
        self.assertEqual(stats["turnover"], 5000)
        self.assertAlmostEqual(stats["avg_ratio"],
                               (10.0 - 10.0 + 19.9 + 0.0 + 29.6) / 5, places=6)

    def test_top_gainers_and_losers(self):
        stats = fetch_stocks.market_breadth(self._rows())
        self.assertEqual(stats["top_gainers"][0]["thscode"], "830001.BJ")
        self.assertEqual(stats["top_losers"][0]["thscode"], "600002.SH")
        self.assertEqual(len(stats["top_gainers"]), 5)   # 无价那只不进榜

    def test_empty_rows_do_not_explode(self):
        stats = fetch_stocks.market_breadth([])
        self.assertEqual(stats["count"], 0)
        self.assertIsNone(stats["avg_ratio"])
        self.assertEqual(stats["top_gainers"], [])


class FullMarketTest(unittest.TestCase):
    def test_pages_until_total(self):
        calls = []

        def fetch(url, headers, timeout):
            offset = int(re.search(r"offset=(\d+)", url).group(1))
            calls.append(offset)
            rows = [{"thscode": f"6000{i:02d}.SH", "last_price": 1.0}
                    for i in range(offset, min(offset + 2, 5))]
            return json.dumps({"code": 0, "data": {
                "timestamp": 1784275991000, "total": 5, "item": rows}}).encode()

        client = make_client(fetch)
        rows, total = fetch_stocks.fetch_full_market(client, page=2)
        self.assertEqual(total, 5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(calls, [0, 2, 4])

    def test_partial_failure_keeps_what_it_got(self):
        def fetch(url, headers, timeout):
            if "offset=2" in url:
                return json.dumps({"code": 5001, "message": "boom"}).encode()
            return json.dumps({"code": 0, "data": {
                "timestamp": 1, "total": 12, "item": [
                    {"thscode": f"60000{i}.SH", "last_price": 1.0}
                    for i in range(2)]}}).encode()

        client = make_client(fetch, max_attempts=1)
        rows, _total = fetch_stocks.fetch_full_market(client, page=2)
        self.assertEqual(len(rows), 2)


def _tencent_line(code, name, last, prev, open_, high, low, change, ratio,
                  turnover, stamp="2026/11/18 16:08:10"):
    """按腾讯的字段位置拼一行（下标 1 名 / 3 现价 / 4 昨收 / 30 时间 / 37 成交额）。"""
    fields = ["0"] * 40
    fields[1] = name
    fields[2] = code[2:]
    fields[3] = f"{last:.2f}"
    fields[4] = f"{prev:.2f}"
    fields[5] = f"{open_:.2f}"
    fields[30] = stamp
    fields[31] = f"{change:.2f}"
    fields[32] = f"{ratio:.2f}"
    fields[33] = f"{high:.2f}"
    fields[34] = f"{low:.2f}"
    fields[37] = f"{turnover:.0f}"
    return f'v_{code}="' + "~".join(fields) + '";\n'


TENCENT_TEXT = (
    _tencent_line("hkHSI", "恒生指数", 23294.75, 23509.35, 23400.0,
                  23600.0, 23100.0, -214.60, -0.91, 0)
    + _tencent_line("hk00700", "腾讯控股", 372.80, 376.00, 375.20,
                    378.0, 370.0, -3.20, -0.85, 7890123456)
    + 'v_hk99999="";\n'
)


class HkTest(unittest.TestCase):
    def test_parse_lines(self):
        import fetch_hk
        rows = fetch_hk.parse_response(TENCENT_TEXT)
        self.assertEqual([r["thscode"] for r in rows], ["hkHSI", "hk00700"])
        self.assertEqual(rows[0]["name"], "恒生指数")
        self.assertAlmostEqual(rows[0]["last_price"], 23294.75)
        self.assertAlmostEqual(rows[0]["price_change_ratio_pct"], -0.91)
        self.assertAlmostEqual(rows[1]["price_change_ratio_pct"], -0.85)
        self.assertAlmostEqual(rows[1]["turnover"], 7890123456)
        self.assertEqual(rows[1]["currency"], "HKD")

    def test_quote_time_is_hong_kong_time(self):
        import fetch_hk
        ms = fetch_hk.quote_time_ms("2026/11/18 16:08:10")
        # 香港 16:08 = UTC 08:08
        from datetime import datetime, timezone
        self.assertEqual(datetime.fromtimestamp(ms / 1000, tz=timezone.utc).hour, 8)

    def test_fetch_payload_marks_indices_and_missing(self):
        import fetch_hk
        def fake_fetch(url, headers, timeout):
            return TENCENT_TEXT.encode("gbk")

        payload = fetch_hk.fetch_payload(
            codes=["hkHSI", "hk00700", "hk09988"], fetch=fake_fetch)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertTrue(payload["rows"][0]["is_index"])
        self.assertFalse(payload["rows"][1]["is_index"])
        self.assertTrue(any("hk09988" in e for e in payload["errors"]))
        self.assertEqual(payload["data_timestamp"],
                         fetch_hk.quote_time_ms("2026/11/18 16:08:10"))

    def test_network_failure_returns_error_not_exception(self):
        import fetch_hk
        def boom(url, headers, timeout):
            raise URLError("no route")

        payload = fetch_hk.fetch_payload(codes=["hk00700"], fetch=boom,
                                         sleep=lambda s: None)
        self.assertEqual(payload["rows"], [])
        self.assertTrue(payload["errors"])


class WatchlistTest(unittest.TestCase):
    def test_codes_well_formed_and_unique(self):
        import market_watchlist as mw
        seen = set()
        for code, name in mw.INDICES + mw.SECTORS:
            self.assertRegex(code, r"^\d{6}\.(SH|SZ|TI)$", name)
            self.assertNotIn(code, seen, f"{code} 重复出现")
            seen.add(code)
            self.assertTrue(name.strip())
        self.assertEqual(len(mw.INDICES), 13)
        self.assertEqual(len(mw.SECTORS), 91)
        # 同花顺行业指数的代码是「连续编号」的，缺号说明清单里混了不存在的代码
        numbers = sorted(int(code[:6]) for code, _ in mw.SECTORS
                         if code.endswith(".TI") and code.startswith("881"))
        self.assertEqual(len(numbers), 90)
        self.assertEqual(numbers[0], 881101)
        self.assertEqual(numbers[-1], 881284)
        # 宽基与板块不能混（000001.SH 属于宽基，881xxx.TI 属于板块）
        self.assertTrue(all(c.endswith((".SH", ".SZ")) for c, _ in mw.INDICES))

    def test_hk_codes_well_formed(self):
        import market_watchlist as mw
        self.assertEqual(len(mw.HK_INDICES), 3)
        self.assertEqual(len(mw.HK_STOCKS), 20)
        for code, name in mw.HK_INDICES + mw.HK_STOCKS:
            self.assertRegex(code, r"^hk[0-9A-Z]+$", name)


class SectorClassificationTest(unittest.TestCase):
    """「有色金属」是同花顺唯一不是 .TI 后缀的行业指数（000819.SH）。

    只按后缀判会把它排进【指数】段，盘面上宽基就变成 14 个了。
    """

    def test_non_ti_sector_code_is_classified_as_sector(self):
        self.assertTrue(build_stocks.is_sector_row({"thscode": "000819.SH"}))
        self.assertFalse(build_stocks.is_sector_row({"thscode": "000001.SH"}))
        self.assertTrue(build_stocks.is_sector_row({"thscode": "881101.TI"}))
        # enrich 也要标对，否则网页 JS 会跟着分错
        out = fetch_stocks.enrich_index_rows(
            [{"thscode": "000819.SH", "last_price": 1.0},
             {"thscode": "000001.SH", "last_price": 2.0}],
            [("000001.SH", "上证指数"), ("000819.SH", "有色金属")])
        by_code = {r["thscode"]: r["is_sector"] for r in out}
        self.assertTrue(by_code["000819.SH"])
        self.assertFalse(by_code["000001.SH"])

    def test_counts_split_broad_and_sector(self):
        payload = {"indices": [
            {"thscode": "000001.SH"}, {"thscode": "000819.SH"},
            {"thscode": "881101.TI"}]}
        self.assertEqual(len(build_stocks.core_indices(payload)), 1)
        self.assertEqual(len(build_stocks.sector_indices(payload)), 2)


class SectorTextLayoutTest(unittest.TestCase):
    """91 个板块挤成两栏时，一个都不能丢、也不能因为截断而撞名。

    实测踩过：``汽车服务及其他`` 与 ``油气开采及服务`` 都被截成 4 字（``汽车服务…``
    / ``油气开采…``），撞名后那一格只剩 6 个字符，被「右侧为空」的判定当空行跳过，
    盘面上直接少了板块。
    """

    def _payload(self):
        import market_watchlist as mw
        return {"data_timestamp": 1784275991000, "market": None, "hk": None,
                "indices": [
                    {"thscode": code, "name": name, "last_price": 1000.0 + i,
                     "price_change": 1.0,
                     "price_change_ratio_pct": round((i % 20) - 10, 2),
                     "turnover": 1e9,
                     "is_sector": code.endswith(".TI") or code == "000819.SH"}
                    for i, (code, name) in enumerate(mw.SECTORS)]}

    def test_every_sector_appears_exactly_once(self):
        import market_watchlist as mw
        text = build_stocks.render_txt(self._payload(), "2026-11-19 09:30:00")
        section = text.split("【板块】")[1].split("【港股】")[0]
        for _code, name in mw.SECTORS:
            self.assertIn(name, section, f"板块「{name}」没出现在文本面板里")

    def test_long_names_are_shortened_uniquely(self):
        short = build_stocks._short_name
        # 7 字名按宽度 14 截断后仍要能区分
        self.assertNotEqual(short("汽车服务及其他"), short("油气开采及服务"))
        self.assertEqual(short("银行"), "银行")
        # 截断只发生在超宽时，且结果不超宽
        long_name = short("汽车服务及其他")
        self.assertTrue(long_name.endswith("…"))
        self.assertLessEqual(build_stocks._display_width(long_name), 14)
