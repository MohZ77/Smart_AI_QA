from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from huixiaoan.config import load_config
from huixiaoan.service.container import Container


def main() -> None:
    parser = argparse.ArgumentParser(description="构建小智财答知识库")
    parser.add_argument("--path", nargs="+", default=["data/raw"], help="文件或目录")
    parser.add_argument("--rebuild", action="store_true", help="清空后全量重建")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()

    container = Container(load_config(args.config))
    try:
        report = container.ingest(args.path, rebuild=args.rebuild)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        print(json.dumps(container.health(), ensure_ascii=False, indent=2))
    finally:
        container.close()


if __name__ == "__main__":
    main()
