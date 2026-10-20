from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from ..auth import AuthError, AuthUser
from ..config import Config, load_config
from ..profile import QUESTIONNAIRE, assess_profile
from ..types import UserProfile
from .container import Container
from .metrics import Metrics
from .schemas import (
    ChatRequest,
    AccountProfileUpdate,
    FeedbackRequest,
    IngestRequest,
    IngestTaskResponse,
    LoginRequest,
    ProfileAnswers,
    RegisterRequest,
)

logger = logging.getLogger(__name__)


class TokenBucket:
    def __init__(self, rate_per_second: float, capacity: int | None = None) -> None:
        self.rate = max(0.1, rate_per_second)
        self.capacity = float(capacity or max(1, int(rate_per_second)))
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, updated = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - updated) * self.rate)
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1, now)
        if len(self._buckets) > 100000:
            self._buckets.clear()
        return True


def create_app(config_path: str | Path | None = None, container: Container | None = None) -> FastAPI:
    cfg = container.cfg if container else load_config(config_path)
    metrics = Metrics()
    limiter = TokenBucket(float(cfg.get("security.rate_limit_qps", 20)))
    login_rate = max(1.0, float(cfg.get("security.login_rate_per_minute", 10)))
    login_limiter = TokenBucket(login_rate / 60.0, capacity=int(login_rate))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        owns_container = application.state.container is None
        application.state.container = application.state.container or Container(cfg)
        logger.info("服务启动: %s", application.state.container.health())
        try:
            yield
        finally:
            if owns_container:
                application.state.container.close()

    app = FastAPI(
        title=str(cfg.get("app.name", "小智财答")),
        version=str(cfg.get("app.version", "0.1.0")),
        description="合规优先的智能理财顾问：混合检索、精排、适当性审查、流式输出。",
        lifespan=lifespan,
    )
    app.state.cfg = cfg
    app.state.container = container
    app.state.metrics = metrics
    app.state.ingest_tasks = {}
    web_dir = Path(__file__).resolve().parents[3] / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/", include_in_schema=False)
    async def home():
        index = web_dir / "index.html"
        if index.exists():
            return FileResponse(str(index), headers={"Cache-Control": "no-store"})
        return JSONResponse(
            {"message": "小智财答 API", "docs": "/docs", "health": "/healthz"}
        )

    def get_container(request: Request) -> Container:
        instance = request.app.state.container
        if instance is None:
            # 兼容未触发 lifespan 的测试客户端/嵌入场景。
            instance = Container(cfg)
            request.app.state.container = instance
        return instance

    def authorize(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        expected = str(cfg.get("security.api_key", "") or "")
        if expected and x_api_key != expected:
            raise HTTPException(status_code=401, detail="无效的 API Key")
        client = request.client.host if request.client else "anonymous"
        user_key = x_api_key or client
        if not limiter.allow(user_key):
            metrics.inc("rate_limited_total")
            raise HTTPException(status_code=429, detail="请求过于频繁")

    def authorize_admin(
        request: Request,
        x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        expected = str(cfg.get("security.admin_api_key", "") or "")
        if expected and x_admin_key != expected:
            raise HTTPException(status_code=403, detail="需要管理员权限")
        authorize(request, x_api_key)

    def client_ip(request: Request) -> str:
        if bool(cfg.get("security.trust_proxy_headers", False)):
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",", 1)[0].strip()
        return request.client.host if request.client else "anonymous"

    def limit_login(request: Request) -> None:
        if not login_limiter.allow(client_ip(request)):
            metrics.inc("login_rate_limited_total")
            raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")

    def bearer_token(request: Request) -> str:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return ""

    def optional_user(request: Request) -> AuthUser | None:
        token = bearer_token(request)
        if not token:
            return None
        try:
            return get_container(request).auth.authenticate(token)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.message,
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    def require_user(request: Request) -> AuthUser:
        user = optional_user(request)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail="请先登录",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    def resolve_profile(body: ChatRequest, user: AuthUser | None) -> UserProfile:
        """已登录用户以服务端画像为准，防止客户端伪造身份或风险等级。"""
        if user is not None:
            return UserProfile(**user.to_profile_dict())
        profile = UserProfile(**body.profile.model_dump())
        # 匿名模式只允许使用部署方默认租户，避免客户端伪造租户污染缓存/审计。
        profile.tenant_id = str(cfg.get("security.default_tenant_id", "default"))
        profile.profile_version = 0
        return profile

    def ensure_chat_allowed(user: AuthUser | None) -> None:
        if bool(cfg.get("security.require_auth_for_chat", False)) and user is None:
            raise HTTPException(
                status_code=401,
                detail="请登录后使用智能问答",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def scoped_session_id(session_id: str, user: AuthUser | None) -> str:
        base = (session_id or "default").strip()[:96] or "default"
        prefix = f"u:{user.id}" if user is not None else "anon"
        return f"{prefix}:{base}"

    def auth_payload(user: AuthUser, token: str) -> dict[str, Any]:
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": int(cfg.get("security.token_ttl_hours", 8)) * 3600,
            "user": user.to_public_dict(),
        }

    async def prepare_image(container_: Container, body: ChatRequest):
        if not body.image_base64:
            return "", None
        payload = body.image_base64.strip().replace("\n", "").replace("\r", "").replace(" ", "")
        mime = body.image_mime or ""
        if payload.startswith("data:") and "," in payload:
            header, payload = payload.split(",", 1)
            if ";" in header:
                mime = header[5:].split(";", 1)[0] or mime
        try:
            image_bytes = base64.b64decode(payload, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="图片 base64 解码失败") from exc
        try:
            vision_result, image_record = await asyncio.to_thread(
                container_.vision.recognize, image_bytes, mime
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        visual = {
            **vision_result.to_dict(),
            "image": image_record,
        }
        return (vision_result.text if vision_result.ok else ""), visual

    @app.middleware("http")
    async def request_metrics(request: Request, call_next):
        started = time.perf_counter()
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            metrics.inc("errors_total")
            raise
        elapsed = (time.perf_counter() - started) * 1000
        metrics.inc("requests_total")
        metrics.observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{elapsed:.2f}"
        return response

    @app.get("/healthz")
    async def healthz(request: Request):
        return get_container(request).health()

    @app.get("/v1/profile/questions")
    async def profile_questions():
        return {"questions": QUESTIONNAIRE}

    @app.post("/v1/auth/register")
    async def register(request: Request, body: RegisterRequest):
        limit_login(request)
        container_ = get_container(request)
        try:
            user, token = container_.auth.register(
                body.username, body.password, body.display_name
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        metrics.inc("users_registered_total")
        return auth_payload(user, token)

    @app.post("/v1/auth/login")
    async def login(request: Request, body: LoginRequest):
        limit_login(request)
        container_ = get_container(request)
        try:
            user, token = container_.auth.login(body.username, body.password)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        metrics.inc("users_login_total")
        return auth_payload(user, token)

    @app.get("/v1/auth/me")
    async def auth_me(user: AuthUser = Depends(require_user)):
        return {"user": user.to_public_dict()}

    @app.patch("/v1/auth/profile")
    async def update_account_profile(
        body: AccountProfileUpdate,
        user: AuthUser = Depends(require_user),
        connection: Any = Depends(get_container),
    ):
        try:
            updated = connection.auth.update_profile(
                user.id,
                risk_level=body.risk_level,
                investment_experience=body.investment_experience,
                horizon=body.horizon,
                investable_assets=body.investable_assets,
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        return {"user": updated.to_public_dict()}

    @app.get("/v1/images/{image_id}")
    async def image_asset(image_id: str, request: Request):
        container_ = get_container(request)
        record = container_.image_store.get(image_id)
        if not record:
            raise HTTPException(status_code=404, detail="图片不存在")
        path = container_.image_store.path(image_id)
        if path is not None:
            return FileResponse(
                str(path),
                media_type=str(record.get("mime") or "application/octet-stream"),
            )
        remote_url = str(record.get("url") or "")
        if remote_url.startswith(("http://", "https://")):
            return RedirectResponse(remote_url)
        raise HTTPException(status_code=404, detail="图片文件不可用")

    @app.post("/v1/profile/assess")
    async def profile_assess(
        request: Request,
        body: ProfileAnswers,
        _: None = Depends(authorize),
        user: AuthUser | None = Depends(optional_user),
    ):
        user_id = user.id if user is not None else body.user_id
        try:
            profile, average = assess_profile(body.answers, user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if user is not None:
            container_ = get_container(request)
            updated = container_.auth.update_profile(
                user.id,
                risk_level=profile.risk_level,
                investment_experience=profile.investment_experience,
                horizon=profile.horizon,
                investable_assets=profile.investable_assets,
            )
            profile = UserProfile(**updated.to_profile_dict())
        return {
            "profile": profile.to_dict(),
            "average_score": round(average, 2),
            "risk_level": profile.risk_level,
        }

    @app.get("/readyz")
    async def readyz(request: Request):
        state = get_container(request).health()
        ready = state["vector_count"] > 0
        return JSONResponse(state, status_code=200 if ready else 503)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus(request: Request):
        container_ = get_container(request)
        return metrics.render_prometheus(container_.cache.stats.to_dict())

    @app.post("/v1/chat")
    async def chat(
        request: Request,
        body: ChatRequest,
        _: None = Depends(authorize),
        user: AuthUser | None = Depends(optional_user),
    ):
        container_ = get_container(request)
        ensure_chat_allowed(user)
        profile = resolve_profile(body, user)
        image_context, visual = await prepare_image(container_, body)
        try:
            result = await container_.answerer.answer(
                body.question,
                profile,
                scoped_session_id(body.session_id, user),
                request_id=getattr(request.state, "request_id", None),
                image_context=image_context,
                visual=visual,
                domain=body.domain,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            metrics.inc("chat_errors_total")
            logger.exception("问答失败")
            raise HTTPException(status_code=500, detail=f"问答失败: {exc}") from exc
        if result.cached:
            metrics.inc("cache_hits_total")
        return result.to_dict()

    @app.post("/v1/chat/stream")
    async def chat_stream(
        request: Request,
        body: ChatRequest,
        _: None = Depends(authorize),
        user: AuthUser | None = Depends(optional_user),
    ):
        container_ = get_container(request)
        ensure_chat_allowed(user)
        profile = resolve_profile(body, user)
        image_context, visual = await prepare_image(container_, body)

        async def event_generator():
            try:
                async for event in container_.answerer.stream_answer(
                    body.question,
                    profile,
                    scoped_session_id(body.session_id, user),
                    request_id=getattr(request.state, "request_id", None),
                    image_context=image_context,
                    visual=visual,
                    domain=body.domain,
                ):
                    event_type = event.get("type", "message")
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"event: {event_type}\ndata: {payload}\n\n"
            except Exception as exc:  # pragma: no cover
                logger.exception("流式问答失败")
                yield f"event: error\ndata: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/chat/file")
    async def chat_file(
        request: Request,
        file: UploadFile = File(...),
        question: str = Form(""),
        session_id: str = Form("default"),
        profile_json: str = Form("{}"),
        _: None = Depends(authorize),
        user: AuthUser | None = Depends(optional_user),
    ):
        container_ = get_container(request)
        ensure_chat_allowed(user)
        try:
            profile_payload = json.loads(profile_json or "{}")
            profile = (
                UserProfile(**user.to_profile_dict())
                if user is not None
                else UserProfile(**profile_payload)
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail="用户画像格式不正确") from exc
        data = await file.read()
        try:
            result = await asyncio.to_thread(
                container_.documents.review,
                file.filename or "contract.md",
                data,
                question,
                profile,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("文件审查失败")
            raise HTTPException(status_code=500, detail=f"文件审查失败: {exc}") from exc
        return result.to_dict()

    @app.websocket("/v1/ws/chat")
    async def websocket_chat(websocket: WebSocket):
        await websocket.accept()
        container_ = websocket.app.state.container
        token = websocket.query_params.get("token", "")
        try:
            user = container_.auth.authenticate(token) if token else None
        except AuthError as exc:
            await websocket.close(code=1008, reason=exc.message)
            return
        try:
            while True:
                payload = await websocket.receive_json()
                if payload.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                question = str(payload.get("question", "")).strip()
                if not question:
                    await websocket.send_json({"type": "error", "message": "question 不能为空"})
                    continue
                if bool(cfg.get("security.require_auth_for_chat", False)) and user is None:
                    await websocket.send_json(
                        {"type": "error", "message": "请登录后使用智能问答"}
                    )
                    continue
                profile = (
                    UserProfile(**user.to_profile_dict())
                    if user is not None
                    else UserProfile(**payload.get("profile", {}))
                )
                session_id = str(payload.get("session_id", "default"))
                domain = str(payload.get("domain", "all"))
                async for event in container_.answerer.stream_answer(
                    question,
                    profile,
                    scoped_session_id(session_id, user),
                    domain=domain,
                ):
                    await websocket.send_json(event)
        except WebSocketDisconnect:
            return
        except Exception as exc:  # pragma: no cover
            logger.exception("WebSocket 会话异常")
            try:
                await websocket.send_json({"type": "error", "message": str(exc)})
            except Exception:
                pass

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(
        request: Request,
        session_id: str,
        _: None = Depends(authorize),
        user: AuthUser | None = Depends(optional_user),
    ):
        """清空会话的服务端上下文；客户端会话 ID 会按账号命名空间隔离。"""
        ensure_chat_allowed(user)
        container_ = get_container(request)
        scoped = scoped_session_id(session_id, user)
        try:
            container_.memory.clear(scoped)
        except Exception as exc:
            logger.exception("会话上下文清理失败")
            raise HTTPException(status_code=503, detail="会话清理失败，请稍后重试") from exc
        return {"status": "cleared", "session_id": session_id}

    async def _run_ingest(task_id: str, paths: list[str], rebuild: bool) -> None:
        container_ = app.state.container
        app.state.ingest_tasks[task_id] = {"status": "running", "started_at": time.time()}
        try:
            report = await asyncio.to_thread(container_.ingest, paths, rebuild)
            app.state.ingest_tasks[task_id] = {
                "status": "completed",
                "report": report.to_dict(),
                "finished_at": time.time(),
            }
        except Exception as exc:  # pragma: no cover
            logger.exception("索引任务失败")
            app.state.ingest_tasks[task_id] = {
                "status": "failed",
                "error": str(exc),
                "finished_at": time.time(),
            }

    @app.post("/v1/ingest", response_model=IngestTaskResponse)
    async def ingest(
        request: Request,
        body: IngestRequest,
        background_tasks: BackgroundTasks,
        _: None = Depends(authorize_admin),
    ):
        paths = body.paths or [str(cfg.path("raw_dir", "data/raw"))]
        task_id = uuid.uuid4().hex
        app.state.ingest_tasks[task_id] = {"status": "queued"}
        background_tasks.add_task(_run_ingest, task_id, paths, body.rebuild)
        return {"task_id": task_id, "status": "queued"}

    @app.post("/v1/ingest/upload", response_model=IngestTaskResponse)
    async def upload(
        request: Request,
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        _: None = Depends(authorize_admin),
    ):
        upload_dir = cfg.path("data_dir", "data") / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(file.filename or "upload.bin").name
        target = upload_dir / f"{uuid.uuid4().hex}_{safe_name}"
        content = await file.read()
        target.write_bytes(content)
        task_id = uuid.uuid4().hex
        app.state.ingest_tasks[task_id] = {"status": "queued"}
        background_tasks.add_task(_run_ingest, task_id, [str(target)], False)
        return {"task_id": task_id, "status": "queued"}

    @app.get("/v1/ingest/{task_id}")
    async def ingest_status(
        request: Request, task_id: str, _: None = Depends(authorize_admin)
    ):
        task = app.state.ingest_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"task_id": task_id, **task}

    @app.get("/v1/audit/{request_id}")
    async def audit(
        request: Request, request_id: str, _: None = Depends(authorize_admin)
    ):
        audit_store = get_container(request).audit
        if hasattr(audit_store, "flush"):
            audit_store.flush()
        record = audit_store.get(request_id)
        if not record:
            raise HTTPException(status_code=404, detail="审计记录不存在")
        return record

    @app.post("/v1/feedback")
    async def feedback(
        request: Request,
        body: FeedbackRequest,
        _: None = Depends(authorize),
        user: AuthUser | None = Depends(optional_user),
    ):
        logger.info(
            "feedback request_id=%s user=%s helpful=%s comment=%s",
            body.request_id,
            user.id if user is not None else "anonymous",
            body.helpful,
            body.comment[:200],
        )
        return {"status": "recorded"}

    return app


app = create_app()
