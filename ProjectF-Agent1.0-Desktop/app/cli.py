from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import uvicorn

from .config import default_workspace_root
from .harness import Harness
from .schemas import PetContext, TaskRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project F AI 宠物管家 Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="执行一次本地媒体分析")
    run.add_argument(
        "--skill",
        required=True,
        choices=[
            "pet-report-analysis",
            "home-health-check-dental",
            "home-health-check-gait",
            "home-health-check-behavior",
            "home-health-check-stool",
            "home-health-check-xray",
        ],
    )
    run.add_argument("--media", required=True)
    run.add_argument("--mode", choices=["fake", "real"], default="fake")
    run.add_argument("--pet-id")
    run.add_argument("--pet-name")
    serve = sub.add_parser("serve", help="启动 HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


async def run_once(args: argparse.Namespace) -> None:
    harness = Harness(default_workspace_root())
    response = await harness.execute(
        TaskRequest(
            skill_name=args.skill,
            media_path=str(Path(args.media).resolve()),
            pet=PetContext(pet_id=args.pet_id, pet_name=args.pet_name),
            mode=args.mode,
        )
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(response.model_dump(), ensure_ascii=False, indent=2))


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "serve":
        uvicorn.run("app.api:app", host=args.host, port=args.port, reload=False)
    else:
        asyncio.run(run_once(args))


if __name__ == "__main__":
    main()
