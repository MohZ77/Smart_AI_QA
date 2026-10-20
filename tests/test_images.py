from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from huixiaoan.config import load_config
from huixiaoan.ingestion.chunking import StructureChunker
from huixiaoan.ingestion.loaders import load_document
from huixiaoan.service.app import create_app


def test_markdown_image_is_extracted_and_chunk_linked() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "configs" / "default.yaml")
    doc = load_document(root / "data" / "raw" / "insurance" / "平安福示例条款.md", cfg)
    assert doc.images
    assert doc.images[0].alt == "等待期示意图"
    assert doc.images[0].data
    chunks = StructureChunker().chunk_document(doc)
    linked = [chunk for chunk in chunks if chunk.metadata.get("image_ids")]
    assert linked
    assert linked[0].metadata["image_ids"] == [doc.images[0].image_id]


def test_citation_contains_image_and_image_endpoint(container) -> None:
    import asyncio

    from huixiaoan.types import UserProfile

    result = asyncio.run(
        container.answerer.answer(
            "平安福的等待期是多少天？",
            UserProfile(user_id="test", risk_level="C3"),
            "test-image-answer",
        )
    )
    images = [image for citation in result.citations for image in citation.images]
    assert images
    assert "![等待期示意图]" in result.answer

    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.get(images[0]["url"])
        assert response.status_code == 200
        assert "svg" in response.headers["content-type"]
