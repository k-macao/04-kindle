"""英文条目补中文：判断、接口回退、缓存、写回与 CLI。

翻译是唯一另需联网的环节，所以这里全程注入假 fetch——测的是「哪家失败换哪家、
缓存命中不发请求、接口回的都是英文就不算翻成」这些规矩，不是网络。
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import translate_news  # noqa: E402

NOW = datetime(2026, 11, 8, 0, 30, tzinfo=timezone.utc)


def google_payload(zh="美联储降息25个基点"):
    # 真实接口是「分段数组」：[[[译文, 原文, None, None, 10], …], None, "en"]
    return json.dumps([[[zh, "source text", None, None, 10]], None, "en"]).encode()


def lingva_payload(zh="灵瓦译文"):
    return json.dumps({"translation": zh}).encode()


def mymemory_payload(zh="我的记忆译文"):
    return json.dumps({"responseStatus": 200,
                       "responseData": {"translatedText": zh}}).encode()


def fake_fetch(mapping):
    calls = []

    def fetch(url, timeout=12):
        calls.append(url)
        for fragment, payload in mapping.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise OSError("HTTP Error 500: boom")
    fetch.calls = calls
    return fetch


def english_item(**kwargs):
    item = {"title": "Fed cuts rates by 25 basis points",
            "link": "https://example.com/fed",
            "published": "2026-11-07T20:00:00Z",
            "summary": "The Federal Reserve lowered its benchmark rate for the second time.",
            "source": "BLOOMBERG",
            "group": "global",
            "lang": "en"}
    item.update(kwargs)
    return item


class LanguageTest(unittest.TestCase):
    def test_looks_english(self):
        self.assertTrue(translate_news.looks_english("Fed cuts rates by 25bp"))
        self.assertTrue(translate_news.looks_english("Aeon: what is consciousness?"))

    def test_chinese_title_is_not_english(self):
        self.assertFalse(translate_news.looks_english("央行降准 0.5 个百分点"))
        self.assertFalse(translate_news.looks_english("GPT-5 发布，OpenAI 再融资"))

    def test_short_token_is_not_english(self):
        # 三个字母的代码/简称不值得为它发一次请求。
        self.assertFalse(translate_news.looks_english("FOMC"))
        self.assertFalse(translate_news.looks_english(""))

    def test_has_cjk(self):
        self.assertTrue(translate_news.has_cjk("中文"))
        self.assertFalse(translate_news.has_cjk("plain english"))

    def test_needs_translation_uses_source_language_and_title(self):
        self.assertTrue(translate_news.needs_translation(english_item()))
        self.assertTrue(translate_news.needs_translation(
            {"title": "Why we dream", "lang": "zh"}))          # 标错语言的标题也能救
        self.assertFalse(translate_news.needs_translation(
            {"title": "恒指收涨 1.2%", "lang": "zh"}))
        self.assertFalse(translate_news.needs_translation(
            {"title": "央行降准，Fed 同步缩表", "lang": "zh"}))


class ProviderTest(unittest.TestCase):
    def test_google_first(self):
        fetch = fake_fetch({"translate.googleapis.com": google_payload()})
        zh, by = translate_news.translate("Fed cuts rates by 25bp", fetch=fetch)
        self.assertEqual(by, "google")
        self.assertIn("降息", zh)
        self.assertEqual(len(fetch.calls), 1)

    def test_falls_back_to_lingva_then_mymemory(self):
        fetch = fake_fetch({"translate.googleapis.com": OSError("HTTP Error 403: Forbidden"),
                            "lingva": lingva_payload()})
        zh, by = translate_news.translate("Something about markets", fetch=fetch)
        self.assertEqual((zh, by), ("灵瓦译文", "lingva"))

        fetch = fake_fetch({"translate.googleapis.com": OSError("403"),
                            "lingva": OSError("timeout"),
                            "mymemory": mymemory_payload()})
        zh, by = translate_news.translate("Something about markets", fetch=fetch)
        self.assertEqual((zh, by), ("我的记忆译文", "mymemory"))

    def test_english_reply_is_not_a_translation(self):
        # 接口原样回英文 / 回「不是中文」的乱码，都算失败，继续换下一家。
        fetch = fake_fetch({"translate.googleapis.com": google_payload(zh="Fed cuts rates"),
                            "lingva": lingva_payload(zh="中文来了")})
        zh, by = translate_news.translate("Fed cuts rates by 25bp", fetch=fetch)
        self.assertEqual((zh, by), ("中文来了", "lingva"))

    def test_everything_failing_returns_none(self):
        fetch = fake_fetch({"translate.googleapis.com": OSError("403"),
                            "lingva": OSError("403"),
                            "mymemory": OSError("403")})
        zh, why = translate_news.translate("Fed cuts rates by 25bp", fetch=fetch)
        self.assertIsNone(zh)
        self.assertIn("mymemory", why)

    def test_mymemory_quota_warning_is_rejected(self):
        warn = json.dumps({"responseStatus": 200,
                           "responseData": {"translatedText":
                                            "MYMEMORY WARNING: YOU USED ALL AVAILABLE"}}).encode()
        fetch = fake_fetch({"mymemory": warn})
        zh, _why = translate_news.translate(
            "Fed cuts rates", fetch=fetch,
            providers=(("mymemory", translate_news._mymemory_url,
                        translate_news._mymemory_parse),))
        self.assertIsNone(zh)

    def test_tidy_strips_quotes_and_full_stop(self):
        self.assertEqual(translate_news.tidy("  “降息。” \n"), "降息")
        self.assertEqual(translate_news.tidy("降息。"), "降息")
        self.assertEqual(translate_news.tidy("降息 25 个基点"), "降息 25 个基点")


class CacheTest(unittest.TestCase):
    def test_cache_hit_sends_no_request(self):
        cache = {translate_news.cache_key("Fed cuts rates"): {"zh": "降息", "by": "google",
                                                              "at": "2026-11-07T00:00:00+00:00"}}
        fetch = fake_fetch({})
        zh, by = translate_news.translate_text("Fed cuts rates", cache, fetch=fetch)
        self.assertEqual((zh, by), ("降息", "cache"))
        self.assertEqual(fetch.calls, [])

    def test_new_translation_is_cached(self):
        cache = {}
        fetch = fake_fetch({"translate.googleapis.com": google_payload()})
        zh, by = translate_news.translate_text("Fed cuts rates by 25bp", cache, fetch=fetch)
        self.assertEqual(by, "google")
        self.assertTrue(zh)
        stored = cache[translate_news.cache_key("Fed cuts rates by 25bp")]
        self.assertEqual(stored["by"], "google")
        self.assertTrue(stored["at"])

    def test_expired_entries_are_dropped(self):
        old = (NOW - timedelta(days=translate_news.CACHE_DAYS + 5)).isoformat()
        fresh = (NOW - timedelta(days=1)).isoformat()
        folder = Path(tempfile.mkdtemp())
        path = folder / "cache.json"
        path.write_text(json.dumps({"entries": {
            "old": {"zh": "旧", "by": "google", "at": old},
            "new": {"zh": "新", "by": "google", "at": fresh},
            "broken": {"zh": "没有时间"},
        }}), encoding="utf-8")
        cache = translate_news.load_cache(path, now=NOW)
        self.assertEqual(sorted(cache), ["new"])

    def test_round_trip(self):
        folder = Path(tempfile.mkdtemp())
        path = folder / "sub" / "cache.json"
        entries = {"k": {"zh": "中文", "by": "lingva", "at": NOW.isoformat()}}
        translate_news.save_cache(path, entries)
        self.assertEqual(translate_news.load_cache(path, now=NOW), entries)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["count"], 1)


class PayloadTest(unittest.TestCase):
    def test_english_items_get_chinese(self):
        payload = {"items": [english_item(), {"title": "恒指收涨 1.2%，南向资金净买入 24 亿",
                                              "lang": "zh", "summary": "港股今日收涨"}]}
        fetch = fake_fetch({"translate.googleapis.com": google_payload()})
        stats = translate_news.translate_payload(payload, {}, fetch=fetch, log=None)
        first, chinese = payload["items"]
        self.assertTrue(first["title_zh"])
        self.assertTrue(first["summary_zh"])
        self.assertNotIn("title_zh", chinese)      # 中文条目一个请求都不发
        self.assertEqual(stats["english"], 1)
        self.assertEqual(stats["translated"], 1)
        self.assertEqual(stats["summary_translated"], 1)
        self.assertEqual(stats["failed"], 0)

    def test_english_summary_with_chinese_title_is_translated(self):
        payload = {"items": [{"title": "市场消息", "lang": "zh",
                              "summary": "Markets rallied after the announcement."}]}
        fetch = fake_fetch({"translate.googleapis.com": google_payload("市场上涨")})
        stats = translate_news.translate_payload(payload, {}, fetch=fetch, log=None)
        item = payload["items"][0]
        self.assertNotIn("title_zh", item)
        self.assertEqual(item["summary_zh"], "市场上涨")
        self.assertEqual(stats["summary_translated"], 1)
        self.assertEqual(len(fetch.calls), 1)

    def test_failed_translation_leaves_item_untouched(self):
        payload = {"items": [english_item()]}
        fetch = fake_fetch({"translate.googleapis.com": OSError("403"),
                            "lingva": OSError("403"),
                            "mymemory": OSError("403")})
        stats = translate_news.translate_payload(payload, {}, fetch=fetch, log=None)
        self.assertNotIn("title_zh", payload["items"][0])
        self.assertEqual(stats["failed"], 1)

    def test_max_items_caps_work(self):
        payload = {"items": [english_item(title=f"Fed cuts rates number {index}")
                             for index in range(5)]}
        fetch = fake_fetch({"translate.googleapis.com": google_payload()})
        stats = translate_news.translate_payload(payload, {}, fetch=fetch, max_items=2, log=None)
        self.assertEqual(stats["english"], 2)
        self.assertEqual(stats["pending"], 3)


class CliTest(unittest.TestCase):
    def run_cli(self, payload, extra=(), fetch=None):
        folder = Path(tempfile.mkdtemp())
        data = folder / "news_data.json"
        data.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        cache = folder / "site" / "news_zh_cache.json"
        argv = ["--data", str(data), "--cache", str(cache), "--delay", "0", *extra]
        code = translate_news.main(argv, fetch=fetch)
        return code, data, cache

    def test_writes_translations_back(self):
        fetch = fake_fetch({"translate.googleapis.com": google_payload()})
        code, data, cache = self.run_cli({"items": [english_item()]}, fetch=fetch)
        self.assertEqual(code, 0)
        written = json.loads(data.read_text(encoding="utf-8"))["items"][0]
        self.assertTrue(written["title_zh"])
        self.assertIn("entries", json.loads(cache.read_text(encoding="utf-8")))

    def test_dry_run_changes_nothing(self):
        fetch = fake_fetch({"translate.googleapis.com": google_payload()})
        code, data, _cache = self.run_cli({"items": [english_item()]},
                                          extra=["--dry-run"], fetch=fetch)
        self.assertEqual(code, 0)
        self.assertNotIn("title_zh", json.loads(data.read_text(encoding="utf-8"))["items"][0])

    def test_strict_exits_three_when_nothing_translated(self):
        fetch = fake_fetch({"translate.googleapis.com": OSError("403"),
                            "lingva": OSError("403"),
                            "mymemory": OSError("403")})
        code, _data, _cache = self.run_cli({"items": [english_item()]},
                                           extra=["--strict"], fetch=fetch)
        self.assertEqual(code, 3)

    def test_missing_data_file(self):
        code = translate_news.main(["--data", "/nonexistent/news.json"], fetch=fake_fetch({}))
        self.assertEqual(code, 2)
