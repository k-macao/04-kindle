"""大 V 近五天报告：窗口、校验、渲染（纯标准库）。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bigv_report as br  # noqa: E402


def people_doc():
    return {"people": [
        {"id": "a", "market": "US", "name": "甲", "org": "机构甲", "role": "策略", "note": ""},
        {"id": "b", "market": "CN", "name": "乙", "org": "机构乙", "role": "策略", "note": "备注乙"},
        {"id": "c", "market": "CN", "name": "丙", "org": "机构丙", "role": "策略", "note": ""},
    ]}


def item(person, day, dim="观点", summary="要点", url="https://example.com/x"):
    return {"person": person, "date": day, "dimension": dim, "title": f"{person}-{day}",
            "url": url, "summary": summary, "source": "来源"}


class WindowTest(unittest.TestCase):
    def test_five_days_inclusive(self):
        self.assertEqual(br.window("2026-11-25", 5), ("2026-11-21", "2026-11-25"))

    def test_boundaries(self):
        items = [item("a", "2026-11-20"), item("a", "2026-11-21"),
                 item("a", "2026-11-25"), item("a", "2026-11-26")]
        s = br.summarize(people_doc()["people"], items,
                         "2026-11-25", 5)
        self.assertEqual(s["items_in_window"], 2)
        self.assertEqual(s["rows"][0]["count"], 2)


class SummarizeTest(unittest.TestCase):
    def test_counts_by_dimension_and_coverage(self):
        items = [item("a", "2026-11-24", "观点"), item("a", "2026-11-23", "采访"),
                 item("b", "2026-11-22", "报道")]
        s = br.summarize(people_doc()["people"], items, "2026-11-25", 5)
        a = s["rows"][0]
        self.assertEqual(a["count"], 2)
        self.assertEqual(a["dimensions"], {"观点": 1, "采访": 1})
        self.assertEqual(a["latest"], "2026-11-24")
        self.assertEqual(s["coverage"], {"US": {"people": 1, "with_items": 1},
                                         "CN": {"people": 2, "with_items": 1}})
        self.assertEqual(s["dims_seen"], ["观点", "采访", "报道"])
        self.assertIn("发言", s["dims_missing"])

    def test_latest_first(self):
        items = [item("a", "2026-11-22"), item("a", "2026-11-24")]
        s = br.summarize(people_doc()["people"], items, "2026-11-25", 5)
        self.assertEqual([it["date"] for it in s["rows"][0]["items"]],
                         ["2026-11-24", "2026-11-22"])


class ValidateTest(unittest.TestCase):
    def _items(self, **override):
        base = item("a", "2026-11-24")
        base.update(override)
        return {"items": [base]}

    def test_unknown_person(self):
        with self.assertRaises(br.DataError):
            br.validate(people_doc(), self._items(person="zzz"))

    def test_bad_date(self):
        with self.assertRaises(br.DataError):
            br.validate(people_doc(), self._items(date="11/24"))

    def test_bad_dimension(self):
        with self.assertRaises(br.DataError):
            br.validate(people_doc(), self._items(dimension="推文"))

    def test_url_must_be_https(self):
        with self.assertRaises(br.DataError):
            br.validate(people_doc(), self._items(url="http://example.com"))

    def test_duplicate_person_id(self):
        doc = people_doc()
        doc["people"].append(dict(doc["people"][0]))
        with self.assertRaises(br.DataError):
            br.validate(doc, {"items": []})

    def test_ai_prob_out_of_range(self):
        doc = people_doc()
        doc["people"][0]["ai_bull_prob"] = 1.5
        with self.assertRaises(br.DataError):
            br.validate(doc, {"items": []})

    def test_valid_passes(self):
        people, items = br.validate(people_doc(), self._items())
        self.assertEqual(len(people), 3)
        self.assertEqual(len(items), 1)


class AiViewTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(br.ai_view_label(0.7), "偏多 70%")
        self.assertEqual(br.ai_view_label(0.3), "偏空 70%")
        self.assertEqual(br.ai_view_label(0.5), "偏多 50%")
        self.assertEqual(br.ai_view_label(None), "—")

    def test_report_header_says_it_is_ai_judgement(self):
        text = br.render_markdown(br.summarize(people_doc()["people"], [], "2026-11-25", 5))
        self.assertIn(br.AI_VIEW_NOTE, text)
        self.assertIn("AI 多空判断", text)


class RenderTest(unittest.TestCase):
    def test_empty_person_and_missing_dims_are_stated(self):
        items = [item("a", "2026-11-24", summary="看多")]
        s = br.summarize(people_doc()["people"], items, "2026-11-25", 5)
        text = br.render_markdown(s)
        self.assertIn("五天内未找到公开观点：乙（备注乙）、丙。", text)
        self.assertIn("本期没有条目的维度：发言、评论", text)
        self.assertIn("[来源](https://example.com/x)", text)
        self.assertIn("乙（备注乙）", text)

    def test_pipe_in_cell_is_escaped(self):
        self.assertEqual(br._cell("a|b"), "a\\|b")


class StocksPanelTest(unittest.TestCase):
    """大 V 段接进股票面板：文本、清单、指纹、坏文件不挡行情。"""

    def setUp(self):
        import build_stocks
        self.bs = build_stocks

    def _section(self):
        items = [item("a", "2026-11-24", summary="看多一"), item("a", "2026-11-23", summary="看多二"),
                 item("b", "2026-11-22", "报道", summary="报道乙")]
        return br.summarize(people_doc()["people"], items, "2026-11-25", 5)

    def test_text_full_shows_two_per_person_and_brief_one(self):
        sec = self._section()
        full = "\n".join(self.bs.render_bigv_text(sec))
        brief = "\n".join(self.bs.render_bigv_text(sec, brief=True))
        self.assertIn("看多一", full)
        self.assertIn("看多二", full)
        self.assertIn("看多一", brief)
        self.assertNotIn("看多二", brief)
        self.assertIn("A 股五天内未找到：丙", full)

    def test_empty_window_says_so(self):
        sec = br.summarize(people_doc()["people"], [], "2026-11-25", 5)
        text = "\n".join(self.bs.render_bigv_text(sec))
        self.assertIn("窗口内没有条目", text)

    def test_bad_file_is_isolated(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "items.json"
            bad.write_text('{"items": [{"person": "nobody"}]}', encoding="utf-8")
            old = br.DEFAULT_ITEMS
            br.DEFAULT_ITEMS = bad
            try:
                section, error = self.bs.bigv_section({"data_timestamp": None})
            finally:
                br.DEFAULT_ITEMS = old
        self.assertIsNone(section)
        self.assertIn("大 V 观点未生成", error)

    def test_default_files_build_a_section(self):
        section, error = self.bs.bigv_section({"data_timestamp": 1_764_000_000_000})
        self.assertIsNone(error)
        self.assertEqual(len(section["rows"]), 32)
        self.assertRegex(section["window"][1], r"^\d{4}-\d{2}-\d{2}$")

    def test_hash_changes_when_items_change(self):
        base = {"bigv": self._section()}
        changed = {"bigv": br.summarize(people_doc()["people"], [item("a", "2026-11-24", summary="不同")],
                                        "2026-11-25", 5)}
        self.assertNotEqual(self.bs.data_hash(base), self.bs.data_hash(changed))

    def test_demo_build_writes_the_section(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.bs.build(demo=True, out_dir=tmp)
            txt = (Path(tmp) / "stocks.txt").read_text(encoding="utf-8")
            manifest = json.loads((Path(tmp) / "stocks.json").read_text(encoding="utf-8"))
        self.assertIn("【大 V 观点 · 近五天】", txt)
        self.assertIsNotNone(manifest.get("bigv"))


if __name__ == "__main__":
    unittest.main()
