"""DeepSeek 每条新闻线索链分析：离线覆盖批次、缓存、格式守卫与 API 失败。"""

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import analyze_news  # noqa: E402

NOW = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)


def clue(title):
    return (f"线索：报道提到{title}；事实：这是输入摘要明确记载的事件；"
            "推断：可能形成后续影响，但仍需验证；影响：相关主体取决于事件后续发展；"
            "验证：观察后续公开数据和当事方公告。")


def payload(count=3):
    return {"items": [{"title": f"新闻{index}", "summary": f"摘要{index}",
                       "source": "CLS", "group": "flash",
                       "published": "2026-11-08T02:00:00Z",
                       "link": f"https://example.com/{index}"}
                      for index in range(count)]}


def fake_deepseek(calls=None, omit_id=None):
    def fetch(api_key, request, timeout):
        if calls is not None:
            calls.append((api_key, request, timeout))
        content = request["messages"][1]["content"]
        batch = json.loads(content.split("\n", 1)[1])["items"]
        rows = [{"id": item["id"], "analysis": clue(item["title"])}
                for item in batch if item["id"] != omit_id]
        return {"choices": [{"message": {"content": json.dumps(
            {"items": rows}, ensure_ascii=False)}}]}
    return fetch


class AnalyzePayloadTest(unittest.TestCase):
    def test_every_item_gets_clue_chain_and_batches_are_bounded(self):
        data = payload(6)
        calls = []
        stats = analyze_news.analyze_payload(
            data, "test-key", fetch=fake_deepseek(calls), batch_size=2, delay=0,
            log=lambda *_: None, now=NOW)
        self.assertEqual(stats["total"], 6)
        self.assertEqual(stats["analyzed"], 6)
        self.assertEqual(stats["batches"], 3)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(all(label in item["analysis"]
                                for label in analyze_news.REQUIRED_LABELS)
                            for item in data["items"]))
        self.assertEqual(calls[0][0], "test-key")

    def test_cache_prevents_repeat_api_calls(self):
        data = payload(2)
        cache = {}
        calls = []
        first = analyze_news.analyze_payload(data, "key", cache=cache,
                                             fetch=fake_deepseek(calls), delay=0,
                                             log=lambda *_: None, now=NOW)
        second_data = payload(2)
        second = analyze_news.analyze_payload(second_data, "key", cache=cache,
                                              fetch=fake_deepseek(calls), delay=0,
                                              log=lambda *_: None, now=NOW)
        self.assertEqual(first["analyzed"], 2)
        self.assertEqual(second["analyzed"], 0)
        self.assertEqual(second["cached"], 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second_data["items"][0]["analysis"], data["items"][0]["analysis"])

    def test_missing_api_key_fails_closed(self):
        with self.assertRaisesRegex(analyze_news.AnalysisError, "DEEPSEEK_API_KEY"):
            analyze_news.analyze_payload(payload(1), "", fetch=fake_deepseek(), delay=0)

    def test_missing_row_fails_closed(self):
        with self.assertRaisesRegex(analyze_news.AnalysisError, "漏了条目"):
            analyze_news.analyze_payload(payload(2), "key",
                                         fetch=fake_deepseek(omit_id=1), delay=0,
                                         log=lambda *_: None, now=NOW)

    def test_wrong_shape_fails_required_logic_labels(self):
        def response(_key, _request, _timeout):
            content = json.dumps({"items": [{"id": 0, "analysis": "只有一句普通总结"}]})
            return {"choices": [{"message": {"content": content}}]}
        with self.assertRaisesRegex(analyze_news.AnalysisError, "缺少线索链标签"):
            analyze_news.analyze_payload(payload(1), "key", fetch=response,
                                         delay=0, log=lambda *_: None)

    def test_output_budget_scales_with_the_batch(self):
        """写死 max_tokens 会让「某批写得长一点」整批被截断，进而把 feed 停更一天。"""
        one = analyze_news._request_payload([{"id": 0}], "deepseek-chat")
        five = analyze_news._request_payload([{"id": index} for index in range(5)],
                                             "deepseek-chat")
        self.assertGreater(five["max_tokens"], one["max_tokens"])
        self.assertGreaterEqual(one["max_tokens"], analyze_news.TOKENS_PER_ITEM)
        self.assertLessEqual(five["max_tokens"], analyze_news.MAX_TOKENS)

    def test_truncated_batch_is_split_instead_of_failing_the_run(self):
        """finish_reason=length 的那批拆小重试：坏掉的只该是一条，不是整份 feed。"""
        calls = []

        def fetch(_key, request, _timeout):
            items = json.loads(request["messages"][1]["content"].split("\n", 1)[1])["items"]
            calls.append(len(items))
            if len(items) > 1:      # 多条一批就「写得太多被截断」
                return {"choices": [{"finish_reason": "length",
                                     "message": {"content": "{\"items\": [{\"id\": 0, \"anal"}}]}
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"content": json.dumps(
                                     {"items": [{"id": items[0]["id"],
                                                 "analysis": clue(items[0]["title"])}]},
                                     ensure_ascii=False)}}]}

        data = payload(4)
        log = []
        stats = analyze_news.analyze_payload(data, "key", fetch=fetch, batch_size=4,
                                             delay=0, log=log.append, now=NOW)
        self.assertEqual(stats["analyzed"], 4)
        self.assertTrue(all(item.get("analysis") for item in data["items"]))
        # 先按 4 条试 → 失败后对半拆成 2+2 → 各再拆成单条；只有单条能成功。
        self.assertEqual(sorted(calls), [1, 1, 1, 1, 2, 2, 4])
        self.assertTrue(any("拆成" in line for line in log))

    def test_transport_error_is_not_split(self):
        """402/429/连不上这类错误拆小批次也没用，别白烧调用次数。"""
        from urllib.error import HTTPError
        calls = []

        def fetch(_key, request, _timeout):
            calls.append(request["max_tokens"])
            raise HTTPError(analyze_news.ENDPOINT, 402, "Payment Required", {}, None)

        with self.assertRaisesRegex(analyze_news.AnalysisError, "HTTP 402"):
            analyze_news.analyze_payload(payload(3), "key", fetch=fetch, batch_size=3,
                                         delay=0, log=lambda *_: None, now=NOW)
        self.assertEqual(len(calls), 1)

    def test_cache_round_trip(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.json"
            cache = {"abc": {"analysis": clue("测试新闻"), "model": "deepseek-chat",
                             "updated": NOW.isoformat()}}
            analyze_news.save_cache(cache, path, now=NOW)
            self.assertEqual(analyze_news.load_cache(path), cache)


if __name__ == "__main__":
    unittest.main()
