from __future__ import annotations

import math
import re
from typing import Any

from ..types import SearchHit
from ..utils import split_sentences, tokenize


def is_relevant(hit: SearchHit, item: dict[str, Any]) -> bool:
    keywords = item.get("gold_doc_keywords") or []
    if keywords and not any(keyword in hit.chunk.title for keyword in keywords):
        return False
    articles = item.get("gold_articles") or []
    if articles and hit.chunk.article not in articles and not any(
        article in hit.chunk.section for article in articles
    ):
        return False
    sections = item.get("gold_sections") or []
    if sections and not any(section in hit.chunk.section for section in sections):
        return False
    return True


def recall_at_k(hits: list[SearchHit], item: dict[str, Any], k: int = 5) -> float:
    if not hits:
        return 0.0
    relevant = sum(1 for hit in hits[:k] if is_relevant(hit, item))
    target = max(1, len(item.get("gold_articles") or [1]))
    return min(1.0, relevant / target)


def mrr_at_k(hits: list[SearchHit], item: dict[str, Any], k: int = 5) -> float:
    for index, hit in enumerate(hits[:k], start=1):
        if is_relevant(hit, item):
            return 1.0 / index
    return 0.0


def ndcg_at_k(hits: list[SearchHit], item: dict[str, Any], k: int = 5) -> float:
    gains = [1.0 if is_relevant(hit, item) else 0.0 for hit in hits[:k]]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal_count = min(k, max(1, len(item.get("gold_articles") or [1])))
    idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
    return dcg / idcg if idcg else 0.0


def answer_keyword_coverage(answer: str, item: dict[str, Any]) -> float:
    keywords = item.get("answer_keywords") or []
    if not keywords:
        return 0.0
    return sum(1 for keyword in keywords if keyword in answer) / len(keywords)


def faithfulness_lite(answer: str, contexts: list[str], threshold: float = 0.45) -> float:
    """轻量 Faithfulness：答案句子的词项是否能在检索上下文中找到支撑。"""
    if not answer or not contexts:
        return 0.0
    context_tokens = set()
    for context in contexts:
        context_tokens.update(tokenize(context))
    sentences = [
        sentence for sentence in split_sentences(answer) if len(tokenize(sentence)) >= 4
    ]
    if not sentences:
        return 0.0
    supported = 0
    for sentence in sentences:
        tokens = set(tokenize(sentence))
        if not tokens:
            continue
        overlap = len(tokens & context_tokens) / len(tokens)
        if overlap >= threshold:
            supported += 1
    return supported / len(sentences)


def citation_coverage(answer: str) -> float:
    markers = re.findall(r"\[(\d+)\]", answer)
    if not markers:
        return 0.0
    return 1.0 if len(set(markers)) >= 1 else 0.0
