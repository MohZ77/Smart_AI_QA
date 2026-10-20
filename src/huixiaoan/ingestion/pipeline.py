from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..config import Config
from ..indexing.embeddings import BaseEmbedder
from ..indexing.images import ImageStore
from ..indexing.vector_store import BaseVectorStore
from ..types import Chunk
from ..utils import Timer, mask_pii, stable_id
from .chunking import StructureChunker
from .loaders import discover_files, load_documents

logger = logging.getLogger(__name__)


@dataclass
class IngestionReport:
    files: int = 0
    documents: int = 0
    chunks: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    doc_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "files": self.files,
            "documents": self.documents,
            "chunks": self.chunks,
            "skipped": self.skipped,
            "errors": self.errors,
            "elapsed_s": round(self.elapsed_s, 3),
            "doc_ids": self.doc_ids,
        }


class IngestionPipeline:
    """离线索引流水线：解析 -> 清洗 -> 分块 -> Embedding -> 向量库 + BM25 索引。"""

    def __init__(
        self,
        cfg: Config,
        embedder: BaseEmbedder,
        vector_store: BaseVectorStore,
        chunks_path: Path,
        image_store: ImageStore | None = None,
    ) -> None:
        self.cfg = cfg
        self.embedder = embedder
        self.vector_store = vector_store
        self.chunks_path = chunks_path
        self.image_store = image_store
        self.chunkers = {
            name: self._make_chunker(name, cfg)
            for name in ("default", "research", "fund", "insurance")
        }
        self.chunker = self.chunkers["default"]
        self.batch_size = int(cfg.get("ingestion.batch_size", 64))

    @staticmethod
    def _make_chunker(name: str, cfg: Config) -> StructureChunker:
        prefix = f"ingestion.profiles.{name}"
        return StructureChunker(
            max_chars=int(cfg.get(f"{prefix}.max_chars", cfg.get("ingestion.max_chars", 520))),
            min_chars=int(cfg.get(f"{prefix}.min_chars", cfg.get("ingestion.min_chars", 120))),
            overlap_chars=int(cfg.get(f"{prefix}.overlap_chars", cfg.get("ingestion.overlap_chars", 100))),
            parent_max_chars=int(cfg.get(f"{prefix}.parent_max_chars", cfg.get("ingestion.parent_max_chars", 1600))),
            table_max_chars=int(cfg.get("ingestion.table_max_chars", 1800)),
            pii_masking=bool(cfg.get("ingestion.pii_masking", True)),
        )

    def run(self, paths: Iterable[str | Path], rebuild: bool = False) -> IngestionReport:
        started = time.perf_counter()
        report = IngestionReport()
        files = []
        for path in paths:
            files.extend(discover_files(path))
        report.files = len(files)
        if not files:
            report.errors.append("未发现可解析的 PDF/DOCX/MD/TXT/HTML/CSV/XLSX 文件")
            report.elapsed_s = time.perf_counter() - started
            return report

        if rebuild:
            self._clear_index()

        documents, errors = load_documents(files, self.cfg)
        report.errors.extend(errors)
        report.documents = len(documents)
        all_chunks: list[Chunk] = []
        for doc in documents:
            chunker = self.chunkers.get(doc.doc_type, self.chunker)
            for page in doc.pages:
                if chunker.pii_masking:
                    page.text = mask_pii(page.text)
            try:
                chunks = chunker.chunk_document(doc)
            except Exception as exc:
                logger.exception("分块失败: %s", doc.source_path)
                report.errors.append(f"{doc.source_path}: {exc}")
                continue
            if not chunks:
                report.skipped += 1
                continue
            self._attach_images(doc, chunks)
            all_chunks.extend(chunks)
            report.doc_ids.append(doc.doc_id)

        if not all_chunks:
            report.elapsed_s = time.perf_counter() - started
            return report

        for start in range(0, len(all_chunks), self.batch_size):
            batch = all_chunks[start : start + self.batch_size]
            vectors = self.embedder.encode([c.text for c in batch])
            self.vector_store.upsert(batch, vectors)
        self.vector_store.flush()

        self._persist_chunks(all_chunks)
        report.chunks = len(all_chunks)
        report.elapsed_s = time.perf_counter() - started
        logger.info(
            "索引完成: %s 个文档, %s 个 chunk, 耗时 %.1fs",
            report.documents,
            report.chunks,
            report.elapsed_s,
        )
        return report

    def _attach_images(self, doc, chunks: list[Chunk]) -> None:
        if self.image_store is None or not doc.images:
            return
        records = {
            asset.image_id: self.image_store.save(asset) for asset in doc.images
        }
        for chunk in chunks:
            image_ids = chunk.metadata.get("image_ids", [])
            chunk.metadata["images"] = [
                {
                    key: records[image_id].get(key)
                    for key in ("id", "url", "alt", "caption", "page", "mime")
                }
                for image_id in image_ids
                if image_id in records
            ]

    def _clear_index(self) -> None:
        self.vector_store.clear()
        if self.chunks_path.exists():
            self.chunks_path.unlink()

    def _persist_chunks(self, new_chunks: list[Chunk]) -> None:
        """维护全量 chunks.jsonl，供 BM25 重建与离线评测使用。"""
        self.chunks_path.parent.mkdir(parents=True, exist_ok=True)
        replaced_docs = {c.doc_id for c in new_chunks}
        existing: list[dict] = []
        if self.chunks_path.exists():
            with self.chunks_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("doc_id") not in replaced_docs:
                        existing.append(record)
        with self.chunks_path.open("w", encoding="utf-8") as handle:
            for record in existing:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            for chunk in new_chunks:
                handle.write(json.dumps(chunk.to_record(), ensure_ascii=False) + "\n")


def load_chunks(path: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    if not path.exists():
        return chunks
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                chunks.append(Chunk.from_record(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                logger.warning("跳过损坏的 chunk 记录")
    return chunks
