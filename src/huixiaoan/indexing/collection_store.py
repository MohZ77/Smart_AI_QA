from __future__ import annotations

import logging
from typing import Any

import numpy as np

from ..config import Config
from ..types import Chunk, SearchHit
from .vector_store import BaseVectorStore, LocalVectorStore, MilvusVectorStore

logger = logging.getLogger(__name__)

DEFAULT_COLLECTIONS = ["equity", "fund", "insurance", "bond", "bank_wealth", "macro"]


class RoutedVectorStore(BaseVectorStore):
    """按资产类别隔离的 Collection 路由层。

    - local 后端：每个 Collection 一个独立目录；
    - milvus 后端：每个 Collection 独立 collection，支持独立扩容与索引参数。
    """

    def __init__(self, cfg: Config, dim: int) -> None:
        self.cfg = cfg
        self.dim = dim
        self.names = list(cfg.get("collections.names", DEFAULT_COLLECTIONS))
        backend = str(cfg.get("vector_store.backend", "local")).lower()
        self.inner_backend = "milvus" if backend in {"milvus", "production"} else "local"
        self.backend = f"multi-{self.inner_backend}"
        self.stores: dict[str, BaseVectorStore] = {}

        if self.inner_backend == "local":
            base_dir = cfg.path("index_dir", "data/index") / "collections"
            for name in self.names:
                self.stores[name] = LocalVectorStore(base_dir / name, dim)
        else:
            prefix = str(cfg.get("collections.milvus_prefix", "huixiaoan"))
            for name in self.names:
                child_cfg = cfg.with_overrides(
                    {"vector_store": {"collection": f"{prefix}_{name}"}}
                )
                self.stores[name] = MilvusVectorStore(child_cfg, dim)

    def route(self, chunk: Chunk) -> str:
        name = chunk.asset_class or "macro"
        if name not in self.stores:
            # 未知资产类别进入宏观库，避免写入失败；metadata 仍保留原始类别。
            return "macro"
        return name

    def store_for(self, name: str) -> BaseVectorStore:
        return self.stores.get(name) or self.stores["macro"]

    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不一致")
        groups: dict[str, list[int]] = {}
        for index, chunk in enumerate(chunks):
            groups.setdefault(self.route(chunk), []).append(index)
        for name, indices in groups.items():
            group_chunks = [chunks[i] for i in indices]
            group_vectors = np.asarray([vectors[i] for i in indices], dtype=np.float32)
            self.store_for(name).upsert(group_chunks, group_vectors)

    def search(
        self,
        vector: np.ndarray,
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        # 兼容单库调用：搜索所有 Collection。
        return self.search_collections(list(self.stores), vector, top_k, filters)

    def search_collections(
        self,
        collections: list[str] | None,
        vector: np.ndarray,
        top_k: int = 30,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        targets = collections or list(self.stores)
        hits: list[SearchHit] = []
        for name in targets:
            store = self.stores.get(name)
            if store is None:
                continue
            for hit in store.search(vector, top_k, filters):
                hit.components["collection"] = name
                hits.append(hit)
        hits.sort(key=lambda item: item.score, reverse=True)
        for rank, hit in enumerate(hits, start=1):
            hit.rank = rank
        return hits[:top_k]

    def clear(self) -> None:
        for store in self.stores.values():
            store.clear()

    def count(self) -> int:
        return sum(store.count() for store in self.stores.values())

    def flush(self) -> None:
        for store in self.stores.values():
            store.flush()

    def close(self) -> None:
        for store in self.stores.values():
            store.close()

    def breakdown(self) -> dict[str, int]:
        return {name: store.count() for name, store in self.stores.items()}
