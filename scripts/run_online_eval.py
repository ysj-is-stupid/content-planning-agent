"""Run paired real-model observations from disclosed prebuilt business states.

Secrets are loaded by the model adapter from .env and are never exported.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from content_agent.catalog import BY_ID
from content_agent.repository import Repository
from content_agent.schemas import ContentFormat, PlanSpec


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "examples" / "online"
DATABASE = ROOT / "runtime" / "online-evaluation.sqlite"
CHECKPOINT = ROOT / "runtime" / "online-evaluation-checkpoints.sqlite"


def input_for(case: str, budget: int) -> dict:
    return {
        "request_id": f"online-{case}-{uuid.uuid4()}",
        "topic": "新品发布",
        "budget_units": budget,
        "campaign_deadline_at": "2030-01-01T18:00:00+08:00",
        "allowed_formats": ["video", "image_text"] if case.startswith("budget") else ["image_text"],
        "required_points": ["新品"],
    }


def spec(selected_format: ContentFormat, script_hash: str | None = None) -> PlanSpec:
    material = BY_ID["launch-01"]
    return PlanSpec(
        topic_id="product_launch", selected_format=selected_format,
        channel="social", material_ids=(material.material_id,),
        material_hashes=(material.content_hash,), length=60, quantity=1,
        required_points=("新品",), generator_strategy="rich", script_hash=script_hash,
    )


def prepare(repo: Repository, case: str, task_id: str) -> dict:
    repo.search_materials(task_id, query="")
    selected_format = ContentFormat.VIDEO if case.startswith("budget") else ContentFormat.IMAGE_TEXT
    initial = repo.create_plan(task_id, spec(selected_format))
    if case.startswith("failure-permanent"):
        failed = repo.generate_script(task_id, initial["plan_id"], failure="permanent")
        return {"initial_plan_id": initial["plan_id"], "observed_tool_outcome": failed["outcome"], "prebuilt_steps": ["search_materials", "create_plan", "generate_script"]}
    generated = repo.generate_script(task_id, initial["plan_id"])
    if case.startswith("failure-success"):
        return {"initial_plan_id": initial["plan_id"], "artifact_id": generated["data"]["artifact_id"], "observed_tool_outcome": generated["outcome"], "prebuilt_steps": ["search_materials", "create_plan", "generate_script"]}
    bound = repo.create_plan(task_id, spec(selected_format, generated["data"]["content_hash"]))
    estimated = repo.estimate_cost(task_id, bound["plan_id"], generated["data"]["artifact_id"])
    return {"initial_plan_id": bound["plan_id"], "artifact_id": generated["data"]["artifact_id"], "quote_id": estimated["data"]["quote_id"], "observed_tool_outcome": "estimate_success", "prebuilt_steps": ["search_materials", "create_plan", "generate_script", "bind_script", "estimate_cost"]}


def run_case(case: str, budget: int, output: Path) -> dict:
    raw = input_for(case, budget)
    for name in ("inputs", "outputs", "traces", "setups"):
        (output / name).mkdir(parents=True, exist_ok=True)
    input_path = output / "inputs" / f"{case}.json"
    input_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with Repository(DATABASE) as repo:
        if case == "normal-full":
            setup = {"case": case, "model_start": "raw_input", "initial_budget_units": budget, "prebuilt_steps": []}
            task_id = None
        else:
            task_id = repo.submit(raw)["task_id"]
            setup = prepare(repo, case, task_id)
            setup.update({"task_id": task_id, "case": case, "model_start": "prebuilt_business_state", "initial_budget_units": budget})
    (output / "setups" / f"{case}.json").write_text(json.dumps(setup, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    command = [sys.executable, "-m", "content_agent.cli", "--db", str(DATABASE), "--checkpoint", str(CHECKPOINT)]
    command += ["run", "--input", str(input_path)] if case == "normal-full" else ["resume", "--task-id", task_id]
    command += ["--model", "deepseek"]
    if case.startswith("failure-permanent"):
        command.append("--fail-rich-generation")
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    result = json.loads(completed.stdout) if completed.stdout.strip() else {"status": "error", "reason_code": "NO_CLI_OUTPUT"}
    result["cli_exit_code"] = completed.returncode
    (output / "outputs" / f"{case}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    task_id = task_id or result.get("task_id")
    with Repository(DATABASE) as repo:
        events = repo.events(task_id) if task_id else []
        decisions = [json.loads(row["response_json"]) for row in repo.connection.execute("SELECT response_json FROM decisions WHERE task_id=? ORDER BY seq", (task_id,))] if task_id else []
    (output / "traces" / f"{case}.jsonl").write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
    return {"case": case, "status": result.get("status"), "reason_code": result.get("reason_code"), "first_model_action": decisions[0]["action"] if decisions else None, "model_decisions": len(decisions), "tool_units": result.get("incurred_tool_units"), "total_units": (result.get("last_result") or {}).get("total_units")}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("cases", nargs="*", choices=("normal-full", "budget-low", "budget-high", "failure-permanent", "failure-success"))
    args = parser.parse_args()
    cases = args.cases or ["budget-low", "budget-high", "failure-permanent", "failure-success"]
    output = args.output_dir.resolve()
    summaries = [run_case(case, 20 if case == "budget-high" else 10, output) for case in cases]
    report = {"evaluated_at": datetime.now(timezone.utc).isoformat(), "provider": "deepseek", "model": "deepseek-chat", "cases": summaries}
    summary_name = "summary.json" if len(cases) == 4 and cases == ["budget-low", "budget-high", "failure-permanent", "failure-success"] else f"summary-{'-'.join(cases)}.json"
    (output / summary_name).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for summary in summaries:
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
