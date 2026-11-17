#!/usr/bin/env python3
"""把 stocks_data.json 排成三份产物：清单、Kindle 纯文本、网页面板。

    python tools/build_stocks.py --data stocks_data.json --out site
    python tools/build_stocks.py --demo --out site   # 没 Key 时先看版式

三份产物（都在 ``site/`` 下，跟着仓库走）：

- ``stocks.json`` —— 清单 + 数据二合一：``generated`` 是 Kindle 插件的签名
  （变了才重下正文），``doc`` 指向 ``stocks.txt``，``data_time`` 是快照的
  数据时间（不是生成时间），``data_hash`` 供工作流判断「行情没变就不提交」；
- ``stocks.txt`` —— Kindle 纯文本面板（电子墨水友好：白底黑字、▲▼ + 正负号，
  不靠颜色传信息）；
- ``stocks.html`` —— 浏览器数据面板：每 60 秒拉一次 ``stocks.json``，
  ``generated`` 变了就重绘；拉不到就保留上一次的数据并标 OFFLINE。

抓取只负责取回并归一化（``tools/fetch_stocks.py``），这里只负责判断与输出：
数据条数太少、清单解析不过都不写文件，不覆盖上一份。
"""

import argparse
import hashlib
import html as html_mod
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TITLE = "同花顺行情面板"
DISCLAIMER = "仅供信息展示，不构成投资建议"
BEIJING = timezone.utc  # 北京时间 = UTC+8 固定偏移，不用 tzdata
POLL_SECONDS = 60


def beijing_text(epoch_ms, fmt="%Y-%m-%d %H:%M:%S"):
    """毫秒时间戳 → 北京时间文字；None 时返回空串。"""
    if not isinstance(epoch_ms, (int, float)):
        return ""
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    from datetime import timedelta
    return (moment + timedelta(hours=8)).strftime(fmt)


def now_beijing_text():
    from datetime import timedelta
    moment = datetime.now(timezone.utc) + timedelta(hours=8)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def fmt_price(value):
    return "--" if value is None else f"{value:,.2f}"


def fmt_ratio(value):
    if value is None:
        return "--"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def fmt_change(value):
    if value is None:
        return ""
    arrow = "▲" if value > 0 else ("▼" if value < 0 else "·")
    return f"{arrow}{value:+,.2f}"


def fmt_volume(value):
    """成交量（股）→ 手：3098875 股 = 309.89 万手。"""
    if value is None:
        return "--"
    hands = value / 100.0
    if hands >= 10000:
        return f"{hands / 10000:.2f}万手"
    return f"{hands:,.2f}手"


def fmt_turnover(value):
    """成交额（元）→ 亿：3937375200 元 = 39.37亿。"""
    if value is None:
        return "--"
    yi = value / 1e8
    if abs(yi) >= 10000:
        return f"{yi / 10000:.2f}万亿"
    return f"{yi:.2f}亿"


def fmt_seal(value):
    if value is None:
        return ""
    return f"封单{fmt_turnover(value)}"


def data_hash(payload):
    """行情指纹：价格/涨跌幅/条数变了才变，供工作流跳过无变化的提交。"""
    core = {
        "indices": [(r.get("thscode"), r.get("last_price"),
                     r.get("price_change_ratio_pct")) for r in payload.get("indices") or []],
        "stocks": [(r.get("thscode"), r.get("last_price"),
                    r.get("price_change_ratio_pct")) for r in payload.get("stocks") or []],
        "limit_up": [(r.get("thscode"), r.get("last_price")) for r in payload.get("limit_up") or []],
        "data_timestamp": payload.get("data_timestamp"),
    }
    raw = json.dumps(core, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def manifest(payload, generated):
    data_ms = payload.get("data_timestamp")
    data_time = None
    if isinstance(data_ms, (int, float)):
        # 插件的 isoSeconds 只认 "...Z" 结尾（和 epub.json 的 data_time 同形）。
        data_time = datetime.fromtimestamp(
            data_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "generated": generated,
        "doc": "stocks.txt",
        "title": TITLE,
        "data_time": data_time,
        "data_time_kind": "exact" if data_time else None,
        "data_hash": data_hash(payload),
        "demo": bool(payload.get("demo")),
        "counts": {
            "indices": len(payload.get("indices") or []),
            "stocks": len(payload.get("stocks") or []),
            "limit_up": len(payload.get("limit_up") or []),
        },
        "indices": payload.get("indices") or [],
        "stocks": payload.get("stocks") or [],
        "limit_up": payload.get("limit_up") or [],
        "errors": payload.get("errors") or [],
    }


def render_txt(payload, generated):
    lines = [TITLE]
    demo = "【演示数据】" if payload.get("demo") else ""
    data_text = beijing_text(payload.get("data_timestamp")) or "无"
    lines.append(f"生成 {generated} 北京时间 · 数据 {data_text}{demo}")
    lines.append(f"每5分钟刷新 · 每60秒检查 · {DISCLAIMER}")
    lines.append("")

    indices = payload.get("indices") or []
    lines.append(f"【指数】{len(indices)}")
    for row in indices:
        name = row.get("name") or row.get("thscode") or ""
        lines.append(f"{name} {fmt_price(row.get('last_price'))} "
                     f"{fmt_ratio(row.get('price_change_ratio_pct'))} "
                     f"{fmt_change(row.get('price_change'))}".rstrip())
    lines.append("")

    stocks = payload.get("stocks") or []
    lines.append(f"【自选股】{len(stocks)} · 按涨跌幅排序")
    for row in stocks:
        name = row.get("name") or row.get("thscode") or ""
        lines.append(f"{name} {fmt_price(row.get('last_price'))} "
                     f"{fmt_ratio(row.get('price_change_ratio_pct'))} "
                     f"{fmt_change(row.get('price_change'))}".rstrip())
        lines.append(f"  开{fmt_price(row.get('open_price'))} "
                     f"高{fmt_price(row.get('high_price'))} "
                     f"低{fmt_price(row.get('low_price'))} "
                     f"成交{fmt_volume(row.get('volume'))} "
                     f"成交额{fmt_turnover(row.get('turnover'))}")
    lines.append("")

    limit_up = payload.get("limit_up") or []
    if limit_up:
        lines.append(f"【涨停池】{len(limit_up)} · 按封板时间")
        for row in limit_up:
            name = row.get("name") or row.get("thscode") or ""
            extra = " ".join(part for part in (
                row.get("limit_up_time") or "",
                row.get("limit_up_reason") or "",
                row.get("continue_day_text") or "",
                fmt_seal(row.get("seal_money"))) if part)
            lines.append(f"{name} {fmt_price(row.get('last_price'))} "
                         f"{fmt_ratio(row.get('price_change_ratio_pct'))} {extra}".rstrip())
        lines.append("")

    for error in payload.get("errors") or []:
        lines.append(f"注：{error}")
    lines.append(DISCLAIMER)
    return "\n".join(lines) + "\n"


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#fff;color:#000;
font-family:"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:16px}
header{border-bottom:3px solid #000;padding-bottom:8px}
h1{font-size:26px;margin:0 0 4px}
.meta{font-size:14px;color:#333}
.badge{display:inline-block;border:1px solid #000;padding:0 6px;font-size:13px;margin-left:6px}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:15px}
th,td{border:1px solid #999;padding:5px 7px;text-align:right;white-space:nowrap}
th{background:#eee}td:first-child,th:first-child{text-align:left}
.up{font-weight:bold}.down{font-weight:bold}
section h2{font-size:19px;border-bottom:2px solid #000;padding-bottom:4px;margin:20px 0 4px}
footer{border-top:1px solid #000;margin-top:16px;padding-top:8px;font-size:13px;color:#333}
</style>
</head>
<body>
<div class="wrap">
<header>
<h1>__TITLE__<span class="badge" id="state">…</span></h1>
<div class="meta" id="meta">加载中…</div>
</header>
<main id="main"></main>
<footer>__DISCLAIMER__ · 数据来源：同花顺金融数据服务（hithink-finance）·
面板每 __POLL__ 秒拉一次 stocks.json，generated 变了才重绘。</footer>
</div>
<noscript><p>需要 JavaScript 才能每分钟刷新；Kindle 请用 stocks.txt 纯文本版。</p></noscript>
<script>
'use strict';
var POLL = __POLL__;
var lastGenerated = null;
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function num(v,d){return (v==null||isNaN(v))?'--':Number(v).toLocaleString('zh-CN',
{minimumFractionDigits:d==null?2:d,maximumFractionDigits:d==null?2:d});}
function ratio(v){if(v==null||isNaN(v))return '--';
return (v>0?'+':'')+Number(v).toFixed(2)+'%';}
function change(v){if(v==null||isNaN(v))return '';
var a=v>0?'▲':(v<0?'▼':'·');return a+(v>0?'+':'')+Number(v).toFixed(2);}
function cls(v){return v>0?'up':(v<0?'down':'');}
function vol(v){if(v==null)return '--';var h=v/100;
return h>=10000?(h/10000).toFixed(2)+'万手':h.toLocaleString('zh-CN',
{minimumFractionDigits:2,maximumFractionDigits:2})+'手';}
function money(v){if(v==null)return '--';var y=v/1e8;
return Math.abs(y)>=10000?(y/10000).toFixed(2)+'万亿':y.toFixed(2)+'亿';}
function quoteRows(rows){
var h='<table><tr><th>名称</th><th>最新</th><th>涨跌幅</th><th>涨跌</th>'+
'<th>开盘</th><th>最高</th><th>最低</th><th>成交量</th><th>成交额</th></tr>';
rows.forEach(function(r){h+='<tr><td>'+esc(r.name||r.thscode)+'</td>'+
'<td>'+num(r.last_price)+'</td>'+
'<td class="'+cls(r.price_change_ratio_pct)+'">'+ratio(r.price_change_ratio_pct)+'</td>'+
'<td class="'+cls(r.price_change)+'">'+change(r.price_change)+'</td>'+
'<td>'+num(r.open_price)+'</td><td>'+num(r.high_price)+'</td>'+
'<td>'+num(r.low_price)+'</td><td>'+vol(r.volume)+'</td>'+
'<td>'+money(r.turnover)+'</td></tr>';});
return h+'</table>';}
function limitRows(rows){
var h='<table><tr><th>名称</th><th>最新</th><th>涨跌幅</th><th>封板</th>'+
'<th>原因</th><th>连板</th><th>封单额</th></tr>';
rows.forEach(function(r){h+='<tr><td>'+esc(r.name||r.thscode)+'</td>'+
'<td>'+num(r.last_price)+'</td>'+
'<td class="'+cls(r.price_change_ratio_pct)+'">'+ratio(r.price_change_ratio_pct)+'</td>'+
'<td>'+esc(r.limit_up_time||'')+'</td><td>'+esc(r.limit_up_reason||'')+'</td>'+
'<td>'+esc(r.continue_day_text||'')+'</td><td>'+money(r.seal_money)+'</td></tr>';});
return h+'</table>';}
function render(d){
var h='';
if(d.indices&&d.indices.length)h+='<section><h2>指数（'+d.indices.length+'）</h2>'+
quoteRows(d.indices)+'</section>';
if(d.stocks&&d.stocks.length)h+='<section><h2>自选股（'+d.stocks.length+
'）· 按涨跌幅排序</h2>'+quoteRows(d.stocks)+'</section>';
if(d.limit_up&&d.limit_up.length)h+='<section><h2>涨停池（'+d.limit_up.length+
'）· 按封板时间</h2>'+limitRows(d.limit_up)+'</section>';
(d.errors||[]).forEach(function(e){h+='<p>注：'+esc(e)+'</p>';});
document.getElementById('main').innerHTML=h||'<p>暂无数据</p>';
var demo=d.demo?'【演示数据】':'';
document.getElementById('meta').textContent='生成 '+d.generated+'（北京时间） · 数据时间 '+
(d.data_time||'无')+' '+demo+' · 每'+POLL+'秒检查一次';
document.getElementById('state').textContent='ONLINE';
}
function load(){
fetch('stocks.json?t='+Date.now(),{cache:'no-store'}).then(function(r){
if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(d){
if(d.generated===lastGenerated)return;lastGenerated=d.generated;render(d);
}).catch(function(){document.getElementById('state').textContent='OFFLINE / CACHED';});}
load();setInterval(load,POLL*1000);
</script>
</body>
</html>
"""


def render_html():
    return (HTML_TEMPLATE.replace("__TITLE__", TITLE)
            .replace("__DISCLAIMER__", DISCLAIMER)
            .replace("__POLL__", str(POLL_SECONDS)))


def check(out_dir):
    """产物自检：清单可解析、字段齐、文本非空、面板带 60 秒轮询。"""
    out = Path(out_dir)
    problems = []
    manifest_path = out / "stocks.json"
    if not manifest_path.is_file():
        return ["缺少 stocks.json"]
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"stocks.json 解析失败：{exc}"]
    for key in ("generated", "doc", "title", "data_hash", "counts"):
        if not data.get(key):
            problems.append(f"stocks.json 缺少 {key}")
    if data.get("doc") != "stocks.txt":
        problems.append("stocks.json 的 doc 不是 stocks.txt")
    counts = data.get("counts") or {}
    if not counts.get("indices") and not counts.get("stocks"):
        problems.append("stocks.json 里指数与股票都是空的")
    txt_path = out / "stocks.txt"
    if not txt_path.is_file() or txt_path.stat().st_size < 100:
        problems.append("stocks.txt 缺失或过小")
    elif TITLE not in txt_path.read_text(encoding="utf-8"):
        problems.append("stocks.txt 里没有面板标题")
    html_path = out / "stocks.html"
    if not html_path.is_file():
        problems.append("缺少 stocks.html")
    else:
        page = html_path.read_text(encoding="utf-8")
        if "stocks.json" not in page or f"var POLL = {POLL_SECONDS}" not in page:
            problems.append("stocks.html 没有每 60 秒拉 stocks.json 的轮询")
    return problems


# 演示数据：取自同花顺官方文档的响应示例，只为没 Key 时先看版式。
# 真实数据由工作流抓取后覆盖；面板与清单都标了「演示数据」。
DEMO_PAYLOAD = {
    "demo": True,
    "data_timestamp": 1784275991000,
    "indices": [
        {"thscode": "000001.SH", "ticker": "000001", "name": "上证指数",
         "last_price": 3665.94, "price_change": 16.79,
         "price_change_ratio_pct": 0.46, "open_price": 3650.12,
         "high_price": 3670.0, "low_price": 3648.5, "prev_price": 3649.15,
         "volume": 51234567800.0, "turnover": 1567890000000.0},
        {"thscode": "399006.SZ", "ticker": "399006", "name": "创业板指",
         "last_price": 2478.94, "price_change": 13.86,
         "price_change_ratio_pct": 0.56, "open_price": 2468.0,
         "high_price": 2485.0, "low_price": 2465.0, "prev_price": 2465.08,
         "volume": 23456789000.0, "turnover": 987650000000.0},
    ],
    "stocks": [
        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
         "last_price": 1277.8, "price_change": 21.8,
         "price_change_ratio_pct": 1.735669, "open_price": 1252.08,
         "high_price": 1282.0, "low_price": 1250.21, "prev_price": 1256.0,
         "volume": 3098875.0, "turnover": 3937375200.0},
        {"thscode": "000001.SZ", "ticker": "000001", "name": "平安银行",
         "last_price": 11.23, "price_change": -0.12,
         "price_change_ratio_pct": -1.057, "open_price": 11.35,
         "high_price": 11.4, "low_price": 11.2, "prev_price": 11.35,
         "volume": 45678900.0, "turnover": 512340000.0},
    ],
    "limit_up": [
        {"thscode": "603986.SH", "ticker": "603986", "name": "兆易创新",
         "last_price": 118.23, "price_change_ratio_pct": 10.0008,
         "limit_up_time": "09:34", "limit_up_reason": "存储芯片",
         "continue_day_text": "2连板", "seal_money": 123456789.12},
    ],
    "errors": [],
}


def build(data_path=None, out_dir="site", demo=False):
    """读 stocks_data.json（或演示数据）→ 写三份产物，返回清单。"""
    if demo:
        payload = dict(DEMO_PAYLOAD)
    else:
        if not data_path:
            raise ValueError("缺少 --data（或用 --demo 先看版式）")
        payload = json.loads(Path(data_path).read_text(encoding="utf-8"))
    total = len(payload.get("indices") or []) + len(payload.get("stocks") or [])
    if total == 0:
        raise ValueError("行情一条都没有，不生成空面板")
    # 清单里的 generated 与 epub.json 同形（ISO-8601 UTC），只做签名用；
    # 文本面板上显示北京时间文字。
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = manifest(payload, generated)
    (out / "stocks.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "stocks.txt").write_text(
        render_txt(payload, now_beijing_text()), encoding="utf-8")
    (out / "stocks.html").write_text(render_html(), encoding="utf-8")
    problems = check(out)
    if problems:
        raise ValueError("产物自检不过：" + "；".join(problems))
    print(f"已生成 {out}/stocks.json、stocks.txt、stocks.html"
          f"（指数 {data['counts']['indices']} / 股票 {data['counts']['stocks']}"
          f" / 涨停 {data['counts']['limit_up']}，hash {data['data_hash']}）")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description="stocks_data.json → 面板三件套")
    parser.add_argument("--data", default="stocks_data.json",
                        help="fetch_stocks 的中间产物")
    parser.add_argument("--out", default="site", help="产物目录（默认 site）")
    parser.add_argument("--demo", action="store_true",
                        help="用演示数据排版（没 Key 时先看版式）")
    args = parser.parse_args(argv)
    try:
        build(args.data, args.out, demo=args.demo)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"排版失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
