from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from huixiaoan.service.app import create_app
from huixiaoan.types import UserProfile


def test_api_key_auth(container) -> None:
    old_key = container.cfg.data.get("security", {}).get("api_key", "")
    container.cfg.data.setdefault("security", {})["api_key"] = "test-key"
    app = create_app(container=container)
    try:
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert client.post("/v1/chat", json={"question": "基金怎么买？"}).status_code == 401
            ok = client.post(
                "/v1/chat",
                headers={"X-API-Key": "test-key"},
                json={"question": "基金怎么买？"},
            )
            assert ok.status_code == 200
    finally:
        container.cfg.data["security"]["api_key"] = old_key


def test_audit_is_written_after_request(container) -> None:
    result = asyncio.run(
        container.answerer.answer(
            "基金怎么买？",
            UserProfile(user_id="audit-user", risk_level="C3"),
            "audit-test",
        )
    )
    container.audit.flush()
    record = container.audit.get(result.request_id)
    assert record is not None
    assert record["request_id"] == result.request_id
    assert record["intent"] == "product_consultation"
    assert record["profile_snapshot"]["risk_level"] == "C3"
    assert record["profile_snapshot"]["profile_version"] == 0
    assert "investable_assets" not in record["profile_snapshot"]
