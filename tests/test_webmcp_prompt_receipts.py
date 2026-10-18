"""Durable prompt admission checks with synthetic runtimes and disposable state."""

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analyst.app import Workspace, canonical_json, create_app
from analyst.cloud_runtime import CloudSessionStore, PersistenceUnavailable
from analyst.config import Settings
from analyst.store import PromptRequestConflict, SessionStore
from fastapi.testclient import TestClient

A = "a" * 64


def request_payload(**changes):
    return {
        "text": "Analyze the fixture",
        "analysis_context_id": None,
        "language": "en",
        "action_intent": "none",
        "steer_run_id": None,
        "reply_to": None,
        "request_origin": "webmcp",
        **changes,
    }


def configuration(tmp_path, **changes):
    return replace(
        Settings(), state_dir=str(tmp_path / "state"), mode="mock", memory_enabled=False, **changes
    )


class SyntheticRuntime:
    calls = []

    def __init__(self, settings, toolkit, emit, **options):
        self.emit = emit

    async def run(self, text):
        self.calls.append(text)
        return {"status": "completed", "usage": [], "cost_usd": 0}


def wait_for_completed(client, sid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        runs = client.get(f"/api/sessions/{sid}").json()["runs"]
        if runs and all(run["state"] == "completed" for run in runs):
            return runs
        time.sleep(0.01)
    raise AssertionError("Synthetic runtime did not complete")


def test_two_connections_claim_one_identity_and_preserve_original_payload(tmp_path):
    first, second = SessionStore(tmp_path), SessionStore(tmp_path)
    try:
        sid = first.create_session()["id"]
        key = str(uuid.uuid4())
        request = request_payload(text="Tiếng Việt 😀")
        barrier = threading.Barrier(2)

        def claim(store):
            barrier.wait(timeout=5)
            return store.reserve_prompt_request(sid, key, request)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (first, second)))
        assert sum(inserted for inserted, _ in results) == 1
        assert results[0][1] == results[1][1]
        receipt = results[0][1]
        assert (
            receipt["request_sha256"]
            == hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()
        )
        with pytest.raises(PromptRequestConflict):
            second.reserve_prompt_request(sid, key, {**request, "text": "Other input"})
        assert first.prompt_request(sid, key) == receipt
        other = first.create_session()["id"]
        with pytest.raises(KeyError):
            second.prompt_request(other, key)
        assert second.reserve_prompt_request(other, key, request)[0] is True
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_keyed_ack_readback_and_replay_start_only_one_run(tmp_path, transport):
    SyntheticRuntime.calls = []
    application = create_app(configuration(tmp_path), runtime_factory=SyntheticRuntime)
    with TestClient(application) as client:
        sid = client.post("/api/sessions").json()["id"]
        key = str(uuid.uuid4())
        body = {**request_payload(), "client_request_id": key}
        url = f"/api/sessions/{sid}/prompt"
        if transport == "http":
            receipt = client.post(url, json=body).json()
        else:
            with client.websocket_connect(f"/api/sessions/{sid}/ws") as socket:
                socket.send_json({"type": "prompt", **body})
                while True:
                    event = socket.receive_json()
                    if event["type"] == "prompt_receipt":
                        receipt = event["data"]
                        break
        assert receipt["status"] == "accepted"
        assert receipt["action"] == "start"
        assert receipt["queued"] is False
        assert receipt["run_id"]
        assert receipt["request"] == request_payload()
        runs = wait_for_completed(client, sid)
        assert [run["id"] for run in runs] == [receipt["run_id"]]
        readback = client.get(f"/api/sessions/{sid}/prompt-requests/{key}")
        assert readback.headers["Cache-Control"] == "no-store"
        saved = readback.json()
        assert saved["status"] == "accepted"
        replay = client.post(url, json={**body, "text": "  Analyze the fixture  "}).json()
        assert replay == {**saved, "replayed": True}
        assert SyntheticRuntime.calls == ["Analyze the fixture"]
        messages = [
            event
            for event in application.state.workspace.store.events(sid)
            if event["type"] == "message" and event["data"].get("role") == "user"
        ]
        assert len(messages) == 1
        assert messages[0]["data"]["client_request_id"] == key
        assert client.post(url, json={**body, "text": "Changed"}).status_code == 409
        assert client.get(f"/api/sessions/{sid}/prompt-requests/{key}").json() == saved
        other = client.post("/api/sessions").json()["id"]
        assert client.get(f"/api/sessions/{other}/prompt-requests/{key}").status_code == 404
        assert client.get(f"/api/sessions/missing/prompt-requests/{key}").status_code == 404
        assert client.get(f"/api/sessions/{sid}/prompt-requests/not-a-uuid").status_code == 422


def test_reopened_app_replays_before_unavailable_provider_stop_and_context_checks(tmp_path):
    config = configuration(tmp_path)
    application = create_app(config, runtime_factory=SyntheticRuntime)
    with TestClient(application) as client:
        sid = client.post("/api/sessions").json()["id"]
        key = str(uuid.uuid4())
        body = {**request_payload(), "client_request_id": key}
        receipt = client.post(f"/api/sessions/{sid}/prompt", json=body).json()
        wait_for_completed(client, sid)
        application.state.workspace.store.stop_session(sid, "Synthetic provider stop")
    restored = create_app(replace(config, mode="auto", api_key=""))
    with TestClient(restored) as client:
        workspace = restored.state.workspace

        async def unavailable_context(*args):
            raise AssertionError("Replay must not resolve a current context")

        workspace.analysis.context_by_id = unavailable_context
        workspace.analysis.get_context = unavailable_context
        replay = client.post(f"/api/sessions/{sid}/prompt", json=body)
        assert replay.status_code == 200
        assert replay.json() == {**receipt, "replayed": True}
        assert len(workspace.store.runs(sid)) == 1
        assert workspace.tasks == {}
        conflict = client.post(
            f"/api/sessions/{sid}/prompt", json={**body, "analysis_context_id": A}
        )
        assert conflict.status_code == 409
        assert (
            client.post(
                f"/api/sessions/{sid}/prompt", json={"text": "Legacy UI request"}
            ).status_code
            == 503
        )


def test_queued_request_is_admitted_once_then_runs_at_existing_boundary(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Runtime:
        def __init__(self, settings, toolkit, emit, **options):
            pass

        async def run(self, prompt):
            calls.append(prompt)
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return {"status": "completed", "usage": [], "cost_usd": 0}

    application = create_app(configuration(tmp_path), runtime_factory=Runtime)
    with TestClient(application) as client:
        sid = client.post("/api/sessions").json()["id"]
        url = f"/api/sessions/{sid}/prompt"
        first = {**request_payload(text="First analysis"), "client_request_id": str(uuid.uuid4())}
        client.post(url, json=first).raise_for_status()
        assert entered.wait(5)
        key = str(uuid.uuid4())
        body = {**request_payload(text="Next analysis"), "client_request_id": key}
        receipt = client.post(url, json=body).json()
        assert receipt["status"] == "accepted"
        assert receipt["queued"] is True
        assert receipt["run_id"] is None
        assert client.post(url, json=body).json() == {**receipt, "replayed": True}
        assert application.state.workspace.store.session(sid)["pending_instructions"] == 1
        release.set()
        deadline = time.monotonic() + 5
        while len(calls) != 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == ["First analysis", "Next analysis"]
        assert len(wait_for_completed(client, sid)) == 2
        assert client.post(url, json=body).json() == {**receipt, "replayed": True}
        assert application.state.workspace.store.session(sid)["pending_instructions"] == 0


@pytest.mark.asyncio
async def test_followup_receipt_precedes_late_answer_invalidation_guard(tmp_path):
    workspace = Workspace(configuration(tmp_path))
    sid = workspace.store.create_session()["id"]
    original_rid, _ = workspace.store.begin_run(sid)
    workspace.store.finish_run(original_rid, "completed")
    original = workspace.store.emit(
        sid,
        original_rid,
        "message",
        {"role": "assistant", "text": "Verified fixture answer", "qa": {"verified": True}},
    )
    started = []

    async def synthetic_run(*args, prepared_run=None, **kwargs):
        started.append(args[1])
        workspace.store.finish_run(prepared_run[0], "completed")

    workspace._run = synthetic_run
    key = str(uuid.uuid4())
    body = {
        **request_payload(reply_to={"run_id": original_rid, "message_id": original["id"]}),
        "client_request_id": key,
    }
    try:
        receipt = await workspace.prompt(sid, **body)
        await workspace.tasks[sid]
        assert receipt["status"] == "accepted"
        workspace.store.emit(
            sid,
            original_rid,
            "message",
            {"role": "assistant", "text": "Late invalidation", "qa": {"verified": False}},
        )
        replay = await workspace.prompt(sid, **body)
        assert replay == {**receipt, "replayed": True}
        rejected = await workspace.prompt(sid, **{**body, "client_request_id": str(uuid.uuid4())})
        assert rejected["status"] == "rejected"
        assert started == [body["text"]]
        assert len(workspace.store.runs(sid)) == 2
    finally:
        await workspace.close()


def test_websocket_conflict_ack_names_original_request_without_replacing_receipt(tmp_path):
    application = create_app(configuration(tmp_path), runtime_factory=SyntheticRuntime)
    with TestClient(application) as client:
        sid = client.post("/api/sessions").json()["id"]
        key = str(uuid.uuid4())
        body = {**request_payload(), "client_request_id": key}
        receipt = client.post(f"/api/sessions/{sid}/prompt", json=body).json()
        wait_for_completed(client, sid)
        after = application.state.workspace.store.events(sid)[-1]["id"]
        with client.websocket_connect(f"/api/sessions/{sid}/ws?after={after}") as socket:
            socket.send_json({"type": "prompt", **body, "text": "Different prompt"})
            event = socket.receive_json()
            assert event["type"] == "error"
            assert event["data"]["code"] == "idempotency_conflict"
            assert event["data"]["client_request_id"] == key
        assert client.get(f"/api/sessions/{sid}/prompt-requests/{key}").json() == receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancel_before_first_coroutine_step_reconciles_precreated_run(tmp_path, shutdown):
    SyntheticRuntime.calls = []
    workspace = Workspace(configuration(tmp_path), runtime_factory=SyntheticRuntime)
    sid = workspace.store.create_session()["id"]
    key = str(uuid.uuid4())
    receipt = await workspace.prompt(sid, **request_payload(), client_request_id=key)
    rid = receipt["run_id"]
    assert receipt["status"] == "accepted"
    assert rid in workspace.scheduled_run_ids
    assert SyntheticRuntime.calls == []
    if shutdown:
        await workspace.close()
        store = SessionStore(workspace.state_dir)
    else:
        cancelled = await workspace.cancel(sid, rid)
        assert cancelled == {"cancel_requested": True, "run_id": rid}
        await asyncio.gather(workspace.tasks[sid], return_exceptions=True)
        store = workspace.store
    try:
        assert store.runs(sid)[0]["state"] == ("interrupted" if shutdown else "cancelled")
        assert store.prompt_request(sid, key) == receipt
        assert SyntheticRuntime.calls == []
        assert not workspace.scheduled_run_ids
        assert sid not in workspace.active_run_ids
        assert sid not in workspace.user_cancelled
        assert store.events(sid)[-1]["data"]["state"] != "running"
    finally:
        if shutdown:
            store.close()
        else:
            await workspace.close()


@pytest.mark.asyncio
async def test_pending_concurrent_duplicate_returns_reserved_without_second_admission(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Analysis:
        async def context_by_id(self, context_id):
            entered.set()
            await release.wait()
            raise LookupError("Synthetic expired context")

    workspace = Workspace(configuration(tmp_path), analysis_service=Analysis())
    sid = workspace.store.create_session()["id"]
    key = str(uuid.uuid4())
    body = {**request_payload(analysis_context_id=A), "client_request_id": key}
    task = asyncio.create_task(workspace.prompt(sid, **body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = await workspace.prompt(sid, **body)
        assert duplicate["status"] == "reserved"
        assert duplicate["replayed"] is True
        with pytest.raises(PromptRequestConflict):
            await workspace.prompt(sid, **{**body, "language": "vi"})
        release.set()
        rejected = await task
        assert rejected["status"] == "rejected"
        assert workspace.store.runs(sid) == []
        assert not any(e["type"] == "message" for e in workspace.store.events(sid))
        assert (await workspace.prompt(sid, **body)) == {**rejected, "replayed": True}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await workspace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "failure", "cancelled"])
async def test_steering_replay_survives_terminal_target_and_uncertain_delivery(tmp_path, outcome):
    workspace = Workspace(configuration(tmp_path))
    sid = workspace.store.create_session()["id"]
    rid, _ = workspace.store.begin_run(sid)
    workspace.tasks[sid] = asyncio.create_task(asyncio.Event().wait())
    workspace.active_run_ids[sid] = rid
    workspace.active_contexts[sid] = None
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def steer(text, *, instruction_id):
        calls.append(instruction_id)
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise OSError("Synthetic response loss after provider admission")
        return {"accepted": True, "mode": "synthetic"}

    workspace.active_runtimes[sid] = SimpleNamespace(accepting_steering=True, steer=steer)
    key = str(uuid.uuid4())
    body = {**request_payload(steer_run_id=rid), "client_request_id": key}
    task = asyncio.create_task(workspace.prompt(sid, **body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending = await workspace.prompt(sid, **body)
        assert pending["status"] == "reserved"
        assert pending["run_id"] == rid
        assert pending["instruction_id"] == calls[0]
        if outcome == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            receipt = workspace.store.prompt_request(sid, key)
        else:
            release.set()
            receipt = await task
        assert receipt["status"] == ("accepted" if outcome == "accepted" else "uncertain")
        workspace.store.finish_run(rid, "completed")
        workspace.steering_closed.add(rid)
        workspace.store.stop_session(sid, "Synthetic stop")
        assert await workspace.prompt(sid, **body) == {**receipt, "replayed": True}
        assert len(calls) == 1
        assert not workspace.store.has_queued_instruction(sid)
        assert workspace.store.db.execute("SELECT count(*) FROM steering").fetchone()[0] == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await workspace.close()


@pytest.mark.parametrize("side_effect", [False, True])
def test_crashed_reservation_is_uncertain_and_never_enters_future_run(tmp_path, side_effect):
    store = SessionStore(tmp_path)
    sid = store.create_session()["id"]
    key = str(uuid.uuid4())
    payload = request_payload()
    store.reserve_prompt_request(sid, key, payload)
    if side_effect:
        store.mark_prompt_request_effects(sid, key)
        store.queue_instruction(sid, payload["text"], client_request_id=key)
    store.close()
    restored = SessionStore(tmp_path)
    try:
        restored.recover_interrupted()
        receipt = restored.prompt_request(sid, key)
        assert receipt["status"] == "uncertain"
        assert receipt["code"] == "admission_interrupted"
        inserted, replay = restored.reserve_prompt_request(sid, key, payload)
        assert inserted is False
        assert replay == receipt
        assert not restored.has_queued_instruction(sid)
        assert restored.next_queued_run(sid) is None
        rid, _ = restored.begin_run(sid)
        assert restored.take_instructions(sid, rid, include_unresolved=True) == []
        if side_effect:
            assert restored.session(sid)["pending_instructions"] == 1
    finally:
        restored.close()


def test_additive_migration_keeps_history_and_receipt_in_sqlite_backup(tmp_path):
    database = tmp_path / "workspace.sqlite3"
    sid = uuid.uuid4().hex
    history = [{"role": "user", "content": "Existing history remains complete"}]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE sessions(id TEXT PRIMARY KEY,created TEXT NOT NULL,history TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO sessions VALUES(?,?,?)", (sid, "old", json.dumps(history)))
    store = SessionStore(tmp_path)
    key = str(uuid.uuid4())
    try:
        store.reserve_prompt_request(sid, key, request_payload())
        receipt = store.finish_prompt_request(sid, key, "rejected", {"message": "Fixture"})
        copy = tmp_path / "backup"
        copy.mkdir()
        with sqlite3.connect(copy / "workspace.sqlite3") as destination:
            store.db.backup(destination)
        restored = SessionStore(copy)
        try:
            assert restored.history(sid) == history
            assert restored.prompt_request(sid, key) == receipt
            assert "client_request_id" in {
                row[1] for row in restored.db.execute("PRAGMA table_info(steering)")
            }
        finally:
            restored.close()
    finally:
        store.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"client_request_id": None},
        {"client_request_id": "not-a-uuid"},
        {"text": 123},
        {"text": " "},
        {"language": "fr"},
        {"request_origin": []},
        {"analysis_context_id": "../other"},
        {"steer_run_id": "not-a-run"},
        {"reply_to": {"run_id": "a" * 32, "message_id": True}},
        {"action_intent": "delete"},
        {"extra": "rejected"},
    ],
)
def test_invalid_webmcp_payload_has_no_admission_or_paid_effect(tmp_path, changes):
    application = create_app(configuration(tmp_path), runtime_factory=SyntheticRuntime)
    with TestClient(application) as client:
        sid = client.post("/api/sessions").json()["id"]
        key = str(uuid.uuid4())
        body = {**request_payload(), "client_request_id": key, **changes}
        response = client.post(f"/api/sessions/{sid}/prompt", json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["client_request_id"] == body["client_request_id"]
        store = application.state.workspace.store
        assert store.runs(sid) == []
        assert store.events(sid) == []
        assert store.db.execute("SELECT count(*) FROM prompt_requests").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_cloud_checkpoints_reservation_and_effect_before_steer_and_ack(tmp_path):
    workspace = Workspace(configuration(tmp_path))
    workspace.store.close()
    snapshots = []

    class Bridge:
        failed = False

        def checkpoint(self, *, force=False):
            with sqlite3.connect(workspace.state_dir / "workspace.sqlite3") as connection:
                snapshots.append(
                    connection.execute(
                        "SELECT state,effects_started FROM prompt_requests"
                    ).fetchall()
                )

    workspace.store = CloudSessionStore(workspace.state_dir, Bridge())
    sid = workspace.store.create_session()["id"]
    rid, _ = workspace.store.begin_run(sid)
    workspace.tasks[sid] = asyncio.create_task(asyncio.Event().wait())
    workspace.active_run_ids[sid] = rid
    workspace.active_contexts[sid] = None

    async def steer(text, *, instruction_id):
        assert snapshots[0] == [("reserved", 0)]
        assert snapshots[-1] == [("reserved", 1)]
        return {"accepted": True}

    workspace.active_runtimes[sid] = SimpleNamespace(accepting_steering=True, steer=steer)
    try:
        receipt = await workspace.prompt(
            sid, **request_payload(steer_run_id=rid), client_request_id=str(uuid.uuid4())
        )
        assert receipt["status"] == "accepted"
        assert snapshots[-1] == [("accepted", 1)]
        assert workspace.store.events(sid)[-1]["type"] == "prompt_receipt"
    finally:
        await workspace.close()


@pytest.mark.asyncio
async def test_failed_cloud_reservation_checkpoint_never_starts_paid_work(tmp_path):
    workspace = Workspace(configuration(tmp_path))
    workspace.store.close()

    class Bridge:
        failed = True

        def checkpoint(self, *, force=False):
            raise PersistenceUnavailable("Synthetic storage failure")

    workspace.store = CloudSessionStore(workspace.state_dir, Bridge())
    sid = workspace.store.create_session()["id"]
    key = str(uuid.uuid4())
    try:
        for _ in range(2):
            with pytest.raises(PersistenceUnavailable):
                await workspace.prompt(sid, **request_payload(), client_request_id=key)
        assert workspace.tasks == {}
        assert workspace.store.runs(sid) == []
        assert workspace.store.events(sid) == []
        assert workspace.store.prompt_request(sid, key)["status"] == "reserved"
    finally:
        await workspace.close()
