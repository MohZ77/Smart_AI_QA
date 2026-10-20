from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Config
from ..types import Chunk, SearchHit
from .filters import match_filters

logger = logging.getLogger(__name__)


class BaseVectorStore(ABC):
    backend: str = "base"

    @abstractmethod
    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None: ...

    @abstractmethod
    def search(
        self,
        vector: np.ndarray,
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]: ...

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def count(self) -> int: ...

    def close(self) -> None:
        return None

    def flush(self) -> None:
        return None


class LocalVectorStore(BaseVectorStore):
    """numpy 余弦检索，文件持久化。适合单机开发与评测；生产切 Milvus。"""

    backend = "local"

    def __init__(self, index_dir: Path, dim: int) -> None:
        self.index_dir = index_dir
        self.dim = dim
        self.vectors_path = index_dir / "vectors.npy"
        self.records_path = index_dir / "records.jsonl"
        self._lock = threading.RLock()
        self._records: list[Chunk] = []
        self._vectors = np.zeros((0, dim), dtype=np.float32)
        self._index: dict[str, int] = {}
        index_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if self.vectors_path.exists() and self.records_path.exists():
            try:
                vectors = np.load(self.vectors_path)
                records = []
                with self.records_path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            records.append(Chunk.from_record(json.loads(line)))
                if len(records) == len(vectors):
                    self._vectors = vectors.astype(np.float32)
                    self._records = records
                    self._index = {chunk.chunk_id: i for i, chunk in enumerate(records)}
                    logger.info("本地向量库已加载: %s 条", len(records))
            except Exception as exc:
                logger.warning("本地向量库加载失败，将重建: %s", exc)
                self._records, self._vectors, self._index = [], np.zeros((0, self.dim)), {}

    def _save(self) -> None:
        tmp_vectors = self.vectors_path.with_name("vectors.tmp.npy")
        tmp_records = self.records_path.with_suffix(".jsonl.tmp")
        with tmp_vectors.open("wb") as handle:
            np.save(handle, self._vectors)
        with tmp_records.open("w", encoding="utf-8") as handle:
            for record in self._records:
                handle.write(json.dumps(record.to_record(), ensure_ascii=False) + "\n")
        tmp_vectors.replace(self.vectors_path)
        tmp_records.replace(self.records_path)

    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不一致")
        if not chunks:
            return
        with self._lock:
            vectors = np.asarray(vectors, dtype=np.float32)
            for chunk, vector in zip(chunks, vectors):
                if chunk.chunk_id in self._index:
                    position = self._index[chunk.chunk_id]
                    self._records[position] = chunk
                    self._vectors[position] = vector
                else:
                    self._index[chunk.chunk_id] = len(self._records)
                    self._records.append(chunk)
                    self._vectors = np.vstack([self._vectors, vector.reshape(1, -1)])

    def search(
        self,
        vector: np.ndarray,
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        with self._lock:
            if not self._records:
                return []
            query = np.asarray(vector, dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(query))
            if norm > 0:
                query = query / norm
            scores = self._vectors @ query
            candidate_indices = np.argsort(scores)[::-1]
            hits: list[SearchHit] = []
            for position in candidate_indices:
                chunk = self._records[int(position)]
                if not match_filters(chunk, filters):
                    continue
                hits.append(
                    SearchHit(
                        chunk=chunk,
                        score=float(scores[int(position)]),
                        retriever="vector",
                        rank=len(hits) + 1,
                        components={"vector": float(scores[int(position)])},
                    )
                )
                if len(hits) >= top_k:
                    break
            return hits

    def clear(self) -> None:
        with self._lock:
            self._records = []
            self._vectors = np.zeros((0, self.dim), dtype=np.float32)
            self._index = {}
            for path in (self.vectors_path, self.records_path):
                if path.exists():
                    path.unlink()

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def flush(self) -> None:
        with self._lock:
            self._save()

    def iter_chunks(self) -> list[Chunk]:
        with self._lock:
            return list(self._records)


class MilvusVectorStore(BaseVectorStore):
    """Milvus 适配器：生产环境水平扩展向量检索。"""

    backend = "milvus"
    _OUTPUT_FIELDS = [
        "chunk_id", "doc_id", "text", "title", "section", "article", "page",
        "page_end", "parent_text", "doc_type", "product_code", "effective_date",
        "asset_class", "industry", "publish_date", "risk_level", "metadata",
    ]

    def __init__(self, cfg: Config, dim: int) -> None:
        try:
            from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("未安装 pymilvus，请执行 `pip install -e .[ml]`") from exc

        self._collection_cls = Collection
        self._schema_cls = CollectionSchema
        self._data_type = DataType
        self._field_schema = FieldSchema
        self._connections = connections
        self._utility = utility
        self.dim = dim
        self.collection_name = str(cfg.get("vector_store.collection", "huixiaoan_chunks"))
        self.metric = str(cfg.get("vector_store.metric", "IP"))
        self.index_type = str(cfg.get("vector_store.index_type", "HNSW"))
        self.consistency_level = str(cfg.get("vector_store.consistency_level", "Session"))
        self._connections.connect(
            alias="default",
            uri=str(cfg.get("vector_store.milvus_uri", "http://127.0.0.1:19530")),
            token=str(cfg.get("vector_store.milvus_token", "") or "") or None,
        )
        self.collection = self._ensure_collection()
        self.collection.load()

    def _ensure_collection(self):
        if self._utility.has_collection(self.collection_name):
            collection = self._collection_cls(self.collection_name)
            existing_dim = None
            for field in collection.schema.fields:
                if field.name == "vector":
                    existing_dim = field.params.get("dim")
            if existing_dim and int(existing_dim) != self.dim:
                raise ValueError(
                    f"Milvus collection 维度 {existing_dim} 与配置 {self.dim} 不一致，请更换 collection 名称"
                )
            return collection

        fields = [
            self._field_schema("chunk_id", self._data_type.VARCHAR, is_primary=True, max_length=64),
            self._field_schema("doc_id", self._data_type.VARCHAR, max_length=64),
            self._field_schema("text", self._data_type.VARCHAR, max_length=8192),
            self._field_schema("title", self._data_type.VARCHAR, max_length=512),
            self._field_schema("section", self._data_type.VARCHAR, max_length=1024),
            self._field_schema("article", self._data_type.VARCHAR, max_length=64),
            self._field_schema("page", self._data_type.INT64),
            self._field_schema("page_end", self._data_type.INT64),
            self._field_schema("parent_text", self._data_type.VARCHAR, max_length=16384),
            self._field_schema("doc_type", self._data_type.VARCHAR, max_length=64),
            self._field_schema("asset_class", self._data_type.VARCHAR, max_length=64),
            self._field_schema("industry", self._data_type.VARCHAR, max_length=64),
            self._field_schema("publish_date", self._data_type.VARCHAR, max_length=32),
            self._field_schema("risk_level", self._data_type.VARCHAR, max_length=16),
            self._field_schema("product_code", self._data_type.VARCHAR, max_length=128),
            self._field_schema("effective_date", self._data_type.VARCHAR, max_length=64),
            self._field_schema("metadata", self._data_type.JSON),
            self._field_schema("vector", self._data_type.FLOAT_VECTOR, dim=self.dim),
        ]
        schema = self._schema_cls(fields, description="小智财答金融知识库")
        collection = self._collection_cls(self.collection_name, schema=schema)
        index_params = {
            "metric_type": self.metric,
            "index_type": self.index_type,
            "params": {"M": 32, "efConstruction": 200},
        }
        collection.create_index(field_name="vector", index_params=index_params)
        return collection

    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        if not chunks:
            return
        vectors = np.asarray(vectors, dtype=np.float32)
        rows = [
            [
                chunk.chunk_id, chunk.doc_id, chunk.text[:8192], chunk.title[:512],
                chunk.section[:1024], chunk.article[:64], chunk.page, chunk.page_end,
                chunk.parent_text[:16384], chunk.doc_type, chunk.asset_class,
                chunk.industry, chunk.publish_date, chunk.risk_level,
                chunk.product_code, chunk.effective_date, chunk.metadata,
            ]
            for chunk in chunks
        ]
        self.collection.upsert(rows + [vectors.tolist()])

    def search(
        self,
        vector: np.ndarray,
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        from .filters import to_milvus_expr

        expr = to_milvus_expr(filters)
        results = self.collection.search(
            data=[np.asarray(vector, dtype=np.float32).tolist()],
            anns_field="vector",
            param={"metric_type": self.metric, "params": {"ef": 64}},
            limit=top_k,
            expr=expr or None,
            output_fields=self._OUTPUT_FIELDS,
        )
        hits: list[SearchHit] = []
        for rank, item in enumerate(results[0], start=1):
            entity = item.entity
            record = {
                "chunk_id": entity.get("chunk_id"),
                "doc_id": entity.get("doc_id"),
                "text": entity.get("text"),
                "title": entity.get("title"),
                "section": entity.get("section"),
                "article": entity.get("article"),
                "page": entity.get("page"),
                "page_end": entity.get("page_end"),
                "parent_text": entity.get("parent_text"),
                "doc_type": entity.get("doc_type"),
                "asset_class": entity.get("asset_class"),
                "industry": entity.get("industry"),
                "publish_date": entity.get("publish_date"),
                "risk_level": entity.get("risk_level"),
                "product_code": entity.get("product_code"),
                "effective_date": entity.get("effective_date"),
                "metadata": entity.get("metadata") or {},
            }
            hits.append(
                SearchHit(
                    chunk=Chunk.from_record(record),
                    score=float(item.score),
                    retriever="vector",
                    rank=rank,
                    components={"vector": float(item.score)},
                )
            )
        return hits

    def clear(self) -> None:
        if self._utility.has_collection(self.collection_name):
            self._utility.drop_collection(self.collection_name)
        self.collection = self._ensure_collection()
        self.collection.load()

    def count(self) -> int:
        try:
            return int(self.collection.num_entities)
        except Exception:
            return 0

    def flush(self) -> None:
        self.collection.flush()

    def close(self) -> None:
        try:
            self._connections.disconnect("default")
        except Exception:
            pass


def build_vector_store(cfg: Config, dim: int) -> BaseVectorStore:
    if bool(cfg.get("collections.enabled", True)):
        from .collection_store import RoutedVectorStore

        try:
            return RoutedVectorStore(cfg, dim)
        except Exception as exc:
            if not bool(cfg.get("resilience.vector_fallback_local", True)):
                raise
            logger.warning("Milvus/主向量库不可用，降级本地向量库: %s", exc)
            fallback_cfg = cfg.with_overrides({"vector_store": {"backend": "local"}})
            return RoutedVectorStore(fallback_cfg, dim)
    backend = str(cfg.get("vector_store.backend", "local")).lower()
    index_dir = cfg.path("index_dir", "data/index")
    if backend in {"milvus", "production"}:
        return MilvusVectorStore(cfg, dim)
    if backend == "local":
        return LocalVectorStore(index_dir=index_dir, dim=dim)
    raise ValueError(f"未知 vector_store.backend: {backend}")
