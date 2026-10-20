from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Page:
    number: int
    text: str


@dataclass
class ImageAsset:
    image_id: str
    doc_id: str
    page: int = 1
    alt: str = ""
    caption: str = ""
    mime: str = "image/png"
    ext: str = ".png"
    data: bytes = b""
    remote_url: str = ""


@dataclass
class RawDocument:
    doc_id: str
    title: str
    source_path: str
    doc_type: str = "other"
    asset_class: str = "other"
    industry: str = ""
    publish_date: str = ""
    risk_level: str = ""
    product_code: str = ""
    effective_date: str = ""
    pages: list[Page] = field(default_factory=list)
    images: list[ImageAsset] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    title: str
    section: str = ""
    article: str = ""
    page: int = 0
    page_end: int = 0
    parent_text: str = ""
    doc_type: str = "other"
    asset_class: str = "other"
    industry: str = ""
    publish_date: str = ""
    risk_level: str = ""
    product_code: str = ""
    effective_date: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "text": self.text,
            "title": self.title,
            "section": self.section,
            "article": self.article,
            "page": self.page,
            "page_end": self.page_end,
            "parent_text": self.parent_text,
            "doc_type": self.doc_type,
            "asset_class": self.asset_class,
            "industry": self.industry,
            "publish_date": self.publish_date,
            "risk_level": self.risk_level,
            "product_code": self.product_code,
            "effective_date": self.effective_date,
            "metadata": self.metadata,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "Chunk":
        known = {
            "chunk_id", "doc_id", "text", "title", "section", "article", "page",
            "page_end", "parent_text", "doc_type", "asset_class", "industry",
            "publish_date", "risk_level", "product_code", "effective_date", "metadata",
        }
        payload = {k: record.get(k, "") for k in known}
        payload["page"] = int(record.get("page") or 0)
        payload["page_end"] = int(record.get("page_end") or payload["page"])
        payload["metadata"] = record.get("metadata") or {}
        return cls(**payload)


@dataclass
class SearchHit:
    chunk: Chunk
    score: float = 0.0
    retriever: str = ""
    rank: int = 0
    components: dict[str, float] = field(default_factory=dict)


@dataclass
class Citation:
    index: int
    doc_id: str
    title: str
    doc_type: str = ""
    section: str = ""
    article: str = ""
    page: int = 0
    quote: str = ""
    source_path: str = ""
    source_url: str = ""
    images: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "doc_id": self.doc_id,
            "title": self.title,
            "doc_type": self.doc_type,
            "section": self.section,
            "article": self.article,
            "page": self.page,
            "quote": self.quote,
            "source_path": self.source_path,
            "source_url": self.source_url,
            "images": self.images,
        }


@dataclass
class UserProfile:
    user_id: str = "anonymous"
    risk_level: str = "C3"
    investment_experience: str = ""
    horizon: str = ""
    investable_assets: float | None = None
    profile_version: int = 0
    tenant_id: str = "default"

    @property
    def risk_rank(self) -> int:
        digits = "".join(ch for ch in self.risk_level if ch.isdigit())
        return int(digits) if digits else 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "risk_level": self.risk_level,
            "investment_experience": self.investment_experience,
            "horizon": self.horizon,
            "investable_assets": self.investable_assets,
            "profile_version": self.profile_version,
            "tenant_id": self.tenant_id,
        }


@dataclass
class IntentResult:
    name: str
    confidence: float
    entities: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class ComplianceResult:
    passed: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    action: str = "allow"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violations": self.violations,
            "warnings": self.warnings,
            "action": self.action,
            "details": self.details,
        }


@dataclass
class AnswerResult:
    answer: str
    citations: list[Citation]
    confidence: float
    intent: IntentResult
    strategy: str
    cached: bool = False
    cache_level: str = ""
    compliance: ComplianceResult | None = None
    latency_ms: float = 0.0
    request_id: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    visual: dict[str, Any] = field(default_factory=dict)
    debug: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": [c.to_dict() for c in self.citations],
            "confidence": round(self.confidence, 4),
            "intent": {
                "name": self.intent.name,
                "confidence": round(self.intent.confidence, 4),
                "entities": self.intent.entities,
            },
            "strategy": self.strategy,
            "cached": self.cached,
            "cache_level": self.cache_level,
            "compliance": self.compliance.to_dict() if self.compliance else None,
            "latency_ms": round(self.latency_ms, 2),
            "request_id": self.request_id,
            "model": self.model,
            "usage": self.usage,
            "visual": self.visual,
            "debug": self.debug,
        }
