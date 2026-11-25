"""板块情绪 EW-SDM（单日横截面）：公式、数据对接、段落与面板排版。

公式照 `autumn-go/strategy-factor-dashboard` 的 `ews_engine.py` 逐条核对；
关键数值用手算值锁住。数据全是造的，不打网络。
"""

import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ew_sdm  # noqa: E402
import fetch_stocks  # noqa: E402


def _to_float(value):
    return fetch_stocks._num(value)


class ParseStreakTest(unittest.TestCase):
    def test_first_board_and_streaks(self):
        self.assertEqual(ew_sdm.parse_streak("首板"), 1)
        self.assertEqual(ew_sdm.parse_streak("3连板"), 3)
        self.assertEqual(ew_sdm.parse_streak("5 连板"), 5)

    def test_unparseable_text_in_pool_counts_as_first_board(self):
        # 池里出现的股票一定涨停，连板数至少为 1。
        self.assertEqual(ew_sdm.parse_streak(""), 1)
        self.assertEqual(ew_sdm.parse_streak(None), 1)
        self.assertEqual(ew_sdm.parse_streak("涨停"), 1)


class EmotionWeightTest(unittest.TestCase):
    def test_rules_follow_original(self):
        # 非涨停：涨幅 > 3% 记 1.0，否则 0.0
        self.assertEqual(ew_sdm.emotion_weight(4.0, 0), 1.0)
        self.assertEqual(ew_sdm.emotion_weight(3.0, 0), 0.0)  # 恰好 3% 不算趋势
        self.assertEqual(ew_sdm.emotion_weight(-2.0, 0), 0.0)
        # 首板 1.5；连板 n≥2 为 1 + 0.5n
        self.assertEqual(ew_sdm.emotion_weight(10.0, 1), 1.5)
        self.assertEqual(ew_sdm.emotion_weight(10.0, 2), 2.0)
        self.assertEqual(ew_sdm.emotion_weight(10.0, 3), 2.5)

    def test_broken_board_negative_feedback(self):
        self.assertEqual(ew_sdm.emotion_weight(-6.0, 0, is_broken=True), -1.0)
        # 炸板但跌幅没到 5%，不触发负反馈
        self.assertEqual(ew_sdm.emotion_weight(-3.0, 0, is_broken=True), 0.0)


class SScoreTest(unittest.TestCase):
    def test_bounds_and_centre(self):
        self.assertEqual(ew_sdm.s_score(0), 1.0)
        self.assertEqual(ew_sdm.s_score(None), 1.0)
        # 中心 3.5：2 × 0.5 + 1 = 2
        self.assertAlmostEqual(ew_sdm.s_score(3.5), 2.0, places=9)
        # 连板越多越接近 3，但不超过 3
        self.assertLess(ew_sdm.s_score(10), 3.0)
        self.assertGreater(ew_sdm.s_score(10), ew_sdm.s_score(5))

    def test_two_boards_value(self):
        expected = 2.0 / (1.0 + math.exp(1.5)) + 1.0
        self.assertAlmostEqual(ew_sdm.s_score(2), expected, places=9)


class SectorFactorTest(unittest.TestCase):
    def test_hand_computed_two_stock_sector(self):
        # A：成交额 100，涨 10%，2 连板，封单 50 → 权重 150，情绪 2.0
        # B：成交额 300，跌 1%，非涨停 → 权重 300，情绪 0.0
        quotes = {"A": {"pct": 10.0, "amount": 100.0},
                  "B": {"pct": -1.0, "amount": 300.0}}
        limits = {"A": {"streak": 2, "seal": 50.0}}
        out = ew_sdm.sector_factor(["A", "B"], quotes, limits, name="测试")
        self.assertAlmostEqual(out["momentum"], (150 * 10 - 300) / 450, places=9)
        self.assertAlmostEqual(out["emotion_diff"], (150 * 2.0) / 450, places=9)
        self.assertAlmostEqual(out["s_score"], 2.0 / (1 + math.exp(1.5)) + 1, places=9)
        self.assertAlmostEqual(out["final_score"], 2.42642, places=4)
        self.assertEqual(out["members"], 2)
        self.assertEqual(out["priced"], 2)
        self.assertEqual(out["limit_up_count"], 1)
        self.assertEqual(out["up_count"], 1)
        self.assertEqual(out["max_streak"], 2)
        self.assertEqual(out["amount"], 400.0)

    def test_unpriced_members_are_not_weighted(self):
        quotes = {"A": {"pct": 2.0, "amount": 100.0}}
        out = ew_sdm.sector_factor(["A", "GHOST"], quotes, {}, name="x")
        self.assertEqual(out["members"], 2)
        self.assertEqual(out["priced"], 1)
        self.assertAlmostEqual(out["momentum"], 2.0, places=9)

    def test_no_price_gives_zero_not_crash(self):
        out = ew_sdm.sector_factor(["A"], {}, {}, name="x")
        self.assertEqual(out["priced"], 0)
        self.assertEqual(out["final_score"], 0.0)
        self.assertEqual(out["s_score"], 1.0)

    def test_pct_is_clipped_like_original(self):
        quotes = {"A": {"pct": 80.0, "amount": 1.0}}
        out = ew_sdm.sector_factor(["A"], quotes, {}, name="x")
        self.assertAlmostEqual(out["momentum"], 30.0, places=9)

    def test_zero_weight_sector(self):
        quotes = {"A": {"pct": 5.0, "amount": 0.0}}
        out = ew_sdm.sector_factor(["A"], quotes, {}, name="x")
        self.assertEqual(out["momentum"], 0.0)
        self.assertEqual(out["final_score"], 0.0)

    def test_duplicate_members_counted_once(self):
        quotes = {"A": {"pct": 1.0, "amount": 10.0}}
        out = ew_sdm.sector_factor(["A", "A"], quotes, {}, name="x")
        self.assertEqual(out["members"], 1)


class ParsersTest(unittest.TestCase):
    def test_quotes_and_limits_from_raw_rows(self):
        quotes = ew_sdm.quotes_from_market(
            [{"thscode": "600000.SH", "price_change_ratio_pct": "1.5", "turnover": 2e8},
             {"thscode": "", "price_change_ratio_pct": 9},
             "junk"], _to_float)
        self.assertEqual(quotes, {"600000.SH": {"pct": 1.5, "amount": 2e8}})
        limits = ew_sdm.limits_from_pool(
            [{"thscode": "002579.SZ", "continue_day_text": "5连板", "seal_money": 1e8},
             {"thscode": "603360.SH", "continue_day_text": "首板", "seal_money": None}])
        self.assertEqual(limits["002579.SZ"], {"streak": 5, "seal": 1e8})
        self.assertEqual(limits["603360.SH"], {"streak": 1, "seal": 0.0})


class BuildSectionTest(unittest.TestCase):
    def setUp(self):
        self.sectors = [("881101.TI", "种植业"), ("881102.TI", "养殖业"), ("881103.TI", "空板块")]
        self.members = {
            "881101.TI": [{"thscode": "A", "name": "a"}, {"thscode": "B", "name": "b"}],
            "881102.TI": [{"thscode": "C", "name": "c"}],
            "881103.TI": [{"thscode": "Z", "name": "z"}],
        }
        self.quotes = {"A": {"pct": 4.0, "amount": 100.0},
                       "B": {"pct": 1.0, "amount": 100.0},
                       "C": {"pct": -3.0, "amount": 100.0}}

    def test_sorted_by_score_and_no_data_last(self):
        section = ew_sdm.build_section(self.sectors, self.members, self.quotes, {},
                                       as_of="2026-11-25", pool_size=0, market_limit_up=0)
        names = [row["name"] for row in section["rows"]]
        self.assertEqual(names[:2], ["种植业", "养殖业"])
        self.assertEqual(names[-1], "空板块")
        self.assertEqual(section["rows"][-1]["status"], "no_data")
        self.assertGreater(section["rows"][0]["final_score"], section["rows"][1]["final_score"])
        self.assertEqual(section["coverage"]["sectors"], 3)
        self.assertEqual(section["coverage"]["sectors_ok"], 2)
        self.assertEqual(section["coverage"]["priced"], 3)
        self.assertEqual(section["as_of"], "2026-11-25")
        self.assertEqual(section["source"]["repo"], "autumn-go/strategy-factor-dashboard")

    def test_text_render_lists_strong_and_weak(self):
        section = ew_sdm.build_section(self.sectors, self.members, self.quotes, {},
                                       as_of="2026-11-25", notes=["口径测试"])
        text = "\n".join(ew_sdm.render_text_lines(section, limit=1))
        self.assertIn("【板块情绪 · EW-SDM】单日横截面（2026-11-25）", text)
        self.assertIn("强：", text)
        self.assertIn("弱：", text)
        self.assertIn("注：口径测试", text)
        self.assertIn("种植业", text)
        self.assertIn("养殖业", text)
        # 弱侧第一行应是分数最低的板块
        weak_line = text.split("弱：")[1].splitlines()[1]
        self.assertIn("养殖业", weak_line)

    def test_empty_and_missing_sections(self):
        self.assertEqual(ew_sdm.render_text_lines(None), [])
        section = ew_sdm.build_section([], {}, {}, {})
        text = "\n".join(ew_sdm.render_text_lines(section))
        self.assertIn("暂无可算板块", text)

    def test_notes_describe_real_gaps(self):
        notes = ew_sdm.build_notes(pool_size=20, market_limit_up=57, priced=900, members=1000)
        joined = "；".join(notes)
        self.assertIn("炸板/跌停数据未接入", joined)
        self.assertIn("涨停池只取前 20 只，市场涨停 57 只", joined)
        self.assertIn("有快照行情 900/1000 只", joined)
        none_pool = "；".join(ew_sdm.build_notes(0, 57, 1, 2))
        self.assertIn("涨停池这轮没取到", none_pool)
        self.assertNotIn("只取前", none_pool)
        full = "；".join(ew_sdm.build_notes(80, 57, 1, 2))
        self.assertNotIn("只取前", full)


class FetchIntegrationTest(unittest.TestCase):
    def test_build_ew_sdm_uses_cached_members_without_network(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "members.json"
            cache.write_text(json.dumps({
                "updated": today,
                "sectors": {"881101.TI": [{"thscode": "600000.SH", "name": "甲"}],
                            "881102.TI": [{"thscode": "600001.SH", "name": "乙"}]},
            }, ensure_ascii=False), encoding="utf-8")

            class NoNetwork:
                def get(self, *_args, **_kwargs):
                    raise AssertionError("缓存新鲜时不该请求网络")

            market = [{"thscode": "600000.SH", "price_change_ratio_pct": 5.0, "turnover": 1e9},
                      {"thscode": "600001.SH", "price_change_ratio_pct": -2.0, "turnover": 1e9}]
            pool = [{"thscode": "600000.SH", "continue_day_text": "2连板", "seal_money": 1e8}]
            section = fetch_stocks.build_ew_sdm(
                NoNetwork(), [("881101.TI", "种植业"), ("881102.TI", "养殖业")], market, pool,
                members_cache=str(cache), pool_size=1, market_limit_up=57,
                as_of="2026-11-25")
        self.assertEqual(section["coverage"]["sectors_ok"], 2)
        self.assertEqual(section["rows"][0]["name"], "种植业")
        self.assertGreater(section["rows"][0]["final_score"], 0)
        # 养殖业：只跌 2%、没有涨停 → 情绪为 0，分为 0（原版公式如此）
        self.assertEqual(section["rows"][1]["final_score"], 0.0)
        self.assertLess(section["rows"][1]["momentum"], 0)
        self.assertTrue(any("涨停池只取前 1 只" in n for n in section["notes"]))


if __name__ == "__main__":
    unittest.main()
