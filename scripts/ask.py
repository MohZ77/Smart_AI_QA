from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from huixiaoan.config import load_config
from huixiaoan.service.container import Container
from huixiaoan.types import UserProfile


async def run(args: argparse.Namespace) -> None:
    container = Container(load_config(args.config))
    try:
        result = await container.answerer.answer(
            args.question,
            UserProfile(user_id=args.user_id, risk_level=args.risk_level),
            args.session_id,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    finally:
        container.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="向小智财答提问")
    parser.add_argument("question")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--user-id", default="cli")
    parser.add_argument("--risk-level", default="C3")
    parser.add_argument("--session-id", default="cli")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
