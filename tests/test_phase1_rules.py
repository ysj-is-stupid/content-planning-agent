from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from content_agent.canonical import fingerprint
from content_agent.rules import (
    PRICES,
    PRICING_RULES_VERSION,
    ToolName,
    ToolOutcome,
    can_dispatch_tool,
    evaluate_feasibility,
    quote_matches,
    script_eligible,
    tool_fee,
)
from content_agent.schemas import (
    ArtifactStatus,
    ContentFormat,
    PlanSpec,
    Quote,
    ScriptArtifact,
    ScriptBody,
    TaskInput,
    parse_submission,
)

NOW = datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc)


def task(budget=10, deadline=None, allowed=None):
    return TaskInput(
        topic="新品发布",
        budget_units=budget,
        campaign_deadline_at=deadline or NOW + timedelta(hours=4),
        allowed_formats=allowed or (ContentFormat.VIDEO, ContentFormat.IMAGE_TEXT),
        required_points=("新品",),
    )


def spec(format=ContentFormat.VIDEO, script_hash=None, strategy="rich"):
    return PlanSpec(
        topic_id="product_launch",
        selected_format=format,
        channel="social",
        material_ids=("m1",),
        material_hashes=("h1",),
        length=60,
        quantity=1,
        required_points=("新品",),
        generator_strategy=strategy,
        script_hash=script_hash,
    )


def quote(plan, quote_id="q1"):
    price = PRICES[plan.selected_format]
    return Quote(
        quote_id=quote_id,
        task_id="t1",
        plan_id="p1",
        spec_hash=plan.spec_hash,
        script_hash=plan.script_hash or "",
        pricing_rules_version=PRICING_RULES_VERSION,
        base_units=price.base_units,
        license_units=price.license_units,
        production_minutes=price.production_minutes,
    )


def test_missing_input_needs_input_before_any_work():
    for raw, missing in [
        ({"topic": "新品发布", "campaign_deadline_at": NOW.isoformat()}, ("budget_units",)),
        ({"topic": "新品发布", "budget_units": 10}, ("campaign_deadline_at",)),
        ({"topic": "新品发布"}, ("budget_units", "campaign_deadline_at")),
    ]:
        result = parse_submission(raw, NOW)
        assert result.status == "needs_input"
        assert result.missing_fields == missing
        assert result.task_input is None


def test_strict_input_and_deadline_require_timezone():
    assert parse_submission({"topic": "新品发布", "budget_units": -1, "campaign_deadline_at": NOW.isoformat()}, NOW).status == "invalid_input"
    assert parse_submission({"topic": "新品发布", "budget_units": 10, "campaign_deadline_at": "2026-11-06T12:00:00"}, NOW).status == "invalid_input"
    assert parse_submission({"topic": "新品发布", "budget_units": 10, "campaign_deadline_at": NOW.isoformat()}, NOW).reason_code == "DEADLINE_EXCEEDED"
    with pytest.raises(ValidationError):
        TaskInput(topic="x", budget_units=-1, campaign_deadline_at=NOW)


def test_fee_components_and_sunk_cost_replanning():
    original = spec(script_hash="video-script")
    first = evaluate_feasibility(task(), quote(original), incurred_tool_units=2, now=NOW)
    assert (first.campaign_base_units, first.campaign_license_units, first.campaign_units, first.incurred_tool_units, first.total_units, first.budget_shortfall_units) == (8, 2, 10, 2, 12, 2)
    assert not first.feasible
    revised = spec(ContentFormat.IMAGE_TEXT, "image-script")
    second = evaluate_feasibility(task(), quote(revised), incurred_tool_units=3, now=NOW)
    assert (second.campaign_base_units, second.campaign_license_units, second.total_units) == (4, 1, 8)
    assert second.feasible
    assert tool_fee(ToolName.SEARCH, ToolOutcome.SUCCESS) == 1
    assert tool_fee(ToolName.GENERATE, ToolOutcome.PARTIAL) == 1
    assert tool_fee(ToolName.GENERATE, ToolOutcome.FAILED) == 0
    assert tool_fee(ToolName.ESTIMATE, ToolOutcome.SUCCESS) == 0
    assert can_dispatch_tool(3, 2, 0, ToolName.GENERATE)
    assert not can_dispatch_tool(2, 2, 0, ToolName.GENERATE)


def test_deadline_exact_boundary_and_time_advancement():
    plan = spec(ContentFormat.IMAGE_TEXT, "script")
    due = NOW + timedelta(minutes=45)
    exact = evaluate_feasibility(task(deadline=due), quote(plan), 1, NOW)
    late = evaluate_feasibility(task(deadline=due), quote(plan), 1, NOW + timedelta(seconds=1))
    assert exact.deadline_ok
    assert not late.deadline_ok
    assert late.completion_estimate_at == due + timedelta(seconds=1)


def test_plan_fingerprint_changes_only_for_business_spec():
    a = spec()
    assert a.spec_hash == spec().spec_hash
    assert a.spec_hash != spec(strategy="compact").spec_hash
    assert a.generation_fingerprint == spec(script_hash="bound").generation_fingerprint


def test_partial_corrupt_and_stale_script_cannot_be_quoted():
    body = ScriptBody(title="新品", opening="开场", body="介绍新品", call_to_action="立即了解")
    pending = spec()
    artifact = ScriptArtifact(
        artifact_id="a1", task_id="t1", dependency_fingerprint=pending.generation_fingerprint,
        status=ArtifactStatus.COMPLETE, content=body, content_hash=fingerprint(body),
    )
    bound = pending.model_copy(update={"script_hash": artifact.content_hash})
    assert script_eligible(artifact, bound, task())
    assert quote_matches(quote(bound), bound, artifact)
    assert not script_eligible(artifact.model_copy(update={"status": ArtifactStatus.PARTIAL}), bound, task())
    assert not script_eligible(artifact.model_copy(update={"content_hash": "tampered"}), bound, task())
    assert not script_eligible(artifact, spec(script_hash=artifact.content_hash, strategy="compact"), task())
    assert not script_eligible(artifact, bound, task(allowed=(ContentFormat.IMAGE_TEXT,)))
    assert not quote_matches(quote(bound), bound.model_copy(update={"length": 30}), artifact)
