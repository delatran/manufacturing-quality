"""Prewrite review gates use disposable SQLite state and offline QA responses."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agents import ModelResponse, Usage
from analyst.evidence import canonical, sha256
from analyst.model import BudgetExceeded, ProviderStopped
from analyst.operations import OperationConflict
from analyst.runtime import (
    DraftNotSaved,
    FixtureModel,
    QAReview,
    ReportDraft,
    _message,
    audit_report,
)
from test_draft_evidence import database_rows, draft, make_runtime


def approved():
    return SimpleNamespace(final_output=QAReview(verified=True, errors=[]))


def test_percent_as_count_is_blocked_before_any_durable_write_then_corrected_once(
    tmp_path, monkeypatch
):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        runtime.prompt = "Create a draft to review the Component Critical events."
        detail = json.loads(
            await runtime._call(
                "evidence_detail",
                {
                    "receipt_id": runtime.context_entry["execution_id"],
                    "path": "$.location_severity.cells",
                    "offset": 0,
                    "limit": 100,
                },
            )
        )
        ids.update({item["path"]: item["id"] for item in detail["numeric_evidence"]})
        cell_path = next(path for path in ids if path.endswith(".row_pct"))
        count_path = cell_path.removesuffix("row_pct") + "count"
        # This is precisely the distinction numeric matching alone cannot make.
        wrong = draft(ids)
        wrong["description"] = f"Component has [value:{ids[cell_path]}] Critical events."
        resolved = runtime.resolve_report(ReportDraft(report=wrong["description"]))
        assert audit_report(resolved, runtime.toolkit.ledger)["verified"]
        before = database_rows(operations)
        inspected = []

        async def review(agent, text, **kwargs):
            payload = json.loads(text)
            inspected.append(payload)
            assert agent.name == "QA Verifier" and agent.output_type is QAReview
            assert payload["stage"] == "prewrite_draft" and payload["audit"]["verified"]
            assert payload["user_request"] == runtime.prompt
            assert payload["delivery"]["artifacts"] == []
            assert payload["saved_draft_content"] == []
            proposal = payload["proposal"]
            assert sha256(canonical(proposal)) == payload["proposal_sha256"]
            assert proposal["context_id"] == runtime.analysis_context["context"]["context_id"]
            assert proposal["operation"] == "operations_create_draft"
            comparators = [
                group
                for item in payload["qualitative_support"]
                for group in item["comparative_aggregates"]
                if group["path"] == "$.location_severity.cells"
            ]
            assert comparators and any(
                row["severity"] == "Critical" and row["count"] == 1 and row["row_pct"] == 50
                for row in comparators[0]["rows"]
            )
            assert database_rows(operations) == before
            is_count = (
                next(
                    item
                    for item in payload["numeric_support"]
                    if item["path"] in {cell_path, count_path}
                )["path"]
                == count_path
            )
            return SimpleNamespace(
                final_output=QAReview(
                    verified=is_count,
                    errors=[] if is_count else ["The field labels a percentage as an event count."],
                )
            )

        monkeypatch.setattr("analyst.runtime.Runner.run", review)
        result = json.loads(await runtime._call("operations_create_draft", wrong))["result"]
        assert result["ok"] is False and result["write_occurred"] is False
        assert "percentage" in result["error"]
        assert database_rows(operations) == before
        corrected = draft(ids)
        corrected["description"] = f"Component has [value:{ids[count_path]}] Critical events."
        saved = await runtime.operation("operations_create_draft", corrected)
        assert len(inspected) == 2 and saved["case"]["version"] == 1
        assert saved["case"]["description"] == "Component has 1 Critical events."
        rows = database_rows(operations)
        assert [len(rows[name]) for name in rows] == [1, 1, 1]
        proof = saved["case"]["content_evidence"]["description"]
        assert proof["content_sha256"] == sha256(saved["case"]["description"])
        assert proof["claims"][0]["path"] == count_path
        reviews = [
            item["result"]
            for item in runtime.toolkit.ledger.records()
            if item["tool"] == "draft_semantic_review" and item["result"]["phase"] == "reviewed"
        ]
        assert [item["verified"] for item in reviews] == [False, True]
        assert all(item["write_occurred"] is False for item in reviews)
        requests = [
            item["result"]
            for item in runtime.toolkit.ledger.records()
            if item["tool"] == "draft_semantic_review" and item["result"]["phase"] == "requested"
        ]
        for request, review in zip(requests, reviews, strict=True):
            assert (
                sha256(request["proposal_json"])
                == request["proposal_sha256"]
                == review["proposal_sha256"]
            )
            assert (
                sha256(request["review_input_json"])
                == request["review_input_sha256"]
                == review["review_input_sha256"]
            )
            assert json.loads(request["review_input_json"])["proposal"] == json.loads(
                request["proposal_json"]
            )
            assert review["qa_verdict"]["verified"] == review["verified"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "outcome", ["rejected", "errors", "none", "untyped", "exception", "timeout", "cancelled"]
)
def test_prewrite_qa_failure_never_changes_any_operation_table(tmp_path, monkeypatch, outcome):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        before = database_rows(operations)

        async def review(*args, **kwargs):
            if outcome == "exception":
                raise RuntimeError("Review unavailable")
            if outcome == "timeout":
                raise TimeoutError("Review expired")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
            outputs = {
                "rejected": QAReview(verified=False, errors=[]),
                "errors": QAReview(verified=True, errors=["Wrong population"]),
                "none": None,
                "untyped": {"verified": True, "errors": []},
            }
            return SimpleNamespace(final_output=outputs[outcome])

        monkeypatch.setattr("analyst.runtime.Runner.run", review)
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await runtime._call("operations_create_draft", draft(ids))
        else:
            result = json.loads(await runtime._call("operations_create_draft", draft(ids)))[
                "result"
            ]
            assert result["ok"] is False and result["write_occurred"] is False
        assert database_rows(operations) == before
        evidence = [
            entry["result"]
            for entry in runtime.toolkit.ledger.records()
            if entry["tool"] == "draft_semantic_review"
        ]
        assert evidence[0]["phase"] == "requested" and len(evidence) == 2
        assert sha256(evidence[0]["proposal_json"]) == evidence[-1]["proposal_sha256"]
        assert sha256(evidence[0]["review_input_json"]) == evidence[-1]["review_input_sha256"]
        assert evidence[-1]["verified"] is False
        assert evidence[-1]["phase"] == (
            "reviewed" if outcome in {"rejected", "errors"} else "failed"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw",
    [
        '{"verified":"true","errors":[]}',
        '{"verified":1,"errors":[]}',
        '{"verified":true}',
        '{"verified":true,"errors":"none"}',
    ],
)
def test_real_sdk_rejects_malformed_or_coerced_semantic_verdicts(tmp_path, raw):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        before = database_rows(operations)

        class BadQA(FixtureModel):
            async def get_response(self, *args, **kwargs):
                return ModelResponse(
                    output=[_message(raw)], usage=Usage(requests=0), response_id="invalid_qa"
                )

        runtime.model = lambda role: BadQA(role, runtime)
        result = json.loads(await runtime._call("operations_create_draft", draft(ids)))["result"]
        assert result["ok"] is False and result["write_occurred"] is False
        assert database_rows(operations) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [ProviderStopped, BudgetExceeded])
@pytest.mark.parametrize("nested", [False, True])
def test_provider_stop_and_budget_failure_propagate_without_write(
    tmp_path, monkeypatch, kind, nested
):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        before = database_rows(operations)

        async def review(*args, **kwargs):
            try:
                raise kind("Stopped during semantic review")
            except kind as error:
                if nested:
                    raise RuntimeError("SDK wrapper") from error
                raise

        monkeypatch.setattr("analyst.runtime.Runner.run", review)
        with pytest.raises((kind, DraftNotSaved)) as caught:
            await runtime._call("operations_create_draft", draft(ids))
        chain, seen = caught.value, set()
        while chain is not None and id(chain) not in seen and not isinstance(chain, kind):
            seen.add(id(chain))
            chain = chain.__cause__ or chain.__context__
        assert isinstance(chain, kind)
        assert database_rows(operations) == before

    asyncio.run(scenario())


def test_budget_reached_during_approved_review_still_prevents_write(tmp_path, monkeypatch):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        runtime.settings = replace(runtime.settings, unbounded_runs=False)
        before = database_rows(operations)

        async def review(*args, **kwargs):
            runtime.budget["cost_usd"] = runtime.settings.max_cost_usd_per_run
            return approved()

        monkeypatch.setattr("analyst.runtime.Runner.run", review)
        with pytest.raises(BudgetExceeded):
            await runtime._call("operations_create_draft", draft(ids))
        assert database_rows(operations) == before

    asyncio.run(scenario())


def test_immutable_review_snapshot_saves_reviewed_fields_despite_caller_mutation(
    tmp_path, monkeypatch
):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        values = draft(ids)
        reviewed = []

        async def review(agent, text, **kwargs):
            payload = json.loads(text)
            reviewed.append(copy.deepcopy(payload["proposal"]))
            values["description"] = "Unreviewed mutation"
            values["priority"] = "high"
            payload["proposal"]["fields"]["description"] = "Mutation of decoded QA input"
            return approved()

        monkeypatch.setattr("analyst.runtime.Runner.run", review)
        result = await runtime.operation("operations_create_draft", values)
        saved = operations.get_case(result["case"]["id"])
        assert all(saved[field] == value for field, value in reviewed[0]["fields"].items())
        assert saved["content_evidence"] == reviewed[0]["proofs"]
        assert result["semantic_review"]["proposal_sha256"] == sha256(canonical(reviewed[0]))
        assert result["semantic_review"]["field_sha256"]["description"] == sha256(
            saved["description"]
        )

    asyncio.run(scenario())


def test_update_captures_target_version_null_clears_and_retains_concurrent_human_edit(
    tmp_path, monkeypatch
):
    async def scenario():
        creator, operations, ids = await make_runtime(tmp_path)
        saved = (await creator.operation("operations_create_draft", draft(ids)))["case"]
        updater, _, next_ids = await make_runtime(tmp_path / "updater", "draft_update")
        updater.operations_service = operations
        request = {
            "case_id": saved["id"],
            "expected_version": saved["version"],
            "changes": [
                {
                    "field": "description",
                    "value": f"Review [value:{next_ids['$.kpis.event_count']}] events.",
                },
                {"field": "proposed_owner", "value": None},
                {"field": "action_plan", "value": "   "},
            ],
        }
        reviewed = []
        human_after = []

        async def review(agent, text, **kwargs):
            proposal = json.loads(text)["proposal"]
            reviewed.append(proposal)
            assert proposal["operation"] == "operations_update_draft"
            assert (
                proposal["case_id"] == saved["id"]
                and proposal["expected_version"] == saved["version"]
            )
            assert (
                proposal["fields"]["proposed_owner"] is None
                and proposal["fields"]["action_plan"] is None
            )
            await operations.update_case(
                saved["id"], saved["version"], {"description": "Human edit"}
            )
            human_after.append(database_rows(operations))
            request["expected_version"] = saved["version"] + 1
            request["changes"][0]["value"] = "Unreviewed replacement"
            return approved()

        monkeypatch.setattr("analyst.runtime.Runner.run", review)
        with pytest.raises(OperationConflict, match="changed since"):
            await updater.operation("operations_update_draft", request)
        assert reviewed and database_rows(operations) == human_after[0]
        current = operations.get_case(saved["id"])
        assert current["description"] == "Human edit" and current["version"] == saved["version"] + 1

    asyncio.run(scenario())


def test_prewrite_does_not_bind_artifacts_or_inherit_unrelated_saved_draft_errors(
    tmp_path, monkeypatch
):
    async def scenario():
        runtime, _, ids = await make_runtime(tmp_path)
        runtime.toolkit.ledger.record(
            "operations_create_draft",
            "# unrelated old operation",
            {
                "ok": True,
                "case": {
                    "id": "unrelated",
                    "content_evidence": {
                        "description": {
                            "run_id": runtime.toolkit.run_dir.name,
                            "annotated_text": "wrong",
                        }
                    },
                },
            },
        )
        runtime.delivery_bindings = {"keep": {"sha256": "existing-binding"}}
        before = copy.deepcopy(runtime.delivery_bindings)

        async def review(agent, text, **kwargs):
            payload = json.loads(text)
            assert payload["audit"]["verified"] and payload["saved_draft_content"] == []
            assert payload["delivery"] == {
                "version": 1,
                "artifacts": [],
                "source_execution_ids": [],
                "scope": None,
                "source_manifest": {},
            }
            return approved()

        monkeypatch.setattr("analyst.runtime.Runner.run", review)
        assert (await runtime.operation("operations_create_draft", draft(ids)))["ok"]
        assert runtime.delivery_bindings == before

    asyncio.run(scenario())


def test_denied_intent_never_calls_semantic_qa(tmp_path, monkeypatch):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path, "none")
        before = database_rows(operations)

        async def forbidden(*args, **kwargs):
            raise AssertionError("Unauthorized mutation reached semantic QA")

        monkeypatch.setattr("analyst.runtime.Runner.run", forbidden)
        with pytest.raises(ValueError, match="did not authorize"):
            await runtime.operation("operations_create_draft", draft(ids))
        assert database_rows(operations) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("arrival", ["during_review", "review_event"])
def test_new_steering_invalidates_prewrite_approval(tmp_path, monkeypatch, operation, arrival):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        name, arguments = "operations_create_draft", draft(ids)
        if operation == "update":
            saved = (await runtime.operation(name, arguments))["case"]
            runtime, _, _ = await make_runtime(tmp_path / "updater", "draft_update")
            runtime.operations_service = operations
            name = "operations_update_draft"
            arguments = {
                "case_id": saved["id"],
                "expected_version": saved["version"],
                "changes": [{"field": "description", "value": "Review the observed evidence."}],
            }
        runtime.prompt = "Save the proposed draft."
        before = database_rows(operations)
        requests = []

        async def withdraw():
            receipt = await runtime.steer(
                "Do not save or change the draft. Explain the evidence only.",
                instruction_id="withdraw-draft",
            )
            assert receipt["accepted"] is True
            # Even when the Lead has consumed the update, an earlier QA verdict
            # must not authorize the old proposal.
            await runtime.model_steering("Lead Analyst")()
            assert not runtime._steering_pending()

        async def review(agent, text, **kwargs):
            requests.append(json.loads(text))
            if arrival == "during_review":
                await withdraw()
            return approved()

        original_emit = runtime.emit

        async def emit(kind, payload):
            if kind == "draft_validation" and arrival == "review_event":
                await withdraw()
            await original_emit(kind, payload)

        monkeypatch.setattr("analyst.runtime.Runner.run", review)
        monkeypatch.setattr(runtime, "emit", emit)
        result = json.loads(await runtime._call(name, arguments))["result"]
        assert result["ok"] is False and result["write_occurred"] is False
        assert "new instructions" in result["error"]
        assert requests[0]["user_request"] == "Save the proposed draft."
        assert runtime.steering_revision == 1
        assert database_rows(operations) == before

    asyncio.run(scenario())


def test_steering_during_service_context_lookup_prevents_draft_commit(tmp_path, monkeypatch):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        before = database_rows(operations)
        original_lookup = operations.analysis.context_by_id

        async def context_by_id(context_id):
            payload = await original_lookup(context_id)
            receipt = await runtime.steer(
                "Do not save this draft.", instruction_id="withdraw-during-context-lookup"
            )
            assert receipt["accepted"] is True
            return payload

        monkeypatch.setattr(operations.analysis, "context_by_id", context_by_id)
        result = json.loads(await runtime._call("operations_create_draft", draft(ids)))["result"]
        assert result["ok"] is False and result["write_occurred"] is False
        assert "new instructions" in result["error"]
        assert database_rows(operations) == before
        reviewed = [
            entry["result"]
            for entry in runtime.toolkit.ledger.records()
            if entry["tool"] == "draft_semantic_review" and entry["result"]["phase"] == "reviewed"
        ]
        assert reviewed and reviewed[-1]["verified"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["pending", "unresolved"])
def test_unconsumed_steering_prevents_prewrite_review(tmp_path, monkeypatch, state):
    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path)
        before = database_rows(operations)
        await runtime.steer("Explain the evidence only.", instruction_id="new-request")
        if state == "unresolved":
            runtime._direct_steering["new-request"]["state"] = state

        async def forbidden(*args, **kwargs):
            raise AssertionError("Pending steering reached semantic QA")

        monkeypatch.setattr("analyst.runtime.Runner.run", forbidden)
        result = json.loads(await runtime._call("operations_create_draft", draft(ids)))["result"]
        assert result["ok"] is False and result["write_occurred"] is False
        expected_error = "unresolved admission" if state == "unresolved" else "new instructions"
        assert expected_error in result["error"]
        assert database_rows(operations) == before
        assert not any(
            entry["tool"] == "draft_semantic_review" for entry in runtime.toolkit.ledger.records()
        )

    asyncio.run(scenario())
