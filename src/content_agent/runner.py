"""Model-selected actions routed through a small LangGraph state machine."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict

from pydantic import Field, ValidationError

from .canonical import fingerprint
from .catalog import BY_ID
from .repository import BusinessError, Repository, as_json, utcnow
from .rules import evaluate_feasibility
from .schemas import ContentFormat, PlanSpec, StrictModel


class Action(StrictModel):
    action: Literal["create_plan", "revise_plan", "select_plan", "search_materials", "generate_script", "estimate_cost", "save_plan", "stop"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason_summary: str = Field(min_length=1, max_length=500)
    observed_event_ids: tuple[int, ...] = ()


class ActionModel(Protocol):
    async def decide(self, context: dict[str, Any], seq: int) -> dict[str, Any]: ...


def preflight_model_budget(system_prompt: str, context: dict[str, Any], max_output_tokens: int, context_limit: int) -> int:
    """Conservative token upper bound: UTF-8 bytes for input plus output quota."""
    upper_bound = len(system_prompt.encode("utf-8")) + len(json.dumps(context, ensure_ascii=False).encode("utf-8")) + max_output_tokens + 256
    if upper_bound > context_limit:
        raise BusinessError("CONTEXT_LIMIT")
    return upper_bound


class ScriptedModel:
    """Deterministic protocol substitute; it does not prove model adaptation."""

    def __init__(self, actions: list[dict[str, Any]]):
        self.actions = actions

    async def decide(self, context: dict[str, Any], seq: int) -> dict[str, Any]:
        if seq > len(self.actions):
            return {"action": "stop", "arguments": {"reason_code": "SCRIPT_EXHAUSTED"}, "reason_summary": "离线动作序列已结束", "observed_event_ids": []}
        return self.actions[seq - 1]


class DemoModel:
    """Deterministic offline workflow for integration tests and examples only."""

    async def decide(self, context: dict[str, Any], seq: int) -> dict[str, Any]:
        events = context["recent_events"]
        def response(action: str, arguments: dict[str, Any], reason: str) -> dict[str, Any]:
            return {"action": action, "arguments": arguments, "reason_summary": reason, "observed_event_ids": [event["event_id"] for event in events[-2:]]}

        task = context["task"]
        searched = context["search_results"]
        if not searched and context["current_plan"] is None:
            return response("search_materials", {"query": ""}, "先检索可用的合成素材")
        if context["current_plan"] is None:
            materials = searched[-1]["materials"] if searched else []
            if not materials:
                return response("stop", {"reason_code": "NO_MATERIAL"}, "没有可用素材")
            chosen = "video" if "video" in task["allowed_formats"] else "image_text"
            return response("create_plan", {"selected_format": chosen, "material_ids": [materials[0]["material_id"]], "generator_strategy": "rich"}, "用检索结果建立候选方案")
        plan = context["current_plan"]
        spec = plan["spec"]
        matching_artifacts = [item for item in context["artifacts"] if item["dependency_fingerprint"] == fingerprint({key: value for key, value in spec.items() if key != "script_hash"})]
        if not matching_artifacts:
            failed = any(event["kind"] == "operation_finished" and event["payload"].get("tool") == "generate_script" and event["payload"].get("outcome") == "failed" for event in events)
            if failed and spec["generator_strategy"] == "rich":
                return response("revise_plan", {"selected_format": spec["selected_format"], "material_ids": spec["material_ids"], "generator_strategy": "compact"}, "脚本策略失败，改用备用策略")
            return response("generate_script", {"plan_id": plan["plan_id"]}, "生成当前方案脚本")
        artifact = matching_artifacts[0]
        if spec["script_hash"] != artifact["content_hash"]:
            return response("revise_plan", {"selected_format": spec["selected_format"], "material_ids": spec["material_ids"], "generator_strategy": spec["generator_strategy"], "script_hash": artifact["content_hash"]}, "将完整脚本绑定到方案版本")
        matching_quotes = [item for item in context["quotes"] if item["plan_id"] == plan["plan_id"]]
        if not matching_quotes:
            return response("estimate_cost", {"plan_id": plan["plan_id"], "artifact_id": artifact["artifact_id"]}, "计算当前版本的费用和工时")
        quote = matching_quotes[0]
        total = quote["campaign_units"] + context["incurred_tool_units"]
        if total > task["budget_units"]:
            if spec["selected_format"] == "video" and "image_text" in task["allowed_formats"]:
                return response("revise_plan", {"selected_format": "image_text", "material_ids": spec["material_ids"], "generator_strategy": "rich"}, "视频总成本超预算，改图文方案")
            return response("stop", {"reason_code": "BUDGET_EXCEEDED"}, "当前允许形式无法满足预算")
        return response("save_plan", {"plan_id": plan["plan_id"], "artifact_id": artifact["artifact_id"], "quote_id": quote["quote_id"]}, "保存已校验可行的方案")


class OpenAIActionModel:
    def __init__(self, model: str | None, provider: str = "openai"):
        from dotenv import load_dotenv
        from openai import AsyncOpenAI

        load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
        self.provider = provider
        if provider == "deepseek":
            api_key = os.environ.get("DEEPSEEK_API_KEY")
            base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
            resolved_model = model or os.environ.get("DEEPSEEK_MODEL")
        elif provider == "openai":
            api_key = os.environ.get("OPENAI_API_KEY")
            base_url = os.environ.get("OPENAI_BASE_URL")
            resolved_model = model or "gpt-4.1-mini"
        else:
            raise BusinessError("MODEL_PROVIDER_UNSUPPORTED")
        if not api_key or not resolved_model:
            raise BusinessError("MODEL_UNAVAILABLE", f"{provider} API key or model is missing")
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=30)
        self.model = resolved_model
        self.context_limit = int(os.environ.get("MODEL_CONTEXT_TOKEN_UPPER_BOUND", "32000"))
        self.last_metadata: dict[str, Any] = {}

    async def decide(self, context: dict[str, Any], seq: int) -> dict[str, Any]:
        system = (
            "你是内容任务规划 Agent。每次仅返回一个 JSON 动作，字段为 action、arguments、reason_summary、observed_event_ids。"
            "可用 action: search_materials、create_plan、revise_plan、select_plan、generate_script、estimate_cost、save_plan、stop。"
            "先检索合成素材，再选择允许的形式与素材制定方案。生成完整脚本后，用 content_hash 绑定新方案版本，"
            "再估算并保存。观察失败、超预算或超时后自行选择修改、替代或停止。"
            "不可修改任务预算、截止时间、费用、工具结果或状态。只能引用上下文里的 ID。"
            "create_plan/revise_plan 参数: selected_format(video/image_text),material_ids(非空，必须来自检索结果),"
            "channel(字符串，默认 social),length(1-10000 的整数，默认 60),quantity(1-100 的整数，默认 1),"
            "generator_strategy(rich/compact，默认 rich),script_hash(绑定完整脚本时填写)。"
            "绑定已有脚本时必须原样保留当前方案的 selected_format、material_ids、channel、length、quantity、"
            "generator_strategy，只新增 script_hash；改变任何生成规格会令旧脚本失效并需要重新生成。"
            "若 rich 策略生成脚本返回 SCRIPT_GENERATION_FAILED，不要对同一方案改 mode 重试；"
            "先用 revise_plan 保留其他规格并把 generator_strategy 改为 compact，再对新 plan_id 生成。"
            "complete_partial 仅可在已有 partial_artifact_id 时调用。"
            "检索无结果时可用空 query 扩大查询，不得用空 material_ids 创建方案。"
            "generate_script 参数: plan_id,mode(full/partial/complete_partial),partial_artifact_id(可选)。"
            "estimate_cost 参数: plan_id,artifact_id。save_plan 参数: plan_id,artifact_id,quote_id。"
            "select_plan 参数: plan_id。search_materials 参数: query,selected_format(可选)。"
            "观察事件 ID 必须来自上下文。不要输出 markdown。"
            "JSON 示例：{\"action\":\"search_materials\",\"arguments\":{\"query\":\"\"},"
            "\"reason_summary\":\"先检索素材\",\"observed_event_ids\":[]}。"
        )
        preflight_model_budget(system, context, max_output_tokens=1200, context_limit=self.context_limit)
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=1200,
        )
        self.last_metadata = {
            "provider": self.provider,
            "model": self.model,
            "provider_request_id": response.id,
            "usage": response.usage.model_dump(mode="json") if response.usage else None,
            "finish_reason": response.choices[0].finish_reason,
        }
        content = response.choices[0].message.content or "{}"
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"invalid_model_output": content[:500]}
        return parsed if isinstance(parsed, dict) else {"invalid_model_output": content[:500]}


class GraphState(TypedDict, total=False):
    task_id: str
    run_id: str
    pending_decision_id: str | None
    last_result: dict[str, Any]
    terminal: bool
    reason_code: str | None
    seen_business_revision: int


class AgentRunner:
    def __init__(self, repo: Repository, model: ActionModel, checkpoint_path: str | Path, fail_rich_generation: bool = False, max_active_runtime_seconds: float = 180.0, max_context_utf8_bytes: int = 20000, max_tool_dispatches: int = 24):
        self.repo = repo
        self.model = model
        self.checkpoint_path = str(checkpoint_path)
        self.fail_rich_generation = fail_rich_generation
        self.max_active_runtime_seconds = max_active_runtime_seconds
        self.max_context_utf8_bytes = max_context_utf8_bytes
        self.max_tool_dispatches = max_tool_dispatches
        Path(self.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)

    def context(self, task_id: str) -> dict[str, Any]:
        task = self.repo.task_input(task_id)
        row = self.repo.task_row(task_id)
        events = self.repo.events(task_id)[-6:]
        current_plan = None
        if row["current_plan_id"]:
            spec = self.repo.plan(task_id, row["current_plan_id"])
            current_plan = {"plan_id": row["current_plan_id"], "spec": spec.model_dump(mode="json"), "spec_hash": spec.spec_hash}
        artifacts = []
        for item in self.repo.connection.execute("SELECT artifact_id,artifact_json FROM artifacts WHERE task_id=? ORDER BY rowid DESC LIMIT 5", (task_id,)):
            data = json.loads(item["artifact_json"])
            matches_current = bool(current_plan and data["dependency_fingerprint"] == self.repo.plan(task_id, current_plan["plan_id"]).generation_fingerprint)
            artifacts.append({"artifact_id": item["artifact_id"], "status": data["status"], "content_hash": data["content_hash"], "dependency_fingerprint": data["dependency_fingerprint"], "matches_current_generation_spec": matches_current})
        quotes = []
        for item in self.repo.connection.execute("SELECT quote_id,quote_json FROM quotes WHERE task_id=? ORDER BY rowid DESC LIMIT 5", (task_id,)):
            data = json.loads(item["quote_json"])
            quote = self.repo.quote(task_id, item["quote_id"])
            current = evaluate_feasibility(task, quote, self.repo.incurred_units(task_id), utcnow())
            quotes.append({"quote_id": item["quote_id"], "plan_id": data["plan_id"], "campaign_units": data["base_units"] + data["license_units"], "production_minutes": data["production_minutes"], "current_feasibility": current.model_dump(mode="json")})
        search_results = []
        for item in self.repo.connection.execute("SELECT result_json FROM operations WHERE task_id=? AND tool_name='search_materials' AND status='finished' ORDER BY rowid DESC LIMIT 3", (task_id,)):
            data = json.loads(item["result_json"])
            search_results.append({"materials": data.get("data", {}).get("materials", []), "outcome": data.get("outcome")})
        search_results.reverse()
        context = {
            "task": {"task_id": task_id, "topic": task.topic, "topic_id": row["topic_id"], "budget_units": task.budget_units, "campaign_deadline_at": task.campaign_deadline_at.isoformat(), "allowed_formats": [value.value for value in task.allowed_formats], "required_points": list(task.required_points)},
            "incurred_tool_units": self.repo.incurred_units(task_id),
            "current_plan": current_plan,
            "artifacts": artifacts,
            "quotes": quotes,
            "search_results": search_results,
            "recent_events": [{"event_id": event["event_id"], "kind": event["kind"], "payload": json.loads(event["payload_json"])} for event in events],
            "remaining_decisions": max(0, 12 - self.repo.decision_count(task_id)),
        }
        def size_bytes() -> int:
            return len(as_json(context).encode("utf-8"))
        while size_bytes() > self.max_context_utf8_bytes and len(context["recent_events"]) > 3:
            context["recent_events"].pop(0)
        while size_bytes() > self.max_context_utf8_bytes and len(context["artifacts"]) > 1:
            context["artifacts"].pop()
        while size_bytes() > self.max_context_utf8_bytes and len(context["quotes"]) > 1:
            context["quotes"].pop()
        while size_bytes() > self.max_context_utf8_bytes and len(context["search_results"]) > 1:
            context["search_results"].pop(0)
        if size_bytes() > self.max_context_utf8_bytes:
            raise BusinessError("CONTEXT_LIMIT")
        return context

    def _make_plan(self, task_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {"selected_format", "material_ids", "channel", "length", "quantity", "generator_strategy", "script_hash"}
        if set(arguments) - allowed:
            raise BusinessError("INVALID_ARGUMENT")
        task = self.repo.task_input(task_id)
        materials = tuple(sorted(set(arguments["material_ids"])))
        if len(materials) != len(arguments["material_ids"]):
            raise BusinessError("INVALID_ARGUMENT")
        hashes = tuple(BY_ID[material_id].content_hash for material_id in materials)
        spec = PlanSpec(
            topic_id=self.repo.task_row(task_id)["topic_id"],
            selected_format=ContentFormat(arguments["selected_format"]),
            channel=arguments.get("channel", "social"),
            material_ids=materials, material_hashes=hashes,
            length=arguments.get("length", 60), quantity=arguments.get("quantity", 1),
            required_points=task.required_points,
            generator_strategy=arguments.get("generator_strategy", "rich"),
            script_hash=arguments.get("script_hash"),
        )
        return self.repo.create_plan(task_id, spec)

    def _dispatch(self, task_id: str, action: Action) -> dict[str, Any]:
        args = action.arguments
        kind = action.action
        if kind in ("create_plan", "revise_plan"):
            return self._make_plan(task_id, args)
        if kind == "select_plan":
            if set(args) != {"plan_id"}:
                raise BusinessError("INVALID_ARGUMENT")
            spec = self.repo.plan(task_id, args["plan_id"])
            return self.repo.create_plan(task_id, spec)
        if kind == "search_materials":
            if set(args) - {"query", "selected_format"}:
                raise BusinessError("INVALID_ARGUMENT")
            format_value = ContentFormat(args["selected_format"]) if args.get("selected_format") else None
            return self.repo.search_materials(task_id, query=args.get("query", ""), selected_format=format_value)
        if kind == "generate_script":
            if set(args) - {"plan_id", "mode", "partial_artifact_id"}:
                raise BusinessError("INVALID_ARGUMENT")
            plan = self.repo.plan(task_id, args["plan_id"])
            fault = "permanent" if self.fail_rich_generation and plan.generator_strategy == "rich" else None
            return self.repo.generate_script(task_id, args["plan_id"], mode=args.get("mode", "full"), partial_artifact_id=args.get("partial_artifact_id"), failure=fault)
        if kind == "estimate_cost":
            if set(args) != {"plan_id", "artifact_id"}:
                raise BusinessError("INVALID_ARGUMENT")
            return self.repo.estimate_cost(task_id, args["plan_id"], args["artifact_id"])
        if kind == "save_plan":
            if set(args) != {"plan_id", "artifact_id", "quote_id"}:
                raise BusinessError("INVALID_ARGUMENT")
            return self.repo.save_plan(task_id, args["plan_id"], args["artifact_id"], args["quote_id"])
        if kind == "stop":
            if set(args) - {"reason_code"}:
                raise BusinessError("INVALID_ARGUMENT")
            return {"status": "incomplete", "reason_code": args.get("reason_code", "MODEL_STOPPED")}
        raise BusinessError("INVALID_ACTION")

    def _graph(self, checkpointer):
        from langgraph.graph import END, START, StateGraph

        async def reconcile(state: GraphState) -> GraphState:
            task = self.repo.snapshot(state["task_id"])
            if task["status"] == "completed":
                return {"terminal": True, "last_result": task, "seen_business_revision": task["revision"]}
            pending = self.repo.pending_decision(state["run_id"])
            return {"pending_decision_id": pending["decision_id"] if pending else None, "terminal": False, "seen_business_revision": task["revision"]}

        async def decide(state: GraphState) -> GraphState:
            task_id = state["task_id"]
            pending = self.repo.pending_decision(state["run_id"])
            if pending:
                return {"pending_decision_id": pending["decision_id"], "seen_business_revision": self.repo.snapshot(task_id)["revision"]}
            context = self.context(task_id)
            reserved_seconds = self.repo.reserve_runtime(task_id, state["run_id"], 30.0, self.max_active_runtime_seconds)
            call_started = time.monotonic()
            try:
                model_request_id = self.repo.begin_model_request(task_id, state["run_id"], context)
                response = await self.model.decide(context, self.repo.decision_count(task_id) + 1)
            except Exception:
                if "model_request_id" in locals():
                    self.repo.mark_model_request_unknown(model_request_id)
                raise
            finally:
                self.repo.settle_runtime(state["run_id"], reserved_seconds, time.monotonic() - call_started)
            metadata = getattr(self.model, "last_metadata", None)
            record = self.repo.record_decision(task_id, state["run_id"], context, response, metadata=metadata, model_request_id=model_request_id)
            return {"pending_decision_id": record["decision_id"], "seen_business_revision": self.repo.snapshot(task_id)["revision"]}

        async def apply(state: GraphState) -> GraphState:
            row = self.repo.connection.execute("SELECT * FROM decisions WHERE decision_id=?", (state["pending_decision_id"],)).fetchone()
            if row is None:
                return {"terminal": True, "reason_code": "STATE_INCONSISTENT"}
            raw = json.loads(row["response_json"])
            try:
                action = Action.model_validate_json(json.dumps(raw, ensure_ascii=False))
                requested_context = json.loads(row["context_json"])
                context_event_ids = {event["event_id"] for event in requested_context["recent_events"]}
                if any(event_id not in context_event_ids for event_id in action.observed_event_ids):
                    raise BusinessError("INVALID_OBSERVATION")
                if action.action in ("search_materials", "generate_script", "estimate_cost", "save_plan"):
                    self.repo.reserve_tool_dispatch(state["task_id"], state["run_id"], limit=self.max_tool_dispatches)
                    reserved_seconds = self.repo.reserve_runtime(state["task_id"], state["run_id"], 5.0, self.max_active_runtime_seconds)
                    call_started = time.monotonic()
                    try:
                        result = self._dispatch(state["task_id"], action)
                    finally:
                        self.repo.settle_runtime(state["run_id"], reserved_seconds, time.monotonic() - call_started)
                else:
                    result = self._dispatch(state["task_id"], action)
            except (BusinessError, ValidationError, ValueError, KeyError, TypeError) as exc:
                result = {"status": "error", "reason_code": exc.code if isinstance(exc, BusinessError) else "INVALID_ACTION", "detail": str(exc)[:300]}
            result["progress_hash"] = self.repo.progress_hash(state["task_id"])
            self.repo.mark_decision_applied(row["decision_id"], result)
            completed = self.repo.snapshot(state["task_id"])["status"] == "completed"
            stopped = raw.get("action") == "stop"
            recent = self.repo.previous_decisions(state["task_id"], limit=12)
            reason_code = result.get("reason_code")
            if len(recent) >= 2:
                old_response = json.loads(recent[1]["response_json"])
                old_result = json.loads(recent[1]["result_json"])
                repeated = old_response.get("action") == raw.get("action") and old_response.get("arguments") == raw.get("arguments")
                def stalled(value: dict[str, Any]) -> bool:
                    return value.get("status") == "error" or value.get("outcome") == "failed" or bool(value.get("replayed"))
                if repeated and stalled(result) and stalled(old_result) and result["progress_hash"] == old_result.get("progress_hash"):
                    reason_code = "NO_PROGRESS"
                if result.get("reason_code") in ("INVALID_ACTION", "INVALID_OBSERVATION") and old_result.get("reason_code") in ("INVALID_ACTION", "INVALID_OBSERVATION"):
                    reason_code = "INVALID_ACTION_LIMIT"
            if raw.get("action") in ("create_plan", "revise_plan", "select_plan") and result.get("plan_id"):
                for prior in recent[1:]:
                    prior_result = json.loads(prior["result_json"])
                    if prior_result.get("plan_id") == result["plan_id"] and prior_result.get("progress_hash") == result["progress_hash"]:
                        reason_code = "NO_PROGRESS"
                        break
            return {"pending_decision_id": None, "last_result": result, "terminal": completed or stopped or reason_code in ("NO_PROGRESS", "INVALID_ACTION_LIMIT", "TOOL_DISPATCH_LIMIT", "TOOL_ATTEMPT_LIMIT"), "reason_code": reason_code, "seen_business_revision": self.repo.snapshot(state["task_id"])["revision"]}

        async def route_reconcile(state: GraphState) -> str:
            if state.get("terminal"):
                return "end"
            if state.get("pending_decision_id"):
                return "apply"
            if self.repo.decision_count(state["task_id"]) >= 12:
                return "end"
            return "decide"

        async def route_apply(state: GraphState) -> str:
            return "end" if state.get("terminal") else "reconcile"

        graph = StateGraph(GraphState)
        graph.add_node("reconcile", reconcile)
        graph.add_node("decide", decide)
        graph.add_node("apply", apply)
        graph.add_edge(START, "reconcile")
        graph.add_conditional_edges("reconcile", route_reconcile, {"end": END, "apply": "apply", "decide": "decide"})
        graph.add_edge("decide", "apply")
        graph.add_conditional_edges("apply", route_apply, {"end": END, "reconcile": "reconcile"})
        return graph.compile(checkpointer=checkpointer)

    async def run(self, task_id: str) -> dict[str, Any]:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        remaining = self.max_active_runtime_seconds - self.repo.active_seconds(task_id)
        if remaining <= 0:
            run = self.repo.begin_run(task_id)
            self.repo.finish_run(run["run_id"], "incomplete", reason_code="ACTIVE_RUNTIME_LIMIT")
            return {
                **self.repo.result_view(task_id, "ACTIVE_RUNTIME_LIMIT"),
                "run_id": run["run_id"],
                "thread_id": run["thread_id"],
                "reason_code": "ACTIVE_RUNTIME_LIMIT",
            }
        run = self.repo.begin_run(task_id)
        config = {"configurable": {"thread_id": run["thread_id"]}, "recursion_limit": 100}
        started = time.monotonic()
        try:
            async with AsyncSqliteSaver.from_conn_string(self.checkpoint_path) as saver:
                graph = self._graph(saver)
                final_state = await asyncio.wait_for(graph.ainvoke({"task_id": task_id, "run_id": run["run_id"], "terminal": False}, config=config, durability="sync"), timeout=remaining)
            status = self.repo.snapshot(task_id)["status"]
            if status != "completed":
                status = "incomplete"
            reason_code = final_state.get("reason_code") or ("DECISION_LIMIT" if self.repo.decision_count(task_id) >= 12 else None)
            self.repo.finish_run(run["run_id"], status, active_seconds=time.monotonic() - started, reason_code=reason_code)
            return {**self.repo.result_view(task_id, reason_code), "run_id": run["run_id"], "thread_id": run["thread_id"], "last_result": final_state.get("last_result"), "reason_code": reason_code}
        except TimeoutError:
            self.repo.finish_run(run["run_id"], "incomplete", active_seconds=time.monotonic() - started, reason_code="ACTIVE_RUNTIME_LIMIT")
            return {**self.repo.result_view(task_id, "ACTIVE_RUNTIME_LIMIT"), "run_id": run["run_id"], "thread_id": run["thread_id"], "reason_code": "ACTIVE_RUNTIME_LIMIT"}
        except BusinessError as exc:
            self.repo.finish_run(run["run_id"], "incomplete", active_seconds=time.monotonic() - started, reason_code=exc.code)
            return {**self.repo.result_view(task_id, exc.code), "run_id": run["run_id"], "thread_id": run["thread_id"], "reason_code": exc.code}
        except Exception:
            self.repo.finish_run(run["run_id"], "incomplete", active_seconds=time.monotonic() - started)
            raise

    async def recover(self, task_id: str) -> dict[str, Any]:
        """Continue a running graph on its original thread after an abrupt process exit."""
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        run = self.repo.running_run(task_id)
        config = {"configurable": {"thread_id": run["thread_id"]}, "recursion_limit": 100}
        async with AsyncSqliteSaver.from_conn_string(self.checkpoint_path) as saver:
            graph = self._graph(saver)
            checkpoint = await graph.aget_state(config)
            if not checkpoint.values:
                raise BusinessError("CHECKPOINT_MISSING")
            values = checkpoint.values
            if values.get("task_id") != task_id or values.get("run_id") != run["run_id"]:
                raise BusinessError("STATE_INCONSISTENT")
            if values.get("seen_business_revision", 0) > self.repo.snapshot(task_id)["revision"]:
                raise BusinessError("STATE_INCONSISTENT")
            pending_id = values.get("pending_decision_id")
            if pending_id and self.repo.connection.execute("SELECT 1 FROM decisions WHERE decision_id=? AND run_id=?", (pending_id, run["run_id"])).fetchone() is None:
                raise BusinessError("STATE_INCONSISTENT")
            self.repo.consume_reserved_runtime(run["run_id"])
            base_active = float(self.repo.connection.execute("SELECT active_seconds FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()[0])
            remaining = self.max_active_runtime_seconds - self.repo.active_seconds(task_id)
            if remaining <= 0:
                self.repo.finish_run(run["run_id"], "incomplete", active_seconds=base_active, reason_code="ACTIVE_RUNTIME_LIMIT")
                return {
                    **self.repo.result_view(task_id, "ACTIVE_RUNTIME_LIMIT"),
                    "run_id": run["run_id"],
                    "thread_id": run["thread_id"],
                    "reason_code": "ACTIVE_RUNTIME_LIMIT",
                    "recovered": True,
                }
            started = time.monotonic()
            try:
                final_state = await asyncio.wait_for(graph.ainvoke(None, config=config, durability="sync"), timeout=remaining)
            except TimeoutError:
                active_seconds = base_active + time.monotonic() - started
                self.repo.finish_run(run["run_id"], "incomplete", active_seconds=active_seconds, reason_code="ACTIVE_RUNTIME_LIMIT")
                return {
                    **self.repo.result_view(task_id, "ACTIVE_RUNTIME_LIMIT"),
                    "run_id": run["run_id"],
                    "thread_id": run["thread_id"],
                    "reason_code": "ACTIVE_RUNTIME_LIMIT",
                    "recovered": True,
                }
            except BusinessError as exc:
                active_seconds = base_active + time.monotonic() - started
                self.repo.finish_run(run["run_id"], "incomplete", active_seconds=active_seconds, reason_code=exc.code)
                return {
                    **self.repo.result_view(task_id, exc.code),
                    "run_id": run["run_id"],
                    "thread_id": run["thread_id"],
                    "reason_code": exc.code,
                    "recovered": True,
                }
            except Exception:
                active_seconds = base_active + time.monotonic() - started
                self.repo.finish_run(run["run_id"], "incomplete", active_seconds=active_seconds)
                raise
        status = "completed" if self.repo.snapshot(task_id)["status"] == "completed" else "incomplete"
        reason_code = final_state.get("reason_code")
        self.repo.finish_run(run["run_id"], status, active_seconds=base_active + time.monotonic() - started, reason_code=reason_code)
        return {**self.repo.result_view(task_id, reason_code), "run_id": run["run_id"], "thread_id": run["thread_id"], "last_result": final_state.get("last_result"), "reason_code": reason_code, "recovered": True}

    async def rebuild_checkpoint(self, task_id: str) -> dict[str, Any]:
        """Explicitly start a new thread from intact business facts when no checkpoint exists."""
        if self.repo.snapshot(task_id)["status"] == "completed":
            return {**self.repo.result_view(task_id), "replayed": True}
        self.repo.abandon_run_for_rebuild(task_id)
        result = await self.run(task_id)
        result["checkpoint_rebuilt"] = True
        return result
