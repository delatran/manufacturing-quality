"""The live compatibility command must preserve honest cancellation evidence."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import Model as SDKModel
from analyst.config import Settings
from analyst.store import SessionStore
from scripts import check_managed_agents as command


def test_cancelled_compatibility_check_retains_reservation_and_session(tmp_path, monkeypatch):
    settings = Settings(
        root=tmp_path,
        state_dir="state",
        api_key="fixture",
        mode="live",
        unbounded_runs=False,
        max_cost_usd_per_run=0.30,
    )
    initial = SessionStore(tmp_path / "state")
    initial.db.close()
    monkeypatch.setattr(command.Settings, "from_env", lambda *_: settings)

    async def close():
        pass

    class Model(SDKModel):
        def __init__(self, settings, emit, *, budget, session_budget_usd):
            self.session_id = "asess_cancelled_fixture"
            self.client = SimpleNamespace(close=close)
            budget.update(cost_usd=session_budget_usd, unresolved_generation_requests=1)

        async def get_response(self, *args, **kwargs):
            raise AssertionError("The command test intercepts Runner before model invocation")

        async def stream_response(self, *args, **kwargs):
            raise AssertionError("The command test intercepts Runner before model invocation")
            yield

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(command, "ManagedAgentsModel", Model)
    monkeypatch.setattr(command.Runner, "run", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(command.check(True))

    store = SessionStore(tmp_path / "state")
    try:
        run = store.db.execute("SELECT state,result FROM runs").fetchone()
        result = json.loads(run["result"])
        assert run["state"] == "cancelled"
        assert result["status"] == "cancelled"
        assert result["provider_session_id"] == "asess_cancelled_fixture"
        assert result["unresolved_generation_requests"] == 1
        assert result["reservation_retained"] is True
        assert result["fallback_dispatched"] is False
        reservation = store.db.execute("SELECT amount,settled FROM budget_reservations").fetchone()
        assert reservation["amount"] == 0.30
        assert reservation["settled"] == 0
        assert store.remaining_budget(settings.dev_api_budget_usd) == 9.70
    finally:
        store.db.close()
