from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from huixiaoan.config import load_config
from huixiaoan.service.container import Container
from huixiaoan.sync import SyncPipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="增量同步研报/基金/新闻")
    parser.add_argument("--source", choices=["wind", "fund", "news"], required=True)
    parser.add_argument("--input", default="")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()

    container = Container(load_config(args.config))
    try:
        report = SyncPipeline(container.cfg, container).run(
            args.source, args.input or None
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    finally:
        container.close()


if __name__ == "__main__":
    main()
