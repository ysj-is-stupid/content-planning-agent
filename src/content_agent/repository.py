"""SQLite authority for task, tool receipt, fee, artifact, quote, and save facts."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .canonical import fingerprint
from .catalog import BY_ID, CATALOG_VERSION, resolve_topic, search
from .rules import (
    FEE_POLICY_VERSION, PRICING_RULESETS, PRICING_RULES_VERSION, ToolName, ToolOutcome,
    can_dispatch_tool, evaluate_feasibility, price_for, quote_matches, script_eligible, tool_fee,
)
from .schemas import (
    ArtifactStatus, ContentFormat, PlanSpec, Quote, ScriptArtifact, ScriptBody,
    TaskInput, parse_submission,
)


class BusinessError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


class TransientToolError(Exception):
    """A known failure before a local tool result was committed."""

    def __init__(self, code: str = "TRANSIENT_TOOL_ERROR"):
        super().__init__(code)
        self.code = code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Repository:
    def __init__(self, path: str | Path, new_task_pricing_version: str = PRICING_RULES_VERSION):
        if new_task_pricing_version not in PRICING_RULESETS:
            raise BusinessError("RULESET_UNAVAILABLE")
        self.new_task_pricing_version = new_task_pricing_version
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=1000")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _create_schema(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
              input_hash TEXT NOT NULL, input_json TEXT NOT NULL, topic_id TEXT NOT NULL,
              status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
              tool_dispatch_count INTEGER NOT NULL DEFAULT 0,
              current_plan_id TEXT, saved_plan_id TEXT,
              catalog_version TEXT NOT NULL, fee_policy_version TEXT NOT NULL,
              pricing_rules_version TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plans (
              plan_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              revision INTEGER NOT NULL, spec_hash TEXT NOT NULL, spec_json TEXT NOT NULL,
              parent_plan_id TEXT, UNIQUE(task_id, spec_hash), UNIQUE(task_id, revision)
            );
            CREATE TABLE IF NOT EXISTS operations (
              operation_key TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              tool_name TEXT NOT NULL, args_json TEXT NOT NULL,
              status TEXT NOT NULL, outcome TEXT, result_json TEXT,
              created_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS attempts (
              attempt_id TEXT PRIMARY KEY, operation_key TEXT NOT NULL REFERENCES operations(operation_key),
              outcome TEXT NOT NULL, error_code TEXT, at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
              artifact_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              operation_key TEXT NOT NULL UNIQUE REFERENCES operations(operation_key),
              artifact_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quotes (
              quote_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              operation_key TEXT NOT NULL UNIQUE REFERENCES operations(operation_key),
              quote_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fee_events (
              charge_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              operation_key TEXT NOT NULL UNIQUE REFERENCES operations(operation_key),
              tool_name TEXT NOT NULL, outcome TEXT NOT NULL,
              fee_policy_version TEXT NOT NULL, units INTEGER NOT NULL CHECK(units > 0)
            );
            CREATE TABLE IF NOT EXISTS saved_plans (
              saved_plan_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
              plan_id TEXT NOT NULL REFERENCES plans(plan_id), quote_id TEXT NOT NULL REFERENCES quotes(quote_id),
              artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
              incurred_tool_units INTEGER NOT NULL, campaign_units INTEGER NOT NULL,
              total_units INTEGER NOT NULL, evaluated_at TEXT NOT NULL,
              completion_estimate_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              revision INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
              at TEXT NOT NULL, UNIQUE(task_id, revision)
            );
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              thread_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
              ended_at TEXT, active_seconds REAL NOT NULL DEFAULT 0,
              reserved_active_seconds REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS decisions (
              decision_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
              run_id TEXT NOT NULL REFERENCES runs(run_id), seq INTEGER NOT NULL,
              context_hash TEXT NOT NULL, context_json TEXT NOT NULL,
              response_json TEXT NOT NULL, result_json TEXT,
              model_metadata_json TEXT,
              applied INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
              UNIQUE(task_id, seq)
            );
            CREATE TABLE IF NOT EXISTS model_requests (
              model_request_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(task_id),
              run_id TEXT NOT NULL REFERENCES runs(run_id),
              seq INTEGER NOT NULL,
              context_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              decision_id TEXT REFERENCES decisions(decision_id),
              created_at TEXT NOT NULL, finished_at TEXT
            );
        """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)")}
        if "tool_dispatch_count" not in columns:
            self.connection.execute("ALTER TABLE tasks ADD COLUMN tool_dispatch_count INTEGER NOT NULL DEFAULT 0")
        run_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(runs)")}
        if "reserved_active_seconds" not in run_columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN reserved_active_seconds REAL NOT NULL DEFAULT 0")

    def _event(self, task_id: str, kind: str, payload: dict[str, Any], at: datetime) -> int:
        self.connection.execute("UPDATE tasks SET revision=revision+1 WHERE task_id=?", (task_id,))
        revision = self.connection.execute("SELECT revision FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
        cursor = self.connection.execute(
            "INSERT INTO events(task_id,revision,kind,payload_json,at) VALUES(?,?,?,?,?)",
            (task_id, revision, kind, as_json(payload), at.isoformat()),
        )
        return int(cursor.lastrowid)

    def submit(self, raw: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        preliminary = parse_submission(raw, now)
        task = preliminary.task_input
        if task is not None and task.request_id:
            existing = self.connection.execute("SELECT * FROM tasks WHERE request_id=?", (task.request_id,)).fetchone()
            if existing:
                if existing["input_hash"] != task.input_hash:
                    raise BusinessError("REQUEST_CONFLICT")
                return {"status": existing["status"], "task_id": existing["task_id"], "request_id": task.request_id, "replayed": True}
        if preliminary.status != "ready":
            return preliminary.model_dump(mode="json", exclude={"task_input"})
        assert task is not None
        topic_id = resolve_topic(task.topic)
        if topic_id is None:
            return {"status": "needs_input", "missing_fields": ["supported_topic"], "reason_code": "UNKNOWN_TOPIC"}
        request_id = task.request_id or str(uuid.uuid4())
        with self.transaction():
            existing = self.connection.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["input_hash"] != task.input_hash:
                    raise BusinessError("REQUEST_CONFLICT")
                return {"status": existing["status"], "task_id": existing["task_id"], "request_id": request_id, "replayed": True}
            task_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO tasks(task_id,request_id,input_hash,input_json,topic_id,status,catalog_version,fee_policy_version,pricing_rules_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, request_id, task.input_hash, task.model_dump_json(), topic_id, "running", CATALOG_VERSION, FEE_POLICY_VERSION, self.new_task_pricing_version, now.isoformat()),
            )
            self._event(task_id, "task_created", {"request_id": request_id, "input_hash": task.input_hash}, now)
        return {"status": "running", "task_id": task_id, "request_id": request_id, "replayed": False}

    def task_row(self, task_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise BusinessError("TASK_NOT_FOUND")
        if row["catalog_version"] != CATALOG_VERSION or row["fee_policy_version"] != FEE_POLICY_VERSION or row["pricing_rules_version"] not in PRICING_RULESETS:
            raise BusinessError("RULESET_UNAVAILABLE")
        return row

    def task_input(self, task_id: str) -> TaskInput:
        return TaskInput.model_validate_json(self.task_row(task_id)["input_json"])

    def incurred_units(self, task_id: str) -> int:
        return int(self.connection.execute("SELECT COALESCE(SUM(units),0) FROM fee_events WHERE task_id=?", (task_id,)).fetchone()[0])

    def create_plan(self, task_id: str, spec: PlanSpec, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        with self.transaction():
            row = self.task_row(task_id)
            if row["status"] == "completed":
                raise BusinessError("ALREADY_COMPLETED")
            task = self.task_input(task_id)
            if spec.topic_id != row["topic_id"] or spec.selected_format not in task.allowed_formats or spec.required_points != task.required_points:
                raise BusinessError("HARD_CONSTRAINT")
            observed_materials: dict[str, str] = {}
            for operation in self.connection.execute("SELECT result_json FROM operations WHERE task_id=? AND tool_name=? AND status='finished'", (task_id, ToolName.SEARCH.value)):
                result = json.loads(operation["result_json"])
                for item in result.get("data", {}).get("materials", []):
                    observed_materials[item["material_id"]] = item["content_hash"]
            for material_id, content_hash in zip(spec.material_ids, spec.material_hashes):
                material = BY_ID.get(material_id)
                if material is None or material.topic_id != spec.topic_id or material.content_hash != content_hash or spec.selected_format not in material.formats:
                    raise BusinessError("INVALID_MATERIAL")
                if observed_materials.get(material_id) != content_hash:
                    raise BusinessError("MATERIAL_NOT_OBSERVED")
            existing = self.connection.execute("SELECT plan_id FROM plans WHERE task_id=? AND spec_hash=?", (task_id, spec.spec_hash)).fetchone()
            if existing:
                plan_id = existing["plan_id"]
                created = False
            else:
                plan_id = str(uuid.uuid4())
                revision = self.connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM plans WHERE task_id=?", (task_id,)).fetchone()[0]
                self.connection.execute(
                    "INSERT INTO plans(plan_id,task_id,revision,spec_hash,spec_json,parent_plan_id) VALUES(?,?,?,?,?,?)",
                    (plan_id, task_id, revision, spec.spec_hash, spec.model_dump_json(), row["current_plan_id"]),
                )
                created = True
            if row["current_plan_id"] != plan_id:
                self.connection.execute("UPDATE tasks SET current_plan_id=? WHERE task_id=?", (plan_id, task_id))
                self._event(task_id, "plan_selected", {"plan_id": plan_id, "spec_hash": spec.spec_hash, "created": created}, now)
        return {"plan_id": plan_id, "spec_hash": spec.spec_hash, "created": created}

    def plan(self, task_id: str, plan_id: str) -> PlanSpec:
        row = self.connection.execute("SELECT spec_json FROM plans WHERE task_id=? AND plan_id=?", (task_id, plan_id)).fetchone()
        if row is None:
            raise BusinessError("PLAN_NOT_FOUND")
        return PlanSpec.model_validate_json(row["spec_json"])

    def _operation_key(self, task_id: str, tool: ToolName, args: dict[str, Any]) -> str:
        return fingerprint({"task_id": task_id, "tool": tool.value, "args": args, "catalog_version": CATALOG_VERSION, "fee_policy_version": FEE_POLICY_VERSION, "pricing_rules_version": self.task_row(task_id)["pricing_rules_version"]})

    def _tool(self, task_id: str, tool: ToolName, args: dict[str, Any], compute: Callable[[], tuple[ToolOutcome, dict[str, Any], ScriptArtifact | Quote | None]], now: datetime) -> dict[str, Any]:
        key = self._operation_key(task_id, tool, args)
        with self.transaction():
            task = self.task_row(task_id)
            existing = self.connection.execute("SELECT * FROM operations WHERE operation_key=?", (key,)).fetchone()
            if existing and existing["status"] == "finished":
                result = json.loads(existing["result_json"])
                result["replayed"] = True
                return result
            if task["status"] == "completed":
                raise BusinessError("ALREADY_COMPLETED")
            operation_count = self.connection.execute("SELECT COUNT(*) FROM operations WHERE task_id=?", (task_id,)).fetchone()[0]
            attempt_count = self.connection.execute("SELECT COUNT(*) FROM attempts WHERE operation_key IN (SELECT operation_key FROM operations WHERE task_id=?)", (task_id,)).fetchone()[0]
            if existing is None and operation_count >= 24:
                raise BusinessError("TOOL_DISPATCH_LIMIT")
            if attempt_count >= 18:
                raise BusinessError("TOOL_ATTEMPT_LIMIT")
            if not can_dispatch_tool(self.task_input(task_id).budget_units, self.incurred_units(task_id), 0, tool):
                raise BusinessError("TOOL_FEE_BUDGET_EXCEEDED")
            if existing is None:
                self.connection.execute(
                    "INSERT INTO operations(operation_key,task_id,tool_name,args_json,status,created_at) VALUES(?,?,?,?,?,?)",
                    (key, task_id, tool.value, as_json(args), "pending", now.isoformat()),
                )
                self._event(task_id, "operation_started", {"operation_key": key, "tool": tool.value}, now)
            else:
                self.connection.execute("UPDATE attempts SET outcome='aborted',error_code='PROCESS_INTERRUPTED' WHERE operation_key=? AND outcome='started'", (key,))
        attempt_id = ""
        try:
            for retry in Retrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=0.01, min=0.01, max=0.05),
                retry=retry_if_exception_type(TransientToolError),
                reraise=True,
            ):
                with retry:
                    with self.transaction():
                        attempt_count = self.connection.execute(
                            "SELECT COUNT(*) FROM attempts WHERE operation_key IN (SELECT operation_key FROM operations WHERE task_id=?)",
                            (task_id,),
                        ).fetchone()[0]
                        if attempt_count >= 18:
                            raise BusinessError("TOOL_ATTEMPT_LIMIT")
                        attempt_id = str(uuid.uuid4())
                        self.connection.execute(
                            "INSERT INTO attempts(attempt_id,operation_key,outcome,error_code,at) VALUES(?,?,?,?,?)",
                            (attempt_id, key, "started", None, utcnow().isoformat()),
                        )
                    try:
                        outcome, data, artifact_or_quote = compute()
                        error_code = None
                    except TransientToolError as exc:
                        with self.transaction():
                            self.connection.execute("UPDATE attempts SET outcome='failed',error_code=? WHERE attempt_id=?", (exc.code, attempt_id))
                            self._event(task_id, "tool_attempt_failed", {"operation_key": key, "attempt_id": attempt_id, "error_code": exc.code}, utcnow())
                        raise
                    except BusinessError as exc:
                        outcome, data, artifact_or_quote = ToolOutcome.FAILED, {"error_code": exc.code}, None
                        error_code = exc.code
                if not retry.retry_state.outcome.failed:
                    break
        except TransientToolError as exc:
            outcome, data, artifact_or_quote = ToolOutcome.FAILED, {"error_code": "TRANSIENT_EXHAUSTED", "last_error_code": exc.code}, None
            error_code = "TRANSIENT_EXHAUSTED"
        with self.transaction():
            existing = self.connection.execute("SELECT * FROM operations WHERE operation_key=?", (key,)).fetchone()
            if existing["status"] == "finished":
                result = json.loads(existing["result_json"])
                result["replayed"] = True
                return result
            charge = tool_fee(tool, outcome)
            if charge and not can_dispatch_tool(self.task_input(task_id).budget_units, self.incurred_units(task_id), 0, tool):
                raise BusinessError("TOOL_FEE_BUDGET_EXCEEDED")
            result = {"operation_key": key, "tool": tool.value, "outcome": outcome.value, "data": data, "fee_units": charge, "replayed": False}
            if isinstance(artifact_or_quote, ScriptArtifact):
                self.connection.execute("INSERT INTO artifacts(artifact_id,task_id,operation_key,artifact_json) VALUES(?,?,?,?)", (artifact_or_quote.artifact_id, task_id, key, artifact_or_quote.model_dump_json()))
            if isinstance(artifact_or_quote, Quote):
                self.connection.execute("INSERT INTO quotes(quote_id,task_id,operation_key,quote_json) VALUES(?,?,?,?)", (artifact_or_quote.quote_id, task_id, key, artifact_or_quote.model_dump_json()))
            self.connection.execute("UPDATE attempts SET outcome=?,error_code=? WHERE attempt_id=?", (outcome.value, error_code, attempt_id))
            if charge:
                self.connection.execute("INSERT INTO fee_events(charge_id,task_id,operation_key,tool_name,outcome,fee_policy_version,units) VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), task_id, key, tool.value, outcome.value, FEE_POLICY_VERSION, charge))
            self.connection.execute("UPDATE operations SET status='finished',outcome=?,result_json=?,finished_at=? WHERE operation_key=?", (outcome.value, as_json(result), now.isoformat(), key))
            self._event(task_id, "operation_finished", result, now)
        return result

    def search_materials(self, task_id: str, query: str = "", selected_format: ContentFormat | None = None, now: datetime | None = None) -> dict[str, Any]:
        row = self.task_row(task_id)
        if selected_format is not None and selected_format not in self.task_input(task_id).allowed_formats:
            raise BusinessError("HARD_CONSTRAINT")
        args = {"topic_id": row["topic_id"], "query": query.strip(), "selected_format": selected_format.value if selected_format else None}

        def compute():
            items = search(row["topic_id"], query, selected_format)
            return ToolOutcome.SUCCESS, {"materials": [{"material_id": m.material_id, "title": m.title, "content_hash": m.content_hash} for m in items], "catalog_version": CATALOG_VERSION, "hint": "query 可留空以检索该主题的全部素材" if not items else None}, None

        return self._tool(task_id, ToolName.SEARCH, args, compute, now or utcnow())

    def artifact(self, task_id: str, artifact_id: str) -> ScriptArtifact:
        row = self.connection.execute("SELECT artifact_json FROM artifacts WHERE task_id=? AND artifact_id=?", (task_id, artifact_id)).fetchone()
        if row is None:
            raise BusinessError("ARTIFACT_NOT_FOUND")
        artifact = ScriptArtifact.model_validate_json(row["artifact_json"])
        if not artifact.hash_valid:
            raise BusinessError("ARTIFACT_CORRUPT")
        return artifact

    def generate_script(self, task_id: str, plan_id: str, mode: str = "full", partial_artifact_id: str | None = None, now: datetime | None = None, failure: str | None = None) -> dict[str, Any]:
        plan = self.plan(task_id, plan_id)
        task = self.task_input(task_id)
        if mode not in ("full", "partial", "complete_partial"):
            raise BusinessError("INVALID_ARGUMENT")
        if mode == "complete_partial":
            if partial_artifact_id is None:
                raise BusinessError("INVALID_ARGUMENT")
            partial = self.artifact(task_id, partial_artifact_id)
            if partial.status != ArtifactStatus.PARTIAL or partial.dependency_fingerprint != plan.generation_fingerprint:
                raise BusinessError("INVALID_PARTIAL")
        elif partial_artifact_id is not None:
            raise BusinessError("INVALID_ARGUMENT")
        args = {"generation_fingerprint": plan.generation_fingerprint, "mode": mode, "partial_artifact_id": partial_artifact_id}

        def compute():
            if failure == "permanent":
                raise BusinessError("SCRIPT_GENERATION_FAILED")
            if failure is not None:
                raise BusinessError("INVALID_FAILURE_INJECTION")
            materials = [BY_ID[material_id] for material_id in plan.material_ids]
            if any(material.content_hash != content_hash for material, content_hash in zip(materials, plan.material_hashes)):
                raise BusinessError("MATERIAL_STALE")
            points = "；".join(task.required_points)
            body = ScriptBody(
                title=f"{task.topic} · {plan.selected_format.value}",
                opening=f"今天一起了解{task.topic}。",
                body="" if mode == "partial" else f"{materials[0].summary} {points}。",
                call_to_action=f"欢迎参与{task.topic}。",
            )
            status = ArtifactStatus.PARTIAL if mode == "partial" else ArtifactStatus.COMPLETE
            artifact = ScriptArtifact(artifact_id=str(uuid.uuid4()), task_id=task_id, dependency_fingerprint=plan.generation_fingerprint, status=status, content=body, content_hash=fingerprint(body))
            return ToolOutcome.PARTIAL if mode == "partial" else ToolOutcome.SUCCESS, {"artifact_id": artifact.artifact_id, "status": status.value, "content_hash": artifact.content_hash}, artifact

        return self._tool(task_id, ToolName.GENERATE, args, compute, now or utcnow())

    def estimate_cost(self, task_id: str, plan_id: str, artifact_id: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        plan = self.plan(task_id, plan_id)
        artifact = self.artifact(task_id, artifact_id)
        if not script_eligible(artifact, plan, self.task_input(task_id)):
            raise BusinessError("SCRIPT_INELIGIBLE")
        args = {"spec_hash": plan.spec_hash, "artifact_id": artifact_id, "script_hash": artifact.content_hash}

        def compute():
            rules_version = self.task_row(task_id)["pricing_rules_version"]
            price = price_for(plan.selected_format, rules_version)
            quote = Quote(quote_id=str(uuid.uuid4()), task_id=task_id, plan_id=plan_id, spec_hash=plan.spec_hash, script_hash=artifact.content_hash, pricing_rules_version=rules_version, base_units=price.base_units * plan.quantity, license_units=price.license_units * plan.quantity, production_minutes=price.production_minutes * plan.quantity)
            feasibility = evaluate_feasibility(self.task_input(task_id), quote, self.incurred_units(task_id), now)
            return ToolOutcome.SUCCESS, {"quote_id": quote.quote_id, "feasibility": feasibility.model_dump(mode="json")}, quote

        result = self._tool(task_id, ToolName.ESTIMATE, args, compute, now)
        quote_id = result["data"].get("quote_id")
        if quote_id:
            quote = self.quote(task_id, quote_id)
            result["current_feasibility"] = evaluate_feasibility(self.task_input(task_id), quote, self.incurred_units(task_id), now).model_dump(mode="json")
        return result

    def quote(self, task_id: str, quote_id: str) -> Quote:
        row = self.connection.execute("SELECT quote_json FROM quotes WHERE task_id=? AND quote_id=?", (task_id, quote_id)).fetchone()
        if row is None:
            raise BusinessError("QUOTE_NOT_FOUND")
        return Quote.model_validate_json(row["quote_json"])

    def save_plan(self, task_id: str, plan_id: str, artifact_id: str, quote_id: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        with self.transaction():
            task_row = self.task_row(task_id)
            existing = self.connection.execute("SELECT * FROM saved_plans WHERE task_id=?", (task_id,)).fetchone()
            if existing:
                if (existing["plan_id"], existing["artifact_id"], existing["quote_id"]) != (plan_id, artifact_id, quote_id):
                    raise BusinessError("ALREADY_COMPLETED")
                return {"status": "completed", "saved_plan_id": existing["saved_plan_id"], "replayed": True, "total_units": existing["total_units"]}
            if task_row["current_plan_id"] != plan_id:
                raise BusinessError("PLAN_NOT_CURRENT")
            plan = self.plan(task_id, plan_id)
            task = self.task_input(task_id)
            artifact = self.artifact(task_id, artifact_id)
            quote = self.quote(task_id, quote_id)
            if quote.plan_id != plan_id or not script_eligible(artifact, plan, task) or not quote_matches(quote, plan, artifact):
                raise BusinessError("STALE_REFERENCE")
            feasibility = evaluate_feasibility(task, quote, self.incurred_units(task_id), now)
            if not feasibility.budget_ok:
                raise BusinessError("BUDGET_EXCEEDED")
            if not feasibility.deadline_ok:
                raise BusinessError("DEADLINE_EXCEEDED")
            saved_plan_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO saved_plans(saved_plan_id,task_id,plan_id,quote_id,artifact_id,incurred_tool_units,campaign_units,total_units,evaluated_at,completion_estimate_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (saved_plan_id, task_id, plan_id, quote_id, artifact_id, feasibility.incurred_tool_units, feasibility.campaign_units, feasibility.total_units, feasibility.evaluated_at.isoformat(), feasibility.completion_estimate_at.isoformat()),
            )
            self.connection.execute("UPDATE tasks SET status='completed',saved_plan_id=? WHERE task_id=?", (saved_plan_id, task_id))
            self._event(task_id, "plan_saved", {"saved_plan_id": saved_plan_id, "plan_id": plan_id, "total_units": feasibility.total_units}, now)
        return {"status": "completed", "saved_plan_id": saved_plan_id, "replayed": False, "total_units": feasibility.total_units}

    def events(self, task_id: str) -> list[dict[str, Any]]:
        self.task_row(task_id)
        return [dict(row) for row in self.connection.execute("SELECT * FROM events WHERE task_id=? ORDER BY revision", (task_id,))]

    @staticmethod
    def _result_status(result: dict[str, Any]) -> str:
        if result.get("status") == "completed" or result.get("outcome") == ToolOutcome.SUCCESS.value:
            return "succeeded"
        if result.get("outcome") == ToolOutcome.PARTIAL.value:
            return "partial"
        if result.get("status") == "error" or result.get("outcome") == ToolOutcome.FAILED.value:
            return "failed"
        return "in_progress"

    def _tool_result_views(self, task_id: str) -> list[dict[str, Any]]:
        """Return every persisted tool result, including failures and save decisions."""
        decisions_by_operation: dict[str, list[dict[str, Any]]] = {}
        save_decisions: list[dict[str, Any]] = []
        for row in self.connection.execute(
            "SELECT decision_id,seq,response_json,result_json,created_at FROM decisions WHERE task_id=? AND applied=1 ORDER BY seq",
            (task_id,),
        ):
            response = json.loads(row["response_json"])
            result = json.loads(row["result_json"])
            decision = {
                "decision_id": row["decision_id"],
                "sequence": row["seq"],
                "reason_summary": response.get("reason_summary"),
                "observed_event_ids": response.get("observed_event_ids", []),
            }
            operation_key = result.get("operation_key")
            if operation_key:
                decisions_by_operation.setdefault(operation_key, []).append(decision)
            if response.get("action") == "save_plan":
                save_decisions.append({
                    "result_id": f'decision:{row["decision_id"]}',
                    "tool": ToolName.SAVE.value,
                    "arguments": response.get("arguments", {}),
                    "status": self._result_status(result),
                    "outcome": "success" if result.get("status") == "completed" else "failed",
                    "result": result,
                    "fee_units": 0,
                    "attempts": [],
                    "model_decisions": [decision],
                    "created_at": row["created_at"],
                    "finished_at": row["created_at"],
                })

        views: list[dict[str, Any]] = []
        for row in self.connection.execute("SELECT rowid,* FROM operations WHERE task_id=? ORDER BY rowid", (task_id,)):
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            attempts = [
                {
                    "attempt_id": attempt["attempt_id"],
                    "outcome": attempt["outcome"],
                    "error_code": attempt["error_code"],
                    "at": attempt["at"],
                }
                for attempt in self.connection.execute(
                    "SELECT attempt_id,outcome,error_code,at FROM attempts WHERE operation_key=? ORDER BY rowid",
                    (row["operation_key"],),
                )
            ]
            views.append({
                "result_id": row["operation_key"],
                "tool": row["tool_name"],
                "arguments": json.loads(row["args_json"]),
                "status": self._result_status(result) if row["status"] == "finished" else "in_progress",
                "outcome": row["outcome"],
                "result": result,
                "fee_units": result.get("fee_units", 0),
                "attempts": attempts,
                "model_decisions": decisions_by_operation.get(row["operation_key"], []),
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            })

        if save_decisions:
            views.extend(save_decisions)
        else:
            saved = self.connection.execute("SELECT * FROM saved_plans WHERE task_id=?", (task_id,)).fetchone()
            if saved:
                views.append({
                    "result_id": f'saved-plan:{saved["saved_plan_id"]}',
                    "tool": ToolName.SAVE.value,
                    "arguments": {"plan_id": saved["plan_id"], "artifact_id": saved["artifact_id"], "quote_id": saved["quote_id"]},
                    "status": "succeeded",
                    "outcome": "success",
                    "result": {"status": "completed", "saved_plan_id": saved["saved_plan_id"], "total_units": saved["total_units"]},
                    "fee_units": 0,
                    "attempts": [],
                    "model_decisions": [],
                    "created_at": saved["evaluated_at"],
                    "finished_at": saved["evaluated_at"],
                })
        return views

    def _latest_incomplete_reason(self, task_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT payload_json FROM events WHERE task_id=? AND kind='run_finished' ORDER BY revision DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row:
            reason_code = json.loads(row["payload_json"]).get("reason_code")
            if reason_code:
                return reason_code
        row = self.connection.execute(
            "SELECT result_json FROM decisions WHERE task_id=? AND applied=1 ORDER BY seq DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row and row["result_json"]:
            return json.loads(row["result_json"]).get("reason_code")
        return None

    def delivery_view(self, task_id: str, reason_code: str | None = None) -> dict[str, Any]:
        """Aggregate the authoritative task facts into the evaluator-facing deliverable."""
        task_row = self.task_row(task_id)
        task = self.task_input(task_id)
        saved = self.connection.execute("SELECT * FROM saved_plans WHERE task_id=?", (task_id,)).fetchone()
        selected_plan_id = saved["plan_id"] if saved else task_row["current_plan_id"]
        plan_row = None
        plan = None
        if selected_plan_id:
            plan_row = self.connection.execute("SELECT * FROM plans WHERE task_id=? AND plan_id=?", (task_id, selected_plan_id)).fetchone()
            if plan_row:
                plan = PlanSpec.model_validate_json(plan_row["spec_json"])

        artifact = None
        if saved:
            artifact = self.artifact(task_id, saved["artifact_id"])
        elif plan is not None:
            for row in self.connection.execute("SELECT artifact_json FROM artifacts WHERE task_id=? ORDER BY rowid DESC", (task_id,)):
                candidate = ScriptArtifact.model_validate_json(row["artifact_json"])
                if candidate.hash_valid and candidate.dependency_fingerprint == plan.generation_fingerprint:
                    artifact = candidate
                    break

        quote = None
        if saved:
            quote = self.quote(task_id, saved["quote_id"])
        elif selected_plan_id:
            for row in self.connection.execute("SELECT quote_json FROM quotes WHERE task_id=? ORDER BY rowid DESC", (task_id,)):
                candidate = Quote.model_validate_json(row["quote_json"])
                if candidate.plan_id == selected_plan_id:
                    quote = candidate
                    break

        tool_results = self._tool_result_views(task_id)
        results_by_tool: dict[str, list[dict[str, Any]]] = {}
        for item in tool_results:
            results_by_tool.setdefault(item["tool"], []).append(item)

        def tool_step_status(tool: ToolName) -> str:
            results = results_by_tool.get(tool.value, [])
            if not results:
                return "not_started"
            if any(item["status"] == "succeeded" for item in results):
                return "succeeded"
            if any(item["status"] == "partial" for item in results):
                return "partial"
            if any(item["status"] == "in_progress" for item in results):
                return "in_progress"
            return "failed"

        script_status = "not_started"
        if artifact is not None:
            script_status = "succeeded" if artifact.status == ArtifactStatus.COMPLETE else "partial"
        elif results_by_tool.get(ToolName.GENERATE.value):
            script_status = tool_step_status(ToolName.GENERATE)

        tool_units = self.incurred_units(task_id)
        campaign_base_units = quote.base_units if quote else None
        campaign_license_units = quote.license_units if quote else None
        campaign_units = quote.campaign_units if quote else None
        total_units = saved["total_units"] if saved else tool_units + (campaign_units or 0)
        saved_plan = None if saved is None else {
            "saved_plan_id": saved["saved_plan_id"],
            "plan_id": saved["plan_id"],
            "artifact_id": saved["artifact_id"],
            "quote_id": saved["quote_id"],
            "evaluated_at": saved["evaluated_at"],
            "completion_estimate_at": saved["completion_estimate_at"],
        }
        plan_view = None if plan is None else {
            "plan_id": selected_plan_id,
            "revision": plan_row["revision"],
            "parent_plan_id": plan_row["parent_plan_id"],
            "specification": plan.model_dump(mode="json"),
            "script": None if artifact is None else {
                "artifact_id": artifact.artifact_id,
                "status": artifact.status.value,
                "content_hash": artifact.content_hash,
                "content": artifact.content.model_dump(mode="json"),
            },
            "quote": None if quote is None else {
                "quote_id": quote.quote_id,
                "pricing_rules_version": quote.pricing_rules_version,
                "production_minutes": quote.production_minutes,
            },
        }
        steps = [
            {"step": "search_materials", "status": tool_step_status(ToolName.SEARCH), "result_ids": [item["result_id"] for item in results_by_tool.get(ToolName.SEARCH.value, [])]},
            {"step": "plan_selection", "status": "succeeded" if plan else "not_started", "result_ids": [selected_plan_id] if selected_plan_id else []},
            {"step": "generate_script", "status": script_status, "result_ids": [item["result_id"] for item in results_by_tool.get(ToolName.GENERATE.value, [])]},
            {"step": "estimate_cost", "status": "succeeded" if quote else tool_step_status(ToolName.ESTIMATE), "result_ids": [item["result_id"] for item in results_by_tool.get(ToolName.ESTIMATE.value, [])]},
            {"step": "save_plan", "status": "succeeded" if saved else tool_step_status(ToolName.SAVE), "result_ids": [item["result_id"] for item in results_by_tool.get(ToolName.SAVE.value, [])]},
        ]
        return {
            "task_requirements": task.model_dump(mode="json"),
            "task_plan": plan_view,
            "steps": steps,
            "tool_results": tool_results,
            "cost_summary": {
                "budget_units": task.budget_units,
                "incurred_tool_units": tool_units,
                "campaign_base_units": campaign_base_units,
                "campaign_license_units": campaign_license_units,
                "campaign_units": campaign_units,
                "total_units": total_units,
                "remaining_budget_units": task.budget_units - total_units,
            },
            "saved_plan": saved_plan,
            "incomplete_reason": None if saved else reason_code or self._latest_incomplete_reason(task_id),
        }

    def result_view(self, task_id: str, reason_code: str | None = None) -> dict[str, Any]:
        return {**self.snapshot(task_id), "delivery": self.delivery_view(task_id, reason_code)}

    def snapshot(self, task_id: str) -> dict[str, Any]:
        row = self.task_row(task_id)
        return {"task_id": task_id, "request_id": row["request_id"], "status": row["status"], "revision": row["revision"], "current_plan_id": row["current_plan_id"], "saved_plan_id": row["saved_plan_id"], "incurred_tool_units": self.incurred_units(task_id), "tool_dispatch_count": row["tool_dispatch_count"]}

    def reserve_tool_dispatch(self, task_id: str, run_id: str, limit: int = 24, now: datetime | None = None) -> int:
        now = now or utcnow()
        with self.transaction():
            row = self.task_row(task_id)
            run = self.connection.execute("SELECT 1 FROM runs WHERE run_id=? AND task_id=? AND status='running'", (run_id, task_id)).fetchone()
            if run is None:
                raise BusinessError("RUN_NOT_FOUND")
            if row["tool_dispatch_count"] >= limit:
                raise BusinessError("TOOL_DISPATCH_LIMIT")
            count = row["tool_dispatch_count"] + 1
            self.connection.execute("UPDATE tasks SET tool_dispatch_count=? WHERE task_id=?", (count, task_id))
            self._event(task_id, "tool_dispatched", {"run_id": run_id, "count": count}, now)
        return count

    def begin_run(self, task_id: str, now: datetime | None = None) -> dict[str, str]:
        now = now or utcnow()
        with self.transaction():
            row = self.task_row(task_id)
            if row["status"] == "completed":
                raise BusinessError("ALREADY_COMPLETED")
            run_id = str(uuid.uuid4())
            thread_id = str(uuid.uuid4())
            self.connection.execute("INSERT INTO runs(run_id,task_id,thread_id,status,started_at) VALUES(?,?,?,?,?)", (run_id, task_id, thread_id, "running", now.isoformat()))
            self._event(task_id, "run_started", {"run_id": run_id, "thread_id": thread_id}, now)
        return {"run_id": run_id, "thread_id": thread_id}

    def running_run(self, task_id: str) -> dict[str, Any]:
        self.task_row(task_id)
        row = self.connection.execute("SELECT * FROM runs WHERE task_id=? AND status='running' ORDER BY started_at DESC LIMIT 1", (task_id,)).fetchone()
        if row is None:
            raise BusinessError("RUN_NOT_FOUND")
        return dict(row)

    def abandon_run_for_rebuild(self, task_id: str, now: datetime | None = None) -> None:
        now = now or utcnow()
        with self.transaction():
            row = self.running_run(task_id)
            self.connection.execute("UPDATE runs SET status='abandoned',ended_at=?,active_seconds=active_seconds+reserved_active_seconds,reserved_active_seconds=0 WHERE run_id=?", (now.isoformat(), row["run_id"]))
            self._event(task_id, "checkpoint_rebuilt", {"abandoned_run_id": row["run_id"], "old_thread_id": row["thread_id"]}, now)

    def active_seconds(self, task_id: str) -> float:
        return float(self.connection.execute("SELECT COALESCE(SUM(active_seconds+reserved_active_seconds),0) FROM runs WHERE task_id=?", (task_id,)).fetchone()[0])

    def reserve_runtime(self, task_id: str, run_id: str, seconds: float, limit: float) -> float:
        with self.transaction():
            row = self.connection.execute("SELECT * FROM runs WHERE run_id=? AND task_id=? AND status='running'", (run_id, task_id)).fetchone()
            if row is None:
                raise BusinessError("RUN_NOT_FOUND")
            remaining = limit - self.active_seconds(task_id)
            reservation = min(float(seconds), remaining)
            if reservation <= 0:
                raise BusinessError("ACTIVE_RUNTIME_LIMIT")
            self.connection.execute("UPDATE runs SET reserved_active_seconds=reserved_active_seconds+? WHERE run_id=?", (reservation, run_id))
        return reservation

    def settle_runtime(self, run_id: str, reserved_seconds: float, elapsed_seconds: float) -> None:
        with self.transaction():
            row = self.connection.execute("SELECT reserved_active_seconds FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["reserved_active_seconds"] + 0.000001 < reserved_seconds:
                raise BusinessError("STATE_INCONSISTENT")
            self.connection.execute("UPDATE runs SET reserved_active_seconds=MAX(0,reserved_active_seconds-?),active_seconds=active_seconds+? WHERE run_id=?", (reserved_seconds, max(0.0, elapsed_seconds), run_id))

    def consume_reserved_runtime(self, run_id: str) -> float:
        with self.transaction():
            row = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise BusinessError("RUN_NOT_FOUND")
            reserved = float(row["reserved_active_seconds"])
            if reserved:
                self.connection.execute("UPDATE runs SET active_seconds=active_seconds+reserved_active_seconds,reserved_active_seconds=0 WHERE run_id=?", (run_id,))
                self._event(row["task_id"], "runtime_reservation_consumed", {"run_id": run_id, "seconds": reserved}, utcnow())
        return reserved

    def decision_count(self, task_id: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM decisions WHERE task_id=?", (task_id,)).fetchone()[0])

    def pending_decision(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM decisions WHERE run_id=? AND applied=0 ORDER BY seq LIMIT 1", (run_id,)).fetchone()
        return dict(row) if row else None

    def begin_model_request(self, task_id: str, run_id: str, context: dict[str, Any], now: datetime | None = None) -> str:
        now = now or utcnow()
        with self.transaction():
            if self.pending_decision(run_id):
                raise BusinessError("PENDING_DECISION")
            pending = self.connection.execute("SELECT model_request_id FROM model_requests WHERE run_id=? AND status='pending'", (run_id,)).fetchall()
            for row in pending:
                self.connection.execute("UPDATE model_requests SET status='outcome_unknown',finished_at=? WHERE model_request_id=?", (now.isoformat(), row["model_request_id"]))
                self._event(task_id, "model_outcome_unknown", {"model_request_id": row["model_request_id"], "supplier_cost": "not_measured"}, now)
            attempts = self.connection.execute("SELECT COUNT(*) FROM model_requests WHERE task_id=?", (task_id,)).fetchone()[0]
            if attempts >= 18:
                raise BusinessError("MODEL_TRANSFER_LIMIT")
            request_id = str(uuid.uuid4())
            seq = self.decision_count(task_id) + 1
            self.connection.execute(
                "INSERT INTO model_requests(model_request_id,task_id,run_id,seq,context_hash,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (request_id, task_id, run_id, seq, fingerprint(context), "pending", now.isoformat()),
            )
            self._event(task_id, "model_request_started", {"model_request_id": request_id, "seq": seq, "context_hash": fingerprint(context)}, now)
        return request_id

    def mark_model_request_unknown(self, model_request_id: str, now: datetime | None = None) -> None:
        now = now or utcnow()
        with self.transaction():
            row = self.connection.execute("SELECT * FROM model_requests WHERE model_request_id=?", (model_request_id,)).fetchone()
            if row is None:
                raise BusinessError("MODEL_REQUEST_NOT_FOUND")
            if row["status"] != "pending":
                return
            self.connection.execute("UPDATE model_requests SET status='outcome_unknown',finished_at=? WHERE model_request_id=?", (now.isoformat(), model_request_id))
            self._event(row["task_id"], "model_outcome_unknown", {"model_request_id": model_request_id, "supplier_cost": "not_measured"}, now)

    def record_decision(self, task_id: str, run_id: str, context: dict[str, Any], response: dict[str, Any], now: datetime | None = None, metadata: dict[str, Any] | None = None, model_request_id: str | None = None) -> dict[str, Any]:
        now = now or utcnow()
        with self.transaction():
            if self.pending_decision(run_id):
                raise BusinessError("PENDING_DECISION")
            seq = self.decision_count(task_id) + 1
            if seq > 12:
                raise BusinessError("DECISION_LIMIT")
            decision_id = str(uuid.uuid4())
            context_hash = fingerprint(context)
            self.connection.execute("INSERT INTO decisions(decision_id,task_id,run_id,seq,context_hash,context_json,response_json,model_metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (decision_id, task_id, run_id, seq, context_hash, as_json(context), as_json(response), as_json(metadata) if metadata else None, now.isoformat()))
            if model_request_id:
                updated = self.connection.execute("UPDATE model_requests SET status='finished',decision_id=?,finished_at=? WHERE model_request_id=? AND task_id=? AND run_id=? AND status='pending' AND context_hash=?", (decision_id, now.isoformat(), model_request_id, task_id, run_id, context_hash))
                if updated.rowcount != 1:
                    raise BusinessError("STATE_INCONSISTENT")
            self._event(task_id, "model_decision", {"decision_id": decision_id, "seq": seq, "context_hash": context_hash, "response": response, "model_metadata": metadata}, now)
        return {"decision_id": decision_id, "seq": seq}

    def mark_decision_applied(self, decision_id: str, result: dict[str, Any], now: datetime | None = None) -> None:
        now = now or utcnow()
        with self.transaction():
            row = self.connection.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
            if row is None:
                raise BusinessError("DECISION_NOT_FOUND")
            if row["applied"]:
                return
            self.connection.execute("UPDATE decisions SET applied=1,result_json=? WHERE decision_id=?", (as_json(result), decision_id))
            self._event(row["task_id"], "decision_applied", {"decision_id": decision_id, "result": result}, now)

    def previous_decisions(self, task_id: str, limit: int = 2) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM decisions WHERE task_id=? AND applied=1 ORDER BY seq DESC LIMIT ?", (task_id, limit))]

    def progress_hash(self, task_id: str) -> str:
        """Ignore plan selection and trace events; include new business observations."""
        counts = {}
        for table in ("operations", "artifacts", "quotes", "fee_events"):
            counts[table] = self.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE task_id=?", (task_id,)).fetchone()[0]
        return fingerprint(counts)

    def finish_run(self, run_id: str, status: str, now: datetime | None = None, active_seconds: float = 0.0, reason_code: str | None = None) -> None:
        now = now or utcnow()
        with self.transaction():
            row = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise BusinessError("RUN_NOT_FOUND")
            self.connection.execute("UPDATE runs SET status=?,ended_at=?,active_seconds=MAX(active_seconds,?)+reserved_active_seconds,reserved_active_seconds=0 WHERE run_id=?", (status, now.isoformat(), max(0.0, active_seconds), run_id))
            if status == "incomplete" and self.task_row(row["task_id"])["status"] != "completed":
                self.connection.execute("UPDATE tasks SET status='incomplete' WHERE task_id=?", (row["task_id"],))
            self._event(row["task_id"], "run_finished", {"run_id": run_id, "status": status, "reason_code": reason_code}, now)
