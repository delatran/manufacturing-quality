"""Concurrent durable reads preserve the same proof and cancellation boundary."""

import asyncio

import pytest
from analyst.managed_agents import managed_trace
from analyst.model import ResponseProtocolError
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


def test_snapshot_reads_overlap_and_return_only_after_both_complete():
    async def run():
        started = {kind: asyncio.Event() for kind in ("turn", "items")}
        finished = []
        records = [{"id": "durable_item", "type": "message"}]
        model, _ = model_for(Sessions([], [], []))

        async def verify_turn(snapshot):
            started["turn"].set()
            await started["items"].wait()
            finished.append("turn")

        async def items():
            started["items"].set()
            await started["turn"].wait()
            finished.append("items")
            return records

        model._verify_turn_boundary = verify_turn
        model._items = items
        assert await asyncio.wait_for(model._snapshot_items(session()), timeout=1) == records
        assert set(finished) == {"turn", "items"}

    asyncio.run(run())


@pytest.mark.parametrize("failed_read", ["turn", "items"])
def test_snapshot_failure_cancels_and_awaits_other_read_without_wrapping_error(failed_read):
    async def run():
        started = {kind: asyncio.Event() for kind in ("turn", "items")}
        stopped = []
        failure = ResponseProtocolError("Durable read rejected")
        model, _ = model_for(Sessions([], [], []))

        async def read(kind):
            started[kind].set()
            other = "items" if kind == "turn" else "turn"
            await started[other].wait()
            if kind == failed_read:
                raise failure
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                stopped.append(kind)

        model._verify_turn_boundary = lambda snapshot: read("turn")
        model._items = lambda: read("items")
        with pytest.raises(ResponseProtocolError) as caught:
            await asyncio.wait_for(model._snapshot_items(session()), timeout=1)
        assert caught.value is failure
        assert stopped == ["items" if failed_read == "turn" else "turn"]
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(run())


def test_cancelled_snapshot_stops_both_reads_before_returning():
    async def run():
        started = {kind: asyncio.Event() for kind in ("turn", "items")}
        stopped = []
        model, _ = model_for(Sessions([], [], []))

        async def read(kind):
            started[kind].set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                stopped.append(kind)

        model._verify_turn_boundary = lambda snapshot: read("turn")
        model._items = lambda: read("items")
        pending = asyncio.create_task(model._snapshot_items(session()))
        await asyncio.wait_for(asyncio.gather(*(value.wait() for value in started.values())), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert set(stopped) == {"turn", "items"}
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(run())


@pytest.mark.parametrize("mismatch", ["session", "turn", "status"])
def test_prefetched_items_cannot_escape_a_mismatched_durable_boundary(mismatch):
    async def run():
        final = session()
        prefetched = asyncio.Event()

        class MismatchedSessions(Sessions):
            async def list_turns(self, session_id, **kwargs):
                await prefetched.wait()
                return Page(
                    [
                        Node(
                            id="turn_new" if mismatch == "turn" else "turn_real",
                            session_id="another_session" if mismatch == "session" else session_id,
                            status="failed" if mismatch == "status" else "completed",
                        )
                    ]
                )

            async def list_items(self, session_id, **kwargs):
                prefetched.set()
                return await super().list_items(session_id, **kwargs)

        sessions = MismatchedSessions(
            [final],
            [[message("Unverified prefetched answer")]],
            [
                Stream(
                    [
                        event("agent.session.created", session=final),
                        event(
                            "agent.session.turn.completed",
                            session_id="sess_real",
                            turn_id="turn_real",
                        ),
                        event("agent.session.idle", session=final),
                    ]
                )
            ],
        )
        model, emitted = model_for(sessions)
        with pytest.raises(ResponseProtocolError):
            await asyncio.wait_for(request(model), timeout=1)
        assert prefetched.is_set()
        assert not model.history and not model.usage and not model._seen_items
        assert "Unverified prefetched answer" not in str(emitted)
        assert len(sessions.creates) == 1
        assert all(
            item["events"] == [{"type": "agent.session.input.cancel"}] for item in sessions.inputs
        )

    asyncio.run(run())


def test_later_item_page_failure_never_delivers_the_prefetched_prefix():
    async def run():
        final = session()
        failure = ResponseProtocolError("Second item page rejected")

        class FailingPage(Page):
            async def __aiter__(self):
                yield message("Incomplete snapshot answer")
                raise failure

        class FailingSessions(Sessions):
            async def list_items(self, session_id, **kwargs):
                assert kwargs == {"order": "asc", "limit": 100}
                return FailingPage([])

        sessions = FailingSessions(
            [final],
            [],
            [
                Stream(
                    [
                        event("agent.session.created", session=final),
                        event(
                            "agent.session.turn.completed",
                            session_id="sess_real",
                            turn_id="turn_real",
                        ),
                        event("agent.session.idle", session=final),
                    ]
                )
            ],
        )
        model, emitted = model_for(sessions)
        with pytest.raises(ResponseProtocolError) as caught:
            await request(model)
        assert caught.value is failure
        assert not model.history and not model.usage and not model._seen_items
        assert "Incomplete snapshot answer" not in str(emitted)
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(run())


def test_observer_normalizes_each_event_once_and_preserves_public_projection():
    class CountedEvent(Node):
        dump_calls = 0

        def model_dump(self, **kwargs):
            self.dump_calls += 1
            value = super().model_dump(**kwargs)
            value.pop("dump_calls", None)
            return value

    events = [
        CountedEvent(
            type="agent.session.turn.output_text.delta",
            event_id="text_delta",
            session_id="sess_real",
            turn_id="turn_real",
            item_id="message",
            delta="Visible text",
        ),
        CountedEvent(
            type="agent.session.turn.reasoning_summary_text.done",
            event_id="public_summary",
            session_id="sess_real",
            turn_id="turn_real",
            item_id="reason",
            summary_index=0,
            text="Public summary",
            encrypted_content="PRIVATE",
        ),
        CountedEvent(
            type="agent.session.turn.function_call_arguments.delta",
            event_id="arguments",
            session_id="sess_real",
            turn_id="turn_real",
            item_id="call",
            call_id="call",
            delta="{}",
        ),
    ]
    expected = [managed_trace(item) for item in events]
    for item in events:
        item.dump_calls = 0
    model, emitted = model_for(Sessions([], [], []))
    model.session_id = "sess_real"
    model.turn_id = "turn_real"
    assert asyncio.run(model._observe(Stream(events))) is False
    assert [item.dump_calls for item in events] == [1, 1, 1]
    assert emitted == expected
    assert "PRIVATE" not in str(emitted)
