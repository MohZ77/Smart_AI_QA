from __future__ import annotations

import json
import logging
import re
from typing import Any, TYPE_CHECKING

from ..config import Config
from ..types import Citation, ComplianceResult, IntentResult, UserProfile

if TYPE_CHECKING:
    from ..generation.llm import LLMClient

logger = logging.getLogger(__name__)

_FORBIDDEN_RULES = [
    (re.compile(r"保证(?:收益|回报|本金|赔付)|承诺(?:收益|回报|赔付)|稳赚|必赚|包赚"), "收益/赔付承诺"),
    (re.compile(r"保本保息|保本|零风险|无风险|绝对安全|刚性兑付"), "保本保收益表述"),
    (re.compile(r"百分之百(?:赔付|报销|承保)|100%\s*(?:赔付|报销|承保)|一定赔|肯定赔"), "绝对化赔付表述"),
    (re.compile(r"无需健康告知|不用健康告知|带病投保|什么病都能赔|任何情况都赔"), "销售误导"),
    (re.compile(r"返佣|返点|私下转账|避税|洗钱"), "违规销售行为"),
    (re.compile(r"最好|顶级|全网最低|(?:行业|全国|市场|销量|排名)第一|第一品牌|第一名"), "绝对化宣传用语"),
    (re.compile(r"唯一(?:选择|品牌|官方|指定渠道)"), "绝对化宣传用语"),
]

_INPUT_RULES = [
    (re.compile(r"保证(?:赚钱|盈利|收益|回报)|稳赚|必赚|包赚|保本高收益"), "收益承诺请求"),
    (re.compile(r"内幕消息|内幕信息|老鼠仓|操纵市场"), "内幕/操纵市场请求"),
    (re.compile(r"帮我(?:洗钱|避税)|如何(?:洗钱|避税)"), "违法违规请求"),
    (re.compile(r"返佣|返点|私下给(?:我|你)钱"), "违规利益请求"),
]

_POLICY_QUESTION_RE = re.compile(r"可以|能否|是否|能不能|允许|合规|违规|禁止|规定")
_NEGATION_RE = re.compile(r"不得|不能|禁止|不可以|严禁|避免|不要|并非|不是|不属于|不承诺")

_DISCLAIMER = (
    "以上内容仅依据知识库资料整理，不构成投资建议、投保建议或销售承诺；"
    "实际产品责任、风险收益与理赔结论以产品合同及持牌机构审核结果为准。"
)

CITATION_RE = re.compile(r"\[(\d+)\]")


class ComplianceChecker:
    """两层合规审查：规则引擎（确定性）+ 可选 LLM 复核。"""

    def __init__(self, cfg: Config, llm: "LLMClient | None" = None) -> None:
        self.enabled = bool(cfg.get("compliance.enabled", True))
        self.llm_judge = bool(cfg.get("compliance.llm_judge", False))
        self.block_on_violation = bool(cfg.get("compliance.block_on_violation", True))
        self.require_citation = bool(cfg.get("compliance.require_citation", True))
        self.require_disclaimer = bool(cfg.get("compliance.require_disclaimer", True))
        self.max_risk_gap = int(cfg.get("compliance.max_risk_gap", 0))
        self.llm = llm

    def check(
        self,
        answer: str,
        citations: list[Citation],
        intent: IntentResult,
        profile: UserProfile,
        product_meta: dict[str, Any] | None = None,
    ) -> ComplianceResult:
        if not self.enabled:
            return ComplianceResult(passed=True, action="allow")

        violations: list[str] = []
        warnings: list[str] = []

        for pattern, label in _FORBIDDEN_RULES:
            match = pattern.search(answer)
            if match:
                window = (
                    answer[max(0, match.start() - 160) : match.start()]
                    + match.group(0)
                    + answer[match.end() : match.end() + 20]
                )
                if _NEGATION_RE.search(window):
                    continue
                violations.append(f"{label}: “{match.group(0)}”")

        if self.require_citation and citations and not CITATION_RE.search(answer):
            violations.append("答案缺少引用标注，无法追溯依据")

        if self.require_disclaimer and intent.name in {
            "investment_strategy", "product_consultation", "claim", "risk_assessment"
        }:
            if "不构成" not in answer and "以合同" not in answer and "以保险合同" not in answer:
                warnings.append("缺少风险/免责提示，已自动补充标准声明")

        risk_level = str((product_meta or {}).get("risk_level", "")).upper()
        product_rank = _risk_rank(risk_level)
        if product_rank and product_rank - profile.risk_rank > self.max_risk_gap:
            violations.append(
                f"适当性不匹配：产品风险等级 {risk_level} 高于用户风险承受能力 {profile.risk_level}"
            )

        action = "allow"
        if violations:
            action = "block" if self.block_on_violation else "warn"
        elif warnings:
            action = "warn"

        result = ComplianceResult(
            passed=not violations,
            violations=violations,
            warnings=warnings,
            action=action,
            details={"layer": "rules", "citation_count": len(citations)},
        )

        if self.llm_judge and self.llm is not None and result.passed:
            judge = self._llm_review(answer, citations, intent, profile)
            if judge and not judge.passed:
                return judge
        return result

    def precheck(self, question: str) -> ComplianceResult:
        """输入层合规预检：拦截明确违规请求，放过“是否允许”类合规咨询。"""
        if not self.enabled:
            return ComplianceResult(passed=True, action="allow")
        if _POLICY_QUESTION_RE.search(question):
            return ComplianceResult(
                passed=True,
                action="allow",
                details={"layer": "input", "policy_question": True},
            )
        violations = []
        for pattern, label in _INPUT_RULES:
            match = pattern.search(question)
            if match:
                violations.append(f"{label}: “{match.group(0)}”")
        return ComplianceResult(
            passed=not violations,
            violations=violations,
            action="block" if violations else "allow",
            details={"layer": "input"},
        )

    def _llm_review(
        self,
        answer: str,
        citations: list[Citation],
        intent: IntentResult,
        profile: UserProfile,
    ) -> ComplianceResult | None:
        prompt = (
            "你是保险销售合规审查员。判断下面回答是否存在收益承诺、绝对化赔付、"
            "销售误导、适当性不匹配或缺少依据。只输出 JSON："
            '{"passed": true/false, "violations": ["..."]}。\n'
            f"用户风险等级：{profile.risk_level}\n意图：{intent.name}\n"
            f"引用数：{len(citations)}\n回答：{answer}"
        )
        try:
            raw = self.llm.complete(
                [{"role": "system", "content": "只输出合法 JSON。"}, {"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=256,
            )
            payload = json.loads(_extract_json(raw))
            passed = bool(payload.get("passed", True))
            violations = [str(item) for item in payload.get("violations", [])]
            return ComplianceResult(
                passed=passed,
                violations=violations,
                action="block" if not passed and self.block_on_violation else "warn",
                details={"layer": "llm"},
            )
        except Exception as exc:  # pragma: no cover - 外部模型不稳定
            logger.warning("LLM 合规复核失败，沿用规则结果: %s", exc)
            return None

    @staticmethod
    def ensure_disclaimer(answer: str) -> str:
        if "不构成" in answer and "以合同" in answer:
            return answer
        return f"{answer.rstrip()}\n\n{_DISCLAIMER}"

    @staticmethod
    def safe_fallback(citations: list[Citation]) -> str:
        refs = "".join(f"[{c.index}]" for c in citations[:3])
        return (
            "抱歉，当前回答未通过合规校验，我不能给出确定性结论。"
            f"请以条款原文{refs}及保险公司核保/理赔结果为准；"
            "如需投保或理赔，请由持牌顾问结合您的实际情况进一步确认。\n\n"
            f"{_DISCLAIMER}"
        )


def _risk_rank(value: str) -> int:
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits:
        return int(digits)
    mapping = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
    return mapping.get(value, 0)


def _extract_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start >= 0 and end > start else "{}"
