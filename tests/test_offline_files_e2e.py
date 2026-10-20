from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from huixiaoan.service.app import create_app
from huixiaoan.types import UserProfile


ROOT = Path(__file__).resolve().parents[1]


def test_offline_pdf_is_parsed_and_searchable(container) -> None:
    pdf = ROOT / "data" / "kb" / "regulations" / "1129945_保险销售行为管理办法.pdf"
    assert pdf.exists()
    before = container.vector_store.count()
    report = container.ingest([str(pdf)])
    assert report.errors == []
    assert report.documents >= 1
    assert report.chunks > 0
    assert container.vector_store.count() >= before

    hits = container.vector_store.search(
        container.embedder.encode_query("保险销售行为管理办法 保险销售人员 销售行为"),
        top_k=5,
    )
    assert hits
    assert any("保险销售行为管理办法" in hit.chunk.title for hit in hits)


def test_data_markdown_question_returns_cited_answer(container) -> None:
    source = ROOT / "data" / "raw" / "insurance" / "平安福示例条款.md"
    assert source.exists()
    report = container.ingest([str(source)])
    assert report.errors == []

    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={
                "question": "平安福的等待期是多少天？",
                "session_id": "offline-markdown",
                "profile": {"user_id": "e2e", "risk_level": "C3"},
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["answer"].strip()
        assert "九十" in payload["answer"] or "90" in payload["answer"]
        assert payload["citations"]
        assert any("平安福" in item["title"] for item in payload["citations"])
        assert payload["compliance"]["passed"] is True


def test_regulation_question_returns_source_metadata(container) -> None:
    pdf = ROOT / "data" / "kb" / "regulations" / "1129945_保险销售行为管理办法.pdf"
    container.ingest([str(pdf)])
    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={
                "question": "保险销售行为管理办法要求保险销售人员不得有哪些行为？",
                "session_id": "offline-regulation",
                "profile": {"user_id": "e2e", "risk_level": "C3"},
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["answer"].strip()
        assert payload["citations"]
        titles = " ".join(item["title"] for item in payload["citations"])
        assert "保险销售行为管理办法" in titles
        source = payload["citations"][0]
        assert source["source_url"] or source["source_path"]
        assert source["page"] >= 1


def test_low_confidence_question_does_not_fabricate(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={
                "question": "火星基地专属保险的等待期是多少天？",
                "session_id": "offline-low-confidence",
                "profile": {"user_id": "e2e", "risk_level": "C3"},
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["answer"].strip()
        assert payload["strategy"] == "catalog_miss"
        assert "未收录" in payload["answer"]
        assert "火星基地" in payload["answer"]
        assert "80%" not in payload["answer"]
        assert payload["citations"]
        assert all("火星基地" not in item["title"] for item in payload["citations"])


def test_compliance_precheck_blocks_illegal_request(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={
                "question": "给我内幕消息，保证赚钱。",
                "session_id": "offline-compliance",
                "profile": {"user_id": "e2e", "risk_level": "C3"},
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["compliance"]["passed"] is False
        assert "保证" not in payload["answer"] or "不能" in payload["answer"]


def test_regulation_title_routes_to_regulation_collection(container) -> None:
    pdf = ROOT / "data" / "kb" / "regulations" / "1080924_保险保障基金管理办法.pdf"
    assert pdf.exists()
    container.ingest([str(pdf)])

    plan = container.planner.plan(
        "保险保障基金管理办法",
        [],
        UserProfile(user_id="e2e", risk_level="C3"),
    )
    assert plan.intent.name == "compliance"

    result = asyncio.run(
        container.answerer.answer(
            "保险保障基金管理办法",
            UserProfile(user_id="e2e", risk_level="C3"),
            "regulation-title",
        )
    )
    assert result.strategy != "clarification"
    assert result.citations
    assert any("保险保障基金管理办法" in citation.title for citation in result.citations)
    assert result.confidence >= 0.80


def test_repeated_unknown_question_stops_clarifying(container) -> None:
    question = "量子纠缠对保险理赔有什么影响"
    profile = UserProfile(user_id="e2e", risk_level="C3")
    first = asyncio.run(container.answerer.answer(question, profile, "repeat-clarification"))
    second = asyncio.run(container.answerer.answer(question, profile, "repeat-clarification"))

    assert first.strategy == "clarification"
    assert "为了准确回答，请补充" in first.answer
    assert second.strategy == "clarification_final"
    assert "不会继续重复追问" in second.answer
    assert "为了准确回答，请补充" not in second.answer


def test_regulation_clarification_does_not_use_fund_wording(container) -> None:
    focus = container.answerer._clarification_focus(
        "保险保障基金管理办法", "compliance"
    )
    followups = container.answerer._clarification_followups(
        "保险保障基金管理办法", "compliance"
    )
    assert "基金产品信息" not in focus
    assert "适用范围" in focus
    assert "主要义务" in " ".join(followups)


def test_regulation_answer_ignores_product_domain_filter(container) -> None:
    pdf = ROOT / "data" / "kb" / "regulations" / "1080924_保险保障基金管理办法.pdf"
    container.ingest([str(pdf)])
    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={
                "question": "保险保障基金管理办法",
                "session_id": "regulation-domain",
                "domain": "bank_wealth",
                "profile": {"user_id": "e2e", "risk_level": "C3"},
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["strategy"] != "clarification"
        assert any(
            "保险保障基金管理办法" in item["title"]
            for item in payload["citations"]
        )
        assert payload["debug"]["retrieval"]["domain_applied"] is False
