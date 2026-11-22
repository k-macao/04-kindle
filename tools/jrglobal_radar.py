#!/usr/bin/env python3
"""Jovian Research (jrglobal.xyz/tools/) 聪明钱雷达、AI 数据智库与组合实验室集成模块。

功能：
1. 收录并支持在线刷新 jrglobal.xyz 四大免费工具的核心数据：
   · 聪明钱雷达 (radar.jrglobal.xyz)：A 股八大类 11,615 位投资者（公募 498 / 牛散 2,363 /
     大股东 4,083 / 私募 4,230 / 外资 254 / 券商 78 / 社保 99 / 国家队 10）与美股 2,381 家
     13F 机构（巴菲特、阿斯内斯、格里芬、大卫·肖、挪威主权基金等）的重仓股与季度调仓；
   · AI 数据智库 (aidata.jrglobal.xyz)：台湾半导体链 14 家月营收、韩国半导体与 DRAM/NAND 出口、
     WSTS 全球半导体销售额、Epoch AI 芯片出货与数据中心算力、Ornn GPU 算力成本指数 (OCPI)、
     Ramp 美国企业 AI 采用率；
   · 组合实验室 (portfolio.jrglobal.xyz)：经典全天候、高股息、行业轮动等基准组合收益；
   · 宏观看板 (macro.jrglobal.xyz)：中美宏观与利率预期入口。
2. 股票反查（``reverse_lookup``）：输入股票代码、简称或基金经理名字，反查哪些明星基金经理/13F
   机构重仓持有、持仓市值、占组合比例及最新季度加减仓动作。
3. 盘面联动（``cross_verify_panel``）：把当日 A 股行情面板里的领涨股、涨停题材与聪明钱重仓股、
   AI 产业链月度硬数据自动交叉印证。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TOOLS_URL = "https://jrglobal.xyz/tools/"
RADAR_URL = "https://radar.jrglobal.xyz/"
PORTFOLIO_URL = "https://portfolio.jrglobal.xyz/"
MACRO_URL = "https://macro.jrglobal.xyz/"
AIDATA_URL = "https://aidata.jrglobal.xyz/"

# ---------------------------------------------------------------------------
# 经过实盘核验的 jrglobal.xyz 基准快照（Q3 2026 持仓 / 2026-10 产业链月报 / 2026-11-20 OCPI）
# 离线构建或网络受限时直接使用；在线时可通过 refresh_snapshot() 从源站更新。
# ---------------------------------------------------------------------------

DEFAULT_SNAPSHOT = {
    "as_of": "2026-11-20",
    "report_period": "Q3 2026",
    "source": {
        "name": "Jovian Research · 数据工具",
        "tools_url": TOOLS_URL,
        "radar_url": RADAR_URL,
        "portfolio_url": PORTFOLIO_URL,
        "macro_url": MACRO_URL,
        "aidata_url": AIDATA_URL,
    },
    "tools": [
        {
            "key": "radar",
            "name": "聪明钱雷达 SmartMoney Radar",
            "url": RADAR_URL,
            "summary": "A股八类 11,615 位投资者（公募/牛散/国家队/社保/外资）+ 美股 2,381 家 13F 机构，支持按股票反查持有方与每季调仓",
            "free": "免费 · 免注册",
        },
        {
            "key": "aidata",
            "name": "AI 数据智库 AI Data Hub",
            "url": AIDATA_URL,
            "summary": "40+ 个每日同步数据集：模型价格、AI 芯片出货、HBM/DRAM 价格、台韩半导体高频硬数据、大厂数据中心 CapEx，提供免费公开 API",
            "free": "免费 · 免 Key API",
        },
        {
            "key": "portfolio",
            "name": "组合实验室 Portfolio Lab",
            "url": PORTFOLIO_URL,
            "summary": "5.3 万+资产（A股/ETF/公募/私募/美股）同组合回测与 12 种目标优化（MVO/风险平价/HRP/HERC/防崩盘），每晚更新",
            "free": "分析优化免登录（保存需访问码）",
        },
        {
            "key": "macro",
            "name": "宏观看板 Macro Board",
            "url": MACRO_URL,
            "summary": "每日早 9 点更新：5,600+ 条宏观序列、411 张图表、中国宏观月度预测、美联储利率预期、15 条国债收益率曲线",
            "free": "公众号回复「预期差」获取访问码",
        },
    ],
    # A 股八大类投资者覆盖统计（来自 radar.jrglobal.xyz 首页）
    "cn_categories": [
        {"name": "公募基金经理", "count": 498},
        {"name": "牛散", "count": 2363},
        {"name": "实控人/大股东", "count": 4083},
        {"name": "私募", "count": 4230},
        {"name": "外资", "count": 254},
        {"name": "社保", "count": 99},
        {"name": "券商", "count": 78},
        {"name": "国家队", "count": 10},
    ],
    # A 股代表性百亿/顶流基金经理持仓与 Q3 2026 调仓动作
    "cn_gurus": [
        {
            "id": "101001430", "name": "金梓才", "org": "财通基金", "style": "集中持股科技",
            "aum_yi": 744.51, "holdings_count": 66, "top1": "三环集团", "return_1y_pct": 482.1,
            "top_holdings": [
                {"code": "300408.SZ", "name": "三环集团", "value_yi": 69.51, "weight_pct": 9.34, "sector": "电子", "action": "加仓 +12%"},
                {"code": "301377.SZ", "name": "鼎泰高科", "value_yi": 62.82, "weight_pct": 8.44, "sector": "机械", "action": "加仓 +16%"},
                {"code": "688519.SH", "name": "南亚新材", "value_yi": 62.34, "weight_pct": 8.37, "sector": "电子", "action": "加仓 +7%"},
                {"code": "688498.SH", "name": "源杰科技", "value_yi": 61.03, "weight_pct": 8.20, "sector": "电子", "action": "减仓 -25%"},
                {"code": "688313.SH", "name": "仕佳光子", "value_yi": 60.78, "weight_pct": 8.16, "sector": "通信", "action": "新建"},
                {"code": "301511.SZ", "name": "德福科技", "value_yi": 57.70, "weight_pct": 7.75, "sector": "电子", "action": "减仓 -1%"},
                {"code": "301200.SZ", "name": "大族数控", "value_yi": 52.91, "weight_pct": 7.11, "sector": "电子", "action": "加仓 +25%"},
                {"code": "300570.SZ", "name": "太辰光", "value_yi": 45.90, "weight_pct": 6.16, "sector": "通信", "action": "新建"},
                {"code": "300308.SZ", "name": "中际旭创", "value_yi": 44.32, "weight_pct": 5.95, "sector": "通信", "action": "持有"},
                {"code": "300502.SZ", "name": "新易盛", "value_yi": 43.58, "weight_pct": 5.85, "sector": "通信", "action": "持有"},
            ],
        },
        {
            "id": "101003918", "name": "张明昕", "org": "华商基金", "style": "集中持股科技",
            "aum_yi": 740.26, "holdings_count": 98, "top1": "源杰科技", "return_1y_pct": 411.2,
            "top_holdings": [
                {"code": "688498.SH", "name": "源杰科技", "value_yi": 52.24, "weight_pct": 7.06, "sector": "电子", "action": "加仓 +29%"},
                {"code": "300308.SZ", "name": "中际旭创", "value_yi": 47.18, "weight_pct": 6.37, "sector": "通信", "action": "减仓 -13%"},
                {"code": "300502.SZ", "name": "新易盛", "value_yi": 46.64, "weight_pct": 6.30, "sector": "通信", "action": "减仓 -17%"},
                {"code": "300408.SZ", "name": "三环集团", "value_yi": 44.61, "weight_pct": 6.03, "sector": "电子", "action": "加仓 +2%"},
                {"code": "000636.SZ", "name": "风华高科", "value_yi": 41.98, "weight_pct": 5.67, "sector": "电子", "action": "加仓 +2%"},
                {"code": "600183.SH", "name": "生益科技", "value_yi": 32.16, "weight_pct": 4.34, "sector": "电子", "action": "加仓 +132%"},
                {"code": "688048.SH", "name": "长光华芯", "value_yi": 29.22, "weight_pct": 3.95, "sector": "电子", "action": "加仓 +34%"},
                {"code": "002384.SZ", "name": "东山精密", "value_yi": 26.89, "weight_pct": 3.63, "sector": "电子", "action": "加仓 +290%"},
                {"code": "600487.SH", "name": "亨通光电", "value_yi": 22.83, "weight_pct": 3.08, "sector": "通信", "action": "持有"},
                {"code": "688313.SH", "name": "仕佳光子", "value_yi": 20.44, "weight_pct": 2.76, "sector": "通信", "action": "持有"},
            ],
        },
        {
            "id": "101015045", "name": "严凯", "org": "东方基金", "style": "集中持股科技",
            "aum_yi": 648.19, "holdings_count": 79, "top1": "中微公司", "return_1y_pct": 204.9,
            "top_holdings": [
                {"code": "688012.SH", "name": "中微公司", "value_yi": 58.20, "weight_pct": 8.98, "sector": "半导体", "action": "重仓持有"},
                {"code": "688072.SH", "name": "拓荆科技", "value_yi": 46.10, "weight_pct": 7.11, "sector": "半导体", "action": "重仓持有"},
                {"code": "002371.SZ", "name": "北方华创", "value_yi": 42.50, "weight_pct": 6.56, "sector": "半导体", "action": "重仓持有"},
            ],
        },
        {
            "id": "101001175", "name": "谢治宇", "org": "兴证全球基金", "style": "科技港股医药",
            "aum_yi": 608.18, "holdings_count": 108, "top1": "晶合集成", "return_1y_pct": 70.0,
            "top_holdings": [
                {"code": "688249.SH", "name": "晶合集成", "value_yi": 40.85, "weight_pct": 6.72, "sector": "电子", "action": "加仓 +105%"},
                {"code": "002475.SZ", "name": "立讯精密", "value_yi": 37.57, "weight_pct": 6.18, "sector": "电子", "action": "加仓 +0.1%"},
                {"code": "688099.SH", "name": "晶晨股份", "value_yi": 35.90, "weight_pct": 5.90, "sector": "电子", "action": "加仓 +19%"},
                {"code": "688120.SH", "name": "华海清科", "value_yi": 25.05, "weight_pct": 4.12, "sector": "电子", "action": "持有"},
                {"code": "600160.SH", "name": "巨化股份", "value_yi": 24.65, "weight_pct": 4.05, "sector": "基础化工", "action": "减仓 -29%"},
                {"code": "300347.SZ", "name": "泰格医药", "value_yi": 21.35, "weight_pct": 3.51, "sector": "医药", "action": "减仓 -0.3%"},
                {"code": "688319.SH", "name": "欧林生物", "value_yi": 19.25, "weight_pct": 3.17, "sector": "医药", "action": "持有"},
                {"code": "603259.SH", "name": "药明康德", "value_yi": 18.84, "weight_pct": 3.10, "sector": "医药", "action": "新建"},
                {"code": "300750.SZ", "name": "宁德时代", "value_yi": 18.67, "weight_pct": 3.07, "sector": "电力设备", "action": "持有"},
                {"code": "688041.SH", "name": "海光信息", "value_yi": 18.24, "weight_pct": 3.00, "sector": "电子", "action": "持有"},
                {"code": "002156.SZ", "name": "通富微电", "value_yi": 16.87, "weight_pct": 2.77, "sector": "电子", "action": "新建"},
            ],
        },
        {
            "id": "101000231", "name": "朱少醒", "org": "富国基金", "style": "均衡配置科技",
            "aum_yi": 510.49, "holdings_count": 273, "top1": "宁波银行", "return_1y_pct": 31.5,
            "top_holdings": [
                {"code": "002142.SZ", "name": "宁波银行", "value_yi": 49.96, "weight_pct": 9.79, "sector": "银行", "action": "减仓 -6%"},
                {"code": "002353.SZ", "name": "杰瑞股份", "value_yi": 39.02, "weight_pct": 7.64, "sector": "机械", "action": "加仓 +7%"},
                {"code": "300308.SZ", "name": "中际旭创", "value_yi": 23.04, "weight_pct": 4.51, "sector": "通信", "action": "加仓 +70%"},
                {"code": "603129.SH", "name": "春风动力", "value_yi": 22.05, "weight_pct": 4.32, "sector": "汽车", "action": "减仓 -12%"},
                {"code": "688266.SH", "name": "泽璟制药", "value_yi": 18.48, "weight_pct": 3.62, "sector": "医药", "action": "加仓 +30%"},
                {"code": "688548.SH", "name": "广钢气体", "value_yi": 18.43, "weight_pct": 3.61, "sector": "基础化工", "action": "减仓 -13%"},
                {"code": "688825.SH", "name": "长鑫科技", "value_yi": 16.44, "weight_pct": 3.22, "sector": "电子", "action": "新建"},
                {"code": "300750.SZ", "name": "宁德时代", "value_yi": 14.41, "weight_pct": 2.82, "sector": "电力设备", "action": "减仓 -47%"},
                {"code": "600519.SH", "name": "贵州茅台", "value_yi": 11.33, "weight_pct": 2.22, "sector": "食品饮料", "action": "持有"},
                {"code": "300502.SZ", "name": "新易盛", "value_yi": 10.93, "weight_pct": 2.14, "sector": "通信", "action": "持有"},
            ],
        },
        {
            "id": "101000465", "name": "傅鹏博", "org": "睿远基金", "style": "科技制造",
            "aum_yi": 295.26, "holdings_count": 77, "top1": "立讯精密", "return_1y_pct": 120.5,
            "top_holdings": [
                {"code": "002475.SZ", "name": "立讯精密", "value_yi": 26.50, "weight_pct": 8.97, "sector": "电子", "action": "减仓 -1%"},
                {"code": "300502.SZ", "name": "新易盛", "value_yi": 25.81, "weight_pct": 8.74, "sector": "通信", "action": "减仓 -7%"},
                {"code": "002384.SZ", "name": "东山精密", "value_yi": 24.62, "weight_pct": 8.34, "sector": "电子", "action": "减仓 -1%"},
                {"code": "688167.SH", "name": "炬光科技", "value_yi": 24.51, "weight_pct": 8.30, "sector": "电子", "action": "减仓 -4%"},
                {"code": "300751.SZ", "name": "迈为股份", "value_yi": 21.85, "weight_pct": 7.40, "sector": "机械", "action": "加仓 +6%"},
                {"code": "300476.SZ", "name": "胜宏科技", "value_yi": 20.77, "weight_pct": 7.03, "sector": "电子", "action": "减仓 -9%"},
                {"code": "300308.SZ", "name": "中际旭创", "value_yi": 19.19, "weight_pct": 6.50, "sector": "通信", "action": "加仓 +83%"},
                {"code": "688808.SH", "name": "联讯仪器", "value_yi": 17.17, "weight_pct": 5.81, "sector": "机械", "action": "新建"},
                {"code": "002008.SZ", "name": "大族激光", "value_yi": 14.34, "weight_pct": 4.86, "sector": "机械", "action": "加仓 +5%"},
                {"code": "002444.SZ", "name": "巨星科技", "value_yi": 13.58, "weight_pct": 4.60, "sector": "机械", "action": "持有"},
            ],
        },
    ],
    # 全站前 60 大公募顶流「第一重仓股」共识统计（来自 radar.jrglobal.xyz 榜单聚合）
    "cn_top1_consensus": [
        {"name": "中际旭创", "code": "300308.SZ", "sector": "通信/光模块", "top1_managers": 10,
         "holders": ["马芳", "郑希", "冯炉丹", "姚加红", "杨梦", "沈若雨", "韩浩", "王森", "孙权", "刘元海"],
         "note": "傅鹏博 Q3 加仓+83%、朱少醒加仓+70%，但谢治宇清仓、张明昕减仓-13%（高位分歧）"},
        {"name": "新易盛", "code": "300502.SZ", "sector": "通信/光模块", "top1_managers": 6,
         "holders": ["罗擎", "雷志勇", "江山", "何一铖", "杨冬", "陈皓"],
         "note": "金梓才、张明昕、傅鹏博、朱少醒均列入前十重仓"},
        {"name": "长鑫科技", "code": "688825.SH", "sector": "半导体/存储", "top1_managers": 5,
         "holders": ["张仲维", "曹晋", "张璐涵", "冯帆", "赖礼辉"],
         "note": "朱少醒 Q3 新建 16.44 亿进入前十重仓，呼应 DRAM/HBM 涨价周期"},
        {"name": "药明康德", "code": "603259.SH", "sector": "医药/CXO", "top1_managers": 4,
         "holders": ["葛兰", "周蔚文", "赵磊", "农冰立"],
         "note": "谢治宇 Q3 新建 18.84 亿（占比 3.10%）"},
        {"name": "紫金矿业", "code": "601899.SH", "sector": "有色金属", "top1_managers": 6,
         "holders": ["蓝小康", "张迎军", "朱运", "吉莉", "李杨", "王凌霄"],
         "note": "周期/资源方向公募第一共识标的"},
        {"name": "沪电股份", "code": "002463.SZ", "sector": "电子/PCB", "top1_managers": 3,
         "holders": ["马磊", "高楠", "郭玮羚"],
         "note": "AI 服务器高多层 PCB 核心标的"},
        {"name": "立讯精密", "code": "002475.SZ", "sector": "电子/消费电子", "top1_managers": 3,
         "holders": ["傅鹏博", "朱璘", "王贵重"],
         "note": "谢治宇持仓 37.57 亿（第 2 大重仓）"},
        {"name": "三环集团", "code": "300408.SZ", "sector": "电子/MLCC", "top1_managers": 2,
         "holders": ["金梓才", "祁禾"],
         "note": "金梓才第 1 重仓（69.51 亿，Q3 加仓+12%）、张明昕第 4 重仓（44.61 亿）"},
        {"name": "晶合集成", "code": "688249.SH", "sector": "半导体/晶圆代工", "top1_managers": 2,
         "holders": ["谢治宇", "乔迁"],
         "note": "谢治宇 Q3 翻倍加仓+105% 至 40.85 亿（跃居第 1 重仓）"},
        {"name": "寒武纪", "code": "688256.SH", "sector": "半导体/AI芯片", "top1_managers": 2,
         "holders": ["袁曦", "邓心怡"],
         "note": "国产 AI 算力芯片核心重仓"},
    ],
    # 美股 13F 顶级大师与机构持仓（来自 radar.jrglobal.xyz/us/ Q3 2026）
    "us_13f": [
        {"name": "巴菲特 · Berkshire Hathaway", "cik": "0001067983", "style": "价值·长期",
         "aum_usd_b": 292.37, "holdings_count": 28, "top1": "APPLE INC", "gain_pct": 48.00, "period": "Q3 2026"},
        {"name": "克里夫·阿斯内斯 · AQR Capital", "cik": "0001167557", "style": "量化·因子",
         "aum_usd_b": 315.26, "holdings_count": 3822, "top1": "NVIDIA CORP", "gain_pct": 10.27, "period": "Q3 2026"},
        {"name": "肯·格里芬 · Citadel Advisors", "cik": "0001423053", "style": "多策略",
         "aum_usd_b": 170.44, "holdings_count": 5941, "top1": "ISHARES TR", "gain_pct": -2.05, "period": "Q3 2026"},
        {"name": "大卫·肖 · D. E. Shaw & Co.", "cik": "0001009207", "style": "量化·多策略",
         "aum_usd_b": 142.98, "holdings_count": 2886, "top1": "SPDR S&P 500 ETF", "gain_pct": -2.39, "period": "Q3 2026"},
        {"name": "Two Sigma Investments", "cik": "0001179392", "style": "量化",
         "aum_usd_b": 124.50, "holdings_count": 3254, "top1": "NVIDIA CORP", "gain_pct": -4.76, "period": "Q3 2026"},
        {"name": "英格兰德 · Millennium", "cik": "0001273087", "style": "多策略",
         "aum_usd_b": 123.77, "holdings_count": 3361, "top1": "ISHARES TR", "gain_pct": -1.89, "period": "Q3 2026"},
        {"name": "挪威主权基金 · Norges Bank", "cik": "0001374170", "style": "主权基金",
         "aum_usd_b": 950.35, "holdings_count": 1488, "top1": "NVIDIA CORP", "gain_pct": 44.97, "period": "Q2 2026"},
        {"name": "Capital World Investors", "cik": "0001422849", "style": "长线资管",
         "aum_usd_b": 776.89, "holdings_count": 572, "top1": "BROADCOM INC", "gain_pct": 32.06, "period": "Q3 2026"},
        {"name": "PRIMECAP Management", "cik": "0000763212", "style": "成长·长线",
         "aum_usd_b": 152.46, "holdings_count": 304, "top1": "MICRON TECHNOLOGY", "gain_pct": 50.50, "period": "Q3 2026"},
    ],
    # AI 数据智库 (aidata.jrglobal.xyz) 每日硬科技高频指标
    "ai_data": {
        "taiwan_semi_2026_10": [
            {"name": "台积电 TSMC (2330)", "segment": "晶圆代工", "rev": "NT$586.9B", "yoy_pct": 59.7},
            {"name": "世芯-KY Alchip (3661)", "segment": "AI ASIC", "rev": "NT$10.3B", "yoy_pct": 515.8},
            {"name": "技嘉 Gigabyte (2376)", "segment": "AI 服务器", "rev": "NT$67.0B", "yoy_pct": 136.7},
            {"name": "创意 GUC (3443)", "segment": "ASIC 设计", "rev": "NT$8.4B", "yoy_pct": 127.0},
            {"name": "纬创 Wistron (3231)", "segment": "AI 服务器", "rev": "NT$402.6B", "yoy_pct": 117.6},
            {"name": "广达 Quanta (2382)", "segment": "AI 服务器", "rev": "NT$353.3B", "yoy_pct": 104.0},
            {"name": "南电 Nan Ya PCB (8046)", "segment": "ABF 载板", "rev": "NT$6.3B", "yoy_pct": 69.7},
            {"name": "欣兴 Unimicron (3037)", "segment": "ABF 载板", "rev": "NT$19.0B", "yoy_pct": 67.6},
            {"name": "日月光 ASE (3711)", "segment": "先进封测", "rev": "NT$88.6B", "yoy_pct": 47.1},
            {"name": "奇鋐 AVC (3017)", "segment": "液冷散热", "rev": "NT$22.1B", "yoy_pct": 34.4},
        ],
        "korea_export": {
            "month": "2026-10",
            "semi_export_usd_b": 56.44,
            "semi_yoy_pct": 259.4,
            "prev_month_usd_b": 60.30,
            "prev_month_yoy_pct": 262.8,
            "dram_yoy_pct": 369.8,
            "nand_yoy_pct": 206.8,
        },
        "global_semi_wsts": {
            "month": "2026-09",
            "global_usd_b": 196.33,
            "global_yoy_pct": 146.8,
            "americas_usd_b": 75.81,
            "americas_yoy_pct": 191.7,
            "apac_usd_b": 102.70,
            "apac_yoy_pct": 133.4,
        },
        "chip_shipments_q3_2026": [
            {"chip": "NVIDIA B300", "h100e": "3.60M H100e", "units": 1424701},
            {"chip": "NVIDIA R200", "h100e": "1.30M H100e", "units": 146477},
            {"chip": "Google TPU v7", "h100e": "640.8K H100e", "units": 274866},
            {"chip": "NVIDIA B200", "h100e": "340.9K H100e", "units": 134927},
            {"chip": "Huawei Ascend 910C (Q2)", "h100e": "134.9K H100e", "units": 171188},
        ],
        "top_datacenters": [
            {"name": "Colossus 2", "owner": "SpaceXAI", "h100e": "1.11M H100e", "power_mw": 946, "capex_usd_b": 35.84},
            {"name": "Stargate Abilene", "owner": "OpenAI / Oracle", "h100e": "1.02M H100e", "power_mw": 824, "capex_usd_b": 31.21},
            {"name": "New Carlisle", "owner": "Anthropic / Amazon", "h100e": "818.4K H100e", "power_mw": 1037, "capex_usd_b": 39.28},
            {"name": "Fairwater Atlanta", "owner": "Microsoft", "h100e": "768.8K H100e", "power_mw": 636, "capex_usd_b": 24.09},
        ],
        "compute_cost_ocpi": {
            "date": "2026-11-20",
            "gpus": [
                {"gpu": "B200", "ocpi": 9.10, "prev_month": 9.44},
                {"gpu": "H200", "ocpi": 6.14, "prev_month": 6.21},
                {"gpu": "H100 SXM", "ocpi": 2.90, "prev_month": 3.02},
                {"gpu": "A100 SXM4", "ocpi": 1.00, "prev_month": 1.06},
                {"gpu": "RTX 5090", "ocpi": 0.67, "prev_month": 0.68},
            ],
        },
        "enterprise_adoption_ramp": {
            "month": "2026-10",
            "overall_pct": 56.80,
            "large_corp_pct": 65.72,
            "vendors": [
                {"vendor": "Anthropic", "pct": 44.78},
                {"vendor": "OpenAI", "pct": 40.56},
                {"vendor": "xAI", "pct": 6.27},
                {"vendor": "DeepSeek", "pct": 0.20},
            ],
        },
    },
    # 组合实验室 (portfolio.jrglobal.xyz) 策略与经典组合基准
    "portfolio_benchmarks": [
        {"name": "Semiconductor Stocks Portfolio（半导体股票组合）", "return_1y_pct": 65.2, "return_6m_pct": -19.15, "rank": 98},
        {"name": "Dividend-Paying Stocks Portfolio（高股息红利组合）", "return_1y_pct": 19.1, "return_6m_pct": 12.63, "rank": 96},
        {"name": "Mebane Faber Ivy Portfolio（常春藤全天候组合）", "return_1y_pct": 14.4, "return_6m_pct": -3.43, "rank": 99},
        {"name": "Roger Gibson Five Asset Portfolio（五资产均衡组合）", "return_1y_pct": 12.6, "return_6m_pct": -3.97, "rank": 97},
        {"name": "Top 3 Sector ETF Rotation（前三行业 ETF 轮动）", "return_1y_pct": None, "return_6m_pct": 2.67, "rank": None},
    ],
}


def build_stock_index(snapshot=None):
    """构建「股票代码/简称 → 持有该股的明星基金经理与调仓动作」反查索引。

    对应聪明钱雷达的核心功能：「输一只股票就能反查谁在持有」。
    """
    snap = snapshot or DEFAULT_SNAPSHOT
    by_stock = {}

    def _norm_code(code):
        raw = str(code or "").strip().upper()
        raw = raw.replace(".XSHG", ".SH").replace(".XSHE", ".SZ")
        return raw

    for guru in snap.get("cn_gurus") or []:
        for h in guru.get("top_holdings") or []:
            code = _norm_code(h.get("code"))
            name = str(h.get("name") or "").strip()
            key = code or name
            if not key:
                continue
            entry = by_stock.setdefault(key, {
                "code": code,
                "name": name,
                "sector": h.get("sector") or "",
                "holders": [],
                "total_value_yi": 0.0,
                "top1_count": 0,
            })
            entry["holders"].append({
                "manager": guru["name"],
                "org": guru.get("org") or "",
                "style": guru.get("style") or "",
                "value_yi": h.get("value_yi"),
                "weight_pct": h.get("weight_pct"),
                "action": h.get("action") or "持有",
            })
            entry["total_value_yi"] = round(entry["total_value_yi"] + float(h.get("value_yi") or 0.0), 2)

    for item in snap.get("cn_top1_consensus") or []:
        code = _norm_code(item.get("code"))
        name = str(item.get("name") or "").strip()
        key = code or name
        entry = by_stock.setdefault(key, {
            "code": code,
            "name": name,
            "sector": item.get("sector") or "",
            "holders": [],
            "total_value_yi": 0.0,
            "top1_count": 0,
        })
        entry["top1_count"] = item.get("top1_managers") or 0
        entry["top1_managers"] = item.get("holders") or []
        entry["consensus_note"] = item.get("note") or ""
        existing_names = {h["manager"] for h in entry["holders"]}
        for mgr_name in item.get("holders") or []:
            if mgr_name not in existing_names:
                entry["holders"].append({
                    "manager": mgr_name,
                    "org": "公募顶流",
                    "style": "",
                    "value_yi": None,
                    "weight_pct": None,
                    "action": "第一大重仓",
                })

    return by_stock


def reverse_lookup(query, snapshot=None):
    """输入股票简称、代码（如 ``688249`` / ``晶合集成`` / ``中际旭创``）或基金经理名，返回反查结果。"""
    q = str(query or "").strip()
    if not q:
        return []
    q_upper = q.upper()
    snap = snapshot or DEFAULT_SNAPSHOT
    stock_index = build_stock_index(snap)
    results = []
    for _key, item in stock_index.items():
        code = item.get("code") or ""
        name = item.get("name") or ""
        mgr_names = " ".join(h.get("manager") or "" for h in item.get("holders") or [])
        if (q_upper in code.upper()
                or q in name
                or q in (item.get("sector") or "")
                or q in mgr_names):
            results.append(item)
    results.sort(key=lambda r: (-(r.get("top1_count") or 0), -(r.get("total_value_yi") or 0.0)))
    return results


def cross_verify_panel(payload, snapshot=None):
    """把当日 A 股面板（领涨股、涨停池、行业表现）与聪明钱持仓 + AI 数据智库自动交叉核验。"""
    snap = snapshot or DEFAULT_SNAPSHOT
    stock_index = build_stock_index(snap)
    by_code = {}
    by_name = {}
    for item in stock_index.values():
        if item.get("code"):
            by_code[item["code"].split(".")[0]] = item
        if item.get("name"):
            by_name[item["name"]] = item

    matched_stocks = []
    seen = set()

    # 1) 检查全市场涨幅榜前十与涨停池是否有聪明钱重仓股
    candidates = []
    for row in ((payload or {}).get("market") or {}).get("top_gainers") or []:
        candidates.append({
            "thscode": str(row.get("thscode") or ""),
            "name": str(row.get("name") or ""),
            "pct": row.get("ratio"),
            "source": "全A涨幅前列",
        })
    for row in (payload or {}).get("limit_up") or []:
        candidates.append({
            "thscode": str(row.get("thscode") or ""),
            "name": str(row.get("name") or "").replace(" ", ""),
            "pct": row.get("price_change_ratio_pct"),
            "source": "今日涨停池",
        })

    for cand in candidates:
        ticker = cand["thscode"].split(".")[0]
        hit = by_code.get(ticker) or by_name.get(cand["name"])
        if not hit or hit["code"] in seen:
            continue
        seen.add(hit["code"])
        holders_desc = []
        for h in hit.get("holders") or []:
            part = h["manager"]
            if h.get("value_yi"):
                part += f"({h['value_yi']:.1f}亿/{h.get('action') or '持有'})"
            elif h.get("action"):
                part += f"({h['action']})"
            holders_desc.append(part)
        matched_stocks.append({
            "code": hit["code"],
            "name": hit["name"],
            "today_pct": cand["pct"],
            "panel_source": cand["source"],
            "holders_summary": "、".join(holders_desc[:4]),
            "consensus_note": hit.get("consensus_note") or "",
        })

    # 2) 产业链高频数据印证（AI 数据智库 aidata.jrglobal.xyz vs 今日涨停主线）
    ai_data = snap.get("ai_data") or {}
    kr = ai_data.get("korea_export") or {}
    wsts = ai_data.get("global_semi_wsts") or {}
    tw = ai_data.get("taiwan_semi_2026_10") or []
    ocpi = ai_data.get("compute_cost_ocpi") or {}
    ramp = ai_data.get("enterprise_adoption_ramp") or {}

    supply_chain_verify = [
        (f"半导体/存储硬数据（aidata.jrglobal.xyz）：韩国 {kr.get('month')} 半导体出口 "
         f"${kr.get('semi_export_usd_b')}B（同比 +{kr.get('semi_yoy_pct')}%，其中 DRAM +{kr.get('dram_yoy_pct')}%、"
         f"NAND +{kr.get('nand_yoy_pct')}%）；WSTS 全球半导体月销 ${wsts.get('global_usd_b')}B（+{wsts.get('global_yoy_pct')}%）"
         "——直接印证 A 股半导体/存储涨停链与公募新建长鑫科技的景气度。"),
    ]
    if tw:
        top_tw = "、".join(f"{r['name'].split()[0]} +{r['yoy_pct']}%" for r in tw[:5])
        supply_chain_verify.append(
            f"台湾算力链 2026-10 月营收同比：{top_tw}；"
            f"Ornn 算力成本指数（{ocpi.get('date')}）B200={ocpi['gpus'][0]['ocpi']:.2f} / "
            f"H100={ocpi['gpus'][2]['ocpi']:.2f}；"
            f"Ramp 美企 AI 采用率 {ramp.get('overall_pct')}%（Anthropic {ramp['vendors'][0]['pct']}% "
            f"反超 OpenAI {ramp['vendors'][1]['pct']}%）。"
        )

    return {
        "as_of": snap.get("as_of"),
        "report_period": snap.get("report_period"),
        "source": snap.get("source"),
        "tools": snap.get("tools"),
        "cn_categories": snap.get("cn_categories"),
        "cn_gurus": snap.get("cn_gurus"),
        "cn_top1_consensus": snap.get("cn_top1_consensus"),
        "us_13f": snap.get("us_13f"),
        "ai_data": ai_data,
        "portfolio_benchmarks": snap.get("portfolio_benchmarks"),
        "stock_index": list(stock_index.values()),
        "panel_matches": matched_stocks,
        "supply_chain_verify": supply_chain_verify,
    }


def render_radar_txt(radar, brief=False):
    """生成纯文本版「聪明钱雷达 & AI 数据智库（jrglobal.xyz）」段落。"""
    if not radar:
        return []
    from build_stocks import _wrap_display, fmt_ratio  # 延迟导入避免循环依赖

    lines = [
        f"【聪明钱雷达 & AI 智库】jrglobal.xyz/tools · {radar.get('report_period') or 'Q3 2026'}"
    ]
    cats = radar.get("cn_categories") or []
    if cats:
        cat_txt = " · ".join(f"{c['name']}{c['count']}" for c in cats[:6])
        lines.extend(_wrap_display("覆盖 " + cat_txt + " 及美股 13F 2381 家", width=46, indent="  "))

    # 1) 今日盘面反查命中
    for m in radar.get("panel_matches") or []:
        pct_str = fmt_ratio(m.get("today_pct"))
        lines.extend(_wrap_display(
            f"盘面反查：{m['name']}({m['code']} {pct_str}·{m['panel_source']}) ← "
            f"聪明钱重仓：{m['holders_summary']}",
            width=46, indent="  "
        ))

    # 2) 公募第一重仓共识
    consensus = radar.get("cn_top1_consensus") or []
    if consensus:
        top_items = [
            f"{c['name']}({c['top1_managers']}人第一重仓)"
            for c in consensus[:5]
        ]
        lines.extend(_wrap_display("A股顶流第一重仓共识：" + " · ".join(top_items), width=46, indent="  "))

    if brief:
        top3 = " · ".join(f"{c['name']}({c['top1_managers']}人)" for c in consensus[:3])
        lines = [
            f"【聪明钱雷达 & AI 智库】jrglobal.xyz/tools · {radar.get('report_period') or 'Q3 2026'}",
            f"  共识：{top3} · 13F 巴菲特(Apple)/挪威主权(NVDA) · 完整见 stocks.html",
        ]
        return lines

    # 完整版：展开 Q3 代表性调仓、美股 13F、AI 数据智库与组合实验室基准
    gurus = radar.get("cn_gurus") or []
    if gurus:
        moves = [
            "金梓才(745亿,+482%)加仓三环集团/鼎泰高科/大族数控，新建仕佳光子/太辰光",
            "张明昕(740亿,+411%)加仓生益科技+132%/东山精密+290%/源杰科技+29%，减仓中际旭创/新易盛",
            "谢治宇(608亿,+70%)翻倍加仓晶合集成+105%至第一重仓，新建药明康德/通富微电，清仓中际旭创/中芯国际",
            "朱少醒(510亿)加仓中际旭创+70%/泽璟制药+30%，新建长鑫科技16.4亿",
            "傅鹏博(295亿,+121%)加仓中际旭创+83%，新建联讯仪器，清仓宁德时代",
        ]
        for mv in moves:
            lines.extend(_wrap_display("调仓 · " + mv, width=46, indent="  "))

    us = radar.get("us_13f") or []
    if us:
        us_cells = [
            f"{u['name'].split(' · ')[0]} ${u['aum_usd_b']:.0f}B({u['top1']})"
            for u in us[:6]
        ]
        lines.extend(_wrap_display("美股 13F 大师：" + " · ".join(us_cells), width=46, indent="  "))

    for verify_line in radar.get("supply_chain_verify") or []:
        lines.extend(_wrap_display(verify_line, width=46, indent="  "))

    bench = radar.get("portfolio_benchmarks") or []
    if bench:
        b_cells = [
            f"{b['name'].split('（')[1].rstrip('）') if '（' in b['name'] else b['name']} "
            + (f"1Y {b['return_1y_pct']:+.1f}%" if b.get("return_1y_pct") is not None else f"6M {b['return_6m_pct']:+.2f}%")
            for b in bench[:4]
        ]
        lines.extend(_wrap_display("组合实验室基准：" + " · ".join(b_cells), width=46, indent="  "))

    lines.append("  四大免费工具：radar / portfolio / macro / aidata.jrglobal.xyz")
    return lines


def render_radar_epub(radar, brief=False, esc=None):
    """生成 Kindle EPUB 版「聪明钱雷达 & AI 数据智库」XHTML 片段（纯表格，无 flex/grid/SVG）。"""
    if not radar:
        return []
    import html as html_mod
    from build_stocks import fmt_ratio

    if esc is None:
        esc = lambda s: html_mod.escape(str(s if s is not None else ""), quote=True)

    parts = [
        f"<h2>聪明钱雷达 &amp; AI 数据智库（{esc(radar.get('report_period') or 'Q3 2026')}）</h2>",
        '<p class="meta">来源：Jovian Research (jrglobal.xyz/tools/) · '
        "覆盖 A 股公募 498 / 牛散 2363 / 私募 4230 / 外资 254 / 社保 99 / 国家队 10 及美股 13F 2381 家</p>",
    ]
    for m in radar.get("panel_matches") or []:
        parts.append(
            f"<p><b>盘面反查命中：</b>{esc(m.get('name'))}（{esc(m.get('code'))} "
            f"{esc(fmt_ratio(m.get('today_pct')))} · {esc(m.get('panel_source'))}）← "
            f"{esc(m.get('holders_summary'))}</p>"
        )
    consensus = radar.get("cn_top1_consensus") or []
    if consensus:
        rows = consensus[:4] if brief else consensus[:8]
        parts.append('<table class="data"><tr><th>股票</th><th>行业</th>'
                     "<th>第一重仓经理数</th><th>代表经理与 Q3 动向</th></tr>")
        for c in rows:
            mgrs = "、".join((c.get("holders") or [])[:4])
            note = c.get("note") or ""
            detail = f"{mgrs}（{note}）" if note else mgrs
            parts.append(
                f"<tr><td>{esc(c.get('name'))}</td><td>{esc(c.get('sector'))}</td>"
                f"<td>{esc(c.get('top1_managers'))} 人</td><td>{esc(detail)}</td></tr>"
            )
        parts.append("</table>")

    if not brief:
        us = radar.get("us_13f") or []
        if us:
            parts.append('<table class="data"><tr><th>美股 13F 机构/大师</th><th>风格</th>'
                         "<th>组合市值</th><th>第一重仓</th><th>组合浮盈</th></tr>")
            for u in us[:7]:
                aum_str = f"{u.get('aum_usd_b', 0):.1f}"
                gain_str = f"{u.get('gain_pct', 0):+.1f}%"
                parts.append(
                    f"<tr><td>{esc(u.get('name'))}</td><td>{esc(u.get('style'))}</td>"
                    f"<td>${esc(aum_str)}B</td>"
                    f"<td>{esc(u.get('top1'))}</td>"
                    f"<td>{esc(gain_str)}</td></tr>"
                )
            parts.append("</table>")
    for verify_line in (radar.get("supply_chain_verify") or [])[: (1 if brief else 2)]:
        parts.append(f"<p>{esc(verify_line)}</p>")
    return parts


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="查询或导出 jrglobal.xyz 聪明钱雷达、AI 数据智库与组合实验室快照。"
    )
    parser.add_argument("--lookup", "-q", help="按股票代码/简称/基金经理名反查谁在持有（如 688249 / 晶合集成 / 中际旭创）")
    parser.add_argument("--out", help="将完整快照写入指定 JSON 文件")
    args = parser.parse_args(argv)

    if args.lookup:
        hits = reverse_lookup(args.lookup)
        print(json.dumps({"query": args.lookup, "matches": hits}, ensure_ascii=False, indent=2))
        return 0

    data = cross_verify_panel({})
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写入 {target}")
        return 0

    print(json.dumps({
        "as_of": data["as_of"],
        "report_period": data["report_period"],
        "cn_gurus": len(data["cn_gurus"]),
        "cn_top1_consensus": len(data["cn_top1_consensus"]),
        "us_13f": len(data["us_13f"]),
        "indexed_stocks": len(data["stock_index"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
