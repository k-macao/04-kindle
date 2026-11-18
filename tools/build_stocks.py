#!/usr/bin/env python3
"""把 stocks_data.json 排成四份产物：清单、Kindle 纯文本、网页面板、EPUB 卡片版。

    python tools/build_stocks.py --data stocks_data.json --out site
    python tools/build_stocks.py --demo --out site   # 没 Key 时先看版式

四份产物（都在 ``site/`` 下，跟着仓库走）：

- ``stocks.json`` —— 清单 + 数据二合一：``generated`` 是 Kindle 插件的签名
  （变了才重下正文），``doc`` 指向 ``stocks.txt``，``epub`` 指向 ``stocks.epub``，
  ``data_time`` 是快照的数据时间（不是生成时间），``data_hash`` 供工作流判断
  「行情没变就不提交」；
- ``stocks.txt`` —— Kindle 纯文本面板（电子墨水友好：白底黑字、▲▼ + 正负号，
  不靠颜色传信息）；
- ``stocks.html`` —— 浏览器卡片仪表盘：指数卡 + 自选股卡（带近 20 日迷你走势图）
  + 涨停表，每 60 秒拉一次 ``stocks.json``，``generated`` 变了就重绘；
  拉不到就保留上一次的数据并标 OFFLINE；
- ``stocks.epub`` —— Kindle 字模式卡片版：同一套数据排成表格卡片，交给
  KOReader 阅读器显示（crengine 不认 flex/grid/SVG，所以迷你图只出现在网页面板）。

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
    """行情指纹：价格/涨跌幅/条数/历史收盘变了才变，供工作流跳过无变化的提交。"""
    history = payload.get("history") or {}
    core = {
        "indices": [(r.get("thscode"), r.get("last_price"),
                     r.get("price_change_ratio_pct")) for r in payload.get("indices") or []],
        "stocks": [(r.get("thscode"), r.get("last_price"),
                    r.get("price_change_ratio_pct")) for r in payload.get("stocks") or []],
        "limit_up": [(r.get("thscode"), r.get("last_price")) for r in payload.get("limit_up") or []],
        "history": {code: [pair[1] for pair in bars]
                    for code, bars in sorted(history.items())},
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
        "epub": "stocks.epub",
        "title": TITLE,
        # 数据时间两写：ISO 给插件显示，毫秒原值留着——这样拿 stocks.json
        # 本身当输入重排一次（--data site/stocks.json）也不会把数据时间弄丢。
        "data_timestamp": data_ms if isinstance(data_ms, (int, float)) else None,
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
        "history": payload.get("history") or {},
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
.wrap{max-width:1080px;margin:0 auto;padding:16px}
header{border-bottom:3px solid #000;padding-bottom:8px}
h1{font-size:26px;margin:0 0 4px}
.meta{font-size:14px;color:#333}
.badge{display:inline-block;border:1px solid #000;padding:0 6px;font-size:13px;margin-left:6px}
section h2{font-size:19px;border-bottom:2px solid #000;padding-bottom:4px;margin:22px 0 10px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.cards.index{grid-template-columns:repeat(auto-fill,minmax(170px,1fr))}
.card{border:2px solid #000;padding:0;background:#fff}
.card .head{display:flex;justify-content:space-between;align-items:baseline;
padding:8px 12px;border-bottom:1px solid #000;background:#f2f2f2}
.card .name{font-size:15px;font-weight:bold}
.card .code{font-size:12px;color:#555}
.card .body{padding:8px 12px}
.card .price{font-size:28px;font-weight:bold;line-height:1.1}
.card .ratio{font-size:16px;font-weight:bold;margin-bottom:6px}
.card table.kv{width:100%;border-collapse:collapse;font-size:12.5px}
.card table.kv td{padding:1px 0}
.card table.kv td:last-child{text-align:right}
.card .spark{margin-top:6px;border-top:1px dashed #999;padding-top:4px}
.card .spark svg{display:block;width:100%;height:36px}
.card .spark-label{font-size:11px;color:#555;display:flex;justify-content:space-between}
.up{font-weight:bold}.down{font-weight:bold}
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
<noscript><p>需要 JavaScript 才能每分钟刷新；Kindle 请用 stocks.txt 纯文本版或 stocks.epub 卡片版。</p></noscript>
<script>
'use strict';
var POLL = __POLL__;
var lastGenerated = null;
// 清单里的时间都是 ISO-8601 UTC（"...Z"），显示前换成北京时间（+8h）。
function bj(iso){
if(!iso)return '无';
var s=String(iso).replace('Z','');s=s.replace(' ','T');
var p=s.match(/^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2}):(\\d{2})$/);
if(!p)return String(iso);
var d=new Date(Date.UTC(+p[1],+p[2]-1,+p[3],+p[4],+p[5],+p[6])+8*3600*1000);
function z(n){return (n<10?'0':'')+n;}
return d.getUTCFullYear()+'-'+z(d.getUTCMonth()+1)+'-'+z(d.getUTCDate())+' '+
z(d.getUTCHours())+':'+z(d.getUTCMinutes())+':'+z(d.getUTCSeconds());
}
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
function spark(closes){
if(!closes||closes.length<2)return '';
var pts=closes.filter(function(v){return typeof v==='number';});
if(pts.length<2)return '';
var lo=Math.min.apply(null,pts),hi=Math.max.apply(null,pts),span=(hi-lo)||1;
var w=140,h=32,step=w/(pts.length-1),coords=[];
pts.forEach(function(v,i){coords.push((i*step).toFixed(1)+','+
(h-2-((v-lo)/span)*(h-4)).toFixed(1));});
return '<svg width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" '+
'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'+
'<polyline points="'+coords.join(' ')+'" fill="none" stroke="#000" '+
'stroke-width="1.5"/></svg>';}
function closesOf(d,code){
var bars=(d.history||{})[code]||[];
return bars.map(function(p){return p[1];});}
function rangeOf(closes){
var pts=closes.filter(function(v){return typeof v==='number';});
if(!pts.length)return null;
return {first:pts[0],last:pts[pts.length-1],
lo:Math.min.apply(null,pts),hi:Math.max.apply(null,pts)};}
function card(r,d,small){
var closes=closesOf(d,r.thscode);
var s=spark(closes),rg=rangeOf(closes);
var kv='';
if(!small){
kv='<table class="kv"><tr><td>今开</td><td>'+num(r.open_price)+'</td></tr>'+
'<tr><td>最高</td><td>'+num(r.high_price)+'</td></tr>'+
'<tr><td>最低</td><td>'+num(r.low_price)+'</td></tr>'+
'<tr><td>成交量</td><td>'+vol(r.volume)+'</td></tr>'+
'<tr><td>成交额</td><td>'+money(r.turnover)+'</td></tr></table>';
}
var sparkHtml='';
if(s){
sparkHtml='<div class="spark">'+s+'<div class="spark-label"><span>近'+
closes.length+'日 '+num(rg.first)+'→'+num(rg.last)+'</span>'+
'<span>低 '+num(rg.lo)+' 高 '+num(rg.hi)+'</span></div></div>';
}
return '<div class="card"><div class="head"><span class="name">'+
esc(r.name||r.thscode)+'</span><span class="code">'+esc(r.thscode||'')+
'</span></div><div class="body"><div class="price">'+num(r.last_price)+
'</div><div class="ratio '+cls(r.price_change_ratio_pct)+'">'+
ratio(r.price_change_ratio_pct)+' '+change(r.price_change)+'</div>'+
kv+sparkHtml+'</div></div>';}
function limitRows(rows){
var h='<table class="data"><tr><th>名称</th><th>最新</th><th>涨跌幅</th><th>封板</th>'+
'<th>原因</th><th>连板</th><th>封单额</th></tr>';
rows.forEach(function(r){h+='<tr><td>'+esc(r.name||r.thscode)+'</td>'+
'<td>'+num(r.last_price)+'</td>'+
'<td class="'+cls(r.price_change_ratio_pct)+'">'+ratio(r.price_change_ratio_pct)+'</td>'+
'<td>'+esc(r.limit_up_time||'')+'</td><td>'+esc(r.limit_up_reason||'')+'</td>'+
'<td>'+esc(r.continue_day_text||'')+'</td><td>'+money(r.seal_money)+'</td></tr>';});
return h+'</table>';}
function render(d){
var h='';
if(d.indices&&d.indices.length){h+='<section><h2>指数（'+d.indices.length+'）</h2><div class="cards index">';
d.indices.forEach(function(r){h+=card(r,d,true);});h+='</div></section>';}
if(d.stocks&&d.stocks.length){h+='<section><h2>自选股（'+d.stocks.length+
'）· 按涨跌幅排序</h2><div class="cards">';
d.stocks.forEach(function(r){h+=card(r,d,false);});h+='</div></section>';}
if(d.limit_up&&d.limit_up.length)h+='<section><h2>涨停池（'+d.limit_up.length+
'）· 按封板时间</h2>'+limitRows(d.limit_up)+'</section>';
(d.errors||[]).forEach(function(e){h+='<p>注：'+esc(e)+'</p>';});
document.getElementById('main').innerHTML=h||'<p>暂无数据</p>';
var demo=d.demo?'【演示数据】':'';
document.getElementById('meta').textContent='生成 '+bj(d.generated)+'（北京时间） · 数据时间 '+
bj(d.data_time)+'（北京时间） '+demo+' · 每'+POLL+'秒检查一次';
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


# ---------------------------------------------------------------------------
# EPUB（Kindle 字模式卡片版）：crengine 不认 flex/grid/SVG，这里用表格排卡片，
# 迷你走势图只出现在网页面板；EPUB 里给 20 日收盘的起止与区间高低。
# ---------------------------------------------------------------------------

EPUB_CSS = """body{font-family:sans-serif;line-height:1.5}
h1{font-size:1.4em;border-bottom:2px solid #000;padding-bottom:4px}
h2{font-size:1.1em;border-bottom:1px solid #000;padding-bottom:2px;margin-top:1.2em}
.meta{font-size:0.85em}
table.card{border:1px solid #000;width:100%;border-collapse:collapse;margin:8px 0}
table.card td{padding:3px 6px;vertical-align:top}
table.card .big{font-size:1.3em;font-weight:bold}
table.data{border-collapse:collapse;width:100%}
table.data th,table.data td{border:1px solid #999;padding:3px 5px;font-size:0.9em}
.up{font-weight:bold}.down{font-weight:bold}
"""

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def _epub_escape(text):
    return html_mod.escape(str(text if text is not None else ""), quote=True)


def render_epub_xhtml(payload, generated_bj):
    """EPUB 章节 XHTML：指数表 + 自选股卡片表 + 涨停表（crengine 友好）。"""
    esc = _epub_escape
    demo = "【演示数据】" if payload.get("demo") else ""
    data_text = beijing_text(payload.get("data_timestamp")) or "无"
    parts = [f"<h1>{esc(TITLE)}{esc(demo)}</h1>",
             f'<p class="meta">生成 {esc(generated_bj)} 北京时间 · 数据 {esc(data_text)}</p>',
             f'<p class="meta">{esc(DISCLAIMER)}</p>']

    indices = payload.get("indices") or []
    if indices:
        parts.append(f"<h2>指数（{len(indices)}）</h2>")
        parts.append('<table class="data"><tr><th>名称</th><th>最新</th>'
                     "<th>涨跌幅</th><th>涨跌</th><th>开盘</th><th>最高</th>"
                     "<th>最低</th></tr>")
        for row in indices:
            parts.append(
                f"<tr><td>{esc(row.get('name') or row.get('thscode'))}</td>"
                f"<td>{esc(fmt_price(row.get('last_price')))}</td>"
                f"<td>{esc(fmt_ratio(row.get('price_change_ratio_pct')))}</td>"
                f"<td>{esc(fmt_change(row.get('price_change')))}</td>"
                f"<td>{esc(fmt_price(row.get('open_price')))}</td>"
                f"<td>{esc(fmt_price(row.get('high_price')))}</td>"
                f"<td>{esc(fmt_price(row.get('low_price')))}</td></tr>")
        parts.append("</table>")

    history = payload.get("history") or {}
    stocks = payload.get("stocks") or []
    if stocks:
        parts.append(f"<h2>自选股（{len(stocks)}）· 按涨跌幅排序</h2>")
        for row in stocks:
            closes = [pair[1] for pair in history.get(row.get("thscode") or [], [])
                      if len(pair) > 1 and isinstance(pair[1], (int, float))]
            trend = ""
            if len(closes) >= 2:
                trend = (f"近{len(closes)}日收盘 {closes[0]:,.2f} → "
                         f"{closes[-1]:,.2f}（区间 {min(closes):,.2f}–"
                         f"{max(closes):,.2f}）")
            parts.append(
                '<table class="card">'
                f"<tr><td><b>{esc(row.get('name') or row.get('thscode'))}</b> "
                f"{esc(row.get('thscode') or '')}</td>"
                f'<td class="big">{esc(fmt_price(row.get("last_price")))}</td>'
                f"<td>{esc(fmt_ratio(row.get('price_change_ratio_pct')))} "
                f"{esc(fmt_change(row.get('price_change')))}</td></tr>"
                f"<tr><td>开 {esc(fmt_price(row.get('open_price')))} · "
                f"高 {esc(fmt_price(row.get('high_price')))} · "
                f"低 {esc(fmt_price(row.get('low_price')))}</td>"
                f"<td>成交 {esc(fmt_volume(row.get('volume')))}</td>"
                f"<td>成交额 {esc(fmt_turnover(row.get('turnover')))}</td></tr>"
                + (f"<tr><td colspan=\"3\">{esc(trend)}</td></tr>" if trend else "")
                + "</table>")

    limit_up = payload.get("limit_up") or []
    if limit_up:
        parts.append(f"<h2>涨停池（{len(limit_up)}）· 按封板时间</h2>")
        parts.append('<table class="data"><tr><th>名称</th><th>最新</th>'
                     "<th>涨跌幅</th><th>封板</th><th>原因</th><th>连板</th>"
                     "<th>封单额</th></tr>")
        for row in limit_up:
            parts.append(
                f"<tr><td>{esc(row.get('name') or row.get('thscode'))}</td>"
                f"<td>{esc(fmt_price(row.get('last_price')))}</td>"
                f"<td>{esc(fmt_ratio(row.get('price_change_ratio_pct')))}</td>"
                f"<td>{esc(row.get('limit_up_time'))}</td>"
                f"<td>{esc(row.get('limit_up_reason'))}</td>"
                f"<td>{esc(row.get('continue_day_text'))}</td>"
                f"<td>{esc(fmt_turnover(row.get('seal_money')))}</td></tr>")
        parts.append("</table>")

    for error in payload.get("errors") or []:
        parts.append(f"<p>注：{esc(error)}</p>")
    parts.append(f"<p>{esc(DISCLAIMER)}</p>")
    body = "\n".join(parts)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">'
            f"<head><title>{esc(TITLE)}</title>"
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>'
            f"<body>{body}</body></html>\n")


def write_epub(path, xhtml, title=TITLE):
    """写一个自包含的 EPUB 3（mimetype 必须第一且不压缩）。"""
    import uuid
    import zipfile

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    identifier = "urn:uuid:" + str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"004-kindle:stocks:{hashlib.sha256(xhtml.encode('utf-8')).hexdigest()}"))
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">
    <dc:identifier id="BookID">{identifier}</dc:identifier>
    <dc:title>{_epub_escape(title)}</dc:title>
    <dc:language>zh-CN</dc:language>
    <meta property="dcterms:modified">{stamp}</meta>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="style" href="style.css" media-type="text/css"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"><itemref idref="chapter"/></spine>
</package>
"""
    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN">
<head><title>{_epub_escape(title)}</title></head>
<body><nav epub:type="toc"><h1>{_epub_escape(title)}</h1>
<ol><li><a href="chapter.xhtml">{_epub_escape(title)}</a></li></ol></nav></body></html>
"""
    ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{identifier}"/></head>
  <docTitle><text>{_epub_escape(title)}</text></docTitle>
  <navMap><navPoint id="n1" playOrder="1"><navLabel><text>{_epub_escape(title)}</text></navLabel>
  <content src="chapter.xhtml"/></navPoint></navMap>
</ncx>
"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(temporary, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML,
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/style.css", EPUB_CSS, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/chapter.xhtml", xhtml, compress_type=zipfile.ZIP_DEFLATED)
    temporary.replace(path)


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
    epub_path = out / "stocks.epub"
    if not epub_path.is_file():
        problems.append("缺少 stocks.epub")
    else:
        import zipfile
        try:
            with zipfile.ZipFile(epub_path) as zf:
                names = zf.namelist()
                if not names or names[0] != "mimetype":
                    problems.append("stocks.epub 的 mimetype 不是第一项")
                if "OEBPS/chapter.xhtml" not in names:
                    problems.append("stocks.epub 里没有章节")
                chapter = zf.read("OEBPS/chapter.xhtml").decode("utf-8")
                if TITLE not in chapter:
                    problems.append("stocks.epub 章节里没有面板标题")
        except zipfile.BadZipFile as exc:
            problems.append(f"stocks.epub 不是有效 EPUB：{exc}")
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
    "history": {
        "600519.SH": [[1784275991000 - (19 - i) * 86400000,
                       1200.0 + i * 4.1] for i in range(20)],
        "000001.SH": [[1784275991000 - (19 - i) * 86400000,
                       3600.0 + i * 3.4] for i in range(20)],
    },
    "errors": [],
}


def build(data_path=None, out_dir="site", demo=False):
    """读 stocks_data.json（或演示数据）→ 写四份产物，返回清单。"""
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
    write_epub(out / "stocks.epub",
               render_epub_xhtml(payload, now_beijing_text()))
    problems = check(out)
    if problems:
        raise ValueError("产物自检不过：" + "；".join(problems))
    print(f"已生成 {out}/stocks.json、stocks.txt、stocks.html、stocks.epub"
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
