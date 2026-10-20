from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UserProfileIn(BaseModel):
    user_id: str = "anonymous"
    risk_level: str = "C3"
    investment_experience: str = ""
    horizon: str = ""
    investable_assets: float | None = None
    profile_version: int = 0
    tenant_id: str = "default"


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    session_id: str = "default"
    profile: UserProfileIn = Field(default_factory=UserProfileIn)
    domain: str = "all"
    stream: bool = False
    image_base64: str | None = None
    image_mime: str | None = None


class IngestRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)
    rebuild: bool = False


class FeedbackRequest(BaseModel):
    request_id: str
    helpful: bool
    comment: str = ""


class ProfileAnswers(BaseModel):
    user_id: str = "anonymous"
    answers: dict[str, int]


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=64)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class AccountProfileUpdate(BaseModel):
    risk_level: str | None = None
    investment_experience: str | None = None
    horizon: str | None = None
    investable_assets: float | None = None


class ChatResponse(BaseModel):
    answer: str
    citations: list[dict[str, Any]]
    confidence: float
    intent: dict[str, Any]
    strategy: str
    cached: bool
    cache_level: str = ""
    compliance: dict[str, Any] | None = None
    latency_ms: float
    request_id: str
    model: str
    usage: dict[str, Any] = Field(default_factory=dict)


class IngestTaskResponse(BaseModel):
    task_id: str
    status: str
