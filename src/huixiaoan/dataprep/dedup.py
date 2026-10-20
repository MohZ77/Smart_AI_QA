from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass

from ..utils import tokenize
from .schema import SFTRecord


def _features(text: str) -> Counter[str]:
    tokens = tokenize(text)
    compact = "".join(text.split())
    tokens.extend(compact[index : index + 2] for index in range(max(0, len(compact) - 1)))
    return Counter(tokens)


def simhash64(text: str) -> int:
    vector = [0] * 64
    for feature, weight in _features(text).items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "little")
        for bit in range(64):
            vector[bit] += weight if (value >> bit) & 1 else -weight
    result = 0
    for bit, value in enumerate(vector):
        if value >= 0:
            result |= 1 << bit
    return result


def hamming(a: int, b: int) -> int:
    return int.bit_count(a ^ b)


class NearDuplicateIndex:
    """4 段 16-bit band 索引，避免 6 万条数据两两比较。"""

    def __init__(self, bands: int = 4, threshold: int = 3) -> None:
        self.bands = bands
        self.threshold = threshold
        self._buckets: dict[tuple[int, int], list[tuple[int, str]]] = {}

    def lookup(self, fingerprint: int) -> str | None:
        for band in range(self.bands):
            key = (band, (fingerprint >> (band * 16)) & 0xFFFF)
            for other, record_id in self._buckets.get(key, []):
                if hamming(fingerprint, other) <= self.threshold:
                    return record_id
        return None

    def add(self, fingerprint: int, record_id: str) -> None:
        for band in range(self.bands):
            key = (band, (fingerprint >> (band * 16)) & 0xFFFF)
            self._buckets.setdefault(key, []).append((fingerprint, record_id))


@dataclass
class DedupStats:
    exact_duplicates: int = 0
    near_duplicates: int = 0
    kept: int = 0


def dedupe_records(records: list[SFTRecord]) -> tuple[list[SFTRecord], DedupStats]:
    stats = DedupStats()
    exact: set[str] = set()
    near = NearDuplicateIndex()
    kept: list[SFTRecord] = []
    # 高质量样本优先保留，避免近似重复时丢掉更好的答案。
    for record in sorted(records, key=lambda item: (-item.quality_score, item.id)):
        normalized = "".join(
            (record.instruction + record.input + record.output).split()
        ).lower()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if digest in exact:
            stats.exact_duplicates += 1
            continue
        fingerprint = simhash64(record.instruction + "\n" + record.output)
        matched = near.lookup(fingerprint)
        if matched:
            stats.near_duplicates += 1
            continue
        exact.add(digest)
        near.add(fingerprint, record.id)
        kept.append(record)
    stats.kept = len(kept)
    return kept, stats
