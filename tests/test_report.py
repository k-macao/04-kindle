import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kindle_report as k


class ReportTest(unittest.TestCase):
    def test_main_only_preserves_order_and_escapes_script(self):
        src = '''<header>外部</header><main><h2>标题</h2><p>第一<strong>行</strong></p>
        <table><tr><th>市场</th><th>价格</th></tr><tr><td>恒指</td><td>123</td></tr></table>
        <div class="push-wrap">不显示</div><script>secret()</script><p>&lt;/script&gt;第二行</p></main>'''
        out = k.render(src, '2026-01-01T00:00:00+00:00')
        self.assertNotIn('secret()', out)
        self.assertNotIn('不显示', out)
        self.assertNotIn('外部', out)
        self.assertIn('\\u003c/script>', out)
        self.assertIn('2026-01-01T00:00:00+00:00', out)
        parser = k.ReportParser(); parser.feed(src)
        text = parser.text()
        self.assertLess(text.index('标题'), text.index('第一行'))
        self.assertLess(text.index('市场'), text.index('恒指'))
        self.assertIn('第二行', text)

    def test_template_rejected(self):
        with self.assertRaises(ValueError):
            k.render('<main>日期 {{TS_FULL}}</main>')
        with self.assertRaises(ValueError):
            k.render('<body>没有正文</body>')

    def test_failed_generation_does_not_replace_previous_file(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d)/'input.html', Path(d)/'output.html'
            src.write_text('<main>{{BAD}}</main>')
            out.write_text('old')
            with patch('sys.argv', ['kindle_report.py', str(src), '-o', str(out)]):
                self.assertEqual(k.main(), 1)
            self.assertEqual(out.read_text(), 'old')

    def test_text_cli_writes_report_not_html(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d)/'report.html', Path(d)/'kindle.txt'
            src.write_text('<main><h2>行情</h2><p>恒指 123</p></main>', encoding='utf-8')
            with patch('sys.argv', ['kindle_report.py', str(src), '--format', 'text', '-o', str(out)]):
                self.assertEqual(k.main(), 0)
            result = out.read_text(encoding='utf-8')
            self.assertIn('行情', result)
            self.assertIn('恒指 123', result)
            self.assertIn('数据时间以正文为准', result)
            self.assertNotIn('<main>', result)

    def test_self_closing_does_not_truncate(self):
        p = k.ReportParser(); p.feed('<main>甲<br/>乙<img src="x"/>丙</main>')
        self.assertIn('乙丙', p.text())

if __name__ == '__main__':
    unittest.main()
