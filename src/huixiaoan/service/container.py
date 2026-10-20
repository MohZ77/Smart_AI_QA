from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ..audit import AuditStore, BufferedAuditStore
from ..auth import AuthService
from ..cache import MultiLevelCache
from ..compliance.rules import ComplianceChecker
from ..config import Config, setup_logging
from ..documents import DocumentReviewService
from ..generation.answerer import Answerer
from ..generation.llm import build_llm
from ..indexing.bm25 import BM25Index
from ..indexing.embeddings import build_embedder
from ..indexing.faq import FAQStore
from ..indexing.images import ImageStore
from ..indexing.vector_store import build_vector_store
from ..ingestion.pipeline import IngestionPipeline, IngestionReport, load_chunks
from ..memory import ConversationStore
from ..query.understanding import QueryPlanner
from ..retrieval.hybrid import HybridRetriever
from ..retrieval.rerank import build_reranker
from ..utils import stable_id
from ..vision import ImageUnderstandingService

logger = logging.getLogger(__name__)


class Container:
    """依赖装配与生命周期管理。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        setup_logging(
            str(cfg.get("observability.log_level", "INFO")),
            bool(cfg.get("observability.log_json", False)),
            log_file=self._log_file(cfg),
        )
        self.cache = MultiLevelCache(cfg)
        self.embedder = build_embedder(cfg, self.cache.redis)
        self.vector_store = build_vector_store(cfg, self.embedder.dim)
        self.index_dir = cfg.path("index_dir", "data/index")
        self.image_store = ImageStore(cfg.path("image_dir", "data/images"))
        self.vision = ImageUnderstandingService(cfg, self.image_store)
        self.chunks_path = self.index_dir / "chunks.jsonl"
        self.bm25_path = self.index_dir / "bm25.json"
        self.faq = self._open_faq()
        self.memory = self._open_memory()
        self.auth = self._open_auth()
        audit_store = self._open_audit()
        self.audit = (
            BufferedAuditStore(audit_store, cfg)
            if bool(cfg.get("resilience.audit_async", True))
            else audit_store
        )
        self.llm = build_llm(cfg)
        self.reranker = build_reranker(cfg, cfg.path("domain_dict", "configs/domain_dict.txt"))

        self.bm25: BM25Index | None = None
        self._load_or_build_bm25()
        self.catalog = self._load_catalog()
        self.retriever = HybridRetriever(
            cfg, self.embedder, self.vector_store, self.bm25, self.reranker
        )
        self.planner = QueryPlanner(cfg, self.llm)
        self.compliance = ComplianceChecker(cfg, self.llm)
        self.documents = DocumentReviewService(cfg, self.llm, self.compliance)
        self.pipeline = IngestionPipeline(
            cfg,
            self.embedder,
            self.vector_store,
            self.chunks_path,
            image_store=self.image_store,
        )
        self.kb_version = self._kb_version()
        self.answerer = Answerer(
            cfg=cfg,
            planner=self.planner,
            retriever=self.retriever,
            llm=self.llm,
            compliance=self.compliance,
            cache=self.cache,
            faq=self.faq,
            memory=self.memory,
            audit=self.audit,
            kb_version=self.kb_version,
            catalog=self.catalog,
        )
        self.bootstrap()

    def _fallback_path(self, name: str) -> str:
        path = self.cfg.path("data_dir", "data") / "audit" / f"{name}-fallback.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    @staticmethod
    def _log_file(cfg: Config) -> str | None:
        """observability.log_file：相对路径按项目根解析，留空表示关闭文件日志。"""
        raw = str(cfg.get("observability.log_file", "") or "").strip()
        if not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (cfg.base_dir / candidate).resolve()
        return str(candidate)

    def _open_faq(self) -> FAQStore:
        try:
            return FAQStore(
                self.cfg.db("faq_db", "data/audit/faq.db"),
                threshold=float(self.cfg.get("faq.threshold", 0.85)),
                redis_client=self.cache.redis,
                redis_top_n=int(self.cfg.get("faq.redis_top_n", 3000)),
                redis_ttl_s=int(self.cfg.get("faq.redis_ttl_s", 21600)),
            )
        except Exception as exc:
            logger.warning("FAQ 主库不可用，降级本地 SQLite: %s", exc)
            return FAQStore(self._fallback_path("faq"), redis_client=self.cache.redis)

    def _open_memory(self) -> ConversationStore:
        try:
            return ConversationStore(self.cfg.db("memory_db", "data/audit/memory.db"))
        except Exception as exc:
            logger.warning("会话主库不可用，降级本地 SQLite: %s", exc)
            return ConversationStore(self._fallback_path("memory"))

    def _open_audit(self) -> AuditStore:
        try:
            return AuditStore(self.cfg.db("audit_db", "data/audit/audit.db"))
        except Exception as exc:
            logger.warning("审计主库不可用，降级本地 SQLite: %s", exc)
            return AuditStore(self._fallback_path("audit"))

    def _open_auth(self) -> AuthService:
        try:
            return AuthService(
                self.cfg.db("auth_db", "data/audit/auth.db"),
                secret=str(self.cfg.get("security.token_secret", "") or ""),
                token_ttl_seconds=int(self.cfg.get("security.token_ttl_hours", 8)) * 3600,
                profile_cache_ttl_s=float(
                    self.cfg.get("security.profile_cache_ttl_s", 30)
                ),
                tenant_id=str(self.cfg.get("security.default_tenant_id", "default")),
            )
        except Exception as exc:
            logger.warning("账号主库不可用，降级本地 SQLite: %s", exc)
            return AuthService(
                self._fallback_path("auth"),
                secret=str(self.cfg.get("security.token_secret", "") or ""),
                token_ttl_seconds=int(self.cfg.get("security.token_ttl_hours", 8)) * 3600,
                profile_cache_ttl_s=float(
                    self.cfg.get("security.profile_cache_ttl_s", 30)
                ),
                tenant_id=str(self.cfg.get("security.default_tenant_id", "default")),
            )

    def _load_or_build_bm25(self) -> None:
        chunks = load_chunks(self.chunks_path)
        if self.bm25_path.exists() and len(chunks) > 0:
            try:
                self.bm25 = BM25Index.load(
                    self.bm25_path, self.cfg.path("domain_dict", "configs/domain_dict.txt")
                )
                fresh = self.bm25_path.stat().st_mtime >= self.chunks_path.stat().st_mtime
                if len(self.bm25.chunks) == len(chunks) and fresh:
                    logger.info("BM25 索引已加载: %s 条", len(chunks))
                    return
            except Exception as exc:
                logger.warning("BM25 索引加载失败，将重建: %s", exc)
        if chunks:
            self.bm25 = BM25Index(
                dict_path=self.cfg.path("domain_dict", "configs/domain_dict.txt")
            ).build(chunks)
            self.bm25.save(self.bm25_path)
            logger.info("BM25 索引已重建: %s 条", len(chunks))

    def bootstrap(self) -> None:
        try:
            pruned = self.memory.prune_older_than(
                int(self.cfg.get("resilience.memory_retention_days", 90))
            )
            if pruned:
                logger.info("会话记忆清理旧记录: %s 条", pruned)
        except Exception as exc:
            logger.warning("会话记忆清理失败: %s", exc)
        if bool(self.cfg.get("bootstrap.seed_faq", True)):
            faq_file = self.cfg.path("raw_dir", "data/raw") / "faq" / "faq.jsonl"
            if faq_file.exists():
                from ..ingestion.loaders import load_faq_jsonl

                count = self.faq.add_many(load_faq_jsonl(faq_file))
                logger.info("FAQ 已同步: %s 条", count)

        if (
            bool(self.cfg.get("bootstrap.auto_ingest", True))
            and self.vector_store.count() == 0
        ):
            raw_dir = self.cfg.path("raw_dir", "data/raw")
            kb_dir = self.cfg.path("data_dir", "data") / "kb"
            paths = [raw_dir]
            paths.extend(
                kb_dir / name
                for name in ("regulations", "products", "research")
                if (kb_dir / name).exists()
            )
            paths = [path for path in paths if path.exists()]
            if paths:
                logger.info("向量库为空，自动索引语料目录: %s", [str(p) for p in paths])
                report = self.ingest([str(path) for path in paths], rebuild=False)
                logger.info("自动索引完成: %s", report.to_dict())

    def ingest(self, paths: list[str], rebuild: bool = False) -> IngestionReport:
        report = self.pipeline.run(paths, rebuild=rebuild)
        chunks = load_chunks(self.chunks_path)
        self.catalog = self._catalog_from_chunks(chunks)
        self.bm25 = BM25Index(
            dict_path=self.cfg.path("domain_dict", "configs/domain_dict.txt")
        ).build(chunks)
        self.bm25.save(self.bm25_path)
        self.retriever.update_bm25(self.bm25)
        self.kb_version = self._kb_version()
        self.answerer.kb_version = self.kb_version
        self.answerer.catalog = self.catalog
        return report

    def _load_catalog(self) -> list[dict[str, Any]]:
        return self._catalog_from_chunks(load_chunks(self.chunks_path))

    @staticmethod
    def _catalog_from_chunks(chunks) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for chunk in chunks:
            if chunk.doc_id in seen:
                continue
            seen[chunk.doc_id] = {
                "doc_id": chunk.doc_id,
                "title": chunk.title,
                "doc_type": chunk.doc_type,
                "asset_class": chunk.asset_class,
                "product_code": chunk.product_code,
                "risk_level": chunk.risk_level,
                "source_path": str(chunk.metadata.get("source_path", "")),
                "source_url": str(chunk.metadata.get("source_url", "")),
            }
        return sorted(
            seen.values(),
            key=lambda item: (item["asset_class"], item["title"]),
        )

    def _kb_version(self) -> str:
        try:
            stat = self.bm25_path.stat()
            return stable_id(len(self.bm25.chunks) if self.bm25 else 0, stat.st_size, stat.st_mtime, length=12)
        except OSError:
            return "empty"

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "vector_backend": self.vector_store.backend,
            "vector_count": self.vector_store.count(),
            "collections": (
                self.vector_store.breakdown()
                if hasattr(self.vector_store, "breakdown")
                else {}
            ),
            "bm25_count": len(self.bm25.chunks) if self.bm25 else 0,
            "faq_count": self.faq.count(),
            "user_count": self.auth.count(),
            "image_count": self.image_store.count(),
            "vision": self.vision.health(),
            "document_review": {"max_file_mb": self.documents.max_bytes // 1024 // 1024},
            "llm": self.llm.model_name,
            "embedding": self.embedder.name,
            "kb_version": self.kb_version,
            "cache": {
                "redis": self.cache.redis is not None,
                **self.cache.stats.to_dict(),
            },
            "timestamp": time.time(),
        }

    def close(self) -> None:
        for resource in (
            self.faq,
            self.memory,
            self.auth,
            self.audit,
            self.vector_store,
            self.cache,
        ):
            try:
                resource.close()
            except Exception:
                pass
