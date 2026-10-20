from __future__ import annotations

import re

from .schema import SFTRecord

DISCLAIMER = (
    "以上内容基于公开资料整理，不构成投资建议或销售承诺；"
    "产品过往业绩不代表未来表现，投资有风险，请结合自身风险承受能力审慎决策。"
)
INSURANCE_DISCLAIMER = (
    "以上内容仅供参考，具体保险责任、免责事项与理赔结论以保险合同及保险公司审核结果为准。"
)

_FORBIDDEN = [
    (re.compile(r"保证(?:收益|回报|本金|赔付)|承诺(?:收益|回报|赔付)|稳赚|必赚|包赚"), "收益/赔付承诺"),
    (re.compile(r"保本保息|保本|零风险|无风险|刚性兑付"), "保本保收益表述"),
    (re.compile(r"百分之百(?:赔付|报销|承保)|100%\s*(?:赔付|报销|承保)|一定赔|肯定赔"), "绝对化赔付"),
    (re.compile(r"内幕消息|老鼠仓|操纵市场|返佣|返点"), "违规销售/市场行为"),
]
_NEGATION = re.compile(r"不得|不能|禁止|不可以|严禁|避免|不要|并非|不是|不属于|不承诺|不代表")
_PERFORMANCE = re.compile(r"年化|收益率|回撤|涨幅|业绩|净值|PE|PB|百分[之]?[一二三四五六七八九十0-9]+")
_RISK_WORDS = re.compile(r"(?:R[1-5]).{0,20}(?:C[1-5])|(?:C[1-5]).{0,20}(?:R[1-5])", re.I)
_RECOMMEND = re.compile(r"推荐|适合|可以买|建议配置|优先配置|选择")


class DataComplianceFilter:
    def __init__(self, strict: bool = False) -> None:
        self.strict = strict

    def apply(self, record: SFTRecord) -> tuple[SFTRecord | None, dict]:
        report = self.inspect(record)
        if report["violations"]:
            return None, report
        if self.strict and report["warnings"]:
            return None, report
        original_output = record.output
        updated = self.ensure_disclaimer(record)
        report["modified"] = updated.output != original_output
        if report["modified"]:
            updated.metadata["disclaimer_added"] = True
        return updated, report

    def inspect(self, record: SFTRecord) -> dict:
        text = record.output
        violations: list[str] = []
        warnings: list[str] = []
        for pattern, label in _FORBIDDEN:
            for match in pattern.finditer(text):
                window = text[max(0, match.start() - 160) : match.start()] + text[
                    match.end() : match.end() + 20
                ]
                if _NEGATION.search(window):
                    continue
                violations.append(f"{label}: {match.group(0)}")

        if _PERFORMANCE.search(text) and not any(
            phrase in text
            for phrase in ("不代表未来", "不构成投资建议", "历史业绩", "过往业绩")
        ):
            warnings.append("包含收益/业绩表述但缺少“过往业绩不代表未来”或“不构成投资建议”")

        risk_pair = _RISK_WORDS.search(text)
        if risk_pair and _RECOMMEND.search(text):
            pair = risk_pair.group(0).upper()
            r_match = re.search(r"R([1-5])", pair)
            c_match = re.search(r"C([1-5])", pair)
            if r_match and c_match and int(r_match.group(1)) > int(c_match.group(1)):
                window = text[
                    max(0, risk_pair.start() - 40) : min(len(text), risk_pair.end() + 40)
                ]
                if not re.search(r"不适合|不得推荐|风险高于|超出|不匹配", window):
                    violations.append(
                        f"适当性不匹配：产品 R{r_match.group(1)} 高于用户 C{c_match.group(1)}"
                    )

        if record.asset_class in {"fund", "equity", "bond", "bank_wealth"}:
            if not any(
                phrase in text
                for phrase in ("不构成投资建议", "投资有风险", "风险提示", "不代表未来")
            ):
                warnings.append("投资类样本缺少风险提示")
        if record.asset_class == "insurance" and not any(
            phrase in text for phrase in ("以保险合同", "以合同", "条款为准", "核保")
        ):
            warnings.append("保险类样本缺少合同/核保声明")

        return {
            "violations": violations,
            "warnings": warnings,
            "allowed": not violations,
        }

    @staticmethod
    def ensure_disclaimer(record: SFTRecord) -> SFTRecord:
        text = record.output
        if record.asset_class == "insurance":
            if not any(phrase in text for phrase in ("以保险合同", "条款为准", "核保")):
                record.output = f"{text.rstrip()}\n\n{INSURANCE_DISCLAIMER}"
        else:
            if not any(
                phrase in text
                for phrase in ("不构成投资建议", "投资有风险", "风险提示", "不代表未来")
            ):
                record.output = f"{text.rstrip()}\n\n{DISCLAIMER}"
        return record
