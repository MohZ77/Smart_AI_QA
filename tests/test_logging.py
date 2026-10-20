from __future__ import annotations

import logging
from pathlib import Path

import pytest

from huixiaoan.config import setup_logging


@pytest.fixture()
def restore_root_logging():
    """setup_logging 会动 root logger，测试结束后原样恢复，避免污染其他用例。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield root
    root.handlers = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture()
def log_dir(tmp_path: Path) -> Path:
    """日志测试放在项目内临时目录，自动清理，不污染正式数据。"""
    return tmp_path


def _managed(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if getattr(h, "_huixiaoan_managed", False)]


def test_console_and_file_dual_output(log_dir: Path, restore_root_logging: logging.Logger) -> None:
    log_file = log_dir / "logs" / "huixiaoan.log"
    setup_logging("INFO", log_file=log_file)
    logging.getLogger("huixiaoan.test").info("双通道测试消息")

    kinds = {type(h).__name__ for h in _managed(restore_root_logging)}
    assert "StreamHandler" in kinds, "控制台通道必须保留"
    assert "RotatingFileHandler" in kinds, "文件通道必须安装"

    content = log_file.read_text(encoding="utf-8")
    assert "双通道测试消息" in content
    assert "INFO" in content


def test_creates_parent_directory(log_dir: Path, restore_root_logging: logging.Logger) -> None:
    log_file = log_dir / "deep" / "nested" / "dir" / "app.log"
    setup_logging("INFO", log_file=log_file)
    logging.getLogger("huixiaoan.test").warning("目录自动创建")
    assert log_file.exists()
    assert "目录自动创建" in log_file.read_text(encoding="utf-8")


def test_repeated_calls_do_not_stack_handlers(log_dir: Path, restore_root_logging: logging.Logger) -> None:
    setup_logging("INFO", log_file=log_dir / "a.log")
    setup_logging("INFO", log_file=log_dir / "b.log")
    assert len(_managed(restore_root_logging)) == 2, "重复装配不应叠加 handler"

    logging.getLogger("huixiaoan.test").info("重复调用消息")
    assert "重复调用消息" in (log_dir / "b.log").read_text(encoding="utf-8")
    assert "重复调用消息" not in (log_dir / "a.log").read_text(encoding="utf-8")


def test_no_file_handler_when_disabled(log_dir: Path, restore_root_logging: logging.Logger) -> None:
    setup_logging("INFO", log_file=None)
    assert all(not isinstance(h, logging.FileHandler) for h in _managed(restore_root_logging))


def test_file_failure_degrades_to_console(
    log_dir: Path, restore_root_logging: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    bogus = log_dir / "occupied-as-dir"
    bogus.mkdir()
    with caplog.at_level("WARNING", logger="huixiaoan.config"):
        setup_logging("INFO", log_file=bogus)

    assert all(not isinstance(h, logging.FileHandler) for h in _managed(restore_root_logging))
    assert any("降级" in record.message for record in caplog.records), "降级必须告警"


def test_level_applied(log_dir: Path, restore_root_logging: logging.Logger) -> None:
    setup_logging("WARNING", log_file=log_dir / "lvl.log")
    assert restore_root_logging.level == logging.WARNING

    logging.getLogger("huixiaoan.test").info("低于级别不应写入")
    logging.getLogger("huixiaoan.test").warning("达到级别应写入")
    content = (log_dir / "lvl.log").read_text(encoding="utf-8")
    assert "低于级别不应写入" not in content
    assert "达到级别应写入" in content


def test_rotation_writes_backup(log_dir: Path, restore_root_logging: logging.Logger) -> None:
    log_file = log_dir / "rotate.log"
    setup_logging("INFO", log_file=log_file, max_bytes=2048, backup_count=2)
    noise = "x" * 400
    for index in range(12):
        logging.getLogger("huixiaoan.test").info("rot-%d %s", index, noise)

    assert log_file.exists()
    assert (log_dir / "rotate.log.1").exists(), "超过 max_bytes 必须产生备份"
