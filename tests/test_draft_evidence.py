"""Agent-authored draft content is checked before durable local mutation."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from analyst.case_study.preparation import build_snapshot
from analyst.config import Settings
from analyst.dashboard import AnalysisService
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.operations import OperationsService
from analyst.runtime import AnalystReport, AnalystRuntime, FixtureModel

ROOT = Path(__file__).resolve().parents[1]
MASTER = (
    b"product_id,product_name,category,production_line,monthly_output\n"
    b"1,Product 1,Category 1,Line-4,100\n"
)


async def make_runtime(tmp_path, intent="draft_create"):
    rows = [
        {
            "defect_id": index,
            "product_id": 1,
            "defect_type": "Functional",
            "defect_date": "2024-01-15",
            "defect_location": "Component",
            "severity": "Critical" if index == 1 else "Minor",
            "inspection_method": "Visual Inspection",
            "repair_cost": cost,
        }
        for index, cost in ((1, 10), (2, None))
    ]
    snapshot = build_snapshot(
        rows,
        {
            "source_url": "https://synthetic.invalid/draft-evidence",
            "source_sha256": sha256(canonical(rows)),
            "fetched_at": "2026-01-01T00:00:00+00:00",
        },
        MASTER,
    )
    analysis = AnalysisService(ROOT, tmp_path / "analysis", snapshot_loader=lambda: snapshot)
    payload = await analysis.get_context()
    settings = Settings(
        root=ROOT, state_dir=str(tmp_path / "state"), mode="live", api_key="offline-test"
    )
    operations = OperationsService(settings, analysis)
    run_dir = tmp_path / "runtime-run"
    toolkit = SimpleNamespace(
        ledger=EvidenceLedger(run_dir), run_dir=run_dir, schemas=[], input_paths=[]
    )

    async def emit(*_):
        pass

    runtime = AnalystRuntime(
        settings,
        toolkit,
        emit,
        analysis_context=payload,
        analysis_service=analysis,
        operations_service=operations,
        action_intent=intent,
    )
    # Exercise the SDK review path with deterministic offline QA. These checks
    # establish mutation/evidence behavior, not live semantic-model quality.
    runtime.model = lambda role: FixtureModel(role, runtime)
    view = json.loads(await runtime._call("dashboard_context", {}))
    ids = {item["path"]: item["id"] for item in view["numeric_evidence"]}
    return runtime, operations, ids


def draft(ids):
    return {
        "title": "Review Component",
        "description": f"Component has [value:{ids['$.kpis.event_count']}] logged events.",
        "category": "investigation",
        "priority": "medium",
        "proposed_owner": "Quality review team",
        "due_date": None,
        "action_plan": f"Validate [value:{ids['$.kpis.cost_unknown_count']}] events with unknown repair cost.",
    }


def database_rows(operations):
    with sqlite3.connect(operations.path) as db:
        return {
            name: db.execute(f"SELECT * FROM {name}").fetchall()
            for name in ("operation_cases", "operation_history", "operation_requests")
        }


@pytest.mark.parametrize("field", ["title", "description", "proposed_owner", "action_plan"])
def test_wrong_or_unknown_draft_numbers_never_mutate_state(tmp_path, field):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        before = database_rows(operations)
        for text in (
            f"Observed 11 events [claim:{ids['$.kpis.cost_unknown_count']}].",
            "Observed 11 events [claim:unknown].",
            "Observed [value:unknown].",
        ):
            values = draft(ids)
            values[field] = text
            with pytest.raises(ValueError, match="was not saved"):
                await runtime.operation("operations_create_draft", values)
            assert database_rows(operations) == before

    asyncio.run(scenario())


def test_valid_draft_saves_clean_text_and_exact_current_run_evidence(tmp_path):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        view = json.loads(await runtime._call("operations_create_draft", draft(ids)))
        assert view["numeric_evidence"] == []
        assert (
            "content_evidence" not in view["result"]["case"]
            and "history" not in view["result"]["case"]
        )
        assert view["result"]["saved_fields_verified"] is True
        saved = operations.get_case(view["result"]["case"]["id"])
        assert saved["description"] == "Component has 2 logged events."
        assert saved["action_plan"] == "Validate 1 events with unknown repair cost."
        assert all(
            "[claim:" not in saved[field] and "[value:" not in saved[field]
            for field in ("title", "description", "proposed_owner", "action_plan")
        )
        proof = saved["content_evidence"]["description"]
        assert proof["content_sha256"] == sha256(saved["description"])
        assert proof["claims"][0]["id"] == ids["$.kpis.event_count"]
        assert proof["receipts"][0]["execution_id"] == proof["claims"][0]["execution_id"]
        support, errors = runtime.qa_evidence(
            AnalystReport(report="A local draft was saved.", claims=[])
        )
        assert not errors
        assert any(
            item["field"] == "description" and item["saved_text"] == saved["description"]
            for item in support["saved_draft_content"]
        )
        assert any(
            item["claim_id"] == ids["$.kpis.event_count"] for item in support["numeric_support"]
        )
        assert support["agent_capabilities"]["available_operations"] == [
            "operations_list_cases",
            "operations_create_draft",
        ]

    asyncio.run(scenario())


def test_cross_run_claim_rejection_and_update_preserve_existing_proofs(tmp_path):
    async def scenario():
        creator, operations, first_ids = await make_runtime(tmp_path / "first")
        saved = (await creator.operation("operations_create_draft", draft(first_ids)))["case"]
        updater, _, ids = await make_runtime(tmp_path / "second", "draft_update")
        updater.operations_service = operations
        before = database_rows(operations)
        with pytest.raises(ValueError, match="was not saved"):
            await updater.operation(
                "operations_update_draft",
                {
                    "case_id": saved["id"],
                    "expected_version": saved["version"],
                    "changes": [
                        {
                            "field": "description",
                            "value": f"Observed 2 [claim:{first_ids['$.kpis.event_count']}].",
                        }
                    ],
                },
            )
        assert database_rows(operations) == before
        result = await updater.operation(
            "operations_update_draft",
            {
                "case_id": saved["id"],
                "expected_version": saved["version"],
                "changes": [
                    {
                        "field": "description",
                        "value": f"Review [value:{ids['$.kpis.event_count']}] logged events.",
                    },
                    {"field": "proposed_owner", "value": "   "},
                ],
            },
        )
        assert result["ok"] and result["case"]["description"] == "Review 2 logged events."
        assert result["case"]["proposed_owner"] is None
        assert "proposed_owner" not in result["case"]["content_evidence"]
        assert result["case"]["content_evidence"]["title"] == saved["content_evidence"]["title"]

    asyncio.run(scenario())


def test_readback_mismatch_keeps_write_identity_and_never_replays(tmp_path, monkeypatch):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        original = operations.get_case

        def changed_readback(case_id):
            value = copy.deepcopy(original(case_id))
            value["description"] = "Different durable text"
            return value

        monkeypatch.setattr(operations, "get_case", changed_readback)
        result = await runtime.operation("operations_create_draft", draft(ids))
        assert result["ok"] is False and result["write_occurred"] is True and result["case_id"]
        assert "do not automatically repeat" in result["effect"]
        assert len(database_rows(operations)["operation_cases"]) == 1
        assert len(database_rows(operations)["operation_requests"]) == 1
        assert original(result["case_id"])["description"] == "Component has 2 logged events."

    asyncio.run(scenario())


def test_capabilities_keep_human_workflow_transitions_separate_from_agent_authority(tmp_path):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path, "none")
        user_case = await operations.create_case(
            {
                "context_id": runtime.analysis_context["context"]["context_id"],
                "title": "Human review",
                "idempotency_key": "human-review-fixture",
            }
        )
        view = json.loads(await runtime._call("operations_list_cases", {}))
        assert view["result"]["cases"][0]["user_ui_allowed_transitions"] == [
            "acknowledged",
            "dismissed",
        ]
        capabilities = runtime.operation_capabilities()
        assert capabilities["available_operations"] == ["operations_list_cases"]
        assert "change_status" in capabilities["unavailable_effects"]
        with pytest.raises(ValueError, match="did not authorize"):
            await runtime.operation("operations_create_draft", draft(ids))
        assert operations.get_case(user_case["id"])["status"] == "draft"

    asyncio.run(scenario())
