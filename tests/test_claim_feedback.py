"""Typed labels and atomic citation repair use actual deterministic tool receipts."""

from __future__ import annotations

import json
from dataclasses import fields

import pytest
import pytest_asyncio
from analyst.case_study.analytics import Scope
from analyst.case_study.preparation import build_snapshot
from analyst.config import Settings
from analyst.evidence import canonical, sha256
from analyst.runtime import AnalystRuntime, ReportDraft, audit_field_reports, audit_report
from analyst.store import SessionStore
from analyst.tools import AnalystTools
from test_dashboard import Loader, event, service


def filters(**changes):
    return {**{field.name: None for field in fields(Scope)}, **changes}


@pytest_asyncio.fixture
async def evidence_subject(tmp_path):
    master = b"product_id,product_name,category,production_line,monthly_output\n63,Widget A,Category A,Line-1,1000\n81,Widget B,Category A,Line-1,1000\n83,Widget C,Category B,Line-2,1000\n"
    rows = []
    for month, product, count in (
        (1, 63, 2),
        (1, 81, 2),
        (1, 83, 6),
        (6, 63, 8),
        (6, 81, 7),
        (6, 83, 1),
    ):
        for _ in range(count):
            rows.append(
                event(len(rows) + 1, product_id=product, defect_date=f"2024-{month:02d}-15")
            )
    snapshot = build_snapshot(
        rows,
        {
            "source_url": "https://synthetic.invalid/events",
            "source_sha256": sha256(canonical(rows)),
            "fetched_at": "2026-01-01T00:00:00+00:00",
        },
        master,
    )
    backend = service(tmp_path, Loader(snapshot))
    bound = await backend.get_context()
    store = SessionStore(tmp_path / "sessions")
    sid = store.create_session()["id"]
    tools = AnalystTools(tmp_path / "run", [], session_id=sid, memory_enabled=False)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    runtime = AnalystRuntime(
        Settings(mode="live", api_key="offline", memory_enabled=False),
        tools,
        emit,
        analysis_context=bound,
        analysis_service=backend,
        state_store=store,
    )
    compared = json.loads(
        await runtime._call(
            "analysis_compare",
            {
                "baseline_filters": filters(start_month="2024-01", end_month="2024-01"),
                "comparison_filters": filters(start_month="2024-06", end_month="2024-06"),
                "dimension": "product_id",
            },
        )
    )
    january = json.loads(
        await runtime._call(
            "analysis_query",
            {"section": "products", "filters": filters(start_month="2024-01", end_month="2024-01")},
        )
    )
    june = json.loads(
        await runtime._call(
            "analysis_query",
            {"section": "products", "filters": filters(start_month="2024-06", end_month="2024-06")},
        )
    )

    def identity(view, path):
        return next(item["id"] for item in view["numeric_evidence"] if item["path"] == path)

    ids = {
        "product63": identity(compared, "$.decomposition.rows[0].event_count_change"),
        "january6": identity(january, "$.analysis.products.top[0].event_count"),
        "june8": identity(june, "$.analysis.products.top[0].event_count"),
        "delta": identity(compared, "$.decomposition.event_count_change"),
    }
    yield runtime, tools, store, sid, ids, june, events
    await tools.close()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ("Product decomposition: ID 63 changed by [value:TOKEN] events.", True),
        ("product_id 63 changed by [value:TOKEN] events.", True),
        ("Product ID 63 changed by [value:TOKEN] events.", True),
        ("Product decomposition: ID 6 changed by [value:TOKEN] events.", False),
        ("Product decomposition: ID 999 changed by [value:TOKEN] events.", False),
        ("ID 63 changed by [value:TOKEN] events.", False),
        ("Product decomposition: ID 63 had 63 events [value:TOKEN].", False),
    ],
)
async def test_typed_identifier_binding_cannot_borrow_a_measurement_value(
    evidence_subject, text, valid
):
    runtime, tools, _, _, ids, _, _ = evidence_subject
    report = runtime.resolve_report(ReportDraft(report=text.replace("TOKEN", ids["product63"])))
    audit = audit_report(report, tools.ledger)
    assert audit["verified"] is valid, audit


@pytest.mark.asyncio
async def test_save_reports_every_invalid_field_and_offers_exact_period_refs_atomically(
    evidence_subject,
):
    runtime, tools, state, sid, ids, june, _ = evidence_subject
    before = state.investigation(sid)
    arguments = {
        "question": "Investigate product mix",
        "summary": "A descriptive comparison.",
        "findings": [
            f"June's top product has 8 events [value:{ids['january6']}].",
            f"The repeated total is 105 events [value:{ids['january6']}].",
            "No events occurred after day 28.",
        ],
        "hypotheses": [],
        "next_steps": ["Review 999 events."],
        "priorities": [],
        "filters": filters(),
    }
    rejected = json.loads(await runtime._call("investigation_update", arguments))
    result = rejected["result"]
    assert rejected["status"] == "failed", rejected
    assert set(result["error_fields"]) == {
        "findings[0]",
        "findings[1]",
        "findings[2]",
        "next_steps[0]",
    }
    assert state.investigation(sid) == before
    assert tools.ledger.artifacts() == []
    errors = {item["field"]: item for item in result["validation_errors"]}
    first = errors["findings[0]"]
    assert first["referenced_evidence"][0]["value"] == 6
    assert first["referenced_evidence"][0]["scope"]["start_month"] == "2024-01"
    offered = next(item for item in first["candidate_evidence"] if item["id"] == ids["june8"])
    assert offered["scope"]["start_month"] == "2024-06" and offered["labels"]["product_id"] == 63
    assert errors["findings[2]"]["candidate_evidence"] == []
    assert len(canonical(rejected).encode()) < 12000
    assert any(
        row["scope"].get("start_month") == "2024-06"
        and row["measures"].get("event_count", {}).get("marker") == offered["marker"]
        for row in june["citation_rows"]
    )
    repaired = {
        **arguments,
        "findings": [
            f"June product_id 63 has {offered['marker']} events.",
            f"The overall change is [value:{ids['delta']}].",
        ],
        "next_steps": ["Collect actual production and inspect reporting completeness."],
    }
    saved = json.loads(await runtime._call("investigation_update", repaired))
    assert saved["status"] == "completed", saved
    current = state.investigation(sid)
    assert current["content_status"] == "unverified" and current["semantic_review"] is None
    assert current["findings"][0]["text"] == "June product_id 63 has 8 events."
    assert tools.ledger.verify_integrity() == []


@pytest.mark.asyncio
async def test_batch_audit_preserves_field_and_physical_line_citation_boundaries(evidence_subject):
    runtime, tools, _, _, ids, _, _ = evidence_subject
    reports = {
        "summary": runtime.resolve_report(ReportDraft(report="Uncited 8 events.")),
        "findings[0]": runtime.resolve_report(
            ReportDraft(report=f"June has [value:{ids['june8']}] events.")
        ),
        "findings[1]": runtime.resolve_report(
            ReportDraft(
                report=f"A supported count [value:{ids['january6']}].\nA repeated 8 without its marker."
            )
        ),
    }
    audits = audit_field_reports(reports, tools.ledger)
    assert not audits["summary"]["verified"]
    assert audits["findings[0]"]["verified"]
    assert audits["findings[1]"]["numeric_issues"][0]["line"] == 2
    assert all(
        audit["verified"] == audit_report(reports[field], tools.ledger)["verified"]
        for field, audit in audits.items()
    )


@pytest.mark.asyncio
async def test_first_analysis_call_carries_visible_plan_without_a_planning_only_tool(
    evidence_subject,
):
    runtime, tools, _, _, _, _, events = evidence_subject
    assert "todo_planner" not in {tool.name for tool in runtime.sdk_tools()}
    events.clear()
    plan = "Verify period counts, compare product mix, then save open hypotheses."
    result = json.loads(
        await runtime._call(
            "analysis_query", {"section": "kpis", "filters": filters(), "plan": plan}
        )
    )
    assert (
        result["status"] == "completed"
        and result["result"]["analysis"]["kpis"]["event_count"] == 26
    )
    kinds = [kind for kind, _ in events]
    assert kinds.index("todo") < kinds.index("tool_completed")
    assert tools.todos == [{"title": plan, "status": "in_progress"}]


@pytest.mark.asyncio
async def test_records_repeat_derived_query_filters_without_following_ui(
    evidence_subject,
):
    runtime, tools, _, _, _, _, _ = evidence_subject
    selected = filters(
        production_line="Line-1", severity="Critical", start_month="2024-06", end_month="2024-06"
    )
    records = json.loads(
        await runtime._call(
            "analysis_records",
            {
                "page": 1,
                "page_size": 50,
                "sort_by": "defect_id",
                "sort_order": "asc",
                "filters": selected,
            },
        )
    )
    assert records["status"] == "completed", records
    assert (
        records["scope"]["start_month"] == "2024-06"
        and records["scope"]["production_line"] == "Line-1"
    )
    rows = records["result"]["rows"]
    assert rows and all(
        row["severity"] == "Critical"
        and row["product_id"] in {63, 81}
        and row["defect_date"].startswith("2024-06")
        for row in rows
    )
    assert runtime.analysis_context["context"]["filters"]["production_line"] == ""
    cost = next(
        item for item in records["numeric_evidence"] if item["path"] == "$.rows[0].repair_cost"
    )
    valid = runtime.resolve_report(
        ReportDraft(
            report=f"Defect ID {rows[0]['defect_id']} has observed cost [value:{cost['id']}]."
        )
    )
    assert audit_report(valid, tools.ledger)["verified"]
    wrong = runtime.resolve_report(
        ReportDraft(report=f"Defect ID 10 has observed cost [value:{cost['id']}].")
    )
    assert not audit_report(wrong, tools.ledger)["verified"]
