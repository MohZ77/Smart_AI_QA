from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from huixiaoan.service.app import create_app


def _policy_bytes() -> bytes:
    root = Path(__file__).resolve().parents[1]
    return (root / "tests" / "fixtures" / "insurance-policy.md").read_bytes()


def test_rule_based_contract_review(container) -> None:
    result = container.documents.review(
        "insurance-policy.md",
        _policy_bytes(),
        "请检查免责、等待期、理赔和退保条款，并说明是否具备进一步审阅的条件",
    )
    assert result.citations
    assert "初步" in result.answer
    assert "等待期" in result.answer
    assert "责任免除" in result.answer or "免责" in result.answer


def test_file_chat_endpoint(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/file",
            files={
                "file": (
                    "insurance-policy.md",
                    _policy_bytes(),
                    "text/markdown",
                )
            },
            data={
                "question": "这份合同有哪些风险？",
                "profile_json": json.dumps(
                    {"user_id": "doc-user", "risk_level": "C3"}
                ),
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["citations"]
        assert "合同" in payload["answer"]
