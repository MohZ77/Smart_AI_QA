from __future__ import annotations

import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..config import Config
from ..indexing.collection_store import RoutedVectorStore
from ..indexing.bm25 import BM25Index
from ..indexing.embeddings import BaseEmbedder
from ..indexing.vector_store import BaseVectorStore
from ..types import SearchHit
from ..utils import tokenize
from .rerank import BaseReranker

logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    result_lists: list[list[SearchHit]], k: int = 60
) -> list[SearchHit]:
    """RRF：只依赖排名，天然适配向量相似度与 BM25 分数量纲不一致。"""
    scores: dict[str, float] = defaultdict(float)
    best: dict[str, SearchHit] = {}
    sources: dict[str, list[str]] = defaultdict(list)
    for hits in result_lists:
        for hit in hits:
            chunk_id = hit.chunk.chunk_id
            scores[chunk_id] += 1.0 / (k + max(1, hit.rank))
            sources[chunk_id].append(hit.retriever)
            current = best.get(chunk_id)
            if current is None or hit.score > current.score:
                best[chunk_id] = hit
    fused = []
    for chunk_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
        hit = best[chunk_id]
        hit.score = score
        hit.components = {
            **hit.components,
            "rrf": score,
            "retrievers": ",".join(sorted(set(sources[chunk_id]))),
        }
        hit.retriever = "hybrid"
        fused.append(hit)
    for rank, hit in enumerate(fused, start=1):
        hit.rank = rank
    return fused


def dedupe_by_section(hits: list[SearchHit], max_per_section: int = 1) -> list[SearchHit]:
    """同一章节的多个 child chunk 只保留最靠前的一个，避免上下文被重复段落占满。"""
    seen: dict[tuple[str, str], int] = defaultdict(int)
    result: list[SearchHit] = []
    for hit in sorted(hits, key=lambda item: item.score, reverse=True):
        key = (hit.chunk.doc_id, hit.chunk.section or hit.chunk.chunk_id)
        if seen[key] >= max_per_section:
            continue
        seen[key] += 1
        result.append(hit)
    return result


class HybridRetriever:
    def __init__(
        self,
        cfg: Config,
        embedder: BaseEmbedder,
        vector_store: BaseVectorStore,
        bm25: BM25Index | None,
        reranker: BaseReranker,
    ) -> None:
        self.cfg = cfg
        self.embedder = embedder
        self.vector_store = vector_store
        self.bm25 = bm25
        self.reranker = reranker
        self.vector_top_k = int(cfg.get("retrieval.vector_top_k", 30))
        self.bm25_top_k = int(cfg.get("retrieval.bm25_top_k", 30))
        self.fusion_top_k = int(cfg.get("retrieval.fusion_top_k", 20))
        self.final_top_k = int(cfg.get("retrieval.final_top_k", 5))
        self.rrf_k = int(cfg.get("retrieval.rrf_k", 60))
        self.min_confidence = float(cfg.get("retrieval.min_confidence", 0.8))
        self.dict_path = cfg.path("domain_dict", "configs/domain_dict.txt")

    def update_bm25(self, bm25: BM25Index) -> None:
        self.bm25 = bm25

    def retrieve(
        self,
        queries: list[str],
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
        collections: list[str] | None = None,
    ) -> tuple[list[SearchHit], float, dict[str, Any]]:
        if not queries:
            return [], 0.0, {"reason": "empty_query"}
        top_k = top_k or self.final_top_k
        result_lists: list[list[SearchHit]] = []
        debug: dict[str, Any] = {
            "queries": [],
            "filters": filters,
            "collections": collections,
        }
        router = self.vector_store if isinstance(self.vector_store, RoutedVectorStore) else None
        targets = (
            [name for name in (collections or list(router.stores)) if name in router.stores]
            if router
            else [None]
        )
        if not targets:
            targets = [None]
        bm25_filters = dict(filters or {})
        if collections:
            bm25_filters["asset_class"] = list(collections)

        # 子查询与跨 Collection 检索并行执行；IO/CPU 混合场景用线程池足够。
        max_workers = max(1, min(16, len(queries) * (len(targets) + 1)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = []
            for query_index, query in enumerate(queries):
                vector = self.embedder.encode_query(query)
                if router:
                    for name in targets:
                        futures.append(
                            (
                                query_index,
                                f"vector:{name}",
                                query,
                                pool.submit(
                                    router.store_for(name).search,
                                    vector,
                                    self.vector_top_k,
                                    filters,
                                ),
                            )
                        )
                else:
                    futures.append(
                        (
                            query_index,
                            "vector",
                            query,
                            pool.submit(
                                self.vector_store.search, vector, self.vector_top_k, filters
                            ),
                        )
                    )
                if self.bm25:
                    futures.append(
                        (
                            query_index,
                            "bm25",
                            query,
                            pool.submit(
                                self.bm25.search, query, self.bm25_top_k, bm25_filters
                            ),
                        )
                    )

            per_query: dict[int, dict[str, list[SearchHit]]] = defaultdict(dict)
            for query_index, source, query, future in futures:
                try:
                    hits = future.result()
                except Exception as exc:
                    logger.warning("检索分支失败 %s: %s", source, exc)
                    hits = []
                per_query[query_index][source] = hits
                result_lists.append(hits)

        for query_index, query in enumerate(queries):
            branches = per_query.get(query_index, {})
            vector_hits = [h for key, hits in branches.items() if key.startswith("vector") for h in hits]
            bm25_hits = branches.get("bm25", [])
            debug["queries"].append(
                {
                    "query": query,
                    "collections": targets,
                    "vector_hits": len(vector_hits),
                    "bm25_hits": len(bm25_hits),
                    "top_vector": _brief(_best(vector_hits)) if vector_hits else None,
                    "top_bm25": _brief(bm25_hits[0]) if bm25_hits else None,
                }
            )

        fused = dedupe_by_section(reciprocal_rank_fusion(result_lists, self.rrf_k))
        fused = fused[: self.fusion_top_k]
        reranked = self.reranker.rerank(queries, fused, top_k)
        confidence = self._confidence(queries[0], reranked)
        debug["fusion_count"] = len(fused)
        debug["candidate_count"] = len(result_lists)
        debug["reranker"] = getattr(self.reranker, "name", self.reranker.__class__.__name__)
        debug["top_hits"] = [
            {
                "chunk_id": hit.chunk.chunk_id,
                "doc_id": hit.chunk.doc_id,
                "title": hit.chunk.title,
                "article": hit.chunk.article,
                "score": round(hit.score, 4),
                "components": hit.components,
            }
            for hit in reranked[:5]
        ]
        return reranked, confidence, debug

    def _confidence(self, query: str, hits: list[SearchHit]) -> float:
        if not hits:
            return 0.0
        top = hits[0]
        top_score = max(0.0, min(1.0, top.score))
        query_tokens = set(tokenize(query, self.dict_path))
        if not query_tokens:
            return round(top_score, 4)
        context_tokens: set[str] = set()
        context_texts: list[str] = []
        for hit in hits[:3]:
            context_tokens.update(tokenize(hit.chunk.text, self.dict_path))
            context_tokens.update(tokenize(hit.chunk.title, self.dict_path))
            context_tokens.update(tokenize(hit.chunk.section, self.dict_path))
            context_texts.append(
                f"{hit.chunk.title} {hit.chunk.section} {hit.chunk.text}"
            )
        coverage = len(query_tokens & context_tokens) / len(query_tokens)
        title_coverage = 0.0
        for hit in hits[:3]:
            title_tokens = set(tokenize(hit.chunk.title, self.dict_path))
            title_coverage = max(
                title_coverage,
                len(query_tokens & title_tokens) / len(query_tokens),
            )
        entity = _entity_signal(query, context_texts)
        bm25_signal = min(1.0, max(0.0, float(top.components.get("bm25", 0.0))) / 12.0)
        # 噪声 OR 融合：只有多个证据同时成立时才接近 1，避免单一 BM25 虚高。
        confidence = 1 - (
            (1 - top_score)
            * (1 - coverage)
            * (1 - 0.20 * title_coverage)
            * (1 - 0.45 * entity)
            * (1 - 0.20 * bm25_signal)
        )
        unknown_terms = [
            token
            for token in query_tokens
            if len(token) >= 2
            and token not in _QUERY_FILLER
            and token not in _DOMAIN_TERMS
            and not _RISK_TERM_RE.match(token)
            and token not in context_tokens
            and not token.isdigit()
        ]
        exact_title = bool(
            top.chunk.title
            and (top.chunk.title in query or query in top.chunk.title)
        )
        if unknown_terms and not exact_title:
            # 主题实体完全没有出现在证据中：通用词命中也不能算高置信。
            confidence = min(confidence, 0.79)
        return round(max(0.0, min(1.0, confidence)), 4)


_GENERIC_TERMS = (
    "医疗", "保险", "基金", "债券", "理财", "等待期", "免赔额", "既往症",
    "理赔", "风险", "收益", "消费", "医药", "科技", "金融", "制造",
)
_NON_ENTITY_PHRASES = (
    "有哪些", "是多少", "多少天", "是什么", "怎么样", "可以吗", "能不能",
    "请问", "帮我", "告诉", "一下",
)
_QUERY_FILLER = {
    "最近", "当前", "现在", "值得", "比较", "合理", "怎么", "怎么样", "如何",
    "什么", "哪些", "多少", "可以", "能否", "是否", "帮我", "请问", "一下",
    "有没有", "需要", "应该", "这个", "那个", "相关", "具体", "主要", "大概",
    "天", "年", "月", "日", "多久", "期是",
    "处理", "没有", "几天", "才能", "几只", "多长", "期有", "症能",
    "买点", "想买", "买什么", "买哪", "买哪只", "想", "买",
    "图片", "这张", "里", "截图", "上传", "识别", "内容",
}
_DOMAIN_TERMS = {
    "保险", "医疗", "医疗险", "重疾", "寿险", "年金", "意外险", "理赔", "赔付",
    "报销", "等待期", "免责", "免赔额", "保额", "保费", "核保", "条款", "现金价值",
    "基金", "净值", "回撤", "基金经理", "申购", "赎回", "费率", "分红",
    "债券", "久期", "信用", "票息", "固收", "股票", "权益", "估值", "PE", "PB",
    "银行理财", "理财产品", "净值型", "理财", "风险", "收益", "利率",
    "资产配置", "配置", "推荐", "适合", "产品", "客户", "等级", "适当性",
    "合规", "销售", "监管", "市场", "行业", "趋势", "政策", "宏观", "经济",
    "返佣", "返点", "医保",
    "人员", "利用", "促成", "交易", "方式", "行为", "情况", "问题", "办理",
    "申请", "材料", "条件", "标准", "范围", "比例", "额度", "限额", "时间",
    "期限", "费用", "价格", "金额", "责任", "保障", "权益", "义务", "告知",
    "续保", "退保", "投保", "承保", "缴费", "领取", "给付", "审核", "材料",
    "重疾险", "保险公司", "保险产品", "险种", "金险", "社保", "市场趋势",
    "消费", "医药", "科技", "金融", "制造", "投资", "建议", "资料", "分析",
}
_RISK_TERM_RE = re.compile(r"^[CR][1-5]$", re.I)


def _entity_signal(query: str, texts: list[str]) -> float:
    """查询中的产品名/条款实体是否真实出现在检索证据中。

    这是低置信度门禁的关键：通用词（保险、基金）不能把无关产品“救”到高置信度。
    """
    compact = re.sub(r"\s+", "", query)
    for text in texts:
        for size in (6, 5, 4, 3):
            for start in range(0, max(0, len(compact) - size + 1)):
                term = compact[start : start + size]
                if term in _NON_ENTITY_PHRASES or not re.search(r"[\u4e00-\u9fa5A-Za-z]", term):
                    continue
                if term in text:
                    return 1.0
    for term in _GENERIC_TERMS:
        if term in query and any(term in text for text in texts):
            return 0.7
    return 0.0


def _brief(hit: SearchHit) -> dict[str, Any]:
    return {
        "chunk_id": hit.chunk.chunk_id,
        "title": hit.chunk.title,
        "article": hit.chunk.article,
        "score": round(hit.score, 4),
    }


def _best(hits: list[SearchHit]) -> SearchHit:
    return max(hits, key=lambda item: item.score)
