"""Run-targeted details steer existing work without entering a future-run queue."""

import asyncio
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from analyst.app import Workspace, create_app
from analyst.config import Settings
from analyst.store import SessionStore
from fastapi.testclient import TestClient

A, B = "a" * 64, "b" * 64


class ScopedAnalysis:
    def __init__(self, root):
        self.paths = {}
        for cid in (A, B):
            path = root / f"{cid[0]}.csv"
            path.write_text("value\n1\n", encoding="utf-8")
            self.paths[cid] = path

    async def context_by_id(self, cid):
        if cid not in self.paths:
            raise LookupError(cid)
        return {
            "context": {"context_id": cid, "filters": {}, "snapshot_id": "synthetic"},
            "evidence": {},
            "links": {},
        }

    async def input_paths_for_context(self, cid):
        return [self.paths[cid]]


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Synthetic steering boundary was not reached")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original,requested,prompt",
    [("vi", "en", "Please answer in English."), ("en", "vi", "Trả lời bằng tiếng Việt.")],
)
async def test_explicit_language_change_queues_without_mutating_active_work(
    tmp_path, original, requested, prompt
):
    config = Settings(mode="mock", state_dir=str(tmp_path / "state"), memory_enabled=False)
    workspace = Workspace(config, analysis_service=ScopedAnalysis(tmp_path))
    sid = workspace.store.create_session()["id"]
    rid, _ = workspace.store.begin_run(sid, A, original, "draft_update")
    release = asyncio.Event()
    workspace.tasks[sid] = asyncio.create_task(release.wait())
    workspace.active_run_ids[sid] = rid
    workspace.active_contexts[sid] = A
    workspace.active_languages[sid] = original
    workspace.active_action_intents[sid] = "draft_update"
    workspace._bind_ui_scope(sid, {"context": {"context_id": B}})
    details = []

    async def steer(text, *, instruction_id):
        details.append(text)
        return {"accepted": True}

    workspace.active_runtimes[sid] = SimpleNamespace(accepting_steering=True, steer=steer)
    try:
        result = await workspace.prompt(sid, prompt, steer_run_id=rid)
        assert result == {"queued": True}
        assert details == []
        assert workspace.store.run_language(rid) == original
        assert workspace.active_languages[sid] == original
        assert workspace.active_action_intents[sid] == "draft_update"
        assert workspace.store.investigation(sid)["ui"]["context_id"] == B
        expected = {"analysis_context_id": A, "language": requested, "action_intent": "none"}
        assert workspace.store.next_queued_run(sid) == expected
        persisted = SessionStore(workspace.state_dir)
        try:
            assert persisted.next_queued_run(sid) == expected
        finally:
            persisted.close()
        queued = workspace.store.events(sid)[-1]
        assert queued["data"]["state"] == "queued"
        assert queued["data"]["language"] == requested
    finally:
        release.set()
        await workspace.close()


@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_detail_targets_active_run_and_preserves_frozen_tuple_and_dashboard(tmp_path, transport):
    entered, release = threading.Event(), threading.Event()

    class Runtime:
        calls = []
        accepting_steering = True

        def __init__(self, settings, toolkit, emit, steering=None, **options):
            self.emit, self.consume = emit, steering
            self.options, self.details = options, []
            self.calls.append(self)

        async def steer(self, text, *, instruction_id):
            self.details.append((text, instruction_id))
            await self.emit("steering_applied", {"message": text, "mode": "fixture"})
            return {"accepted": True, "mode": "native_inflight"}

        async def run(self, prompt):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            assert await self.consume() == [], "Direct details must not be injected again"
            return {"status": "completed", "usage": [], "cost_usd": 0}

    settings = Settings(mode="mock", state_dir=str(tmp_path / "state"), memory_enabled=False)
    app = create_app(settings, runtime_factory=Runtime, analysis_service=ScopedAnalysis(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions").json()["id"]
        url = f"/api/sessions/{sid}/prompt"
        client.post(
            url,
            json={
                "text": "Phân tích phạm vi này",
                "analysis_context_id": A,
                "language": "vi",
                "action_intent": "draft_create",
            },
        ).raise_for_status()
        wait_for(entered.is_set)
        workspace = app.state.workspace
        workspace._bind_ui_scope(sid, {"context": {"context_id": B}})
        rid = client.get(f"/api/sessions/{sid}").json()["runs"][0]["id"]
        payload = {
            "text": "Focus on Critical severity instead",
            "steer_run_id": rid,
            "analysis_context_id": B,
            "language": "en",
            "action_intent": "none",
        }
        if transport == "http":
            response = client.post(url, json=payload)
            response.raise_for_status()
            assert response.json()["queued"] is False
            assert response.json()["steering"] is True
            assert response.json()["run_id"] == rid
        else:
            with client.websocket_connect(f"/api/sessions/{sid}/ws") as socket:
                socket.send_json({"type": "prompt", **payload})
                while True:
                    event = socket.receive_json()
                    if event["type"] == "steering_applied":
                        break
        session = client.get(f"/api/sessions/{sid}").json()
        assert len(session["runs"]) == len(Runtime.calls) == 1
        assert session["runs"][0]["state"] == "running"
        assert workspace.store.next_queued_run(sid) is None
        assert workspace.store.investigation(sid)["ui"]["context_id"] == B
        assert Runtime.calls[0].details[0][0] == payload["text"]
        user = [e for e in session["events"] if e["type"] == "message"][-1]
        assert user["run_id"] == rid
        assert user["data"]["analysis_context_id"] == A
        assert user["data"]["language"] == "vi"
        assert user["data"]["action_intent"] == "draft_create"
        assert any(
            e["type"] == "steering_received" and e["run_id"] == rid for e in session["events"]
        )
        assert not any(
            e["type"] == "status" and e["data"]["state"] == "queued" for e in session["events"]
        )
        release.set()
        wait_for(
            lambda: client.get(f"/api/sessions/{sid}").json()["runs"][0]["state"] == "completed"
        )
        completed = client.get(f"/api/sessions/{sid}").json()
        assert completed["pending_instructions"] == 0
        before = len(completed["events"])
        assert client.post(url, json=payload).status_code == 422
        after = client.get(f"/api/sessions/{sid}").json()
        assert len(after["events"]) == before
        assert len(after["runs"]) == 1


def test_run_bound_pending_instruction_never_becomes_a_separate_run(tmp_path):
    store = SessionStore(tmp_path)
    try:
        sid = store.create_session()["id"]
        rid, _ = store.begin_run(sid, A, "vi", "draft_update")
        instruction = store.record_run_instruction(sid, rid, "Prioritize Critical", applied=False)
        assert store.next_queued_run(sid) is None
        assert store.has_queued_instruction(sid) is False
        assert (
            store.take_instructions(
                sid, "other-run", analysis_context_id=A, language="vi", action_intent="draft_update"
            )
            == []
        )
        assert store.take_instructions(
            sid, rid, analysis_context_id=A, language="vi", action_intent="draft_update"
        ) == ["Prioritize Critical"]
        assert (
            store.take_instructions(
                sid, rid, analysis_context_id=A, language="vi", action_intent="draft_update"
            )
            == []
        )
        store.mark_instruction_unconfirmed(instruction, rid)
        store.finish_run(rid, "completed")
        assert store.session(sid)["pending_instructions"] == 1
        assert store.next_queued_run(sid) is None
        next_rid, _ = store.begin_run(sid, A, "vi", "draft_update")
        assert (
            store.take_instructions(
                sid,
                next_rid,
                include_unresolved=True,
                analysis_context_id=A,
                language="vi",
                action_intent="draft_update",
            )
            == []
        )
        assert store.session(sid)["pending_instructions"] == 1
        with pytest.raises(ValueError, match="finished"):
            store.record_run_instruction(sid, rid, "Too late", applied=True)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_finalization_rejects_details_and_uncertain_admission_stays_unresolved(tmp_path):
    settings = Settings(mode="mock", state_dir=str(tmp_path / "state"), memory_enabled=False)
    workspace = Workspace(settings, analysis_service=ScopedAnalysis(tmp_path))
    sid = workspace.store.create_session()["id"]
    rid, _ = workspace.store.begin_run(sid, A, "vi")
    release = asyncio.Event()
    workspace.tasks[sid] = asyncio.create_task(release.wait())
    workspace.active_run_ids[sid] = rid
    workspace.active_contexts[sid] = A
    workspace.active_languages[sid] = "vi"

    async def uncertain_send(text, *, instruction_id):
        raise TimeoutError("Synthetic lost provider acknowledgement")

    runtime = SimpleNamespace(accepting_steering=False, steer=uncertain_send)
    workspace.active_runtimes[sid] = runtime
    try:
        before = workspace.store.events(sid)
        with pytest.raises(ValueError, match="finished"):
            await workspace.prompt(sid, "Too late", steer_run_id=rid)
        assert workspace.store.events(sid) == before
        assert workspace.store.session(sid)["pending_instructions"] == 0
        runtime.accepting_steering = True
        with pytest.raises(ValueError, match="could not be confirmed"):
            await workspace.prompt(sid, "Refocus the analysis", steer_run_id=rid)
        session = workspace.store.session(sid)
        assert session["pending_instructions"] == 1
        assert len(session["runs"]) == 1
        assert workspace.store.next_queued_run(sid) is None
        row = workspace.store.db.execute(
            "SELECT state,run_id FROM steering WHERE session_id=?", (sid,)
        ).fetchone()
        assert tuple(row) == ("unconfirmed", rid)
        assert any(event["type"] == "steering_failed" for event in session["events"])
        assert not any(
            event["type"] == "status" and event["data"]["state"] == "queued"
            for event in session["events"]
        )
        await workspace.cancel(sid, rid)
        stopped_events = workspace.store.events(sid)
        with pytest.raises(ValueError, match="stopping"):
            await workspace.prompt(sid, "After Stop", steer_run_id=rid)
        assert workspace.store.events(sid) == stopped_events
    finally:
        await workspace.close()


def test_target_migration_distinguishes_explicit_details_from_legacy_resume(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create_session()["id"]
    rid, _ = store.begin_run(sid)
    store.queue_instruction(sid, "Legacy continuation")
    assert store.take_instructions(sid, rid) == ["Legacy continuation"]
    explicit = store.record_run_instruction(sid, rid, "Only this run", applied=True)
    store.emit(
        sid,
        rid,
        "steering_received",
        {"instruction_id": f"steering-{rid}-{explicit}"},
    )
    store.mark_instruction_unconfirmed(explicit, rid)
    store.close()
    db = sqlite3.connect(tmp_path / "workspace.sqlite3")
    db.execute("ALTER TABLE steering DROP COLUMN target_run_id")
    db.commit()
    db.close()
    restored = SessionStore(tmp_path)
    try:
        rows = restored.db.execute("SELECT text,target_run_id FROM steering ORDER BY id").fetchall()
        assert [tuple(row) for row in rows] == [
            ("Legacy continuation", None),
            ("Only this run", rid),
        ]
        assert restored.take_instructions(sid, "new-run", include_unresolved=True) == [
            "Legacy continuation"
        ]
        assert (
            restored.db.execute("SELECT run_id FROM steering WHERE id=?", (explicit,)).fetchone()[0]
            == rid
        )
        receipt = restored.db.execute(
            "SELECT data FROM events WHERE run_id=? AND type='steering_received'", (rid,)
        ).fetchone()[0]
        assert json.loads(receipt)["instruction_id"] == f"steering-{rid}-{explicit}"
    finally:
        restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("admission_fails", [False, True])
async def test_slow_native_admission_does_not_block_other_sessions(tmp_path, admission_fails):
    running, admitting, release_run, release_admission = (asyncio.Event() for _ in range(4))

    class Runtime:
        accepting_steering = True

        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt):
            if prompt == "First request":
                running.set()
                await release_run.wait()
            return {"status": "completed", "usage": [], "cost_usd": 0}

        async def steer(self, text, *, instruction_id):
            admitting.set()
            await release_admission.wait()
            if admission_fails:
                raise TimeoutError("Synthetic lost admission acknowledgement")
            return {"accepted": True, "mode": "native_inflight"}

    workspace = Workspace(
        Settings(mode="mock", state_dir=str(tmp_path / "state"), memory_enabled=False),
        runtime_factory=Runtime,
    )
    first = workspace.store.create_session()["id"]
    other = workspace.store.create_session()["id"]
    detail = None
    try:
        await workspace.prompt(first, "First request")
        await asyncio.wait_for(running.wait(), 5)
        rid = workspace.active_run_ids[first]
        detail = asyncio.create_task(workspace.prompt(first, "Refocus this run", steer_run_id=rid))
        await asyncio.wait_for(admitting.wait(), 5)
        assert workspace.store.session(first)["pending_instructions"] == 1
        assert any(event["type"] == "steering_received" for event in workspace.store.events(first))
        assert not detail.done()

        response = await asyncio.wait_for(workspace.prompt(other, "Independent request"), 2)
        assert response == {"queued": False}
        # Await the whole task, including its final admission-lock cleanup.
        await asyncio.wait_for(workspace.tasks[other], 5)
        assert workspace.store.session(other)["runs"][0]["state"] == "completed"
        assert other not in workspace.active_run_ids
        assert not detail.done()

        release_admission.set()
        if admission_fails:
            with pytest.raises(ValueError, match="could not be confirmed"):
                await detail
        else:
            receipt = await detail
            assert receipt["steering"] is True and receipt["run_id"] == rid
        assert workspace.store.next_queued_run(first) is None
        release_run.set()
        await asyncio.wait_for(workspace.tasks[first], 5)
        assert workspace.store.session(first)["pending_instructions"] == int(admission_fails)
    finally:
        release_admission.set()
        release_run.set()
        if detail is not None:
            await asyncio.gather(detail, return_exceptions=True)
        await workspace.close()
