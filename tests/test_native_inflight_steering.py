"""Native steering admission and current-run revision fences, without network calls."""

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from analyst.config import Settings
from analyst.model import ResponseProtocolError
from analyst.runtime import AnalystRuntime, FixtureModel
from analyst.tools import AnalystTools
from openai import ConflictError
from test_managed_agents import (
    Node,
    Page,
    Sessions,
    Stream,
    event,
    message,
    model_for,
    request,
    session,
)


class GatedStream:
    def __init__(self):
        self.ready = asyncio.Event()
        self.finish = asyncio.Event()

    async def __aiter__(self):
        yield event("agent.session.created", session=session(status="in_progress"))
        yield event(
            "agent.session.turn.in_progress",
            session_id="sess_real",
            turn_id="turn_real",
            turn=Node(id="turn_real", session_id="sess_real", status="in_progress"),
        )
        self.ready.set()
        await self.finish.wait()
        yield event("agent.session.turn.completed", session_id="sess_real", turn_id="turn_real")
        yield event("agent.session.idle", session=session())

    async def close(self):
        pass


def test_created_and_queued_turn_details_wait_for_same_run_boundary():
    async def scenario():
        class StartingStream(GatedStream):
            def __init__(self):
                super().__init__()
                self.created = asyncio.Event()
                self.queue = asyncio.Event()
                self.queued = asyncio.Event()
                self.start = asyncio.Event()

            async def __aiter__(self):
                yield event("agent.session.created", session=session(status="in_progress"))
                self.created.set()
                await self.queue.wait()
                yield event(
                    "agent.session.turn.created",
                    session_id="sess_real",
                    turn_id="turn_real",
                    turn=Node(id="turn_real", session_id="sess_real", status="queued"),
                )
                yield event("agent.session.in_progress", session=session(status="in_progress"))
                self.queued.set()
                await self.start.wait()
                yield event(
                    "agent.session.turn.in_progress",
                    session_id="sess_real",
                    turn_id="turn_real",
                    turn=Node(id="turn_real", session_id="sess_real", status="in_progress"),
                )
                self.ready.set()
                await self.finish.wait()
                yield event(
                    "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
                )
                yield event("agent.session.idle", session=session())

        live = StartingStream()
        sessions = Sessions([session()], [[message()]], [live])
        lead, events = model_for(sessions)
        lead.role = "Lead Analyst"

        async def emit(*args):
            events.append(args)

        runtime = AnalystRuntime(Settings(mode="mock"), None, emit)
        runtime.prompt = "Original question"
        runtime.models = [lead]
        consume = runtime.model_steering("Lead Analyst")
        running = asyncio.create_task(request(lead))
        try:
            await asyncio.wait_for(live.created.wait(), 2)
            created = await runtime.steer("Detail during creation", instruction_id="created")
            assert created["accepted"] and created["mode"] == "same_run_boundary"
            assert not sessions.inputs and not lead._live_submissions
            live.queue.set()
            await asyncio.wait_for(live.queued.wait(), 2)
            queued = await runtime.steer("Detail while queued", instruction_id="queued")
            assert queued["accepted"] and queued["mode"] == "same_run_boundary"
            assert not sessions.inputs and not lead._live_submissions
            live.start.set()
            await asyncio.wait_for(live.ready.wait(), 2)
            active = await runtime.steer("Detail during generation", instruction_id="active")
            assert active["accepted"] and active["mode"] == "native_inflight"
            assert active["session_id"] == "sess_real"
            assert len(sessions.inputs) == 1
            assert sessions.inputs[0]["idempotency_key"] == "active"
            assert await consume() == ["Detail during creation", "Detail while queued"]
            assert await consume() == []
            assert not runtime._steering_pending()
        finally:
            live.queue.set()
            live.start.set()
            live.finish.set()
            await running
        assert len(sessions.creates) == 1
        assert lead.history[1] == {"role": "user", "content": "Detail during generation"}
        assert [kind for kind, _ in events].count("steering_submitted") == 1

    asyncio.run(scenario())


def test_native_detail_is_sent_before_generation_finishes_and_is_not_replayed():
    async def scenario():
        live = GatedStream()
        second_message = message("Revised result")
        second_message.id = "msg_repaired"
        repaired_turn = event(
            "agent.session.turn.completed", session_id="sess_real", turn_id="turn_real"
        )
        repaired_turn.event_id = "evt_repaired_turn_completed"
        repaired_idle = event("agent.session.idle", session=session())
        repaired_idle.event_id = "evt_repaired_session_idle"
        sessions = Sessions(
            [session()],
            [[message()], [second_message]],
            [
                live,
                Stream(
                    [
                        event(
                            "agent.session.turn.item.done",
                            session_id="sess_real",
                            turn_id="turn_real",
                            item=second_message,
                        ),
                        repaired_turn,
                        repaired_idle,
                    ]
                ),
            ],
        )
        model, _ = model_for(sessions)
        running = asyncio.create_task(request(model))
        await asyncio.wait_for(live.ready.wait(), 2)
        receipt = await model.steer("Compare the baseline too", instruction_id="detail-1")
        assert receipt["accepted"] and receipt["mode"] == "native_inflight"
        assert not running.done()
        assert len(sessions.inputs) == 1
        assert sessions.inputs[0] == {
            "idempotency_key": "detail-1",
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Compare the baseline too"}],
                        }
                    ],
                }
            ],
        }
        await model.steer("Compare the baseline too", instruction_id="detail-1")
        assert len(sessions.inputs) == 1
        with pytest.raises(ValueError, match="different text"):
            await model.steer("Different detail", instruction_id="detail-1")
        live.finish.set()
        await running
        assert model.history[1] == {"role": "user", "content": "Compare the baseline too"}
        assert len(model._caller_history) == 2
        revised = await request(
            model, input=model._caller_history + [{"role": "user", "content": "Repair"}]
        )
        assert revised.output[0].content[0].text == "Revised result"
        assert len(sessions.creates) == 1
        assert sessions.inputs[-1]["events"] == [
            {
                "type": "agent.session.input.message",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Repair"}]}],
            }
        ]
        assert sum(item.get("content") == "Compare the baseline too" for item in model.history) == 1

    asyncio.run(scenario())


def test_closed_native_boundary_does_not_start_unobserved_turn():
    async def scenario():
        sessions = Sessions([], [], [])
        model, _ = model_for(sessions)
        model.session_id = "sess_real"
        assert await model.steer("More detail", instruction_id="detail-1") == {
            "accepted": False,
            "mode": "same_run_boundary",
        }
        assert not sessions.inputs

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind",
    [
        "agent.session.turn.created",
        "agent.session.requires_action",
        "agent.session.idle",
        "agent.session.failed",
        "agent.session.turn.completed",
        "agent.session.turn.failed",
        "agent.session.turn.cancelled",
    ],
)
def test_native_window_closes_before_publishing_nonrunning_phase(kind):
    async def scenario():
        sessions = Sessions([], [], [])
        model, _ = model_for(sessions)
        model._session_limit_cents = 30
        observed = []

        async def emit(event_kind, data):
            if event_kind == "provider_phase" and data["provider_event"] == kind:
                observed.append(await model.steer("Late detail", instruction_id="late"))

        model.emit = emit
        await model._observe(
            Stream(
                [
                    event("agent.session.created", session=session(status="in_progress")),
                    event(
                        "agent.session.turn.in_progress",
                        session_id="sess_real",
                        turn_id="turn_real",
                        turn=Node(id="turn_real", session_id="sess_real", status="in_progress"),
                    ),
                    event(kind, session_id="sess_real", turn_id="turn_real"),
                ]
            )
        )
        assert observed == [{"accepted": False, "mode": "same_run_boundary"}]
        assert not sessions.inputs and not model._live_submissions

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "turn",
    [
        {},
        {"id": "different", "session_id": "sess_real", "status": "in_progress"},
        {"id": "turn_real", "session_id": "different", "status": "in_progress"},
        {"id": "turn_real", "session_id": "sess_real", "status": "queued"},
        {
            "id": "turn_real",
            "session_id": "sess_real",
            "status": "in_progress",
            "subagent_id": "subagent_other",
        },
    ],
)
def test_native_window_requires_matching_running_root_turn(turn):
    async def scenario():
        sessions = Sessions([], [], [])
        model, _ = model_for(sessions)
        model._session_limit_cents = 30
        await model._observe(
            Stream(
                [
                    event("agent.session.created", session=session(status="in_progress")),
                    event(
                        "agent.session.turn.in_progress",
                        session_id="sess_real",
                        turn_id="turn_real",
                        turn=turn,
                    ),
                ]
            )
        )
        assert await model.steer("Early detail", instruction_id="early") == {
            "accepted": False,
            "mode": "same_run_boundary",
        }
        assert not sessions.inputs and not model._live_submissions

    asyncio.run(scenario())


def test_provider_idle_race_settles_new_turn_in_same_native_session():
    async def scenario():
        live = GatedStream()
        revised = message("Latest turn result")
        revised.turn_id = "turn_after_idle"
        sessions = Sessions([session(status="in_progress"), session()], [[revised]], [live])

        async def latest_turn(*args, **kwargs):
            return Page([Node(id="turn_after_idle", session_id="sess_real", status="completed")])

        sessions.turns.list = latest_turn
        model, _ = model_for(sessions)
        running = asyncio.create_task(request(model))
        await asyncio.wait_for(live.ready.wait(), 2)
        await model.steer("Racing detail", instruction_id="idle-race")
        live.finish.set()
        result = await running
        assert result.output[0].content[0].text == "Latest turn result"
        assert model.turn_id == "turn_after_idle"
        assert len(sessions.creates) == 1
        assert len(sessions.inputs) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("conflict", [False, True])
def test_ambiguous_native_admission_is_not_automatically_replayed(conflict):
    async def scenario():
        sessions = Sessions([], [], [])
        calls = []

        async def lost_reply(*args, **kwargs):
            calls.append(kwargs)
            if conflict:
                raise ConflictError(
                    "Current resource state conflicts with admission",
                    response=httpx2.Response(
                        409, request=httpx2.Request("POST", "https://api.openai.com/events")
                    ),
                    body={"code": "conflict_error"},
                )
            raise ConnectionError("reply lost after dispatch")

        sessions.events.create = lost_reply
        model, _ = model_for(sessions)
        model.session_id = "sess_real"
        model._accepts_live_steering = True
        with pytest.raises(ConflictError if conflict else ConnectionError):
            await model.steer("More detail", instruction_id="durable-key")
        with pytest.raises(ResponseProtocolError, match="unresolved"):
            await model.steer("More detail", instruction_id="durable-key")
        assert len(calls) == 1 and calls[0]["idempotency_key"] == "durable-key"
        assert not model._live_inputs

    asyncio.run(scenario())


@pytest.mark.parametrize("ambiguous", [False, True])
def test_stop_cancels_after_blocked_message_admission_and_closes_new_steering(ambiguous):
    async def scenario():
        live = GatedStream()
        sessions = Sessions([session()], [], [live])
        admitted = asyncio.Event()
        release = asyncio.Event()
        order = []

        async def delayed_input(session_id, **kwargs):
            kind = kwargs["events"][0]["type"]
            if kind == "agent.session.input.message":
                order.append("message-dispatched")
                admitted.set()
                await release.wait()
                order.append("message-finished")
                if ambiguous:
                    raise ConnectionError("Provider admission reply lost")
            else:
                assert kind == "agent.session.input.cancel"
                order.append("cancel-admitted")

        original_retrieve = sessions.retrieve

        async def retrieve(session_id):
            order.append("idle-readback")
            return await original_retrieve(session_id)

        sessions.events.create = delayed_input
        sessions.retrieve = retrieve
        model, events = model_for(sessions)
        running = asyncio.create_task(request(model))
        await asyncio.wait_for(live.ready.wait(), 2)
        sending = asyncio.create_task(
            model.steer("Current detail", instruction_id="blocked-detail")
        )
        await asyncio.wait_for(admitted.wait(), 2)
        running.cancel()
        await asyncio.sleep(0)
        assert model._cancel_requested
        assert order == ["message-dispatched"]
        with pytest.raises(ResponseProtocolError, match="stopped accepting"):
            await model.steer("After Stop", instruction_id="late-detail")
        release.set()
        if ambiguous:
            with pytest.raises(ConnectionError):
                await sending
        else:
            assert (await sending)["accepted"]
        with pytest.raises(asyncio.CancelledError):
            await running
        assert order == [
            "message-dispatched",
            "message-finished",
            "cancel-admitted",
            "idle-readback",
        ]
        receipt = next(data for kind, data in events if kind == "provider_cancel")
        assert receipt["terminal_observed"]
        assert receipt["steering_admission_unresolved"] is ambiguous
        assert receipt["terminal_confirmed"] is not ambiguous
        assert model.budget["unresolved_generation_requests"] == 1
        assert len(sessions.creates) == 1
        with pytest.raises(ResponseProtocolError, match="stopped accepting"):
            await model.steer("Current detail", instruction_id="blocked-detail")

    asyncio.run(scenario())


def test_repeated_stop_cannot_interrupt_pending_native_cancellation_cleanup():
    async def scenario():
        sessions = Sessions([session()], [], [])
        started, release = asyncio.Event(), asyncio.Event()
        order = []

        async def delayed_input(session_id, **kwargs):
            if kwargs["events"][0]["type"] == "agent.session.input.message":
                started.set()
                await release.wait()
                order.append("message")
            else:
                order.append("cancel")

        sessions.events.create = delayed_input
        model, events = model_for(sessions)
        model.session_id = "sess_real"
        model._accepts_live_steering = True
        sending = asyncio.create_task(model.steer("Detail", instruction_id="one"))
        await asyncio.wait_for(started.wait(), 2)
        stop = asyncio.create_task(model._cancel_managed())
        await asyncio.sleep(0)
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
        assert model._cancel_task is not None and not model._cancel_task.done()
        release.set()
        await sending
        await model._cancel_managed()
        assert order == ["message", "cancel"]
        assert len([kind for kind, _ in events if kind == "provider_cancel"]) == 1

    asyncio.run(scenario())


def test_runtime_routes_direct_detail_only_to_lead_and_consumes_boundary_once():
    async def scenario():
        events = []

        async def emit(*args):
            events.append(args)

        sessions = Sessions([], [], [])
        lead, _ = model_for(sessions)
        lead.role = "Lead Analyst"
        lead.session_id = "sess_real"
        lead._accepts_live_steering = True
        runtime = AnalystRuntime(Settings(mode="mock"), None, emit)
        runtime.prompt = "Original question"
        runtime.models = [lead, SimpleNamespace(role="QA Verifier")]
        result = await runtime.steer("More detail", instruction_id="detail-1")
        assert result["mode"] == "native_inflight"
        assert runtime.steering_revision == 1
        assert runtime.prompt == "Original question\nMore detail"
        assert await runtime.steer("More detail", instruction_id="detail-1") == result
        assert len(sessions.inputs) == 1 and runtime.steering_revision == 1
        assert [kind for kind, _ in events] == ["steering_submitted"]
        runtime._verified_revision = 1
        await runtime._announce_verified_steering()
        await runtime._announce_verified_steering()
        assert [kind for kind, _ in events] == ["steering_submitted", "steering_applied"]
        assert events[-1][1]["mode"] == "verified_report_revision"
        assert events[-1][1]["instruction_id"] == "detail-1"
        consume = runtime.model_steering("Lead Analyst")
        assert await consume() == []
        assert runtime.model_steering("QA Verifier") is None
        lead._accepts_live_steering = False
        held = await runtime.steer("Second detail", instruction_id="detail-2")
        assert held["accepted"] and held["mode"] == "same_run_boundary"
        assert await consume() == ["Second detail"]
        assert await consume() == []
        assert not runtime._steering_pending()

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["qa", "finalization"])
def test_detail_during_qa_or_finalization_revises_same_run_before_publication(
    tmp_path, monkeypatch, stage
):
    async def scenario():
        source = tmp_path / "sample.csv"
        source.write_text("group,value\na,2\na,4\nb,6\n")
        tools = AnalystTools(tmp_path / "run", [source])
        events = []

        async def emit(kind, data):
            events.append((kind, data))
            if (
                stage == "finalization"
                and kind == "todo"
                and data.get("items")
                and all(item["status"] == "completed" for item in data["items"])
            ):
                await inject()

        runtime = AnalystRuntime(Settings(mode="mock", memory_enabled=False), tools, emit)
        original_response = FixtureModel.get_response
        qa_prompts = []
        injected = False

        async def inject():
            nonlocal injected
            if not injected:
                injected = True
                await runtime.steer(
                    "Include the baseline interpretation", instruction_id="during-finalization"
                )

        async def response(model, *args, **kwargs):
            if model.role == "QA Verifier":
                qa_prompts.append(json.dumps(args[1] if len(args) > 1 else kwargs["input"]))
                if stage == "qa":
                    await inject()
            return await original_response(model, *args, **kwargs)

        monkeypatch.setattr(FixtureModel, "get_response", response)
        try:
            result = await runtime.run("Profile the supplied CSV")
            assert result["status"] == "completed", result["qa"]
            assert result["instruction_revision"] == 1
            assert result["qa"]["verified"]
            assert len(qa_prompts) == 2
            assert "Include the baseline interpretation" not in qa_prompts[0]
            assert "Include the baseline interpretation" in qa_prompts[1]
            assert len([model for model in runtime.models if model.role == "Lead Analyst"]) == 1
            assert len([item for item in events if item[0] == "report"]) == 1
            assert not runtime.accepting_steering
            assert result["artifacts"] == result["delivery"]["artifacts"] == []
            assert not (tools.run_dir / "artifacts/business_report.html").exists()
        finally:
            await tools.close()

    asyncio.run(scenario())


def test_runtime_rejects_closed_or_unresolved_steering_without_a_new_run():
    async def scenario():
        async def emit(*args):
            pass

        runtime = AnalystRuntime(Settings(mode="mock"), None, emit)
        runtime.accepting_steering = False
        with pytest.raises(ValueError, match="finished accepting"):
            await runtime.steer("Late detail", instruction_id="late")
        assert not runtime._direct_steering
        runtime._direct_steering["unknown"] = {"text": "Unknown", "state": "unresolved"}
        with pytest.raises(ResponseProtocolError, match="unresolved"):
            runtime._steering_pending()

    asyncio.run(scenario())
