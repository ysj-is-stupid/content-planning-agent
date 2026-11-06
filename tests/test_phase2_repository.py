from datetime import datetime, timedelta, timezone

import pytest

from content_agent.catalog import BY_ID
from content_agent.repository import BusinessError, Repository
from content_agent.rules import ToolName, ToolOutcome
from content_agent.schemas import ContentFormat, PlanSpec

NOW = datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc)


def input_data(request_id="r1", budget=10, allowed=None, deadline=None):
    return {
        "request_id": request_id,
        "topic": "新品发布",
        "budget_units": budget,
        "campaign_deadline_at": (deadline or NOW + timedelta(hours=4)).isoformat(),
        "allowed_formats": allowed or ["video", "image_text"],
        "required_points": ["新品"],
    }


def plan_spec(selected_format=ContentFormat.IMAGE_TEXT, script_hash=None, strategy="rich"):
    return PlanSpec(
        topic_id="product_launch", selected_format=selected_format, channel="social",
        material_ids=("launch-01",), material_hashes=(BY_ID["launch-01"].content_hash,),
        length=60, quantity=1, required_points=("新品",),
        generator_strategy=strategy, script_hash=script_hash,
    )


def prepare(db, task_id, selected_format=ContentFormat.IMAGE_TEXT):
    first = db.create_plan(task_id, plan_spec(selected_format))
    generated = db.generate_script(task_id, first["plan_id"])
    artifact_id = generated["data"]["artifact_id"]
    bound = db.create_plan(task_id, plan_spec(selected_format, generated["data"]["content_hash"]))
    estimated = db.estimate_cost(task_id, bound["plan_id"], artifact_id)
    return bound["plan_id"], artifact_id, estimated["data"]["quote_id"]


def test_normal_save_repeated_request_and_receipts(tmp_path):
    path = tmp_path / "business.sqlite"
    with Repository(path) as db:
        submission = db.submit(input_data(), NOW)
        task_id = submission["task_id"]
        searched = db.search_materials(task_id, now=NOW)
        replay = db.search_materials(task_id, now=NOW)
        assert searched["data"] == replay["data"]
        assert replay["replayed"]
        plan_id, artifact_id, quote_id = prepare(db, task_id)
        saved = db.save_plan(task_id, plan_id, artifact_id, quote_id, NOW)
        assert saved["total_units"] == 7
        assert db.save_plan(task_id, plan_id, artifact_id, quote_id, NOW)["replayed"]
        assert db.submit(input_data(), NOW)["replayed"]
        assert db.submit(input_data(), NOW + timedelta(days=5))["status"] == "completed"
        assert db.snapshot(task_id)["incurred_tool_units"] == 2
        result = db.result_view(task_id)
        delivery = result["delivery"]
        assert delivery["task_plan"]["plan_id"] == plan_id
        assert delivery["task_plan"]["specification"]["selected_format"] == "image_text"
        assert delivery["task_plan"]["script"]["content"]["body"]
        assert [step["status"] for step in delivery["steps"]] == ["succeeded"] * 5
        assert [item["tool"] for item in delivery["tool_results"]] == [
            "search_materials", "generate_script", "estimate_cost", "save_plan",
        ]
        assert delivery["cost_summary"] == {
            "budget_units": 10,
            "incurred_tool_units": 2,
            "campaign_base_units": 4,
            "campaign_license_units": 1,
            "campaign_units": 5,
            "total_units": 7,
            "remaining_budget_units": 3,
        }
        assert delivery["saved_plan"]["saved_plan_id"] == saved["saved_plan_id"]
        assert delivery["incomplete_reason"] is None
        assert db.connection.execute("SELECT COUNT(*) FROM fee_events").fetchone()[0] == 2
        assert db.connection.execute("SELECT COUNT(*) FROM saved_plans").fetchone()[0] == 1
    with Repository(path) as reopened:
        assert reopened.result_view(task_id)["delivery"]["task_plan"]["script"]["content"]["title"]
        assert reopened.search_materials(task_id, now=NOW)["replayed"]


def test_budget_replan_preserves_sunk_fee(tmp_path):
    with Repository(tmp_path / "business.sqlite") as db:
        task_id = db.submit(input_data(), NOW)["task_id"]
        db.search_materials(task_id, now=NOW)
        video_id, video_artifact, video_quote = prepare(db, task_id, ContentFormat.VIDEO)
        video_result = db.estimate_cost(task_id, video_id, video_artifact, NOW)
        assert video_result["current_feasibility"]["total_units"] == 12
        with pytest.raises(BusinessError, match="BUDGET_EXCEEDED"):
            db.save_plan(task_id, video_id, video_artifact, video_quote, NOW)
        image_id, image_artifact, image_quote = prepare(db, task_id)
        assert db.save_plan(task_id, image_id, image_artifact, image_quote, NOW)["total_units"] == 8
        assert db.snapshot(task_id)["incurred_tool_units"] == 3


def test_partial_and_failed_generation_do_not_hide_history(tmp_path):
    with Repository(tmp_path / "business.sqlite") as db:
        task_id = db.submit(input_data(), NOW)["task_id"]
        db.search_materials(task_id, now=NOW)
        plan_id = db.create_plan(task_id, plan_spec())["plan_id"]
        failed = db.generate_script(task_id, plan_id, failure="permanent", now=NOW)
        assert failed["outcome"] == "failed" and failed["fee_units"] == 0
        partial = db.generate_script(task_id, plan_id, mode="partial", now=NOW)
        assert partial["outcome"] == "partial"
        partial_id = partial["data"]["artifact_id"]
        with pytest.raises(BusinessError, match="SCRIPT_INELIGIBLE"):
            db.estimate_cost(task_id, plan_id, partial_id, NOW)
        complete = db.generate_script(task_id, plan_id, mode="complete_partial", partial_artifact_id=partial_id, now=NOW)
        assert complete["outcome"] == "success"
        assert db.snapshot(task_id)["incurred_tool_units"] == 3
        assert db.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 4


def test_request_conflict_and_hard_constraints(tmp_path):
    with Repository(tmp_path / "business.sqlite") as db:
        task_id = db.submit(input_data(allowed=["video"]), NOW)["task_id"]
        with pytest.raises(BusinessError, match="REQUEST_CONFLICT"):
            db.submit(input_data(budget=11, allowed=["video"]), NOW)
        with pytest.raises(BusinessError, match="HARD_CONSTRAINT"):
            db.create_plan(task_id, plan_spec(ContentFormat.IMAGE_TEXT), NOW)
        assert db.snapshot(task_id)["incurred_tool_units"] == 0


def test_plan_material_must_have_appeared_in_task_search(tmp_path):
    with Repository(tmp_path / "business.sqlite") as db:
        task_id = db.submit(input_data(), NOW)["task_id"]
        with pytest.raises(BusinessError, match="MATERIAL_NOT_OBSERVED"):
            db.create_plan(task_id, plan_spec(), NOW)
        db.search_materials(task_id, query="新品亮点", now=NOW)
        assert db.create_plan(task_id, plan_spec(), NOW)["created"]


def test_topic_words_in_search_query_find_synthetic_materials(tmp_path):
    with Repository(tmp_path / "business.sqlite") as db:
        task_id = db.submit(input_data(), NOW)["task_id"]
        result = db.search_materials(task_id, query="新品发布 图文素材", selected_format=ContentFormat.IMAGE_TEXT, now=NOW)
        assert result["outcome"] == "success"
        assert [item["material_id"] for item in result["data"]["materials"]] == ["launch-01", "launch-02"]


def test_quote_revalidated_at_save_time(tmp_path):
    with Repository(tmp_path / "business.sqlite") as db:
        task_id = db.submit(input_data(deadline=NOW + timedelta(minutes=46)), NOW)["task_id"]
        db.search_materials(task_id, now=NOW)
        plan_id, artifact_id, quote_id = prepare(db, task_id)
        with pytest.raises(BusinessError, match="DEADLINE_EXCEEDED"):
            db.save_plan(task_id, plan_id, artifact_id, quote_id, NOW + timedelta(minutes=2))
        assert db.snapshot(task_id)["status"] == "running"
        assert db.connection.execute("SELECT COUNT(*) FROM saved_plans").fetchone()[0] == 0


def test_pending_operation_replays_after_crash_window(tmp_path):
    path = tmp_path / "business.sqlite"
    with Repository(path) as db:
        task_id = db.submit(input_data(), NOW)["task_id"]
        args = {"probe": "crash-window"}
        def crash():
            raise RuntimeError("simulated process loss after intent commit")
        with pytest.raises(RuntimeError):
            db._tool(task_id, ToolName.SEARCH, args, crash, NOW)
        key = db._operation_key(task_id, ToolName.SEARCH, args)
        assert db.connection.execute("SELECT status FROM operations WHERE operation_key=?", (key,)).fetchone()[0] == "pending"
    with Repository(path) as reopened:
        task_id = reopened.submit(input_data(), NOW)["task_id"]
        def recovered():
            return ToolOutcome.SUCCESS, {"materials": []}, None
        result = reopened._tool(task_id, ToolName.SEARCH, args, recovered, NOW)
        assert result["fee_units"] == 1
        assert reopened._tool(task_id, ToolName.SEARCH, args, crash, NOW)["replayed"]
        assert [row[0] for row in reopened.connection.execute("SELECT outcome FROM attempts WHERE operation_key=? ORDER BY rowid", (key,))] == ["aborted", "success"]
        assert reopened.connection.execute("SELECT COUNT(*) FROM fee_events WHERE operation_key=?", (key,)).fetchone()[0] == 1
