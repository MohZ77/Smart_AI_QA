from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, AsyncIterator

from ..audit import AuditStore
from ..cache import MultiLevelCache
from ..compliance.rules import ComplianceChecker, _risk_rank
from ..config import Config
from ..indexing.faq import FAQStore
from ..memory import ConversationStore
from ..profile_policy import profile_fingerprint, profile_snapshot
from ..query.understanding import QueryPlan, QueryPlanner
from ..retrieval.hybrid import HybridRetriever, dedupe_by_section, reciprocal_rank_fusion
from ..types import AnswerResult, Citation, ComplianceResult, IntentResult, SearchHit, UserProfile
from ..utils import normalize_text, normalize_question, stable_id, truncate
from .llm import LLMClient
from .prompts import build_context, build_messages, build_strict_messages

logger = logging.getLogger(__name__)

_GREETING_RE = re.compile(
    r"^\s*(你好|您好|hi|hello|在吗|早上好|下午好|晚上好|谢谢|再见)[!！。,.，\s]*$", re.I
)
_RECOMMEND_RE = re.compile(r"推荐|适合|买哪|选哪|配置|对比|哪个好|怎么买|买点|想买|买什么")
_LATEST_RE = re.compile(r"最新|近期|当前|今年|本月|这几个月|3个月|三个月")
_CATALOG_RE = re.compile(
    r"(?:有哪些|包含哪些|收录哪些|支持哪些|有什么).{0,10}"
    r"(?:险种|保险产品|产品|基金|资产类别|文档)|"
    r"(?:险种|保险产品|知识库).{0,10}(?:有哪些|包含|收录|支持)"
)
_ATTRIBUTE_RE = re.compile(
    r"^\s*(?:请问|帮我查|想了解)?\s*([\u4e00-\u9fa5A-Za-z0-9]{2,12})的"
    r"(等待期|免责|保障|理赔|犹豫期|免赔额|风险等级|续保|保费|条款|责任)"
)
_GENERIC_PRODUCTS = {
    "医疗险", "重疾险", "年金险", "寿险", "意外险", "保险", "保险产品",
    "基金", "债券基金", "混合基金", "股票基金", "银行理财", "理财产品",
    "股票", "债券", "消费板块", "医药板块", "科技板块", "金融板块", "制造板块",
}
_CLARIFICATION_MARKER = "当前知识库没有找到足够可靠的依据"
_NO_MORE_INFO_RE = re.compile(
    r"不知道|没有了|没别的|不用问|别再问|直接回答|就这些|无法提供|没有更多"
)
_REGULATION_HINT_RE = re.compile(
    r"管理办法|办法|规定|条例|指引|准则|细则|通知|决定|公告|监管|法规|规章"
)
_INDUSTRY_KEYWORDS = {
    "消费": ["消费", "食品饮料", "家电"],
    "医药": ["医药", "医疗", "生物"],
    "科技": ["科技", "半导体", "人工智能", "算力"],
    "金融": ["金融板块", "金融行业", "券商行业"],
    "制造": ["制造", "机械", "汽车", "新能源"],
}


class Answerer:
    def __init__(
        self,
        cfg: Config,
        planner: QueryPlanner,
        retriever: HybridRetriever,
        llm: LLMClient,
        compliance: ComplianceChecker,
        cache: MultiLevelCache,
        faq: FAQStore,
        memory: ConversationStore,
        audit: AuditStore,
        kb_version: str = "v1",
        catalog: list[dict[str, Any]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.planner = planner
        self.retriever = retriever
        self.llm = llm
        self.compliance = compliance
        self.cache = cache
        self.faq = faq
        self.memory = memory
        self.audit = audit
        self.kb_version = kb_version
        self.catalog = catalog or []
        self.history_turns = int(cfg.get("query.history_turns", 5))
        self.max_context_chars = int(cfg.get("retrieval.max_context_chars", 6000))
        self.parent_expansion = bool(cfg.get("retrieval.parent_expansion", True))
        self.min_confidence = float(cfg.get("retrieval.min_confidence", 0.8))
        self.max_question_chars = int(cfg.get("app.max_question_chars", 1000))
        self.retry_on_violation = bool(cfg.get("compliance.retry_on_violation", True))
        self.research_days = int(cfg.get("freshness.research_days", 90))
        self.offline = str(cfg.get("llm.provider", "offline")).lower() == "offline"

    async def answer(
        self,
        question: str,
        profile: UserProfile | None = None,
        session_id: str = "default",
        request_id: str | None = None,
        image_context: str = "",
        visual: dict[str, Any] | None = None,
        domain: str = "all",
    ) -> AnswerResult:
        started = time.perf_counter()
        profile = profile or UserProfile()
        question = self._sanitize_question(question)
        request_id = request_id or stable_id(question, session_id, time.time(), length=24)

        if visual and not visual.get("ok", False):
            result = self._visual_failure_result(visual, request_id, started)
            await self._finish(session_id, profile, question, result, cache=False)
            return result
        precheck = self.compliance.precheck(question)
        if not precheck.passed:
            result = self._blocked_result(question, precheck, request_id, started)
            result.visual = visual or {}
            await self._finish(session_id, profile, question, result, cache=False)
            return result
        if _GREETING_RE.match(question):
            result = self._greeting_result(question, request_id, started)
            result.visual = visual or {}
            await self._finish(session_id, profile, question, result, cache=False)
            return result

        if self._is_catalog_question(question):
            result = self._catalog_result(question, request_id, started)
            result.visual = visual or {}
            await self._finish(session_id, profile, question, result, cache=False)
            return result

        missing_product = self._missing_product(question)
        if missing_product:
            result = self._missing_product_result(missing_product, request_id, started)
            result.visual = visual or {}
            await self._finish(session_id, profile, question, result, cache=False)
            return result

        cached = self._cache_lookup(question, profile, image_context, domain)
        if cached is not None:
            cached.request_id = request_id
            cached.latency_ms = (time.perf_counter() - started) * 1000
            cached.visual = visual or {}
            await self._remember(session_id, profile, question, cached.answer)
            self._audit(session_id, profile, question, cached)
            return cached

        try:
            faq_hit = self.faq.lookup(question)
        except Exception as exc:
            logger.warning("FAQ 检索不可用，继续走 RAG: %s", exc)
            faq_hit = None
        if faq_hit:
            result = self._faq_result(question, faq_hit, profile, request_id, started)
            result.visual = visual or {}
            await self._finish(
                session_id,
                profile,
                question,
                result,
                cache=True,
                image_context=image_context,
                domain=domain,
            )
            return result

        try:
            history = self.memory.recent(session_id, self.history_turns)
        except Exception as exc:
            logger.warning("会话记忆不可用，忽略历史: %s", exc)
            history = []
        plan = self.planner.plan(question, history, profile)
        if image_context:
            plan.queries.append(image_context[:1200])
        hits, confidence, debug = self._retrieve(plan, profile, domain)

        if not hits or confidence < self.min_confidence:
            if self.offline:
                result = self._refusal_result(
                    question,
                    plan,
                    hits,
                    confidence,
                    debug,
                    request_id,
                    started,
                    history,
                )
            else:
                result = await self._llm_fallback_result(
                    question,
                    plan,
                    hits,
                    confidence,
                    debug,
                    profile,
                    history,
                    request_id,
                    started,
                )
            result.visual = visual or {}
            await self._finish(
                session_id, profile, question, result, cache=False, image_context=image_context
            )
            return result

        context, citations = build_context(
            hits, self.max_context_chars, self.parent_expansion
        )
        messages = build_messages(
            question, context, citations, profile, history, image_context=image_context
        )
        answer = await self.llm.acomplete(
            messages,
            temperature=float(self.cfg.get("llm.temperature", 0.1)),
            max_tokens=int(self.cfg.get("llm.max_tokens", 900)),
        )
        answer = self._ensure_citation(answer, citations)

        product_meta = self._product_meta(hits)
        compliance = self.compliance.check(answer, citations, plan.intent, profile, product_meta)
        if not compliance.passed and self.retry_on_violation:
            strict_messages = build_strict_messages(messages, answer)
            answer = await self.llm.acomplete(
                strict_messages,
                temperature=0.0,
                max_tokens=int(self.cfg.get("llm.max_tokens", 900)),
            )
            answer = self._ensure_citation(answer, citations)
            compliance = self.compliance.check(
                answer, citations, plan.intent, profile, product_meta
            )
        if not compliance.passed and compliance.action == "block":
            answer = ComplianceChecker.safe_fallback(citations)
        elif (not compliance.passed or compliance.action == "warn") and self.compliance.require_disclaimer:
            answer = ComplianceChecker.ensure_disclaimer(answer)
        answer = self._append_images(answer, citations)

        result = AnswerResult(
            answer=answer,
            citations=citations,
            confidence=confidence,
            intent=plan.intent,
            strategy=plan.strategy,
            cached=False,
            compliance=compliance,
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
            model=self.llm.model_name,
            visual=visual or {},
            debug={
                "plan_rationale": plan.rationale,
                "queries": plan.queries,
                "profile_hints": plan.profile_hints,
                "profile": profile_snapshot(profile),
                "retrieval": debug,
            },
        )
        await self._finish(
            session_id,
            profile,
            question,
            result,
            cache=True,
            image_context=image_context,
            domain=domain,
        )
        return result

    async def stream_answer(
        self,
        question: str,
        profile: UserProfile | None = None,
        session_id: str = "default",
        request_id: str | None = None,
        image_context: str = "",
        visual: dict[str, Any] | None = None,
        domain: str = "all",
    ) -> AsyncIterator[dict[str, Any]]:
        """WebSocket/SSE 事件流：meta -> delta* -> done / replace。"""
        started = time.perf_counter()
        profile = profile or UserProfile()
        question = self._sanitize_question(question)
        request_id = request_id or stable_id(question, session_id, time.time(), length=24)

        if visual and not visual.get("ok", False):
            result = self._visual_failure_result(visual, request_id, started)
            yield {
                "type": "meta",
                "request_id": request_id,
                "cached": False,
                "intent": result.intent.name,
                "strategy": result.strategy,
                "visual": visual,
            }
            yield {"type": "delta", "content": result.answer}
            yield {"type": "done", **result.to_dict()}
            await self._finish(session_id, profile, question, result, cache=False)
            return
        precheck = self.compliance.precheck(question)
        if not precheck.passed:
            result = self._blocked_result(question, precheck, request_id, started)
            yield {
                "type": "meta",
                "request_id": request_id,
                "cached": False,
                "intent": "compliance",
                "strategy": "precheck",
            }
            yield {"type": "delta", "content": result.answer}
            yield {"type": "done", **result.to_dict()}
            await self._finish(session_id, profile, question, result, cache=False)
            return
        if _GREETING_RE.match(question):
            result = self._greeting_result(question, request_id, started)
            yield {
                "type": "meta",
                "request_id": request_id,
                "cached": False,
                "intent": result.intent.name,
                "strategy": result.strategy,
            }
            yield {"type": "delta", "content": result.answer}
            yield {"type": "done", **result.to_dict()}
            await self._finish(session_id, profile, question, result, cache=False)
            return

        if self._is_catalog_question(question):
            result = self._catalog_result(question, request_id, started)
            yield {
                "type": "meta",
                "request_id": request_id,
                "cached": False,
                "intent": result.intent.name,
                "strategy": result.strategy,
            }
            yield {"type": "delta", "content": result.answer}
            yield {"type": "done", **result.to_dict()}
            await self._finish(session_id, profile, question, result, cache=False)
            return

        missing_product = self._missing_product(question)
        if missing_product:
            result = self._missing_product_result(missing_product, request_id, started)
            yield {
                "type": "meta",
                "request_id": request_id,
                "cached": False,
                "intent": result.intent.name,
                "strategy": result.strategy,
            }
            yield {"type": "delta", "content": result.answer}
            yield {"type": "done", **result.to_dict()}
            await self._finish(session_id, profile, question, result, cache=False)
            return

        cached = self._cache_lookup(question, profile, image_context, domain)
        if cached is not None:
            cached.request_id = request_id
            cached.latency_ms = (time.perf_counter() - started) * 1000
            yield {
                "type": "meta",
                "request_id": request_id,
                "cached": True,
                "cache_level": cached.cache_level,
                "intent": cached.intent.name,
                "strategy": cached.strategy,
            }
            yield {"type": "delta", "content": cached.answer}
            yield {"type": "done", **cached.to_dict()}
            await self._remember(session_id, profile, question, cached.answer)
            self._audit(session_id, profile, question, cached)
            return

        try:
            faq_hit = self.faq.lookup(question)
        except Exception as exc:
            logger.warning("FAQ 检索不可用，继续走 RAG: %s", exc)
            faq_hit = None
        if faq_hit:
            result = self._faq_result(question, faq_hit, profile, request_id, started)
            result.visual = visual or {}
            yield {
                "type": "meta",
                "request_id": request_id,
                "cached": True,
                "cache_level": "faq",
                "intent": result.intent.name,
                "strategy": result.strategy,
            }
            yield {"type": "delta", "content": result.answer}
            yield {"type": "done", **result.to_dict()}
            await self._finish(
                session_id,
                profile,
                question,
                result,
                cache=True,
                image_context=image_context,
                domain=domain,
            )
            return

        try:
            history = self.memory.recent(session_id, self.history_turns)
        except Exception as exc:
            logger.warning("会话记忆不可用，忽略历史: %s", exc)
            history = []
        plan = self.planner.plan(question, history, profile)
        if image_context:
            plan.queries.append(image_context[:1200])
        hits, confidence, debug = self._retrieve(plan, profile, domain)
        yield {
            "type": "meta",
            "request_id": request_id,
            "cached": False,
            "intent": plan.intent.name,
            "strategy": plan.strategy,
            "confidence": confidence,
        }

        if not hits or confidence < self.min_confidence:
            if self.offline:
                result = self._refusal_result(
                    question,
                    plan,
                    hits,
                    confidence,
                    debug,
                    request_id,
                    started,
                    history,
                )
            else:
                result = await self._llm_fallback_result(
                    question,
                    plan,
                    hits,
                    confidence,
                    debug,
                    profile,
                    history,
                    request_id,
                    started,
                )
            result.visual = visual or {}
            yield {"type": "delta", "content": result.answer}
            yield {"type": "done", **result.to_dict()}
            await self._finish(session_id, profile, question, result, cache=False)
            return

        context, citations = build_context(
            hits, self.max_context_chars, self.parent_expansion
        )
        messages = build_messages(
            question, context, citations, profile, history, image_context=image_context
        )
        chunks: list[str] = []
        async for token in self.llm.astream(
            messages,
            temperature=float(self.cfg.get("llm.temperature", 0.1)),
            max_tokens=int(self.cfg.get("llm.max_tokens", 900)),
        ):
            chunks.append(token)
            yield {"type": "delta", "content": token}

        answer = self._ensure_citation("".join(chunks), citations)
        product_meta = self._product_meta(hits)
        compliance = self.compliance.check(answer, citations, plan.intent, profile, product_meta)
        if not compliance.passed and compliance.action == "block":
            safe = ComplianceChecker.safe_fallback(citations)
            yield {"type": "replace", "content": safe}
            answer = safe
        elif self.compliance.require_disclaimer:
            with_disclaimer = ComplianceChecker.ensure_disclaimer(answer)
            if len(with_disclaimer) > len(answer):
                suffix = with_disclaimer[len(answer) :]
                yield {"type": "delta", "content": suffix}
                answer = with_disclaimer
        with_images = self._append_images(answer, citations)
        if len(with_images) > len(answer):
            yield {"type": "delta", "content": with_images[len(answer) :]}
            answer = with_images

        result = AnswerResult(
            answer=answer,
            citations=citations,
            confidence=confidence,
            intent=plan.intent,
            strategy=plan.strategy,
            compliance=compliance,
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
            model=self.llm.model_name,
            visual=visual or {},
            debug={
                "plan_rationale": plan.rationale,
                "queries": plan.queries,
                "profile_hints": plan.profile_hints,
                "profile": profile_snapshot(profile),
                "retrieval": debug,
            },
        )
        yield {"type": "done", **result.to_dict()}
        await self._finish(
            session_id,
            profile,
            question,
            result,
            cache=True,
            image_context=image_context,
            domain=domain,
        )

    def _sanitize_question(self, question: str) -> str:
        question = normalize_text(question)
        if not question:
            raise ValueError("问题不能为空")
        if len(question) > self.max_question_chars:
            question = question[: self.max_question_chars]
        return question

    def _cache_key(
        self,
        question: str,
        profile: UserProfile,
        image_context: str = "",
        domain: str = "all",
    ) -> str:
        return self.cache.key(
            "answer",
            normalize_question(question),
            profile_fingerprint(profile),
            self.llm.model_name,
            self.kb_version,
            self.min_confidence,
            image_context[:1000],
            (domain or "all").strip().lower(),
        )

    def _cache_lookup(
        self,
        question: str,
        profile: UserProfile,
        image_context: str = "",
        domain: str = "all",
    ) -> AnswerResult | None:
        payload, level = self.cache.get(
            self._cache_key(question, profile, image_context, domain)
        )
        if not payload:
            return None
        try:
            result = AnswerResult(
                answer=payload["answer"],
                citations=[Citation(**item) for item in payload.get("citations", [])],
                confidence=float(payload.get("confidence", 0)),
                intent=IntentResult(**payload["intent"]),
                strategy=payload.get("strategy", "direct"),
                cached=True,
                cache_level=level,
                compliance=ComplianceResult(**payload["compliance"])
                if payload.get("compliance")
                else None,
                model=payload.get("model", ""),
                visual=payload.get("visual", {}),
            )
            return result
        except Exception as exc:  # pragma: no cover
            logger.warning("缓存反序列化失败: %s", exc)
            return None

    def _cache_store(
        self,
        question: str,
        profile: UserProfile,
        result: AnswerResult,
        image_context: str = "",
        domain: str = "all",
    ) -> None:
        self.cache.set(
            self._cache_key(question, profile, image_context, domain),
            {
                "answer": result.answer,
                "citations": [c.to_dict() for c in result.citations],
                "confidence": result.confidence,
                "intent": {
                    "name": result.intent.name,
                    "confidence": result.intent.confidence,
                    "entities": result.intent.entities,
                    "rationale": result.intent.rationale,
                },
                "strategy": result.strategy,
                "compliance": result.compliance.to_dict() if result.compliance else None,
                "model": result.model,
                "visual": result.visual,
            },
        )

    def _retrieve(
        self, plan: QueryPlan, profile: UserProfile, domain: str = "all"
    ) -> tuple[list[SearchHit], float, dict[str, Any]]:
        if plan.strategy == "subquery" and len(plan.queries) > 1:
            result_lists: list[list[SearchHit]] = []
            debug: dict[str, Any] = {"subqueries": []}
            for query in plan.queries:
                collections = self._apply_domain(
                    self._collections_for_query(query, plan.intent.name),
                    plan.intent.name,
                    domain,
                )
                filters = self._filters_for_query(query, plan.intent.name, profile, collections)
                hits, _, branch_debug = self.retriever.retrieve(
                    [query],
                    top_k=self.retriever.fusion_top_k,
                    filters=filters,
                    collections=collections,
                )
                if not hits and filters:
                    relaxed = {k: v for k, v in filters.items() if k not in {"publish_date", "industry"}}
                    hits, _, branch_debug = self.retriever.retrieve(
                        [query],
                        top_k=self.retriever.fusion_top_k,
                        filters=relaxed or None,
                        collections=collections,
                    )
                    branch_debug["filter_fallback"] = True
                result_lists.append(hits)
                debug["subqueries"].append(
                    {"query": query, "collections": collections, "hits": len(hits), **branch_debug}
                )
            fused = dedupe_by_section(
                reciprocal_rank_fusion(result_lists, self.retriever.rrf_k)
            )
            fused = fused[: self.retriever.fusion_top_k]
            reranked = self.retriever.reranker.rerank(
                [plan.question, *plan.queries], fused, self.retriever.final_top_k
            )
            confidence = self.retriever._confidence(plan.question, reranked)
            debug["domain"] = domain
            debug["domain_applied"] = self._domain_applied(
                collections, domain, plan.intent.name
            )
            return reranked, confidence, debug

        collections = self._apply_domain(
            self._collections_for_query(plan.question, plan.intent.name),
            plan.intent.name,
            domain,
        )
        filters = self._filters_for_query(
            plan.question, plan.intent.name, profile, collections
        )
        hits, confidence, debug = self.retriever.retrieve(
            plan.queries, filters=filters, collections=collections
        )
        debug["domain"] = domain
        debug["domain_applied"] = self._domain_applied(
            collections, domain, plan.intent.name
        )
        if not hits and filters:
            relaxed = {k: v for k, v in filters.items() if k not in {"publish_date", "industry"}}
            hits, confidence, debug = self.retriever.retrieve(
                plan.queries, filters=relaxed or None, collections=collections
            )
            debug["filter_fallback"] = True
        return hits, confidence, debug

    @staticmethod
    def _apply_domain(
        collections: list[str], intent: str, domain: str
    ) -> list[str]:
        selected = (domain or "all").strip().lower()
        if selected in {"", "all"}:
            return collections
        if intent == "compliance":
            # 法规是跨产品域的监管依据，不能被“银行理财/基金”等业务域过滤掉。
            return collections
        filtered = [item for item in collections if item == selected]
        return filtered or collections

    @staticmethod
    def _domain_applied(collections: list[str], domain: str, intent: str) -> bool:
        selected = (domain or "all").strip().lower()
        return (
            selected not in {"", "all"}
            and intent != "compliance"
            and selected in collections
        )

    @staticmethod
    def _collections_for_query(query: str, intent: str) -> list[str]:
        if intent == "compliance":
            return ["macro"]
        if intent == "market_analysis":
            return ["macro"]
        collections: list[str] = []
        if any(
            token in query
            for token in (
                "研报", "行业", "市场", "宏观", "趋势", "政策", "板块", "前景",
                "估值", "走势", "消费", "医药", "科技", "利率", "下调",
            )
        ):
            collections.append("macro")
        if "基金" in query:
            collections.append("fund")
        if any(token in query for token in ("保险", "条款", "理赔", "核保", "等待期", "免责")):
            collections.append("insurance")
        if any(token in query for token in ("债券", "固收")):
            collections.append("bond")
        if "银行理财" in query:
            collections.append("bank_wealth")
        if any(token in query for token in ("股票", "权益")):
            collections.append("equity")
        if collections:
            return list(dict.fromkeys(collections))

        if intent == "claim":
            return ["insurance"]
        if intent == "risk_assessment":
            return ["insurance", "fund", "bank_wealth", "macro"]
        if intent == "product_consultation":
            return ["fund", "insurance", "bond", "bank_wealth"]
        if intent == "investment_strategy":
            return ["macro", "fund", "insurance", "bond", "bank_wealth"]
        return ["equity", "fund", "insurance", "bond", "bank_wealth", "macro"]

    def _filters_for_query(
        self, query: str, intent: str, profile: UserProfile, collections: list[str]
    ) -> dict[str, Any] | None:
        filters: dict[str, Any] = {}
        if "macro" in collections and _LATEST_RE.search(query):
            cutoff = (datetime.now() - timedelta(days=self.research_days)).strftime("%Y-%m-%d")
            filters["publish_date"] = {"gte": cutoff}
        if intent != "compliance":
            for industry, keywords in _INDUSTRY_KEYWORDS.items():
                if "macro" in collections and any(keyword in query for keyword in keywords):
                    filters["industry"] = industry
                    break
        if intent == "compliance":
            filters["doc_type"] = ["regulation"]
        product_collections = {"fund", "insurance", "bond", "bank_wealth", "equity"}
        if (
            _RECOMMEND_RE.search(query)
            and product_collections.intersection(collections)
        ):
            # 适当性硬约束：只召回不高于用户风险承受能力的产品。
            filters["risk_level"] = [f"R{i}" for i in range(1, profile.risk_rank + 1)]
        return filters or None

    def _is_catalog_question(self, question: str) -> bool:
        return bool(_CATALOG_RE.search(question))

    def _catalog_docs(self, question: str) -> list[dict[str, Any]]:
        if "基金" in question:
            return [item for item in self.catalog if item.get("asset_class") == "fund"]
        if "债券" in question:
            return [item for item in self.catalog if item.get("asset_class") == "bond"]
        if "银行理财" in question:
            return [
                item for item in self.catalog if item.get("asset_class") == "bank_wealth"
            ]
        if "股票" in question or "权益" in question:
            return [item for item in self.catalog if item.get("asset_class") == "equity"]
        if "保险" in question or "险种" in question:
            return [
                item
                for item in self.catalog
                if item.get("asset_class") == "insurance"
                or item.get("doc_type") == "insurance"
            ]
        return list(self.catalog)

    def _catalog_result(
        self, question: str, request_id: str, started: float
    ) -> AnswerResult:
        docs = self._catalog_docs(question)
        requested = ""
        match = re.match(
            r"^\s*(?:请问|帮我查|想了解)?\s*(.{2,12}?)(?:有哪些|包含哪些|有什么|是什么)",
            question,
        )
        if match:
            candidate = match.group(1).strip()
            if "知识库" not in candidate and not any(
                word in candidate for word in ("板块", "行业", "市场", "哪些")
            ):
                requested = candidate

        missing = bool(
            requested
            and docs
            and not any(self._product_match(requested, doc["title"]) for doc in docs)
        )
        lines: list[str] = []
        if missing:
            lines.append(f"知识库中未收录“{requested}”。")
        if docs:
            label = "保险产品" if any(
                doc.get("asset_class") == "insurance" or doc.get("doc_type") == "insurance"
                for doc in docs
            ) else "产品"
            lines.append(f"当前知识库收录的{label}有：")
            lines.extend(
                f"- {doc['title']} [{index}]"
                for index, doc in enumerate(docs[:20], start=1)
            )
            if missing:
                lines.append("如需查询该产品，请上传对应条款，或提供产品全称和条款编号。")
            else:
                lines.append("可以继续询问具体产品的保障责任、等待期、免责或理赔条件。")
        else:
            lines.append("当前知识库尚未收录对应的产品资料。")
            lines.append("请上传产品条款或说明书后再查询。")

        citations = [
            Citation(
                index=index,
                doc_id=doc["doc_id"],
                title=doc["title"],
                doc_type=doc.get("doc_type", ""),
                page=0,
                quote="知识库目录项",
                source_path=doc.get("source_path", ""),
                source_url=doc.get("source_url", ""),
            )
            for index, doc in enumerate(docs[:20], start=1)
        ]
        return AnswerResult(
            answer="\n".join(lines),
            citations=citations,
            confidence=1.0,
            intent=IntentResult(
                name="product_catalog",
                confidence=1.0,
                rationale="catalog_lookup",
            ),
            strategy="catalog",
            compliance=ComplianceResult(
                passed=True, action="allow", details={"layer": "catalog"}
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
            model="catalog",
        )

    def _missing_product(self, question: str) -> str:
        match = _ATTRIBUTE_RE.match(question)
        if not match:
            return ""
        candidate = match.group(1).strip()
        if candidate in _GENERIC_PRODUCTS:
            return ""
        if any(self._product_match(candidate, doc["title"]) for doc in self.catalog):
            return ""
        return candidate

    def _missing_product_result(
        self, candidate: str, request_id: str, started: float
    ) -> AnswerResult:
        docs = [
            item
            for item in self.catalog
            if item.get("asset_class") == "insurance"
            or item.get("doc_type") == "insurance"
        ]
        lines = [
            f"知识库中未收录“{candidate}”，因此无法基于条款确认它的保障责任、等待期或理赔条件。",
        ]
        if docs:
            lines.append("当前知识库已收录的保险产品有：")
            lines.extend(
                f"- {doc['title']} [{index}]"
                for index, doc in enumerate(docs[:20], start=1)
            )
        lines.append("请上传该产品的条款/说明书，或确认产品全称和条款编号后再查询。")
        citations = [
            Citation(
                index=index,
                doc_id=doc["doc_id"],
                title=doc["title"],
                doc_type=doc.get("doc_type", ""),
                page=0,
                quote="知识库目录项",
                source_path=doc.get("source_path", ""),
                source_url=doc.get("source_url", ""),
            )
            for index, doc in enumerate(docs[:20], start=1)
        ]
        return AnswerResult(
            answer="\n".join(lines),
            citations=citations,
            confidence=1.0,
            intent=IntentResult(
                name="product_catalog",
                confidence=1.0,
                rationale="product_not_in_catalog",
            ),
            strategy="catalog_miss",
            compliance=ComplianceResult(
                passed=True, action="allow", details={"layer": "catalog"}
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
            model="catalog",
        )

    @staticmethod
    def _product_match(candidate: str, title: str) -> bool:
        if candidate in title:
            return True
        if len(candidate) < 4:
            return False
        return any(
            candidate[index : index + 4] in title
            for index in range(0, len(candidate) - 3)
        )

    def _faq_result(
        self,
        question: str,
        faq_hit: dict[str, Any],
        profile: UserProfile,
        request_id: str,
        started: float,
    ) -> AnswerResult:
        answer = str(faq_hit["answer"]).strip()
        if "[1]" not in answer:
            answer = f"{answer} [1]"
        citation = Citation(
            index=1,
            doc_id="faq",
            title=str(faq_hit.get("source") or "高频问答库"),
            doc_type="faq",
            article="",
            page=0,
            quote=str(faq_hit.get("answer", ""))[:220],
            source_path="faq",
        )
        intent = IntentResult(
            name=str(faq_hit.get("category") or "other"),
            confidence=0.99,
            rationale=f"faq_{faq_hit.get('match', 'exact')}",
        )
        compliance = self.compliance.check(
            answer, [citation], intent, profile, None
        )
        if self.compliance.require_disclaimer:
            answer = ComplianceChecker.ensure_disclaimer(answer)
        return AnswerResult(
            answer=answer,
            citations=[citation],
            confidence=float(faq_hit.get("score", 0.9)),
            intent=intent,
            strategy="faq",
            cached=True,
            cache_level="faq",
            compliance=compliance,
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
            model="faq",
        )

    def _blocked_result(
        self,
        question: str,
        precheck: ComplianceResult,
        request_id: str,
        started: float,
    ) -> AnswerResult:
        details = "；".join(precheck.violations) or "涉及违规请求"
        answer = (
            "抱歉，该请求涉及收益承诺、内幕信息或违规利益等敏感内容，我不能提供协助。"
            "我可以为您做合规的条款解读、产品信息对比和风险提示。"
        )
        return AnswerResult(
            answer=answer,
            citations=[],
            confidence=0.0,
            intent=IntentResult(
                name="compliance",
                confidence=0.99,
                rationale=f"input_precheck: {details}",
            ),
            strategy="precheck",
            compliance=precheck,
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
            model="policy-engine",
            debug={"precheck": precheck.to_dict()},
        )

    def _visual_failure_result(
        self,
        visual: dict[str, Any],
        request_id: str,
        started: float,
    ) -> AnswerResult:
        error = str(visual.get("error") or "图片识别服务不可用")
        return AnswerResult(
            answer=(
                f"抱歉，图片识别失败：{error}\n\n"
                "请确认图片清晰、格式为 PNG/JPEG/WEBP，或在上传后补充文字说明。"
                "如需自动识别，请配置 vision.provider 和视觉模型 API。"
            ),
            citations=[],
            confidence=0.0,
            intent=IntentResult(
                name="visual_input",
                confidence=0.0,
                rationale="vision_failed",
            ),
            strategy="vision",
            compliance=ComplianceResult(
                passed=True, action="allow", details={"reason": "vision_failed"}
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
            model="vision",
            visual=visual,
            debug={"vision": visual},
        )

    def _greeting_result(
        self, question: str, request_id: str, started: float
    ) -> AnswerResult:
        answer = (
            "您好，我是小智财答，可以帮您做基金/保险/债券产品分析、"
            "资产配置、市场解读和合规查询。请告诉我具体产品或想了解的问题。"
        )
        return AnswerResult(
            answer=answer,
            citations=[],
            confidence=1.0,
            intent=IntentResult(name="smalltalk", confidence=1.0, rationale="greeting"),
            strategy="greeting",
            compliance=ComplianceResult(passed=True, action="allow"),
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
            model="system",
        )

    def _refusal_result(
        self,
        question: str,
        plan: QueryPlan,
        hits: list[SearchHit],
        confidence: float,
        debug: dict[str, Any],
        request_id: str,
        started: float,
        history: list[dict[str, str]] | None = None,
    ) -> AnswerResult:
        if self._should_stop_clarifying(question, history or []):
            return self._final_unknown_result(
                question, plan, confidence, debug, request_id, started
            )
        focus = self._clarification_focus(question, plan.intent.name)
        followups = self._clarification_followups(question, plan.intent.name)
        followup_text = "\n".join(f"- {item}" for item in followups)
        if any(word in question for word in ("基金", "混合", "股票", "债券", "ETF", "指数", "联接")):
            closing = "通常需要结合基金合同、招募说明书和产品资料概要核对；具体以基金合同和官方披露为准。"
        else:
            closing = "通常需要结合产品条款和官方披露核对；具体以合同及保险公司核保/审核结果为准。"
        answer = (
            f"我理解您想确认的是：{focus}。\n\n"
            "当前知识库没有找到足够可靠的依据，暂时不能直接确认具体数值或结论，"
            "以免给您不准确的信息。\n\n"
            "为了准确回答，请补充：\n"
            f"{followup_text}\n\n"
            f"{closing}"
        )
        return AnswerResult(
            answer=answer,
            citations=[],
            confidence=confidence,
            intent=plan.intent,
            strategy="clarification",
            compliance=ComplianceResult(passed=True, action="allow", details={"reason": "refusal"}),
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
            model=self.llm.model_name,
            debug={
                "plan_rationale": plan.rationale,
                "retrieval": debug,
                "reason": "low_confidence",
                "confidence_threshold": self.min_confidence,
            },
        )

    @staticmethod
    def _should_stop_clarifying(
        question: str, history: list[dict[str, str]]
    ) -> bool:
        """同一问题已经澄清过一次，或用户明确表示没有更多信息时停止追问。"""
        if _NO_MORE_INFO_RE.search(question):
            return True
        last_assistant_index = -1
        for index in range(len(history) - 1, -1, -1):
            if history[index].get("role") == "assistant":
                last_assistant_index = index
                break
        if last_assistant_index < 0:
            return False
        last_assistant = str(history[last_assistant_index].get("content", ""))
        if _CLARIFICATION_MARKER not in last_assistant:
            return False
        last_user = ""
        for index in range(last_assistant_index - 1, -1, -1):
            if history[index].get("role") == "user":
                last_user = str(history[index].get("content", ""))
                break
        current = normalize_question(question)
        previous = normalize_question(last_user)
        if not current or not previous:
            return False
        return current == previous or current in previous or previous in current

    def _final_unknown_result(
        self,
        question: str,
        plan: QueryPlan,
        confidence: float,
        debug: dict[str, Any],
        request_id: str,
        started: float,
    ) -> AnswerResult:
        focus = self._clarification_focus(question, plan.intent.name)
        answer = (
            f"我理解您关注的是：{focus}。\n\n"
            "当前知识库仍然没有收录能够支持这个结论的可靠资料。"
            "我不会继续重复追问，也不会编造具体条款、数值或监管要求。\n\n"
            "您可以：\n"
            "- 上传对应的产品条款、合同或监管文件，我会基于原文继续核对；\n"
            "- 提供文件全称、发布机关、文号或具体条款号；\n"
            "- 如果只需要一般概念，请直接说“按通用规则解释”。\n\n"
            "本次回答不构成投资或法律建议。"
        )
        return AnswerResult(
            answer=answer,
            citations=[],
            confidence=confidence,
            intent=plan.intent,
            strategy="clarification_final",
            compliance=ComplianceResult(
                passed=True,
                action="allow",
                details={"reason": "clarification_limit_reached"},
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
            model=self.llm.model_name,
            debug={
                "plan_rationale": plan.rationale,
                "retrieval": debug,
                "reason": "clarification_limit_reached",
                "confidence_threshold": self.min_confidence,
            },
        )

    @staticmethod
    def _clarification_focus(question: str, intent: str) -> str:
        if _REGULATION_HINT_RE.search(question):
            return f"{truncate(question, 40)} 的适用范围、主要要求或具体条款"
        if any(word in question for word in ("基金", "混合", "股票", "债券", "ETF", "指数", "联接")) and len(question) <= 40:
            if "产品信息" in question or "基金产品" in question:
                return truncate(question, 60)
            return f"{truncate(question, 30)} 的基金产品信息"
        if any(
            word in question
            for word in ("投保上限", "保额上限", "最高保额", "能买多少", "投保年龄")
        ):
            return "某款产品的最高投保上限或可投保范围"
        mapping = {
            "claim": "理赔条件、理赔材料或理赔时效",
            "risk_assessment": "投保年龄、职业、健康告知或风险等级",
            "product_consultation": "产品保障责任、保额、费率或期限",
            "compliance": "销售合规、适当性或信息披露要求",
            "market_analysis": "具体行业或板块的市场表现和判断依据",
            "investment_strategy": "资产配置目标、投资期限和风险承受能力",
        }
        return mapping.get(intent, truncate(question, 60) or "您的问题")

    @staticmethod
    def _clarification_followups(question: str, intent: str) -> list[str]:
        if _REGULATION_HINT_RE.search(question):
            return [
                "您想了解适用范围、主要义务、禁止性规定，还是具体条款或罚则？",
                "请提供文件全称、发布机关、文号或具体条款号。",
                "也可以直接上传文件，我会按条款逐条核对。",
            ]
        if any(word in question for word in ("基金", "混合", "股票", "债券", "ETF", "指数", "联接")):
            return [
                "您想了解基金类型、风险等级、费率、历史业绩，还是基金经理？",
                "您的风险等级（C1-C5）和计划投资期限是什么？",
                "请提供基金代码或产品全称，便于核对具体资料。",
            ]
        if any(word in question for word in ("意外险", "医疗险", "重疾险", "寿险", "保险", "投保")):
            return [
                "具体产品名称或条款编号是什么？",
                "适用人群的年龄、职业和健康告知情况如何？",
                "您关注的是最高保额、投保年龄、费率、等待期还是理赔条件？",
            ]
        mapping = {
            "claim": "理赔场景、理赔材料或理赔时效",
            "risk_assessment": "年龄、职业、健康告知或风险等级",
            "product_consultation": "产品名称、关注条款或费率/期限",
            "compliance": "具体销售行为或适当性场景",
            "market_analysis": "行业/板块、时间区间和关注指标",
        }
        return [
            "具体产品/文档名称或条款编号；",
            mapping.get(intent, "您最想确认的指标或结论；"),
            "适用人群、投资期限或使用场景。",
        ]

    async def _llm_fallback_result(
        self,
        question: str,
        plan: QueryPlan,
        hits: list[SearchHit],
        confidence: float,
        debug: dict[str, Any],
        profile: UserProfile,
        history: list[dict[str, str]],
        request_id: str,
        started: float,
    ) -> AnswerResult:
        """知识库置信度不足时，用 LLM 结合查询和可能相关片段做受控通用回答。"""
        context, citations = (
            build_context(hits[:6], max_chars=4500, parent_expansion=False)
            if hits
            else ("", [])
        )
        prompt = f"""你是小智财答的金融智能助手。当前知识库没有检索到足够可靠的依据。

用户问题：
{question}

可能相关但不保证完整的知识库片段：
{context or "（没有可用片段）"}

请结合用户问题和上述片段作答，并遵守：
1. 开头明确写“【知识库未命中，以下为通用分析，不是条款原文】”；
2. 不编造具体产品条款、费率、收益、法规条文或理赔结论；
3. 如果问题涉及具体合同/产品条款，建议用户上传文件或咨询持牌顾问；
4. 给出可执行的通用分析或下一步核对清单；
5. 不承诺收益，不使用“保本、稳赚、一定、百分百”等绝对化表述；
6. 结尾附风险提示，说明不构成投资或法律建议。"""
        try:
            answer = await self.llm.acomplete(
                [
                    {"role": "system", "content": "你是严谨的金融智能助手，不编造未提供的事实。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=900,
            )
            answer = answer.strip()
            if not answer:
                return self._refusal_result(
                    question,
                    plan,
                    hits,
                    confidence,
                    debug,
                    request_id,
                    started,
                    history,
                )
            if "知识库未命中" not in answer:
                answer = "【知识库未命中，以下为通用分析，不是条款原文】\n\n" + answer
            answer = ComplianceChecker.ensure_disclaimer(answer)
            product_meta = self._product_meta(hits)
            compliance = self.compliance.check(
                answer, [], plan.intent, profile, product_meta
            )
            if not compliance.passed and compliance.action == "block":
                answer = ComplianceChecker.safe_fallback(citations)
            return AnswerResult(
                answer=answer,
                citations=citations,
                confidence=confidence,
                intent=plan.intent,
                strategy="llm_fallback",
                compliance=compliance,
                latency_ms=(time.perf_counter() - started) * 1000,
                request_id=request_id,
                model=self.llm.model_name,
                debug={
                    "plan_rationale": plan.rationale,
                    "retrieval": debug,
                    "fallback": True,
                    "reason": "knowledge_base_miss",
                },
            )
        except Exception as exc:
            logger.warning("LLM 兜底回答失败，回退拒答: %s", exc)
            return self._refusal_result(
                question,
                plan,
                hits,
                confidence,
                debug,
                request_id,
                started,
                history,
            )

    @staticmethod
    def _ensure_citation(answer: str, citations: list[Citation]) -> str:
        if not citations or re.search(r"\[\d+\]", answer):
            return answer
        return f"{answer.rstrip()} [1]"

    @staticmethod
    def _append_images(answer: str, citations: list[Citation]) -> str:
        images = []
        seen = set()
        for citation in citations:
            for image in citation.images:
                key = image.get("id") or image.get("url")
                if not key or key in seen:
                    continue
                seen.add(key)
                images.append(image)
        if not images:
            return answer
        lines = [answer.rstrip(), "", "相关图片："]
        for image in images[:6]:
            url = str(image.get("url") or "")
            if not url.startswith(("/v1/images/", "http://", "https://")):
                continue
            alt = str(image.get("alt") or "条款图片").replace("]", "").replace("\n", " ")
            lines.append(f"![{alt}]({url})")
        return "\n".join(lines) if len(lines) > 3 else answer

    @staticmethod
    def _product_meta(hits: list[SearchHit]) -> dict[str, Any]:
        levels = [
            hit.chunk.risk_level or str(hit.chunk.metadata.get("risk_level", ""))
            for hit in hits
        ]
        levels = [level for level in levels if level]
        if levels:
            return {"risk_level": max(levels, key=_risk_rank)}
        return {}
    async def _remember(
        self, session_id: str, profile: UserProfile, question: str, answer: str
    ) -> None:
        try:
            self.memory.append(session_id, profile.user_id, "user", question)
            self.memory.append(session_id, profile.user_id, "assistant", answer)
        except Exception as exc:
            logger.warning("会话写入失败: %s", exc)

    async def _finish(
        self,
        session_id: str,
        profile: UserProfile,
        question: str,
        result: AnswerResult,
        cache: bool,
        image_context: str = "",
        domain: str = "all",
    ) -> None:
        await self._remember(session_id, profile, question, result.answer)
        self._audit(session_id, profile, question, result)
        if cache and result.compliance and result.compliance.passed:
            self._cache_store(question, profile, result, image_context, domain)

    def _audit(
        self,
        session_id: str,
        profile: UserProfile,
        question: str,
        result: AnswerResult,
    ) -> None:
        try:
            self.audit.log(
                {
                    "request_id": result.request_id,
                    "session_id": session_id,
                    "user_id": profile.user_id,
                    "question": question,
                    "answer": result.answer,
                    "intent": result.intent.name,
                    "strategy": result.strategy,
                    "citations": [c.to_dict() for c in result.citations],
                    "compliance": result.compliance.to_dict() if result.compliance else {},
                    "profile_snapshot": profile_snapshot(profile),
                    "confidence": result.confidence,
                    "latency_ms": result.latency_ms,
                    "model": result.model,
                }
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("审计写入失败: %s", exc)
