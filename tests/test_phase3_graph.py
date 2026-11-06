import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from content_agent.repository import Repository
from content_agent.runner import AgentRunner, DemoModel, ScriptedModel


def input_data(request_id, budget=10, allowed=None):
    return {
        "request_id": request_id,
        "topic": "新品发布",
        "budget_units": budget,
        "campaign_deadline_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        "allowed_formats": allowed or ["video", "image_text"],
        "required_points": ["新品"],
    }


def test_graph_budget_observation_and_replan(tmp_path):
    with Repository(tmp_path / "business.sqlite") as repo:
        task_id = repo.submit(input_data("budget"))["task_id"]
        runner = AgentRunner(repo, DemoModel(), tmp_path / "checkpoints.sqlite")
        result = asyncio.run(runner.run(task_id))
        assert result["status"] == "completed"
        assert result["incurred_tool_units"] == 3
        assert result["last_result"]["total_units"] == 8
        assert result["delivery"]["task_plan"]["specification"]["selected_format"] == "image_text"
        assert result["delivery"]["cost_summary"]["total_units"] == 8
        assert any(
            item["tool"] == "estimate_cost" and item["result"].get("current_feasibility", item["result"].get("data", {}).get("feasibility", {})).get("budget_ok") is False
            for item in result["delivery"]["tool_results"]
        )
        events = repo.events(task_id)
        estimates = [event for event in events if event["kind"] == "operation_finished" and '"estimate_cost"' in event["payload_json"]]
        assert len(estimates) == 2
        decisions = [event for event in events if event["kind"] == "model_decision"]
        assert len(decisions) == 10
        assert any('"image_text"' in event["payload_json"] for event in decisions)


def test_graph_failure_observation_changes_strategy(tmp_path):
    with Repository(tmp_path / "business.sqlite") as repo:
        task_id = repo.submit(input_data("failure", budget=12, allowed=["image_text"]))["task_id"]
        runner = AgentRunner(repo, DemoModel(), tmp_path / "checkpoints.sqlite", fail_rich_generation=True)
        result = asyncio.run(runner.run(task_id))
        assert result["status"] == "completed"
        assert result["incurred_tool_units"] == 2
        attempts = list(repo.connection.execute("SELECT outcome FROM attempts ORDER BY rowid"))
        assert [row["outcome"] for row in attempts] == ["success", "failed", "success", "success"]
        assert any('"compact"' in event["payload_json"] for event in repo.events(task_id) if event["kind"] == "model_decision")


def test_two_invalid_model_actions_stop_without_tools(tmp_path):
    actions = [
        {"action": "unknown_tool", "arguments": {}, "reason_summary": "无效动作", "observed_event_ids": []},
        {"action": "unknown_tool", "arguments": {}, "reason_summary": "仍是无效动作", "observed_event_ids": []},
    ]
    with Repository(tmp_path / "business.sqlite") as repo:
        task_id = repo.submit(input_data("invalid"))["task_id"]
        result = asyncio.run(AgentRunner(repo, ScriptedModel(actions), tmp_path / "checkpoints.sqlite").run(task_id))
        assert result["status"] == "incomplete"
        assert result["reason_code"] == "INVALID_ACTION_LIMIT"
        assert repo.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


def test_explicit_resume_in_new_process_reuses_search(tmp_path):
    input_file = tmp_path / "input.json"
    script_file = tmp_path / "script.json"
    input_file.write_text(json.dumps(input_data("resume", budget=10, allowed=["image_text"])), encoding="utf-8")
    script_file.write_text(json.dumps([
        {"action": "search_materials", "arguments": {"query": ""}, "reason_summary": "检索", "observed_event_ids": []},
        {"action": "stop", "arguments": {"reason_code": "PAUSE_FOR_TEST"}, "reason_summary": "暂时停止", "observed_event_ids": []},
    ]), encoding="utf-8")
    db = tmp_path / "business.sqlite"
    checkpoint = tmp_path / "checkpoints.sqlite"
    env = dict(os.environ, PYTHONPATH=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
    first = subprocess.run([sys.executable, "-m", "content_agent.cli", "--db", str(db), "--checkpoint", str(checkpoint), "run", "--input", str(input_file), "--model", "script", "--script", str(script_file)], capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    task_id = json.loads(first.stdout)["task_id"]
    second = subprocess.run([sys.executable, "-m", "content_agent.cli", "--db", str(db), "--checkpoint", str(checkpoint), "resume", "--task-id", task_id, "--model", "demo"], capture_output=True, text=True, env=env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout)["status"] == "completed"
    with Repository(db) as repo:
        assert repo.incurred_units(task_id) == 2
        assert repo.connection.execute("SELECT COUNT(*) FROM runs WHERE task_id=?", (task_id,)).fetchone()[0] == 2
        assert repo.connection.execute("SELECT COUNT(*) FROM operations WHERE tool_name='search_materials'").fetchone()[0] == 1


def test_active_runtime_timeout_cancels_model_without_tool_charge(tmp_path):
    class SlowModel:
        async def decide(self, context, seq):
            await asyncio.sleep(0.2)
            return {"action": "search_materials", "arguments": {}, "reason_summary": "迟到响应", "observed_event_ids": []}

    with Repository(tmp_path / "business.sqlite") as repo:
        task_id = repo.submit(input_data("timeout"))["task_id"]
        runner = AgentRunner(repo, SlowModel(), tmp_path / "checkpoints.sqlite", max_active_runtime_seconds=0.05)
        result = asyncio.run(runner.run(task_id))
        assert result["status"] == "incomplete"
        assert result["reason_code"] == "ACTIVE_RUNTIME_LIMIT"
        assert result["delivery"]["incomplete_reason"] == "ACTIVE_RUNTIME_LIMIT"
        assert repo.incurred_units(task_id) == 0
        assert repo.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
