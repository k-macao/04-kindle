"""指数择时：ICU 均线与 RSRS/QRS 的公式、穿越事件与面板行。

参考值来自原仓库 `hugo2046/QuantsPlaybook` 的实现（scipy.stats.siegelslopes 与
`SignalMaker/qrs.py` 的 QRSCreator），在开发机上对同一组确定性数据跑出，写死在这里。
CI 只用标准库，不依赖 numpy / pandas / scipy。
"""

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import timing_quants as tq  # noqa: E402

# 14 根确定性日线（4 位小数），与生成参考值时用的完全一致。
CLOSES = [100.0, 102.036, 103.8918, 105.4074, 106.4597, 106.977, 106.9465,
          106.4154, 105.4864, 104.3056, 103.0472, 101.8936, 101.016, 100.5549]
LOWS = [98.2, 100.3739, 102.5166, 104.2044, 105.1558, 105.3919, 105.1584,
        104.6892, 104.0301, 103.0789, 101.7989, 100.3923, 99.2628, 98.7827]
HIGHS = [101.2, 103.5581, 105.5845, 107.039, 107.8272, 108.0016, 107.7107,
         107.1242, 106.3708, 105.514, 104.5757, 103.5877, 102.6433, 101.9144]

# 参考：ICU(5) 末值 = scipy siegelslopes(hierarchical) 的 intercept + slope × 4
REF_ICU_LAST = 100.00040000000001
# 参考：QRSCreator.fit(4, 6, n=2)，末 6 个值（adjust / 不 adjust）
REF_QRS_ADJ = [1.1352594953, 0.2398827541, -0.7312336333,
               -1.1786848337, -1.1560345756, -0.3672896083]
REF_QRS_NOADJ = [1.1235118877, 0.2370651371, -0.723938309,
                 -1.1733152069, -1.1505722528, -0.36437875]


class SiegelFitTest(unittest.TestCase):
    def test_exact_line(self):
        intercept, slope = tq.siegel_fit([1.0, 3.0, 5.0, 7.0, 9.0])
        self.assertAlmostEqual(intercept, 1.0, places=12)
        self.assertAlmostEqual(slope, 2.0, places=12)

    def test_single_outlier_is_ignored(self):
        # 重复中位数对离群点不敏感：斜率仍接近真实的 1
        y = [0.0, 1.0, 2.0, 3.0, 100.0, 5.0, 6.0]
        _intercept, slope = tq.siegel_fit(y)
        self.assertAlmostEqual(slope, 1.0, places=6)

    def test_needs_two_points(self):
        with self.assertRaises(ValueError):
            tq.siegel_fit([1.0])


class IcuTest(unittest.TestCase):
    def test_last_value_matches_reference(self):
        icu = tq.icu_ma(CLOSES, 5)
        self.assertIsNone(icu[3])
        self.assertAlmostEqual(icu[-1], REF_ICU_LAST, places=9)

    def test_cross_events_on_icu(self):
        closes = [10, 9, 8, 9, 11, 12, 10, 8]
        icu = [None, None, 9.5, 9.5, 9.5, 9.5, 9.5, 9.5]
        events, state = tq.icu_cross_events(closes, icu)
        # t=4 收盘 11 上穿 9.5 开仓；t=7 收盘 8 下穿 9.5 平仓（t=6 仍在均线上方）
        self.assertEqual(events, [(4, "open"), (7, "close")])
        self.assertEqual(state, 0)


class QrsTest(unittest.TestCase):
    def test_adjusted_matches_reference(self):
        sig = tq.qrs_series(HIGHS, LOWS, 4, 6, 2, True)
        tail = sig[4 + 6 - 2:]
        self.assertEqual(len(tail), len(REF_QRS_ADJ))
        for got, want in zip(tail, REF_QRS_ADJ):
            self.assertAlmostEqual(got, want, places=8)

    def test_unadjusted_matches_reference(self):
        sig = tq.qrs_series(HIGHS, LOWS, 4, 6, 2, False)
        tail = sig[4 + 6 - 2:]
        for got, want in zip(tail, REF_QRS_NOADJ):
            self.assertAlmostEqual(got, want, places=8)

    def test_warmup_is_none(self):
        sig = tq.qrs_series(HIGHS, LOWS, 4, 6, 2, True)
        self.assertTrue(all(v is None for v in sig[:4 + 6 - 2]))

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            tq.qrs_series([1.0, 2.0], [1.0], 1, 1)

    def test_cross_events_follow_backtrader_rules(self):
        series = [-1.0, 0.5, 0.8, 0.6, -0.8, -0.9, 0.9]
        events, state = tq.cross_events(series, 0.7, -0.7)
        # 上穿 0.7 开仓（t=2）；下穿 -0.7 平仓（t=4）；再上穿开仓（t=6）
        self.assertEqual(events, [(2, "open"), (4, "close"), (6, "open")])
        self.assertEqual(state, 1)

    def test_no_double_open(self):
        series = [0.0, 0.8, 0.9, 1.0]  # 只有第一次上穿算数
        events, _ = tq.cross_events(series, 0.7, -0.7)
        self.assertEqual(events, [(1, "open")])


class BuildRowTest(unittest.TestCase):
    def _dates(self, n):
        return [1_700_000_000_000 + i * 86_400_000 for i in range(n)]

    def test_uptrend_row(self):
        # 先跌后涨：收盘价上穿 ICU 均线后一直在上方，应为「上方 / 持有」，上穿发生在 24 根前
        closes = [100.0 - i for i in range(15)] + [85 + 2.0 * (1.05 ** j) for j in range(25)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        row = tq.build_row("000300.SH", "沪深300", self._dates(len(closes)), highs, lows, closes)
        self.assertEqual(row["icu_side"], "上方")
        self.assertEqual(row["icu_state"], "持有")
        self.assertIsNotNone(row["icu"])
        self.assertAlmostEqual(row["close"], closes[-1], places=9)
        self.assertEqual(row["icu_last_cross"]["side"], "上穿")
        self.assertEqual(row["icu_last_cross"]["bars_ago"], 24)
        # 只有 40 根日线，QRS 不够 617 根：这是唯一允许的说明
        self.assertEqual(row["notes"], ["日线 40 根，QRS 需要 617 根"])

    def test_missing_high_low_notes(self):
        closes = [100 + i for i in range(10)]
        row = tq.build_row("x", "x", self._dates(10), None, None, closes)
        self.assertIsNone(row["qrs"])
        self.assertTrue(any("没有最高/最低价" in n for n in row["notes"]))
        self.assertIsNotNone(row["icu"])

    def test_short_history_notes(self):
        closes = [100 + i for i in range(30)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        row = tq.build_row("x", "x", self._dates(30), highs, lows, closes)
        self.assertIsNone(row["qrs"])
        self.assertTrue(any("QRS 需要" in n for n in row["notes"]))

    def test_missing_close_does_not_crash(self):
        row = tq.build_row("x", "x", [], [], [], [])
        self.assertIsNone(row["close"])
        self.assertTrue(row["notes"])


class SectionTest(unittest.TestCase):
    def _bars(self, closes):
        return [[1_700_000_000_000 + i * 86_400_000, c + 1, c - 1, c]
                for i, c in enumerate(closes)]

    def test_missing_history_keeps_row(self):
        history = {"000300.SH": self._bars([100 - i for i in range(15)]
                                           + [85 + 2.0 * (1.05 ** j) for j in range(25)])}
        indices = [("000300.SH", "沪深300"), ("000905.SH", "中证500")]
        section = tq.build_section(indices, history, as_of="2026-11-25")
        self.assertEqual(section["coverage"], {"indices": 2, "with_close": 1, "with_qrs": 0})
        missing = section["rows"][1]
        self.assertEqual(missing["name"], "中证500")
        self.assertIsNone(missing["close"])
        self.assertIn("指数日线没取到", missing["notes"])

    def test_render_marks_tie_and_missing(self):
        closes = [100.0 - i for i in range(15)] + [85 + 2.0 * (1.05 ** j) for j in range(25)]
        history = {"000300.SH": self._bars(closes)}
        section = tq.build_section([("000300.SH", "沪深300"), ("000905.SH", "中证500")],
                                   history, as_of="2026-11-25")
        lines = tq.render_text_lines(section)
        self.assertTrue(lines[0].startswith("【指数择时"))
        self.assertTrue(any("沪深300 收" in ln and "上方" in ln for ln in lines))
        self.assertTrue(any(ln.startswith("  中证500：") and "指数日线没取到" in ln for ln in lines))
        brief = tq.render_text_lines(section, brief=True)
        self.assertFalse(any(ln.lstrip().startswith("QRS") for ln in brief))

    def test_tie_is_shown_as_on_line(self):
        # 5 点 Siegel 拟合：[10, 10.5, 10.2, 10.9, 10.7] 的末值 ICU 恰好是 10.7 = 收盘 → 显示「贴线」
        ys = [10.0, 10.5, 10.2, 10.9, 10.7]
        bars = [[1_700_000_000_000 + i * 86_400_000, y + 1, y - 1, y] for i, y in enumerate(ys)]
        section = tq.build_section([("x", "测试")], {"x": bars})
        row = section["rows"][0]
        self.assertAlmostEqual(row["icu"], 10.7, places=9)
        self.assertEqual(row["icu_side"], "下方")  # 数据层：收盘不高于 ICU
        lines = tq.render_text_lines(section)
        self.assertTrue(any("ICU 10.70（贴线）" in ln for ln in lines), lines)


class SourceTest(unittest.TestCase):
    def test_sources_point_at_repo(self):
        self.assertEqual(tq.SOURCE["icu"]["repo"], "hugo2046/QuantsPlaybook")
        self.assertIn("ICU均线", tq.SOURCE["icu"]["file"])
        self.assertIn("qrs.py", tq.SOURCE["qrs"]["file"])
        self.assertEqual(tq.QRS_MIN_BARS, 617)
        self.assertTrue(math.isclose(tq.QRS_PARAMS["upper"], 0.7))


if __name__ == "__main__":
    unittest.main()
