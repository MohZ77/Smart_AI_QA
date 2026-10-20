from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from huixiaoan.service.app import create_app


ROOT = Path(__file__).resolve().parents[1]


def _register(client: TestClient) -> dict:
    response = client.post(
        "/v1/auth/register",
        json={
            "username": f"upload{uuid.uuid4().hex[:8]}",
            "password": "Upload2026",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_admin_upload_parses_file_into_vector_store(container) -> None:
    content = """---
asset_class: insurance
doc_type: insurance
risk_level: R2
publish_date: 2026-10-20
---

# 接口上传测试年金险

## 第一条 等待期

接口上传测试年金险的等待期为一百八十日，等待期内不承担保险责任。

## 第二条 风险提示

保险产品以合同条款为准，以上内容仅用于测试。
"""
    before = container.vector_store.count()
    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.post(
            "/v1/ingest/upload",
            files={
                "file": (
                    "uploaded-annuity.md",
                    content.encode("utf-8"),
                    "text/markdown",
                )
            },
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["task_id"]
        status = client.get(f"/v1/ingest/{task_id}")
        assert status.status_code == 200
        payload = status.json()
        assert payload["status"] == "completed", payload
        assert payload["report"]["chunks"] > 0
        assert container.vector_store.count() > before

    hits = container.vector_store.search(
        container.embedder.encode_query("接口上传测试年金险 等待期"),
        top_k=5,
    )
    assert hits
    assert any("接口上传测试年金险" in hit.chunk.title for hit in hits)


def test_authenticated_file_review_uses_account_identity(container) -> None:
    policy = (ROOT / "tests" / "fixtures" / "insurance-policy.md").read_bytes()
    app = create_app(container=container)
    with TestClient(app) as client:
        account = _register(client)
        response = client.post(
            "/v1/chat/file",
            headers={"Authorization": f"Bearer {account['access_token']}"},
            files={"file": ("insurance-policy.md", policy, "text/markdown")},
            data={
                "question": "请检查免责、等待期和退保条款",
                "session_id": "upload-review",
                "profile_json": json.dumps(
                    {"user_id": "spoofed", "risk_level": "C5"}
                ),
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["answer"].strip()
        assert payload["citations"]
        assert "等待期" in payload["answer"]


def test_frontend_contains_login_register_and_token_flow(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        html = response.text
        assert 'id="auth-overlay"' in html
        assert 'id="auth-form"' in html
        assert "登录 / 注册" in html
        assert "/v1/auth/" in html
        assert "Authorization" in html
        assert 'id="account-name"' in html
        assert "hasRounds" in html
        assert "emptyDraft" in html
        assert "暂无历史会话" in html
        assert "/v1/sessions/" in html
        assert "appendMessage(" not in html
        assert "const rendered = addMessage(message.role" in html


def test_login_attempts_are_rate_limited(container) -> None:
    old_rate = container.cfg.data.get("security", {}).get("login_rate_per_minute", 10)
    container.cfg.data.setdefault("security", {})["login_rate_per_minute"] = 2
    try:
        app = create_app(container=container)
        with TestClient(app) as client:
            payload = {"username": "missing-user", "password": "WrongPass2026"}
            assert client.post("/v1/auth/login", json=payload).status_code == 401
            assert client.post("/v1/auth/login", json=payload).status_code == 401
            spoofed = client.post(
                "/v1/auth/login",
                headers={"X-Forwarded-For": "203.0.113.9"},
                json=payload,
            )
            assert spoofed.status_code == 429
    finally:
        container.cfg.data["security"]["login_rate_per_minute"] = old_rate


def test_admin_upload_requires_admin_key_when_configured(container) -> None:
    security = container.cfg.data.setdefault("security", {})
    old_key = security.get("admin_api_key", "")
    security["admin_api_key"] = "admin-secret"
    try:
        app = create_app(container=container)
        with TestClient(app) as client:
            files = {
                "file": (
                    "admin-guard.md",
                    b"# admin guard test\n\n## section\n\ncontent",
                    "text/markdown",
                )
            }
            denied = client.post("/v1/ingest/upload", files=files)
            assert denied.status_code == 403

            allowed = client.post(
                "/v1/ingest/upload",
                headers={"X-Admin-Key": "admin-secret"},
                files={
                    "file": (
                        "admin-guard.md",
                        b"# admin guard test\n\n## section\n\ncontent",
                        "text/markdown",
                    )
                },
            )
            assert allowed.status_code == 200, allowed.text
    finally:
        security["admin_api_key"] = old_key
