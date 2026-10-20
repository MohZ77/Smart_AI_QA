from __future__ import annotations

import json
from typing import Any

from ..types import Chunk
from ..storage.db import Database
from ..utils import normalize_question, tokenize
from .bm25 import BM25Index


class FAQStore:
    """FAQ 精确/近似匹配，作为三级缓存中的 L3 高频问答库。

    默认 SQLite，零依赖启动；生产可把 `paths.faq_db` 换成 MySQL DSN 对应的
    适配实现（字段与 SQL 保持兼容）。
    """

    def __init__(
        self,
        db_url: str,
        threshold: float = 0.85,
        redis_client: Any = None,
        redis_top_n: int = 3000,
        redis_ttl_s: int = 21600,
    ) -> None:
        self.db_url = db_url
        self.threshold = threshold
        self._db = Database(db_url)
        self._bm25: BM25Index | None = None
        self._redis = redis_client
        self._redis_top_n = max(1, redis_top_n)
        self._redis_ttl_s = max(60, redis_ttl_s)
        self._redis_prefix = "huixiaoan:faq:"
        self._init_schema()
        self._rebuild_bm25()

    def _init_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS faq (
                id VARCHAR(64) PRIMARY KEY,
                question TEXT NOT NULL,
                normalized VARCHAR(512) NOT NULL,
                answer TEXT NOT NULL,
                category VARCHAR(64) DEFAULT '',
                source VARCHAR(512) DEFAULT '',
                updated_at VARCHAR(64) DEFAULT ''
            )
            """
        )
        # MySQL 的 TEXT 不能直接建普通索引；前缀索引在两种后端都能工作。
        if self._db.kind == "sqlite":
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_faq_normalized ON faq(normalized)")
        else:
            try:
                self._db.execute(
                    "CREATE INDEX idx_faq_normalized ON faq(normalized(191))"
                )
            except Exception:
                pass

    def add_many(self, items: list[dict[str, Any]]) -> int:
        from ..utils import stable_id

        rows = []
        for item in items:
            question = str(item.get("question", "")).strip()
            answer = str(item.get("answer", "")).strip()
            if not question or not answer:
                continue
            rows.append(
                (
                    str(item.get("id") or stable_id(question, length=16)),
                    question,
                    normalize_question(question),
                    answer,
                    str(item.get("category", "")),
                    str(item.get("source", "")),
                    str(item.get("updated_at", "")),
                )
            )
        self._db.executemany(
            "REPLACE INTO faq(id, question, normalized, answer, category, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._rebuild_bm25()
        return len(rows)

    def _rebuild_bm25(self) -> None:
        rows = self._db.query("SELECT id, question FROM faq")
        chunks = [
            Chunk(
                chunk_id=str(row["id"]),
                doc_id=str(row["id"]),
                text=str(row["question"]),
                title=str(row["question"]),
            )
            for row in rows
        ]
        self._bm25 = BM25Index().build(chunks) if chunks else None
        self._warm_redis()

    def _warm_redis(self) -> None:
        if self._redis is None:
            return
        rows = self._db.query(
            "SELECT question, answer, category, source FROM faq "
            "ORDER BY updated_at DESC, id ASC LIMIT ?",
            (self._redis_top_n,),
        )
        try:
            pipe = self._redis.pipeline()
            for row in rows:
                key = self._redis_prefix + normalize_question(row["question"])
                pipe.set(
                    key,
                    json.dumps(
                        {
                            "question": row["question"],
                            "answer": row["answer"],
                            "category": row["category"],
                            "source": row["source"],
                            "score": 1.0,
                            "match": "redis",
                        },
                        ensure_ascii=False,
                    ),
                    ex=self._redis_ttl_s,
                )
            if rows:
                pipe.execute()
        except Exception:
            # Redis 是加速层，故障不能影响 FAQ 主链路。
            pass

    def lookup(self, question: str) -> dict[str, Any] | None:
        normalized = normalize_question(question)
        if not normalized:
            return None
        if self._redis is not None:
            try:
                cached = self._redis.get(self._redis_prefix + normalized)
                if cached:
                    payload = json.loads(cached)
                    payload["match"] = "redis"
                    return payload
            except Exception:
                pass
        row = self._db.query_one(
            "SELECT question, answer, category, source FROM faq WHERE normalized = ? LIMIT 1",
            (normalized,),
        )
        if row:
            payload = {
                "question": row["question"],
                "answer": row["answer"],
                "category": row["category"],
                "source": row["source"],
                "score": 1.0,
                "match": "exact",
            }
            if self._redis is not None:
                try:
                    self._redis.set(
                        self._redis_prefix + normalized,
                        json.dumps(payload, ensure_ascii=False),
                        ex=self._redis_ttl_s,
                    )
                except Exception:
                    pass
            return payload

        query_tokens = set(tokenize(question))
        if not query_tokens:
            return None
        candidates = (
            self._bm25.search(question, top_k=20)
            if self._bm25 is not None
            else []
        )
        if not candidates:
            return None
        candidate_ids = [hit.chunk.chunk_id for hit in candidates]
        placeholders = ", ".join("?" for _ in candidate_ids)
        rows = self._db.query(
            f"SELECT id, question, normalized, answer, category, source FROM faq "
            f"WHERE id IN ({placeholders})",
            candidate_ids,
        )
        rows_by_id = {str(row["id"]): row for row in rows}
        best: tuple[float, dict[str, Any]] | None = None
        for hit in candidates:
            row = rows_by_id.get(hit.chunk.chunk_id)
            if not row:
                continue
            candidate_tokens = set(tokenize(row["question"]))
            if not candidate_tokens:
                continue
            intersection = len(query_tokens & candidate_tokens)
            union = len(query_tokens | candidate_tokens)
            score = intersection / union if union else 0.0
            if best is None or score > best[0]:
                best = (score, row)
        if best and best[0] >= self.threshold:
            row = best[1]
            return {
                "question": row["question"],
                "answer": row["answer"],
                "category": row["category"],
                "source": row["source"],
                "score": round(best[0], 4),
                "match": "fuzzy",
                "bm25": round(
                    hit.score,
                    4,
                ),
            }
        return None

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM faq")
        if not row:
            return 0
        return int(row["n"])

    def close(self) -> None:
        self._db.close()
