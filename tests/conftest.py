from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from huixiaoan.config import load_config
from huixiaoan.service.container import Container


@pytest.fixture()
def tmp_path() -> Path:
    """覆盖 pytest 默认临时目录，统一放到项目内，避免 Windows ACL 问题。"""
    path = Path(__file__).resolve().parents[1] / ".test-tmp" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def container() -> Container:
    root = Path(__file__).resolve().parents[1]
    workspace = root / ".test-tmp" / uuid.uuid4().hex
    workspace.mkdir(parents=True, exist_ok=True)
    cfg = load_config(root / "configs" / "default.yaml").with_overrides(
        {
            "paths": {
                "data_dir": str(workspace),
                "raw_dir": str(root / "data" / "raw"),
                "index_dir": str(workspace / "index"),
                "image_dir": str(workspace / "images"),
                "audit_db": str(workspace / "audit.db"),
                "memory_db": str(workspace / "memory.db"),
                "faq_db": str(workspace / "faq.db"),
                "auth_db": str(workspace / "auth.db"),
            },
            "embedding": {"provider": "hashing"},
            "vector_store": {"backend": "local"},
            "collections": {"enabled": True},
            "llm": {"provider": "offline"},
            "security": {"token_secret": "test-secret-not-for-production"},
            "resilience": {"audit_spool_dir": str(workspace / "spool")},
            "bootstrap": {"auto_ingest": True, "seed_faq": True},
            "compliance": {"llm_judge": False},
        }
    )
    instance = Container(cfg)
    yield instance
    instance.close()
