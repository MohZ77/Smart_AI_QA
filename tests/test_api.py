from __future__ import annotations

from fastapi.testclient import TestClient

from huixiaoan.service.app import create_app


def test_health_and_chat(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["vector_count"] > 0

        response = client.post(
            "/v1/chat",
            json={
                "question": "医疗险的免赔额是多少？",
                "session_id": "api-test",
                "profile": {"user_id": "u1", "risk_level": "C3"},
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["citations"]
        assert payload["compliance"]["passed"]


def test_websocket_stream(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/ws/chat") as websocket:
            websocket.send_json(
                {
                    "question": "平安福的等待期是多少天？",
                    "session_id": "ws-test",
                    "profile": {"user_id": "u1", "risk_level": "C3"},
                }
            )
            events = []
            while True:
                event = websocket.receive_json()
                events.append(event)
                if event["type"] in {"done", "error"}:
                    break
            assert events[0]["type"] == "meta"
            assert any(event["type"] == "delta" for event in events)
            assert events[-1]["type"] == "done"


def test_profile_questionnaire(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        questions = client.get("/v1/profile/questions")
        assert questions.status_code == 200
        assert len(questions.json()["questions"]) == 10
        answers = {
            item["id"]: 5 for item in questions.json()["questions"]
        }
        assessed = client.post(
            "/v1/profile/assess",
            json={"user_id": "u-profile", "answers": answers},
        )
        assert assessed.status_code == 200
        assert assessed.json()["risk_level"] == "C5"


def test_anonymous_profile_cannot_spoof_tenant(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={
                "question": "医疗险的年度免赔额是多少？",
                "profile": {
                    "user_id": "anonymous",
                    "risk_level": "C3",
                    "tenant_id": "victim-tenant",
                    "profile_version": 99,
                },
            },
        )
        assert response.status_code == 200
        profile = response.json()["debug"]["profile"]
        assert profile["tenant_id"] == "default"
        assert profile["profile_version"] == 0
