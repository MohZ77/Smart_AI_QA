from __future__ import annotations

import re

from .compliance import DataComplianceFilter
from .schema import ASSET_CLASS_LABELS, SFTRecord

_ASSET_KEYWORDS = {
    "equity": ("股票", "权益", "估值", "PE", "板块", "股价"),
    "fund": ("基金", "净值", "回撤", "基金经理", "申购", "赎回"),
    "insurance": ("保险", "条款", "等待期", "免责", "理赔", "保额", "保费"),
    "bond": ("债券", "久期", "信用", "票息", "利率", "固收"),
    "bank_wealth": ("银行理财", "理财", "净值型", "风险等级", "期限"),
    "macro": ("宏观", "利率", "政策", "行业", "市场", "经济", "研报"),
}


def score_record(record: SFTRecord) -> float:
    score = 0.0
    if 4 <= len(record.instruction) <= 300:
        score += 12
    if 40 <= len(record.output) <= 2500:
        score += 20
    elif 20 <= len(record.output) <= 6000:
        score += 12
    if "\n" in record.output:
        score += 8
    if re.search(r"(^|\n)\s*(?:[-*•]|\d+[.、]|[①②③④⑤])", record.output):
        score += 8
    if any(keyword.lower() in record.output.lower() for keyword in _ASSET_KEYWORDS.get(record.asset_class, ())):
        score += 12
    if record.risk_level:
        score += 5
    if any(
        phrase in record.output
        for phrase in ("不构成投资建议", "投资有风险", "风险提示", "不代表未来", "以合同", "条款为准")
    ):
        score += 15
    if re.search(r"\d+(?:\.\d+)?%|第[一二三四五六七八九十0-9]+条|\[\d+\]", record.output):
        score += 8
    if "作为一个AI" in record.output or "无法提供" in record.output:
        score -= 15
    report = DataComplianceFilter().inspect(record)
    if report["violations"]:
        score = 0
    elif report["warnings"]:
        score -= 8 * len(report["warnings"])
    return round(max(0.0, min(100.0, score)), 2)


def asset_label(asset_class: str) -> str:
    return ASSET_CLASS_LABELS.get(asset_class, asset_class)
