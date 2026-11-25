#!/usr/bin/env python3
"""大 V / 策略分析师的近五天公开观点报告（纯标准库）。

输入：``bigv/people.json``（名单）与 ``bigv/items.json``（条目，每条带原文链接）。
输出：``<out>/report-<日期>.md`` 与 ``<out>/latest.md``。

    python3 tools/bigv_report.py --as-of 2026-11-25 --days 5 --out bigv/report

范围与限制
----------
- 时间窗口：``as_of`` 往前数 ``days`` 天（含当天）。窗口外的条目不计入。
- 维度：发言、观点、评论、采访、报道、视频、文章、直播、互动、讨论。
  沙箱访问不了 X、YouTube、微博、雪球等平台，所以没有条目的维度会在报告里如实写明，
  不会被悄悄算成 0 或者补数。
- 结果导向：每位对象给出五天内的条目数、最近一条、要点和原文链接；没有记录的写「五天内未找到公开观点」。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PEOPLE = ROOT / "bigv" / "people.json"
DEFAULT_ITEMS = ROOT / "bigv" / "items.json"
DEFAULT_OUT = ROOT / "bigv" / "report"

DIMENSIONS = ["发言", "观点", "评论", "采访", "报道", "视频", "文章", "直播", "互动", "讨论"]
MARKETS = {"US": "美股", "CN": "A 股"}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AI_VIEW_NOTE = "多空概率是 AI 依据该人近五天条目的语气作出的判断，不是本人表态，仅供参考。"


class DataError(ValueError):
    """名单或条目不合法：直接报错，不静默丢弃。"""


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate(people_doc: dict, items_doc: dict) -> tuple[list[dict], list[dict]]:
    people = people_doc.get("people") or []
    ids = {p["id"] for p in people}
    if len(ids) != len(people):
        raise DataError("people.json 里有重复的 id")
    for p in people:
        if p.get("market") not in MARKETS:
            raise DataError(f"{p.get('id')}: market 只能是 US 或 CN")
        prob = p.get("ai_bull_prob")
        if prob is not None and not (isinstance(prob, (int, float)) and 0 <= prob <= 1):
            raise DataError(f"{p.get('id')}: ai_bull_prob 必须在 0 到 1 之间或为 null")
    items = items_doc.get("items") or []
    for i, it in enumerate(items):
        where = f"items[{i}]"
        if it.get("person") not in ids:
            raise DataError(f"{where}: 未知的 person {it.get('person')!r}")
        if not _ISO.match(str(it.get("date", ""))):
            raise DataError(f"{where}: date 必须是 YYYY-MM-DD")
        if it.get("dimension") not in DIMENSIONS:
            raise DataError(f"{where}: dimension {it.get('dimension')!r} 不在支持的维度里")
        if not str(it.get("url", "")).startswith("https://"):
            raise DataError(f"{where}: url 必须是 https 链接（每条都要能核对原文）")
        if not str(it.get("summary", "")).strip():
            raise DataError(f"{where}: 缺少 summary")
    return people, items


def window(as_of: str, days: int) -> tuple[str, str]:
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=max(days, 1) - 1)
    return start.isoformat(), end.isoformat()


def summarize(people: list[dict], items: list[dict], as_of: str, days: int) -> dict:
    start, end = window(as_of, days)
    inside = [it for it in items if start <= it["date"] <= end]
    rows = []
    for p in people:
        mine = sorted((it for it in inside if it["person"] == p["id"]),
                      key=lambda it: (it["date"], it["title"]), reverse=True)
        dims: dict[str, int] = {}
        for it in mine:
            dims[it["dimension"]] = dims.get(it["dimension"], 0) + 1
        rows.append({"id": p["id"], "market": p["market"], "name": p["name"],
                     "org": p.get("org", ""), "role": p.get("role", ""),
                     "note": p.get("note", ""), "count": len(mine),
                     "ai_bull_prob": p.get("ai_bull_prob"),
                     "dimensions": dims, "latest": mine[0]["date"] if mine else None,
                     "items": mine})
    seen_dims = {it["dimension"] for it in inside}
    coverage = {m: {"people": sum(1 for r in rows if r["market"] == m),
                    "with_items": sum(1 for r in rows if r["market"] == m and r["count"])}
                for m in MARKETS}
    return {"as_of": as_of, "window": [start, end], "days": days, "rows": rows,
            "items_in_window": len(inside), "coverage": coverage,
            "dims_seen": [d for d in DIMENSIONS if d in seen_dims],
            "dims_missing": [d for d in DIMENSIONS if d not in seen_dims]}


def ai_view_label(prob) -> str:
    """0~1 的多头概率 → 「偏多 70%」「偏空 65%」；没有概率（五天内无记录）返回「—」。"""
    if prob is None:
        return "—"
    lean = "偏多" if prob >= 0.5 else "偏空"
    return f"{lean} {max(prob, 1 - prob):.0%}"


def _cell(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def render_markdown(s: dict) -> str:
    start, end = s["window"]
    cov = s["coverage"]
    out = [f"# 大 V 与策略分析师：近五天公开观点（{start} 至 {end}）", "",
           f"截至 {s['as_of']}，窗口内共 {s['items_in_window']} 条。"
           + "；".join(f"{MARKETS[m]} {cov[m]['with_items']}/{cov[m]['people']} 位有记录"
                      for m in MARKETS) + "。",
           "",
           f"**注意：{AI_VIEW_NOTE}**",
           "",
           "名单标准：机构策略分析师、基金管理人（美股、A 股各 50 位为目标，当前已核实 "
           f"{len(s['rows'])} 位）。个别人与「同级别」不完全一致，在表格备注里说明。",
           ""]
    for m, label in MARKETS.items():
        out += [f"## {label}", "",
                "| 人物 | 机构 | 条目 | 维度 | 最近 | 状态 | AI 多空判断 |",
                "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in (x for x in s["rows"] if x["market"] == m):
            dims = "、".join(f"{d}×{n}" for d, n in r["dimensions"].items()) or "—"
            status = "有记录" if r["count"] else "五天内未找到公开观点"
            out.append(f"| {_cell(r['name'])} | {_cell(r['org'])} | {r['count']} | {dims} | "
                       f"{r['latest'] or '—'} | {status} | {ai_view_label(r['ai_bull_prob'])} |")
        out.append("")
    out += ["## 逐条要点", ""]
    for m, label in MARKETS.items():
        out += [f"### {label}", ""]
        for r in (x for x in s["rows"] if x["market"] == m and x["items"]):
            out.append(f"**{r['name']}**（{r['org']}，{r['role']}）")
            for it in r["items"]:
                out.append(f"- {it['date']} · {it['dimension']} · {it['summary']} "
                           f"[{it['source']}]({it['url']})")
            if r["note"]:
                out.append(f"- 备注：{r['note']}")
            out.append("")
        empty = [x for x in s["rows"] if x["market"] == m and not x["items"]]
        if empty:
            names = [x["name"] + (f"（{x['note']}）" if x["note"] else "") for x in empty]
            out.append("五天内未找到公开观点：" + "、".join(names) + "。")
            out.append("")
    out += ["## 数据覆盖说明", ""]
    out.append("本期出现的维度：" + ("、".join(s["dims_seen"]) or "无") + "。")
    out.append("本期没有条目的维度：" + "、".join(s["dims_missing"]) + "。"
               "这些平台（如 X、YouTube、微博、雪球、直播间）沙箱访问不了，需要数据源（RSSHub 或导入文件）才能补上。")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--as-of", default=date.today().isoformat(), help="报告截止日 YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=5, help="窗口天数（含当天），默认 5")
    ap.add_argument("--people", default=str(DEFAULT_PEOPLE))
    ap.add_argument("--items", default=str(DEFAULT_ITEMS))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)
    try:
        people, items = validate(load_json(Path(args.people)), load_json(Path(args.items)))
    except (DataError, KeyError, json.JSONDecodeError) as exc:
        print(f"数据有问题：{exc}", file=sys.stderr)
        return 2
    summary = summarize(people, items, args.as_of, args.days)
    text = render_markdown(summary)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"report-{args.as_of}.md").write_text(text, encoding="utf-8")
    (out / "latest.md").write_text(text, encoding="utf-8")
    print(f"已生成 {out / f'report-{args.as_of}.md'}（窗口 {summary['window'][0]}~{summary['window'][1]}，"
          f"{summary['items_in_window']} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
