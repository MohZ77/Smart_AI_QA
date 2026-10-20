from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ASSET_CLASSES = {"fund", "insurance", "bond", "bank_wealth", "equity"}
SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm"}


def _safe(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("_")[:100] or "untitled"


def _find_local(path_value: str, manifest: Path) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    if path.is_absolute() and path.exists():
        return path
    for candidate in (Path.cwd() / path, manifest.parent / path):
        if candidate.exists():
            return candidate
    return None


def _download(url: str, timeout: int = 60) -> tuple[bytes, str]:
    import httpx

    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "XiaozhiCaiDa-KB-Builder/1.0"},
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        suffix = Path(url.split("?", 1)[0]).suffix.lower()
        if suffix not in SUPPORTED:
            if "pdf" in content_type:
                suffix = ".pdf"
            elif "html" in content_type:
                suffix = ".html"
            else:
                suffix = ".pdf"
        return response.content, suffix


def main() -> None:
    parser = argparse.ArgumentParser(description="从产品披露清单导入基金/保险/理财文档")
    parser.add_argument("--manifest", default="data/kb/products/MANIFEST.csv")
    parser.add_argument("--out", default="data/kb/products")
    parser.add_argument("--download", action="store_true", help="允许下载 official_url")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    manifest = Path(args.manifest)
    if not manifest.exists():
        raise SystemExit(f"找不到产品清单: {manifest}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    imported = 0
    skipped = 0
    failed: list[str] = []

    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if args.limit:
        rows = rows[: args.limit]
    for index, row in enumerate(rows, start=1):
        asset_class = (row.get("asset_class") or "").strip().lower()
        if asset_class not in ASSET_CLASSES:
            failed.append(f"第{index}行 asset_class 非法: {asset_class}")
            continue
        title = (row.get("title") or row.get("id") or "").strip()
        local = _find_local((row.get("local_path") or "").strip(), manifest)
        official_url = (row.get("official_url") or "").strip()
        if not local and not (official_url and args.download):
            if args.check_only:
                skipped += 1
                continue
            failed.append(f"第{index}行没有 local_path，且未开启 --download")
            continue
        try:
            if local:
                data = local.read_bytes()
                suffix = local.suffix.lower()
            else:
                data, suffix = _download(official_url)
                time.sleep(max(0.0, args.delay))
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                skipped += 1
                continue
            seen.add(digest)
            if args.check_only:
                imported += 1
                continue
            target_dir = out_dir / asset_class
            target_dir.mkdir(parents=True, exist_ok=True)
            record_id = (row.get("id") or digest[:16]).strip()
            target = target_dir / f"{_safe(record_id)}_{_safe(title)}{suffix}"
            target.write_bytes(data)
            sidecar = {
                "id": record_id,
                "title": title,
                "asset_class": asset_class,
                "doc_type": (row.get("doc_type") or asset_class).strip(),
                "product_code": row.get("product_code", ""),
                "risk_level": row.get("risk_level", ""),
                "publish_date": row.get("publish_date", ""),
                "source": row.get("source", ""),
                "license": row.get("license", ""),
                "source_url": official_url,
            }
            target.with_name(target.name + ".meta.json").write_text(
                json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            imported += 1
        except Exception as exc:
            failed.append(f"第{index}行 {title}: {exc}")

    print(
        json.dumps(
            {
                "rows": len(rows),
                "imported": imported,
                "skipped": skipped,
                "failed": failed,
                "out": str(out_dir),
                "download": args.download,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
