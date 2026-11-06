"""Command line entry point for a single local worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .repository import BusinessError, Repository
from .runner import AgentRunner, DemoModel, OpenAIActionModel, ScriptedModel


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="content-agent")
    result.add_argument("--db", default="runtime/business.sqlite")
    result.add_argument("--checkpoint", default="runtime/checkpoints.sqlite")
    result.add_argument("--pricing-rules-version", choices=("r1", "r2"), default="r1", help="pricing version for newly submitted tasks")
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--input", required=True)
    run.add_argument("--model", choices=("demo", "script", "openai", "deepseek"), default="demo")
    run.add_argument("--script")
    run.add_argument("--model-name")
    run.add_argument("--fail-rich-generation", action="store_true")
    resume = commands.add_parser("resume")
    resume.add_argument("--task-id", required=True)
    resume.add_argument("--model", choices=("demo", "script", "openai", "deepseek"), default="demo")
    resume.add_argument("--script")
    resume.add_argument("--model-name")
    resume.add_argument("--fail-rich-generation", action="store_true")
    recover = commands.add_parser("recover")
    recover.add_argument("--task-id", required=True)
    recover.add_argument("--model", choices=("demo", "script", "openai", "deepseek"), default="demo")
    recover.add_argument("--script")
    recover.add_argument("--model-name")
    rebuild = commands.add_parser("rebuild-checkpoint")
    rebuild.add_argument("--task-id", required=True)
    rebuild.add_argument("--model", choices=("demo", "script", "openai", "deepseek"), default="demo")
    rebuild.add_argument("--script")
    rebuild.add_argument("--model-name")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--task-id", required=True)
    export = commands.add_parser("export")
    export.add_argument("--task-id", required=True)
    export.add_argument("--output", required=True)
    return result


def choose_model(args: argparse.Namespace):
    if args.model == "demo":
        return DemoModel()
    if args.model in ("openai", "deepseek"):
        return OpenAIActionModel(args.model_name, provider=args.model)
    if not args.script:
        raise BusinessError("SCRIPT_REQUIRED")
    data = json.loads(Path(args.script).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise BusinessError("INVALID_SCRIPT")
    return ScriptedModel(data)


async def execute(args: argparse.Namespace) -> dict:
    import portalocker

    if args.command == "export":
        with Repository(args.db, new_task_pricing_version=args.pricing_rules_version) as repo:
            events = repo.events(args.task_id)
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
            return {"status": "exported", "events": len(events), "output": str(target)}
    if args.command == "inspect":
        with Repository(args.db, new_task_pricing_version=args.pricing_rules_version) as repo:
            return repo.result_view(args.task_id)
    lock_path = Path(args.db).with_suffix(".worker.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(str(lock_path), mode="a", timeout=0):
            with Repository(args.db, new_task_pricing_version=args.pricing_rules_version) as repo:
                if args.command == "run":
                    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
                    submitted = repo.submit(raw)
                    if submitted["status"] != "running":
                        if submitted.get("task_id"):
                            return {**submitted, **repo.result_view(submitted["task_id"])}
                        return submitted
                    task_id = submitted["task_id"]
                else:
                    task_id = args.task_id
                model = choose_model(args)
                runner = AgentRunner(repo, model, args.checkpoint, fail_rich_generation=getattr(args, "fail_rich_generation", False))
                if args.command == "recover":
                    return await runner.recover(task_id)
                if args.command == "rebuild-checkpoint":
                    return await runner.rebuild_checkpoint(task_id)
                return await runner.run(task_id)
    except portalocker.exceptions.LockException as exc:
        raise BusinessError("RUNNER_BUSY") from exc


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(execute(args))
    except BusinessError as exc:
        result = {"status": "error", "reason_code": exc.code, "detail": str(exc)}
    except Exception as exc:
        result = {"status": "error", "reason_code": "INTERNAL_ERROR", "detail": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") not in ("error", "invalid_input") else 1


if __name__ == "__main__":
    sys.exit(main())
