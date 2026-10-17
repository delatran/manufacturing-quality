"""Independent cross-boundary release checks without provider requests."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from analyst.app import Workspace, create_app
from analyst.config import Settings
from analyst.store import SessionStore
from fastapi.testclient import TestClient


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Expected offline integration boundary was not reached")


def test_unbounded_public_contract_uses_json_null_and_preserves_model_identity():
    settings = Settings()
    settings.validate()
    public = settings.public_config()
    assert settings.agent_backend == public["agent_backend"] == "agents_api"
    assert settings.unbounded_runs is public["unbounded_runs"] is True
    assert public["model"] == "gpt-6-luna"
    for name in (
        "max_cost_usd_per_run",
        "dev_api_budget_usd",
        "max_output_tokens",
        "max_turns",
        "max_tool_calls",
        "max_hosted_tool_calls",
        "qa_max_retries",
        "run_timeout_seconds",
    ):
        assert settings.run_limit(name) is public[name] is None
    encoded = json.dumps(public, allow_nan=False)
    assert "Infinity" not in encoded and "NaN" not in encoded


def test_unbounded_http_availability_preserves_exhausted_historical_ledger(tmp_path):
    state = tmp_path / "state"
    store = SessionStore(state)
    sid = store.create_session()["id"]
    rid, _ = store.begin_run(sid)
    store.reserve_budget(rid, 10, 10)
    store.record_cost(rid, "historical-settled-provider-response", 4)
    before = {
        table: [tuple(row) for row in store.db.execute(f"SELECT * FROM {table}")]
        for table in ("budget_reservations", "costs")
    }
    store.close()
    settings = Settings(
        state_dir=str(state),
        mode="live",
        api_key="offline-key-never-dispatched",
        memory_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/config")
        assert response.status_code == 200
        config = response.json()
        assert config["ai_available"] is True
        assert config["ai_unavailable_reason"] is None
        assert config["dev_budget_remaining_usd"] is None
        assert config["limits"]["max_cost_usd_per_run"] is None
        json.dumps(config, allow_nan=False)
    reopened = SessionStore(state)
    try:
        after = {
            table: [tuple(row) for row in reopened.db.execute(f"SELECT * FROM {table}")]
            for table in before
        }
        assert after == before
    finally:
        reopened.close()


def test_owner_stop_does_not_resume_queued_dashboard_scope(tmp_path):
    first, second = "a" * 64, "b" * 64
    entered = threading.Event()

    class Analysis:
        async def context_by_id(self, context_id):
            return {
                "context": {
                    "context_id": context_id,
                    "snapshot_id": "offline",
                    "filters": {"category": context_id[0]},
                },
                "evidence": {},
                "links": {},
            }

        async def input_paths_for_context(self, context_id):
            return []

    class Runtime:
        calls = []

        def __init__(self, settings, toolkit, emit, analysis_context=None, **kwargs):
            self.context_id = analysis_context["context"]["context_id"]

        async def run(self, prompt):
            self.calls.append((prompt, self.context_id))
            entered.set()
            await asyncio.Future()

    settings = Settings(state_dir=str(tmp_path / "state"), mode="mock", memory_enabled=False)
    app = create_app(settings, runtime_factory=Runtime, analysis_service=Analysis())
    with TestClient(app) as client:
        sid = client.post("/api/sessions").json()["id"]
        endpoint = f"/api/sessions/{sid}"
        assert (
            client.post(
                endpoint + "/prompt", json={"text": "FIRST", "analysis_context_id": first}
            ).status_code
            == 200
        )
        wait_until(entered.is_set)
        assert client.post(
            endpoint + "/prompt", json={"text": "QUEUED", "analysis_context_id": second}
        ).json()["queued"]
        running = client.get(endpoint).json()
        assert len(running["runs"]) == 1
        assert running["runs"][0]["analysis_context_id"] == first
        rid = running["runs"][0]["id"]
        assert client.post(endpoint + "/cancel", json={"run_id": rid}).json()["cancel_requested"]
        wait_until(lambda: client.get(endpoint).json()["runs"][0]["state"] == "cancelled")
        # An independent subsequent request lets the server finish cancellation cleanup.
        assert client.get("/health").status_code == 200
        stopped = client.get(endpoint).json()
        assert Runtime.calls == [("FIRST", first)]
        assert len(stopped["runs"]) == 1
        assert stopped["runs"][0]["analysis_context_id"] == first
        assert stopped["pending_instructions"] == 1


@pytest.mark.asyncio
async def test_cancel_after_managed_boundary_preserves_unknown_billing(tmp_path):
    entered = asyncio.Event()

    class Runtime:
        def __init__(self, settings, toolkit, emit, **kwargs):
            self.budget = {"cost_usd": 0.12, "unresolved_generation_requests": 0}
            self.models = [
                SimpleNamespace(
                    usage=[
                        {
                            "provider_api": "agents",
                            "provider_session_id": "sess_accounted",
                            "provider_turn_id": "turn_accounted",
                            "usage_complete": False,
                            "usage_is_best_effort": True,
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "cost_usd": 0.12,
                            "actual_cost_usd": None,
                            "billing_settled": False,
                        }
                    ]
                )
            ]

        async def run(self, prompt):
            entered.set()
            await asyncio.Future()

    workspace = Workspace(
        Settings(state_dir=str(tmp_path / "state"), mode="mock", memory_enabled=False),
        runtime_factory=Runtime,
    )
    sid = workspace.store.create_session()["id"]
    task = asyncio.create_task(workspace._run(sid, "Continue checking the saved evidence"))
    workspace.tasks[sid] = task
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (await workspace.cancel(sid))["cancel_requested"]
        with pytest.raises(asyncio.CancelledError):
            await task
        result = json.loads(
            workspace.store.db.execute(
                "SELECT result FROM runs WHERE session_id=?", (sid,)
            ).fetchone()[0]
        )
        assert result["status"] == "cancelled"
        assert result["usage_complete"] is False
        assert result["cost_usd"] is None
        assert result["known_accounted_cost_usd"] == 0.12
        assert result["usage"][0]["provider_session_id"] == "sess_accounted"
        assert result["usage"][0]["actual_cost_usd"] is None
    finally:
        await workspace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bind_run_id", [False, True])
async def test_stop_at_completed_event_boundary_does_not_start_queued_run(tmp_path, bind_run_id):
    entered, release, terminal, finish, second = (asyncio.Event() for _ in range(5))

    class Runtime:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt):
            self.calls.append(prompt)
            if len(self.calls) == 1:
                entered.set()
                await release.wait()
                return {
                    "status": "completed",
                    "report": "Offline completed report",
                    "qa": {"verified": True, "errors": []},
                    "usage": [],
                    "usage_complete": True,
                    "cost_usd": 0.25,
                }
            second.set()
            await asyncio.Future()

    workspace = Workspace(
        Settings(state_dir=str(tmp_path / "state"), mode="mock", memory_enabled=False),
        runtime_factory=Runtime,
    )
    original_emit = workspace.emit

    async def emit(sid, rid, kind, data):
        if kind == "usage" and data.get("scope") == "run" and len(Runtime.calls) == 1:
            terminal.set()
            await finish.wait()
        return await original_emit(sid, rid, kind, data)

    workspace.emit = emit
    sid = workspace.store.create_session()["id"]
    try:
        await workspace.prompt(sid, "FIRST")
        await asyncio.wait_for(entered.wait(), 5)
        await workspace.prompt(sid, "QUEUED", language="vi")
        first_task = workspace.tasks[sid]
        release.set()
        await asyncio.wait_for(terminal.wait(), 5)
        run = workspace.store.session(sid)["runs"][0]
        assert run["state"] == "completed"
        saved_result = json.loads(
            workspace.store.db.execute(
                "SELECT result FROM runs WHERE id=?", (run["id"],)
            ).fetchone()[0]
        )
        assert saved_result["report"] == "Offline completed report"
        with pytest.raises(ValueError, match="not the active run"):
            await workspace.cancel(sid, "different-run-id")
        assert not first_task.done()
        result = await workspace.cancel(sid, run["id"] if bind_run_id else None)
        assert result["cancel_requested"] is True
        assert result["run_id"] == run["id"]
        try:
            await first_task
        except asyncio.CancelledError:
            pass
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second.wait(), 0.05)
        assert Runtime.calls == ["FIRST"]
        assert workspace.store.session(sid)["pending_instructions"] == 1
        assert workspace.store.session(sid)["runs"][0]["state"] == "completed"
        assert (
            json.loads(
                workspace.store.db.execute(
                    "SELECT result FROM runs WHERE id=?", (run["id"],)
                ).fetchone()[0]
            )
            == saved_result
        )
    finally:
        finish.set()
        await workspace.close()


@pytest.mark.asyncio
async def test_previous_completed_run_id_cannot_cancel_followup_run(tmp_path):
    entered, release, second = (asyncio.Event() for _ in range(3))

    class Runtime:
        calls = []

        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt):
            self.calls.append(prompt)
            if len(self.calls) == 1:
                entered.set()
                await release.wait()
                return {"status": "completed", "usage": [], "cost_usd": 0}
            second.set()
            await asyncio.Future()

    workspace = Workspace(
        Settings(state_dir=str(tmp_path / "state"), mode="mock", memory_enabled=False),
        runtime_factory=Runtime,
    )
    sid = workspace.store.create_session()["id"]
    try:
        await workspace.prompt(sid, "FIRST")
        await asyncio.wait_for(entered.wait(), 5)
        old_id = workspace.store.session(sid)["runs"][0]["id"]
        await workspace.prompt(sid, "SECOND", language="vi")
        release.set()
        await asyncio.wait_for(second.wait(), 5)
        current_task = workspace.tasks[sid]
        current_run = workspace.store.session(sid)["runs"][-1]
        assert current_run["state"] == "running" and current_run["id"] != old_id
        with pytest.raises(ValueError, match="not the active run"):
            await workspace.cancel(sid, old_id)
        assert not current_task.done()
        assert (await workspace.cancel(sid, current_run["id"]))["cancel_requested"]
        with pytest.raises(asyncio.CancelledError):
            await current_task
    finally:
        await workspace.close()
