from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from huixiaoan.config import load_config
from huixiaoan.service.container import Container

SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm", ".csv", ".xlsx"}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scan_source(name: str, spec: dict) -> dict:
    directory = Path(spec["dir"])
    directory.mkdir(parents=True, exist_ok=True)
    files = [
        path
        for path in directory.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUPPORTED
        and not path.name.lower().startswith(("readme", "license", "manifest"))
        and not any(part.startswith(".") for part in path.parts)
    ]
    seen: set[str] = set()
    duplicates = 0
    faq_records = 0
    errors: list[str] = []
    for path in files:
        digest = _digest(path)
        if digest in seen:
            duplicates += 1
            continue
        seen.add(digest)
        if name == "faq" and path.suffix.lower() == ".jsonl":
            with path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                        if payload.get("question") and payload.get("answer"):
                            faq_records += 1
                        else:
                            errors.append(f"{path.name}:{line_no} 缺少 question/answer")
                    except json.JSONDecodeError as exc:
                        errors.append(f"{path.name}:{line_no} JSON 错误: {exc}")
    count = faq_records if name == "faq" and faq_records else len(files) - duplicates
    return {
        "name": spec.get("name", name),
        "dir": str(directory),
        "files": len(files),
        "valid_records": count,
        "duplicates": duplicates,
        "errors": errors[:20],
        "suggested_sources": spec.get("suggested_sources", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="统计并构建离线金融知识库")
    parser.add_argument("--sources", default="data/kb/sources.yaml")
    parser.add_argument("--manifest", default="data/kb/manifest.json")
    parser.add_argument("--ingest", action="store_true", help="统计后调用统一索引流水线")
    parser.add_argument("--rebuild", action="store_true", help="配合 --ingest 全量重建")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--strict", action="store_true", help="未达到目标时返回非零")
    args = parser.parse_args()

    sources_path = Path(args.sources)
    if not sources_path.exists():
        raise SystemExit(f"找不到数据源配置: {sources_path}")
    spec = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    targets = spec.get("targets", {})
    report = {}
    for name, source_spec in spec.get("sources", {}).items():
        info = scan_source(name, source_spec)
        target = int(targets.get(name, 0))
        info["target"] = target
        info["gap"] = max(0, target - info["valid_records"])
        report[name] = info

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "targets": targets,
        "sources": report,
        "note": "目标规模来自 sources.yaml；未配置目标时只统计实际文件。",
    }
    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for name, info in report.items():
        print(
            f"{info['name']:<16} valid={info['valid_records']:<7} "
            f"target={info['target']:<7} gap={info['gap']:<7} "
            f"duplicates={info['duplicates']} errors={len(info['errors'])}"
        )
    print(f"manifest: {manifest_path}")

    missing = any(info["gap"] > 0 for info in report.values())
    if args.ingest:
        cfg = load_config(args.config)
        container = Container(cfg)
        try:
            dirs = [info["dir"] for info in report.values()]
            result = container.ingest(dirs, rebuild=args.rebuild)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        finally:
            container.close()
    if args.strict and missing:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
