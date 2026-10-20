from __future__ import annotations

import re

from ..utils import mask_pii, normalize_text
from .schema import SFTRecord

_ASSET_ALIASES = {
    "股票": "equity", "权益": "equity", "equity": "equity", "stock": "equity",
    "基金": "fund", "fund": "fund", "公募": "fund",
    "保险": "insurance", "insurance": "insurance", "保险产品": "insurance",
    "债券": "bond", "固收": "bond", "bond": "bond", "fixed income": "bond",
    "银行理财": "bank_wealth", "理财": "bank_wealth", "bank_wealth": "bank_wealth",
    "宏观": "macro", "宏观经济": "macro", "研报": "macro", "macro": "macro",
}

_INTENT_KEYWORDS = {
    "product_consultation": (
        "产品", "基金", "保险", "债券", "理财", "费率", "保障", "条款", "买哪",
    ),
    "market_analysis": (
        "市场", "行情", "板块", "行业", "估值", "趋势", "宏观", "利率",
    ),
    "investment_strategy": (
        "配置", "资产", "组合", "规划", "推荐", "策略", "仓位", "养老",
    ),
    "risk_assessment": (
        "风险", "回撤", "波动", "适当性", "风险等级", "承受能力",
    ),
    "claim": ("理赔", "赔付", "报销", "等待期", "免赔额", "免责"),
    "compliance": ("合规", "监管", "返佣", "误导", "销售", "披露"),
}


def normalize_record(record: SFTRecord) -> SFTRecord:
    record.instruction = _clean(record.instruction)
    record.input = _clean(record.input)
    record.output = _clean(record.output)
    record.asset_class = normalize_asset_class(record.asset_class, record.instruction)
    record.intent = normalize_intent(record.intent, record.instruction, record.output)
    record.risk_level = normalize_risk_level(record.risk_level, record.output)
    record.source_id = record.source_id or record.id
    record.language = record.language or "zh"
    return record


def _clean(text: str) -> str:
    return mask_pii(normalize_text(text))


def normalize_asset_class(value: str, instruction: str = "") -> str:
    key = value.strip().lower()
    if key in _ASSET_ALIASES:
        return _ASSET_ALIASES[key]
    haystack = f"{value} {instruction}".lower()
    for alias, asset_class in _ASSET_ALIASES.items():
        if alias.lower() in haystack:
            return asset_class
    return "macro"


def normalize_intent(value: str, instruction: str, output: str = "") -> str:
    key = value.strip().lower()
    if key in _INTENT_KEYWORDS or key == "other":
        return key
    haystack = f"{instruction} {output}"
    best = "other"
    best_hits = 0
    for intent, keywords in _INTENT_KEYWORDS.items():
        hits = sum(1 for keyword in keywords if keyword in haystack)
        if hits > best_hits:
            best, best_hits = intent, hits
    return best


def normalize_risk_level(value: str, output: str = "") -> str:
    matches = re.findall(r"\b([CR][1-5])\b", f"{value} {output}", flags=re.I)
    if not matches:
        return "C3"
    # 用户画像优先使用 C；若只有产品等级 R，则保留 R 便于后续适当性检查。
    for match in matches:
        if match.upper().startswith("C"):
            return match.upper()
    return matches[0].upper()


def canonical_text(record: SFTRecord) -> str:
    return re.sub(r"\s+", "", f"{record.instruction}{record.input}{record.output}").lower()
