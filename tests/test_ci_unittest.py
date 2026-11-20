"""tools/ci_unittest.py：CI 红了要能一眼看到是哪个用例挂的（日志常常拿不到）。

这里只测解析与注解生成——真跑 unittest 的那层交给工作流自己验证。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ci_unittest  # noqa: E402

TRANSCRIPT = """\
...FFFF...
======================================================================
FAIL: test_demo_section_uses_the_real_formula (test_sector_rrg.DemoTest.test_demo_section_uses_the_real_formula)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/runner/work/04-kindle/04-kindle/tests/test_sector_rrg.py", line 280, in test_demo_section_uses_the_real_formula
    self.assertEqual(section["as_of"], today)
AssertionError: '2026-11-20' != '2026-11-21'

======================================================================
ERROR: test_fetch (test_stocks.FetchTest.test_fetch) (code='000001')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/runner/work/04-kindle/04-kindle/tests/test_stocks.py", line 42, in test_fetch
    raise RuntimeError("接口挂了")
RuntimeError: 接口挂了

----------------------------------------------------------------------
Ran 518 tests in 1.182s

FAILED (failures=1, errors=1, skipped=23)
"""


class ParseFailuresTest(unittest.TestCase):
    def setUp(self):
        self.failures = ci_unittest.parse_failures(TRANSCRIPT)

    def test_finds_every_failing_case(self):
        self.assertEqual([case for case, _where, _msg in self.failures],
                         ["test_demo_section_uses_the_real_formula", "test_fetch"])

    def test_keeps_the_class_path(self):
        self.assertEqual(self.failures[0][1],
                         "test_sector_rrg.DemoTest.test_demo_section_uses_the_real_formula")

    def test_keeps_the_assertion_line(self):
        self.assertEqual(self.failures[0][2], "AssertionError: '2026-11-20' != '2026-11-21'")
        self.assertEqual(self.failures[1][2], "RuntimeError: 接口挂了")

    def test_subtest_parameter_does_not_break_the_header(self):
        # `FAIL: test_x (mod.Class) (key='值')` 的第二个括号要丢掉，否则模块名认不出来。
        output = ("FAIL: test_pairs (test_feed_xml.PairTest) (pair='EN→ZH')\n"
                  "----------------------------------------------------------------------\n"
                  "Traceback (most recent call last):\n"
                  "AssertionError: 配错\n")
        self.assertEqual(ci_unittest.parse_failures(output),
                         [("test_pairs", "test_feed_xml.PairTest", "AssertionError: 配错")])

    def test_clean_run_has_no_failures(self):
        self.assertEqual(ci_unittest.parse_failures("Ran 3 tests in 0.01s\n\nOK\n"), [])


class AnnotationLinesTest(unittest.TestCase):
    def test_one_annotation_per_failure_pointing_at_the_file(self):
        lines = ci_unittest.annotation_lines(
            ci_unittest.parse_failures(TRANSCRIPT))
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith(
            "::error file=tests/test_sector_rrg.py,title=测试失败::"))
        self.assertIn("test_demo_section_uses_the_real_formula", lines[0])
        self.assertIn("'2026-11-20' != '2026-11-21'", lines[0])
        self.assertTrue(lines[1].startswith("::error file=tests/test_stocks.py,"))

    def test_unparsed_case_still_gets_a_path(self):
        lines = ci_unittest.annotation_lines([("test_x", "", "")])
        self.assertTrue(lines[0].startswith("::error file=tests/test_x.py,"))

    def test_message_is_single_line(self):
        for line in ci_unittest.annotation_lines(ci_unittest.parse_failures(TRANSCRIPT)):
            self.assertEqual(line.count("\n"), 0)
            self.assertLess(len(line), 600)


if __name__ == "__main__":
    unittest.main()
