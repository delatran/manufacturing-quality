"""Bound real tool views and exports without network or model generation."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import fields
from zipfile import ZipFile

import pytest
from analyst.case_study.analytics import Scope
from analyst.case_study.preparation import build_snapshot
from analyst.config import Settings
from analyst.evidence import canonical, sha256
from analyst.runtime import AnalystReport, AnalystRuntime, ReportDraft, audit_report, result_at_path
from analyst.store import SessionStore
from analyst.tools import AnalystTools
from retained_dashboard_export_fixture import retained_dashboard_export
from test_dashboard import Loader, event, service


def filters(**changes):
    return {**{field.name: None for field in fields(Scope)}, **changes}


def broad_snapshot():
    master = "product_id,product_name,category,production_line,monthly_output\n" + "".join(
        f"{index},Widget {index},Category {index % 3},Line-{index % 4},1000\n"
        for index in range(1, 101)
    )
    rows = []
    for index in range(1, 101):
        for month, count in ((1, index % 3 + 1), (6, index % 5 + 2)):
            for ordinal in range(count):
                rows.append(
                    event(
                        len(rows) + 1,
                        product_id=index,
                        defect_date=f"2024-{month:02d}-15",
                        severity="Critical" if ordinal % 2 else "Minor",
                        repair_cost=10 if ordinal % 2 else 125.5,
                    )
                )
    return build_snapshot(
        rows,
        {
            "source_url": "https://synthetic.invalid/events",
            "source_sha256": sha256(canonical(rows)),
            "fetched_at": "2026-01-01T00:00:00+00:00",
        },
        master.encode(),
    )


async def subject_at(tmp_path, language="en", *, state_store=None, session_id="local"):
    subject = service(tmp_path, Loader(broad_snapshot()))
    bound = await subject.get_context()
    tools = AnalystTools(tmp_path / "run", [], memory_enabled=False, session_id=session_id)

    async def emit(*_):
        pass

    runtime = AnalystRuntime(
        Settings(mode="live", api_key="offline-test", memory_enabled=False),
        tools,
        emit,
        analysis_context=bound,
        analysis_service=subject,
        language=language,
        state_store=state_store,
    )
    return subject, bound, tools, runtime


@pytest.mark.asyncio
async def test_real_large_comparison_views_preserve_exact_claims_and_raw_receipts(tmp_path):
    _, _, tools, runtime = await subject_at(tmp_path)
    try:
        overview = json.loads(await runtime._call("dashboard_context", {}))
        comparison = json.loads(
            await runtime._call(
                "analysis_compare",
                {
                    "baseline_filters": filters(start_month="2024-01", end_month="2024-01"),
                    "comparison_filters": filters(start_month="2024-06", end_month="2024-06"),
                    "dimension": "product_id",
                },
            )
        )
        entries = {entry["execution_id"]: entry for entry in tools.ledger.records()}
        for view in (overview, comparison):
            assert len(canonical(view).encode()) < 24000
            source = entries[view["receipt_id"]]
            assert len(view["numeric_evidence"]) < 125
            for number in view["numeric_evidence"]:
                assert result_at_path(source["result"], number["path"]) == number["value"]
        raw = entries[comparison["receipt_id"]]
        assert "decomposition" in raw["result"]["baseline"]["analysis"]
        assert "decomposition" not in comparison["result"]["baseline"]["analysis"]
        assert len(raw["result"]["decomposition"]["rows"]) == 100
        assert comparison["result"]["decomposition"]["reconciliation"]["event_change_reconciles"]
        assert (
            comparison["omitted_collections"][0]["next_call"]["receipt_id"] == raw["execution_id"]
        )
        assert tools.ledger.verify_integrity() == []
    finally:
        await tools.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_edit", [False, True])
async def test_investigation_save_preserves_unseen_owner_edits_but_allows_ui_bookkeeping(
    tmp_path, owner_edit
):
    state = SessionStore(tmp_path / "sessions")
    sid = state.create_session()["id"]
    _, _, tools, runtime = await subject_at(tmp_path, state_store=state, session_id=sid)
    try:
        current = state.investigation(sid)
        changes = (
            {"summary": "Owner's newer content."}
            if owner_edit
            else {"ui": {**current["ui"], "panel": "records"}}
        )
        changed = state.save_investigation(sid, current["version"], changes)
        result = json.loads(
            await runtime._call(
                "investigation_update",
                {
                    "question": "Review retained events",
                    "summary": "Agent's proposed content.",
                    "findings": [],
                    "hypotheses": [],
                    "next_steps": [],
                    "priorities": [],
                    "filters": filters(),
                },
            )
        )
        saved = state.investigation(sid)
        if owner_edit:
            assert (
                result["status"] == "failed" and "edits were preserved" in result["result"]["error"]
            )
            assert saved == changed
        else:
            assert result["status"] == "completed", result
            assert saved["summary"] == "Agent's proposed content."
            assert saved["ui"]["panel"] == "records"
    finally:
        await tools.close()
        state.close()


@pytest.mark.asyncio
async def test_rich_detail_adapts_requested_page_and_qa_keeps_tail_extrema(tmp_path):
    _, _, tools, runtime = await subject_at(tmp_path)
    try:
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
        detail = json.loads(
            await runtime._call(
                "evidence_detail",
                {
                    "receipt_id": compared["receipt_id"],
                    "path": "$.decomposition.rows",
                    "offset": 0,
                    "limit": 100,
                },
            )
        )
        assert detail["status"] == "completed", detail
        assert 0 < detail["page"]["returned_count"] < 100
        assert detail["page"]["next_offset"] == detail["page"]["returned_count"]
        assert len(canonical(detail).encode()) < 24000
        tail = json.loads(
            await runtime._call(
                "evidence_detail",
                {
                    "receipt_id": compared["receipt_id"],
                    "path": "$.decomposition.rows[99].event_count_change",
                    "offset": 0,
                    "limit": 1,
                },
            )
        )
        identity = tail["numeric_evidence"][0]["id"]
        report = runtime.resolve_report(
            ReportDraft(report=f"A tail product's change is [value:{identity}].")
        )
        assert audit_report(report, tools.ledger)["verified"]
        evidence, errors = runtime.qa_evidence(
            report, delivery={"source_tables": [], "artifacts": []}
        )
        assert not errors
        assert len(canonical(evidence).encode()) < 40000
        groups = next(
            item for item in evidence["qualitative_support"] if item["tool"] == "analysis_compare"
        )["comparative_aggregates"]
        group = next(item for item in groups if item["path"] == "$.decomposition.rows")
        assert group["population_size"] == 100
        assert any(row["source_path"].endswith("[99]") for row in group["rows"])
        field = next(item for item in group["extrema"] if item["field"] == "$.event_count_change")
        raw = next(
            entry
            for entry in tools.ledger.records()
            if entry["execution_id"] == compared["receipt_id"]
        )
        values = [row["event_count_change"] for row in raw["result"]["decomposition"]["rows"]]
        assert field["minimum"]["value"] == min(values)
        assert field["maximum"]["value"] == max(values)
        assert field["maximum"]["matching_rows"] == values.count(max(values))
        assert len(evidence["numeric_support"]) == 1
    finally:
        await tools.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize("export_format", ["csv", "zip"])
async def test_retained_dashboard_export_uses_requested_locale_and_explicit_derived_scope(
    tmp_path, language, export_format
):
    subject, bound, tools, runtime = await subject_at(tmp_path, language)
    try:
        selected_filters = filters(production_line="Line-1", severity="Critical")
        selected = await subject.get_context(
            {**bound["context"]["filters"], "production_line": "Line-1", "severity": "Critical"},
            snapshot_id=bound["context"]["snapshot_id"],
        )
        view = await retained_dashboard_export(runtime, export_format, selected_filters)
        assert view["status"] == "completed", view
        assert view["scope"] == selected["context"]["filters"]
        artifact = view["result"]["artifact"]
        assert (
            artifact["context_id"]
            == selected["context"]["context_id"]
            != bound["context"]["context_id"]
        )
        assert artifact["scope"] == view["scope"] and artifact["export_locale"] == language
        content = tools.ledger.resolve_artifact(artifact["path"]).read_bytes()
        if export_format == "csv":
            records = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
            assert records and len(records) == selected["analysis"]["kpis"]["event_count"]
            assert {row["severity"] for row in records} == {"Critical"}
            assert all(
                row["export_locale"] == language and row["value_locale"] == "machine"
                for row in records
            )
            assert all(
                row["dataset_version"] == bound["context"]["dataset_version"] for row in records
            )
            assert all(
                row["policy_version"] == "canonical-v2" and row["scenario"] == "" for row in records
            )
        else:
            with ZipFile(io.BytesIO(content)) as archive:
                metadata = json.loads(archive.read("export_metadata.json"))
                assert metadata["context_id"] == selected["context"]["context_id"]
                assert metadata["scope"] == view["scope"]
                assert (
                    metadata["export_locale"] == language and metadata["value_locale"] == "machine"
                )
                assert metadata["dataset_version"] == bound["context"]["dataset_version"]
                assert metadata["policy_version"] == "canonical-v2" and metadata["scenario"] == ""
        # Retained legacy files remain auditable without exposing an Agent export tool.
        report = AnalystReport(
            report="Retained scoped data.", claims=[], deliverable_paths=[artifact["path"]]
        )
        delivery, errors = runtime.begin_delivery_proposal(report)
        assert not errors and delivery["artifacts"][0] == artifact
        assert delivery["scope"] == selected["context"]["filters"]
        assert runtime.analysis_context == bound
        assert tools.ledger.verify_integrity() == []
    finally:
        await tools.close()
