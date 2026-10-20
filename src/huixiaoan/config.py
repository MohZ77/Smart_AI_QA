from __future__ import annotations

import copy
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class Config:
    """轻量配置对象：支持 `cfg.get("retrieval.final_top_k", 5)` 形式的点路径访问。"""

    def __init__(self, data: dict[str, Any], base_dir: Path | None = None) -> None:
        self.data = data
        self.base_dir = (base_dir or Path.cwd()).resolve()

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    def path(self, key: str, default: str) -> Path:
        raw = str(self.get(f"paths.{key}", default))
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else (self.base_dir / candidate).resolve()

    def db(self, key: str, default: str) -> str:
        """数据库配置既可以是 SQLite 文件路径，也可以是 mysql:// DSN。"""
        raw = str(self.get(f"paths.{key}", default))
        if "://" in raw:
            return raw
        return str(self.path(key, default))

    def env(self, name: str, default: str = "") -> str:
        return os.getenv(name, default)

    def with_overrides(self, overrides: dict[str, Any]) -> "Config":
        merged = copy.deepcopy(self.data)
        _deep_merge(merged, overrides)
        return Config(merged, self.base_dir)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            return os.getenv(match.group(1), match.group(2) or "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: str | Path | None = None) -> Config:
    """加载 YAML 配置，并用 HUIXIAOAN_* 环境变量覆盖常用项。"""
    config_path = Path(path or os.getenv("HUIXIAOAN_CONFIG", "configs/default.yaml")).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    data = _expand_env(data)

    env_overrides: dict[str, Any] = {}
    env_map = {
        "HUIXIAOAN_LLM_PROVIDER": ("llm", "provider"),
        "HUIXIAOAN_LLM_BASE_URL": ("llm", "base_url"),
        "HUIXIAOAN_LLM_MODEL": ("llm", "model"),
        "HUIXIAOAN_LLM_API_KEY": ("llm", "api_key"),
        "HUIXIAOAN_EMBEDDING_PROVIDER": ("embedding", "provider"),
        "HUIXIAOAN_EMBEDDING_MODEL": ("embedding", "model"),
        "HUIXIAOAN_VECTOR_BACKEND": ("vector_store", "backend"),
        "HUIXIAOAN_MILVUS_URI": ("vector_store", "milvus_uri"),
        "HUIXIAOAN_REDIS_URL": ("cache", "redis_url"),
        "HUIXIAOAN_API_KEY": ("security", "api_key"),
        "HUIXIAOAN_LOG_LEVEL": ("observability", "log_level"),
        "HUIXIAOAN_LOG_FILE": ("observability", "log_file"),
        "HUIXIAOAN_AUDIT_DB": ("paths", "audit_db"),
        "HUIXIAOAN_MEMORY_DB": ("paths", "memory_db"),
        "HUIXIAOAN_FAQ_DB": ("paths", "faq_db"),
        "HUIXIAOAN_AUTH_DB": ("paths", "auth_db"),
        "HUIXIAOAN_RATE_LIMIT_QPS": ("security", "rate_limit_qps"),
        "HUIXIAOAN_TOKEN_SECRET": ("security", "token_secret"),
        "HUIXIAOAN_VISION_PROVIDER": ("vision", "provider"),
        "HUIXIAOAN_VISION_BASE_URL": ("vision", "base_url"),
        "HUIXIAOAN_VISION_MODEL": ("vision", "model"),
        "HUIXIAOAN_VISION_API_KEY": ("vision", "api_key"),
    }
    for env_name, (section, key) in env_map.items():
        value = os.getenv(env_name)
        if value is not None and value != "":
            env_overrides.setdefault(section, {})[key] = value

    if env_overrides:
        _deep_merge(data, env_overrides)

    return Config(data, base_dir=config_path.parent.parent)


def setup_logging(
    level: str = "INFO",
    json_logs: bool = False,
    log_file: str | Path | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    """双通道日志：控制台 + 滚动文件。

    - 控制台固定保留，文件不可用时降级为仅控制台并告警，不阻塞启动；
    - 文件用 RotatingFileHandler 轮转，避免长时间运行写满磁盘；
    - 重复调用会先移除本函数此前安装的 handler，避免叠加双写。
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    for stale in [h for h in root.handlers if getattr(h, "_huixiaoan_managed", False)]:
        root.removeHandler(stale)
        stale.close()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._huixiaoan_managed = True
    root.addHandler(console)

    if json_logs:
        logging.getLogger(__name__).warning("json_logs 目前未启用，使用文本日志格式")

    if not log_file:
        return
    try:
        target = Path(log_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            target, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler._huixiaoan_managed = True
        root.addHandler(file_handler)
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "日志文件不可用，降级为仅控制台输出: %s (%s)", log_file, exc
        )
