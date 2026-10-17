"""Managed harness boundary tests; no provider/network calls."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
from agents import Agent, ModelSettings, Runner, function_tool, set_tracing_disabled
from agents.models.interface import ModelTracing
from analyst.config import Settings
from analyst.managed_agents import ManagedAgentsModel, managed_trace
from analyst.model import BudgetExceeded, ResponseProtocolError
from openai import BadRequestError
from openai.types.beta.agent_session_error_event import AgentSessionErrorEvent


class Node(SimpleNamespace):
    def model_dump(self, **kwargs):
        def plain(value):
            if isinstance(value, Node):
                return {key: plain(item) for key, item in vars(value).items()}
            if isinstance(value, list):
                return [plain(item) for item in value]
            return value

        return plain(self)


def session(*, status="idle", actions=(), model="gpt-6-luna", limit=30):
    return Node(
        id="sess_real",
        status=status,
        agent=Node(model=model, multi_agent=Node(enabled=False)),
        environment=Node(type="none"),
        required_actions=list(actions),
        spend_control=Node(limit=limit, consumed=0),
        error=None,
        usage=Node(
            input_tokens=12,
            output_tokens=4,
            total_tokens=16,
            input_tokens_details=Node(cached_tokens=0),
        ),
    )


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def close(self):
        self.closed = True


class Page:
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        for item in self.data:
            yield item


class Sessions:
    def __init__(self, snapshots, pages, streams):
        self.snapshots = list(snapshots)
        self.pages = list(pages)
        self.streams = list(streams)
        self.creates = []
        self.inputs = []
        self.order = []
        self.events = SimpleNamespace(create=self.add_input, stream=self.open_stream)
        self.items = SimpleNamespace(list=self.list_items)
        self.turns = SimpleNamespace(list=self.list_turns)
        self.last_snapshot = None

    async def create(self, **kwargs):
        self.creates.append(kwargs)
        return self.streams.pop(0)

    async def retrieve(self, session_id):
        self.last_snapshot = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        return self.last_snapshot

    async def list_turns(self, session_id, **kwargs):
        return Page(
            [
                Node(
                    id="turn_real",
                    session_id="sess_real",
                    status="waiting"
                    if self.last_snapshot.status == "requires_action"
                    else "completed",
                )
            ]
        )

    async def list_items(self, session_id, **kwargs):
        return Page(self.pages.pop(0) if len(self.pages) > 1 else self.pages[0])

    async def add_input(self, session_id, **kwargs):
        self.order.append("input")
        self.inputs.append(kwargs)

    async def open_stream(self, session_id):
        self.order.append("stream")
        return self.streams.pop(0)


def event(kind, **kwargs):
    return Node(type=kind, event_id=kind + str(kwargs.get("turn_id", "")), **kwargs)


def message(text="Verified result"):
    return Node(
        id="msg_real",
        turn_id="turn_real",
        type="message",
        role="assistant",
        status="completed",
        phase="final_answer",
        content=[Node(type="output_text", text=text)],
    )


def model_for(sessions, *, budget=None, cap=0.30):
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    model = ManagedAgentsModel(
        Settings(api_key="test", max_cost_usd_per_run=1, unbounded_runs=False),
        emit,
        client=SimpleNamespace(beta=SimpleNamespace(agents=SimpleNamespace(sessions=sessions))),
        budget=budget,
        session_budget_usd=cap,
    )
    return model, events


async def request(model, input="Inspect current scope", tools=()):
    return await model.get_response(
        "Rules", input, ModelSettings(), list(tools), None, [], ModelTracing.DISABLED
    )


@function_tool
def inspect_scope(limit: int) -> str:
    """Inspect the frozen scope."""
    return str(limit)


def test_required_actions_round_trip_to_runner_without_replaying_context():
    action = Node(
        type="function_call",
        call_id="call_real",
        turn_id="turn_real",
        name="inspect_scope",
        arguments={"limit": 2},
    )
    waiting = session(status="requires_action", actions=[action])
    final = session()
    created = event("agent.session.created", session=waiting)
    stopped = event("agent.session.requires_action", session=waiting)
    terminal = [
        event("agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"),
        event("agent.session.idle", session=final),
    ]
    sessions = Sessions(
        [waiting, final],
        [[Node(id="fc_real", **vars(action))], [message()]],
        [Stream([created, stopped]), Stream(terminal)],
    )
    model, events = model_for(sessions)

    async def run():
        first = await request(model, tools=[inspect_scope])
        assert first.output[0].call_id == "call_real"
        assert first.response_id is None
        history = model.history + [
            {"type": "function_call_output", "call_id": "call_real", "output": "2"}
        ]
        second = await request(model, input=history, tools=[inspect_scope])
        assert second.output[0].content[0].text == "Verified result"

    asyncio.run(run())
    assert len(sessions.creates) == 1
    assert sessions.creates[0]["spend_control"] == {"limit": 30}
    assert sessions.creates[0]["agent"]["model"] == "gpt-6-luna"
    assert sessions.creates[0]["agent"]["multi_agent"] == {"enabled": False}
    assert sessions.creates[0]["environment"] == {"type": "none"}
    assert sessions.order == ["stream", "input"]
    assert sessions.inputs[0]["events"] == [
        {
            "type": "agent.session.input.tool_result",
            "call_id": "call_real",
            "turn_id": "turn_real",
            "success": True,
            "output": "2",
        }
    ]
    assert model.budget["cost_usd"] == pytest.approx(0.30)
    assert [row["cost_usd"] for row in model.usage] == [0.30, 0]
    assert model.budget["unresolved_generation_requests"] == 0
    assert all(row["usage_complete"] is False for row in model.usage)
    assert all("response_id" not in row for row in model.usage)
    assert any(kind == "managed_session_created" for kind, _ in events)


def reasoning_item(identity, *, turn="turn_real", status="completed", summary=None):
    return Node(
        id=identity,
        turn_id=turn,
        type="reasoning",
        status=status,
        summary=[
            Node(type="summary_text", text=text)
            for text in (
                summary
                if summary is not None
                else ["Public first", "Public second", "Public third"]
            )
        ],
    )


def reasoning_events(item, prefix):
    observed = [
        event(
            "agent.session.turn.item.added",
            session_id="sess_real",
            turn_id=item.turn_id,
            item=item,
        ),
    ]
    observed.extend(
        event(
            "agent.session.turn.reasoning_summary_text.done",
            session_id="sess_real",
            turn_id=item.turn_id,
            item_id=item.id,
            summary_index=index,
            text=part.text,
        )
        for index, part in enumerate(item.summary)
    )
    for index, observed_event in enumerate(observed):
        observed_event.event_id = f"{prefix}_{index}"
    return observed


def test_continuation_reasoning_alias_keeps_unique_durable_ui_identity():
    action = Node(
        type="function_call",
        call_id="call_real",
        turn_id="turn_real",
        name="inspect_scope",
        arguments={"limit": 2},
    )
    waiting, final = session(status="requires_action", actions=[action]), session()
    canonical, alias = reasoning_item("rs_original"), reasoning_item("item_replay")
    alias.status = "in_progress"
    original_events = reasoning_events(canonical, "original")
    replay_events = reasoning_events(alias, "replay")
    sessions = Sessions(
        [waiting, final],
        [[canonical, Node(id="fc_real", **vars(action))], [canonical, message()]],
        [
            Stream(
                [
                    event("agent.session.created", session=waiting),
                    *original_events,
                    event("agent.session.requires_action", session=waiting),
                ]
            ),
            Stream(
                [
                    *replay_events,
                    event(
                        "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
                    ),
                    event("agent.session.idle", session=final),
                ]
            ),
        ],
    )
    model, observed = model_for(sessions)

    async def run():
        await request(model, tools=[inspect_scope])
        await request(
            model,
            model.history
            + [{"type": "function_call_output", "call_id": "call_real", "output": "2"}],
            [inspect_scope],
        )

    asyncio.run(run())
    public = [payload for kind, payload in observed if kind in {"output_item", "reasoning_summary"}]
    assert {payload["item_id"] for payload in public} == {"rs_original", "item_replay"}
    aliases = [payload for kind, payload in observed if kind == "reasoning_alias"]
    assert len(aliases) == 1
    assert aliases[0] == {
        "provider_api": "agents",
        "source": "session_snapshot",
        "reconciled": True,
        "session_id": "sess_real",
        "turn_id": "turn_real",
        "item_id": "item_replay",
        "canonical_item_id": "rs_original",
    }
    assert alias.id == "item_replay", "Source provider objects remain unchanged"
    assert len(sessions.creates) == 1 and len(sessions.inputs) == 1
    assert sessions.inputs[0]["events"][0]["type"] == "agent.session.input.tool_result"
    assert model.budget["unresolved_generation_requests"] == 0


@pytest.mark.parametrize(
    "case", ["persisted_repeat", "new_text", "other_turn", "ambiguous", "partial_summary"]
)
def test_reasoning_alias_does_not_merge_distinct_or_ambiguous_output(case):
    model, observed = model_for(Sessions([], [], []))
    model.session_id = "sess_real"
    model.turn_id = "turn_real"
    canonical = reasoning_item("rs_original")
    model._remember_reasoning_items([canonical.model_dump()])
    incoming = reasoning_item("rs_new")
    records = [canonical.model_dump()]
    if case == "persisted_repeat":
        records.append(incoming.model_dump())
    elif case == "new_text":
        incoming.summary[0].text = "Genuinely different public reasoning"
    elif case == "other_turn":
        incoming.turn_id = "turn_other"
    elif case == "ambiguous":
        records.append(reasoning_item("rs_other_original").model_dump())
    else:
        incoming.summary.pop()

    async def run():
        await model._observe(Stream(reasoning_events(incoming, case)))
        await model._reconcile_reasoning_aliases(records)

    asyncio.run(run())
    assert {payload["item_id"] for _, payload in observed} == {"rs_new"}
    assert not any(kind == "reasoning_alias" for kind, _ in observed)


def test_uncertain_create_does_not_retry_and_retains_budget():
    sessions = Sessions([], [], [Stream([ConnectionError("lost")])])
    model, events = model_for(sessions)
    with pytest.raises(ConnectionError):
        asyncio.run(request(model))
    assert len(sessions.creates) == 1
    assert model.budget["blocked"] is True
    assert model.budget["unresolved_generation_requests"] == 1
    assert model.budget["cost_usd"] == 0.30
    assert not model.usage
    assert not sessions.inputs


def test_unexpected_model_rejects_output_and_cancels_same_session():
    wrong = session(model="gpt-6-sol")
    sessions = Sessions([wrong], [], [Stream([event("agent.session.created", session=wrong)])])
    model, events = model_for(sessions)
    with pytest.raises(ResponseProtocolError, match="different model"):
        asyncio.run(request(model))
    assert not model.usage
    assert sessions.inputs[0]["events"] == [{"type": "agent.session.input.cancel"}]
    assert not any(kind == "text_done" for kind, _ in events)


def test_missing_pending_result_is_rejected_before_provider_write():
    waiting = session(
        status="requires_action",
        actions=[
            Node(
                type="function_call",
                call_id="call_real",
                turn_id="turn_real",
                name="inspect_scope",
                arguments={"limit": 2},
            )
        ],
    )
    sessions = Sessions(
        [waiting],
        [[]],
        [
            Stream(
                [
                    event("agent.session.created", session=waiting),
                    event("agent.session.requires_action", session=waiting),
                ]
            )
        ],
    )
    model, _ = model_for(sessions)

    async def run():
        await request(model, tools=[inspect_scope])
        with pytest.raises(ResponseProtocolError, match="every pending"):
            await request(model, model.history, [inspect_scope])

    asyncio.run(run())
    assert not sessions.inputs
    assert len(sessions.creates) == 1


def test_no_fractional_cent_or_unlimited_dispatch():
    sessions = Sessions([], [], [])
    model, _ = model_for(sessions, budget={"cost_usd": 0.995})
    with pytest.raises(BudgetExceeded):
        asyncio.run(request(model))
    assert not sessions.creates


def test_reasoning_mapping_only_exposes_documented_summary():
    raw = event(
        "agent.session.turn.reasoning_summary_text.done",
        session_id="sess_real",
        item_id="reason_real",
        text="Checking scoped evidence",
        encrypted_content="SECRET",
        reasoning="PRIVATE",
    )
    kind, payload = managed_trace(raw)
    assert kind == "reasoning_summary"
    assert payload["text"] == "Checking scoped evidence"
    assert "SECRET" not in str(payload) and "PRIVATE" not in str(payload)
    assert managed_trace(event("agent.session.turn.reasoning.delta", delta="PRIVATE")) is None


def test_actual_sdk_runner_executes_local_function_and_continues_managed_session():
    set_tracing_disabled(True)
    action = Node(
        type="function_call",
        call_id="call_real",
        turn_id="turn_real",
        name="inspect_scope",
        arguments={"limit": 2},
    )
    waiting = session(status="requires_action", actions=[action])
    final = session()
    sessions = Sessions(
        [waiting, final],
        [[Node(id="fc_real", **vars(action))], [message()]],
        [
            Stream(
                [
                    event("agent.session.created", session=waiting),
                    event("agent.session.requires_action", session=waiting),
                ]
            ),
            Stream(
                [
                    event(
                        "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
                    ),
                    event("agent.session.idle", session=final),
                ]
            ),
        ],
    )
    model, _ = model_for(sessions)
    agent = Agent(
        name="Local evidence owner", instructions="Rules", model=model, tools=[inspect_scope]
    )
    result = asyncio.run(Runner.run(agent, "Inspect current scope"))
    assert result.final_output == "Verified result"
    assert len(sessions.creates) == 1
    assert sessions.inputs[0]["events"][0]["output"] == "2"


def test_capped_create_validation_rejection_never_removes_guardrail_or_falls_back():
    class RejectedSessions(Sessions):
        async def create(self, **kwargs):
            self.creates.append(kwargs)
            response = httpx2.Response(
                400, request=httpx2.Request("POST", "https://api.openai.com/v1/agents/sessions")
            )
            raise BadRequestError(
                "Unknown parameter: spend_control",
                response=response,
                body={"code": "unknown_parameter", "param": "spend_control"},
            )

    sessions = RejectedSessions([], [], [])
    model, events = model_for(sessions)
    with pytest.raises(BadRequestError):
        asyncio.run(request(model))
    assert len(sessions.creates) == 1
    assert sessions.creates[0]["spend_control"] == {"limit": 30}
    assert model.session_id is None
    assert not sessions.inputs
    assert not model.usage
    assert model.budget["cost_usd"] == 0
    assert model.budget["unresolved_generation_requests"] == 0
    assert model.budget["preflight_failure"]["generation_dispatched"] is False
    assert any(
        kind == "managed_session_rejected" and data["fallback_dispatched"] is False
        for kind, data in events
    )


def test_old_turn_message_cannot_satisfy_current_turn_completion():
    final = session()
    stale = message("Stale result from another turn")
    stale.turn_id = "turn_old"
    sessions = Sessions(
        [final],
        [[stale]],
        [
            Stream(
                [
                    event("agent.session.created", session=final),
                    event(
                        "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
                    ),
                    event("agent.session.idle", session=final),
                ]
            )
        ],
    )
    model, events = model_for(sessions)
    with pytest.raises(ResponseProtocolError, match="without a final assistant"):
        asyncio.run(request(model))
    assert not model.history and not model.usage
    assert not any("Stale result" in str(payload) for _, payload in events)


def test_pending_action_cannot_borrow_function_item_from_old_turn():
    action = Node(
        type="function_call",
        call_id="call_real",
        turn_id="turn_real",
        name="inspect_scope",
        arguments={"limit": 2},
    )
    stale = Node(id="fc_old", **vars(action))
    stale.turn_id = "turn_old"
    waiting = session(status="requires_action", actions=[action])
    sessions = Sessions(
        [waiting],
        [[stale]],
        [
            Stream(
                [
                    event("agent.session.created", session=waiting),
                    event("agent.session.requires_action", session=waiting),
                ]
            )
        ],
    )
    model, events = model_for(sessions)
    with pytest.raises(ResponseProtocolError, match="action item belongs to a different turn"):
        asyncio.run(request(model, tools=[inspect_scope]))
    assert not model.history and not model.usage
    assert not any(kind == "tool_arguments" for kind, _ in events)


@pytest.mark.parametrize("disconnect", [True, False])
def test_recovery_reconciles_complete_public_current_turn_snapshot(disconnect):
    final = session()
    stale = message("Old turn text")
    stale.id, stale.turn_id = "msg_old", "turn_old"
    reasoning = Node(
        id="reason_real",
        type="reasoning",
        turn_id="turn_real",
        status="completed",
        summary=[Node(type="summary_text", text="Public summary")],
        encrypted_content="PRIVATE",
    )
    stream_events = [
        event("agent.session.created", session=final),
        event(
            "agent.session.turn.output_text.delta",
            session_id="sess_real",
            turn_id="turn_real",
            item_id="msg_real",
            output_index=0,
            content_index=0,
            delta="Verified ",
        ),
    ]
    if disconnect:
        stream_events.append(ConnectionError("lost connection"))
    sessions = Sessions(
        [final], [[stale, reasoning, message()]], [Stream(stream_events), Stream([])]
    )
    model, events = model_for(sessions)
    result = asyncio.run(request(model))
    assert result.output[0].content[0].text == "Verified result"
    restored = [
        (kind, payload) for kind, payload in events if payload.get("source") == "session_snapshot"
    ]
    assert any(
        kind == "text_done"
        and payload["text"] == "Verified result"
        and payload["turn_id"] == "turn_real"
        for kind, payload in restored
    )
    assert any(
        kind == "reasoning_summary" and payload["text"] == "Public summary" and payload["done"]
        for kind, payload in restored
    )
    assert any(
        kind == "provider_phase"
        and payload["phase"] == "completed"
        and payload["provider_status"] == "idle"
        for kind, payload in restored
    )
    assert all(payload["reconciled"] for _, payload in restored)
    assert "Old turn text" not in str(events) and "PRIVATE" not in str(events)
    assert len(sessions.creates) == 1


def test_disconnect_reconnects_live_progress_and_discards_finalized_buffered_items():
    live, final = session(status="in_progress"), session()
    completed = message("Saved commentary")
    completed.id, completed.phase = "msg_saved", "commentary"

    def delta(item_id, text):
        value = event(
            "agent.session.turn.output_text.delta",
            session_id="sess_real",
            turn_id="turn_real",
            item_id=item_id,
            delta=text,
        )
        value.event_id = "evt_" + item_id
        return value

    reconnected = Stream(
        [
            delta("msg_saved", "duplicate saved text"),
            delta("msg_real", "Live after reconnect"),
            event("agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"),
            event("agent.session.idle", session=final),
        ]
    )
    sessions = Sessions(
        [live, final],
        [[completed], [completed, message()]],
        [
            Stream([event("agent.session.created", session=live), ConnectionError("lost")]),
            reconnected,
        ],
    )
    model, events = model_for(sessions)
    model.settings = replace(model.settings, unbounded_runs=True)
    live.spend_control = final.spend_control = None
    result = asyncio.run(request(model))
    assert result.output[0].content[0].text == "Verified result"
    assert any(
        kind == "text_delta" and row["delta"] == "Live after reconnect" for kind, row in events
    )
    assert "duplicate saved text" not in str(events)
    assert any(kind == "text_done" and row["text"] == "Saved commentary" for kind, row in events)
    assert len(sessions.creates) == 1 and not sessions.inputs
    assert reconnected.closed


def test_disconnect_after_tool_result_does_not_resubmit_admitted_input():
    action = Node(
        type="function_call",
        call_id="call_real",
        turn_id="turn_real",
        name="inspect_scope",
        arguments={"limit": 2},
    )
    waiting, final = session(status="requires_action", actions=[action]), session()
    sessions = Sessions(
        [waiting, final],
        [[Node(id="fc_real", **vars(action))], [message()]],
        [
            Stream(
                [
                    event("agent.session.created", session=waiting),
                    event("agent.session.requires_action", session=waiting),
                ]
            ),
            Stream([ConnectionError("lost after accepted tool result")]),
            Stream([]),
        ],
    )
    model, _ = model_for(sessions)

    async def run():
        await request(model, tools=[inspect_scope])
        return await request(
            model,
            model.history
            + [{"type": "function_call_output", "call_id": "call_real", "output": "2"}],
            [inspect_scope],
        )

    result = asyncio.run(run())
    assert result.output[0].content[0].text == "Verified result"
    assert len(sessions.creates) == 1 and len(sessions.inputs) == 1
    assert sessions.inputs[0]["events"][0]["call_id"] == "call_real"
    assert sessions.inputs[0]["idempotency_key"]


def test_cancel_during_reconnected_live_stream_stops_observer_and_provider():
    async def run():
        opened = asyncio.Event()

        class WaitingStream(Stream):
            async def __aiter__(self):
                opened.set()
                await asyncio.Event().wait()
                yield  # pragma: no cover

        live, final = session(status="in_progress"), session()
        live.spend_control = final.spend_control = None
        waiting_stream = WaitingStream([])
        sessions = Sessions(
            [live, final],
            [[]],
            [
                Stream([event("agent.session.created", session=live), ConnectionError("lost")]),
                waiting_stream,
            ],
        )
        model, events = model_for(sessions)
        model.settings = replace(model.settings, unbounded_runs=True)
        task = asyncio.create_task(request(model))
        await asyncio.wait_for(opened.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert waiting_stream.closed
        assert len(sessions.creates) == 1
        assert len(sessions.inputs) == 1
        assert sessions.inputs[0]["events"] == [{"type": "agent.session.input.cancel"}]
        assert any(kind == "provider_cancel" and row["terminal_confirmed"] for kind, row in events)

    asyncio.run(run())


def test_transient_snapshot_read_retries_same_session_without_replaying_input():
    class TransientSessions(Sessions):
        attempts = 0

        async def retrieve(self, session_id):
            self.attempts += 1
            if self.attempts == 1:
                raise httpx2.ReadTimeout("snapshot transport lost")
            return await super().retrieve(session_id)

    final = session()
    final.spend_control = None
    sessions = TransientSessions(
        [final],
        [[message()]],
        [
            Stream([event("agent.session.created", session=final), ConnectionError("lost")]),
            Stream([]),
        ],
    )
    model, _ = model_for(sessions)
    model.settings = replace(model.settings, unbounded_runs=True)
    assert asyncio.run(request(model)).output[0].content[0].text == "Verified result"
    assert sessions.attempts >= 2
    assert len(sessions.creates) == 1 and not sessions.inputs


def test_bounded_recovery_deadline_cancels_an_unresponsive_read():
    async def run():
        model, _ = model_for(Sessions([], [], []))
        model._recovery_deadline = time.monotonic() + 0.01
        cancelled = asyncio.Event()

        async def stalled_read():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with pytest.raises(TimeoutError, match="bounded run deadline"):
            await asyncio.wait_for(model._read_retry(stalled_read), timeout=1)
        assert cancelled.is_set()

    asyncio.run(run())


def test_actual_nested_provider_error_event_is_visible():
    raw = AgentSessionErrorEvent.model_validate(
        {
            "type": "error",
            "event_id": "evt_error",
            "session_id": "sess_real",
            "error": {
                "type": "server_error",
                "code": "execution_failed",
                "message": "Execution interrupted",
            },
        }
    )
    kind, payload = managed_trace(raw)
    assert kind == "provider_error"
    assert payload["provider_event"] == "error"
    assert payload["code"] == "execution_failed"
    assert payload["message"] == "Execution interrupted"
    assert payload["error_type"] == "server_error"


def test_managed_spend_policy_is_a_reservation_not_settled_billing():
    final = session()
    sessions = Sessions(
        [final],
        [[message()]],
        [
            Stream(
                [
                    event("agent.session.created", session=final),
                    event(
                        "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
                    ),
                    event("agent.session.idle", session=final),
                ]
            )
        ],
    )
    model, events = model_for(sessions)
    asyncio.run(request(model))
    usage = model.usage[0]
    assert usage["reservation_usd"] == 0.30 and usage["cost_is_reservation"]
    assert usage["actual_cost_usd"] is None and usage["billing_settled"] is False
    assert "cost_upper_bound_usd" not in usage and "cost_is_upper_bound" not in usage
    assert model.budget["managed_reserved_cost_usd"] == 0.30


@pytest.mark.parametrize("reported_usage", [True, False])
def test_unbounded_create_omits_spend_control_and_preserves_unknown_usage(reported_usage):
    final = session()
    final.spend_control = None
    if not reported_usage:
        final.usage = None
    sessions = Sessions(
        [final],
        [[message()]],
        [
            Stream(
                [
                    event("agent.session.created", session=final),
                    event(
                        "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
                    ),
                    event("agent.session.idle", session=final),
                ]
            )
        ],
    )
    model, events = model_for(sessions)
    model.settings = replace(model.settings, unbounded_runs=True)
    asyncio.run(request(model))
    assert "spend_control" not in sessions.creates[0]
    assert model.budget["managed_reserved_cost_usd"] == 0
    usage = model.usage[0]
    assert usage["reservation_usd"] == 0
    assert usage["cost_is_reservation"] is False
    assert usage["actual_cost_usd"] is None
    assert usage["billing_settled"] is False
    assert usage["provider_session_id"] == "sess_real"
    assert usage["provider_turn_id"] == "turn_real"
    if reported_usage:
        assert usage["estimated_token_cost_usd"] > 0
        assert model.budget["cost_usd"] == usage["estimated_token_cost_usd"]
    else:
        assert usage["input_tokens"] is None and usage["output_tokens"] is None
        assert usage["cost_usd"] is None
        assert usage["estimated_token_cost_usd"] is None
        assert model.budget["managed_cost_unknown"] is True


def test_unbounded_transport_loss_keeps_admission_unknown_without_fabricated_reservation():
    sessions = Sessions([], [], [Stream([ConnectionError("lost")])])
    model, events = model_for(sessions)
    model.settings = replace(model.settings, unbounded_runs=True)
    with pytest.raises(ConnectionError):
        asyncio.run(request(model))
    assert len(sessions.creates) == 1
    assert "spend_control" not in sessions.creates[0]
    assert model.budget["cost_usd"] == 0
    assert model.budget["unresolved_generation_requests"] == 1
    assert model.budget["managed_reserved_cost_usd"] == 0
    assert not model.usage


def test_unbounded_session_rejects_unexpected_provider_spending_limit():
    final = session()
    sessions = Sessions([final], [], [Stream([event("agent.session.created", session=final)])])
    model, _ = model_for(sessions)
    model.settings = replace(model.settings, unbounded_runs=True)
    with pytest.raises(ResponseProtocolError, match="returned a spending limit"):
        asyncio.run(request(model))
    assert len(sessions.creates) == 1
    assert sessions.inputs[0]["events"] == [{"type": "agent.session.input.cancel"}]


def test_agent_reasoning_omits_responses_only_reasoning_mode():
    model, _ = model_for(Sessions([], [], []))
    model.settings = replace(model.settings, reasoning_mode="auto")
    configuration = model._agent_configuration("Rules", [], None, [])
    assert set(configuration["reasoning"]) <= {"effort", "summary"}


def test_real_native_text_event_has_session_turn_and_event_identity():
    from openai.types.beta.agent_session_turn_output_text_delta_event import (
        AgentSessionTurnOutputTextDeltaEvent,
    )

    raw = AgentSessionTurnOutputTextDeltaEvent.model_validate(
        {
            "type": "agent.session.turn.output_text.delta",
            "event_id": "evt_native",
            "session_id": "sess_real",
            "turn_id": "turn_real",
            "item_id": "msg_real",
            "output_index": 0,
            "content_index": 0,
            "delta": "Reading scope",
        }
    )
    kind, payload = managed_trace(raw)
    assert kind == "text_delta"
    assert payload["event_id"] == "evt_native"
    assert payload["session_id"] == "sess_real"
    assert payload["turn_id"] == "turn_real"
    assert payload["delta"] == "Reading scope"


def test_real_native_summary_part_exposes_public_text_only():
    from openai.types.beta.agent_session_turn_reasoning_summary_part_done_event import (
        AgentSessionTurnReasoningSummaryPartDoneEvent,
    )

    raw = AgentSessionTurnReasoningSummaryPartDoneEvent.model_validate(
        {
            "type": "agent.session.turn.reasoning_summary_part.done",
            "event_id": "evt_summary",
            "session_id": "sess_real",
            "turn_id": "turn_real",
            "item_id": "reason_real",
            "output_index": 0,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": "Checking scoped records"},
        }
    )
    kind, payload = managed_trace(raw)
    assert kind == "reasoning_part"
    assert payload["text"] == "Checking scoped records" and payload["phase"] == "done"
    assert payload["summary_index"] == 0


def test_unbounded_cancellation_uses_existing_session_and_never_retries_creation():
    live = session(status="in_progress")
    live.spend_control = None
    settled = session()
    settled.spend_control = None
    sessions = Sessions(
        [settled],
        [],
        [
            Stream(
                [
                    event("agent.session.created", session=live),
                    asyncio.CancelledError(),
                ]
            )
        ],
    )
    model, events = model_for(sessions)
    model.settings = replace(model.settings, unbounded_runs=True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(request(model))
    assert len(sessions.creates) == 1
    assert "spend_control" not in sessions.creates[0]
    assert sessions.inputs[0]["events"] == [{"type": "agent.session.input.cancel"}]
    cancelled = [payload for kind, payload in events if kind == "provider_cancel"]
    assert cancelled[0]["terminal_confirmed"] is True
    assert cancelled[0]["reservation_retained"] is False
    assert model.budget["unresolved_generation_requests"] == 1


def test_unbounded_usage_estimates_only_new_cumulative_tokens_at_continuation():
    action = Node(
        type="function_call",
        call_id="call_real",
        turn_id="turn_real",
        name="inspect_scope",
        arguments={"limit": 2},
    )
    waiting = session(status="requires_action", actions=[action])
    final = session()
    waiting.spend_control = final.spend_control = None
    final.usage.input_tokens = 20
    final.usage.output_tokens = 9
    sessions = Sessions(
        [waiting, final],
        [[Node(id="fc_real", **vars(action))], [message()]],
        [
            Stream(
                [
                    event("agent.session.created", session=waiting),
                    event("agent.session.requires_action", session=waiting),
                ]
            ),
            Stream(
                [
                    event(
                        "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
                    ),
                    event("agent.session.idle", session=final),
                ]
            ),
        ],
    )
    model, _ = model_for(sessions)
    model.settings = replace(model.settings, unbounded_runs=True)

    async def run():
        first = await request(model, tools=[inspect_scope])
        second = await request(
            model,
            model.history
            + [{"type": "function_call_output", "call_id": "call_real", "output": "2"}],
            [inspect_scope],
        )
        assert first.usage.input_tokens + second.usage.input_tokens == 20
        assert first.usage.output_tokens + second.usage.output_tokens == 9

    asyncio.run(run())
    assert model.budget["cost_usd"] == pytest.approx(
        model.usage[-1]["estimated_session_token_cost_usd"]
    )
    assert sum(row["estimated_token_cost_usd"] for row in model.usage) == pytest.approx(
        model.budget["cost_usd"]
    )
    assert model.budget["managed_reserved_cost_usd"] == 0


@pytest.mark.parametrize("output_model_name", ["ReportDraft", "QAReview"])
def test_native_structured_output_preserves_full_application_schema_without_responses_fields(
    output_model_name,
):
    from agents import AgentOutputSchema
    from analyst.runtime import QAReview, ReportDraft
    from openai.lib.beta.agents._output import agent_text_format
    from openai.types.beta.text_format_param import TextFormatParamJSONSchema

    output_model = {"ReportDraft": ReportDraft, "QAReview": QAReview}[output_model_name]
    schema = AgentOutputSchema(output_model)
    model, _ = model_for(Sessions([], [], []))
    configuration = model._agent_configuration("Rules", [], schema, [])
    native_format = configuration["text"]["format"]
    assert set(native_format) == set(TextFormatParamJSONSchema.__annotations__)
    assert native_format == {"type": "json_schema", "schema": schema.json_schema()}
    assert native_format == agent_text_format(output_model)
    assert native_format["schema"]["additionalProperties"] is False
    assert "name" not in native_format and "strict" not in native_format


def test_unbounded_native_parameter_rejection_records_no_admission_without_retry():
    class RejectedSessions(Sessions):
        async def create(self, **kwargs):
            self.creates.append(kwargs)
            response = httpx2.Response(
                400, request=httpx2.Request("POST", "https://api.openai.com/v1/agents/sessions")
            )
            raise BadRequestError(
                "Unknown parameter: agent.text.format.name",
                response=response,
                body={"code": "unknown_parameter", "param": "agent.text.format.name"},
            )

    sessions = RejectedSessions([], [], [])
    model, events = model_for(sessions)
    model.settings = replace(model.settings, unbounded_runs=True)
    with pytest.raises(BadRequestError):
        asyncio.run(request(model))
    assert len(sessions.creates) == 1
    assert "spend_control" not in sessions.creates[0]
    assert model.session_id is None and not model.usage
    assert model.budget["unresolved_generation_requests"] == 0
    assert model.budget["cost_usd"] == 0
    assert model.budget["preflight_failure"]["generation_dispatched"] is False
    assert any(
        kind == "managed_session_rejected"
        and data["generation_dispatched"] is False
        and data["fallback_dispatched"] is False
        for kind, data in events
    )


@pytest.mark.parametrize("output_model_name", ["ReportDraft", "QAReview"])
def test_sdk_runner_returns_validated_native_application_output(output_model_name):
    from analyst.runtime import QAReview, ReportDraft

    output_model = {"ReportDraft": ReportDraft, "QAReview": QAReview}[output_model_name]
    typed_output = (
        ReportDraft(report="Verified analysis", chart_paths=[])
        if output_model_name == "ReportDraft"
        else QAReview(verified=True, errors=[], suggestions=[])
    )
    final = session()
    sessions = Sessions(
        [final],
        [[message(typed_output.model_dump_json())]],
        [
            Stream(
                [
                    event("agent.session.created", session=final),
                    event(
                        "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
                    ),
                    event("agent.session.idle", session=final),
                ]
            )
        ],
    )
    model, _ = model_for(sessions)
    agent = Agent(
        name="Native structured output", instructions="Rules", model=model, output_type=output_model
    )
    result = asyncio.run(Runner.run(agent, "Inspect current scope"))
    assert isinstance(result.final_output, output_model)
    assert result.final_output == typed_output
    assert set(sessions.creates[0]["agent"]["text"]["format"]) == {"type", "schema"}


def test_unclassified_http400_keeps_native_admission_unresolved():
    class RejectedSessions(Sessions):
        async def create(self, **kwargs):
            self.creates.append(kwargs)
            response = httpx2.Response(
                400, request=httpx2.Request("POST", "https://api.openai.com/v1/agents/sessions")
            )
            raise BadRequestError(
                "Unclassified failure",
                response=response,
                body={"code": "execution_failed", "param": None},
            )

    sessions = RejectedSessions([], [], [])
    model, events = model_for(sessions)
    model.settings = replace(model.settings, unbounded_runs=True)
    with pytest.raises(BadRequestError):
        asyncio.run(request(model))
    assert model.budget["unresolved_generation_requests"] == 1
    assert "preflight_failure" not in model.budget
    assert not any(kind == "managed_session_rejected" for kind, _ in events)
    assert len(sessions.creates) == 1


def test_real_runner_keeps_native_steering_history_across_multiple_tool_boundaries():
    first = Node(
        type="function_call",
        call_id="call_first",
        turn_id="turn_real",
        name="inspect_scope",
        arguments={"limit": 2},
    )
    second = Node(
        type="function_call",
        call_id="call_second",
        turn_id="turn_real",
        name="inspect_scope",
        arguments={"limit": 3},
    )
    waiting_first = session(status="requires_action", actions=[first])
    waiting_second = session(status="requires_action", actions=[second])
    final = session()
    second_event = event("agent.session.requires_action", session=waiting_second)
    second_event.event_id = "evt_wait_second"
    sessions = Sessions(
        [waiting_first, waiting_second, final],
        [
            [Node(id="fc_first", **vars(first))],
            [Node(id="fc_second", **vars(second))],
            [message()],
        ],
        [
            Stream(
                [
                    event("agent.session.created", session=waiting_first),
                    event("agent.session.requires_action", session=waiting_first),
                ]
            ),
            Stream([second_event]),
            Stream(
                [
                    event(
                        "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
                    ),
                    event("agent.session.idle", session=final),
                ]
            ),
        ],
    )
    model, events = model_for(sessions)
    steering_batches = iter([[], ["Check critical severity too"], []])
    model.steering = lambda: next(steering_batches)
    agent = Agent(
        name="Native evidence owner", instructions="Rules", model=model, tools=[inspect_scope]
    )
    result = asyncio.run(Runner.run(agent, "Inspect current scope"))
    assert result.final_output == "Verified result"
    assert len(sessions.creates) == 1
    assert len(sessions.inputs) == 2
    assert sessions.inputs[0]["events"] == [
        {
            "type": "agent.session.input.tool_result",
            "call_id": "call_first",
            "turn_id": "turn_real",
            "success": True,
            "output": "2",
        },
        {
            "type": "agent.session.input.message",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Check critical severity too"}],
                }
            ],
        },
    ]
    assert sessions.inputs[1]["events"] == [
        {
            "type": "agent.session.input.tool_result",
            "call_id": "call_second",
            "turn_id": "turn_real",
            "success": True,
            "output": "3",
        },
    ]
    assert sum(item.get("content") == "Check critical severity too" for item in model.history) == 1
    assert sum(kind == "steering_applied" for kind, _ in events) == 1


def test_fresh_runner_can_resume_exact_durable_native_history_after_steering():
    first_final = session()
    second_final = session()
    revised = message("Repaired verified result")
    revised.id = "msg_revised"
    sessions = Sessions(
        [first_final, second_final],
        [[message()], [revised]],
        [
            Stream(
                [
                    event("agent.session.created", session=first_final),
                    event(
                        "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
                    ),
                    event("agent.session.idle", session=first_final),
                ]
            ),
            Stream([]),
            Stream([]),
        ],
    )
    model, events = model_for(sessions)
    steering_batches = iter([["Check critical severity too"], []])
    model.steering = lambda: next(steering_batches)
    agent = Agent(name="Native evidence owner", instructions="Rules", model=model)

    async def run():
        first = await Runner.run(agent, "Inspect current scope")
        assert first.final_output == "Verified result"
        resumed = model.history + [{"role": "user", "content": "Repair the report"}]
        second = await Runner.run(agent, resumed)
        assert second.final_output == "Repaired verified result"

    asyncio.run(run())
    assert len(sessions.creates) == 1
    assert sessions.inputs[0]["events"] == [
        {
            "type": "agent.session.input.message",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Repair the report"}]},
            ],
        }
    ]
    assert sum(item.get("content") == "Check critical severity too" for item in model.history) == 1


def test_steering_history_reconciliation_still_rejects_changed_caller_prefix():
    action = Node(
        type="function_call",
        call_id="call_first",
        turn_id="turn_real",
        name="inspect_scope",
        arguments={"limit": 2},
    )
    waiting = session(status="requires_action", actions=[action])
    sessions = Sessions(
        [waiting],
        [[Node(id="fc_first", **vars(action))]],
        [
            Stream(
                [
                    event("agent.session.created", session=waiting),
                    event("agent.session.requires_action", session=waiting),
                ]
            )
        ],
    )
    model, _ = model_for(sessions)
    steering_batches = iter([["Check critical severity too"], []])
    model.steering = lambda: next(steering_batches)

    async def run():
        await request(model, tools=[inspect_scope])
        tampered = [{**item} for item in model._caller_history]
        tampered[0]["content"] = "Changed original scope"
        tampered.append({"type": "function_call_output", "call_id": "call_first", "output": "2"})
        with pytest.raises(ResponseProtocolError, match="changed acknowledged"):
            await request(model, tampered, [inspect_scope])

    asyncio.run(run())
    assert not sessions.inputs
    assert model._pending_calls["call_first"]["name"] == "inspect_scope"


def native_completed_provider(session_id, outputs):
    """A network-free native provider with distinct durable turn identities."""

    class IdentifiedSessions(Sessions):
        async def list_turns(self, requested_session_id, **kwargs):
            assert requested_session_id == self.last_snapshot.id
            return Page(
                [
                    Node(
                        id=self.last_snapshot.fixture_turn_id,
                        session_id=requested_session_id,
                        status="completed",
                    )
                ]
            )

    snapshots, pages, streams = [], [], []
    for index, output in enumerate(outputs):
        final = session()
        final.id = session_id
        final.spend_control = None
        final.fixture_turn_id = f"turn_{session_id}_{index}"
        item = message(output)
        item.id, item.turn_id = f"msg_{session_id}_{index}", final.fixture_turn_id
        observed = []
        if index == 0:
            observed.append(event("agent.session.created", session=final))
        observed.extend(
            [
                event(
                    "agent.session.turn.completed",
                    session_id=session_id,
                    turn_id=final.fixture_turn_id,
                ),
                event("agent.session.idle", session=final),
            ]
        )
        for offset, observed_event in enumerate(observed):
            observed_event.event_id = f"evt_{session_id}_{index}_{offset}"
        snapshots.append(final)
        pages.append([item])
        streams.append(Stream(observed))
    return IdentifiedSessions(snapshots, pages, streams)


def test_real_runner_independent_qa_reviews_use_runtime_fresh_native_sessions(
    tmp_path, monkeypatch
):
    from analyst import prompts
    from analyst.runtime import AnalystRuntime, QAReview

    verdict = QAReview(verified=True, errors=[], suggestions=[])
    providers = [
        native_completed_provider(f"sess_qa_{index}", [verdict.model_dump_json()])
        for index in range(2)
    ]
    remaining = iter(providers)
    observed = []

    async def emit(kind, payload):
        observed.append((kind, payload))

    def managed_factory(settings, callback, **kwargs):
        return ManagedAgentsModel(
            settings,
            callback,
            **kwargs,
            client=SimpleNamespace(
                beta=SimpleNamespace(agents=SimpleNamespace(sessions=next(remaining)))
            ),
        )

    monkeypatch.setattr("analyst.runtime.ManagedAgentsModel", managed_factory)
    runtime = AnalystRuntime(
        Settings(root=tmp_path, mode="live", api_key="offline-fixture"), SimpleNamespace(), emit
    )

    async def run():
        for index in range(2):
            qa_agent = runtime.new_qa_agent(prompts.QA)
            result = await Runner.run(qa_agent, f"Independent review {index}")
            assert result.final_output == verdict

    asyncio.run(run())
    assert len(runtime.models) == 2
    assert runtime.models[0] is not runtime.models[1]
    assert {model.session_id for model in runtime.models} == {"sess_qa_0", "sess_qa_1"}
    assert all(model.budget is runtime.budget for model in runtime.models)
    assert all(len(provider.creates) == 1 and not provider.inputs for provider in providers)
    assert all("spend_control" not in provider.creates[0] for provider in providers)


def test_real_runner_lead_guardrail_rejection_repairs_known_native_history(tmp_path, monkeypatch):
    from agents import GuardrailFunctionOutput, output_guardrail
    from agents.exceptions import OutputGuardrailTripwireTriggered
    from analyst import prompts
    from analyst.runtime import AnalystRuntime, QAReview, ReportDraft

    first = ReportDraft(report="Initial analysis", chart_paths=[])
    revised = ReportDraft(report="Repaired analysis", chart_paths=[])
    lead_provider = native_completed_provider(
        "sess_lead_repair", [first.model_dump_json(), revised.model_dump_json()]
    )
    qa_reject = QAReview(verified=False, errors=["Clarify caveat"], suggestions=[])
    qa_accept = QAReview(verified=True, errors=[], suggestions=[])
    qa_providers = [
        native_completed_provider("sess_qa_reject", [qa_reject.model_dump_json()]),
        native_completed_provider("sess_qa_accept", [qa_accept.model_dump_json()]),
    ]
    pending_qa = iter(qa_providers)
    observed = []

    async def emit(kind, payload):
        observed.append((kind, payload))

    def managed_factory(settings, callback, **kwargs):
        provider = lead_provider if kwargs["role"] == "Lead Analyst" else next(pending_qa)
        return ManagedAgentsModel(
            settings,
            callback,
            **kwargs,
            client=SimpleNamespace(beta=SimpleNamespace(agents=SimpleNamespace(sessions=provider))),
        )

    monkeypatch.setattr("analyst.runtime.ManagedAgentsModel", managed_factory)
    steering_batches = iter([["Keep the scope caveat visible"], []])
    runtime = AnalystRuntime(
        Settings(root=tmp_path, mode="live", api_key="offline-fixture"), SimpleNamespace(), emit
    )
    lead_model = runtime.model("Lead Analyst")
    lead_model.steering = lambda: next(steering_batches)
    reviewed = []

    @output_guardrail
    async def review_output(context, agent, output):
        qa_agent = runtime.new_qa_agent(prompts.QA)
        review = await Runner.run(qa_agent, output.report)
        reviewed.append(review.final_output)
        return GuardrailFunctionOutput(
            output_info=review.final_output, tripwire_triggered=not review.final_output.verified
        )

    lead = Agent(
        name="Lead Analyst",
        instructions="Rules",
        model=lead_model,
        output_type=ReportDraft,
        output_guardrails=[review_output],
    )

    async def run():
        with pytest.raises(OutputGuardrailTripwireTriggered):
            await Runner.run(lead, "Inspect current scope")
        assert lead_model.session_id == "sess_lead_repair"
        resumed = lead_model.history + [{"role": "user", "content": "Repair the report caveat"}]
        result = await Runner.run(lead, resumed)
        assert result.final_output == revised

    asyncio.run(run())
    assert [verdict.verified for verdict in reviewed] == [False, True]
    assert len(lead_provider.creates) == 1
    assert lead_provider.inputs[0]["events"] == [
        {
            "type": "agent.session.input.message",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Repair the report caveat"}],
                },
            ],
        }
    ]
    assert [model.session_id for model in runtime.models] == [
        "sess_lead_repair",
        "sess_qa_reject",
        "sess_qa_accept",
    ]
    assert (
        sum(item.get("content") == "Keep the scope caveat visible" for item in lead_model.history)
        == 1
    )
    assert all(len(provider.creates) == 1 and not provider.inputs for provider in qa_providers)
