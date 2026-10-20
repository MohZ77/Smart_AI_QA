from __future__ import annotations

from huixiaoan.indexing.bm25 import BM25Index
from huixiaoan.retrieval.hybrid import dedupe_by_section, reciprocal_rank_fusion
from huixiaoan.types import Chunk, SearchHit


def _chunk(chunk_id: str, text: str, section: str = "") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="doc",
        text=text,
        title="测试文档",
        section=section or chunk_id,
    )


def test_bm25_ranks_keyword_match_first() -> None:
    index = BM25Index().build(
        [
            _chunk("a", "本合同等待期为九十日，等待期内不承担保险责任。"),
            _chunk("b", "本合同的年度免赔额为一万元，扣除免赔额后按比例给付。"),
            _chunk("c", "保险期间为一年，期满可以申请续保。"),
        ]
    )
    hits = index.search("年度免赔额是多少", top_k=3)
    assert hits
    assert hits[0].chunk.chunk_id == "b"


def test_rrf_merges_rankings_and_dedupes_sections() -> None:
    a = _chunk("a", "A", "section-1")
    b = _chunk("b", "B", "section-2")
    c = _chunk("c", "C", "section-3")
    vector_hits = [
        SearchHit(chunk=a, score=0.9, retriever="vector", rank=1),
        SearchHit(chunk=b, score=0.8, retriever="vector", rank=2),
    ]
    bm25_hits = [
        SearchHit(chunk=b, score=12.0, retriever="bm25", rank=1),
        SearchHit(chunk=c, score=8.0, retriever="bm25", rank=2),
    ]
    fused = reciprocal_rank_fusion([vector_hits, bm25_hits], k=60)
    assert fused[0].chunk.chunk_id == "b"
    assert len(dedupe_by_section(fused)) == 3
