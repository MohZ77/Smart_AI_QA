from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config
from .indexing.images import ImageStore
from .types import ImageAsset
from .utils import stable_id

logger = logging.getLogger(__name__)


@dataclass
class VisionResult:
    ok: bool
    text: str = ""
    provider: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "provider": self.provider,
            "error": self.error,
        }


def _image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"GIF8"):
        return "image/gif", ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data.startswith(b"BM"):
        return "image/bmp", ".bmp"
    raise ValueError("图片格式不受支持，请上传 PNG、JPEG、GIF、WEBP 或 BMP")


class VisionClient:
    provider = "base"

    def recognize(self, image_bytes: bytes, mime: str) -> VisionResult:
        raise NotImplementedError


class OfflineVisionClient(VisionClient):
    provider = "offline"

    def recognize(self, image_bytes: bytes, mime: str) -> VisionResult:
        return VisionResult(
            ok=False,
            provider=self.provider,
            error="未配置视觉模型。请设置 HUIXIAOAN_VISION_PROVIDER 和视觉模型 API。",
        )


class OpenAIVisionClient(VisionClient):
    """OpenAI 兼容的多模态 Chat Completions 客户端，适配 Qwen-VL / 其他 VLM。"""

    provider = "openai-compatible"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float = 60.0,
        prompt: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.prompt = prompt or (
            "请识别这张图片中的全部文字，并用中文简要描述与金融、保险或理财相关的关键信息。"
            "看不清或图片中没有相关信息时请明确说明，不要编造数字、产品名称或收益。"
        )

    def recognize(self, image_bytes: bytes, mime: str) -> VisionResult:
        if not self.api_key:
            return VisionResult(
                ok=False,
                provider=self.provider,
                error="视觉模型 API Key 未配置。",
            )
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 800,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ],
                }
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
            text = str(data["choices"][0]["message"]["content"] or "").strip()
            if not text:
                return VisionResult(
                    ok=False, provider=self.provider, error="视觉模型没有返回识别结果。"
                )
            return VisionResult(ok=True, text=text[:8000], provider=self.provider)
        except Exception as exc:
            logger.warning("图片识别失败: %s", exc)
            return VisionResult(
                ok=False,
                provider=self.provider,
                error=f"图片识别调用失败: {exc}",
            )


class ImageUnderstandingService:
    def __init__(self, cfg: Config, image_store: ImageStore) -> None:
        self.cfg = cfg
        self.image_store = image_store
        self.provider = str(cfg.get("vision.provider", "offline")).lower()
        self.max_bytes = int(cfg.get("vision.max_image_mb", 8)) * 1024 * 1024
        self.client: VisionClient
        if self.provider == "offline":
            self.client = OfflineVisionClient()
        else:
            api_key = str(cfg.get("vision.api_key", "") or "")
            if not api_key:
                api_key = os.getenv(
                    str(cfg.get("vision.api_key_env", "HUIXIAOAN_VISION_API_KEY")), ""
                )
            self.client = OpenAIVisionClient(
                base_url=str(cfg.get("vision.base_url", "")),
                model=str(cfg.get("vision.model", "qwen-vl-max")),
                api_key=api_key,
                timeout_s=float(cfg.get("vision.timeout_s", 60)),
                prompt=str(cfg.get("vision.prompt", "") or ""),
            )

    def recognize(self, image_bytes: bytes, mime: str = "") -> tuple[VisionResult, dict]:
        if not image_bytes:
            raise ValueError("图片为空")
        if len(image_bytes) > self.max_bytes:
            raise ValueError(f"图片过大，最大支持 {self.max_bytes // 1024 // 1024} MB")
        detected_mime, ext = _image_type(image_bytes)
        mime = detected_mime if not mime else mime
        image_id = stable_id(
            "user-upload",
            hashlib.sha256(image_bytes).hexdigest(),
            length=24,
        )
        record = self.image_store.save(
            ImageAsset(
                image_id=image_id,
                doc_id="user-uploads",
                page=0,
                alt="用户上传图片",
                caption="用户上传图片",
                mime=mime,
                ext=ext,
                data=image_bytes,
            )
        )
        result = self.client.recognize(image_bytes, mime)
        return result, {
            "id": record.get("id"),
            "url": record.get("url"),
            "mime": record.get("mime"),
            "alt": record.get("alt"),
        }

    @property
    def configured(self) -> bool:
        return self.provider != "offline"

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "configured": self.configured,
            "max_image_mb": self.max_bytes // 1024 // 1024,
        }
