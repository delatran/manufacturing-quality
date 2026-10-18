"""Slow or failed browser transports do not retain unbounded live event queues."""

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analyst.app import create_app
from analyst.config import Settings
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect


def websocket_endpoint(application):
    return next(
        route.endpoint
        for route in application.routes
        if getattr(route, "path", None) == "/api/sessions/{sid}/ws"
    )


def test_slow_browser_replays_all_events_from_bounded_pages(tmp_path, monkeypatch):
    async def scenario():
        app = create_app(replace(Settings(), state_dir=str(tmp_path), mode="mock"))
        workspace = app.state.workspace
        sid = workspace.store.create_session()["id"]
        other = workspace.store.create_session()["id"]
        send_blocked = asyncio.Event()
        release = asyncio.Event()
        delivered = asyncio.Event()
        disconnect = asyncio.Event()
        sent = []
        batches = []
        original_events = workspace.store.events

        def events(*args, **kwargs):
            result = original_events(*args, **kwargs)
            batches.append(len(result))
            return result

        monkeypatch.setattr(workspace.store, "events", events)

        class Socket:
            headers = {}
            query_params = {}

            async def accept(self):
                pass

            async def send_json(self, event):
                send_blocked.set()
                await release.wait()
                sent.append(event)
                if len(sent) == 600:
                    delivered.set()

            async def receive_json(self):
                await disconnect.wait()
                raise WebSocketDisconnect()

        first = await workspace.emit(sid, None, "message", {"text": "First"})
        task = asyncio.create_task(websocket_endpoint(app)(Socket(), sid))
        try:
            await asyncio.wait_for(send_blocked.wait(), 5)
            expected = [first]
            for index in range(599):
                expected.append(await workspace.emit(sid, None, "message", {"text": str(index)}))
            await workspace.emit(other, None, "message", {"text": "Another visitor"})
            notification = next(iter(workspace.listeners[sid]))
            assert notification.maxsize == notification.qsize() == 1
            release.set()
            await asyncio.wait_for(delivered.wait(), 5)
            disconnect.set()
            await asyncio.wait_for(task, 5)
            assert sent == expected
            assert max(batches) <= 256
            assert sid not in workspace.listeners
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await workspace.close()

    asyncio.run(scenario())


def test_failed_sender_cancels_receiver_and_unsubscribes_immediately(tmp_path):
    async def scenario():
        app = create_app(replace(Settings(), state_dir=str(tmp_path), mode="mock"))
        workspace = app.state.workspace
        sid = workspace.store.create_session()["id"]
        await workspace.emit(sid, None, "message", {"text": "Replay"})
        receiving = asyncio.Event()
        receiver_closed = asyncio.Event()

        class Socket:
            headers = {}
            query_params = {}

            async def accept(self):
                pass

            async def send_json(self, event):
                await receiving.wait()
                raise WebSocketDisconnect(code=1006)

            async def receive_json(self):
                receiving.set()
                try:
                    await asyncio.Future()
                finally:
                    receiver_closed.set()

        try:
            await asyncio.wait_for(websocket_endpoint(app)(Socket(), sid), 5)
            assert receiver_closed.is_set()
            assert sid not in workspace.listeners
        finally:
            await workspace.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("disconnect_transport", [False, True])
def test_inflight_steering_survives_transport_loss_and_reconciles_task_cancellation(
    tmp_path, disconnect_transport
):
    async def scenario():
        app = create_app(replace(Settings(), state_dir=str(tmp_path), mode="mock"))
        workspace = app.state.workspace
        sid = workspace.store.create_session()["id"]
        rid, _ = workspace.store.begin_run(sid)
        active_run = asyncio.create_task(asyncio.Event().wait())
        workspace.tasks[sid] = active_run
        workspace.active_run_ids[sid] = rid
        workspace.active_contexts[sid] = None
        workspace.active_languages[sid] = "en"
        workspace.active_action_intents[sid] = "none"
        steering_entered = asyncio.Event()
        steering_release = asyncio.Event()
        send_failed = asyncio.Event()
        steering_cancelled = False

        async def steer(text, instruction_id):
            nonlocal steering_cancelled
            steering_entered.set()
            try:
                await steering_release.wait()
            except asyncio.CancelledError:
                steering_cancelled = True
                raise
            return {"accepted": True}

        workspace.active_runtimes[sid] = SimpleNamespace(accepting_steering=True, steer=steer)

        class Socket:
            headers = {}
            query_params = {}
            received = False

            async def accept(self):
                pass

            async def send_json(self, event):
                await steering_entered.wait()
                send_failed.set()
                raise WebSocketDisconnect(code=1006)

            async def receive_json(self):
                if not self.received:
                    self.received = True
                    return {"type": "prompt", "text": "Add this detail", "steer_run_id": rid}
                await asyncio.Future()

        if disconnect_transport:
            task = asyncio.create_task(websocket_endpoint(app)(Socket(), sid))
        else:
            task = asyncio.create_task(workspace.prompt(sid, "Add this detail", steer_run_id=rid))
        try:
            await asyncio.wait_for(steering_entered.wait(), 5)
            if disconnect_transport:
                await asyncio.wait_for(send_failed.wait(), 5)
                await asyncio.sleep(0)
                assert not task.done()
                assert not active_run.cancelled()
                steering_release.set()
                await asyncio.wait_for(task, 5)
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            row = workspace.store.db.execute(
                "SELECT state FROM steering WHERE run_id=?", (rid,)
            ).fetchone()
            failures = [
                event for event in workspace.store.events(sid) if event["type"] == "steering_failed"
            ]
            if disconnect_transport:
                assert not steering_cancelled
                assert row["state"] == "applied"
                assert not failures
                assert sid not in workspace.listeners
            else:
                assert steering_cancelled
                assert row["state"] == "unconfirmed"
                assert failures[0]["data"]["error_type"] == "CancelledError"
            assert not workspace.store.has_queued_instruction(sid)
        finally:
            steering_release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await workspace.close()

    asyncio.run(scenario())


def test_session_snapshot_http_contract_and_corrupt_data_recovery(tmp_path):
    app = create_app(replace(Settings(), state_dir=str(tmp_path), mode="mock"))
    with TestClient(app, raise_server_exceptions=False) as client:
        store = app.state.workspace.store
        sid = client.post("/api/sessions").json()["id"]
        event = store.emit(
            sid, None, "message", {"text": "Tiếng Việt 😀", "values": [True, None, 10**100, 1e308]}
        )
        response = client.get(f"/api/sessions/{sid}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.json() == store.session(sid)
        missing = client.get("/api/sessions/missing")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Session not found"}
        for raw in ("{", '{"value":NaN}', '{"value":1e999}', r'{"text":"\ud800"}'):
            with store.db:
                store.db.execute("UPDATE events SET data=? WHERE id=?", (raw, event["id"]))
            assert client.get(f"/api/sessions/{sid}").status_code == 500
        with store.db:
            store.db.execute(
                "UPDATE events SET data=? WHERE id=?", (json.dumps(event["data"]), event["id"])
            )
        assert client.get(f"/api/sessions/{sid}").json()["events"] == [event]


@pytest.mark.parametrize("cancel_request", [False, True])
def test_blocked_session_reader_leaves_event_loop_live_and_closes_on_cancel(
    tmp_path, monkeypatch, cancel_request
):
    async def scenario():
        app = create_app(replace(Settings(), state_dir=str(tmp_path), mode="mock"))
        workspace = app.state.workspace
        sid = workspace.store.create_session()["id"]
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        closed = asyncio.Event()
        release = threading.Event()
        connect = sqlite3.connect

        class PausedReader(sqlite3.Connection):
            def execute(self, statement, parameters=()):
                cursor = super().execute(statement, parameters)
                if statement.startswith("SELECT id,created FROM sessions"):
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(5), "Reader was not released by the event loop"
                return cursor

            def close(self):
                super().close()
                loop.call_soon_threadsafe(closed.set)

        def open_reader(database, **kwargs):
            return connect(database, **kwargs, factory=PausedReader)

        monkeypatch.setattr(sqlite3, "connect", open_reader)
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            request = asyncio.create_task(client.get(f"/api/sessions/{sid}"))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                health = await asyncio.wait_for(client.get("/health"), 5)
                assert health.status_code == 200
                event = await workspace.emit(sid, None, "message", {"text": "Committed"})
                assert workspace.store.events(sid) == [event]
                assert not request.done()
                if cancel_request:
                    request.cancel()
                release.set()
                if cancel_request:
                    with pytest.raises(asyncio.CancelledError):
                        await request
                else:
                    response = await asyncio.wait_for(request, 5)
                    assert response.status_code == 200
                    assert response.json()["events"] == []
                await asyncio.wait_for(closed.wait(), 5)
            finally:
                release.set()
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                await workspace.close()

    asyncio.run(scenario())
