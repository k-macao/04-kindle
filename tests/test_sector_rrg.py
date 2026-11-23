"""板块轮动 RRG：公式、warm-up 行数、象限、信号 A 与面板排版。

公式部分照 `hugo2046/QuantsPlaybook` 的《RRG 算法说明》核：**warm-up 行数写错
会静默全 None**，所以这里逐条验精确行数与长度下界——不是「跑通就行」。
数据全是造的，不打网络。
"""

import json
import math
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_stocks  # noqa: E402
import fetch_stocks  # noqa: E402
import market_watchlist  # noqa: E402
import sector_rrg  # noqa: E402


def synthetic(count, start=100.0, drift=0.0003, amp=0.01, period=37, phase=0.0,
              start_day=None):
    """确定性价格序列 → `[[毫秒, 收盘], ...]`（工作日）。"""
    day = start_day or datetime(2026, 11, 20, tzinfo=timezone.utc)
    days = []
    cursor = day
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    price, bars = start, []
    for index, moment in enumerate(days):
        price *= 1.0 + drift + amp * math.sin(2 * math.pi * index / period + phase)
        bars.append([int(moment.timestamp() * 1000), round(price, 4)])
    return bars


def history_for(codes, count=340):
    return {code: synthetic(count, drift=0.0002 + 0.00004 * pos,
                            amp=0.006 + 0.001 * pos, phase=pos * 0.6)
            for pos, code in enumerate(codes)}


class WarmUpTest(unittest.TestCase):
    def test_first_valid_row_matches_the_spec(self):
        # 研报口径 220/60/20：RS-Ratio 第 240 行、RS-Mom 第 319 行才有值。
        codes = ["881121.TI"]
        closes = {code: [100.0 + i * 0.1 for i in range(340)] for code in codes}
        bench = [1.0 + i * 0.0001 for i in range(340)]
        ratio, mom = sector_rrg.compute_rrg(closes, bench)
        first_ratio = next(i for i, v in enumerate(ratio["881121.TI"]) if v is not None)
        first_mom = next(i for i, v in enumerate(mom["881121.TI"]) if v is not None)
        self.assertEqual(first_ratio, 220 + 20 - 1)          # 239
        self.assertEqual(first_mom, 220 + 60 + 2 * (20 - 1))  # 318
        # 后面不许再冒出 None（中间断档就是 bug）
        self.assertNotIn(None, ratio["881121.TI"][first_ratio:])
        self.assertNotIn(None, mom["881121.TI"][first_mom:])

    def test_short_preset_warm_up(self):
        closes = {"881101.TI": [100.0 + i for i in range(120)]}
        bench = [1.0 + i * 0.0005 for i in range(120)]
        ratio, mom = sector_rrg.compute_rrg(
            closes, bench, lookback_ratio=60, lookback_mom=20, smooth_window=5)
        self.assertEqual(next(i for i, v in enumerate(ratio["881101.TI"]) if v), 60 + 5 - 1)
        self.assertEqual(next(i for i, v in enumerate(mom["881101.TI"]) if v),
                         60 + 20 + 2 * (5 - 1))

    def test_length_guard_is_exact(self):
        need = sector_rrg.min_rows(sector_rrg.PRESETS[0])
        self.assertEqual(need, 319)
        self.assertEqual(sector_rrg.min_rows(sector_rrg.PRESETS[1]), 89)
        bench = [1.0] * (need - 1)
        with self.assertRaises(ValueError):
            sector_rrg.compute_rrg({"A": [1.0] * (need - 1)}, bench)
        # 刚好多一根就能算（最后一行有值）
        ratio, mom = sector_rrg.compute_rrg({"A": [100.0 + i for i in range(need)]},
                                            [1.0] * need)
        self.assertIsNotNone(mom["A"][-1])
        self.assertIsNone(mom["A"][-2])


class RatioFormulaTest(unittest.TestCase):
    def test_smooth_ratio_shift_uses_ratio_then_mean(self):
        # x = 100,101,102,...：100*x_t/x_{t-2} 恒为 100*102/100 —— 用等差序列验到小数。
        values = [100.0 + i for i in range(10)]
        out = sector_rrg.smooth_ratio_shift(values, lookback=2, smooth=3)
        self.assertEqual(out[:4], [None, None, None, None])
        expected = (100 * (102 / 100) + 100 * (103 / 101) + 100 * (104 / 102)) / 3
        self.assertAlmostEqual(out[4], expected, places=6)

    def test_window_with_a_hole_stays_empty(self):
        # 窗口里有一个 None → 整格 None（等价于 pandas rolling 默认 min_periods）。
        # 洞在 i=2：ratio[2]、ratio[3] 都缺（前者自己缺、后者基准缺），
        # 所以输出要到窗口完全干净的 i=5 才有值——**不许用可用点凑平均**。
        values = [100.0, 101.0, None, 103.0, 104.0, 105.0]
        out = sector_rrg.smooth_ratio_shift(values, lookback=1, smooth=2)
        self.assertIsNone(out[2])
        self.assertIsNone(out[3])
        self.assertIsNone(out[4])
        self.assertIsNotNone(out[5])

    def test_equal_weight_benchmark_is_a_portfolio_not_an_average(self):
        # 两个板块：A 翻倍、B 不动 → 等权组合 +50%，不是点位均值
        closes = {"A": [100.0, 200.0], "B": [100.0, 100.0]}
        nav = sector_rrg.equal_weight_benchmark(closes)
        self.assertEqual(nav[0], 1.0)
        self.assertAlmostEqual(nav[1], 1.5, places=9)

    def test_missing_price_is_dropped_from_that_day_not_zero_filled(self):
        # 某板块某天没价：那天的截面收益剔除它（等价 pandas 的 mean(skipna)），
        # 不是当 0 收益掺进去——否则一个停牌板块会把整个基准拖平。
        # 注意 align_history 之前已经 ffill 过，这里验的是公式层的兜底口径。
        closes = {"A": [100.0, None, 121.0], "B": [100.0, 100.0, 100.0]}
        nav = sector_rrg.equal_weight_benchmark(closes)
        self.assertEqual(nav[0], 1.0)
        self.assertAlmostEqual(nav[1], 1.0, places=9)
        self.assertAlmostEqual(nav[2], 1.0, places=9)
        # 若 B 那天也涨，基准只反映 B，不掺 A 的「0 收益」
        closes = {"A": [100.0, None, 121.0], "B": [100.0, 110.0, 110.0]}
        nav = sector_rrg.equal_weight_benchmark(closes)
        self.assertAlmostEqual(nav[1], 1.10, places=9)


class QuadrantTest(unittest.TestCase):
    def test_four_quadrants(self):
        self.assertEqual(sector_rrg.quadrant_of(100.5, 100.5), 1)
        self.assertEqual(sector_rrg.quadrant_of(99.5, 100.5), 2)
        self.assertEqual(sector_rrg.quadrant_of(99.5, 99.5), 3)
        self.assertEqual(sector_rrg.quadrant_of(100.5, 99.5), 4)

    def test_on_the_axis_is_not_forced_into_a_quadrant(self):
        self.assertIsNone(sector_rrg.quadrant_of(100.0, 101.0))
        self.assertIsNone(sector_rrg.quadrant_of(101.0, 100.0))
        self.assertIsNone(sector_rrg.quadrant_of(None, 101.0))

    def test_distance_from_center(self):
        self.assertAlmostEqual(sector_rrg.distance_from_center(103.0, 104.0), 5.0)


class AlignTest(unittest.TestCase):
    def test_missing_day_is_forward_filled(self):
        day1 = datetime(2026, 11, 17, tzinfo=timezone.utc)
        day2 = datetime(2026, 11, 18, tzinfo=timezone.utc)
        history = {
            "A": [[int(day1.timestamp() * 1000), 10.0], [int(day2.timestamp() * 1000), 11.0]],
            "B": [[int(day1.timestamp() * 1000), 20.0]],   # B 少了第二天
        }
        days, closes = sector_rrg.align_history(history)
        self.assertEqual(len(days), 2)
        self.assertEqual(closes["B"], [20.0, 20.0])

    def test_today_snapshot_overrides_and_appends(self):
        day1 = datetime(2026, 11, 18, tzinfo=timezone.utc)
        history = {"A": [[int(day1.timestamp() * 1000), 10.0]]}
        days, closes = sector_rrg.align_history(
            history, today_prices={"A": 12.5}, today_day="2026-11-19")
        self.assertEqual(days[-1], "2026-11-19")
        self.assertEqual(closes["A"][-1], 12.5)
        # 同一天则覆盖，不新增一行
        days, closes = sector_rrg.align_history(
            history, today_prices={"A": 12.5}, today_day="2026-11-18")
        self.assertEqual(len(days), 1)
        self.assertEqual(closes["A"], [12.5])

    def test_bad_rows_are_ignored(self):
        history = {"A": [[0, 0], [None, 3], ["x", "y"], [1700000000000, 0]]}
        days, closes = sector_rrg.align_history(history)
        self.assertEqual(days, [])
        self.assertEqual(closes, {})


class SectionTest(unittest.TestCase):
    def setUp(self):
        self.codes = ["881121.TI", "881122.TI", "881155.TI", "881153.TI",
                      "881273.TI", "881145.TI"]
        self.names = dict(zip(self.codes, ["半导体", "光学光电子", "银行", "房地产",
                                           "白酒", "电力"]))
        self.sectors = [(code, self.names[code]) for code in self.codes]
        self.history = history_for(self.codes, 340)

    def test_section_shape(self):
        section, notes = sector_rrg.build_section(self.history, self.sectors)
        self.assertEqual(notes, [])
        self.assertEqual(section["preset_key"], "report")
        self.assertEqual(section["samples"], 340)
        self.assertEqual(len(section["rows"]), len(self.codes))
        self.assertEqual(sum(section["counts"].values()), len(self.codes))
        for row in section["rows"]:
            self.assertIn(row["quadrant"], (1, 2, 3, 4))
            self.assertGreater(row["rs_ratio"], 0)
            self.assertTrue(row["trail"])
            self.assertEqual(len(row["trail"][-1]), 2)
        # 象限按轮动顺序（领先→疲软→滞后→改善）排，同象限里离中心远的在前
        order = [sector_rrg.QUADRANT_ORDER.index(row["quadrant"])
                 for row in section["rows"]]
        self.assertEqual(order, sorted(order))
        self.assertTrue(section["narrative"])
        self.assertIn("领先", section["narrative"][0])

    def test_signal_a_only_takes_the_leading_quadrant(self):
        section, _ = sector_rrg.build_section(self.history, self.sectors, top_n=2)
        picks = section["signal"]["picks"]
        leading = [row for row in section["rows"] if row["quadrant"] == 1]
        self.assertEqual([p["thscode"] for p in picks],
                         [row["thscode"] for row in leading[:2]])
        for pick in picks:
            self.assertGreater(pick["rs_ratio"], 100)
            self.assertGreater(pick["rs_mom"], 100)

    def test_too_little_history_is_a_note_not_a_crash(self):
        section, notes = sector_rrg.build_section(
            history_for(self.codes, 40), self.sectors)
        self.assertIsNone(section)
        self.assertTrue(any("根" in note for note in notes))

    def test_short_preset_is_used_when_history_is_short(self):
        section, notes = sector_rrg.build_section(
            history_for(self.codes, 120), self.sectors)
        self.assertIsNotNone(section)
        self.assertEqual(section["preset_key"], "short")
        self.assertEqual(notes, [])

    def test_two_sectors_are_not_enough(self):
        section, notes = sector_rrg.build_section(
            self.history, self.sectors[:2])
        self.assertIsNone(section)
        self.assertIn("至少 3 个", notes[0])

    def test_today_prices_reach_the_analysis(self):
        section, _ = sector_rrg.build_section(
            self.history, self.sectors,
            today_prices={code: 1.0 for code in self.codes},
            today_day="2026-11-23", today_pct={code: -1.23 for code in self.codes})
        self.assertEqual(section["as_of"], "2026-11-23")
        self.assertTrue(all(row["today_pct"] == -1.23 for row in section["rows"]))

    def test_quadrant_moves_are_reported(self):
        # 迁移名单只看「今天 vs 前一日」的象限编号：进/出领先、进改善
        rows = [
            {"name": "半导体", "thscode": "881121.TI", "quadrant": 1, "prev_quadrant": 4},
            {"name": "银行", "thscode": "881155.TI", "quadrant": 2, "prev_quadrant": 1},
            {"name": "白酒", "thscode": "881273.TI", "quadrant": 2, "prev_quadrant": 3},
            {"name": "电力", "thscode": "881145.TI", "quadrant": 1, "prev_quadrant": 1},
        ]
        moves = sector_rrg._moves(rows)
        self.assertEqual(moves["into_leading"], ["半导体"])
        self.assertEqual(moves["out_of_leading"], ["银行"])
        self.assertEqual(moves["into_improving"], ["银行", "白酒"])

    def test_narrative_mentions_the_move(self):
        section, _ = sector_rrg.build_section(self.history, self.sectors)
        changed = dict(section)
        changed["rows"] = [dict(row) for row in section["rows"]]
        for row in changed["rows"]:
            if row["name"] == "银行":
                row["quadrant"], row["prev_quadrant"] = 1, 4
        changed["moves"] = sector_rrg._moves(changed["rows"])
        narrative = sector_rrg._narrative(changed)
        self.assertTrue(any("新进领先" in line and "银行" in line for line in narrative))


class DemoTest(unittest.TestCase):
    def test_demo_section_uses_the_real_formula(self):
        section = sector_rrg.demo_section()
        self.assertTrue(section["demo"])
        self.assertEqual(section["preset_key"], "report")
        self.assertGreaterEqual(len(section["rows"]), 10)
        # 演示里四个象限都得有人，否则网页演示看不出分区
        self.assertGreater(section["counts"]["leading"], 0)
        self.assertGreater(section["counts"]["improving"], 0)
        self.assertGreater(section["counts"]["lagging"], 0)
        self.assertGreater(section["counts"]["weakening"], 0)
        # 数据日是「最近的工作日」，别停在几个月前；也不能拿 UTC 日期冒充今天
        # （UTC 16:00 = 北京次日，旧写法在这个窗口里差一天，CI 就是这么红的）。
        self.assertEqual(section["as_of"], sector_rrg.demo_days()[-1].strftime("%Y-%m-%d"))
        today = sector_rrg.beijing_today()
        lag = (today - datetime.strptime(section["as_of"], "%Y-%m-%d").date()).days
        self.assertLessEqual(lag, 2)                 # 周末最多停到周五
        self.assertEqual(lag, 0 if today.weekday() < 5 else today.weekday() - 4)

    def test_demo_day_rolls_over_with_beijing_midnight(self):
        """UTC 16:00 之后北京已是次日：数据日跟着北京走，不是跟着 UTC 走。"""
        # 2026-11-20 20:46 UTC = 北京 11-21（周六）04:46 → 最近的工作日是周五 11-20
        late = datetime(2026, 11, 20, 20, 46, tzinfo=timezone.utc)
        self.assertEqual(sector_rrg.demo_days(5, now=late)[-1].strftime("%Y-%m-%d"),
                         "2026-11-20")
        # 2026-11-20 13:56 UTC = 北京 11-20（周五）21:56 → 就是当天
        night = datetime(2026, 11, 20, 13, 56, tzinfo=timezone.utc)
        self.assertEqual(sector_rrg.demo_days(5, now=night)[-1].strftime("%Y-%m-%d"),
                         "2026-11-20")
        # 2026-11-23 02:00 UTC = 北京 11-23（周一）10:00 → 当天
        monday = datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(sector_rrg.demo_days(5, now=monday)[-1].strftime("%Y-%m-%d"),
                         "2026-11-23")

    def test_demo_days_skip_weekends(self):
        days = sector_rrg.demo_days(8, now=datetime(2026, 11, 22, 2, 0, tzinfo=timezone.utc))
        self.assertEqual([day.strftime("%m-%d") for day in days],
                         ["11-11", "11-12", "11-13", "11-16", "11-17", "11-18",
                          "11-19", "11-20"])


class FetchIntegrationTest(unittest.TestCase):
    """build_rrg 走真 fetch_history（假 client），含缓存与失败回退。"""

    class FakeClient:
        def __init__(self, codes, days=340, fail=False):
            self.codes = list(codes)
            self.days = days
            self.fail = fail
            self.calls = []

        def get(self, path, params=None):
            self.calls.append((path, dict(params or {})))
            if self.fail:
                raise fetch_stocks.ThsError("接口挂了")
            wanted = str((params or {}).get("thscodes") or "").split(",")
            history = history_for(self.codes, self.days)
            return {"item": [{"thscode": code,
                              "item": [{"date_ms": ms, "close_price": close}
                                       for ms, close in history.get(code, [])]}
                             for code in wanted]}

    def setUp(self):
        fetch_stocks.RRG_CACHE.update({"day": "", "stamp": 0.0, "history": {},
                                       "codes": frozenset(), "days": 0})
        self.codes = ["881121.TI", "881122.TI", "881155.TI", "881153.TI"]
        self.rows = [{"thscode": code, "name": code, "is_sector": True,
                      "last_price": 10.0, "price_change_ratio_pct": 0.5}
                     for code in self.codes]

    def test_section_is_built_from_the_index_history_endpoint(self):
        client = self.FakeClient(self.codes)
        section, notes = fetch_stocks.build_rrg(
            client, self.rows, days=340, today_day="2026-11-20")
        self.assertEqual(notes, [])
        self.assertEqual(len(section["rows"]), len(self.codes))
        self.assertTrue(client.calls)
        self.assertIn("a-share-index/prices/historical-batch", client.calls[0][0])

    def test_second_round_hits_the_cache(self):
        client = self.FakeClient(self.codes)
        fetch_stocks.build_rrg(client, self.rows, days=340)
        first = len(client.calls)
        fetch_stocks.build_rrg(client, self.rows, days=340)
        self.assertEqual(len(client.calls), first)

    def test_failure_without_cache_is_a_note(self):
        client = self.FakeClient(self.codes, fail=True)
        section, notes = fetch_stocks.build_rrg(client, self.rows, days=340)
        self.assertIsNone(section)
        self.assertIn("取不到", notes[0])

    def test_failure_falls_back_to_the_same_day_cache(self):
        good = self.FakeClient(self.codes)
        fetch_stocks.build_rrg(good, self.rows, days=340, cache_minutes=0)
        broken = self.FakeClient(self.codes, fail=True)
        section, notes = fetch_stocks.build_rrg(broken, self.rows, days=340,
                                               cache_minutes=0)
        self.assertIsNotNone(section)
        self.assertTrue(any("缓存" in note for note in notes))


class RunOnceTest(unittest.TestCase):
    """一轮完整抓取（假接口）：stocks_data.json 里要有 sector_rrg，且不挡其它段。"""

    class Router:
        def __init__(self, sectors):
            self.sectors = sectors
            self.calls = []

        def get(self, path, params=None):
            self.calls.append((path, dict(params or {})))
            if "historical-batch" in path:
                wanted = str((params or {}).get("thscodes") or "").split(",")
                history = history_for(self.sectors, 340)
                return {"item": [{"thscode": code,
                                  "item": [{"date_ms": ms, "close_price": close}
                                           for ms, close in history.get(code, [])]}
                                 for code in wanted]}
            if path.endswith("/a-share-index/prices/snapshot"):
                return {"timestamp": 1795000000000, "item": [
                    {"thscode": code, "last_price": 100.0 + pos,
                     "price_change_ratio_pct": 0.5 - pos * 0.3}
                    for pos, code in enumerate(
                        [str(c).strip() for c in
                         str((params or {}).get("thscodes") or "").split(",")])]}
            if path.endswith("/a-share/prices/snapshot"):
                return {"timestamp": 1795000000000, "total": 2, "item": [
                    {"thscode": "600519.SH", "last_price": 1200.0,
                     "price_change_ratio_pct": 1.0, "turnover": 1e9},
                    {"thscode": "000001.SZ", "last_price": 11.0,
                     "price_change_ratio_pct": -1.0, "turnover": 1e8}]}
            if "limit-up-pool" in path:
                return {"timestamp": 1795000000000, "item": []}
            raise AssertionError(f"意外的请求：{path}")

    def setUp(self):
        fetch_stocks.RRG_CACHE.update({"day": "", "stamp": 0.0, "history": {},
                                       "codes": frozenset(), "days": 0})
        import argparse
        self.args = argparse.Namespace(
            indices="", out="", out_dir="site", codes="", no_limit_up=False,
            no_market=False, no_hk=True, limit_up_size=20, history_days=20,
            names_cache="", base_url="https://example.test", loop=0, build=False,
            no_rrg=False, rrg_days=340, rrg_cache_minutes=60, rrg_top_n=6,
            rrg_preset="")
        self.sectors = [code for code, _ in
                        market_watchlist.SECTORS[:6]]

    def test_run_once_writes_the_rrg_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.args.out = str(Path(tmp) / "stocks_data.json")
            client = self.Router(self.sectors)
            payload = fetch_stocks.run_once(self.args, client)
            section = payload["sector_rrg"]
            self.assertTrue(section["rows"])
            self.assertEqual(len(section["rows"]), len(self.sectors))
            saved = json.loads(
                Path(self.args.out).read_text(encoding="utf-8"))
            self.assertIn("sector_rrg", saved)
            # 历史 K 线只取板块（宽基那 13 个不参与 RRG），每批 10 个代码：
            # 91 个板块 = 10 次批量请求（这是 RRG 唯一的额外成本，缓存 60 分钟）
            history_calls = [call for call in client.calls if "historical-batch" in call[0]]
            self.assertEqual(len(history_calls), 10)
            requested = ",".join(call[1]["thscodes"] for call in history_calls)
            self.assertIn("881121.TI", requested)
            self.assertNotIn("000001.SH", requested)

    def test_short_history_falls_back_to_cross_section(self):
        class ShortRouter(self.Router):
            def get(self, path, params=None):
                if "historical-batch" in path:
                    self.calls.append((path, dict(params or {})))
                    wanted = str((params or {}).get("thscodes") or "").split(",")
                    history = history_for(self.sectors, 20)
                    return {"item": [{"thscode": code,
                                      "item": [{"date_ms": ms, "close_price": close}
                                               for ms, close in history.get(code, [])]}
                                     for code in wanted]}
                return super().get(path, params)

        with tempfile.TemporaryDirectory() as tmp:
            self.args.out = str(Path(tmp) / "stocks_data.json")
            client = ShortRouter(self.sectors)
            payload = fetch_stocks.run_once(self.args, client)
            self.assertNotIn("sector_rrg", payload)
            cross = payload["sector_cross"]
            self.assertEqual(cross["mode"], "cross_section")
            self.assertGreaterEqual(cross["counts"]["total"], 5)
            self.assertIn("根", cross["why"])
            self.assertTrue(cross["narrative"])
            # 主行情照旧，风光照常出
            self.assertTrue(payload["indices"])

    def test_no_rrg_skips_the_history_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.args.out = str(Path(tmp) / "stocks_data.json")
            self.args.no_rrg = True
            client = self.Router(self.sectors)
            payload = fetch_stocks.run_once(self.args, client)
            self.assertNotIn("sector_rrg", payload)
            self.assertFalse([call for call in client.calls
                              if "historical-batch" in call[0]])


def sector_row(name, pct, turnover=None, code=None):
    return {"thscode": code or name, "name": name, "is_sector": True,
            "last_price": 100.0, "price_change_ratio_pct": pct,
            "turnover": turnover}


class CrossSectionTest(unittest.TestCase):
    """单日横截面（RRG 的降级口径）：统计、量价、分化、题材、话术。"""

    def _rows(self):
        return [sector_row("半导体", -0.80, 2163e8),
                sector_row("通信设备", -3.37, 776e8),
                sector_row("银行", -0.62, 310e8),
                sector_row("机场航运", 0.72, 33e8),
                sector_row("文化传媒", -4.69, 276e8),
                sector_row("影视院线", -5.93, 77e8),
                sector_row("煤炭开采加工", -2.10, 120e8),
                sector_row("证券", -0.05, 149e8)]

    def _market(self):
        return {"count": 5584, "up": 649, "down": 4876, "flat": 48,
                "limit_up": 27, "limit_down": 84, "avg_ratio": -2.55,
                "turnover": 13145.8e8}

    def _indices(self):
        return [{"thscode": "000300.SH", "name": "沪深300", "is_sector": False,
                 "price_change_ratio_pct": 0.03},
                {"thscode": "000852.SH", "name": "中证1000", "is_sector": False,
                 "price_change_ratio_pct": -1.88},
                {"thscode": "000001.SH", "name": "上证指数", "is_sector": False,
                 "price_change_ratio_pct": -0.28}]

    def _limit_up(self):
        return [{"name": "盛剑科技", "limit_up_reason": "光刻胶+半导体附属装备"},
                {"name": "大港股份", "limit_up_reason": "集成电路测试+封测"},
                {"name": "康盛股份", "limit_up_reason": "液冷+算力"},
                {"name": "襄阳轴承", "limit_up_reason": "机器人轴承"},
                {"name": "某公司", "limit_up_reason": "???"}]

    def _build(self, **kwargs):
        params = {"market": self._market(), "indices": self._indices(),
                  "limit_up": self._limit_up(), "as_of": "2026-11-19",
                  "why": "板块日 K 只有 20 根，短窗 RRG 要 89 根"}
        params.update(kwargs)
        return sector_rrg.build_cross_section(self._rows(), **params)

    def test_counts_are_about_sectors_not_the_whole_market(self):
        section, notes = self._build()
        self.assertEqual(notes, [])
        counts = section["counts"]
        self.assertEqual(counts["total"], 8)
        self.assertEqual(counts["up"], 1)
        self.assertEqual(counts["down"], 7)
        # 全 A 的 649/4876 单独放 market，绝不混进板块的 up/down
        self.assertEqual(section["market"]["up"], 649)
        self.assertEqual(section["market"]["down"], 4876)
        self.assertIn("红 1", section["narrative"][0])
        self.assertNotIn("649", section["narrative"][0])

    def test_stats_and_bins_agree(self):
        section, _ = self._build()
        self.assertEqual(sum(item["count"] for item in section["bins"]), 8)
        pcts = sorted(row["price_change_ratio_pct"] for row in self._rows())
        self.assertAlmostEqual(section["stats"]["median"],
                               (pcts[3] + pcts[4]) / 2, places=2)
        self.assertLess(section["stats"]["std"], 3)

    def test_all_red_flips_the_label(self):
        rows = [sector_row(f"板块{i}", -1.0 - i * 0.1) for i in range(6)]
        section, _ = sector_rrg.build_cross_section(rows)
        self.assertEqual(section["winners_label"], "相对抗跌")
        self.assertEqual(section["losers_label"], "领跌")
        self.assertEqual(section["winners"][0]["today_pct"], -1.0)

    def test_money_and_share(self):
        section, _ = self._build()
        money = section["money"]
        self.assertEqual(money["top_name"], "半导体")
        self.assertAlmostEqual(money["top_share"], 16.5, places=1)
        self.assertAlmostEqual(money["top"][0]["share"], 16.5, places=1)
        # 成交额前列与末列的平均涨跌都算出来了
        self.assertIsNotNone(money["top_avg"])
        self.assertIsNotNone(money["tail_avg"])

    def test_large_vs_small_cap_spread(self):
        section, _ = self._build()
        spread = section["spread"]
        self.assertEqual(spread["big"], "沪深300")
        self.assertEqual(spread["small"], "中证1000")
        self.assertAlmostEqual(spread["spread"], 1.91, places=2)
        self.assertTrue(any("权重抗跌" in line for line in section["narrative"]))

    def test_limit_up_themes_are_grouped(self):
        section, _ = self._build()
        themes = {item["name"]: item for item in section["themes"]}
        self.assertEqual(themes["半导体材料/封测/存储"]["count"], 2)
        self.assertEqual(themes["算力/液冷/数据中心"]["count"], 1)
        self.assertEqual(themes["机器人"]["count"], 1)
        self.assertEqual(sector_rrg.THEME_OTHER in themes, True)
        # 例子最多 3 个，名字里的空格去掉（同花顺有「红 宝 丽」这种）
        for item in themes.values():
            self.assertLessEqual(len(item["examples"]), 3)

    def test_broad_down_day_is_called_a_deleveraging_day(self):
        section, _ = self._build()
        text = "".join(section["narrative"])
        self.assertIn("普跌", text)
        self.assertIn("不是板块间搬家", text)
        # 叙述里不谈象限：没有日 K 就只讲当天能证明的事
        for line in section["narrative"]:
            self.assertNotIn("象限", line)
        # 「为什么是横截面」写在 why 字段里，由各渲染层单独显示
        self.assertEqual(section["why"], "板块日 K 只有 20 根，短窗 RRG 要 89 根")

    def test_broad_up_day_uses_the_other_wording(self):
        rows = [sector_row(f"板块{i}", 1.0 + i * 0.1) for i in range(6)]
        section, _ = sector_rrg.build_cross_section(
            rows, market={"up": 5000, "down": 400})
        text = "".join(section["narrative"])
        self.assertIn("普涨", text)

    def test_too_few_sectors_is_a_note(self):
        section, notes = sector_rrg.build_cross_section([sector_row("A", 1.0)])
        self.assertIsNone(section)
        self.assertIn("样本太少", notes[0])

    def test_sectors_without_pct_are_ignored(self):
        rows = self._rows() + [{"thscode": "x", "name": "停牌板块",
                                "is_sector": True, "price_change_ratio_pct": None}]
        section, _ = sector_rrg.build_cross_section(rows)
        self.assertEqual(section["counts"]["total"], 8)
        self.assertNotIn("停牌板块", [row["name"] for row in section["winners"]])


class CrossRenderTest(unittest.TestCase):
    def _payload(self):
        rows = [sector_row("半导体", -0.80, 2163e8), sector_row("通信设备", -3.37, 776e8),
                sector_row("银行", -0.62, 310e8), sector_row("机场航运", 0.72, 33e8),
                sector_row("文化传媒", -4.69, 276e8), sector_row("影视院线", -5.93, 77e8)]
        market = {"count": 5584, "up": 649, "down": 4876, "flat": 48,
                  "limit_up": 27, "limit_down": 84, "avg_ratio": -2.55,
                  "turnover": 13145.8e8, "top_gainers": [], "top_losers": []}
        indices = [{"thscode": "000300.SH", "name": "沪深300", "is_sector": False,
                    "price_change_ratio_pct": 0.03, "last_price": 3984.84},
                   {"thscode": "000852.SH", "name": "中证1000", "is_sector": False,
                    "price_change_ratio_pct": -1.88, "last_price": 5938.90}]
        section, _ = sector_rrg.build_cross_section(
            rows + indices, market=market, indices=indices,
            limit_up=[{"name": "盛剑科技", "limit_up_reason": "光刻胶+半导体装备"}],
            as_of="2026-11-19", why="板块日 K 只有 20 根，短窗 RRG 要 89 根")
        payload = dict(build_stocks.DEMO_PAYLOAD)
        payload["indices"] = indices + rows
        payload["market"] = market
        payload["sector_cross"] = section
        return payload

    def test_text_panel_puts_compact_volume_price_results_first(self):
        text = build_stocks.render_txt(self._payload(), "2026-11-19 15:10:00")
        self.assertIn("【结果 · 量价】", text)
        self.assertIn("[市场]", text)
        self.assertIn("[量价]", text)
        self.assertNotIn("【板块轮动 · RRG】", text)
        self.assertLess(text.index("【结果 · 量价】"), text.index("【市场概览】"))
        self.assertNotIn("为什么是横截面", text)

    def test_html_epub_and_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stocks_data.json"
            path.write_text(json.dumps(self._payload(), ensure_ascii=False), encoding="utf-8")
            data = build_stocks.build(str(path), tmp)
            self.assertEqual(data["counts"]["cross"], data["cross"]["counts"]["total"])
            self.assertEqual(data["volume_price"]["counts"]["total"], data["counts"]["cross"])
            self.assertIsNone(data["rrg"])
            html = (Path(tmp) / "stocks.html").read_text(encoding="utf-8")
            self.assertIn("结果 · 量价分析", html)
            self.assertIn("result-grid", html)
            self.assertIn("function crossSection", html)
            with zipfile.ZipFile(Path(tmp) / "stocks.epub") as zf:
                chapter = zf.read("OEBPS/chapter.xhtml").decode("utf-8")
            self.assertIn("结果 · 量价分析", chapter)
            self.assertIn('class="card"', chapter)
            # 删掉结果卡 → 自检要报
            (Path(tmp) / "stocks.txt").write_text("同花顺行情面板\n", encoding="utf-8")
            problems = build_stocks.check(tmp)
            self.assertTrue(any("结果卡" in problem for problem in problems))

    def test_rrg_wins_when_both_are_present(self):
        payload = self._payload()
        payload["sector_rrg"] = sector_rrg.demo_section()
        text = build_stocks.render_txt(payload, "2026-11-19 15:10:00")
        self.assertIn("【结果 · 量价】", text)
        self.assertIn("【板块轮动 · RRG】", text)
        # RRG 与量价结果可以同时显示；旧 cross 兼容字段仍保持互斥。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stocks_data.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            build_stocks.build(str(path), tmp)
            manifest = json.loads((Path(tmp) / "stocks.json").read_text(encoding="utf-8"))
            self.assertIsNotNone(manifest["rrg"])
            # 互斥由 final_sections 裁决：cross 不再附带，但独立量价结果保留。
            self.assertIsNone(manifest["cross"])
            self.assertEqual(manifest["counts"]["cross"], 0)
            self.assertIsNotNone(manifest["volume_price"])
            self.assertEqual(build_stocks.check(tmp), [])


class RenderTest(unittest.TestCase):
    def _payload(self):
        payload = dict(build_stocks.DEMO_PAYLOAD)
        payload["sector_rrg"] = sector_rrg.demo_section()
        payload["indices"] = build_stocks._demo_indices(payload)
        return payload

    def test_text_panel_leads_with_results_and_keeps_compact_rrg(self):
        text = build_stocks.render_txt(self._payload(), "2026-11-20 15:10:00")
        self.assertIn("【结果 · 量价】", text)
        self.assertIn("【板块轮动 · RRG】", text)
        self.assertLess(text.index("【结果 · 量价】"), text.index("【板块轮动 · RRG】"))
        self.assertLess(text.index("【板块轮动 · RRG】"), text.index("【市场概览】"))
        self.assertIn("领先(", text)
        self.assertNotIn("hugo2046/QuantsPlaybook", text)
        # RRG 结果卡不该出现买卖字眼
        rrg_part = text[text.index("【板块轮动 · RRG】"):]
        for word in ("买入", "卖出、", "建议买"):
            self.assertNotIn(word, rrg_part)

    def test_html_and_epub_carry_the_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stocks_data.json"
            path.write_text(json.dumps(self._payload(), ensure_ascii=False),
                            encoding="utf-8")
            data = build_stocks.build(str(path), tmp)
            html = (Path(tmp) / "stocks.html").read_text(encoding="utf-8")
            self.assertIn("板块轮动结果", html)
            self.assertIn("function rrgSection", html)
            self.assertIn("resultCard('领先信号'", html)
            self.assertEqual(data["counts"]["rrg"], len(data["rrg"]["rows"]))
            self.assertIsNotNone(data["volume_price"])
            with zipfile.ZipFile(Path(tmp) / "stocks.epub") as zf:
                chapter = zf.read("OEBPS/chapter.xhtml").decode("utf-8")
            self.assertIn("板块轮动结果", chapter)
            self.assertIn("结果 · 量价分析", chapter)
            self.assertNotIn("<svg", chapter)   # crengine 不认 SVG

    def test_check_catches_a_missing_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stocks_data.json"
            path.write_text(json.dumps(self._payload(), ensure_ascii=False),
                            encoding="utf-8")
            build_stocks.build(str(path), tmp)
            html_path = Path(tmp) / "stocks.html"
            html_path.write_text(html_path.read_text(encoding="utf-8")
                                 .replace("板块轮动结果", "板块轮动"), encoding="utf-8")
            problems = build_stocks.check(tmp)
            self.assertTrue(any("板块轮动" in problem for problem in problems))

    def test_rebuild_from_manifest_keeps_the_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stocks_data.json"
            path.write_text(json.dumps(self._payload(), ensure_ascii=False),
                            encoding="utf-8")
            first = build_stocks.build(str(path), tmp)
            again = build_stocks.build(str(Path(tmp) / "stocks.json"), tmp)
            self.assertEqual(first["counts"]["rrg"], again["counts"]["rrg"])
            self.assertEqual(first["rrg"]["as_of"], again["rrg"]["as_of"])


class QuantSignalEnrichmentTest(unittest.TestCase):
    """测试隔夜/日内 APM 拆分、CLV 分位、0–100 情绪温度计、CCK 羊群效应与涨停微观结构。"""

    def test_intraday_split_sentiment_and_herding_on_real_like_snapshot(self):
        sectors = [
            # 有色金属：隔夜低开 -1.52%，日内逆势走阳 +0.39%，收盘分位 0.81 → 盘中真承接
            {"thscode": "881103.TI", "name": "有色金属", "is_sector": True,
             "prev_price": 100.0, "open_price": 98.48, "high_price": 99.10,
             "low_price": 98.00, "last_price": 98.86,
             "price_change_ratio_pct": -1.14, "turnover": 469e8},
            # 半导体：占两市成交 16.5%，冲高留长上影（CLV 0.28，振幅 3.3%）→ 放量长上影预警
            {"thscode": "881121.TI", "name": "半导体", "is_sector": True,
             "prev_price": 100.0, "open_price": 99.55, "high_price": 101.60,
             "low_price": 98.30, "last_price": 99.20,
             "price_change_ratio_pct": -0.80, "turnover": 2163e8},
            # 软件开发：隔夜高开 +0.33%，日内杀跌 -3.62%，分位 0.11 → 高开低走派发
            {"thscode": "881131.TI", "name": "软件开发", "is_sector": True,
             "prev_price": 100.0, "open_price": 100.33, "high_price": 100.50,
             "low_price": 96.30, "last_price": 96.70,
             "price_change_ratio_pct": -3.30, "turnover": 275e8},
            {"thscode": "881101.TI", "name": "文化传媒", "is_sector": True,
             "prev_price": 100.0, "open_price": 99.50, "high_price": 99.60,
             "low_price": 95.00, "last_price": 95.31,
             "price_change_ratio_pct": -4.69, "turnover": 276e8},
            {"thscode": "881102.TI", "name": "IT服务", "is_sector": True,
             "prev_price": 100.0, "open_price": 99.80, "high_price": 99.90,
             "low_price": 95.50, "last_price": 95.81,
             "price_change_ratio_pct": -4.19, "turnover": 431e8},
        ]
        indices = [
            {"thscode": "000300.SH", "name": "沪深300", "is_sector": False,
             "prev_price": 100.0, "open_price": 99.72, "high_price": 100.20,
             "low_price": 99.10, "last_price": 100.03, "price_change_ratio_pct": 0.03},
            {"thscode": "000688.SH", "name": "科创50", "is_sector": False,
             "prev_price": 100.0, "open_price": 99.54, "high_price": 102.20,
             "low_price": 99.00, "last_price": 101.83, "price_change_ratio_pct": 1.83},
            {"thscode": "000852.SH", "name": "中证1000", "is_sector": False,
             "prev_price": 100.0, "open_price": 99.68, "high_price": 99.80,
             "low_price": 97.70, "last_price": 98.12, "price_change_ratio_pct": -1.88},
        ]
        market = {"count": 5584, "up": 649, "down": 4876, "flat": 48,
                  "limit_up": 27, "limit_down": 84, "avg_ratio": -2.55,
                  "turnover": 13145.8e8}
        limit_up = [
            {"name": "盛剑科技", "limit_up_time": "09:37:00",
             "continue_day_text": "3连板", "seal_money": 7e6,
             "limit_up_reason": "光刻胶+半导体"},
            {"name": "京蓝科技", "limit_up_time": "13:25:00",
             "continue_day_text": "2连板", "seal_money": 8e6,
             "limit_up_reason": "铟资源+半导体"},
            {"name": "兴业股份", "limit_up_time": "13:25:00",
             "continue_day_text": "首板", "seal_money": 1e6,
             "limit_up_reason": "光刻胶树脂"},
        ]

        section, notes = sector_rrg.build_cross_section(
            sectors + indices, market=market, indices=indices,
            limit_up=limit_up, as_of="2026-11-19"
        )
        self.assertEqual(notes, [])
        # 1) 盘中真承接识别出有色金属（低开走阳）
        support_names = [r["name"] for r in section["intraday"]["true_support"]]
        self.assertIn("有色金属", support_names)
        # 2) 冲高回落/高开派发识别出半导体（放量长上影）与软件开发（高开低走派发）
        fade_by_name = {r["name"]: r["tag"] for r in section["intraday"]["fade_warning"]}
        self.assertEqual(fade_by_name.get("半导体"), "放量长上影")
        self.assertEqual(fade_by_name.get("软件开发"), "高开低走派发")
        # 3) 涨停池微观结构
        lu_s = section["limit_up_structure"]
        self.assertEqual(lu_s["max_board"], 3)
        self.assertEqual(lu_s["max_board_name"], "盛剑科技")
        self.assertEqual(lu_s["multi_board_count"], 2)
        self.assertEqual(lu_s["early_count"], 1)
        self.assertEqual(lu_s["afternoon_count"], 2)
        # 4) 0-100 情绪温度计与「指数托底、个股冰点」背离
        sent = section["sentiment"]
        self.assertTrue(0 <= sent["score"] <= 100)
        self.assertEqual(sent["divergence"], "指数托底、个股冰点")
        # 5) 叙述文字里包含 APM 隔夜/日内拆分与情绪温度
        joined = "\n".join(section["narrative"])
        self.assertIn("隔夜 vs 日内", joined)
        self.assertIn("情绪温度", joined)


if __name__ == "__main__":
    unittest.main()
