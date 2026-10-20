from __future__ import annotations

import base64
import asyncio

from fastapi.testclient import TestClient

from huixiaoan.service.app import create_app
from huixiaoan.types import UserProfile
from huixiaoan.vision import VisionResult

TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Wl2n1kAAAAASUVORK5CYII="
)


def test_offline_vision_is_explicit(container) -> None:
    result, record = container.vision.recognize(
        base64.b64decode(TINY_PNG), "image/png"
    )
    assert result.ok is False
    assert result.error
    assert record["url"].startswith("/v1/images/")


def test_image_chat_returns_visual_failure_instead_of_hallucinating(container) -> None:
    app = create_app(container=container)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={
                "question": "这张图片里是什么产品？",
                "session_id": "vision-test",
                "profile": {"user_id": "u1", "risk_level": "C3"},
                "image_base64": TINY_PNG,
                "image_mime": "image/png",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["visual"]["ok"] is False
        assert payload["citations"] == []
        assert "图片识别失败" in payload["answer"]
        image = client.get(payload["visual"]["image"]["url"])
        assert image.status_code == 200


def test_recognized_image_text_enters_answer_pipeline(container) -> None:
    class FakeVision:
        def recognize(self, image_bytes, mime):
            return VisionResult(
                ok=True,
                text="图片中的基金为慧选消费主题基金，风险等级 R3。",
                provider="fake-vision",
            )

    original = container.vision.client
    container.vision.client = FakeVision()
    try:
        visual_result, image_record = container.vision.recognize(
            base64.b64decode(TINY_PNG), "image/png"
        )
        result = asyncio.run(
            container.answerer.answer(
                "图片里的基金风险等级是什么？",
                UserProfile(user_id="vision-user", risk_level="C3"),
                "vision-recognized",
                image_context=visual_result.text,
                visual={**visual_result.to_dict(), "image": image_record},
            )
        )
    finally:
        container.vision.client = original
    assert result.visual["ok"] is True
    assert result.visual["provider"] == "fake-vision"
    assert result.citations


def test_online_llm_fallback_when_knowledge_base_misses(container) -> None:
    class FakeTextLLM:
        model_name = "fake-text-llm"

        async def acomplete(self, messages, temperature=None, max_tokens=None):
            return "这是一个通用的风险分析建议，具体条款仍需核对原文。"

    original_llm = container.answerer.llm
    original_offline = container.answerer.offline
    container.answerer.llm = FakeTextLLM()
    container.answerer.offline = False
    try:
        result = asyncio.run(
            container.answerer.answer(
                "量子纠缠对保险理赔有什么影响",
                UserProfile(user_id="fallback-user", risk_level="C3"),
                "fallback-test",
            )
        )
    finally:
        container.answerer.llm = original_llm
        container.answerer.offline = original_offline
    assert result.strategy == "llm_fallback"
    assert "知识库未命中" in result.answer
