"""Managed Agents harness bridged to the existing Agents SDK tool/guardrail loop.

The service owns session context and compaction. Local function execution stays
with Runner so the application's evidence receipts and output guards still run.
Session usage is explicitly best effort in the API contract. Unlimited sessions
omit spend control and report estimates separately from settled billing.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from contextlib import suppress
from typing import Any
from uuid import uuid4

import httpx2
from agents import ModelResponse, Usage
from agents.models.openai_responses import Converter
from openai import APIConnectionError, APIStatusError, BadRequestError
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputItem,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
)

from .config import (
    estimate_token_cost,
    model_capabilities,
    normalize_model_request,
    require_application_model,
)
from .model import (
    AgentModel,
    BudgetExceeded,
    ProviderStopped,
    ResponseProtocolError,
    compact_items,
    dump,
)


def managed_trace(event: Any) -> tuple[str, dict[str, Any]] | None:
    """Forward public managed events without exposing raw reasoning content."""
    item = dump(event)
    if not isinstance(item, dict):
        item = vars(event)
    return _managed_trace(item)


def _managed_trace(item: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Project an already normalized event without serializing its payload again."""
    kind = item.get("type", "")
    base = {"provider_api": "agents", "provider_event": kind}
    for key in (
        "event_id",
        "session_id",
        "turn_id",
        "item_id",
        "output_index",
        "content_index",
        "summary_index",
    ):
        if item.get(key) is not None:
            base[key] = item[key]
    if kind.endswith(".output_text.delta"):
        return "text_delta", {**base, "delta": item.get("delta", "")}
    if kind.endswith(".output_text.done"):
        return "text_done", {**base, "text": item.get("text", "")}
    if kind.endswith(".function_call_arguments.delta") or kind.endswith(".arguments.delta"):
        return "tool_arguments_delta", {
            **base,
            "call_id": item.get("call_id"),
            "item_id": item.get("item_id"),
            "delta": item.get("delta", ""),
        }
    if kind.endswith(".reasoning_summary_text.delta"):
        return "reasoning_summary", {**base, "delta": item.get("delta", "")}
    if kind.endswith(".reasoning_summary_text.done"):
        return "reasoning_summary", {**base, "text": item.get("text", ""), "done": True}
    if kind in {
        "agent.session.turn.reasoning_summary_part.added",
        "agent.session.turn.reasoning_summary_part.done",
    }:
        return "reasoning_part", {
            **base,
            "phase": "done" if kind.endswith(".done") else "added",
            "text": item.get("part", {}).get("text", ""),
        }
    if kind in {"agent.session.turn.item.added", "agent.session.turn.item.done"}:
        output = item.get("item", {})
        return "output_item", {
            **base,
            "phase": "done" if kind.endswith(".done") else "added",
            "item_id": output.get("id"),
            "item_type": output.get("type"),
            "name": output.get("name"),
            "call_id": output.get("call_id"),
            "status": output.get("status"),
            "message_phase": output.get("phase"),
        }
    if kind == "error":
        error = item.get("error", {})
        return "provider_error", {
            **base,
            "code": error.get("code"),
            "message": error.get("message", ""),
            "error_type": error.get("type"),
        }
    if kind in {
        "agent.session.created",
        "agent.session.in_progress",
        "agent.session.requires_action",
        "agent.session.idle",
        "agent.session.failed",
        "agent.session.turn.created",
        "agent.session.turn.in_progress",
        "agent.session.turn.completed",
        "agent.session.turn.failed",
        "agent.session.turn.cancelled",
    }:
        session = item.get("session", {})
        turn = item.get("turn", {})
        return "provider_phase", {
            **base,
            "session_id": session.get("id", base.get("session_id")),
            "phase": kind.rsplit(".", 1)[-1],
            "provider_status": session.get("status", turn.get("status", kind.rsplit(".", 1)[-1])),
            "provider_turn_status": turn.get("status"),
        }
    return None


class ManagedAgentsModel(AgentModel):
    """A single managed session for one local SDK agent role, with no fallback.

    Bounded callers may allocate ``session_budget_usd`` explicitly. In the owner's
    unbounded profile no spend limit is sent or reserved. Provider usage remains
    a revisable best-effort observation, never settled billing.
    """

    def __init__(self, *args: Any, session_budget_usd: float | None = None, **kwargs: Any) -> None:
        if session_budget_usd is not None and (
            not math.isfinite(session_budget_usd) or session_budget_usd <= 0
        ):
            raise ValueError("Managed session budget must be finite and positive")
        self.role = kwargs.pop("role", "analyst")
        supplied_client = kwargs.get("client")
        super().__init__(*args, **kwargs)
        if supplied_client is None:
            # Each HTTP exchange has finite transport timeouts. A read timeout
            # recovers the same durable session; it does not impose a run deadline.
            self.client = self.client.with_options(
                timeout=httpx2.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0)
            )
        self.session_budget_usd = session_budget_usd
        self.session_id: str | None = None
        self.turn_id: str | None = None
        self._turn_status: str | None = None
        self.pending_session_id: str | None = None
        self._session_limit_cents: int | None = None
        self._pending_calls: dict[str, dict[str, Any]] = {}
        self._submitted_calls: set[str] = set()
        self._seen_items: set[str] = set()
        self._event_ids: set[str] = set()
        self._reasoning_items: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {}
        self._reasoning_summaries: dict[tuple[str, str, str], dict[int, str]] = {}
        self._reasoning_aliases: set[tuple[str, str, str]] = set()
        self._boundary = 0
        self._accounted_cap = False
        self._configuration: str | None = None
        self._estimated_session_cost_usd = 0.0
        self._accounted_tokens = {"input_tokens": 0, "output_tokens": 0}
        self._caller_history: list[dict] = []
        self._current_caller_input: list[dict] = []
        self._steering_lock = asyncio.Lock()
        self._accepts_live_steering = False
        self._steering_root_turn_id: str | None = None
        self._cancel_requested = False
        self._cancel_task: asyncio.Task[None] | None = None
        self._live_inputs: list[dict] = []
        self._live_submissions: dict[str, dict[str, Any]] = {}
        self._recovery_deadline: float | None = None
        self.budget.setdefault("accounted_session_ids", [])
        self.budget.setdefault("managed_reserved_cost_usd", 0.0)

    async def _close_owned_client(self) -> None:
        # Repeated task cancellation may leave the shielded provider readback
        # running. Drain that existing work before closing its HTTP transport;
        # resource cleanup itself never dispatches a cancellation or settles usage.
        try:
            if self._cancel_task is not None:
                await asyncio.shield(self._cancel_task)
        finally:
            await super()._close_owned_client()

    @property
    def unbounded(self) -> bool:
        return self.settings.unbounded_runs

    @property
    def sessions(self) -> Any:
        return self.client.beta.agents.sessions

    async def steer(self, text: str, *, instruction_id: str) -> dict[str, Any]:
        """Admit one durable message to the currently observed native generation.

        A message racing with provider idle may start another native turn, but
        the final snapshot still settles that same session before returning to
        Runner. Startup and closed boundaries are revised by Runner instead of
        dispatching unobserved work. Session creation alone does not establish
        a running turn. An ambiguous HTTP result is never replayed implicitly.
        """
        if self._cancel_requested:
            raise ResponseProtocolError("This managed session has stopped accepting instructions")
        async with self._steering_lock:
            if self._cancel_requested:
                raise ResponseProtocolError(
                    "This managed session has stopped accepting instructions"
                )
            prior = self._live_submissions.get(instruction_id)
            if prior is not None:
                if prior["text"] != text:
                    raise ValueError("An instruction ID cannot be reused for different text")
                if prior["state"] != "accepted":
                    raise ResponseProtocolError("Native steering admission remains unresolved")
                return {"accepted": True, "mode": "native_inflight"}
            if not self._accepts_live_steering or not self.session_id:
                return {"accepted": False, "mode": "same_run_boundary"}
            self._live_submissions[instruction_id] = {"text": text, "state": "unresolved"}
            await self.sessions.events.create(
                self.session_id,
                idempotency_key=instruction_id,
                events=[
                    {
                        "type": "agent.session.input.message",
                        "input": self._messages([{"role": "user", "content": text}]),
                    }
                ],
            )
            self._live_submissions[instruction_id]["state"] = "accepted"
            self._live_inputs.append({"role": "user", "content": text})
            return {
                "accepted": True,
                "mode": "native_inflight",
                "session_id": self.session_id,
            }

    def _steering_phase(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Carry an observed root turn across ordinary tool-result boundaries."""
        kind = raw.get("type", "")
        origin = None
        if (
            kind != "agent.session.turn.in_progress"
            and raw.get("turn_id")
            and raw["turn_id"] != self._steering_root_turn_id
        ):
            self._steering_root_turn_id = None
            self._accepts_live_steering = False
        if kind == "agent.session.turn.in_progress":
            turn = raw.get("turn") or {}
            # Session creation may report in_progress before its turn starts.
            self._accepts_live_steering = (
                not self._cancel_requested
                and bool(self.session_id)
                and raw.get("session_id") == self.session_id
                and isinstance(self.turn_id, str)
                and bool(self.turn_id)
                and raw.get("turn_id") == self.turn_id
                and isinstance(turn, dict)
                and turn.get("id") == raw.get("turn_id")
                and turn.get("session_id") == self.session_id
                and turn.get("status") == "in_progress"
                and turn.get("subagent_id") is None
            )
            self._steering_root_turn_id = self.turn_id if self._accepts_live_steering else None
            origin = "turn_in_progress"
        elif kind == "agent.session.in_progress":
            session = raw.get("session") or {}
            # A tool result resumes the same nonterminal root turn without
            # emitting another turn.in_progress. Never infer a root at startup.
            self._accepts_live_steering = (
                not self._cancel_requested
                and self._steering_root_turn_id is not None
                and self.turn_id == self._steering_root_turn_id
                and session.get("id") == self.session_id
                and session.get("status") == "in_progress"
            )
            if not self._accepts_live_steering:
                self._steering_root_turn_id = None
            origin = "session_continuation"
        elif kind == "agent.session.requires_action":
            self._accepts_live_steering = False
        elif kind in {
            "agent.session.created",
            "agent.session.idle",
            "agent.session.failed",
            "agent.session.turn.created",
            "agent.session.turn.completed",
            "agent.session.turn.failed",
            "agent.session.turn.cancelled",
            "error",
        }:
            self._accepts_live_steering = False
            self._steering_root_turn_id = None
        projection: dict[str, Any] = {"steering_ready": self._accepts_live_steering}
        if self._accepts_live_steering:
            projection.update(
                steering_root_turn_id=self._steering_root_turn_id,
                steering_root_turn_status="in_progress",
                steering_readiness_source=origin,
            )
        return projection

    def _verify_session(self, session: Any) -> None:
        if self.session_id is not None and session.id != self.session_id:
            self._accepts_live_steering = False
            self._steering_root_turn_id = None
            raise ResponseProtocolError("Managed session identity changed")
        if session.agent.model != self.settings.model:
            raise ResponseProtocolError(
                "Managed session returned a different model; output rejected"
            )
        if session.agent.multi_agent.enabled:
            raise ResponseProtocolError("Managed session unexpectedly enabled native subagents")
        if getattr(session.environment, "type", None) != "none":
            raise ResponseProtocolError(
                "Managed session unexpectedly enabled an execution environment"
            )
        spend = getattr(session, "spend_control", None)
        if self.unbounded:
            if spend is not None:
                raise ResponseProtocolError("Unlimited managed session returned a spending limit")
            return
        if spend is None or spend.limit != self._session_limit_cents:
            raise ResponseProtocolError(
                "Managed session did not preserve its finite spending limit"
            )

    def _agent_configuration(
        self, instructions: Any, tools: Any, schema: Any, handoffs: Any
    ) -> dict:
        if handoffs:
            raise ResponseProtocolError("Managed adapter does not permit native handoffs")
        request = normalize_model_request(self.settings)
        functions = dump(Converter.convert_tools(tools, []).tools)
        if any(tool.get("type") != "function" for tool in functions):
            raise ResponseProtocolError("Managed adapter only exposes local function tools")
        functions = [
            {key: tool[key] for key in ("type", "name", "description", "parameters") if key in tool}
            for tool in functions
        ]
        hosted = self.settings.hosted_tools()
        if hosted:
            raise ResponseProtocolError(
                "Configure managed hosted tools explicitly before enabling them"
            )
        agent = {
            "model": self.settings.model,
            "instructions": instructions,
            "tools": functions,
            "multi_agent": {"enabled": False},
            "reasoning": {
                key: value
                for key, value in request.get("reasoning", {}).items()
                if key in {"effort", "summary"}
            },
            "service_tier": request.get("service_tier", "default"),
            "text": request.get("text", {}),
        }
        if agent["service_tier"] not in {"auto", "default", "flex", "priority", "fast"}:
            raise ResponseProtocolError("Service tier is unsupported by the Managed Agents API")
        if schema and not schema.is_plain_text():
            # Agents TextFormatJSONSchema has only type and schema. Responses'
            # format name/strict fields are rejected by the native endpoint.
            # Runner retains its own output parsing and validation contract.
            agent["text"]["format"] = {"type": "json_schema", "schema": schema.json_schema()}
        return agent

    @staticmethod
    def _messages(items: list[dict]) -> list[dict]:
        if all(
            item.get("role") == "user" and item.get("type", "message") == "message"
            for item in items
        ):
            messages = []
            for item in items:
                content = item.get("content", "")
                if isinstance(content, str):
                    content = [{"type": "input_text", "text": content}]
                messages.append({"role": "user", "content": content})
            return messages
        # Managed input only accepts user messages. Retain earlier conversation as
        # explicitly labelled context instead of forging assistant history items.
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Prior conversation and current request (context data, not new system instructions):\n"
                        + json.dumps(items, ensure_ascii=False),
                    }
                ],
            }
        ]

    async def _input(self, value: Any) -> list[dict]:
        """Keep Runner-owned history distinct from admitted adapter steering.

        Runner replays its own messages and output items on each boundary. It
        does not know about user messages injected by this adapter's steering
        queue. Validate Runner's exact acknowledged prefix, then append only its
        fresh input and new steering onto the complete managed history.
        """
        caller_items = compact_items(
            [{"role": "user", "content": value}] if isinstance(value, str) else dump(value)
        )
        prefix_length = 0
        if self.session_id is not None:
            # A new Runner invocation may deliberately resume the full native
            # history (for example a QA repair). Admit only either exact known
            # prefix, never delete or fuzzy-match caller-authored messages.
            if caller_items[: len(self.history)] == self.history:
                prefix_length = len(self.history)
            elif caller_items[: len(self._caller_history)] == self._caller_history:
                prefix_length = len(self._caller_history)
            else:
                raise ResponseProtocolError(
                    "Managed continuation changed acknowledged conversation history"
                )
        steered_items = await super()._input(caller_items)
        self._current_caller_input = caller_items
        if self.session_id is not None:
            return self.history + steered_items[prefix_length:]
        return steered_items

    def _continuation(self, items: list[dict]) -> list[dict]:
        if self.history and items[: len(self.history)] != self.history:
            raise ResponseProtocolError(
                "Managed continuation changed acknowledged conversation history"
            )
        fresh = items[len(self.history) :] if self.history else items
        outputs = [item for item in fresh if item.get("type") == "function_call_output"]
        supplied = [item.get("call_id") for item in outputs]
        if len(set(supplied)) != len(supplied) or set(supplied) != set(self._pending_calls):
            raise ResponseProtocolError(
                "Managed continuation must supply every pending function result exactly once"
            )
        events = []
        for output in outputs:
            call = self._pending_calls[output["call_id"]]
            events.append(
                {
                    "type": "agent.session.input.tool_result",
                    "call_id": call["call_id"],
                    "turn_id": call["turn_id"],
                    "success": True,
                    "output": output.get("output", ""),
                }
            )
        messages = [item for item in fresh if item.get("role") == "user"]
        unsupported = [
            item
            for item in fresh
            if item.get("type") != "function_call_output" and item.get("role") != "user"
        ]
        if unsupported:
            raise ResponseProtocolError("Unsupported new item in managed continuation")
        if messages:
            events.append(
                {"type": "agent.session.input.message", "input": self._messages(messages)}
            )
        if not events:
            raise ResponseProtocolError("Managed continuation has no new input")
        return events

    @staticmethod
    def _durable_reasoning_summary(item: dict[str, Any]) -> tuple[str, ...] | None:
        # Persisted public summaries can be complete while their opaque
        # reasoning item is incomplete. This helper only reconciles identities;
        # it never upgrades provider completion or accounting state.
        if item.get("type") != "reasoning" or item.get("status") not in {
            "completed",
            "incomplete",
        }:
            return None
        parts = item.get("summary")
        if not isinstance(parts, list) or not parts:
            return None
        if not all(
            isinstance(part, dict)
            and part.get("type") == "summary_text"
            and isinstance(part.get("text"), str)
            for part in parts
        ):
            return None
        summary = tuple(part["text"] for part in parts)
        return summary if any(summary) else None

    def _remember_reasoning_items(self, records: list[dict]) -> None:
        """Retain persisted public summary identities without changing completion."""
        if not self.session_id:
            return
        for item in records:
            summary = self._durable_reasoning_summary(item)
            identity, turn = item.get("id"), item.get("turn_id")
            if summary is not None and isinstance(identity, str) and isinstance(turn, str):
                self._reasoning_items.setdefault((self.session_id, turn), {})[identity] = summary

    def _remember_public_summary(self, mapped: tuple[str, dict[str, Any]]) -> None:
        kind, payload = mapped
        if not (
            (kind == "reasoning_summary" and payload.get("done") is True)
            or (kind == "reasoning_part" and payload.get("phase") == "done")
        ):
            return
        session, turn, identity = (
            payload.get("session_id"),
            payload.get("turn_id"),
            payload.get("item_id"),
        )
        index, text = payload.get("summary_index"), payload.get("text")
        if (
            isinstance(session, str)
            and bool(session)
            and isinstance(turn, str)
            and bool(turn)
            and isinstance(identity, str)
            and bool(identity)
            and isinstance(index, int)
            and not isinstance(index, bool)
            and index >= 0
            and isinstance(text, str)
        ):
            self._reasoning_summaries.setdefault((session, turn, identity), {})[index] = text

    async def _reconcile_reasoning_aliases(self, records: list[dict]) -> None:
        """Reconcile transient replay IDs only after the durable items read.

        Continuations can repeat public summaries under a temporary item ID
        absent from saved history. Genuine saved items keep their identities,
        even when their text repeats an earlier item. No provider input changes.
        """
        self._remember_reasoning_items(records)
        durable_ids = self._seen_items | {item["id"] for item in records if item.get("id")}
        for identity, parts in self._reasoning_summaries.items():
            session, turn, item_id = identity
            if (
                session != self.session_id
                or turn != self.turn_id
                or item_id in durable_ids
                or identity in self._reasoning_aliases
                or sorted(parts) != list(range(len(parts)))
            ):
                continue
            summary = tuple(parts[index] for index in range(len(parts)))
            # A live stream can split one durable reasoning item into several
            # transient items, each beginning at summary index zero. Match an
            # exact contiguous sequence within persisted items of this turn;
            # repeated text with more than one possible position is ambiguous.
            matches = [
                (key, offset, len(value))
                for key, value in self._reasoning_items.get((session, turn), {}).items()
                for offset in range(len(value) - len(summary) + 1)
                if value[offset : offset + len(summary)] == summary
            ]
            if len(matches) == 1:
                canonical_item, offset, canonical_length = matches[0]
                await self.emit(
                    "reasoning_alias",
                    {
                        "provider_api": "agents",
                        "source": "session_snapshot",
                        "reconciled": True,
                        "session_id": session,
                        "turn_id": turn,
                        "item_id": item_id,
                        "canonical_item_id": canonical_item,
                        **(
                            {
                                "canonical_summary_indices": list(
                                    range(offset, offset + len(summary))
                                )
                            }
                            if offset or len(summary) != canonical_length
                            else {}
                        ),
                    },
                )
                self._reasoning_aliases.add(identity)

    async def _observe(self, stream: Any, *, finalized_items: set[str] | None = None) -> bool:
        """Consume to a required-action boundary or a confirmed terminal turn."""
        ended = False
        async for event in stream:
            raw = dump(event)
            if not isinstance(raw, dict):
                raw = vars(event)
            event_id = raw.get("event_id")
            if event_id and event_id in self._event_ids:
                continue
            if event_id:
                self._event_ids.add(event_id)
            session = getattr(event, "session", None)
            if session is not None:
                if self.session_id is None:
                    if not isinstance(session.id, str) or not session.id:
                        raise ResponseProtocolError("Managed session omitted its identity")
                    self.session_id = session.id
                    self.pending_session_id = self.session_id
                    await self.emit(
                        "managed_session_created",
                        {
                            "provider_api": "agents",
                            "session_id": session.id,
                            "spend_limit_cents": self._session_limit_cents,
                            "unbounded": self.unbounded,
                        },
                    )
                self._verify_session(session)
            if raw.get("session_id") and raw["session_id"] != self.session_id:
                self._accepts_live_steering = False
                self._steering_root_turn_id = None
                raise ResponseProtocolError("Managed event belongs to a different session")
            # Reconnected streams can buffer updates while saved items are read.
            # The durable completed item wins over any of its buffered updates.
            item_id = raw.get("item_id") or (raw.get("item") or {}).get("id")
            if item_id and finalized_items and item_id in finalized_items:
                continue
            if raw.get("turn_id"):
                self.turn_id = raw["turn_id"]
            kind = raw.get("type", "")
            readiness = self._steering_phase(raw)
            mapped = _managed_trace(raw)
            if mapped:
                if mapped[0] == "provider_phase":
                    mapped = (mapped[0], {**mapped[1], **readiness})
                self._remember_public_summary(mapped)
                await self.emit(*mapped)
            if kind in {
                "agent.session.turn.completed",
                "agent.session.turn.failed",
                "agent.session.turn.cancelled",
            }:
                ended = True
            if kind in {"agent.session.requires_action", "agent.session.failed"}:
                return True
            if kind == "agent.session.idle" and ended:
                return True
        self._accepts_live_steering = False
        self._steering_root_turn_id = None
        return False

    @staticmethod
    def _transient_read_error(error: Exception) -> bool:
        return isinstance(error, (APIConnectionError, httpx2.TransportError, OSError)) or (
            isinstance(error, APIStatusError)
            and (error.status_code in {408, 429} or error.status_code >= 500)
        )

    async def _read_retry(self, operation: Any) -> Any:
        """Retry observations, never input admission, against the same session."""
        delay = 0.25
        while True:
            remaining = (
                self._recovery_deadline - time.monotonic()
                if self._recovery_deadline is not None
                else None
            )
            if remaining is not None and remaining <= 0:
                raise TimeoutError("Managed recovery exceeded the bounded run deadline")
            try:
                async with asyncio.timeout(remaining):
                    return await operation()
            except Exception as error:
                if not self._transient_read_error(error):
                    raise
                if self._recovery_deadline is not None:
                    remaining = self._recovery_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "Managed recovery exceeded the bounded run deadline"
                        ) from error
                    await asyncio.sleep(min(delay, remaining))
                else:
                    await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

    async def _recover_stream(self) -> None:
        """Reattach live output and reconcile saved items without resending work.

        Agents streams do not replay. Buffer the new stream while reading saved
        state, restore completed items by ID, then consume the remaining events.
        Incomplete items settle through their live done event or the final saved
        snapshot; their partial deltas cannot safely be aligned across a gap.
        """
        while True:
            self._accepts_live_steering = False
            self._steering_root_turn_id = None
            stream = await self._read_retry(lambda: self.sessions.events.stream(self.session_id))
            buffered: asyncio.Queue[Any] = asyncio.Queue()
            ended = object()

            async def receive(
                observed_stream: Any = stream,
                queue: asyncio.Queue[Any] = buffered,
                sentinel: object = ended,
            ) -> None:
                try:
                    async for event in observed_stream:
                        queue.put_nowait(event)
                except Exception as error:
                    queue.put_nowait(error)
                finally:
                    queue.put_nowait(sentinel)

            async def events(queue: asyncio.Queue[Any] = buffered, sentinel: object = ended) -> Any:
                while True:
                    item = await queue.get()
                    if item is sentinel:
                        return
                    if isinstance(item, Exception):
                        raise item
                    yield item

            reader = asyncio.create_task(receive())
            try:
                await asyncio.sleep(0)
                session = await self._read_retry(lambda: self.sessions.retrieve(self.session_id))
                self._verify_session(session)
                records = await self._snapshot_items(session)
                complete = [item for item in records if item.get("status") == "completed"]
                await self._reconcile_snapshot(session, complete)
                if session.status != "in_progress":
                    return
                finalized = {item["id"] for item in complete if item.get("id")}
                if await self._observe(events(), finalized_items=finalized):
                    return
            except Exception as error:
                self._steering_root_turn_id = None
                if not self._transient_read_error(error):
                    raise
            finally:
                self._accepts_live_steering = False
                reader.cancel()
                with suppress(asyncio.CancelledError):
                    await reader
                close = getattr(stream, "close", None)
                if callable(close):
                    with suppress(Exception):
                        result = close()
                        if inspect.isawaitable(result):
                            await result
            # A closed observer is not a failed generation. Reattach to the same
            # session after a finite delay, honoring only explicit bounded runs.
            if self._recovery_deadline is not None and time.monotonic() >= self._recovery_deadline:
                raise TimeoutError("Managed recovery exceeded the bounded run deadline")
            await asyncio.sleep(0.25)

    async def _settled_session(self) -> Any:
        if not self.session_id:
            raise ResponseProtocolError(
                "Managed creation ended without a confirmed session ID; no retry"
            )
        session = await self._read_retry(lambda: self.sessions.retrieve(self.session_id))
        self._verify_session(session)
        while session.status == "in_progress":
            await asyncio.sleep(0.25)
            session = await self._read_retry(lambda: self.sessions.retrieve(self.session_id))
            self._verify_session(session)
        return session

    async def _snapshot_items(self, session: Any) -> list[dict]:
        """Overlap independent durable reads, then require both before using any item."""
        reads = (
            asyncio.create_task(self._read_retry(lambda: self._verify_turn_boundary(session))),
            asyncio.create_task(self._read_retry(self._items)),
        )
        try:
            _, records = await asyncio.gather(*reads)
            return records
        finally:
            # gather does not cancel siblings when one read raises. Join cleanup
            # before returning or propagating the original error/cancellation.
            for task in reads:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*reads, return_exceptions=True)

    async def _verify_turn_boundary(self, session: Any) -> None:
        """Read durable turn status after streaming or transport recovery."""
        page = await self.sessions.turns.list(self.session_id, order="desc", limit=1)
        # SDK pages auto-paginate. The requested limit is a page size, so stop
        # after the newest turn instead of retrieving all historical pages.
        turn = await anext(aiter(page), None)
        if turn is None:
            raise ResponseProtocolError("Managed session omitted its turn identity")
        if turn.session_id != self.session_id:
            raise ResponseProtocolError("Managed turn belongs to a different session")
        if self._steering_root_turn_id not in {None, turn.id}:
            self._accepts_live_steering = False
            self._steering_root_turn_id = None
        self.turn_id = turn.id
        self._turn_status = turn.status
        if session.status == "requires_action":
            if turn.status != "waiting":
                raise ResponseProtocolError("Managed required actions have no waiting turn")
            if any(
                action.turn_id != turn.id
                for action in session.required_actions
                if action.type == "function_call"
            ):
                raise ResponseProtocolError("Managed required action belongs to a different turn")
        elif session.status == "idle" and turn.status != "completed":
            raise ResponseProtocolError(f"Managed turn ended {turn.status}; final output rejected")

    async def _items(self) -> list[dict]:
        page = await self.sessions.items.list(self.session_id, order="asc", limit=100)
        result = []
        async for item in page:
            # Keep every page, but avoid copying payloads already reconciled by
            # ID. SDK items expose this field before full JSON normalization.
            identity = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
            if identity in self._seen_items:
                continue
            raw = dump(item)
            if raw.get("id") not in self._seen_items:
                result.append(raw)
        return result

    async def _reconcile_snapshot(self, session: Any, records: list[dict]) -> None:
        """Replace partial UI output with verified public items after disconnects.

        These are explicitly snapshot observations, not reconstructed SSE events.
        The caller has checked session configuration and the current turn status.
        """
        for index, item in enumerate(records):
            if item.get("turn_id") != self.turn_id:
                continue
            base = {
                "provider_api": "agents",
                "source": "session_snapshot",
                "reconciled": True,
                "session_id": self.session_id,
                "turn_id": self.turn_id,
                "output_index": index,
                "item_id": item.get("id"),
            }
            metadata = {
                **base,
                "item_type": item.get("type"),
                "name": item.get("name"),
                "call_id": item.get("call_id"),
                "status": item.get("status"),
                "message_phase": item.get("phase"),
            }
            if item.get("type") == "message" and item.get("role") != "assistant":
                continue
            await self.emit("output_item", {**metadata, "phase": "added"})
            if item.get("type") == "message":
                for content_index, part in enumerate(item.get("content", [])):
                    if part.get("type") == "output_text":
                        await self.emit(
                            "text_done",
                            {
                                **base,
                                "content_index": content_index,
                                "text": part.get("text", ""),
                            },
                        )
            elif item.get("type") == "reasoning":
                for summary_index, part in enumerate(item.get("summary", [])):
                    if part.get("type") == "summary_text":
                        await self.emit(
                            "reasoning_summary",
                            {
                                **base,
                                "summary_index": summary_index,
                                "text": part.get("text", ""),
                                "done": True,
                            },
                        )
            elif item.get("type") == "function_call":
                await self.emit(
                    "tool_arguments",
                    {
                        **base,
                        "phase": "done",
                        "arguments": json.dumps(item.get("arguments", {}), ensure_ascii=False),
                    },
                )
            await self.emit("output_item", {**metadata, "phase": "done"})
        await self.emit(
            "provider_phase",
            {
                "provider_api": "agents",
                "source": "session_snapshot",
                "reconciled": True,
                "session_id": self.session_id,
                "turn_id": self.turn_id,
                "phase": self._turn_status if session.status == "idle" else session.status,
                "provider_status": session.status,
                "provider_turn_status": self._turn_status,
            },
        )

    async def _account_boundary(self, session: Any, boundary_id: str, started: float) -> Usage:
        usage = dump(getattr(session, "usage", None)) or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cached_tokens = (usage.get("input_tokens_details") or {}).get("cached_tokens")
        cumulative_estimate = None
        estimate_delta = None
        estimate_unavailable_reason: str | None = "missing_or_invalid_token_usage"
        if (
            type(input_tokens) is int
            and input_tokens >= 0
            and type(output_tokens) is int
            and output_tokens >= 0
        ):
            threshold = model_capabilities(self.settings.model)["long_context_threshold"]
            if threshold and input_tokens > threshold:
                # Session/turn totals may combine several model generations.
                # They cannot establish which requests incurred long-context prices.
                estimate_unavailable_reason = "insufficient_generation_granularity"
            elif cached_tokens is None or (
                type(cached_tokens) is int and 0 <= cached_tokens <= input_tokens
            ):
                cumulative_estimate = estimate_token_cost(
                    self.settings.model,
                    input_tokens,
                    output_tokens,
                    cached_tokens or 0,
                    service_tier=getattr(session.agent, "service_tier", "default"),
                )
                estimate_unavailable_reason = None
        if cumulative_estimate is not None:
            estimate_delta = max(0.0, cumulative_estimate - self._estimated_session_cost_usd)
            # Late cache details can reduce a best-effort cumulative estimate.
            # Keep the accounted high-water mark so its later recovery is not counted twice.
            self._estimated_session_cost_usd = max(
                self._estimated_session_cost_usd, cumulative_estimate
            )
        reservation = (
            self._session_limit_cents / 100
            if self._session_limit_cents is not None and not self._accounted_cap
            else 0.0
        )
        cost = estimate_delta if self.unbounded else reservation
        record = {
            "provider_api": "agents",
            "provider_session_id": self.session_id,
            "provider_turn_id": self.turn_id,
            "boundary_id": boundary_id,
            "model": session.agent.model,
            "requested_model": self.settings.model,
            "provider_status": session.status,
            "provider_terminal": session.status == "idle",
            "usage_complete": False,
            "usage_is_best_effort": True,
            "usage_scope": "session_cumulative",
            "cost_basis": "best_effort_token_estimate"
            if self.unbounded
            else "managed_session_spend_limit",
            "cost_usd": cost,
            "estimated_token_cost_usd": estimate_delta,
            "estimated_session_token_cost_usd": cumulative_estimate,
            "estimated_cost_unavailable_reason": estimate_unavailable_reason,
            "reservation_usd": reservation,
            "cost_is_reservation": not self.unbounded,
            "actual_cost_usd": None,
            "billing_settled": False,
            "session_spend_limit_cents": self._session_limit_cents,
            "session_consumed_cents_best_effort": getattr(
                getattr(session, "spend_control", None), "consumed", None
            ),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": None,
            "elapsed_seconds": time.monotonic() - started,
        }
        self.usage.append(record)
        if self.unbounded and estimate_delta is not None:
            self.budget["cost_usd"] += estimate_delta
        if self.unbounded and estimate_delta is None:
            self.budget["managed_cost_unknown"] = True
        await self.emit("usage", record)
        if not self._accounted_cap:
            self.budget["accounted_session_ids"].append(self.session_id)
        self._accounted_cap = True
        self.budget["unresolved_generation_requests"] -= 1
        # SDK requires integer token counts; unknown public observations stay null.
        # Retain each high-water mark across downward best-effort revisions so
        # a later recovery cannot count already observed tokens a second time.
        token_deltas = {}
        for field, observed in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
            prior = self._accounted_tokens[field]
            cumulative = max(prior, observed) if type(observed) is int and observed >= 0 else prior
            token_deltas[field] = cumulative - prior
            self._accounted_tokens[field] = cumulative
        input_delta, output_delta = token_deltas["input_tokens"], token_deltas["output_tokens"]
        return Usage(
            requests=1,
            input_tokens=input_delta,
            output_tokens=output_delta,
            total_tokens=input_delta + output_delta,
        )

    async def _cancel_managed(self) -> None:
        # Close admission before waiting for an already-dispatched message. A
        # repeated Stop must not cancel cleanup and let that message start a
        # turn after the provider was last observed idle.
        self._cancel_requested = True
        self._accepts_live_steering = False
        self._steering_root_turn_id = None
        if self._cancel_task is None:
            self._cancel_task = asyncio.create_task(self._cancel_after_steering())
        await asyncio.shield(self._cancel_task)

    async def _cancel_after_steering(self) -> None:
        payload = {
            "provider_api": "agents",
            "session_id": self.session_id,
            "attempted": bool(self.session_id),
            "terminal_confirmed": False,
            "usage_accounted": False,
            "reservation_retained": self._session_limit_cents is not None,
        }
        if self.session_id:
            # Transport timeouts bound the sender's HTTP exchange. Keep this
            # lock wait outside the cancellation exchange timeout so a late
            # successful message is always followed by cancellation/readback.
            async with self._steering_lock:
                unresolved = any(
                    entry["state"] == "unresolved" for entry in self._live_submissions.values()
                )
                payload["steering_admission_unresolved"] = unresolved
                try:
                    async with asyncio.timeout(10.0):
                        await self.sessions.events.create(
                            self.session_id,
                            events=[{"type": "agent.session.input.cancel"}],
                            idempotency_key=str(uuid4()),
                        )
                        session = await self._settled_session()
                        payload.update(
                            status=session.status,
                            terminal_observed=session.status in {"idle", "failed"},
                            terminal_confirmed=session.status in {"idle", "failed"}
                            and not unresolved,
                        )
                except Exception as error:
                    payload["error_type"] = type(error).__name__
        with suppress(Exception):
            await self.emit("provider_cancel", payload)

    async def _get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **kwargs,
    ):
        require_application_model(self.settings.model)
        self._accepts_live_steering = False
        timeout = self.settings.run_limit("run_timeout_seconds")
        self._recovery_deadline = time.monotonic() + timeout if timeout is not None else None
        agent = self._agent_configuration(system_instructions, tools, output_schema, handoffs)
        signature = json.dumps(agent, sort_keys=True)
        if self._configuration is not None and signature != self._configuration:
            raise ResponseProtocolError("Managed session configuration changed during a turn")
        items = await self._input(input)
        continuing = self.session_id is not None
        events = self._continuation(items) if continuing else None
        if not continuing and not self.unbounded:
            remaining = self.settings.max_cost_usd_per_run - self.budget["cost_usd"]
            requested = (
                remaining
                if self.session_budget_usd is None
                else min(self.session_budget_usd, remaining)
            )
            cents = math.floor(round(requested * 100, 8))
            if cents < 1:
                raise BudgetExceeded("Remaining budget cannot fund a finite managed session")
            self._session_limit_cents = cents
        self._boundary += 1
        boundary_id = str(uuid4())
        started = time.monotonic()
        stream = None
        recovered = False
        self.budget["unresolved_generation_requests"] += 1
        if not continuing and self._session_limit_cents is not None:
            self.budget["cost_usd"] += self._session_limit_cents / 100
            self.budget["managed_reserved_cost_usd"] += self._session_limit_cents / 100
        await self.emit(
            "model_started",
            {
                "provider_api": "agents",
                "boundary_id": boundary_id,
                "session_id": self.session_id,
                "model": self.settings.model,
                "reasoning": agent["reasoning"],
                "service_tier": agent["service_tier"],
                "background": True,
                "reserved_cost_usd": self._session_limit_cents / 100
                if self._session_limit_cents is not None and not continuing
                else 0,
                "cost_basis": "best_effort_token_estimate"
                if self.unbounded
                else "managed_session_spend_limit",
                "cost_is_reservation": not self.unbounded,
                "unbounded": self.unbounded,
            },
        )
        try:
            if continuing:
                # Attach the observer first so a fast tool continuation cannot
                # finish between input admission and event-stream connection.
                stream = await self.sessions.events.stream(self.session_id)
                await self.sessions.events.create(
                    self.session_id, events=events, idempotency_key=boundary_id
                )
                self._submitted_calls.update(self._pending_calls)
                self._pending_calls = {}
            else:
                self._configuration = signature
                create_args = dict(
                    environment={"type": "none"},
                    agent=agent,
                    input=self._messages(items),
                    stream=True,
                    extra_headers={"Idempotency-Key": boundary_id},
                )
                if self._session_limit_cents is not None:
                    create_args["spend_control"] = {"limit": self._session_limit_cents}
                stream = await self.sessions.create(**create_args)
            try:
                recovered = not await self._observe(stream)
                self._accepts_live_steering = False
            except (ResponseProtocolError, asyncio.CancelledError):
                raise
            except Exception as error:
                self._accepts_live_steering = False
                self._steering_root_turn_id = None
                if not self.session_id:
                    raise
                recovered = True
                await self.emit(
                    "transport_recovery",
                    {
                        "provider_api": "agents",
                        "session_id": self.session_id,
                        "mode": "reconnect_existing_session",
                        "error_type": type(error).__name__,
                    },
                )
            if recovered:
                self._steering_root_turn_id = None
                close = getattr(stream, "close", None)
                if callable(close):
                    with suppress(Exception):
                        result = close()
                        if inspect.isawaitable(result):
                            await result
                await self._recover_stream()
            # Fence message admission against the durable final snapshot. A
            # sender already inside this lock finishes before settlement; later
            # details are consumed at the next Runner boundary in this run.
            async with self._steering_lock:
                self._accepts_live_steering = False
                session = await self._settled_session()
                records = await self._snapshot_items(session)
                recovered = recovered or bool(self._live_inputs)
                items += self._live_inputs
                self._live_inputs = []
            if session.status == "failed":
                if "misalignment_policy_violation" in str(session.error):
                    raise ProviderStopped("Provider stopped this managed session")
                raise ResponseProtocolError(f"Managed session failed: {session.error}")
            output: list[ResponseOutputItem] = []
            call_items = {}
            for item in records:
                if item.get("type") == "function_call":
                    call_id = item.get("call_id")
                    if item.get("turn_id") != self.turn_id:
                        if any(
                            action.call_id == call_id
                            for action in session.required_actions
                            if action.type == "function_call"
                        ):
                            raise ResponseProtocolError(
                                "Managed action item belongs to a different turn"
                            )
                        continue
                    if call_id in call_items:
                        raise ResponseProtocolError(
                            "Managed snapshot duplicated a function call ID"
                        )
                    call_items[call_id] = item
            records = [item for item in records if item.get("turn_id") == self.turn_id]
            await self._reconcile_reasoning_aliases(records)
            allowed = {tool["name"] for tool in agent["tools"]}
            for action in session.required_actions:
                call = dump(action)
                if call.get("type") != "function_call" or call.get("name") not in allowed:
                    raise ResponseProtocolError("Managed session requested an unconfigured action")
                if call.get("turn_id") != self.turn_id:
                    raise ResponseProtocolError(
                        "Managed required action belongs to a different turn"
                    )
                call_id = call["call_id"]
                if call_id in self._submitted_calls or call_id in self._pending_calls:
                    raise ResponseProtocolError(
                        "Managed session reused a submitted function call ID"
                    )
                self._pending_calls[call_id] = call
                arguments = call.get("arguments")
                if not isinstance(arguments, dict):
                    raise ResponseProtocolError("Managed function arguments must be a JSON object")
                provider_item = call_items.get(call_id, {})
                output.append(
                    ResponseFunctionToolCall(
                        id=provider_item.get("id", call_id),
                        call_id=call_id,
                        name=call["name"],
                        arguments=json.dumps(arguments, ensure_ascii=False),
                        type="function_call",
                        status="completed",
                    )
                )
                await self.emit(
                    "tool_arguments",
                    {
                        "provider_api": "agents",
                        "session_id": self.session_id,
                        "turn_id": call["turn_id"],
                        "item_id": provider_item.get("id", call_id),
                        "phase": "done",
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                )
            for item in records:
                if (
                    item.get("type") == "message"
                    and item.get("role") == "assistant"
                    and item.get("status") == "completed"
                ):
                    content: list[ResponseOutputText | ResponseOutputRefusal] = [
                        ResponseOutputText(type="output_text", text=part["text"], annotations=[])
                        for part in item.get("content", [])
                        if part.get("type") == "output_text"
                    ]
                    if content and (self._pending_calls or item.get("phase") != "commentary"):
                        output.append(
                            ResponseOutputMessage(
                                id=item["id"],
                                type="message",
                                role="assistant",
                                status="completed",
                                content=content,
                            )
                        )
                if item.get("id"):
                    self._seen_items.add(item["id"])
            if not self._pending_calls and not output:
                raise ResponseProtocolError(
                    "Managed session reached idle without a final assistant message"
                )
            if recovered:
                await self._reconcile_snapshot(session, records)
            usage = await self._account_boundary(session, boundary_id, started)
            self.history = items + dump(output)
            self._caller_history = self._current_caller_input + dump(output)
            return ModelResponse(output=output, usage=usage, response_id=None)
        except asyncio.CancelledError:
            await self._cancel_managed()
            raise
        except BadRequestError as error:
            if (
                not continuing
                and self.session_id is None
                and error.code
                in {
                    "unknown_parameter",
                    "invalid_value",
                    "invalid_type",
                    "missing_required_parameter",
                    "invalid_json_schema",
                }
                and isinstance(error.param, str)
                and bool(error.param)
            ):
                # A structured HTTP 400 parameter rejection cannot have admitted
                # this create. Transport errors and unclassified failures remain
                # unresolved; no altered request or automatic fallback is sent.
                if self._session_limit_cents is not None:
                    self.budget["cost_usd"] -= self._session_limit_cents / 100
                    self.budget["managed_reserved_cost_usd"] -= self._session_limit_cents / 100
                self.budget["unresolved_generation_requests"] -= 1
                self.budget["preflight_failure"] = {
                    "phase": "managed_session_request_validation",
                    "generation_dispatched": False,
                    "unresolved_generation_requests": self.budget["unresolved_generation_requests"],
                    "accounted_response_ids": list(self.budget.get("accounted_response_ids", [])),
                    "accounted_session_ids": list(self.budget["accounted_session_ids"]),
                    "cost_usd": self.budget["cost_usd"],
                }
                await self.emit(
                    "managed_session_rejected",
                    {
                        "provider_api": "agents",
                        "boundary_id": boundary_id,
                        "code": error.code,
                        "param": error.param,
                        "request_id": error.request_id,
                        "generation_dispatched": False,
                        "fallback_dispatched": False,
                    },
                )
            else:
                await self._cancel_managed()
            raise
        except BaseException:
            # A protocol failure must not leave a still-running managed harness
            # spending unattended. Cancellation admission alone is not completion.
            await self._cancel_managed()
            raise
        finally:
            self._accepts_live_steering = False
            self.pending_session_id = None
            close = getattr(stream, "close", None)
            if callable(close):
                with suppress(Exception):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
