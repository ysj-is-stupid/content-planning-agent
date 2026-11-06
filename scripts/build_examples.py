"""Run the offline demo in isolated local storage and export actual results."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from content_agent.repository import Repository


ROOT = Path(__file__).resolve().parents[1]
CASES = ("normal", "missing-input", "budget-replan", "tool-failure", "no-feasible", "duplicate")


def audit_event(kind: str, payload: dict, task_id: str | None = None, revision: int = 0) -> dict:
    """Create an explicit non-model audit record from an observed CLI result."""
    return {
        "event_id": None,
        "task_id": task_id,
        "revision": revision,
        "kind": kind,
        "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "at": datetime.now(timezone.utc).isoformat(),
    }


def run_cli(database: Path, checkpoint: Path, case: str) -> dict:
    command = [
        sys.executable, "-m", "content_agent.cli",
        "--db", str(database), "--checkpoint", str(checkpoint),
        "run", "--input", str(ROOT / "examples" / "inputs" / f"{case}.json"),
        "--model", "demo",
    ]
    if case == "tool-failure":
        command.append("--fail-rich-generation")
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"{case} failed: {completed.stdout}\n{completed.stderr}")
    return json.loads(completed.stdout)


def task_counts(repo: Repository, task_id: str) -> dict[str, int]:
    task = repo.snapshot(task_id)
    return {
        "revision": task["revision"],
        "tool_dispatch_count": task["tool_dispatch_count"],
        "model_requests": repo.connection.execute("SELECT COUNT(*) FROM model_requests WHERE task_id=?", (task_id,)).fetchone()[0],
        "decisions": repo.connection.execute("SELECT COUNT(*) FROM decisions WHERE task_id=?", (task_id,)).fetchone()[0],
        "operations": repo.connection.execute("SELECT COUNT(*) FROM operations WHERE task_id=?", (task_id,)).fetchone()[0],
        "fee_events": repo.connection.execute("SELECT COUNT(*) FROM fee_events WHERE task_id=?", (task_id,)).fetchone()[0],
        "fee_units": repo.incurred_units(task_id),
        "saved_plans": repo.connection.execute("SELECT COUNT(*) FROM saved_plans WHERE task_id=?", (task_id,)).fetchone()[0],
    }


def main() -> None:
    (ROOT / "examples" / "outputs").mkdir(parents=True, exist_ok=True)
    (ROOT / "examples" / "traces").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "business.sqlite"
        checkpoint = Path(directory) / "checkpoints.sqlite"
        for case in CASES:
            first_result = run_cli(database, checkpoint, case)
            result = first_result
            task_id = first_result.get("task_id")
            with Repository(database) as repo:
                events = repo.events(task_id) if task_id else []

            if case == "missing-input":
                events.append(audit_event("submission_validation", {
                    "source": "actual_cli_result",
                    "request_id": json.loads((ROOT / "examples" / "inputs" / f"{case}.json").read_text(encoding="utf-8")).get("request_id"),
                    "status": first_result["status"],
                    "missing_fields": first_result["missing_fields"],
                    "reason_code": first_result["reason_code"],
                    "model_requests": 0,
                    "tool_operations": 0,
                    "next_action": "request_missing_fields",
                    "decision_basis": "必填输入缺失，输入守卫在创建任务和调用模型前停止",
                }))

            if case == "duplicate":
                assert task_id is not None
                with Repository(database) as repo:
                    before_replay = task_counts(repo, task_id)
                    first_saved_plan_id = repo.snapshot(task_id)["saved_plan_id"]
                replay_result = run_cli(database, checkpoint, case)
                with Repository(database) as repo:
                    after_replay = task_counts(repo, task_id)
                    request_rows = repo.connection.execute(
                        "SELECT COUNT(*) FROM tasks WHERE request_id=?",
                        (first_result["request_id"],),
                    ).fetchone()[0]
                checks = {
                    "replayed": replay_result.get("replayed") is True,
                    "same_task_id": replay_result.get("task_id") == task_id,
                    "same_saved_plan_id": replay_result.get("saved_plan_id") == first_saved_plan_id,
                    "one_task_for_request_id": request_rows == 1,
                    "no_new_revision": after_replay["revision"] == before_replay["revision"],
                    "no_new_model_request": after_replay["model_requests"] == before_replay["model_requests"],
                    "no_new_decision": after_replay["decisions"] == before_replay["decisions"],
                    "no_new_tool_dispatch": after_replay["tool_dispatch_count"] == before_replay["tool_dispatch_count"],
                    "no_new_operation": after_replay["operations"] == before_replay["operations"],
                    "no_new_fee": after_replay["fee_events"] == before_replay["fee_events"] and after_replay["fee_units"] == before_replay["fee_units"],
                    "no_new_saved_plan": after_replay["saved_plans"] == before_replay["saved_plans"],
                }
                if not all(checks.values()):
                    raise RuntimeError(f"duplicate invariants failed: {checks}")
                result = {
                    "case": "duplicate",
                    "status": replay_result["status"],
                    "task_id": task_id,
                    "request_id": first_result["request_id"],
                    "saved_plan_id": first_saved_plan_id,
                    "first_result": first_result,
                    "replay_result": replay_result,
                    "before_replay": before_replay,
                    "after_replay": after_replay,
                    "checks": checks,
                }
                events.append(audit_event("submission_replay_observed", {
                    "source": "actual_second_cli_result",
                    "request_id": first_result["request_id"],
                    "replayed": replay_result["replayed"],
                    "before_replay": before_replay,
                    "after_replay": after_replay,
                    "checks": checks,
                    "next_action": "return_existing_result",
                    "decision_basis": "相同 request_id 与相同输入命中已完成任务，不重新调用模型或工具",
                }, task_id=task_id, revision=after_replay["revision"]))

            (ROOT / "examples" / "outputs" / f"{case}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (ROOT / "examples" / "traces" / f"{case}.jsonl").write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8"
            )
            print(f"{case}: {result['status']} ({len(events)} events)")


if __name__ == "__main__":
    main()
