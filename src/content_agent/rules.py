"""Versioned, deterministic fee, production cost, and feasibility rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum

from pydantic import Field

from .canonical import normalize_phrase
from .schemas import ArtifactStatus, ContentFormat, PlanSpec, Quote, ScriptArtifact, StrictModel, TaskInput

RULESET_VERSION = "r1"
FEE_POLICY_VERSION = "r1"
PRICING_RULES_VERSION = "r1"


class ToolName(StrEnum):
    SEARCH = "search_materials"
    GENERATE = "generate_script"
    ESTIMATE = "estimate_cost"
    SAVE = "save_plan"


class ToolOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    REJECTED = "rejected"
    NOT_EXECUTED = "not_executed"


class ProductionPrice(StrictModel):
    base_units: int
    license_units: int
    production_minutes: int

    @property
    def total_units(self) -> int:
        return self.base_units + self.license_units


PRICES = {
    ContentFormat.VIDEO: ProductionPrice(base_units=8, license_units=2, production_minutes=120),
    ContentFormat.IMAGE_TEXT: ProductionPrice(base_units=4, license_units=1, production_minutes=45),
}

# Immutable synthetic pricing files expressed in code for the local release.
# r2 is an explicit alternative for new tasks; r1 remains available for old tasks.
PRICING_RULESETS = {
    "r1": PRICES,
    "r2": {
        ContentFormat.VIDEO: ProductionPrice(base_units=9, license_units=2, production_minutes=120),
        ContentFormat.IMAGE_TEXT: ProductionPrice(base_units=5, license_units=1, production_minutes=45),
    },
}


def price_for(selected_format: ContentFormat, version: str) -> ProductionPrice:
    try:
        return PRICING_RULESETS[version][selected_format]
    except KeyError as exc:
        raise ValueError("pricing rules version unavailable") from exc


def tool_fee(tool: ToolName, outcome: ToolOutcome) -> int:
    if tool in (ToolName.SEARCH, ToolName.GENERATE) and outcome in (ToolOutcome.SUCCESS, ToolOutcome.PARTIAL):
        return 1
    return 0


def max_tool_fee(tool: ToolName) -> int:
    return 1 if tool in (ToolName.SEARCH, ToolName.GENERATE) else 0


def can_dispatch_tool(budget_units: int, incurred_tool_units: int, reserved_units: int, tool: ToolName) -> bool:
    if min(budget_units, incurred_tool_units, reserved_units) < 0:
        raise ValueError("units must be nonnegative")
    return incurred_tool_units + reserved_units + max_tool_fee(tool) <= budget_units


class Feasibility(StrictModel):
    campaign_base_units: int = Field(ge=0)
    campaign_license_units: int = Field(ge=0)
    campaign_units: int = Field(ge=0)
    incurred_tool_units: int = Field(ge=0)
    total_units: int = Field(ge=0)
    budget_shortfall_units: int = Field(ge=0)
    production_minutes: int = Field(ge=0)
    evaluated_at: datetime
    completion_estimate_at: datetime
    budget_ok: bool
    deadline_ok: bool

    @property
    def feasible(self) -> bool:
        return self.budget_ok and self.deadline_ok


def evaluate_feasibility(task: TaskInput, quote: Quote, incurred_tool_units: int, now: datetime) -> Feasibility:
    if incurred_tool_units < 0:
        raise ValueError("incurred_tool_units must be nonnegative")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include timezone")
    if quote.pricing_rules_version not in PRICING_RULESETS:
        raise ValueError("pricing rules version unavailable")
    now_utc = now.astimezone(timezone.utc)
    completion = now_utc + timedelta(minutes=quote.production_minutes)
    total = incurred_tool_units + quote.campaign_units
    return Feasibility(
        campaign_base_units=quote.base_units,
        campaign_license_units=quote.license_units,
        campaign_units=quote.campaign_units,
        incurred_tool_units=incurred_tool_units,
        total_units=total,
        budget_shortfall_units=max(0, total - task.budget_units),
        production_minutes=quote.production_minutes,
        evaluated_at=now_utc,
        completion_estimate_at=completion,
        budget_ok=total <= task.budget_units,
        deadline_ok=completion <= task.deadline_utc,
    )


def script_eligible(artifact: ScriptArtifact, spec: PlanSpec, task: TaskInput) -> bool:
    if artifact.status != ArtifactStatus.COMPLETE or not artifact.hash_valid:
        return False
    if artifact.dependency_fingerprint != spec.generation_fingerprint:
        return False
    if spec.selected_format not in task.allowed_formats:
        return False
    if spec.script_hash != artifact.content_hash:
        return False
    sections = artifact.content.model_dump()
    if any(not normalize_phrase(value) for value in sections.values()):
        return False
    searchable = normalize_phrase(" ".join(sections.values()))
    return all(normalize_phrase(point) in searchable for point in task.required_points)


def quote_matches(quote: Quote, spec: PlanSpec, artifact: ScriptArtifact) -> bool:
    if quote.spec_hash != spec.spec_hash or quote.script_hash != artifact.content_hash:
        return False
    if quote.pricing_rules_version not in PRICING_RULESETS:
        return False
    price = price_for(spec.selected_format, quote.pricing_rules_version)
    return (quote.base_units, quote.license_units, quote.production_minutes) == (
        price.base_units * spec.quantity,
        price.license_units * spec.quantity,
        price.production_minutes * spec.quantity,
    )
