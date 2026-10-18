from __future__ import annotations

"""Industry and macro themes for consumers of /api/v1/signals/news (PLATFORM_DESIGN §5.1).

Keyword rules, not a model: a consumer can read exactly why an item carries a theme, and a
change of rules is a new THEMES_VERSION. Titles and summaries are matched in Chinese and English;
English words match whole words only, case-insensitively. Themes may overlap.
"""

import re

# v2 (10-18): chip smuggling counts as export controls ("台湾拟将走私英伟达芯片至中国定为犯罪" had no theme).
THEMES_VERSION = "market-themes-v2"

# key: (scope, Chinese label, Chinese phrases, English words or phrases)
_RULES = {
    "export_controls": ("industry", "出口管制与芯片限制",
                        ["出口管制", "出口限制", "实体清单", "芯片禁令", "芯片出口", "技术封锁", "走私"],
                        ["export control", "export controls", "entity list", "chip ban", "export curbs", "bis rule",
                         "smuggling", "smuggle", "smuggled"]),
    "ai_capex": ("industry", "AI 资本开支",
                 ["资本开支", "资本支出", "算力投资", "数据中心投资", "算力采购"],
                 ["capex", "capital expenditure", "capital spending", "hyperscaler", "ai spending", "ai infrastructure"]),
    "capacity_supply": ("industry", "产能与供应链",
                        ["产能", "扩产", "晶圆厂", "代工", "先进封装", "良率", "供应链", "缺货", "交期"],
                        ["production capacity", "chip capacity", "capacity expansion", "foundry", "fab", "fabs",
                         "wafer", "wafers", "cowos", "advanced packaging", "supply chain", "chip shortage",
                         "lead times"]),
    "memory": ("industry", "存储芯片",
               ["存储芯片", "内存", "闪存", "存储器"],
               ["hbm", "hbm3", "hbm3e", "hbm4", "hbm4e", "dram", "nand", "memory chip", "memory chips",
                "memory prices", "flash memory"]),
    "equipment_materials": ("industry", "半导体设备与材料",
                            ["光刻机", "光刻", "刻蚀", "半导体设备", "晶圆设备", "硅片", "半导体材料"],
                            ["lithography", "euv", "etch", "chip equipment", "chipmaking equipment",
                             "semiconductor equipment", "wafer fab equipment"]),
    "datacenter_power": ("industry", "数据中心与电力",
                         ["数据中心", "智算中心", "液冷", "电力供应", "电网", "算力中心"],
                         ["data center", "data centers", "datacenter", "power grid", "liquid cooling",
                          "power demand"]),
    "end_demand": ("industry", "终端需求",
                   ["出货量", "手机出货", "PC 出货", "PC出货", "需求疲软", "需求回暖", "换机"],
                   ["shipments", "smartphone sales", "pc sales", "pc shipments", "consumer demand"]),
    "industry_data": ("industry", "行业数据",
                      ["半导体销售额", "芯片销售额", "费城半导体", "半导体指数", "晶圆代工营收"],
                      ["semiconductor industry association", "wsts", "semiconductor sales", "chip sales",
                       "philadelphia semiconductor", "sox index"]),
    "rates": ("macro", "货币政策与利率",
              ["美联储", "加息", "降息", "利率决议", "联邦基金利率", "鲍威尔", "议息", "央行"],
              ["federal reserve", "fed", "fomc", "rate cut", "rate cuts", "rate hike", "interest rates", "powell"]),
    "inflation_jobs": ("macro", "通胀与就业",
                       ["通胀", "非农", "就业数据", "失业率", "初请失业金"],
                       ["cpi", "pce", "inflation", "nonfarm", "payrolls", "jobs report", "unemployment rate"]),
    "markets": ("macro", "大盘与指数",
                ["标普500", "标普 500", "纳斯达克指数", "纳指", "道指", "道琼斯", "美股收盘", "美股三大指数"],
                ["s&p 500", "nasdaq composite", "dow jones", "stocks close", "us stocks"]),
    "bonds_fx": ("macro", "国债与汇率",
                 ["美债", "国债收益率", "美元指数", "汇率", "人民币汇率"],
                 ["treasury yield", "treasury yields", "bond yields", "dollar index", "us dollar"]),
    "trade_geopolitics": ("macro", "贸易与地缘",
                          ["关税", "贸易战", "贸易协议", "制裁", "地缘", "军事冲突", "停火"],
                          ["tariff", "tariffs", "trade war", "trade deal", "sanctions", "geopolitical"]),
}

# These themes count only next to a chip or AI word: "玻利维亚放松黄金出口管制" is not about chips.
_NEEDS_CHIP_CONTEXT = {"export_controls", "capacity_supply", "end_demand"}
_CHIP_CONTEXT = (["芯片", "半导体", "晶圆", "算力", "存储", "服务器", "人工智能", "电子", "台积电", "英伟达",
                  "中芯", "海力士", "美光", "三星电子"],
                 ["chip", "chips", "semiconductor", "semiconductors", "wafer", "gpu", "gpus", "ai", "server",
                  "servers", "memory", "electronics", "nvidia", "tsmc"])

THEMES = {key: {"scope": scope, "label": label} for key, (scope, label, _, _) in _RULES.items()}
SCOPES = ("industry", "macro")


def _compile(chinese: list[str], english: list[str]) -> re.Pattern:
    parts = [re.escape(phrase) for phrase in chinese]
    # Whole words: "Asia" is not "SIA", and "fed-up" is not the Fed.
    parts += [r"(?<![a-z0-9-])" + re.escape(phrase) + r"(?![a-z0-9-])" for phrase in english]
    return re.compile("|".join(parts), re.IGNORECASE)


_PATTERNS = {key: _compile(chinese, english) for key, (_, _, chinese, english) in _RULES.items()}
_CONTEXT = _compile(*_CHIP_CONTEXT)


def classify(*texts: str | None) -> list[str]:
    """Theme keys whose rules match any of the texts, in the order of THEMES."""
    text = "\n".join(value for value in texts if value)
    chips = bool(_CONTEXT.search(text))
    return [key for key, pattern in _PATTERNS.items()
            if pattern.search(text) and (chips or key not in _NEEDS_CHIP_CONTEXT)]


def scope_of(themes: list[str]) -> list[str]:
    return sorted({THEMES[key]["scope"] for key in themes})
