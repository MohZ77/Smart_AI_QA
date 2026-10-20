from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Iterator

import httpx

from ..config import Config

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    model_name: str = "unknown"

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str: ...

    @abstractmethod
    def stream(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]: ...

    async def acomplete(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return await asyncio.to_thread(self.complete, messages, temperature, max_tokens)

    async def astream(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def producer() -> None:
            try:
                for token in self.stream(messages, temperature, max_tokens):
                    loop.call_soon_threadsafe(queue.put_nowait, token)
            except Exception as exc:  # pragma: no cover
                loop.call_soon_threadsafe(queue.put_nowait, f"__ERROR__:{exc}")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=producer, daemon=True).start()
        while True:
            item = await queue.get()
            if item is None:
                break
            if item.startswith("__ERROR__:"):
                raise RuntimeError(item.split(":", 1)[1])
            yield item


class OpenAICompatibleLLM(LLMClient):
    """DeepSeek / Qwen(vLLM) / 任意 OpenAI Chat Completions 兼容服务。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_s: float = 60.0,
        max_retries: int = 2,
        concurrency: int = 16,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max(0, max_retries)
        self._sync_semaphore = threading.BoundedSemaphore(max(1, concurrency))
        self._async_semaphore: asyncio.Semaphore | None = None

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(
        self,
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "messages": messages,
            "temperature": 0.1 if temperature is None else temperature,
            "max_tokens": max_tokens or 900,
            "stream": stream,
        }

    def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload = self._payload(messages, temperature, max_tokens, stream=False)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self._sync_semaphore:
                    with httpx.Client(timeout=self.timeout_s) as client:
                        response = client.post(self.endpoint, headers=self.headers, json=payload)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                data = response.json()
                return str(data["choices"][0]["message"]["content"] or "")
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
        raise RuntimeError(f"LLM 调用失败: {last_error}") from last_error

    def stream(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        payload = self._payload(messages, temperature, max_tokens, stream=True)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self._sync_semaphore:
                    with httpx.Client(timeout=self.timeout_s) as client:
                        with client.stream(
                            "POST", self.endpoint, headers=self.headers, json=payload
                        ) as response:
                            if response.status_code in {429, 500, 502, 503, 504}:
                                if attempt < self.max_retries:
                                    time.sleep(2**attempt)
                                    continue
                            response.raise_for_status()
                            for line in response.iter_lines():
                                if not line:
                                    continue
                                if line.startswith("data:"):
                                    line = line[5:].strip()
                                if line == "[DONE]":
                                    return
                                try:
                                    data = json.loads(line)
                                except json.JSONDecodeError:
                                    continue
                                delta = (
                                    data.get("choices", [{}])[0]
                                    .get("delta", {})
                                    .get("content")
                                )
                                if delta:
                                    yield str(delta)
                            return
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
        raise RuntimeError(f"LLM 流式调用失败: {last_error}") from last_error

    async def acomplete(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if self._async_semaphore is None:
            self._async_semaphore = asyncio.Semaphore(16)
        payload = self._payload(messages, temperature, max_tokens, stream=False)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self._async_semaphore:
                    async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                        response = await client.post(
                            self.endpoint, headers=self.headers, json=payload
                        )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                response.raise_for_status()
                data = response.json()
                return str(data["choices"][0]["message"]["content"] or "")
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
        raise RuntimeError(f"LLM 调用失败: {last_error}") from last_error

    async def astream(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        if self._async_semaphore is None:
            self._async_semaphore = asyncio.Semaphore(16)
        payload = self._payload(messages, temperature, max_tokens, stream=True)
        async with self._async_semaphore:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                async with client.stream(
                    "POST", self.endpoint, headers=self.headers, json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if line == "[DONE]":
                            return
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        delta = (
                            data.get("choices", [{}])[0].get("delta", {}).get("content")
                        )
                        if delta:
                            yield str(delta)


class OfflineLLM(LLMClient):
    """抽取式兜底模型：无 API Key / 无 GPU 时仍可端到端演示与跑评测。"""

    model_name = "offline-extractive"

    def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user_text = message.get("content", "")
                break
        if "【知识库资料】" in user_text or "【条款资料】" in user_text:
            marker = "【知识库资料】" if "【知识库资料】" in user_text else "【条款资料】"
            context_text = user_text.split(marker, 1)[1]
            context_text = context_text.split("【顾问问题】", 1)[0]
        else:
            context_text = user_text
        blocks = re.findall(
            r"\[(\d+)\]\s*(.+?)(?=\n\[\d+\]|\Z)", context_text, flags=re.S
        )
        if not blocks:
            return (
                "现有知识库资料中未检索到足够依据，暂时无法给出确定答复。"
                "建议补充具体险种、条款编号或咨询保险公司客服。"
            )
        lines = ["根据知识库资料，我查到的相关信息是："]
        for index, text in blocks[:3]:
            body = text.split("\n", 1)[1] if "\n" in text else text
            body = re.sub(r"(?m)^#{1,6}\s*", "", body)
            snippet = re.sub(r"\s+", " ", body).strip()
            lines.append(f"- {snippet[:260]} [{index}]")
        return "\n".join(lines)

    def stream(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        text = self.complete(messages, temperature, max_tokens)
        for index in range(0, len(text), 12):
            yield text[index : index + 12]


def build_llm(cfg: Config) -> LLMClient:
    provider = str(cfg.get("llm.provider", "offline")).lower()
    if provider == "offline":
        return OfflineLLM()

    api_key = str(cfg.get("llm.api_key", "") or "")
    if not api_key:
        api_key = os.getenv(str(cfg.get("llm.api_key_env", "HUIXIAOAN_LLM_API_KEY")), "")
    base_url = str(cfg.get("llm.base_url", ""))
    model = str(cfg.get("llm.model", ""))
    if provider == "deepseek":
        base_url = base_url or "https://api.deepseek.com/v1"
        model = model or "deepseek-chat"
    elif provider == "qwen":
        base_url = base_url or "http://127.0.0.1:8001/v1"
        model = model or "Qwen2.5-14B-Instruct"
    if not base_url or not model:
        raise ValueError("非 offline 模式必须配置 llm.base_url 与 llm.model")
    return OpenAICompatibleLLM(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_s=float(cfg.get("llm.timeout_s", 60)),
        max_retries=int(cfg.get("llm.max_retries", 2)),
        concurrency=int(cfg.get("llm.concurrency", 16)),
    )
