"""Application execution policy reaches real SDK and product-tool boundaries."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analyst.config import Settings
from analyst.model import BudgetExceeded
from analyst.runtime import AnalystRuntime, FixtureModel, RunLimitExceeded
from analyst.tools import AnalystTools


async def discard(*_):
    pass


def test_live_factory_uses_managed_api_and_preserves_role_steering_and_shared_budget(
    tmp_path,
    monkeypatch,
):
    calls = []

    class Managed:
        def __init__(self, settings, emit, **kwargs):
            calls.append((settings, emit, kwargs))

    monkeypatch.setattr("analyst.runtime.ManagedAgentsModel", Managed)
    settings = Settings(root=tmp_path, mode="live", api_key="offline-fixture")
    runtime = AnalystRuntime(settings, SimpleNamespace(), discard, steering=lambda: [])
    lead, qa = runtime.model("Lead Analyst"), runtime.model("QA Verifier")
    assert runtime.models == [lead, qa]
    assert [call[2]["role"] for call in calls] == ["Lead Analyst", "QA Verifier"]
    assert all(call[2]["budget"] is runtime.budget for call in calls)
    assert calls[0][2]["steering"] is not None
    assert calls[1][2]["steering"] is None
    assert settings.model == "gpt-6-luna"


def test_offline_simulation_keeps_fixture_transport(tmp_path):
    runtime = AnalystRuntime(Settings(root=tmp_path, mode="mock"), SimpleNamespace(), discard)
    assert isinstance(runtime.model("Lead Analyst"), FixtureModel)


def test_unbounded_product_dispatch_ignores_legacy_cost_count_and_deadline(tmp_path):
    async def scenario():
        tools = AnalystTools(tmp_path / "run", [])
        settings = Settings(
            root=tmp_path,
            mode="live",
            api_key="offline-fixture",
            unbounded_runs=True,
            max_tool_calls=1,
            max_cost_usd_per_run=0.01,
            run_timeout_seconds=0.001,
            tool_timeout_seconds=0.001,
        )
        runtime = AnalystRuntime(settings, tools, discard)
        runtime.budget["cost_usd"] = 100.0
        runtime.tool_calls = 1000
        runtime.started_at = time.monotonic() - 1000
        name = runtime.product_tools.schemas[0]["name"]

        async def call(*_):
            await asyncio.sleep(0.02)
            return {"ok": True, "observed": "completed after the historical deadline"}, None

        runtime.product_tools.call = call
        try:
            await runtime._call(name, {})
            assert tools.ledger.records()[-1]["result"]["ok"] is True
            assert runtime.tool_calls == 1001
            runtime.settings = replace(settings, unbounded_runs=False)
            with pytest.raises(BudgetExceeded):
                await runtime._call(name, {})
            runtime.budget["cost_usd"] = 0
            with pytest.raises(RunLimitExceeded):
                await runtime._call(name, {})
        finally:
            await tools.close()

    asyncio.run(scenario())
