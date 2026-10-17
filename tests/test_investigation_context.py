"""Operational inventory is saved proof, not an analytical comparison population."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
import test_cost_grain
from analyst.agent_views import qa_comparisons
from analyst.claim_feedback import reference_context
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.runtime import AnalystRuntime, ReportDraft, audit_report, clean_draft_text
from analyst.tools import AnalystTools
from test_selective_repair import (
    AUTHORED,
    changed_hypotheses,
    filters,
    partial,
    save,
)
from test_selective_repair import subject as subject

grain_subject = test_cost_grain.subject


async def save_with_inventory(subject, tmp_path):
    prior = EvidenceLedger(tmp_path / "earlier-run")
    path = prior.run_dir / "artifacts" / "retained-notes.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"status":"Earlier local work, not a new observation"}', encoding="utf-8")
    artifact = prior.register_artifact(path, kind="investigation", context=subject.bound["context"])
    current = subject.store.investigation(subject.sid)
    inventory = [{**artifact, "run_id": prior.run_dir.name}]
    subject.store.save_investigation(subject.sid, current["version"], {"artifacts": inventory})
    saved = await save(subject, deepcopy(subject.initial))
    assert saved["status"] == "completed", saved
    return prior, inventory, saved


@pytest.mark.asyncio
async def test_inventory_removed_only_from_mutation_comparisons_and_full_proofs_remain(
    subject, tmp_path
):
    prior, inventory, saved = await save_with_inventory(subject, tmp_path)
    runtime = subject.runtime
    pending = deepcopy(runtime.product_tools.pending_state())
    records = deepcopy(subject.tools.ledger.records())
    entries = {entry["execution_id"]: entry for entry in records}
    mutation = next(entry for entry in records if entry["tool"] == "investigation_update")
    original_groups = qa_comparisons(mutation, [])
    artifact_group = next(
        group for group in original_groups if group["path"] == "$.investigation.artifacts"
    )
    assert artifact_group["population_size"] == 1
    assert artifact_group["rows"][0]["sha256"] == inventory[0]["sha256"]

    report = runtime.resolve_report(ReportDraft(report=subject.initial["summary"]))
    # Legacy artifact auditing remains available independently of live model output.
    report.deliverable_paths = [pending["artifact"]["path"]]
    support, errors = runtime.qa_evidence(report)
    assert not errors, errors
    item = next(
        item for item in support["qualitative_support"] if item["tool"] == "investigation_update"
    )
    existing_authored_exclusions = {
        "$.investigation.findings",
        "$.investigation.hypotheses",
        "$.investigation.priorities",
        "$.investigation.next_steps",
    }
    assert item["comparative_aggregates"] == [
        group
        for group in original_groups
        if group["path"] not in existing_authored_exclusions | {"$.investigation.artifacts"}
    ]
    assert any(
        group["values"].get("effect") == mutation["result"]["effect"]
        for group in item["aggregate_availability"]
    )
    assert support["numeric_support"]
    assert all(value["execution_id"] in entries for value in support["numeric_support"])
    assert support["verified_artifacts"][0]["sha256"] == saved["result"]["artifact"]["sha256"]

    # The ordinary QA caller adds this exact pending state, not a reduced copy.
    reviewed = {
        **support,
        "investigation_draft": {key: pending[key] for key in ("version", "state_sha256", "state")},
    }
    assert reviewed["investigation_draft"]["state"]["artifacts"] == inventory
    assert sha256(canonical(reviewed["investigation_draft"]["state"])) == pending["state_sha256"]
    assert runtime.product_tools.pending_state() == pending
    assert subject.store.investigation(subject.sid) == pending["state"]
    assert subject.tools.ledger.records() == records
    assert (
        sha256(prior.resolve_artifact(inventory[0]["path"]).read_bytes()) == inventory[0]["sha256"]
    )
    assert (
        json.loads(
            subject.tools.ledger.resolve_artifact(pending["artifact"]["path"]).read_text(
                encoding="utf-8"
            )
        )
        == pending["state"]
    )


@pytest.mark.asyncio
async def test_inventory_filter_does_not_remove_analytical_groups_with_the_same_path(subject):
    context = subject.bound["context"]
    entry = subject.tools.ledger.record(
        "analysis_query",
        "# Synthetic analytical collection, not operational inventory",
        {
            "ok": True,
            "context": context,
            "investigation": {
                "artifacts": [
                    {"name": "Earlier group", "event_count": 2, "known_repair_cost": None},
                    {"name": "Counterexample", "event_count": 7, "known_repair_cost": 10},
                    {"name": "Tied group", "event_count": 7, "known_repair_cost": 10},
                ]
                + [
                    {"name": f"Other group {index}", "event_count": 1, "known_repair_cost": None}
                    for index in range(15)
                ]
            },
        },
        execution_kind="bounded_analysis",
        analysis_context=context,
    )
    subject.runtime.model_receipt(entry)
    report = subject.runtime.resolve_report(ReportDraft(report="Inspect the declared populations."))
    support, errors = subject.runtime.qa_evidence(report)
    assert not errors, errors
    item = next(
        item
        for item in support["qualitative_support"]
        if entry["execution_id"] in item.get("equivalent_receipt_ids", [])
    )
    expected = qa_comparisons(entry, [])
    assert item["comparative_aggregates"] == expected
    group = next(group for group in expected if group["path"] == "$.investigation.artifacts")
    assert group["population_size"] == 18
    assert group["rows"][0]["known_repair_cost"] is None
    maximum = next(item["maximum"] for item in group["extrema"] if item["field"] == "$.event_count")
    assert maximum["value"] == 7 and maximum["matching_rows"] == 2
    assert maximum["example_paths"] == [
        "$.investigation.artifacts[1].event_count",
        "$.investigation.artifacts[2].event_count",
    ]


@pytest.mark.asyncio
async def test_repair_keeps_inventory_and_unaffected_authored_fields(subject, tmp_path):
    prior, inventory, _ = await save_with_inventory(subject, tmp_path)
    before = deepcopy(subject.store.investigation(subject.sid))
    repaired = await save(subject, partial(hypotheses=changed_hypotheses(subject)))
    assert repaired["status"] == "completed", repaired
    after = subject.store.investigation(subject.sid)
    for name in (
        "question",
        "summary",
        "findings",
        "next_steps",
        "priorities",
        "unit_notes",
        "artifacts",
    ):
        assert after[name] == before[name]
    assert after["artifacts"] == inventory
    assert after["content_status"] == "unverified" and after["semantic_review"] is None
    assert subject.runtime.product_tools.pending_state()["state_sha256"] == sha256(canonical(after))
    target = prior.resolve_artifact(inventory[0]["path"])
    target.write_text("changed bytes", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        prior.resolve_artifact(inventory[0]["path"])


@pytest.mark.asyncio
async def test_followup_first_save_needs_all_fields_and_current_run_markers(subject, tmp_path):
    await save_with_inventory(subject, tmp_path)
    old = deepcopy(subject.store.investigation(subject.sid))
    next_tools = AnalystTools(
        tmp_path / "next-run", [], session_id=subject.sid, memory_enabled=False
    )
    try:
        runtime = AnalystRuntime(
            subject.runtime.settings,
            next_tools,
            subject.runtime.emit,
            analysis_context=subject.bound,
            analysis_service=subject.analysis,
            state_store=subject.store,
        )
        first = json.loads(
            await runtime._call(
                "investigation_update",
                partial(summary="Continue the existing investigation with another explanation."),
            )
        )
        assert first["status"] == "failed", first
        assert subject.store.investigation(subject.sid) == old
        old_ids = json.loads(await runtime._call("investigation_update", deepcopy(subject.initial)))
        assert old_ids["status"] == "failed", old_ids
        assert subject.store.investigation(subject.sid) == old
        query = json.loads(
            await runtime._call("analysis_query", {"section": "kpis", "filters": filters()})
        )
        markers = {item["path"]: item["id"] for item in query["numeric_evidence"]}
        proposal = {
            **deepcopy(subject.initial),
            "summary": "Observed [value:"
            + markers["$.analysis.kpis.event_count"]
            + "] logged events.",
            "findings": [],
            "hypotheses": [],
            "next_steps": [],
            "priorities": [],
        }
        assert all(proposal[name] is not None for name in AUTHORED)
        result = json.loads(await runtime._call("investigation_update", proposal))
        assert result["status"] == "completed", result
        saved = subject.store.investigation(subject.sid)
        assert saved["artifacts"] == old["artifacts"]
        assert saved["content_status"] == "unverified"
        invalid = runtime.resolve_report(ReportDraft(report="Observed 999999 logged events."))
        assert not audit_report(invalid, next_tools.ledger)["verified"]
    finally:
        await next_tools.close()


@pytest.mark.parametrize(
    ("annotated", "expected"),
    [
        ("Recorded 148 [claim:a] . Next 346 [claim:b] .", "Recorded 148. Next 346."),
        ("Đã ghi 148 [claim:a] . Chi phí 346,25 [claim:b] .", "Đã ghi 148. Chi phí 346,25."),
        ("Cost 148.25 [claim:a], coverage 97.09 [claim:b] %.", "Cost 148.25, coverage 97.09 %."),
        ("Value 148 [claim:a] [claim:b] ; keep 346 [claim:c] !", "Value 148; keep 346!"),
        ("Value 148 [claim:a] .25", "Value 148 .25"),
        ("Value 148 [claim:a] ,25", "Value 148 ,25"),
        ("First 148 [claim:a]\n . Next 346 [claim:b].", "First 148 \n . Next 346."),
        (
            "Keep . authored, spacing ; and 1.25.\nNext line",
            "Keep . authored, spacing ; and 1.25.\nNext line",
        ),
    ],
)
def test_only_citation_adjacent_punctuation_gap_is_cleaned(annotated, expected):
    assert clean_draft_text(annotated) == expected


@pytest.mark.asyncio
async def test_saved_priority_punctuation_and_numeric_proof_stay_separate(subject):
    claim = next(
        claim
        for claim in subject.runtime.available_claims().values()
        if claim.path == "$.analysis.kpis.event_count"
    )
    annotated = "Recorded [value:" + claim.id + "] ."
    initial = deepcopy(subject.initial)
    initial["priorities"] = [
        {
            "evidence": annotated,
            "action": "Inspect recording completeness.",
            "owner": "Proposed quality role.",
            "metric": "Review logged event count; collect actual exposure for rates.",
        }
    ]
    result = await save(subject, initial)
    assert result["status"] == "completed", result
    pending = subject.runtime.product_tools.pending_state()
    saved = subject.store.investigation(subject.sid)
    assert claim.value == 4
    assert saved["priorities"][0]["evidence"] == "Recorded 4."
    assert saved["content_status"] == "unverified"
    assert pending["proposal"]["priorities"][0]["evidence"] == annotated
    assert pending["state_sha256"] == sha256(canonical(saved))
    report = subject.runtime.resolve_report(ReportDraft(report=annotated))
    assert claim in report.claims
    assert audit_report(report, subject.tools.ledger)["verified"]
    assert (
        json.loads(
            subject.tools.ledger.resolve_artifact(pending["artifact"]["path"]).read_text(
                encoding="utf-8"
            )
        )
        == saved
    )


@pytest.mark.asyncio
async def test_location_labels_follow_exact_row_and_target_context(grain_subject):
    runtime = grain_subject.runtime
    reports = []
    declarations = []
    for line in ("Line-1", "Line-2"):
        view = json.loads(
            await runtime._call(
                "analysis_query",
                {"section": "location_severity", "filters": filters(production_line=line)},
            )
        )
        assert view["status"] == "completed", view
        entry = next(
            item
            for item in grain_subject.tools.ledger.records()
            if item["execution_id"] == view["receipt_id"]
        )
        totals = entry["result"]["analysis"]["location_severity"]["location_totals"]
        markers = {item["path"]: item["id"] for item in view["numeric_evidence"]}
        for index, row in enumerate(totals):
            for metric in ("critical_count", "event_count", "critical_share_pct"):
                path = f"$.analysis.location_severity.location_totals[{index}].{metric}"
                source = reference_context(entry, path)
                assert source["labels"]["location"] == row["location"]
                assert source["scope"]["production_line"] == line
                declarations.append((entry["execution_id"], path, row["location"], line))
                suffix = " %" if metric == "critical_share_pct" else " events"
                reports.append(row["location"] + " [value:" + markers[path] + "]" + suffix + ".")
        if line == "Line-1":
            assert len({row["location"] for row in totals}) > 1
            assert len({row["event_count"] for row in totals}) < len(totals)
    report = runtime.resolve_report(ReportDraft(report="\n".join(reports)))
    support, errors = runtime.qa_evidence(report)
    assert not errors, errors
    for execution_id, path, location, line in declarations:
        item = next(
            item
            for item in support["numeric_support"]
            if item["execution_id"] == execution_id and item["path"] == path
        )
        assert item["labels"]["location"] == location
        assert item["scope"]["production_line"] == line
    assert runtime.analysis_context == grain_subject.bound


def test_absent_location_is_not_invented_from_neighboring_row_or_filter():
    entry = {
        "tool": "analysis_query",
        "analysis_context": {"filters": {"defect_location": "Component"}},
        "result": {
            "analysis": {
                "location_severity": {
                    "location_totals": [
                        {"location": "Surface", "event_count": 2},
                        {"event_count": 2},
                    ]
                }
            }
        },
    }
    first = reference_context(entry, "$.analysis.location_severity.location_totals[0].event_count")
    second = reference_context(entry, "$.analysis.location_severity.location_totals[1].event_count")
    assert first["labels"] == {"location": "Surface"}
    assert second["labels"] == {}
    assert second["scope"] == {"defect_location": "Component"}
