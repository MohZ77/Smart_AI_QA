from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import TTLCache
from .storage.db import Database
from .utils import now_iso

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fa5]{3,32}$")
_RISK_LEVELS = {"C1", "C2", "C3", "C4", "C5"}
_PASSWORD_ITERATIONS = 310_000


class AuthError(Exception):
    """带 HTTP 语义的认证错误。"""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class AuthUser:
    id: str
    username: str
    display_name: str = ""
    risk_level: str = "C3"
    investment_experience: str = ""
    horizon: str = ""
    investable_assets: float | None = None
    profile_version: int = 1
    tenant_id: str = "default"
    created_at: str = ""
    last_login_at: str = ""
    token_version: int = 0

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "risk_level": self.risk_level,
            "investment_experience": self.investment_experience,
            "horizon": self.horizon,
            "investable_assets": self.investable_assets,
            "profile_version": self.profile_version,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }

    def to_profile_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.id,
            "risk_level": self.risk_level,
            "investment_experience": self.investment_experience,
            "horizon": self.horizon,
            "investable_assets": self.investable_assets,
            "profile_version": self.profile_version,
            "tenant_id": self.tenant_id,
        }


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str, iterations: int = _PASSWORD_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = _b64decode(salt_b64)
        expected = _b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def validate_username(username: str) -> str:
    value = (username or "").strip()
    if not _USERNAME_RE.fullmatch(value):
        raise AuthError(
            "用户名需为 3-32 位，仅支持中文、字母、数字、下划线或短横线", 400
        )
    return value


def validate_password(password: str) -> str:
    value = password or ""
    if len(value) < 8:
        raise AuthError("密码至少需要 8 位", 400)
    if len(value) > 128:
        raise AuthError("密码不能超过 128 位", 400)
    if not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
        raise AuthError("密码需同时包含字母和数字", 400)
    return value


class AuthService:
    """本地账号体系：PBKDF2 密码哈希 + HMAC 签名短期 Token。

    生产环境切换到 MySQL 时，只要把 `auth_db` 配成 mysql:// DSN 即可；
    多实例部署必须通过 `security.token_secret` 共享同一个签名密钥。
    """

    def __init__(
        self,
        db_url: str,
        secret: str = "",
        token_ttl_seconds: int = 8 * 3600,
        profile_cache_ttl_s: float = 30.0,
        tenant_id: str = "default",
    ) -> None:
        self._db = Database(db_url)
        self.token_ttl_seconds = max(60, int(token_ttl_seconds))
        self.default_tenant_id = (tenant_id or "default").strip()[:64] or "default"
        self._profile_cache = TTLCache(
            max_entries=20_000, ttl_s=max(1.0, float(profile_cache_ttl_s))
        )
        self._secret = self._resolve_secret(db_url, secret)
        self._init_schema()

    @staticmethod
    def _resolve_secret(db_url: str, secret: str) -> bytes:
        if secret:
            return secret.encode("utf-8")
        if "://" not in db_url:
            path = Path(f"{db_url}.secret")
            try:
                if path.exists():
                    raw = path.read_text(encoding="ascii").strip()
                    if raw:
                        return raw.encode("ascii")
                raw = secrets.token_hex(32)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(raw, encoding="ascii")
                return raw.encode("ascii")
            except OSError as exc:
                logger.warning("Token 密钥文件不可用，使用进程内临时密钥: %s", exc)
        logger.warning(
            "未配置 security.token_secret，多实例部署时 Token 将无法互通"
        )
        return secrets.token_bytes(32)

    def _init_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id VARCHAR(64) PRIMARY KEY,
                username VARCHAR(64) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                display_name VARCHAR(128) DEFAULT '',
                risk_level VARCHAR(8) DEFAULT 'C3',
                investment_experience VARCHAR(64) DEFAULT '',
                horizon VARCHAR(64) DEFAULT '',
                investable_assets DOUBLE DEFAULT NULL,
                profile_version INTEGER DEFAULT 1,
                tenant_id VARCHAR(64) DEFAULT 'default',
                token_version INTEGER DEFAULT 0,
                disabled INTEGER DEFAULT 0,
                created_at VARCHAR(64) NOT NULL,
                last_login_at VARCHAR(64) DEFAULT ''
            )
            """
        )
        self._ensure_column("profile_version", "INTEGER DEFAULT 1")
        self._ensure_column("tenant_id", "VARCHAR(64) DEFAULT 'default'")

    def _ensure_column(self, name: str, definition: str) -> None:
        """轻量兼容旧库；生产环境应使用正式迁移工具。"""
        try:
            self._db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        except Exception as exc:
            # 列已存在时不同数据库会抛不同异常，忽略即可。
            logger.debug("用户表列 %s 已存在或迁移跳过: %s", name, exc)

    def _row_to_user(self, row: dict[str, Any] | None) -> AuthUser | None:
        if not row:
            return None
        return AuthUser(
            id=str(row["id"]),
            username=str(row["username"]),
            display_name=str(row.get("display_name") or ""),
            risk_level=str(row.get("risk_level") or "C3"),
            investment_experience=str(row.get("investment_experience") or ""),
            horizon=str(row.get("horizon") or ""),
            investable_assets=(
                float(row["investable_assets"])
                if row.get("investable_assets") is not None
                else None
            ),
            profile_version=int(row.get("profile_version") or 1),
            tenant_id=str(row.get("tenant_id") or self.default_tenant_id),
            created_at=str(row.get("created_at") or ""),
            last_login_at=str(row.get("last_login_at") or ""),
            token_version=int(row.get("token_version") or 0),
        )

    def register(
        self,
        username: str,
        password: str,
        display_name: str = "",
    ) -> tuple[AuthUser, str]:
        username = validate_username(username)
        password = validate_password(password)
        normalized = username.casefold()
        if self._find_row(normalized):
            raise AuthError("该用户名已被注册", 409)
        user_id = f"u_{secrets.token_hex(12)}"
        created = now_iso()
        try:
            self._db.execute(
                "INSERT INTO users(id, username, password_hash, display_name, "
                "risk_level, investment_experience, horizon, investable_assets, "
                "profile_version, tenant_id, token_version, disabled, created_at, "
                "last_login_at) VALUES (?, ?, ?, ?, 'C3', '', '', NULL, 1, ?, 0, 0, ?, ?)",
                (
                    user_id,
                    normalized,
                    hash_password(password),
                    (display_name or "").strip()[:64],
                    self.default_tenant_id,
                    created,
                    created,
                ),
            )
        except Exception as exc:
            # 并发注册时依赖数据库唯一约束兜底。
            if self._find_row(normalized):
                raise AuthError("该用户名已被注册", 409) from exc
            raise
        user = self.get_user(user_id)
        if user is None:  # pragma: no cover
            raise AuthError("注册失败，请稍后重试", 500)
        return user, self.create_token(user)

    def login(self, username: str, password: str) -> tuple[AuthUser, str]:
        normalized = (username or "").strip().casefold()
        row = self._find_row(normalized)
        if not row or int(row.get("disabled") or 0):
            # 统一错误信息，避免账号枚举。
            raise AuthError("用户名或密码错误", 401)
        if not verify_password(password or "", str(row.get("password_hash") or "")):
            raise AuthError("用户名或密码错误", 401)
        user = self._row_to_user(row)
        if user is None:  # pragma: no cover
            raise AuthError("用户名或密码错误", 401)
        self._db.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (now_iso(), user.id),
        )
        self._profile_cache.clear()
        refreshed = self.get_user(user.id) or user
        return refreshed, self.create_token(refreshed)

    def _find_row(self, normalized_username: str) -> dict[str, Any] | None:
        return self._db.query_one(
            "SELECT * FROM users WHERE username = ?", (normalized_username,)
        )

    def get_user(self, user_id: str) -> AuthUser | None:
        cached = self._profile_cache.get(user_id)
        if cached is not None:
            return cached
        user = self._row_to_user(
            self._db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        )
        if user is not None:
            self._profile_cache.set(user_id, user)
        return user

    def get_by_username(self, username: str) -> AuthUser | None:
        return self._row_to_user(self._find_row((username or "").strip().casefold()))

    def create_token(self, user: AuthUser, ttl_seconds: int | None = None) -> str:
        now = int(time.time())
        ttl = self.token_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        payload = {
            "sub": user.id,
            "uname": user.username,
            "ver": user.token_version,
            "iat": now,
            "exp": now + ttl,
            "jti": secrets.token_hex(8),
        }
        body = _b64encode(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        signature = hmac.new(
            self._secret, f"v1.{body}".encode("ascii"), hashlib.sha256
        ).digest()
        return f"v1.{body}.{_b64encode(signature)}"

    def authenticate(self, token: str | None) -> AuthUser:
        if not token:
            raise AuthError("请先登录", 401)
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            raise AuthError("登录状态无效，请重新登录", 401)
        body, signature = parts[1], parts[2]
        expected = hmac.new(
            self._secret, f"v1.{body}".encode("ascii"), hashlib.sha256
        ).digest()
        try:
            actual = _b64decode(signature)
        except (ValueError, TypeError) as exc:
            raise AuthError("登录状态无效，请重新登录", 401) from exc
        if not hmac.compare_digest(actual, expected):
            raise AuthError("登录状态无效，请重新登录", 401)
        try:
            payload = json.loads(_b64decode(body).decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise AuthError("登录状态无效，请重新登录", 401) from exc
        if int(payload.get("exp", 0)) < int(time.time()):
            raise AuthError("登录已过期，请重新登录", 401)
        user = self.get_user(str(payload.get("sub", "")))
        if user is None or int(payload.get("ver", -1)) != user.token_version:
            raise AuthError("登录状态已失效，请重新登录", 401)
        return user

    def update_profile(
        self,
        user_id: str,
        *,
        risk_level: str | None = None,
        investment_experience: str | None = None,
        horizon: str | None = None,
        investable_assets: float | None = None,
    ) -> AuthUser:
        user = self.get_user(user_id)
        if user is None:
            raise AuthError("用户不存在", 404)
        fields: list[str] = []
        params: list[Any] = []
        if risk_level is not None:
            value = str(risk_level).upper()
            if value not in _RISK_LEVELS:
                raise AuthError("风险等级必须为 C1-C5", 400)
            fields.append("risk_level = ?")
            params.append(value)
        if investment_experience is not None:
            fields.append("investment_experience = ?")
            params.append(str(investment_experience)[:64])
        if horizon is not None:
            fields.append("horizon = ?")
            params.append(str(horizon)[:64])
        if investable_assets is not None:
            if float(investable_assets) < 0:
                raise AuthError("可投资资产不能为负数", 400)
            fields.append("investable_assets = ?")
            params.append(float(investable_assets))
        if not fields:
            return user
        fields.append("profile_version = profile_version + 1")
        params.append(user_id)
        self._db.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE id = ?", tuple(params)
        )
        self._profile_cache.clear()
        updated = self.get_user(user_id)
        if updated is None:  # pragma: no cover
            raise AuthError("用户不存在", 404)
        return updated

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS total FROM users")
        return int(row["total"]) if row else 0

    def close(self) -> None:
        self._profile_cache.clear()
        self._db.close()
