from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from huixiaoan.service.app import create_app


def _register(client: TestClient, prefix: str = "user") -> dict:
    username = f"{prefix}{uuid.uuid4().hex[:10]}"
    response = client.post(
        "/v1/auth/register",
        json={
            "username": username,
            "password": "FinTest2026",
            "display_name": "测试用户",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    payload["_username"] = username
    return payload


def test_register_login_me_and_validation(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        account = _register(client)
        assert account["token_type"] == "bearer"
        assert account["access_token"]
        assert account["user"]["risk_level"] == "C3"
        assert account["user"]["profile_version"] == 1
        assert account["user"]["display_name"] == "测试用户"

        duplicate = client.post(
            "/v1/auth/register",
            json={"username": account["_username"], "password": "FinTest2026"},
        )
        assert duplicate.status_code == 409

        weak = client.post(
            "/v1/auth/register",
            json={
                "username": f"weak{uuid.uuid4().hex[:8]}",
                "password": "12345678",
            },
        )
        assert weak.status_code == 400

        bad_login = client.post(
            "/v1/auth/login",
            json={"username": account["_username"], "password": "WrongPass2026"},
        )
        assert bad_login.status_code == 401
        assert bad_login.json()["detail"] == "用户名或密码错误"

        login = client.post(
            "/v1/auth/login",
            json={"username": account["_username"], "password": "FinTest2026"},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]

        me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["user"]["id"] == account["user"]["id"]

        invalid = client.get(
            "/v1/auth/me", headers={"Authorization": "Bearer v1.invalid.signature"}
        )
        assert invalid.status_code == 401


def test_password_is_hashed_and_expired_token_is_rejected(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        account = _register(client, "hash")
        row = container.auth._db.query_one(
            "SELECT password_hash FROM users WHERE id = ?", (account["user"]["id"],)
        )
        assert row is not None
        assert row["password_hash"].startswith("pbkdf2_sha256$")
        assert "FinTest2026" not in row["password_hash"]

        user = container.auth.get_user(account["user"]["id"])
        assert user is not None
        expired = container.auth.create_token(user, ttl_seconds=-1)
        response = client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {expired}"}
        )
        assert response.status_code == 401


def test_server_identity_and_risk_profile_cannot_be_spoofed(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        account = _register(client, "spoof")
        token = account["access_token"]
        response = client.post(
            "/v1/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "question": "医疗险的免赔额是多少？",
                "session_id": "spoof-check",
                "profile": {"user_id": "victim", "risk_level": "C1"},
            },
        )
        assert response.status_code == 200, response.text
        container.audit.flush()
        record = container.audit.get(response.json()["request_id"])
        assert record is not None
        assert record["user_id"] == account["user"]["id"]
        assert record["session_id"].startswith(f"u:{account['user']['id']}:")


def test_profile_assessment_persists_to_account(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        account = _register(client, "profile")
        token = account["access_token"]
        questions = client.get("/v1/profile/questions").json()["questions"]
        answers = {item["id"]: 5 for item in questions}
        assessed = client.post(
            "/v1/profile/assess",
            headers={"Authorization": f"Bearer {token}"},
            json={"user_id": "spoofed", "answers": answers},
        )
        assert assessed.status_code == 200
        assert assessed.json()["risk_level"] == "C5"
        assert assessed.json()["profile"]["profile_version"] == 2

        me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["user"]["risk_level"] == "C5"
        assert me.json()["user"]["profile_version"] == 2
        assert me.json()["user"]["id"] == account["user"]["id"]


def test_same_session_id_is_isolated_between_users(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        first = _register(client, "isola")
        second = _register(client, "isolb")
        for account, question in (
            (first, "基金怎么买？"),
            (second, "保险等待期是什么意思？"),
        ):
            response = client.post(
                "/v1/chat",
                headers={"Authorization": f"Bearer {account['access_token']}"},
                json={
                    "question": question,
                    "session_id": "shared-name",
                    "profile": {"user_id": "someone-else", "risk_level": "C3"},
                },
            )
            assert response.status_code == 200, response.text

        rows = container.memory._db.query(
            "SELECT DISTINCT session_id, user_id FROM messages WHERE session_id LIKE ?",
            ("%shared-name",),
        )
        scoped = {
            (row["session_id"], row["user_id"])
            for row in rows
            if row["user_id"] in {first["user"]["id"], second["user"]["id"]}
        }
        assert (
            f"u:{first['user']['id']}:shared-name",
            first["user"]["id"],
        ) in scoped
        assert (
            f"u:{second['user']['id']}:shared-name",
            second["user"]["id"],
        ) in scoped


def test_invalid_token_is_rejected_even_in_anonymous_mode(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            headers={"Authorization": "Bearer v1.bad.token"},
            json={"question": "基金怎么买？"},
        )
        assert response.status_code == 401


def test_streaming_chat_accepts_bearer_token(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        account = _register(client, "stream")
        response = client.post(
            "/v1/chat/stream",
            headers={"Authorization": f"Bearer {account['access_token']}"},
            json={
                "question": "基金怎么买？",
                "session_id": "stream-auth",
                "profile": {"user_id": "spoofed", "risk_level": "C3"},
            },
        )
        assert response.status_code == 200
        assert "event:" in response.text
        assert "done" in response.text


def test_websocket_rejects_invalid_token(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect("/v1/ws/chat?token=v1.bad.token") as ws:
                ws.receive_json()
        assert caught.value.code == 1008


def test_require_auth_for_chat_can_be_enabled(container) -> None:
    security = container.cfg.data.setdefault("security", {})
    old_value = security.get("require_auth_for_chat", False)
    security["require_auth_for_chat"] = True
    try:
        app = create_app(container=container)
        with TestClient(app) as client:
            anonymous = client.post(
                "/v1/chat", json={"question": "基金怎么买？"}
            )
            assert anonymous.status_code == 401

            account = _register(client, "required")
            authenticated = client.post(
                "/v1/chat",
                headers={"Authorization": f"Bearer {account['access_token']}"},
                json={"question": "基金怎么买？"},
            )
            assert authenticated.status_code == 200
    finally:
        security["require_auth_for_chat"] = old_value


def test_delete_session_clears_server_side_memory(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        account = _register(client, "clearsess")
        headers = {"Authorization": f"Bearer {account['access_token']}"}
        chat = client.post(
            "/v1/chat",
            headers=headers,
            json={"question": "基金怎么买？", "session_id": "clear-me"},
        )
        assert chat.status_code == 200, chat.text
        scoped = f"u:{account['user']['id']}:clear-me"
        assert container.memory.recent(scoped, 5)

        cleared = client.delete("/v1/sessions/clear-me", headers=headers)
        assert cleared.status_code == 200
        assert cleared.json()["status"] == "cleared"
        assert container.memory.recent(scoped, 5) == []

        # 幂等：重复清理同一会话不应报错
        assert client.delete("/v1/sessions/clear-me", headers=headers).status_code == 200
