"""HTTP ownership and terminal cleanup through native, network-free runs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from analyst.app import Workspace
from analyst.config import Settings
from analyst.managed_agents import ManagedAgentsModel
from analyst.runtime import AnalystRuntime, QAReview, ReportDraft
from analyst.tools import AnalystTools
from openai import BadRequestError
from test_managed_agents import Stream, event, native_completed_provider

ROOT = Path(__file__).resolve().parents[1]


async def discard(*_):
    pass


@pytest.fixture
def clients(monkeypatch):
    control = SimpleNamespace(
        outcome="completed",
        clients=[],
        opened=[],
        entered=asyncio.Event(),
        cancel_entered=asyncio.Event(),
        cancel_release=asyncio.Event(),
        close_entered=asyncio.Event(),
        close_release=asyncio.Event(),
        on_close=lambda _: None,
    )
    control.cancel_release.set()
    control.close_release.set()

    class Client:
        def __init__(self, **kwargs):
            index = len(control.clients)
            self.close_count = 0
            output = (
                ReportDraft(report="The data is ready for analysis.").model_dump_json()
                if index == 0
                else QAReview(verified=True, errors=[], suggestions=[]).model_dump_json()
            )
            self.provider = native_completed_provider(f"sess_cleanup_{index}", [output])
            original_create = self.provider.create
            original_input = self.provider.events.create

            async def create(**arguments):
                if index == 0 and control.outcome in {"rejected", "uncertain"}:
                    self.provider.creates.append(arguments)
                    if control.outcome == "uncertain":
                        raise ConnectionError("Unknown managed creation outcome")
                    raise BadRequestError(
                        "Fixture schema rejected",
                        response=httpx2.Response(
                            400, request=httpx2.Request("POST", "https://fixture.invalid")
                        ),
                        body={"code": "invalid_json_schema", "param": "agent.text.format.schema"},
                    )
                stream = await original_create(**arguments)
                if index == 0 and control.outcome == "cancelled":

                    class WaitingStream(Stream):
                        async def __aiter__(self):
                            yield event("agent.session.created", session=self.events[0].session)
                            control.entered.set()
                            await asyncio.Future()

                    stream = WaitingStream(stream.events)
                control.opened.append(stream)
                return stream

            async def add_input(*args, **kwargs):
                if any(
                    item["type"] == "agent.session.input.cancel"
                    for item in kwargs.get("events", [])
                ):
                    control.cancel_entered.set()
                    await control.cancel_release.wait()
                await original_input(*args, **kwargs)

            self.provider.create = create
            self.provider.events.create = add_input
            self.beta = SimpleNamespace(agents=SimpleNamespace(sessions=self.provider))
            self.view = SimpleNamespace(beta=self.beta)
            control.clients.append(self)

        def with_options(self, **kwargs):
            assert kwargs["timeout"].read == 60
            # This wrapper has no close method. Both wrappers use the same
            # transport; only the model's original ownership handle closes it.
            return self.view

        async def close(self):
            assert all(stream.closed for stream in control.opened)
            control.close_entered.set()
            await control.close_release.wait()
            control.on_close(self)
            self.close_count += 1

    monkeypatch.setattr("analyst.model.AsyncOpenAI", Client)
    control.factory = Client
    return control


def settings(tmp_path):
    return Settings(
        root=ROOT,
        mode="live",
        api_key="offline-fixture",
        memory_enabled=False,
        state_dir=str(tmp_path / "state"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "rejected", "uncertain", "cancelled"])
@pytest.mark.parametrize("workspace_owned", [False, True])
@pytest.mark.parametrize("injected", [False, True])
async def test_native_run_releases_owned_clients_after_terminal_reconciliation(
    tmp_path, clients, outcome, workspace_owned, injected, monkeypatch
):
    clients.outcome = outcome
    configuration = settings(tmp_path)
    if injected:

        class InjectedModel(ManagedAgentsModel):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, client=clients.factory(), **kwargs)

        monkeypatch.setattr("analyst.runtime.ManagedAgentsModel", InjectedModel)
    workspace = toolkit = runtime = None
    if workspace_owned:
        workspace = Workspace(configuration)
        sid = workspace.store.create_session()["id"]
        observed_states = []

        def after_persistence(client):
            row = workspace.store.db.execute(
                "SELECT id,state,result FROM runs WHERE session_id=?", (sid,)
            ).fetchone()
            state = (
                "completed"
                if outcome == "completed"
                else "cancelled"
                if outcome == "cancelled"
                else "failed"
            )
            assert row["state"] == state
            settled = workspace.store.db.execute(
                "SELECT settled FROM budget_reservations WHERE run_id=?", (row["id"],)
            ).fetchone()[0]
            # Existing accounting conservatively retains the reservation after
            # this managed schema rejection as well as uncertain/cancelled work.
            assert bool(settled) is (outcome == "completed")
            observed_states.append(row["state"])

        clients.on_close = after_persistence
        await workspace.prompt(sid, "Say whether the data is ready for analysis.")
        task = workspace.tasks[sid]
    else:
        toolkit = AnalystTools(tmp_path / "run", [], memory_enabled=False)
        runtime = AnalystRuntime(configuration, toolkit, discard)
        task = asyncio.create_task(runtime.run("Say whether the data is ready for analysis."))
    try:
        if outcome == "cancelled":
            await asyncio.wait_for(clients.entered.wait(), 10)
            if workspace_owned:
                await workspace.cancel(sid)
            else:
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif workspace_owned:
            await task
        else:
            result = await task
            assert result["status"] == ("completed" if outcome == "completed" else "failed")
            if outcome == "uncertain":
                assert result["usage_complete"] is False and result["cost_usd"] is None
                assert runtime.budget["unresolved_generation_requests"] == 1
        assert clients.clients and all(
            client.close_count == int(not injected) for client in clients.clients
        )
        assert all(len(client.provider.creates) == 1 for client in clients.clients)
        cancellations = [
            item
            for client in clients.clients
            for call in client.provider.inputs
            for item in call["events"]
            if item["type"] == "agent.session.input.cancel"
        ]
        assert len(cancellations) == int(outcome == "cancelled")
        if workspace_owned:
            assert len(observed_states) == (0 if injected else len(clients.clients))
            rid = workspace.store.runs(sid)[-1]["id"]
            result = json.loads(
                workspace.store.db.execute("SELECT result FROM runs WHERE id=?", (rid,)).fetchone()[
                    0
                ]
            )
            if outcome in {"uncertain", "cancelled"}:
                assert result["usage_complete"] is False and result["cost_usd"] is None
            assert sid not in workspace.active_runtimes
        else:
            await runtime.aclose()
            assert all(client.close_count == int(not injected) for client in clients.clients)
    finally:
        if workspace is not None:
            await workspace.close()
        if toolkit is not None:
            await toolkit.close()
        if injected:
            clients.on_close = lambda _: None
            for client in clients.clients:
                await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_owned", [False, True])
async def test_setup_failure_after_model_allocation_releases_owned_client(
    tmp_path, clients, monkeypatch, workspace_owned
):
    def fail_tools(self):
        raise ValueError("Fixture tool schema setup failed")

    monkeypatch.setattr(AnalystRuntime, "sdk_tools", fail_tools)
    if workspace_owned:
        workspace = Workspace(settings(tmp_path))
        sid = workspace.store.create_session()["id"]

        def after_failure(_):
            assert workspace.store.runs(sid)[-1]["state"] == "failed"

        clients.on_close = after_failure
        try:
            await workspace.prompt(sid, "Inspect current scope")
            await workspace.tasks[sid]
            assert sid not in workspace.active_runtimes
        finally:
            await workspace.close()
    else:
        toolkit = AnalystTools(tmp_path / "run", [], memory_enabled=False)
        try:
            runtime = AnalystRuntime(settings(tmp_path), toolkit, discard)
            with pytest.raises(ValueError, match="tool schema setup"):
                await runtime.run("Inspect current scope")
        finally:
            await toolkit.close()
    assert len(clients.clients) == 1
    assert clients.clients[0].close_count == 1 and clients.clients[0].provider.creates == []


@pytest.mark.asyncio
async def test_repeated_stop_drains_existing_provider_cancel_before_client_disposal(
    tmp_path, clients
):
    clients.outcome = "cancelled"
    clients.cancel_release.clear()
    workspace = Workspace(settings(tmp_path))
    sid = workspace.store.create_session()["id"]
    try:
        await workspace.prompt(sid, "Inspect current scope")
        task = workspace.tasks[sid]
        await asyncio.wait_for(clients.entered.wait(), 10)
        await workspace.cancel(sid)
        await asyncio.wait_for(clients.cancel_entered.wait(), 10)
        await workspace.cancel(sid)
        # Closing must wait for the already-started reconciliation, even when
        # another Stop interrupts its original shielded await.
        await asyncio.sleep(0)
        assert not task.done() and clients.clients[0].close_count == 0
        clients.cancel_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert clients.clients[0].close_count == 1
        cancellations = clients.clients[0].provider.inputs
        assert len(cancellations) == 1
        assert workspace.store.runs(sid)[-1]["state"] == "cancelled"
    finally:
        clients.cancel_release.set()
        await workspace.close()


@pytest.mark.asyncio
async def test_stop_during_client_disposal_retains_finished_state_and_queued_work(
    tmp_path, clients
):
    clients.close_release.clear()
    workspace = Workspace(settings(tmp_path))
    sid = workspace.store.create_session()["id"]
    try:
        await workspace.prompt(sid, "First analysis")
        task = workspace.tasks[sid]
        await asyncio.wait_for(clients.close_entered.wait(), 10)
        assert workspace.store.runs(sid)[-1]["state"] == "completed"
        assert (await workspace.prompt(sid, "Next analysis"))["queued"]
        await workspace.cancel(sid)
        await asyncio.sleep(0)
        assert not task.done() and all(client.close_count == 0 for client in clients.clients)
        clients.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(client.close_count == 1 for client in clients.clients)
        assert sid not in workspace.active_runtimes and sid not in workspace.user_cancelled
        assert len(workspace.store.runs(sid)) == 1
        assert workspace.store.session(sid)["pending_instructions"] == 1
        assert workspace.store.runs(sid)[-1]["state"] == "completed"
        assert not any(client.provider.inputs for client in clients.clients)
    finally:
        clients.close_release.set()
        await workspace.close()


@pytest.mark.asyncio
async def test_external_falsey_client_is_never_replaced_or_closed(tmp_path, clients):
    class External:
        close_count = 0

        def __bool__(self):
            return False

        async def close(self):
            self.close_count += 1

    external = External()
    model = ManagedAgentsModel(settings(tmp_path), discard, client=external)
    assert model.client is external and clients.clients == []
    await asyncio.gather(model.aclose(), model.aclose())
    assert external.close_count == 0


@pytest.mark.asyncio
async def test_concurrent_owned_close_releases_one_transport(tmp_path, clients):
    model = ManagedAgentsModel(settings(tmp_path), discard)
    assert model.client is clients.clients[0].view
    await asyncio.gather(model.aclose(), model.aclose(), model.aclose())
    assert clients.clients[0].close_count == 1 and clients.clients[0].provider.creates == []


@pytest.mark.parametrize("amount", [0, -1, float("inf"), float("nan")])
def test_invalid_managed_budget_is_rejected_before_client_allocation(tmp_path, clients, amount):
    with pytest.raises(ValueError, match="finite and positive"):
        ManagedAgentsModel(settings(tmp_path), discard, session_budget_usd=amount)
    assert clients.clients == []
