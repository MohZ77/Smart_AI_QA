from __future__ import annotations

import logging
import math
import re
from abc import ABC, abstractmethod

from ..config import Config
from ..types import SearchHit
from ..utils import tokenize

logger = logging.getLogger(__name__)
_ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百千万零〇0-9]+条")


class BaseReranker(ABC):
    @abstractmethod
    def rerank(
        self, query: str | list[str], hits: list[SearchHit], top_k: int
    ) -> list[SearchHit]: ...


class HeuristicReranker(BaseReranker):
    """轻量精排：融合 RRF、查询词覆盖、条款号与标题命中。

    优点是无额外模型依赖、延迟稳定；生产可切换 bge-reranker。
    """

    def __init__(self, dict_path=None) -> None:
        self.dict_path = dict_path

    def rerank(
        self, query: str | list[str], hits: list[SearchHit], top_k: int
    ) -> list[SearchHit]:
        if not hits:
            return []
        queries = [query] if isinstance(query, str) else query
        token_sets = [set(tokenize(item, self.dict_path)) for item in queries]
        token_sets = [tokens for tokens in token_sets if tokens]
        articles = {
            match.group(0)
            for item in queries
            for match in [_ARTICLE_RE.search(item)]
            if match
        }
        max_rrf = max((hit.score for hit in hits), default=1.0) or 1.0

        for hit in hits:
            chunk_tokens = set(
                tokenize(f"{hit.chunk.title} {hit.chunk.section} {hit.chunk.text}", self.dict_path)
            )
            overlap = max(
                (
                    len(tokens & chunk_tokens) / len(tokens)
                    for tokens in token_sets
                ),
                default=0.0,
            )
            article_score = 1.0 if hit.chunk.article in articles else 0.0
            title_tokens = set(tokenize(hit.chunk.title, self.dict_path))
            title_score = max(
                (
                    len(tokens & title_tokens) / len(tokens)
                    for tokens in token_sets
                ),
                default=0.0,
            )
            title_score = 1.0 if title_score >= 0.5 else 0.0
            title_exact = (
                1.0
                if hit.chunk.title
                and any(
                    len(item) > 3
                    and (hit.chunk.title in item or item in hit.chunk.title)
                    for item in queries
                )
                else 0.0
            )
            rrf_norm = hit.score / max_rrf
            score = (
                0.50 * rrf_norm
                + 0.30 * overlap
                + 0.14 * article_score
                + 0.06 * title_score
                + 0.15 * title_exact
            )
            hit.components.update(
                {
                    "rrf_norm": round(rrf_norm, 4),
                    "lexical": round(overlap, 4),
                    "article_match": article_score,
                    "title_exact": title_exact,
                }
            )
            hit.score = round(min(1.0, max(0.0, score)), 6)

        ranked = sorted(hits, key=lambda item: (item.score, item.components.get("rrf_norm", 0)), reverse=True)
        for rank, hit in enumerate(ranked, start=1):
            hit.rank = rank
        return ranked[:top_k]


class CrossEncoderReranker(BaseReranker):
    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-base",
        device: str = "cpu",
        fallback: BaseReranker | None = None,
    ) -> None:
        try:
            from sentence_transformers import CrossEncoder

            self.model = CrossEncoder(model_name, device=device)
            self.name = f"cross-encoder:{model_name}"
            self.fallback = None
        except Exception as exc:  # pragma: no cover - 依赖/模型下载失败
            logger.warning("CrossEncoder 不可用，回退启发式精排: %s", exc)
            self.model = None
            self.fallback = fallback or HeuristicReranker()
            self.name = "cross-encoder-fallback"

    def rerank(
        self, query: str | list[str], hits: list[SearchHit], top_k: int
    ) -> list[SearchHit]:
        if self.model is None or not hits:
            return self.fallback.rerank(query, hits, top_k)  # type: ignore[union-attr]
        first_query = query if isinstance(query, str) else query[0]
        pairs = [[first_query, hit.chunk.text] for hit in hits]
        scores = self.model.predict(pairs)
        for hit, score in zip(hits, scores):
            probability = 1 / (1 + math.exp(-float(score)))
            hit.components["cross_encoder"] = round(probability, 4)
            hit.score = round(probability, 6)
        ranked = sorted(hits, key=lambda item: item.score, reverse=True)
        for rank, hit in enumerate(ranked, start=1):
            hit.rank = rank
        return ranked[:top_k]


def build_reranker(cfg: Config, dict_path=None) -> BaseReranker:
    provider = str(cfg.get("retrieval.reranker", "heuristic")).lower()
    if provider in {"cross-encoder", "bge-reranker", "crossencoder"}:
        return CrossEncoderReranker(
            model_name=str(cfg.get("retrieval.reranker_model", "BAAI/bge-reranker-base")),
            device=str(cfg.get("retrieval.reranker_device", "cpu")),
            fallback=HeuristicReranker(dict_path),
        )
    return HeuristicReranker(dict_path)
