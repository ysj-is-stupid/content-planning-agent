"""Strict input, plan, artifact, and quote contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import fingerprint, normalize_phrase, normalize_text


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ContentFormat(StrEnum):
    VIDEO = "video"
    IMAGE_TEXT = "image_text"


class ArtifactStatus(StrEnum):
    PARTIAL = "partial"
    COMPLETE = "complete"
    INVALID = "invalid"


class TaskInput(StrictModel):
    topic: str = Field(min_length=1, max_length=200)
    budget_units: int = Field(ge=0)
    campaign_deadline_at: datetime
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    allowed_formats: tuple[ContentFormat, ...] = (ContentFormat.VIDEO, ContentFormat.IMAGE_TEXT)
    required_points: tuple[str, ...] = ()
    parent_task_id: str | None = None

    @field_validator("topic", "request_id", "parent_task_id")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return normalize_text(value) if value is not None else None

    @field_validator("campaign_deadline_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("campaign_deadline_at must include timezone")
        return value

    @field_validator("allowed_formats")
    @classmethod
    def normalize_formats(cls, value: tuple[ContentFormat, ...]) -> tuple[ContentFormat, ...]:
        if not value:
            raise ValueError("allowed_formats cannot be empty")
        return tuple(sorted(set(value), key=str))

    @field_validator("required_points")
    @classmethod
    def normalize_points(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 12:
            raise ValueError("at most 12 required_points")
        result = tuple(dict.fromkeys(normalize_phrase(point) for point in value))
        if any(not point or len(point) > 80 for point in result):
            raise ValueError("required_points must be nonempty and at most 80 characters")
        return result

    @property
    def deadline_utc(self) -> datetime:
        return self.campaign_deadline_at.astimezone(timezone.utc)

    @property
    def input_hash(self) -> str:
        return fingerprint(self.model_dump(exclude={"request_id"}))


class SubmissionResult(StrictModel):
    status: Literal["ready", "needs_input", "invalid_input", "incomplete"]
    missing_fields: tuple[str, ...] = ()
    reason_code: str | None = None
    task_input: TaskInput | None = None


class PlanSpec(StrictModel):
    topic_id: str
    selected_format: ContentFormat
    channel: str = Field(min_length=1, max_length=80)
    material_ids: tuple[str, ...]
    material_hashes: tuple[str, ...]
    length: int = Field(ge=1, le=10000)
    quantity: int = Field(ge=1, le=100)
    required_points: tuple[str, ...] = ()
    generator_strategy: Literal["rich", "compact"]
    script_hash: str | None = None

    @model_validator(mode="after")
    def material_pairs(self) -> "PlanSpec":
        if not self.material_ids or len(self.material_ids) != len(self.material_hashes):
            raise ValueError("material_ids and material_hashes must be nonempty paired lists")
        if len(self.material_ids) != len(set(self.material_ids)):
            raise ValueError("duplicate material_ids")
        if self.material_ids != tuple(sorted(self.material_ids)):
            raise ValueError("material_ids and material_hashes must be sorted as pairs")
        return self

    @property
    def spec_hash(self) -> str:
        return fingerprint(self)

    @property
    def generation_fingerprint(self) -> str:
        return fingerprint(self.model_dump(exclude={"script_hash"}))


class ScriptBody(StrictModel):
    title: str = ""
    opening: str = ""
    body: str = ""
    call_to_action: str = ""


class ScriptArtifact(StrictModel):
    artifact_id: str
    task_id: str
    dependency_fingerprint: str
    status: ArtifactStatus
    content: ScriptBody
    content_hash: str

    @property
    def hash_valid(self) -> bool:
        return self.content_hash == fingerprint(self.content)


class Quote(StrictModel):
    quote_id: str
    task_id: str
    plan_id: str
    spec_hash: str
    script_hash: str
    pricing_rules_version: str
    base_units: int = Field(ge=0)
    license_units: int = Field(ge=0)
    production_minutes: int = Field(ge=0)

    @property
    def campaign_units(self) -> int:
        return self.base_units + self.license_units


def parse_submission(raw: dict, now: datetime) -> SubmissionResult:
    missing = tuple(field for field in ("topic", "budget_units", "campaign_deadline_at") if field not in raw or raw[field] is None)
    if missing:
        return SubmissionResult(status="needs_input", missing_fields=missing, reason_code="MISSING_INPUT")
    try:
        parsed = dict(raw)
        if isinstance(parsed["campaign_deadline_at"], str):
            parsed["campaign_deadline_at"] = datetime.fromisoformat(parsed["campaign_deadline_at"].replace("Z", "+00:00"))
        if isinstance(parsed.get("allowed_formats"), list):
            parsed["allowed_formats"] = tuple(ContentFormat(item) for item in parsed["allowed_formats"])
        if isinstance(parsed.get("required_points"), list):
            parsed["required_points"] = tuple(parsed["required_points"])
        task_input = TaskInput.model_validate(parsed)
    except Exception:
        return SubmissionResult(status="invalid_input", reason_code="INVALID_INPUT")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include timezone")
    if task_input.deadline_utc <= now.astimezone(timezone.utc):
        return SubmissionResult(status="incomplete", reason_code="DEADLINE_EXCEEDED", task_input=task_input)
    return SubmissionResult(status="ready", task_input=task_input)
