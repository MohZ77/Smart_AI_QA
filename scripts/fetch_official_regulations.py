from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

NFRA_API = "https://www.nfra.gov.cn/cbircweb/solr/solrRulesSearch"
NFRA_PDF = "https://www.nfra.gov.cn/cbircweb/download/downloadguizPdf?docId={doc_id}"


def _safe_name(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("_")
    return value[:100] or "untitled"


def fetch_catalog(client: httpx.Client, limit: int = 0) -> list[dict]:
    items: dict[str, dict] = {}
    page = 1
    while True:
        response = client.post(
            NFRA_API,
            json={
                "keyWords": "",
                "pageSize": "50",
                "pageNo": str(page),
                "serchType": "",
            },
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        rows = data.get("lists") or []
        for row in rows:
            doc_id = str(row.get("docId") or "")
            if doc_id and doc_id not in items:
                items[doc_id] = row
        total = int(data.get("total") or len(items))
        if not rows or len(items) >= total:
            break
        if limit and len(items) >= limit:
            break
        page += 1
    result = list(items.values())
    return result[:limit] if limit else result


def download_one(client: httpx.Client, item: dict, out_dir: Path) -> dict:
    doc_id = str(item["docId"])
    title = str(item.get("docTitle") or item.get("docSubtitle") or doc_id)
    path = out_dir / f"{doc_id}_{_safe_name(title)}.pdf"
    meta_path = path.with_name(path.name + ".meta.json")
    meta = {
        "doc_id": doc_id,
        "title": title,
        "docTitle": item.get("docTitle", ""),
        "docSubtitle": item.get("docSubtitle", ""),
        "source_url": NFRA_PDF.format(doc_id=doc_id),
        "source": "国家金融监督管理总局规章库",
    }
    if path.exists() and path.stat().st_size > 1000:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"doc_id": doc_id, "title": title, "path": str(path), "status": "exists"}
    response = None
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.get(NFRA_PDF.format(doc_id=doc_id))
            if response.status_code == 403:
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    if response is None or last_error is not None:
        raise RuntimeError(f"下载失败: {last_error}")
    content_type = response.headers.get("content-type", "")
    if "pdf" not in content_type.lower() and not response.content.startswith(b"%PDF"):
        raise RuntimeError(f"不是 PDF: {content_type}")
    path.write_bytes(response.content)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    time.sleep(0.2)
    return {"doc_id": doc_id, "title": title, "path": str(path), "status": "downloaded"}


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取国家金融监督管理总局公开规章 PDF")
    parser.add_argument("--out", default="data/kb/regulations")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; XiaozhiCaiDa-KB-Builder/1.0)",
        "Accept": "application/json, application/pdf, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.nfra.gov.cn/cn/view/pages/zhengwuxinxi/guizhang.html",
    }
    with httpx.Client(headers=headers, timeout=60, follow_redirects=True) as client:
        catalog = fetch_catalog(client, args.limit)
        print(f"catalog={len(catalog)}")
        records = []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(download_one, client, item, out_dir) for item in catalog]
            for future in as_completed(futures):
                try:
                    record = future.result()
                    records.append(record)
                    print(f"{record['status']}: {record['title']}")
                except Exception as exc:
                    print(f"failed: {exc}")
        catalog_path = out_dir / "catalog.json"
        catalog_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"done={len(records)} downloaded="
            f"{sum(1 for item in records if item['status'] == 'downloaded')} "
            f"catalog={catalog_path}"
        )


if __name__ == "__main__":
    main()
