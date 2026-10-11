from __future__ import annotations

"""Versioned relevance definitions shared by owner labeling, the silver labeler and curation.

A dataset declares exactly one definition; rewording or rescoping means a new version
(evaluation/ANNOTATION_GUIDE_V1.md). v1 is kept so datasets labeled under it stay readable.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RelevanceDefinition:
    question: str
    relevant: str
    not_relevant: str
    unknown: str


DEFINITIONS = {
    "relevance-definition-v1": RelevanceDefinition(
        question="主体是否属于科技研究范围（TMT）？",
        relevant=(
            "AI、机器人、半导体与芯片、智能硬件与消费电子、互联网平台、软件与云、智能汽车技术、电信与网络；"
            "科技公司自身的动态与公司事件（财报、申报、回购、并购、评级、内部人交易、人事）；直接针对科技行业或具体科技公司的"
            "政策、监管、资本开支与融资环境。"
        ),
        not_relevant=(
            "大盘与指数、货币政策、非科技焦点的地缘政治、医疗医药、体育娱乐、能源大宗农产品、非科技公司的一般"
            "财经新闻；科技公司只是顺带提及。"
        ),
        unknown="冻结内容不足以判断（含义不明的标题、乱码、公司名歧义）。不是“拿不准”。",
    ),
    # D26 (owner, 2026-10-11): markets and macro news count, as the owner's own labels showed.
    "relevance-definition-v2": RelevanceDefinition(
        question="主体是否属于科技（TMT）或财经与市场？",
        relevant=(
            "科技：AI、机器人、半导体与芯片、智能硬件与消费电子、互联网平台、软件与云、智能汽车技术、电信与网络，"
            "科技公司自身的动态与公司事件，以及针对科技行业的政策、监管、资本开支与融资环境。"
            "财经与市场：货币政策与央行、宏观经济数据（通胀、就业、PMI、GDP）、大盘与指数、国债与汇率、"
            "能源与大宗商品的价格和供需、贸易政策与关税、财政政策；任何公司的财报、股价异动、并购与融资；"
            "产业数据（如汽车产销）；会影响市场的地缘政治（能源供给、制裁、航运要道、主要经济体之间的冲突与谈判）。"
        ),
        not_relevant=(
            "不影响市场的政治与军事新闻（选举、武器交付、战场细节）；社会民生（客流、治安、犯罪、诈骗提示）；"
            "文娱体育与游戏；生活消费、购物促销与生活方式内容（即使出现在科技公司平台上）；医疗健康；"
            "与科技和市场无关的学术内容；只是与关注公司同名的内容（如苹果派、ARM 浮动利率房贷、Mrs. GREEN APPLE 乐队、彩票）。"
        ),
        unknown="冻结内容不足以判断（含义不明的标题、乱码、公司名歧义）。不是“拿不准”。",
    ),
}
CURRENT_DEFINITION = "relevance-definition-v2"


def definition(version: str) -> RelevanceDefinition:
    try:
        return DEFINITIONS[version]
    except KeyError:
        raise ValueError(f"unknown relevance definition {version!r}") from None
