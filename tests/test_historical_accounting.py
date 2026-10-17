"""Historical receipt reconciliation remains readable without a legacy transport."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from analyst.app import Workspace
from analyst.config import Settings
from analyst.store import SessionStore
from openai import APITimeoutError


class HistoricalReceiptReplay:
    """Replay durable receipt shapes without implementing a provider backend."""

    def __init__(self, emit, budget, phase, fail_at):
        self.emit, self.budget = emit, budget
        self.phase, self.fail_at = phase, fail_at
        self.counts = self.creates = 0
        self.usage = []
        budget.update(accounted_response_ids=[], unresolved_generation_requests=0)

    async def replay(self):
        self.counts += 1
        if self.phase == "input_token_count" and self.counts == self.fail_at:
            self.budget["blocked"] = True
            self.budget["preflight_failure"] = {
                "phase": "input_token_count",
                "generation_dispatched": False,
                "unresolved_generation_requests": 0,
                "accounted_response_ids": list(self.budget["accounted_response_ids"]),
                "cost_usd": self.budget["cost_usd"],
            }
            raise APITimeoutError(request=httpx.Request("POST", "https://offline.invalid/receipt"))
        self.creates += 1
        self.budget["unresolved_generation_requests"] += 1
        await self.emit("model_started", {"model": "gpt-6-luna"})
        if self.phase == "generation_create" and self.creates == self.fail_at:
            raise APITimeoutError(request=httpx.Request("POST", "https://offline.invalid/receipt"))
        identity = f"historical_receipt_{self.creates}"
        await self.emit("response_created", {"response_id": identity})
        record = {
            "response_id": identity,
            "provider_terminal": True,
            "provider_status": "completed",
            "model": "gpt-6-luna",
            "requested_model": "gpt-6-luna",
            "input_tokens": 20,
            "output_tokens": 2,
            "cached_tokens": 10,
            "cache_write_tokens": 5,
            "reasoning_tokens": 1,
            "cost_usd": 0.001,
        }
        self.usage.append(record)
        self.budget["cost_usd"] += record["cost_usd"]
        self.budget["accounted_response_ids"].append(identity)
        self.budget["unresolved_generation_requests"] -= 1
        await self.emit("usage", record)


@pytest.fixture
def workspace_factory(tmp_path, monkeypatch):
    def disallow_client(*args, **kwargs):
        raise AssertionError("Offline accounting tests must not create an API client")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", disallow_client)

    def create(*, phase="input_token_count", fail_at=2, storage_cost=0, mutate=None):
        transport = SimpleNamespace(creates=0)
        instances = []

        async def prepare(settings, memory, emit):
            return settings, {
                "storage_reservation_usd": storage_cost,
                "vector_store_id": "vs_disposable",
            }

        monkeypatch.setattr("analyst.knowledge.prepare_knowledge", prepare)

        class Runtime:
            def __init__(self, settings, toolkit, emit, **kwargs):
                self.budget = {"cost_usd": 0.0}
                self.settings, self.emit = settings, emit
                instances.append(self)

            async def run(self, prompt):
                self.model = HistoricalReceiptReplay(self.emit, self.budget, phase, fail_at)
                try:
                    for _ in range(fail_at):
                        await self.model.replay()
                    raise AssertionError("The selected failure must occur")
                except APITimeoutError as error:
                    qa = {"verified": False, "errors": [f"{type(error).__name__}: {error}"]}
                self.result = {
                    "status": "failed",
                    "report": "Saved draft requires QA review.",
                    "qa": qa,
                    "history": [{"role": "user", "content": prompt}],
                    "usage": list(self.model.usage),
                    "cost_usd": self.budget["cost_usd"],
                    "operational_receipt": {"draft_id": "draft_disposable", "status": "draft"},
                }
                transport.creates = self.model.creates
                if mutate:
                    mutate(workspace, self)
                await self.emit("report", self.result)
                return self.result

        settings = Settings(
            root=tmp_path,
            unbounded_runs=False,
            state_dir=str(tmp_path / "state"),
            mode="live",
            api_key="disposable-offline-test-key",
            memory_enabled=False,
            enable_file_search=False,
            max_cost_usd_per_run=5,
            dev_api_budget_usd=10,
        )
        workspace = Workspace(settings, runtime_factory=Runtime, analysis_service=SimpleNamespace())
        return workspace, transport, instances

    return create


def read_run(workspace, sid):
    row = workspace.store.db.execute(
        "SELECT * FROM runs WHERE session_id=? ORDER BY created DESC", (sid,)
    ).fetchone()
    reservation = workspace.store.db.execute(
        "SELECT * FROM budget_reservations WHERE run_id=?", (row["id"],)
    ).fetchone()
    costs = [
        dict(item)
        for item in workspace.store.db.execute("SELECT * FROM costs WHERE run_id=?", (row["id"],))
    ]
    return dict(row), json.loads(row["result"]), dict(reservation), costs


@pytest.mark.parametrize("fail_at,storage_cost", [(1, 0), (2, 0), (2, 0.5)])
def test_count_timeout_settles_only_proven_unused_budget(workspace_factory, fail_at, storage_cost):
    async def scenario():
        workspace, transport, instances = workspace_factory(
            fail_at=fail_at, storage_cost=storage_cost
        )
        sid = workspace.store.create_session()["id"]
        try:
            await workspace._run(sid, "Create a disposable draft", language="vi")
            row, result, reservation, costs = read_run(workspace, sid)
            known_cost = sum(item["amount"] for item in costs)
            assert row["state"] == result["status"] == "failed"
            assert result["qa"] == instances[0].result["qa"]
            assert result["qa"]["errors"] == ["APITimeoutError: Request timed out."]
            assert result["language"] == "vi" and result["report_number_format"] == "vi-VN"
            assert result["operational_receipt"] == {
                "draft_id": "draft_disposable",
                "status": "draft",
            }
            assert reservation["settled"] == 1
            proof = result["budget_reconciliation"]
            assert proof["eligible"] is True and "settled" not in proof
            assert proof["known_cost_usd"] == pytest.approx(known_cost)
            assert proof["unused_reservation_usd"] == pytest.approx(5 - known_cost)
            assert proof["accounted_response_ids"] == [
                entry["response_id"] for entry in result["usage"]
            ]
            assert len(proof["accounting_event_ids"]) == 3 * (fail_at - 1)
            assert transport.creates == fail_at - 1
            assert workspace.store.remaining_budget(10) == pytest.approx(10 - known_cost)
            events = workspace.store.events(sid)
            assert all(event["data"]["language"] == "vi" for event in events)
            assert events[-1]["data"]["state"] == "failed"
        finally:
            await workspace.close()

    asyncio.run(scenario())


def test_generation_timeout_without_id_retains_full_reservation(workspace_factory):
    async def scenario():
        workspace, transport, instances = workspace_factory(phase="generation_create")
        sid = workspace.store.create_session()["id"]
        try:
            await workspace._run(sid, "Fail after a dispatched generation")
            row, result, reservation, costs = read_run(workspace, sid)
            assert row["state"] == "failed" and result["qa"]["verified"] is False
            assert transport.creates == 2 and len(costs) == 1
            assert instances[0].budget["unresolved_generation_requests"] == 1
            assert "budget_reconciliation" not in result
            assert reservation["settled"] == 0
            assert workspace.store.remaining_budget(10) == 5
        finally:
            await workspace.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage",
    [
        "no_signal",
        "unresolved",
        "duplicate_ids",
        "wrong_cost",
        "missing_cost",
        "wrong_response_id",
        "nonterminal_usage",
        "missing_token_usage",
        "unmatched_dispatch",
    ],
)
def test_inconsistent_accounting_never_releases_budget(workspace_factory, damage):
    def mutate(workspace, runtime):
        if damage == "no_signal":
            runtime.budget.pop("preflight_failure", None)
        elif damage == "unresolved":
            runtime.budget["unresolved_generation_requests"] = 1
        elif damage == "duplicate_ids":
            runtime.budget["preflight_failure"]["accounted_response_ids"] *= 2
        elif damage == "wrong_cost":
            runtime.budget["preflight_failure"]["cost_usd"] += 1
        elif damage == "unmatched_dispatch":
            run = workspace.store.db.execute(
                "SELECT id,session_id FROM runs WHERE state='running'"
            ).fetchone()
            workspace.store.emit(
                run["session_id"], run["id"], "model_started", {"model": "gpt-6-luna"}
            )
        else:
            with workspace.store.db:
                if damage == "missing_cost":
                    workspace.store.db.execute("DELETE FROM costs")
                else:
                    row = workspace.store.db.execute(
                        "SELECT id,data FROM events WHERE type='usage'"
                    ).fetchone()
                    data = json.loads(row["data"])
                    if damage == "wrong_response_id":
                        data["response_id"] = "resp_unaccounted"
                    elif damage == "nonterminal_usage":
                        data["provider_terminal"] = False
                    elif damage == "missing_token_usage":
                        data.pop("input_tokens")
                    workspace.store.db.execute(
                        "UPDATE events SET data=? WHERE id=?", (json.dumps(data), row["id"])
                    )

    async def scenario():
        workspace, _, _ = workspace_factory(mutate=mutate)
        sid = workspace.store.create_session()["id"]
        try:
            await workspace._run(sid, "Fail closed on inconsistent accounting")
            row, result, reservation, _ = read_run(workspace, sid)
            assert row["state"] == result["status"] == "failed"
            assert not result.get("budget_reconciliation", {}).get("eligible")
            assert reservation["settled"] == 0
            assert workspace.store.remaining_budget(10) == 5
        finally:
            await workspace.close()

    asyncio.run(scenario())


def test_settlement_persists_without_replaying_draft_or_releasing_other_run(workspace_factory):
    async def scenario():
        drafts = []

        def save_and_queue(workspace, runtime):
            drafts.append(deepcopy(runtime.result["operational_receipt"]))
            workspace.store.queue_instruction(sid, "Keep this change pending for explicit review")

        workspace, _, instances = workspace_factory(mutate=save_and_queue)
        sid = workspace.store.create_session()["id"]
        other_sid = workspace.store.create_session()["id"]
        other_rid, _ = workspace.store.begin_run(other_sid)
        workspace.store.reserve_budget(other_rid, 5, 10)
        try:
            await workspace._run(sid, "Prepare a draft once")
            await asyncio.sleep(0)
            row, result, reservation, costs = read_run(workspace, sid)
            known_cost = sum(item["amount"] for item in costs)
            budget = instances[0].budget
            assert len(instances) == len(drafts) == 1
            assert workspace.store.session(sid)["pending_instructions"] == 1
            assert reservation["settled"] == 1
            assert workspace.store.remaining_budget(10) == pytest.approx(5 - known_cost)
            assert (
                workspace._preflight_settlement_evidence(sid, row["id"], result, budget, 0) is None
            )
            state_dir = workspace.state_dir
        finally:
            await workspace.close()
        restored = SessionStore(state_dir)
        try:
            restored.recover_interrupted()
            assert restored.remaining_budget(10) == pytest.approx(5 - known_cost)
            assert restored.session(sid)["pending_instructions"] == 1
            saved = json.loads(
                restored.db.execute("SELECT result FROM runs WHERE id=?", (row["id"],)).fetchone()[
                    0
                ]
            )
            assert saved == result
            assert (
                restored.db.execute(
                    "SELECT settled FROM budget_reservations WHERE run_id=?", (other_rid,)
                ).fetchone()[0]
                == 0
            )
            assert len(restored.session(sid)["runs"]) == 1
        finally:
            restored.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_stage", ["reconciliation", "settlement"])
def test_accounting_failure_preserves_saved_report_and_original_qa(
    workspace_factory, monkeypatch, failure_stage
):
    async def scenario():
        workspace, _, instances = workspace_factory()
        sid = workspace.store.create_session()["id"]

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("Disposable accounting failure")

        target = workspace if failure_stage == "reconciliation" else workspace.store
        method = (
            "_preflight_settlement_evidence"
            if failure_stage == "reconciliation"
            else "settle_budget"
        )
        monkeypatch.setattr(target, method, fail)
        try:
            await workspace._run(sid, "Keep the failed draft available")
            row, result, reservation, costs = read_run(workspace, sid)
            assert row["state"] == result["status"] == "failed"
            assert result["report"] == instances[0].result["report"]
            assert result["qa"]["errors"] == ["APITimeoutError: Request timed out."]
            assert result["operational_receipt"] == {
                "draft_id": "draft_disposable",
                "status": "draft",
            }
            assert reservation["settled"] == 0 and len(costs) == 1
            assert workspace.store.remaining_budget(10) == 5
            proof = result["budget_reconciliation"]
            assert proof["eligible"] is (failure_stage == "settlement")
            assert proof.get("settlement_error_type", proof.get("error_type")) == "OperationalError"
            assert "settled" not in proof
        finally:
            await workspace.close()

    asyncio.run(scenario())
