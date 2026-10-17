"""Local workflow checks use synthetic receipts and disposable SQLite state."""

from __future__ import annotations

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from analyst.case_study.ingestion import SourceError
from analyst.case_study.preparation import build_snapshot
from analyst.config import Settings
from analyst.dashboard import AnalysisService, EvidenceIntegrityError
from analyst.evidence import canonical, sha256
from analyst.operations import (
    OperationConflict,
    OperationForbidden,
    OperationsService,
    register_operations_routes,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
MASTER = (
    b"product_id,product_name,category,production_line,monthly_output\n"
    b"1,Widget A,Category 1,Line-1,100\n"
    b"2,Widget B,Category 1,Line-1,200\n"
)


def event(identifier=1, **changes):
    return {
        "defect_id": identifier,
        "product_id": 1,
        "defect_type": "Functional",
        "defect_date": "2024-01-15",
        "defect_location": "Component",
        "severity": "Critical",
        "inspection_method": "Visual Inspection",
        "repair_cost": 10,
        **changes,
    }


def snapshot(rows=None, *, master=MASTER):
    rows = (
        rows
        if rows is not None
        else [
            event(),
            event(2, defect_date="2024-02-01", repair_cost=20),
            event(3, defect_date="2024-02-01", severity="Minor", repair_cost=None),
            event(4, defect_date="2024-02-01", repair_cost=30),
        ]
    )
    return build_snapshot(
        rows,
        {
            "source_url": "https://synthetic.invalid/events",
            "source_sha256": sha256(canonical(rows)),
            "fetched_at": "2026-01-01T00:00:00+00:00",
        },
        master,
    )


class Loader:
    def __init__(self, *items):
        self.items = list(items) or [snapshot()]
        self.calls = 0

    def __call__(self):
        item = self.items[min(self.calls, len(self.items) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def service(tmp_path, loader=None):
    settings = replace(Settings(), root=ROOT, state_dir=str(tmp_path / "state"), mode="mock")
    analysis = AnalysisService(ROOT, tmp_path / "analysis", snapshot_loader=loader or Loader())
    return OperationsService(settings, analysis)


async def create(subject, **changes):
    context = await subject.analysis.get_context()
    return await subject.create_case(
        {
            "context_id": context["context"]["context_id"],
            "title": "Inspect recurring failures",
            "idempotency_key": "test-request-1",
            **changes,
        }
    )


async def advance(subject, record, status, **values):
    return await subject.transition_case(
        record["id"], record["version"], status, confirmed=True, **values
    )


def client(subject):
    app = FastAPI()
    register_operations_routes(app, subject)
    return TestClient(app)


def content_proofs(context_id, **fields):
    """Synthetic structurally valid runtime proofs, never live model evidence."""
    return {
        field: {
            "validation_version": "numeric_claims_v1",
            "context_id": context_id,
            "run_id": "synthetic-run",
            "annotated_text": text,
            "annotated_sha256": sha256(text),
            "content_sha256": sha256(text),
            "validator_code_sha256": "a" * 64,
            "claims": [],
            "receipts": [],
        }
        for field, text in fields.items()
        if text is not None
    }


async def agent_create(subject, **changes):
    payload = await subject.analysis.get_context()
    values = {
        "context_id": payload["context"]["context_id"],
        "title": "Inspect selected records",
        "description": "Review the supplied historical evidence",
        "proposed_owner": "Maintenance role",
        "action_plan": "Check the source records",
        "idempotency_key": "agent-evidence-1",
        **changes,
    }
    evidence = content_proofs(
        values["context_id"],
        **{key: values[key] for key in ("title", "description", "proposed_owner", "action_plan")},
    )
    return await subject.create_case(
        values, actor="agent", authorized=True, content_evidence=evidence
    )


def operation_counts(subject):
    with subject._connect() as db:
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("operation_cases", "operation_history", "operation_requests")
        )


@pytest.mark.asyncio
async def test_case_keeps_verified_binding_and_survives_reopen_without_modifying_receipt(tmp_path):
    subject = service(tmp_path)
    payload = await subject.analysis.get_context()
    receipt_path = subject.analysis.contexts_dir / payload["context"]["context_id"] / "ledger.jsonl"
    original = receipt_path.read_bytes()
    record = await create(subject)
    assert subject.path == tmp_path / "state" / "operations.sqlite3"
    assert not (tmp_path / "state" / "workspace.sqlite3").exists()
    assert record["context"] == payload["context"]
    assert record["provenance"]["execution_id"] == payload["evidence"]["execution_id"]
    assert record["provenance"]["result_sha256"] == payload["evidence"]["result_sha256"]
    assert record["status"] == "draft" and record["version"] == 1
    assert record["origin_report_id"] is None
    assert all(
        record[key] is None
        for key in (
            "proposed_owner",
            "assigned_owner",
            "due_date",
            "action_plan",
            "outcome",
            "follow_up",
        )
    )
    record["context"]["filters"]["severity"] = "invented"
    assert subject.get_case(record["id"])["context"] == payload["context"]
    restored = service(tmp_path)
    assert restored.get_case(record["id"])["title"] == "Inspect recurring failures"
    assert receipt_path.read_bytes() == original


@pytest.mark.asyncio
async def test_report_origin_is_an_immutable_reference_separate_from_verified_provenance(tmp_path):
    subject = service(tmp_path)
    payload = await subject.analysis.get_context()
    record = await create(subject, origin_report_id="12345")
    assert record["origin_report_id"] == "12345"
    assert record["context"] == payload["context"]
    assert record["provenance"]["execution_id"] == payload["evidence"]["execution_id"]
    assert record["history"][0]["changes"]["origin_report_id"] == "12345"
    assert service(tmp_path).get_case(record["id"])["origin_report_id"] == "12345"
    assert (await create(subject, origin_report_id="12345"))["id"] == record["id"]
    with pytest.raises(OperationConflict, match="idempotency"):
        await create(subject, origin_report_id="different-report")
    with pytest.raises(ValueError, match="immutable"):
        await subject.update_case(record["id"], 1, {"origin_report_id": "different-report"})


@pytest.mark.asyncio
async def test_create_requires_verified_existing_context_and_valid_source_membership(tmp_path):
    subject = service(tmp_path)
    payload = await subject.analysis.get_context()
    context_id = payload["context"]["context_id"]
    for source in (
        {"kind": "recommendation", "key": "999"},
        {"kind": "historical_candidate", "key": "2026-01-01"},
        {"kind": "manual", "key": "fake"},
        {"kind": "recommendation", "key": "1", "summary": "invented"},
    ):
        with pytest.raises(ValueError):
            await create(subject, source=source)
    assert subject.list_cases()["total"] == 0
    assert payload["analysis"]["anomalies"]["candidates"][0]["date"] == "2024-02-01"
    record = await create(subject, source={"kind": "historical_candidate", "key": "2024-02-01"})
    assert record["source"]["receipt_path"] == "$.anomalies.candidates[0]"
    assert "not a confirmed process alarm" in record["source"]["summary"]
    assert record["source"]["facts"] == payload["analysis"]["anomalies"]["candidates"][0]
    path = subject.analysis.contexts_dir / context_id / "artifacts" / "analysis.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceIntegrityError):
        await create(subject, idempotency_key="new-request-2")
    assert subject.list_cases()["total"] == 1


@pytest.mark.asyncio
async def test_idempotency_and_source_dedup_do_not_duplicate_history(tmp_path):
    subject = service(tmp_path)
    first = await create(subject, source={"kind": "recommendation", "key": "1"})
    again = await create(subject, source={"kind": "recommendation", "key": "1"})
    same_source = await create(
        subject,
        title="A different proposed label",
        idempotency_key="request-new-2",
        source={"kind": "recommendation", "key": "1"},
    )
    assert first["id"] == again["id"] == same_source["id"]
    assert len(same_source["history"]) == 1
    with pytest.raises(OperationConflict, match="idempotency"):
        await create(subject, title="Changed replay", source={"kind": "recommendation", "key": "1"})
    assert subject.list_cases()["total"] == 1


@pytest.mark.asyncio
async def test_concurrent_create_requests_and_distinct_store_instances_share_one_case(tmp_path):
    subject = service(tmp_path)
    other = OperationsService(
        replace(Settings(), state_dir=str(tmp_path / "state")), subject.analysis
    )
    records = await asyncio.gather(
        *(create(subject if index % 2 else other) for index in range(12))
    )
    assert len({record["id"] for record in records}) == 1
    assert len(subject.get_case(records[0]["id"])["history"]) == 1
    assert subject.list_cases()["total"] == 1


@pytest.mark.asyncio
async def test_concurrent_updates_reject_stale_write_and_preserve_one_atomic_audit(tmp_path):
    subject = service(tmp_path)
    record = await create(subject)
    other = OperationsService(
        replace(Settings(), state_dir=str(tmp_path / "state")), subject.analysis
    )

    def update(store, title):
        try:
            return asyncio.run(store.update_case(record["id"], 1, {"title": title}))
        except OperationConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda values: update(*values), [(subject, "First edit"), (other, "Second edit")]
            )
        )
    assert sum(isinstance(item, OperationConflict) for item in results) == 1
    current = subject.get_case(record["id"])
    assert current["version"] == 2
    assert len(current["history"]) == 2
    assert current["history"][-1]["changes"]["title"]["after"] == current["title"]


@pytest.mark.asyncio
async def test_human_loop_plan_validation_resolution_and_reopen_preserve_history(tmp_path):
    subject = service(tmp_path)
    record = await create(subject, due_date="2024-01-01", proposed_owner="Quality engineer")
    assert record["is_overdue"]
    with pytest.raises(OperationForbidden):
        await subject.transition_case(record["id"], 1, "acknowledged")
    with pytest.raises(OperationConflict):
        await advance(subject, record, "resolved", outcome="Not examined")
    record = await advance(subject, record, "acknowledged", note="Reviewed the historical evidence")
    record = await advance(subject, record, "investigating")
    with pytest.raises(ValueError, match="action_plan"):
        await advance(subject, record, "action_planned")
    record = await subject.update_case(
        record["id"],
        record["version"],
        {
            "action_plan": "Inspect sample records and reconcile cost capture",
            "assigned_owner": "Local quality role",
        },
    )
    record = await advance(subject, record, "action_planned")
    with pytest.raises(ValueError, match="action_plan"):
        await subject.update_case(record["id"], record["version"], {"action_plan": None})
    with pytest.raises(ValueError, match="outcome"):
        await advance(subject, record, "resolved")
    record = await advance(
        subject,
        record,
        "resolved",
        outcome="Cost records reconciled; no production improvement has been measured",
    )
    assert record["resolved_at"] is not None and not record["is_overdue"]
    with pytest.raises(OperationConflict, match="Reopen"):
        await subject.update_case(record["id"], record["version"], {"title": "Edit closed work"})
    with pytest.raises(ValueError, match="reason"):
        await advance(subject, record, "investigating")
    record = await advance(subject, record, "investigating", note="New source records need review")
    assert record["outcome"] is None and record["resolved_at"] is None
    assert record["history"][-1]["kind"] == "reopened"
    assert "no production improvement" in record["history"][-1]["changes"]["outcome"]["before"]
    assert record["assigned_owner"] == "Local quality role"
    assert record["proposed_owner"] == "Quality engineer"


@pytest.mark.asyncio
async def test_dismiss_and_notes_require_reason_and_version(tmp_path):
    subject = service(tmp_path)
    record = await create(subject)
    with pytest.raises(ValueError, match="reason"):
        await advance(subject, record, "dismissed")
    record = await advance(
        subject, record, "dismissed", note="Already reviewed in a linked investigation"
    )
    noted = await subject.add_note(
        record["id"], record["version"], "Kept the original source context"
    )
    assert len(noted["notes"]) == 2
    with pytest.raises(OperationConflict):
        await subject.add_note(record["id"], record["version"], "Stale note")
    assert len(subject.get_case(record["id"])["notes"]) == 2
    reopened = await advance(
        subject, noted, "investigating", note="The prior dismissal needs another review"
    )
    assert reopened["history"][-1]["kind"] == "reopened"


@pytest.mark.asyncio
async def test_atomic_save_commits_edits_status_and_note_in_one_version_and_audit(tmp_path):
    subject = service(tmp_path)
    record = await create(subject)
    result = await subject.save_case(
        record["id"],
        1,
        changes={"title": "Reviewed investigation", "proposed_owner": "Quality role"},
        status="acknowledged",
        note="Evidence reviewed before acceptance",
        confirmed=True,
    )
    assert result["version"] == 2 and len(result["history"]) == 2
    assert result["title"] == "Reviewed investigation" and result["status"] == "acknowledged"
    assert set(result["history"][-1]["changes"]) == {"title", "proposed_owner", "status"}
    assert result["history"][-1]["kind"] == "transitioned"
    assert result["notes"][-1]["text"] == "Evidence reviewed before acceptance"
    with pytest.raises(OperationConflict, match="changed since"):
        await subject.save_case(
            record["id"], 1, changes={"title": "Stale edit"}, status="investigating", confirmed=True
        )
    assert subject.get_case(record["id"]) == result


@pytest.mark.asyncio
async def test_atomic_invalid_transition_never_saves_working_details_or_note(tmp_path):
    subject = service(tmp_path)
    record = await create(subject)
    with pytest.raises(OperationConflict, match="Cannot move"):
        await subject.save_case(
            record["id"],
            1,
            changes={"title": "Must not save", "due_date": "2024-05-01"},
            status="resolved",
            outcome="Unsupported conclusion",
            note="Must not persist",
            confirmed=True,
        )
    assert subject.get_case(record["id"]) == record


@pytest.mark.asyncio
async def test_atomic_save_validates_action_plan_against_final_target_state(tmp_path):
    subject = service(tmp_path)
    record = await create(subject, action_plan="Inspect selected records")
    for status in ("acknowledged", "investigating", "action_planned"):
        record = await advance(subject, record, status)
    for status in (None, "action_planned"):
        with pytest.raises(ValueError, match="action_plan"):
            await subject.save_case(
                record["id"],
                record["version"],
                changes={"title": "Must not save", "action_plan": None},
                status=status,
                confirmed=True,
            )
        assert subject.get_case(record["id"]) == record
    result = await subject.save_case(
        record["id"],
        record["version"],
        changes={"title": "Investigate again", "action_plan": None},
        status="investigating",
        note="The prior plan needs another investigation",
        confirmed=True,
    )
    assert result["status"] == "investigating" and result["action_plan"] is None
    assert result["version"] == record["version"] + 1
    assert len(result["history"]) == len(record["history"]) + 1
    with pytest.raises(ValueError, match="action_plan"):
        await subject.save_case(
            result["id"],
            result["version"],
            changes={"title": "Invalid new plan"},
            status="action_planned",
            confirmed=True,
        )
    assert subject.get_case(result["id"]) == result
    planned = await subject.save_case(
        result["id"],
        result["version"],
        changes={"action_plan": "Reconcile the newly identified records"},
        status="action_planned",
        confirmed=True,
    )
    assert planned["status"] == "action_planned"
    assert planned["version"] == result["version"] + 1


@pytest.mark.asyncio
async def test_atomic_closed_case_requires_separate_reopen_before_editing(tmp_path):
    subject = service(tmp_path)
    record = await create(subject, action_plan="Inspect selected records")
    for status in ("acknowledged", "investigating", "action_planned"):
        record = await advance(subject, record, status)
    record = await subject.save_case(
        record["id"],
        record["version"],
        status="resolved",
        outcome="Reviewed historical records",
        confirmed=True,
    )
    for status in (None, "resolved", "investigating"):
        with pytest.raises(OperationConflict, match="Reopen"):
            await subject.save_case(
                record["id"],
                record["version"],
                changes={"title": "Cannot edit a closed case"},
                status=status,
                note="Reason does not authorize combined closed edits",
                confirmed=True,
            )
        assert subject.get_case(record["id"]) == record
    noted = await subject.save_case(
        record["id"],
        record["version"],
        status="resolved",
        note="Additional review note",
        confirmed=True,
    )
    assert noted["status"] == "resolved" and noted["outcome"] == record["outcome"]
    assert noted["history"][-1]["kind"] == "note_added"
    with pytest.raises(ValueError, match="reason"):
        await subject.save_case(
            record["id"], noted["version"], status="investigating", confirmed=True
        )
    reopened = await subject.save_case(
        record["id"],
        noted["version"],
        status="investigating",
        note="Recheck newly supplied records",
        confirmed=True,
    )
    assert reopened["outcome"] is None and reopened["resolved_at"] is None
    assert reopened["history"][-1]["kind"] == "reopened"
    assert reopened["history"][-1]["changes"]["outcome"]["before"] == record["outcome"]
    edited = await subject.save_case(
        record["id"],
        reopened["version"],
        changes={"title": "Reopened working title"},
        confirmed=True,
    )
    assert edited["title"] == "Reopened working title"


@pytest.mark.asyncio
async def test_atomic_save_requires_human_confirmation_and_rolls_back_audit_failure(
    tmp_path, monkeypatch
):
    subject = service(tmp_path)
    record = await create(subject)
    with pytest.raises(OperationForbidden):
        await subject.save_case(record["id"], 1, changes={"title": "Unauthorized"})
    with pytest.raises(OperationForbidden):
        await subject.save_case(
            record["id"], 1, changes={"title": "Agent shortcut"}, actor="agent", confirmed=True
        )

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit write failure")

    monkeypatch.setattr(subject, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit"):
        await subject.save_case(
            record["id"],
            1,
            changes={"title": "Must roll back"},
            status="acknowledged",
            note="Not saved",
            confirmed=True,
        )
    assert subject.get_case(record["id"]) == record


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"title": "a" * 181},
        {"description": 100},
        {"priority": "urgent"},
        {"category": "live_alarm"},
        {"proposed_owner": "a" * 161},
        {"due_date": "2024-02-31"},
        {"due_date": "2024-1-01"},
        {"action_plan": "a" * 4001},
        {"status": "resolved"},
        {"context": {}},
        {"outcome": "Agent declared success"},
        {"idempotency_key": "bad"},
        {"origin_report_id": 123},
        {"origin_report_id": "a" * 161},
    ],
)
@pytest.mark.asyncio
async def test_invalid_fields_never_create_a_case(tmp_path, changes):
    subject = service(tmp_path)
    with pytest.raises(ValueError):
        await create(subject, **changes)
    assert subject.list_cases()["total"] == 0


@pytest.mark.asyncio
async def test_agent_needs_server_authorization_and_can_only_propose_draft_fields(tmp_path):
    subject = service(tmp_path)
    payload = await subject.analysis.get_context()
    values = {
        "context_id": payload["context"]["context_id"],
        "title": "Proposed investigation",
        "idempotency_key": "agent-request-1",
    }
    with pytest.raises(OperationForbidden):
        await subject.create_case(values, actor="agent")
    with pytest.raises(OperationForbidden):
        await subject.create_case(
            {**values, "assigned_owner": "Unconfirmed person"}, actor="agent", authorized=True
        )
    evidence = content_proofs(
        values["context_id"],
        title=values["title"],
        description="",
        proposed_owner="Maintenance role",
    )
    record = await subject.create_case(
        {**values, "proposed_owner": "Maintenance role"},
        actor="agent",
        authorized=True,
        content_evidence=evidence,
    )
    assert record["status"] == "draft" and record["assigned_owner"] is None
    assert record["history"][0]["actor"] == "agent"
    description = "Review user-requested records"
    record = await subject.update_case(
        record["id"],
        record["version"],
        {"description": description},
        actor="agent",
        authorized=True,
        content_evidence=content_proofs(record["context_id"], description=description),
    )
    with pytest.raises(OperationForbidden):
        await subject.update_case(
            record["id"],
            record["version"],
            {"assigned_owner": None},
            actor="agent",
            authorized=True,
        )
    with pytest.raises(OperationForbidden):
        await subject.transition_case(
            record["id"], record["version"], "acknowledged", actor="agent", confirmed=True
        )
    record = await advance(subject, record, "acknowledged")
    with pytest.raises(OperationForbidden, match="only edit draft"):
        await subject.update_case(
            record["id"],
            record["version"],
            {"description": "Overwrite reviewed work"},
            actor="agent",
            authorized=True,
        )


@pytest.mark.asyncio
async def test_source_check_is_bounded_persisted_deduplicated_and_does_not_create_work(tmp_path):
    initial = snapshot([event(), event(2, defect_date="2024-02-01", repair_cost=20)])
    revised = snapshot(
        [
            event(repair_cost=15),
            event(3, defect_date="2024-03-01", severity="Minor", repair_cost=None),
        ]
    )
    loader = Loader(initial, revised)
    subject = service(tmp_path, loader)
    baseline = await subject.analysis.get_context()
    check = await subject.check_source(baseline["context"]["context_id"])
    assert loader.calls == 2
    assert check["status"] == "changed" and check["source_changed"] and check["review_required"]
    assert check["data_changes"] == {"added_events": 1, "removed_events": 1, "changed_events": 1}
    metrics = {item["key"]: item for item in check["metrics"]}
    assert metrics["known_repair_cost"] == {
        "key": "known_repair_cost",
        "before": 30,
        "after": 15,
        "delta": -15,
    }
    assert metrics["critical_count"]["delta"] == -1
    assert metrics["cost_unknown_count"]["delta"] == 1
    assert check["coverage"]["reporting_complete"] is None
    assert check["coverage"]["actual_produced_units"] is None
    assert check["review_cases"] == [] and subject.list_cases()["total"] == 0
    again = await subject.check_source(baseline["context"]["context_id"])
    assert check["id"] == again["id"]
    assert len(subject.list_checks()["checks"]) == 1
    restored = service(tmp_path)
    assert restored.list_checks()["checks"][0]["id"] == check["id"]
    added = await subject.create_case(
        {
            "context_id": check["current_context_id"],
            "title": "Review changed historical records",
            "source": {"kind": "source_check", "key": check["id"]},
            "idempotency_key": "human-add-check-1",
        }
    )
    assert added["status"] == "draft" and added["source"]["facts"]["id"] == check["id"]
    assert subject.list_cases()["total"] == 1
    await subject.check_source(baseline["context"]["context_id"])
    replay = await subject.create_case(
        {
            "context_id": check["current_context_id"],
            "title": "Review changed historical records",
            "source": {"kind": "source_check", "key": check["id"]},
            "idempotency_key": "human-add-check-1",
        }
    )
    assert replay["id"] == added["id"]
    assert replay["source"]["facts"]["checked_at"] == added["source"]["facts"]["checked_at"]


@pytest.mark.asyncio
async def test_unchanged_source_never_claims_effectiveness_or_creates_review_issue(tmp_path):
    subject = service(tmp_path)
    record = await create(subject)
    check = await subject.check_source(record["context_id"])
    assert check["status"] == "unchanged" and not check["review_required"]
    assert check["current_context_id"] == record["context_id"]
    assert "no new evidence" in check["summary"]
    assert subject.list_cases()["total"] == 1
    with pytest.raises(ValueError, match="requires review"):
        await create(
            subject,
            source={"kind": "source_check", "key": check["id"]},
            idempotency_key="wrong-check-1",
        )
    updated = await subject.add_follow_up(record["id"], 1, record["context_id"], confirmed=True)
    assert updated["follow_up"]["status"] == "unchanged"
    assert all(metric["delta"] in (None, 0) for metric in updated["follow_up"]["metrics"])
    assert updated["status"] == "draft" and updated["outcome"] is None


@pytest.mark.parametrize(
    ("filters", "expected_latest"),
    [
        ({"start_month": "2024-02", "end_month": "2024-03", "severity": "Minor"}, "2024-02-20"),
        ({"start_month": "2024-04", "end_month": "2024-04", "severity": "Minor"}, None),
    ],
)
@pytest.mark.asyncio
async def test_comparison_latest_date_uses_exact_retained_scope_and_excludes_out_of_period_rows(
    tmp_path, filters, expected_latest
):
    prepared = snapshot(
        [
            event(1, defect_date="2024-01-10", severity="Minor"),
            event(2, defect_date="2024-02-20", severity="Minor"),
            event(3, defect_date="2024-03-30", severity="Critical"),
            event(4, defect_date="2024-06-25", severity="Minor"),
            event(5, defect_date="2026-06-20", severity="Minor"),
        ]
    )
    assert any(item.defect_date.year == 2026 for item in prepared.events)
    subject = service(tmp_path, Loader(prepared))
    baseline = await subject.analysis.get_context(filters)
    check = await subject.check_source(baseline["context"]["context_id"])
    assert check["coverage"]["latest_event_date"] == expected_latest
    assert check["scope_unchanged"] and check["status"] == "unchanged"


@pytest.mark.asyncio
async def test_failed_source_check_preserves_baseline_and_records_unknown_comparison(tmp_path):
    loader = Loader(snapshot(), SourceError("Synthetic source unavailable"))
    subject = service(tmp_path, loader)
    record = await create(subject)
    check = await subject.check_source(record["context_id"])
    assert check["status"] == "unavailable" and check["review_required"]
    assert check["current_context_id"] is None and check["source_changed"] is None
    assert check["metrics"] == []
    assert all(value is None for value in check["data_changes"].values())
    assert subject.list_cases()["total"] == 1
    assert subject.get_case(record["id"])["context_id"] == record["context_id"]
    assert len(subject.list_checks()["checks"]) == 1


@pytest.mark.parametrize(
    ("field", "selected", "replacement"),
    [
        ("severity", "Critical", "Minor"),
        ("defect_type", "Functional", "Cosmetic"),
        ("defect_location", "Component", "Surface"),
        ("inspection_method", "Visual Inspection", "Automated Testing"),
    ],
)
@pytest.mark.asyncio
async def test_source_check_preserves_saved_event_scope_when_value_disappears_and_returns(
    tmp_path, field, selected, replacement
):
    initial = snapshot([event(1, **{field: selected}), event(2, **{field: replacement})])
    revised = snapshot([event(1, **{field: replacement}), event(2, **{field: replacement})])
    subject = service(tmp_path, Loader(initial, revised))
    baseline = await subject.analysis.get_context({field: selected})
    original_context = baseline["context"]
    record = await create(subject, context_id=original_context["context_id"])
    check = await subject.check_source(original_context["context_id"])
    empty_context_id = check["current_context_id"]
    current = await subject.analysis.context_by_id(empty_context_id)
    assert current["context"]["filters"] == original_context["filters"]
    assert current["context"]["snapshot_id"] != original_context["snapshot_id"]
    assert current["context"]["code_sha256"] == original_context["code_sha256"]
    assert current["analysis"]["kpis"]["event_count"] == 0
    assert (
        current["analysis"]["kpis"]["reference_output"]
        == baseline["analysis"]["kpis"]["reference_output"]
    )
    assert (await subject.analysis.records(empty_context_id))["rows"] == []
    counts = next(item for item in check["metrics"] if item["key"] == "event_count")
    assert counts == {"key": "event_count", "before": 1, "after": 0, "delta": -1}
    assert check["data_changes"] == {"added_events": 0, "removed_events": 1, "changed_events": 0}
    assert check["coverage"]["latest_event_date"] is None
    assert check["coverage"]["reporting_complete"] is None
    assert subject.get_case(record["id"])["context"] == original_context
    assert len(subject.list_checks()["checks"]) == 1
    for value in (selected, "never-seen-value"):
        with pytest.raises(ValueError, match="Unknown"):
            await subject.analysis.get_context({field: value})
    attached = await subject.add_follow_up(record["id"], 1, empty_context_id, confirmed=True)
    assert attached["follow_up"]["current_context_id"] == empty_context_id
    # The empty, inherited scope remains independently reconstructible after a restart.
    restored = service(tmp_path, Loader(revised, initial))
    assert (await restored.analysis.context_by_id(empty_context_id))["analysis"]["kpis"][
        "event_count"
    ] == 0
    returned = await restored.check_source(empty_context_id)
    returned_counts = next(item for item in returned["metrics"] if item["key"] == "event_count")
    assert returned_counts == {"key": "event_count", "before": 0, "after": 1, "delta": 1}
    assert returned["current_context_id"] == original_context["context_id"]
    assert returned["data_changes"] == {"added_events": 1, "removed_events": 0, "changed_events": 0}


@pytest.mark.asyncio
async def test_follow_up_uses_same_filters_keeps_baseline_and_audits_both_receipts(tmp_path):
    subject = service(
        tmp_path,
        Loader(snapshot([event()]), snapshot([event(), event(2, defect_date="2024-02-01")])),
    )
    record = await create(subject)
    changed = await subject.analysis.refresh()
    wrong_scope = await subject.analysis.get_context({"severity": "Critical"})
    with pytest.raises(ValueError, match="same filters"):
        await subject.add_follow_up(
            record["id"], 1, wrong_scope["context"]["context_id"], confirmed=True
        )
    assert subject.get_case(record["id"])["version"] == 1
    result = await subject.add_follow_up(
        record["id"],
        1,
        changed["context"]["context_id"],
        confirmed=True,
        note="Source revision reviewed; no causal conclusion",
    )
    assert result["context_id"] == record["context_id"]
    assert result["follow_up"]["current_context_id"] == changed["context"]["context_id"]
    assert result["follow_up"]["data_changes"]["added_events"] == 1
    assert result["follow_up"]["baseline_provenance"]["provenance"] == record["provenance"]
    assert result["history"][-1]["kind"] == "follow_up_added"
    assert "no causal conclusion" in result["notes"][-1]["text"]
    with pytest.raises(OperationConflict):
        await subject.add_follow_up(
            record["id"], 1, changed["context"]["context_id"], confirmed=True
        )


def test_http_contract_rejects_spoofed_actors_immutable_fields_and_stale_writes(tmp_path):
    subject = service(tmp_path)
    payload = asyncio.run(subject.analysis.get_context())
    with client(subject) as http:
        body = {
            "context_id": payload["context"]["context_id"],
            "title": "Review operation",
            "idempotency_key": "http-request-1",
            "origin_report_id": "42",
        }
        assert (
            http.post("/api/operations/cases", json={**body, "actor": "agent"}).status_code == 422
        )
        created = http.post("/api/operations/cases", json=body)
        assert created.status_code == 200
        record = created.json()
        assert record["origin_report_id"] == "42"
        url = "/api/operations/cases/" + record["id"]
        assert http.get(url).json()["id"] == record["id"]
        assert (
            http.get("/api/operations/cases", params={"context_id": record["context_id"]}).json()[
                "total"
            ]
            == 1
        )
        assert (
            http.patch(
                url, json={"expected_version": 1, "changes": {"context_id": "a" * 64}}
            ).status_code
            == 422
        )
        assert (
            http.patch(
                url, json={"expected_version": True, "changes": {"title": "Bool version"}}
            ).status_code
            == 422
        )
        assert (
            http.post(
                url + "/transitions",
                json={"expected_version": 1, "status": "acknowledged", "confirmed": "true"},
            ).status_code
            == 403
        )
        assert (
            http.post(
                url + "/updates",
                json={
                    "expected_version": 1,
                    "changes": {"title": "Must not partly save"},
                    "status": "resolved",
                    "outcome": "Invalid transition",
                    "confirmed": True,
                },
            ).status_code
            == 409
        )
        assert http.get(url).json()["title"] == record["title"]
        changed = http.patch(url, json={"expected_version": 1, "changes": {"title": "Edited case"}})
        assert changed.status_code == 200 and changed.json()["version"] == 2
        assert (
            http.patch(
                url, json={"expected_version": 1, "changes": {"title": "Stale edit"}}
            ).status_code
            == 409
        )
        saved = http.post(
            url + "/updates",
            json={
                "expected_version": 2,
                "changes": {"title": "Atomic edit"},
                "status": "acknowledged",
                "note": "Reviewed together",
                "confirmed": True,
            },
        )
        assert saved.status_code == 200 and saved.json()["version"] == 3
        assert saved.json()["title"] == "Atomic edit" and saved.json()["status"] == "acknowledged"
        assert http.get("/api/operations/cases/" + "a" * 32).status_code == 404
        assert (
            http.post(
                "/api/operations/cases",
                content="not json",
                headers={"content-type": "application/json"},
            ).status_code
            == 422
        )
        checked = http.post(
            "/api/operations/check-source", json={"context_id": record["context_id"]}
        )
        assert checked.status_code == 200 and checked.json()["status"] == "unchanged"
        assert len(http.get("/api/operations/source-checks").json()["checks"]) == 1


@pytest.mark.asyncio
async def test_failed_transaction_does_not_save_state_without_audit(tmp_path, monkeypatch):
    subject = service(tmp_path)
    record = await create(subject)

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit write failure")

    monkeypatch.setattr(subject, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit"):
        await subject.update_case(record["id"], 1, {"title": "Must roll back"})
    current = subject.get_case(record["id"])
    assert current["version"] == 1 and current["title"] == record["title"]
    assert len(current["history"]) == 1


@pytest.mark.asyncio
async def test_agent_content_proof_is_required_and_only_the_runtime_can_supply_it(tmp_path):
    subject = service(tmp_path)
    payload = await subject.analysis.get_context()
    values = {
        "context_id": payload["context"]["context_id"],
        "title": "Review evidence",
        "idempotency_key": "proof-required-1",
    }
    for missing in (None, {}, content_proofs(values["context_id"], title=values["title"])):
        with pytest.raises(ValueError, match="content_evidence"):
            await subject.create_case(
                values, actor="agent", authorized=True, content_evidence=missing
            )
    with pytest.raises(OperationForbidden, match="runtime"):
        await subject.create_case(values, content_evidence={})
    with pytest.raises(ValueError, match="immutable"):
        await subject.create_case(
            {**values, "content_evidence": {}}, actor="agent", authorized=True
        )
    assert operation_counts(subject) == (0, 0, 0)
    record = await subject.create_case(
        values,
        actor="agent",
        authorized=True,
        content_evidence=content_proofs(
            values["context_id"], title=values["title"], description=""
        ),
    )
    for actor, evidence, error in (("agent", None, ValueError), ("user", {}, OperationForbidden)):
        with pytest.raises(error, match="content_evidence"):
            await subject.update_case(
                record["id"],
                1,
                {"description": "Another review"},
                actor=actor,
                authorized=True,
                content_evidence=evidence,
            )
    assert subject.get_case(record["id"]) == record


@pytest.mark.parametrize(
    "replacement",
    [
        {"context_id": "f" * 64},
        {"validation_version": "unchecked"},
        {"run_id": " "},
        {"content_sha256": "f" * 64},
        {"annotated_sha256": "f" * 64},
        {"validator_code_sha256": "not-a-hash"},
        {"claims": {}},
        {"receipts": {}},
        {"untrusted": True},
    ],
)
@pytest.mark.asyncio
async def test_invalid_agent_proof_never_writes_a_case_or_audit(tmp_path, replacement):
    subject = service(tmp_path)
    payload = await subject.analysis.get_context()
    values = {
        "context_id": payload["context"]["context_id"],
        "title": "Review evidence",
        "idempotency_key": "invalid-proof-1",
    }
    evidence = content_proofs(values["context_id"], title=values["title"], description="")
    evidence["title"].update(replacement)
    with pytest.raises(ValueError):
        await subject.create_case(values, actor="agent", authorized=True, content_evidence=evidence)
    assert operation_counts(subject) == (0, 0, 0)


@pytest.mark.parametrize(
    ("stored", "annotated"),
    [
        ("Different reviewed text", "Original reviewed text"),
        ("Review [value:unresolved] records", "Review [value:unresolved] records"),
        ("Review [claim] records", "Review [claim] records"),
        ("Review [claim:unknown] records", "Review [claim:unknown] records"),
        ("Review records", "Review [claim:unknown] records"),
    ],
)
@pytest.mark.asyncio
async def test_agent_proof_must_produce_exact_clean_text_and_bind_every_marker(
    tmp_path, stored, annotated
):
    subject = service(tmp_path)
    payload = await subject.analysis.get_context()
    context_id = payload["context"]["context_id"]
    evidence = content_proofs(context_id, title=stored, description="")
    evidence["title"].update(annotated_text=annotated, annotated_sha256=sha256(annotated))
    with pytest.raises(ValueError, match="content_evidence"):
        await subject.create_case(
            {"context_id": context_id, "title": stored, "idempotency_key": "unclean-proof-1"},
            actor="agent",
            authorized=True,
            content_evidence=evidence,
        )
    assert operation_counts(subject) == (0, 0, 0)


@pytest.mark.asyncio
async def test_content_proof_binds_clean_text_claims_and_receipts_and_survives_reopen(tmp_path):
    subject = service(tmp_path)
    payload = await subject.analysis.get_context()
    context_id = payload["context"]["context_id"]
    evidence = content_proofs(
        context_id, title="Review selected records", description="Selected 4 records."
    )
    annotated = "  Selected 4 [claim:rows] \t records.  "
    evidence["description"].update(
        annotated_text=annotated,
        annotated_sha256=sha256(annotated),
        claims=[
            {
                "id": "rows",
                "execution_id": "synthetic-receipt",
                "path": "$.kpis.event_count",
                "value": 4.0,
                "code_sha256": "b" * 64,
            }
        ],
        receipts=[
            {
                "execution_id": "synthetic-receipt",
                "entry_sha256": "c" * 64,
                "result_sha256": "d" * 64,
                "code_sha256": "b" * 64,
            }
        ],
    )
    expected = copy.deepcopy(evidence)
    record = await subject.create_case(
        {
            "context_id": context_id,
            "title": "Review selected records",
            "description": "  Selected 4 records.  ",
            "idempotency_key": "clean-proof-1",
        },
        actor="agent",
        authorized=True,
        content_evidence=evidence,
    )
    assert record["description"] == "Selected 4 records."
    assert record["content_evidence"] == expected
    assert record["history"][0]["changes"]["content_evidence"] == expected
    evidence["description"]["claims"][0]["value"] = 999
    record["content_evidence"]["description"]["run_id"] = "changed-outside-store"
    restored = service(tmp_path).get_case(record["id"])
    assert restored["content_evidence"] == expected
    assert restored["history"][0]["changes"]["content_evidence"] == expected


@pytest.mark.parametrize(
    "damage",
    ["claim_value", "claim_duplicate", "receipt_missing", "receipt_hash", "receipt_duplicate"],
)
@pytest.mark.asyncio
async def test_malformed_claim_receipt_bindings_fail_before_agent_update(tmp_path, damage):
    subject = service(tmp_path)
    record = await agent_create(subject)
    evidence = content_proofs(record["context_id"], description="Review 4 records")
    proof = evidence["description"]
    proof.update(
        annotated_text="Review 4 [claim:rows] records",
        annotated_sha256=sha256("Review 4 [claim:rows] records"),
        claims=[
            {
                "id": "rows",
                "execution_id": "receipt",
                "path": "$.kpis.event_count",
                "value": 4.0,
                "code_sha256": "b" * 64,
            }
        ],
        receipts=[
            {
                "execution_id": "receipt",
                "entry_sha256": "c" * 64,
                "result_sha256": "d" * 64,
                "code_sha256": "b" * 64,
            }
        ],
    )
    if damage == "claim_value":
        proof["claims"][0]["value"] = float("nan")
    elif damage == "claim_duplicate":
        proof["claims"].append(copy.deepcopy(proof["claims"][0]))
    elif damage == "receipt_missing":
        proof["receipts"] = []
    elif damage == "receipt_hash":
        proof["receipts"][0]["code_sha256"] = "f" * 64
    else:
        proof["receipts"].append(copy.deepcopy(proof["receipts"][0]))
    with pytest.raises(ValueError):
        await subject.update_case(
            record["id"],
            record["version"],
            {"description": "Review 4 records"},
            actor="agent",
            authorized=True,
            content_evidence=evidence,
        )
    assert subject.get_case(record["id"]) == record
    assert operation_counts(subject) == (1, 1, 1)


@pytest.mark.parametrize("atomic", [False, True])
@pytest.mark.asyncio
async def test_human_edits_invalidate_only_changed_field_proof_and_audit_original(tmp_path, atomic):
    subject = service(tmp_path)
    record = await agent_create(subject)
    original = record["content_evidence"]
    changes = {
        "title": "Human revised working title",
        "description": " " + record["description"] + " ",
        "action_plan": None,
        "priority": "high",
    }
    if atomic:
        changed = await subject.save_case(
            record["id"],
            1,
            changes=changes,
            status="acknowledged",
            note="Human review",
            confirmed=True,
        )
    else:
        changed = await subject.update_case(record["id"], 1, changes)
    retained = {
        key: value for key, value in original.items() if key in {"description", "proposed_owner"}
    }
    assert changed["content_evidence"] == retained
    assert changed["version"] == 2 and len(changed["history"]) == 2
    assert changed["history"][-1]["changes"]["content_evidence"] == {
        "before": original,
        "after": retained,
    }
    assert changed["history"][0]["changes"]["content_evidence"] == original
    assert "description" not in changed["history"][-1]["changes"]
    assert service(tmp_path).get_case(record["id"])["content_evidence"] == retained


@pytest.mark.asyncio
async def test_status_notes_and_follow_up_preserve_narrative_proof(tmp_path):
    subject = service(tmp_path)
    record = await agent_create(subject)
    original = record["content_evidence"]
    record = await subject.add_note(record["id"], record["version"], "Human note")
    for status in ("acknowledged", "investigating", "action_planned"):
        record = await advance(subject, record, status)
    record = await subject.save_case(
        record["id"],
        record["version"],
        status="resolved",
        outcome="Reviewed historical records",
        confirmed=True,
    )
    record = await subject.add_follow_up(
        record["id"], record["version"], record["context_id"], confirmed=True
    )
    assert record["content_evidence"] == original
    assert all("content_evidence" not in item["changes"] for item in record["history"][1:])


@pytest.mark.asyncio
async def test_agent_partial_proof_merge_clear_and_same_text_revalidation_are_versioned(tmp_path):
    subject = service(tmp_path)
    record = await agent_create(subject)
    original = copy.deepcopy(record["content_evidence"])
    supplied = content_proofs(record["context_id"], description=record["description"])
    unchanged = await subject.update_case(
        record["id"],
        1,
        {"description": record["description"]},
        actor="agent",
        authorized=True,
        content_evidence=supplied,
    )
    assert unchanged == record
    supplied["description"]["run_id"] = "revalidation-run"
    updated = await subject.update_case(
        record["id"],
        1,
        {"description": record["description"]},
        actor="agent",
        authorized=True,
        content_evidence=supplied,
    )
    assert updated["version"] == 2 and len(updated["history"]) == 2
    assert set(updated["history"][-1]["changes"]) == {"content_evidence"}
    assert updated["content_evidence"] == {**original, **supplied}
    cleared = await subject.update_case(
        record["id"],
        2,
        {"proposed_owner": " ", "action_plan": None, "priority": "high"},
        actor="agent",
        authorized=True,
        content_evidence={},
    )
    assert cleared["proposed_owner"] is None and cleared["action_plan"] is None
    assert set(cleared["content_evidence"]) == {"title", "description"}
    assert cleared["content_evidence"]["description"] == supplied["description"]


@pytest.mark.asyncio
async def test_legacy_case_read_exposes_empty_evidence_without_mutating_original_row(tmp_path):
    subject = service(tmp_path)
    record = await create(subject)
    with subject._connect(write=True) as db:
        stored = subject._row(db, record["id"])
        stored.pop("content_evidence")
        raw = canonical(stored)
        db.execute("UPDATE operation_cases SET payload=? WHERE id=?", (raw, record["id"]))
    assert subject.get_case(record["id"])["content_evidence"] == {}
    assert subject.list_cases()["cases"][0]["content_evidence"] == {}
    with subject._connect() as db:
        after = db.execute(
            "SELECT payload FROM operation_cases WHERE id=?", (record["id"],)
        ).fetchone()[0]
    assert after == raw and "content_evidence" not in json.loads(after)


@pytest.mark.asyncio
async def test_idempotent_agent_create_returns_original_proof_without_rewriting_it(tmp_path):
    subject = service(tmp_path)
    record = await agent_create(subject)
    values = {
        key: record[key]
        for key in ("title", "description", "proposed_owner", "action_plan", "context_id")
    }
    values["idempotency_key"] = "agent-evidence-1"
    evidence = copy.deepcopy(record["content_evidence"])
    for proof in evidence.values():
        proof["run_id"] = "different-retry-run"
    replay = await subject.create_case(
        values, actor="agent", authorized=True, content_evidence=evidence
    )
    assert replay == record and replay["content_evidence"] != evidence
    assert operation_counts(subject) == (1, 1, 1)


@pytest.mark.parametrize("atomic", [False, True])
@pytest.mark.asyncio
async def test_proof_and_text_rollback_together_when_audit_write_fails(
    tmp_path, monkeypatch, atomic
):
    subject = service(tmp_path)
    record = await agent_create(subject)

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit write failure")

    monkeypatch.setattr(subject, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit"):
        if atomic:
            await subject.save_case(
                record["id"], 1, changes={"title": "Human changed title"}, confirmed=True
            )
        else:
            await subject.update_case(
                record["id"],
                1,
                {"title": "Agent changed title"},
                actor="agent",
                authorized=True,
                content_evidence=content_proofs(record["context_id"], title="Agent changed title"),
            )
    assert subject.get_case(record["id"]) == record
    with pytest.raises(RuntimeError, match="audit"):
        await agent_create(subject, idempotency_key="failed-proof-create")
    assert operation_counts(subject) == (1, 1, 1)


def test_http_cannot_supply_or_replace_server_content_evidence(tmp_path):
    subject = service(tmp_path)
    record = asyncio.run(agent_create(subject))
    url = "/api/operations/cases/" + record["id"]
    with client(subject) as http:
        body = {
            "context_id": record["context_id"],
            "title": "Spoofed proof",
            "idempotency_key": "http-proof-1",
            "content_evidence": {},
        }
        assert http.post("/api/operations/cases", json=body).status_code == 422
        assert (
            http.patch(
                url,
                json={
                    "expected_version": 1,
                    "changes": {"title": "Changed"},
                    "content_evidence": {},
                },
            ).status_code
            == 422
        )
        assert (
            http.patch(
                url, json={"expected_version": 1, "changes": {"content_evidence": {}}}
            ).status_code
            == 422
        )
        assert (
            http.post(
                url + "/updates",
                json={
                    "expected_version": 1,
                    "changes": {"title": "Changed"},
                    "content_evidence": {},
                    "confirmed": True,
                },
            ).status_code
            == 422
        )
        assert (
            http.post(
                url + "/updates",
                json={
                    "expected_version": 1,
                    "changes": {"content_evidence": {}},
                    "confirmed": True,
                },
            ).status_code
            == 422
        )
        assert http.get(url).json() == record
    assert operation_counts(subject) == (1, 1, 1)
