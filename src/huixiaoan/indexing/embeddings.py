from __future__ import annotations

import hashlib
import logging
import os
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Iterable

import httpx
import numpy as np

from ..config import Config
from ..utils import tokenize

logger = logging.getLogger(__name__)


class BaseEmbedder(ABC):
    dim: int

    @abstractmethod
    def encode(self, texts: list[str]) -> np.ndarray:
        """返回 shape=(n, dim) 的 L2 归一化 float32 向量。"""

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode([text])[0]

    @property
    def name(self) -> str:
        return self.__class__.__name__


class HashingEmbedder(BaseEmbedder):
    """离线可用的确定性哈希向量。

    仅用于本地开发、单测和 CI；生产环境请使用 BGE-M3，避免语义表达能力不足。
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        features = tokenize(text)
        compact = "".join(text.split())
        features.extend(compact[i : i + 2] for i in range(max(0, len(compact) - 1)))
        features.extend(compact[i : i + 3] for i in range(max(0, len(compact) - 2)))
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * (1.0 + min(len(feature), 4) * 0.15)
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._vector(text) for text in texts]).astype(np.float32)


class BGEM3Embedder(BaseEmbedder):
    """BGE-M3 稠密向量，生产环境默认 1024 维。"""

    def __init__(self, model_name: str = "BAAI/bge-m3", device: str = "cpu", batch_size: int = 32):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 sentence-transformers，请执行 `pip install -e .[ml]`，"
                "或把 embedding.provider 设为 hashing 做离线演示。"
            ) from exc
        self.model = SentenceTransformer(model_name, device=device)
        self.dim = int(self.model.get_sentence_embedding_dimension() or 1024)
        self.batch_size = batch_size
        self._model_name = model_name

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vectors = self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)

    @property
    def name(self) -> str:
        return f"bge-m3:{self._model_name}"


class OpenAICompatibleEmbedder(BaseEmbedder):
    def __init__(
        self,
        api_base: str,
        model: str,
        api_key: str,
        dim: int = 1024,
        batch_size: int = 32,
        timeout_s: float = 30.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.dim = dim
        self.batch_size = batch_size
        self.timeout_s = timeout_s

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        vectors: list[list[float]] = []
        with httpx.Client(timeout=self.timeout_s) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                response = client.post(
                    f"{self.api_base}/embeddings",
                    headers=headers,
                    json={"model": self.model, "input": batch},
                )
                response.raise_for_status()
                payload = response.json()
                ordered = sorted(payload["data"], key=lambda item: item.get("index", 0))
                vectors.extend(item["embedding"] for item in ordered)
        array = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return array / norms

    @property
    def name(self) -> str:
        return f"openai-embedding:{self.model}"


class CachedEmbedder(BaseEmbedder):
    """进程内 LRU + 可选 Redis 的 Embedding 缓存，避免重复文本重复编码。"""

    def __init__(self, inner: BaseEmbedder, max_entries: int = 20000, redis_client=None) -> None:
        self.inner = inner
        self.dim = inner.dim
        self.max_entries = max(1, max_entries)
        self.redis_client = redis_client
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, text: str) -> str:
        return "emb:" + hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _get(self, key: str) -> np.ndarray | None:
        with self._lock:
            value = self._cache.get(key)
            if value is not None:
                self._cache.move_to_end(key)
                return value
        if self.redis_client is not None:
            try:
                raw = self.redis_client.get(key)
                if raw:
                    vector = np.frombuffer(raw, dtype=np.float32)
                    self._set_local(key, vector)
                    return vector
            except Exception as exc:  # pragma: no cover
                logger.warning("Embedding Redis 读取失败: %s", exc)
        return None

    def _set_local(self, key: str, vector: np.ndarray) -> None:
        with self._lock:
            self._cache[key] = vector
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        keys = [self._key(text) for text in texts]
        output: list[np.ndarray | None] = [self._get(key) for key in keys]
        missing = [i for i, value in enumerate(output) if value is None]
        if missing:
            computed = self.inner.encode([texts[i] for i in missing])
            for position, vector in zip(missing, computed):
                output[position] = vector
                self._set_local(keys[position], vector)
                if self.redis_client is not None:
                    try:
                        self.redis_client.set(
                            keys[position], vector.astype(np.float32).tobytes(), ex=86400
                        )
                    except Exception as exc:  # pragma: no cover
                        logger.warning("Embedding Redis 写入失败: %s", exc)
        return np.vstack(output).astype(np.float32)

    @property
    def name(self) -> str:
        return f"cached:{self.inner.name}"


def build_embedder(cfg: Config, redis_client=None) -> BaseEmbedder:
    provider = str(cfg.get("embedding.provider", "hashing")).lower()
    dim = int(cfg.get("embedding.dim", 1024))
    batch_size = int(cfg.get("embedding.batch_size", 32))

    if provider in {"hashing", "hash", "local-hash"}:
        inner: BaseEmbedder = HashingEmbedder(dim=dim)
    elif provider in {"bge-m3", "bgem3", "bge"}:
        inner = BGEM3Embedder(
            model_name=str(cfg.get("embedding.model", "BAAI/bge-m3")),
            device=str(cfg.get("embedding.device", "cpu")),
            batch_size=batch_size,
        )
    elif provider in {"openai", "openai-compatible"}:
        api_key = os.getenv(str(cfg.get("embedding.api_key_env", "")), "")
        inner = OpenAICompatibleEmbedder(
            api_base=str(cfg.get("embedding.api_base", "")),
            model=str(cfg.get("embedding.model", "")),
            api_key=api_key,
            dim=dim,
            batch_size=batch_size,
        )
    else:
        raise ValueError(f"未知 embedding.provider: {provider}")

    return CachedEmbedder(
        inner,
        max_entries=int(cfg.get("embedding.cache_size", 20000)),
        redis_client=redis_client,
    )
