from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .config import Config
from .utils import stable_id

logger = logging.getLogger(__name__)


@dataclass
class CacheStats:
    l1_hits: int = 0
    l2_hits: int = 0
    misses: int = 0
    sets: int = 0
    evictions: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "l1_hits": self.l1_hits,
            "l2_hits": self.l2_hits,
            "misses": self.misses,
            "sets": self.sets,
            "evictions": self.evictions,
        }


class TTLCache:
    def __init__(
        self,
        max_entries: int = 4096,
        ttl_s: float = 3600,
        clock=time.monotonic,
    ) -> None:
        self.max_entries = max(1, max_entries)
        self.ttl_s = max(0.001, ttl_s)
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at < self._clock():
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        with self._lock:
            self._data[key] = (self._clock() + (ttl_s or self.ttl_s), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class MultiLevelCache:
    """L1 进程内 TTL-LRU + L2 Redis。Redis 不可用时自动降级。"""

    def __init__(self, cfg: Config) -> None:
        self.enabled = bool(cfg.get("cache.enabled", True))
        self.namespace = str(cfg.get("cache.namespace", "huixiaoan"))
        self.ttl_s = int(cfg.get("cache.ttl_s", 3600))
        self.l1 = TTLCache(
            max_entries=int(cfg.get("cache.max_entries", 4096)), ttl_s=self.ttl_s
        )
        self.stats = CacheStats()
        self.redis = self._connect_redis(str(cfg.get("cache.redis_url", "") or ""))

    @staticmethod
    def _connect_redis(url: str):
        if not url:
            return None
        try:
            import redis

            client = redis.Redis.from_url(url, decode_responses=False, socket_timeout=0.5)
            client.ping()
            logger.info("Redis 缓存已连接")
            return client
        except Exception as exc:
            logger.warning("Redis 不可用，降级为进程内缓存: %s", exc)
            return None

    def key(self, *parts: Any) -> str:
        return f"{self.namespace}:" + stable_id(*parts, length=32)

    def get(self, key: str) -> tuple[Any | None, str]:
        if not self.enabled:
            return None, ""
        value = self.l1.get(key)
        if value is not None:
            self.stats.l1_hits += 1
            return value, "l1"
        if self.redis is not None:
            try:
                raw = self.redis.get(key)
                if raw:
                    value = json.loads(raw)
                    self.l1.set(key, value, self.ttl_s)
                    self.stats.l2_hits += 1
                    return value, "l2"
            except Exception as exc:  # pragma: no cover
                logger.warning("Redis 读取失败: %s", exc)
        self.stats.misses += 1
        return None, ""

    def set(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        if not self.enabled:
            return
        ttl = ttl_s or self.ttl_s
        self.l1.set(key, value, ttl)
        if self.redis is not None:
            try:
                self.redis.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
            except Exception as exc:  # pragma: no cover
                logger.warning("Redis 写入失败: %s", exc)
        self.stats.sets += 1

    def close(self) -> None:
        if self.redis is not None:
            try:
                self.redis.close()
            except Exception:
                pass
