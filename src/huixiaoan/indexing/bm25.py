from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..types import Chunk, SearchHit
from ..utils import tokenize
from .filters import match_filters


class BM25Index:
    """BM25-Okapi 关键词召回，支持条款号精确加权。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75, dict_path: Path | None = None) -> None:
        self.k1 = k1
        self.b = b
        self.dict_path = dict_path
        self.chunks: list[Chunk] = []
        self.term_freqs: list[Counter[str]] = []
        self.doc_lengths: list[int] = []
        self.doc_freq: Counter[str] = Counter()
        self.inverted: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.avgdl = 0.0
        self._idf: dict[str, float] = {}

    def build(self, chunks: list[Chunk]) -> "BM25Index":
        self.chunks = list(chunks)
        self.term_freqs = []
        self.doc_lengths = []
        self.doc_freq = Counter()
        self.inverted = defaultdict(list)
        for index, chunk in enumerate(self.chunks):
            tokens = tokenize(f"{chunk.title} {chunk.section} {chunk.text}", self.dict_path)
            frequencies = Counter(tokens)
            self.term_freqs.append(frequencies)
            self.doc_lengths.append(len(tokens))
            self.doc_freq.update(frequencies.keys())
            for token, tf in frequencies.items():
                self.inverted[token].append((index, tf))
        self.avgdl = (
            sum(self.doc_lengths) / len(self.doc_lengths) if self.doc_lengths else 0.0
        )
        total = max(1, len(self.chunks))
        self._idf = {
            term: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for term, freq in self.doc_freq.items()
        }
        return self

    def search(
        self,
        query: str,
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        if not self.chunks or not query.strip():
            return []
        query_tokens = tokenize(query, self.dict_path)
        if not query_tokens:
            return []
        article = ""
        for token in query_tokens:
            if token.startswith("第") and token.endswith("条"):
                article = token
                break

        scores: dict[int, float] = defaultdict(float)
        for token in query_tokens:
            postings = self.inverted.get(token, [])
            if not postings:
                continue
            idf = self._idf.get(token, 0.0)
            for index, tf in postings:
                length = self.doc_lengths[index]
                denominator = tf + self.k1 * (
                    1 - self.b + self.b * (length / (self.avgdl or 1.0))
                )
                scores[index] += idf * (tf * (self.k1 + 1)) / denominator

        scored: list[tuple[float, int]] = []
        for index, score in scores.items():
            chunk = self.chunks[index]
            if not match_filters(chunk, filters):
                continue
            if article and chunk.article == article:
                score *= 1.6
            if score > 0:
                scored.append((score, index))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            SearchHit(
                chunk=self.chunks[index],
                score=score,
                retriever="bm25",
                rank=rank,
                components={"bm25": score},
            )
            for rank, (score, index) in enumerate(scored[:top_k], start=1)
        ]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "k1": self.k1,
            "b": self.b,
            "chunks": [chunk.to_record() for chunk in self.chunks],
            "tokens": [list(freq.elements()) for freq in self.term_freqs],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path, dict_path: Path | None = None) -> "BM25Index":
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        index = cls(k1=payload.get("k1", 1.5), b=payload.get("b", 0.75), dict_path=dict_path)
        chunks = [Chunk.from_record(record) for record in payload.get("chunks", [])]
        index.chunks = chunks
        index.term_freqs = [Counter(tokens) for tokens in payload.get("tokens", [])]
        index.doc_lengths = [sum(freq.values()) for freq in index.term_freqs]
        index.inverted = defaultdict(list)
        for doc_index, frequencies in enumerate(index.term_freqs):
            for token, tf in frequencies.items():
                index.inverted[token].append((doc_index, tf))
        index.doc_freq = Counter()
        for freq in index.term_freqs:
            index.doc_freq.update(freq.keys())
        index.avgdl = (
            sum(index.doc_lengths) / len(index.doc_lengths) if index.doc_lengths else 0.0
        )
        total = max(1, len(chunks))
        index._idf = {
            term: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for term, freq in index.doc_freq.items()
        }
        return index
