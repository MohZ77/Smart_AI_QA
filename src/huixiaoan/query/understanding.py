from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from ..config import Config
from ..profile_policy import profile_query_hints
from ..types import IntentResult, UserProfile

if TYPE_CHECKING:
    from ..generation.llm import LLMClient

logger = logging.getLogger(__name__)

INTENT_PATTERNS: dict[str, list[str]] = {
    "claim": [
        "理赔", "赔付", "报销", "拒赔", "等待期", "免责", "免赔额", "医疗费用",
        "住院", "门诊", "重疾", "身故", "伤残", "给付",
    ],
    "risk_assessment": [
        "健康告知", "核保", "既往症", "能不能买", "可否投保", "体检", "职业",
        "风险等级", "加费", "除外", "拒保",
    ],
    "investment_strategy": [
        "保额", "保费预算", "配置", "规划", "年金", "养老", "教育金", "传承",
        "现金价值", "缴费期", "增额", "收益", "irr",
    ],
    "compliance": [
        "合规", "监管", "销售", "话术", "返佣", "适当性", "误导", "如实告知",
        "双录", "回访", "信息披露",
    ],
    "market_analysis": ["市场", "板块", "行业", "趋势", "前景", "估值", "走势", "政策", "利率", "监管新规"],
    "product_consultation": [
        "条款", "保障", "责任", "费率", "保费", "犹豫期", "宽限期", "保什么",
        "不保什么", "保险期间", "投保年龄", "等待期", "现金价值", "分红",
        "基金", "股票", "债券", "银行理财", "理财产品", "产品", "买点", "申购", "赎回",
    ],
}

_COMPARISON_RE = re.compile(r"对比|比较|区别|差异|哪个好|分别|各自|同时")
_MULTI_RE = re.compile(r"[；;]|以及|还有|另外|并且|同时|分别")
_FOLLOW_UP_RE = re.compile(r"^(那|那么|这个|它|该产品|上面|刚才|还有呢|继续)|它")
_ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百千万零〇0-9]+条")
_RISK_RE = re.compile(r"\b(?:R|C)[1-5]\b", re.I)
_STRONG_COMPLIANCE_RE = re.compile(
    r"合规|监管|返佣|返点|误导|适当性|双录|回访|承诺收益|销售话术|销售行为|信息披露|"
    r"(?:推荐|可以|能否|适合|买|配置|匹配|销售).{0,10}R[1-5].{0,4}产品|"
    r"R[1-5].{0,4}产品.{0,10}(?:推荐|可以|适合|买|匹配)"
)
_RISK_DEFINITION_RE = re.compile(
    r"(?:R[1-5]|C[1-5]|风险等级).{0,12}(?:是什么|是什么意思|含义|定义|解释)|"
    r"(?:什么是|解释).{0,8}[RC][1-5]",
    re.I,
)
_REGULATION_REF_RE = re.compile(
    r"(?:管理办法|办法|规定|条例|指引|准则|细则|通知|决定|公告|法|监管|规章)"
    r"\s*(?:的|中|对|要求|规定|第|$|[？?])"
)

_SYNONYM_EXPANSIONS = [
    (("返佣", "返点"), "合同约定以外的利益 违规利益 销售行为"),
    (("承诺收益", "保证收益"), "禁止承诺收益 不得承诺收益 销售误导"),
    (("适当性", "风险承受能力", "R4产品", "R5产品"), "适当性匹配 特别提示 不得主动推荐 风险等级"),
    (("等待期",), "等待期 观察期 免责"),
    (("免赔额",), "免赔额 起付线 自付"),
]


@dataclass
class QueryPlan:
    question: str
    intent: IntentResult
    strategy: str
    queries: list[str]
    rationale: str = ""
    entities: dict[str, Any] = field(default_factory=dict)
    profile_hints: list[str] = field(default_factory=list)


class QueryUnderstanding:
    def __init__(self, cfg: Config, llm: "LLMClient | None" = None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.use_llm_intent = bool(cfg.get("query.intent_llm", False))

    def analyze(self, question: str, history: list[dict[str, str]] | None = None) -> IntentResult:
        # 监管文件标题经常包含“基金/保险”等产品词，必须先按法规引用识别，
        # 否则会被错误路由到基金产品库，导致明明已入库却检索不到。
        if _REGULATION_REF_RE.search(question):
            return IntentResult(
                name="compliance",
                confidence=0.9,
                rationale="regulation_reference",
            )
        if _RISK_DEFINITION_RE.search(question):
            risk = _RISK_RE.search(question)
            return IntentResult(
                name="risk_assessment",
                confidence=0.95,
                entities={"risk_level": risk.group(0).upper()} if risk else {},
                rationale="risk_level_definition",
            )
        if _STRONG_COMPLIANCE_RE.search(question):
            return IntentResult(
                name="compliance",
                confidence=0.92,
                rationale="strong_compliance_pattern",
            )
        scores: dict[str, int] = {}
        for intent, keywords in INTENT_PATTERNS.items():
            scores[intent] = sum(1 for keyword in keywords if keyword in question)
        best_intent = max(scores, key=scores.get) if scores else "other"
        best_score = scores.get(best_intent, 0)
        total_hits = sum(scores.values())
        confidence = min(0.95, 0.45 + 0.15 * best_score) if best_score else 0.3

        if self.use_llm_intent and self.llm is not None and confidence < 0.6:
            llm_result = self._llm_intent(question, history or [])
            if llm_result:
                return llm_result

        entities: dict[str, Any] = {}
        article = _ARTICLE_RE.search(question)
        if article:
            entities["article"] = article.group(0)
        risk = _RISK_RE.search(question)
        if risk:
            entities["risk_level"] = risk.group(0).upper()
        return IntentResult(
            name=best_intent if best_score else "other",
            confidence=round(confidence, 3),
            entities=entities,
            rationale=f"rule_hits={total_hits}",
        )

    def _llm_intent(
        self, question: str, history: list[dict[str, str]]
    ) -> IntentResult | None:
        labels = ", ".join(INTENT_PATTERNS.keys())
        prompt = (
            "判断金融理财顾问问题的意图，只能从以下标签中选择一个："
            f"{labels}, other。只输出 JSON："
            '{"intent":"...","confidence":0.0,"entities":{}}。\n'
            f"历史：{json.dumps(history[-4:], ensure_ascii=False)}\n问题：{question}"
        )
        try:
            raw = self.llm.complete(
                [{"role": "system", "content": "你是意图分类器，只输出 JSON。"},
                 {"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=128,
            )
            payload = json.loads(_extract_json(raw))
            name = str(payload.get("intent", "other"))
            if name not in INTENT_PATTERNS and name != "other":
                name = "other"
            return IntentResult(
                name=name,
                confidence=float(payload.get("confidence", 0.6)),
                entities=payload.get("entities") or {},
                rationale="llm",
            )
        except Exception as exc:
            logger.warning("LLM 意图分类失败，回退规则: %s", exc)
            return None


class QueryPlanner:
    """动态路由：Direct / HyDE / SubQuery / Backtrack。"""

    def __init__(self, cfg: Config, llm: "LLMClient | None" = None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.understanding = QueryUnderstanding(cfg, llm)
        self.max_subqueries = int(cfg.get("query.subquery_max", 4))

    def plan(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
        profile: UserProfile | None = None,
    ) -> QueryPlan:
        history = history or []
        intent = self.understanding.analyze(question, history)
        strategy, rationale = self._route(question, intent, history)

        if strategy == "backtrack":
            queries = [self._backtrack(question, history)]
        elif strategy == "subquery":
            queries = self._subqueries(question)
        elif strategy == "hyde":
            queries = [question, self._hyde(question, intent)]
        else:
            queries = [question]

        # 条款号/产品名等硬约束始终保留原始问题，避免改写丢失精确信息。
        if strategy != "subquery" and queries[0] != question:
            queries.insert(0, question)
        expansion = self._expand_synonyms(question)
        if expansion:
            queries.append(expansion)
        profile_hints = (
            profile_query_hints(question, profile, intent.name)
            if profile is not None
            else []
        )
        # SubQuery 已经有多个语义明确的子查询，再追加画像词容易稀释召回；
        # 画像在 SubQuery 场景主要通过风险过滤和 Prompt 生效。
        if profile_hints and strategy != "subquery":
            queries.append(f"{question} {' '.join(profile_hints)}")
        return QueryPlan(
            question=question,
            intent=intent,
            strategy=strategy,
            queries=_dedupe(queries)[: self.max_subqueries + 2],
            rationale=rationale,
            entities=intent.entities,
            profile_hints=profile_hints,
        )

    @staticmethod
    def _expand_synonyms(question: str) -> str:
        additions = []
        for triggers, expansion in _SYNONYM_EXPANSIONS:
            if any(trigger in question for trigger in triggers):
                additions.append(expansion)
        return "；".join(additions)

    def _route(
        self, question: str, intent: IntentResult, history: list[dict[str, str]]
    ) -> tuple[str, str]:
        if intent.name == "compliance":
            return "direct", "合规问题需要最高确定性，禁用生成式改写"
        if history and _FOLLOW_UP_RE.search(question) and len(question) < 40:
            return "backtrack", "检测到指代/追问，需要拼接最近对话"
        product_terms = ("基金", "保险", "债券", "银行理财", "股票", "理财产品")
        market_terms = ("板块", "行业", "市场", "趋势", "前景", "估值", "走势", "利率", "下调")
        if (
            any(term in question for term in product_terms)
            and any(term in question for term in market_terms)
        ):
            return "subquery", "产品咨询 + 市场分析复合意图，拆分并行检索"
        if _COMPARISON_RE.search(question) or (
            _MULTI_RE.search(question) and len(question) > 30
        ):
            return "subquery", "复合问题，拆分子问题分别召回"
        if len(question.strip()) <= 8 or any(
            token in question for token in ("怎么样", "可靠吗", "划算吗", "好不好")
        ):
            return "hyde", "问题过短/表述模糊，用假设性条款文本增强召回"
        return "direct", "事实型问题，直接混合检索"

    def _backtrack(self, question: str, history: list[dict[str, str]]) -> str:
        recent_user = [m["content"] for m in history if m.get("role") == "user"]
        if not recent_user:
            return question
        return f"{recent_user[-1]}；继续问：{question}"

    def _subqueries(self, question: str) -> list[str]:
        if self.llm is not None:
            prompt = (
                "把用户的复合保险问题拆成 2-4 个可独立检索的子问题，保留条款号、"
                "产品名和时间等约束。只输出 JSON 数组，不要解释。\n"
                f"问题：{question}"
            )
            try:
                raw = self.llm.complete(
                    [{"role": "system", "content": "只输出 JSON 数组。"},
                     {"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=256,
                )
                items = json.loads(_extract_json_array(raw))
                if isinstance(items, list):
                    cleaned = [str(item).strip() for item in items if str(item).strip()]
                    if cleaned:
                        return cleaned[: self.max_subqueries]
            except Exception as exc:
                logger.warning("SubQuery 分解失败，回退规则: %s", exc)

        parts = re.split(r"[；;。，,]|以及|还有|另外|并且|同时|分别|、", question)
        parts = [part.strip(" ，,") for part in parts if len(part.strip()) >= 4]
        if len(parts) <= 1:
            return [question]
        return parts[: self.max_subqueries]

    def _hyde(self, question: str, intent: IntentResult) -> str:
        if self.llm is not None:
            prompt = (
                "请写一段 120 字以内的、可能出现在保险条款中的规范性表述，"
                "用于向量检索，不要给出结论或建议，不要编造具体数字。\n"
                f"问题：{question}\n意图：{intent.name}"
            )
            try:
                text = self.llm.complete(
                    [{"role": "system", "content": "你是保险条款检索助手。"},
                     {"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=180,
                )
                if text.strip():
                    return text.strip()
            except Exception as exc:
                logger.warning("HyDE 生成失败，回退模板: %s", exc)
        return f"保险条款中关于“{question}”的保障责任、免责情形与理赔条件规定如下。"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _extract_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start >= 0 and end > start else "{}"


def _extract_json_array(text: str) -> str:
    start = text.find("[")
    end = text.rfind("]")
    return text[start : end + 1] if start >= 0 and end > start else "[]"
