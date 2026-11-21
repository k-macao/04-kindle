import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import book_digest  # noqa: E402


class ZlibCliIntegrationTest(unittest.TestCase):
    """测试 heartleo/zlib 的搜索、选书、下载与无状态会话注入。"""

    def test_search_and_pick_best_epub(self):
        recorded = []

        def fake_runner(cmd, **kwargs):
            recorded.append(cmd)
            payload = {
                "page": 1,
                "total_pages": 3,
                "books": [
                    {"id": "1001:aaa", "name": "Thinking in Systems (Scan)",
                     "authors": ["Donella Meadows"], "year": "2008",
                     "extension": "pdf", "rating": "5.0/5.0"},
                    {"id": "1002:bbb", "name": "系统之美",
                     "authors": ["德内拉·梅多斯"], "year": "2012",
                     "extension": "epub", "rating": "4.8/5.0", "size": "1.2 MB"},
                    {"id": "1003:ccc", "name": "系统之美（早期版）",
                     "authors": "", "year": "2010",
                     "extension": "epub", "rating": "4.9/5.0"},
                ],
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        books = book_digest.zlib_search("系统之美", ext="epub", lang="chinese",
                                        count=5, zlib_bin="zlib", runner=fake_runner)
        self.assertEqual(len(books), 3)
        self.assertIn("--json", recorded[0])
        self.assertIn("--lang", recorded[0])
        best = book_digest.pick_best_book(books)
        # 优先选 epub 且作者信息齐全的条目
        self.assertEqual(best["id"], "1002:bbb")
        self.assertEqual(best["authors"], "德内拉·梅多斯")

    def test_session_injection_writes_json_or_cleans_cookie_tempfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_path = book_digest.ensure_zlib_session(
                session_json='{"domain":"https://z-lib.gd","cookies":{"remix_userid":"1"}}',
                config_dir=tmp,
            )
            self.assertTrue(Path(session_path).is_file())
            saved = json.loads(Path(session_path).read_text(encoding="utf-8"))
            self.assertEqual(saved["domain"], "https://z-lib.gd")

        seen_cookie_files = []

        def fake_login_runner(cmd, **kwargs):
            idx = cmd.index("--cookies")
            cpath = Path(cmd[idx + 1])
            seen_cookie_files.append(cpath)
            self.assertTrue(cpath.is_file())
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        mode = book_digest.ensure_zlib_session(
            session_json="",
            cookies_text="# Netscape HTTP Cookie File\n.z-lib.gd\tTRUE\t/\tTRUE\t0\tremix_userid\t1\n",
            domain="https://z-lib.gd",
            runner=fake_login_runner,
        )
        self.assertEqual(mode, "cookies")
        self.assertEqual(len(seen_cookie_files), 1)
        # 临时 Cookie 文件用完即删，不能留在磁盘上
        self.assertFalse(seen_cookie_files[0].exists())


class EpubAndTxtParserTest(unittest.TestCase):
    """测试 EPUB 多级目录、单文件多锚点切章与 TXT 切章。"""

    def test_parse_sample_epub_skips_cover_and_keeps_chapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = book_digest.build_sample_epub(Path(tmp) / "sample.epub")
            parsed = book_digest.parse_epub(epub_path)
            self.assertEqual(parsed["meta"]["title"], "系统思维与市场周期")
            self.assertEqual(parsed["meta"]["authors"], "示例作者")
            self.assertEqual(len(parsed["toc"]), 3)
            self.assertEqual(len(parsed["chapters"]), 3)
            self.assertEqual(parsed["chapters"][0]["title"], "第一章 复杂系统的反馈回路")
            self.assertIn("正反馈", parsed["chapters"][0]["text"])

    def test_single_file_multi_anchor_epub_splits_by_fragment(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / "anchored.epub"
            opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>单文件多章测试</dc:title>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="body" href="all.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx"><itemref idref="body"/></spine>
</package>"""
            ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
    <navPoint id="p1" playOrder="1">
      <navLabel><text>第一部分 基础框架</text></navLabel>
      <content src="all.xhtml#sec1"/>
      <navPoint id="p2" playOrder="2">
        <navLabel><text>1.1 状态与流量</text></navLabel>
        <content src="all.xhtml#sec2"/>
      </navPoint>
    </navPoint>
  </navMap>
</ncx>"""
            body = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
  <h1 id="sec1">第一部分 基础框架</h1>
  <p>存量是系统记忆的载体，任何瞬时冲击都要通过流量逐步改变存量，这是理解滞后效应的起点。</p>
  <p>忽视存量缓冲往往会导致政策过度反应，把周期波动放大成结构性失衡。</p>
  <h2 id="sec2">1.1 状态与流量</h2>
  <p>流入量大于流出量时存量上升，反之下降；识别瓶颈的关键在于找到调节流量的阀门变量。</p>
  <p>当信息延迟超过调节周期的一半时，系统必然出现持续振荡。</p>
</body></html>"""
            with zipfile.ZipFile(epub_path, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip")
                zf.writestr("META-INF/container.xml", book_digest.CONTAINER_XML)
                zf.writestr("OEBPS/content.opf", opf)
                zf.writestr("OEBPS/toc.ncx", ncx)
                zf.writestr("OEBPS/all.xhtml", body)

            parsed = book_digest.parse_epub(epub_path)
            self.assertEqual([t["depth"] for t in parsed["toc"]], [1, 2])
            self.assertEqual(len(parsed["chapters"]), 2)
            self.assertIn("存量是系统记忆", parsed["chapters"][0]["text"])
            self.assertNotIn("流入量大于流出量", parsed["chapters"][0]["text"])
            self.assertIn("流入量大于流出量", parsed["chapters"][1]["text"])

    def test_parse_txt_by_chapter_headings(self):
        with tempfile.TemporaryDirectory() as tmp:
            txt_path = Path(tmp) / "sample.txt"
            txt_path.write_text(
                "第一章 宏观流动性\n"
                + "信用扩张周期决定了资产估值中枢的抬升与收敛，央行资产负债表变化只是起点而非终点。" * 3
                + "\n\n第二章 产业资本周期\n"
                + "供给端资本开支的滞后性是周期行业利润率大幅波动的根本原因，出清往往伴随龙头的逆势整合。" * 3,
                encoding="utf-8",
            )
            parsed = book_digest.parse_txt(txt_path)
            self.assertEqual(len(parsed["chapters"]), 2)
            self.assertEqual(parsed["chapters"][0]["title"], "第一章 宏观流动性")
            self.assertEqual(parsed["chapters"][1]["title"], "第二章 产业资本周期")


class AiSummarizeAndBuildTest(unittest.TestCase):
    """测试 AI 总纲 + 分章精要（含截断对半拆批重试、402 报错与全流程端到端构建）。"""

    def test_ai_batch_splits_on_truncation_and_renders_all_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = book_digest.build_sample_epub(Path(tmp) / "book.epub")
            calls = []

            def fake_fetch(api_key, payload, timeout):
                calls.append(payload)
                user_text = payload["messages"][1]["content"]
                if "请提炼这本书的全书总纲" in user_text:
                    return {
                        "choices": [{
                            "finish_reason": "stop",
                            "message": {"content": json.dumps({
                                "core_thesis": "系统结构决定行为模式，慢变量与尾部生存优先于短期预测。",
                                "structure_logic": "从反馈回路出发，递进到信号衰减规律，最后落脚于风险预算。",
                                "key_takeaways": [
                                    "识别正负反馈切换点比预测价格更关键",
                                    "慢变量的容错率高于拥挤的快变量",
                                    "尾部危机中资产相关性会趋近于1",
                                ],
                                "reading_guide": "先读第一章建立回路框架，再精读第三章的仓位约束。",
                            }, ensure_ascii=False)},
                        }]
                    }
                # 章节总结：如果一次送来 >=3 章，模拟 finish_reason=length 触发对半拆批重试
                marker = "请逐章提炼以下章节：\n"
                batch_data = json.loads(user_text.split(marker, 1)[1])
                ch_rows = batch_data["chapters"]
                if len(ch_rows) >= 3:
                    return {
                        "choices": [{
                            "finish_reason": "length",
                            "message": {"content": '{"chapters":['},
                        }]
                    }
                return {
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({
                            "chapters": [
                                {
                                    "index": row["index"],
                                    "thesis": f"第{row['index']}章核心观点：{row['title']}",
                                    "evidence": f"书中论据（字数 {row['chars']}）：基于反馈与风险约束推导。",
                                    "summary": f"第{row['index']}章一句话结论。",
                                }
                                for row in ch_rows
                            ]
                        }, ensure_ascii=False)},
                    }]
                }

            def fake_runner(cmd, **kwargs):
                if cmd[1] == "search":
                    return subprocess.CompletedProcess(
                        cmd, 0,
                        stdout=json.dumps({"books": [
                            {"id": "888:abc", "name": "系统思维与市场周期",
                             "authors": ["示例作者"], "extension": "epub", "rating": "5.0"}
                        ]}),
                        stderr="",
                    )
                if cmd[1] == "download":
                    dl_dir = Path(cmd[cmd.index("--dir") + 1])
                    target = dl_dir / "downloaded.epub"
                    target.write_bytes(Path(epub_path).read_bytes())
                    return subprocess.CompletedProcess(
                        cmd, 0,
                        stdout=json.dumps({"id": "888:abc", "name": "系统思维与市场周期",
                                           "path": str(target), "size": target.stat().st_size}),
                        stderr="",
                    )
                raise AssertionError(f"Unexpected command: {cmd}")

            out_dir = Path(tmp) / "site"
            data = book_digest.build(
                query="系统思维与市场周期",
                out_dir=out_dir,
                api_key="test-key",
                fetch=fake_fetch,
                runner=fake_runner,
            )
            self.assertTrue(data["ai_used"])
            self.assertEqual(data["counts"]["chapters"], 3)
            self.assertEqual(book_digest.check(out_dir), [])
            # 1 次总纲 + 1 次3章被截断 + 拆成(1章 + 2章) = 4 次调用
            self.assertEqual(len(calls), 4)
            txt = (out_dir / "book.txt").read_text(encoding="utf-8")
            self.assertIn("系统结构决定行为模式", txt)
            self.assertIn("第1章核心观点", txt)
            self.assertIn("第3章一句话结论", txt)

    def test_http_402_raises_clear_balance_message(self):
        parsed = {
            "meta": {"title": "测试书", "authors": "作者"},
            "toc": [{"title": "第一章", "depth": 1}],
            "chapters": [{"index": 1, "title": "第一章", "depth": 1, "chars": 200,
                          "text": "测试正文" * 30, "excerpt": "测试正文" * 30}],
            "total_chapters": 1,
            "total_chars": 120,
        }

        def fetch_402(api_key, payload, timeout):
            raise HTTPError(
                book_digest.ENDPOINT, 402, "Payment Required", hdrs=None,
                fp=io.BytesIO(b'{"error":{"message":"Insufficient Balance"}}'),
            )

        with self.assertRaises(book_digest.BookDigestError) as ctx:
            book_digest.summarize_book(parsed, api_key="k", fetch=fetch_402)
        self.assertIn("余额不足", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
