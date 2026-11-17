"""同花顺行情面板：信封解析、重试、归一化、排版与自检。

这里不打网络：ThsClient 允许注入 fetch，测试用同花顺官方文档里的响应示例
（docs/api/a-share/prices.md 等）跑完整流程。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_stocks  # noqa: E402
import fetch_stocks  # noqa: E402
import stock_watchlist  # noqa: E402

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

    def test_watchlist_codes_are_well_formed(self):
        for code, _name in stock_watchlist.INDICES + stock_watchlist.STOCKS:
            self.assertRegex(code, r"^\d{6}\.(SH|SZ|BJ)$")


class BuildTest(unittest.TestCase):
    def _payload(self):
        return {
            "generated": "2026-11-17T07:00:00+00:00",
            "data_timestamp": 1784275991000,
            "indices": [{"thscode": "000001.SH", "name": "上证指数",
                         "last_price": 3665.94, "price_change": 16.79,
                         "price_change_ratio_pct": 0.46, "open_price": 3650.12,
                         "high_price": 3670.0, "low_price": 3648.5,
                         "prev_price": 3649.15, "volume": 51234567800.0,
                         "turnover": 1567890000000.0}],
            "stocks": [{"thscode": "600519.SH", "name": "贵州茅台",
                        "last_price": 1277.8, "price_change": 21.8,
                        "price_change_ratio_pct": 1.735669,
                        "open_price": 1252.08, "high_price": 1282.0,
                        "low_price": 1250.21, "prev_price": 1256.0,
                        "volume": 3098875.0, "turnover": 3937375200.0}],
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
        self.assertEqual(data["counts"], {"indices": 1, "stocks": 1, "limit_up": 1})
        text = build_stocks.render_txt(payload, "2026-11-17 15:00:00")
        self.assertIn("同花顺行情面板", text)
        self.assertIn("贵州茅台 1,277.80 +1.74%", text)
        self.assertIn("兆易创新", text)
        self.assertIn("不构成投资建议", text)

    def test_data_hash_stable_and_sensitive(self):
        payload = self._payload()
        first = build_stocks.data_hash(payload)
        self.assertEqual(first, build_stocks.data_hash(dict(payload)))
        changed = self._payload()
        changed["stocks"][0]["last_price"] = 1300.0
        self.assertNotEqual(first, build_stocks.data_hash(changed))

    def test_build_writes_three_files_and_passes_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "stocks_data.json"
            data_path.write_text(json.dumps(self._payload()), encoding="utf-8")
            out = Path(tmp) / "site"
            build_stocks.build(str(data_path), str(out))
            for name in ("stocks.json", "stocks.txt", "stocks.html"):
                self.assertTrue((out / name).is_file(), name)
            self.assertEqual(build_stocks.check(str(out)), [])
            page = (out / "stocks.html").read_text(encoding="utf-8")
            self.assertIn("stocks.json", page)
            self.assertIn("var POLL = 60", page)

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

    def test_formatters(self):
        self.assertEqual(build_stocks.fmt_volume(3098875.0), "3.10万手")
        self.assertEqual(build_stocks.fmt_turnover(3937375200.0), "39.37亿")
        self.assertEqual(build_stocks.fmt_ratio(None), "--")
        self.assertEqual(build_stocks.fmt_change(21.8), "▲+21.80")


if __name__ == "__main__":
    unittest.main()
