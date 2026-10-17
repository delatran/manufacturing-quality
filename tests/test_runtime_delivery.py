"""Selected delivery, provenance and failure behavior without live model calls."""

import asyncio
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from agents import ModelResponse, Usage
from analyst.config import Settings
from analyst.evidence import EvidenceLedger
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    FixtureModel,
    ReportDraft,
    _message,
    audit_exported_reports,
    audit_report,
    audit_report_links,
    business_scope,
    render_business_report,
)


async def emit(*_):
    pass


def runtime_at(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    return AnalystRuntime(
        Settings(mode="mock", qa_max_retries=0),
        SimpleNamespace(ledger=ledger, run_dir=tmp_path, input_paths=[], schemas=[]),
        emit,
    )


def artifact(runtime, path, body="group,value\na,12\n"):
    target = runtime.toolkit.run_dir / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return runtime.toolkit.ledger.register_artifact(target)


def canonical_entry(runtime, *, source="source", snapshot="snapshot", line="Line-4"):
    scope = {"start_month": "2024-01", "end_month": "2024-06", "production_line": line}
    return runtime.toolkit.ledger.record(
        "dashboard_context",
        "# verified canonical receipt",
        {"scope": scope, "kpis": {"event_count": 12}},
        execution_kind="canonical_receipt_import",
        analysis_context={
            "context_id": f"synthetic-delivery-{source}-{snapshot}-{line}",
            "dataset_version": "synthetic-delivery-dataset",
            "policy_version": "synthetic-delivery-policy",
            "scenario": "",
            "filters": scope,
            "source_sha256": source,
            "master_sha256": "master",
            "snapshot_id": snapshot,
            "kpi_version": "version",
        },
    )


def report_for(runtime, entry, paths=()):
    claim = next(
        item for item in runtime.model_receipt(entry)["numeric_evidence"] if item["value"] == 12
    )
    report = runtime.resolve_report(
        ReportDraft(report=f"Observed 12 events [claim:{claim['id']}].")
    )
    # Retained artifact auditing remains independent of the live model schema.
    report.deliverable_paths = list(paths)
    return report


def test_visible_claims_do_not_inherit_unused_case_receipts(tmp_path):
    runtime = runtime_at(tmp_path)
    runtime.case_entry = runtime.toolkit.ledger.record(
        "run_case_study",
        "# case",
        {
            "qa": {"status": "verified"},
            "analysis": {"events": 77},
            "numeric_claims": [{"id": "legacy", "path": "$.analysis.events", "value": 77}],
        },
    )
    own = runtime.toolkit.ledger.record("local_sql_engine", "SELECT 12", {"ok": True, "count": 12})
    report = report_for(runtime, own)
    assert len(report.claims) == 1 and report.claims[0].execution_id == own["execution_id"]
    assert "legacy" in runtime.available_claims()
    delivery, errors = runtime.delivery_packet(report, bind=True)
    assert not errors and delivery["scope"] is None
    assert delivery["source_execution_ids"] == [own["execution_id"]]
    legacy = runtime.resolve_report(ReportDraft(report="Observed 77 [claim:legacy]."))
    assert audit_report(legacy, runtime.toolkit.ledger)["verified"]


@pytest.mark.parametrize(
    "path",
    [
        "missing.csv",
        "artifacts/missing.csv",
        "../artifacts/a.csv",
        "/artifacts/a.csv",
        "C:/foreign/a.csv",
        "artifacts\\a.csv",
        "artifacts/./a.csv",
        "artifacts/../a.csv",
        "artifacts//a.csv",
        "artifacts/business_report.html",
    ],
)
def test_delivery_rejects_noncanonical_missing_and_reserved_paths(tmp_path, path):
    runtime = runtime_at(tmp_path)
    artifact(runtime, "artifacts/a.csv")
    report = AnalystReport(report="Requested output.", claims=[], deliverable_paths=[path])
    packet, errors = runtime.delivery_packet(report, bind=True)
    assert errors and packet["artifacts"] == []


@pytest.mark.parametrize("reregister", [False, True])
def test_selected_hash_is_never_rebound_after_drift(tmp_path, reregister):
    runtime = runtime_at(tmp_path)
    original = artifact(runtime, "artifacts/a.csv")
    report = AnalystReport(report="Output.", claims=[], deliverable_paths=[original["path"]])
    packet, errors = runtime.delivery_packet(report, bind=True)
    assert not errors and packet["artifacts"] == [original]
    path = tmp_path / original["path"]
    path.write_text("changed", encoding="utf-8")
    if reregister:
        runtime.toolkit.ledger.register_artifact(path)
    packet, errors = runtime.delivery_packet(report, bind=True)
    assert errors and packet["artifacts"] == []
    assert runtime.delivery_bindings[original["path"]] == original


def test_new_proposal_can_bind_an_explicitly_registered_repair(tmp_path):
    runtime = runtime_at(tmp_path)
    original = artifact(runtime, "artifacts/a.csv")
    report = AnalystReport(report="Output.", claims=[], deliverable_paths=[original["path"]])
    runtime.begin_delivery_proposal(report)
    repaired = artifact(runtime, original["path"], "corrected,content\na,13\n")
    assert runtime.delivery_packet(report, bind=True)[1]
    packet, errors = runtime.begin_delivery_proposal(report)
    assert not errors and packet["artifacts"] == [repaired]


def test_empty_selection_is_authoritative_and_inventory_is_not_producer_binding(tmp_path):
    runtime = runtime_at(tmp_path)
    stored = artifact(runtime, "artifacts/report.md", "Unsupported 999.")
    entry = runtime.toolkit.ledger.record("local_sql_engine", "SELECT 12", {"count": 12})
    view = runtime.model_receipt(entry)
    assert view["artifacts"][0]["path"] == stored["path"]
    assert "Internal tool evidence, not downloadable results" in view["artifact_inventory_note"]
    assert "chart_paths" in view["artifact_inventory_note"]
    report = report_for(runtime, entry)
    packet, errors = runtime.delivery_packet(report, bind=True)
    assert not errors and packet["artifacts"] == []
    assert audit_exported_reports(report, runtime.toolkit.ledger)["verified"]
    report.deliverable_paths = [stored["path"]]
    assert not audit_exported_reports(report, runtime.toolkit.ledger)["verified"]


@pytest.mark.parametrize("difference", ["source", "snapshot", "scope", "generic"])
def test_single_scope_requires_all_cited_source_provenance_to_match(tmp_path, difference):
    runtime = runtime_at(tmp_path)
    first = canonical_entry(runtime)
    second = (
        runtime.toolkit.ledger.record("local_sql_engine", "SELECT 12", {"count": 12})
        if difference == "generic"
        else canonical_entry(
            runtime,
            source="different" if difference == "source" else "source",
            snapshot="different" if difference == "snapshot" else "snapshot",
            line="Line-3" if difference == "scope" else "Line-4",
        )
    )
    assert (
        business_scope(
            runtime.toolkit.ledger,
            source_execution_ids=[first["execution_id"], second["execution_id"]],
        )
        is None
    )
    same = canonical_entry(runtime)
    assert (
        business_scope(
            runtime.toolkit.ledger,
            source_execution_ids=[first["execution_id"], same["execution_id"]],
        )
        == first["result"]["scope"]
    )


def test_selected_artifact_only_claims_supply_scope_without_polluting_chat(tmp_path):
    runtime = runtime_at(tmp_path)
    entry = canonical_entry(runtime)
    content = report_for(runtime, entry)
    file = artifact(runtime, "artifacts/detail.md", content.report)
    report = AnalystReport(
        report="See the requested detail.", claims=[], deliverable_paths=[file["path"]]
    )
    packet, errors = runtime.delivery_packet(report, bind=True)
    assert not errors and packet["scope"] is None
    audit = audit_exported_reports(
        report,
        runtime.toolkit.ledger,
        claim_catalog=runtime.available_claims(),
        artifacts=packet["artifacts"],
    )
    assert audit["verified"]
    packet, errors = runtime.delivery_packet(report, narrative_audit=audit)
    assert not errors and packet["scope"] == entry["result"]["scope"]
    assert packet["source_execution_ids"] == [entry["execution_id"]]
    assert report.claims == []


def test_retained_narrative_packet_binds_only_selected_paths_and_hashes(tmp_path):
    runtime = runtime_at(tmp_path)
    entry = canonical_entry(runtime)
    report = report_for(runtime, entry, ["artifacts/a/report.md", "artifacts/selected.csv"])
    first = artifact(runtime, "artifacts/a/report.md", report.report)
    artifact(runtime, "artifacts/b/report.md", "Unsupported 999.")
    artifact(runtime, "artifacts/selected.csv")
    artifact(runtime, "artifacts/unrelated.csv")
    packet, errors = runtime.delivery_packet(report, bind=True)
    assert not errors
    audit = audit_exported_reports(
        report,
        runtime.toolkit.ledger,
        claim_catalog=runtime.available_claims(),
        artifacts=packet["artifacts"],
    )
    assert audit["verified"] and [
        (item["path"], item["sha256"]) for item in audit["artifacts"]
    ] == [(first["path"], first["sha256"])]
    packet, errors = runtime.delivery_packet(report, narrative_audit=audit)
    assert not errors
    assert {item["path"] for item in packet["artifacts"]} == {
        *report.deliverable_paths,
    }
    assert not (tmp_path / "artifacts/business_report.html").exists()
    assert not (tmp_path / "artifacts/verified_evidence.zip").exists()


def test_source_and_download_links_require_exact_verified_targets(tmp_path):
    runtime = runtime_at(tmp_path)
    file = artifact(runtime, "artifacts/chosen.csv")
    artifact(runtime, "artifacts/not_chosen.csv")
    url = "https://example.org/data.csv?version=source&format=csv"
    runtime.toolkit.ledger.record(
        "local_sql_engine", "# actual input", {"ok": True}, sources=[{"url": url}]
    )
    report = AnalystReport(
        report="[source](" + url + ") [chosen](artifacts/chosen.csv)",
        claims=[],
        deliverable_paths=[file["path"]],
    )
    packet, errors = runtime.delivery_packet(report, bind=True)
    assert not errors and audit_report_links(report.report, runtime.verified_links(packet)) == []
    for target in (
        "artifacts/not_chosen.csv",
        "artifacts/missing.csv",
        "https://example.org/data.csv",
        "javascript:alert(x)",
    ):
        assert audit_report_links("[download](" + target + ")", runtime.verified_links(packet))
    html = render_business_report(
        report.report + "\n<script>bad</script> [unsafe](javascript:alert(x))",
        verified_links=runtime.verified_links(packet),
    )
    assert 'href="https://example.org/data.csv?version=source&amp;format=csv"' in html
    assert (
        "<script>" not in html
        and 'href="javascript:' not in html
        and 'href="artifacts/' not in html
    )
    assert "[source](" not in html and "[chosen](" not in html


@pytest.mark.parametrize("suffix", [".md", ".html", ".docx", ".pdf"])
def test_selected_narrative_links_keep_exact_source_and_selection_contract(tmp_path, suffix):
    runtime = runtime_at(tmp_path)
    source = "https://data.example/actual.csv"
    runtime.toolkit.ledger.record(
        "local_sql_engine", "# actual input", {"ok": True}, sources=[{"url": source}]
    )
    chosen = artifact(runtime, "artifacts/chosen.csv")
    artifact(runtime, "artifacts/unselected.csv")
    path = tmp_path / ("artifacts/narrative" + suffix)

    def save(target):
        if suffix == ".md":
            path.write_text("[Reference](" + target + ")", encoding="utf-8")
        elif suffix == ".html":
            path.write_text('<p><a href="' + target + '">Reference</a></p>', encoding="utf-8")
        elif suffix == ".docx":
            with ZipFile(path, "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Reference</w:t></w:r></w:p></w:body></w:document>',
                )
                archive.writestr(
                    "word/_rels/document.xml.rels",
                    '<Relationships><Relationship TargetMode="External" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="'
                    + target
                    + '" /></Relationships>',
                )
        else:
            from pypdf import PdfWriter
            from pypdf.annotations import Link
            from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

            writer = PdfWriter()
            page = writer.add_blank_page(width=200, height=200)
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 20 150 Td (Reference) Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
            writer.add_annotation(
                page_number=0, annotation=Link(rect=(20, 150, 150, 170), url=target)
            )
            writer.write(path)
        runtime.toolkit.ledger.register_artifact(path)

    report = AnalystReport(
        report="Requested detail.",
        claims=[],
        deliverable_paths=[path.relative_to(tmp_path).as_posix(), chosen["path"]],
    )
    for target, expected in (
        (source, True),
        (chosen["path"], True),
        ("https://data.example/fabricated.csv", False),
        ("artifacts/unselected.csv", False),
    ):
        save(target)
        before = path.read_bytes()
        audited = audit_exported_reports(report, runtime.toolkit.ledger)
        assert audited["verified"] is expected, audited
        assert path.read_bytes() == before


def test_qa_receives_actual_schema_scenario_priority_and_quality_stage(tmp_path):
    runtime = runtime_at(tmp_path)
    runtime.toolkit.ledger.record(
        "dataset_profiler",
        "# schema",
        {
            "datasets": {
                "uploaded.csv": {
                    "columns": [
                        {"name": "severity", "dtype": "object", "missing_count": 0},
                        {"name": "repair_cost", "dtype": "float64"},
                    ]
                }
            }
        },
    )
    runtime.toolkit.ledger.record(
        "dashboard_date_sensitivity",
        "# actual scenarios",
        {
            "ok": True,
            "scenarios": {
                "scenario_a": {
                    "kpis": {"event_count": 12},
                    "priority_signals": {"critical_location": "Component"},
                    "rankings": {"top_products": [81, 2]},
                }
            },
        },
    )
    entry = runtime.toolkit.ledger.record(
        "dashboard_context",
        "# preparation counts",
        {
            "quality": {"issues": [{"code": "ambiguous_date", "affected_count": 3}]},
            "kpis": {"event_count": 12},
        },
    )
    report = report_for(runtime, entry)
    support, errors = runtime.qa_evidence(report)
    assert not errors
    assert support["source_schemas"][0]["name"] == "uploaded.csv"
    assert support["source_schemas"][0]["columns"][0] == {"name": "severity", "dtype": "object"}
    scenario = next(
        item
        for item in support["qualitative_support"]
        if item["tool"] == "dashboard_date_sensitivity"
    )["scenario_evidence"][0]
    assert (
        scenario["name"] == "scenario_a"
        and scenario["priority_signals"]["critical_location"] == "Component"
    )
    assert scenario["rankings"]["top_products"] == [81, 2]
    quality = next(
        item for item in support["qualitative_support"] if item["tool"] == "dashboard_context"
    )
    assert quality["source_quality_issues"][0]["affected_count"] == 3
    assert (
        "audit entries at the issue detection stage"
        in quality["quality_methodology"]["quality.issues[*].affected_count"]
    )


def test_only_exact_verified_workbook_cell_is_a_source_identifier(tmp_path):
    runtime = runtime_at(tmp_path)
    runtime.toolkit.ledger.record(
        "run_case_study",
        "# helper readback",
        {
            "qa": {"status": "verified"},
            "workbook_delivery": {"readback": {"status": "verified", "checked_cell": "Summary!B8"}},
        },
    )
    assert audit_report(
        AnalystReport(report="Checked Summary!B8.", claims=[]), runtime.toolkit.ledger
    )["verified"]
    assert not audit_report(
        AnalystReport(report="Checked Summary!B9.", claims=[]), runtime.toolkit.ledger
    )["verified"]
    assert not audit_report(
        AnalystReport(report="Summary!B8 has 999 events.", claims=[]), runtime.toolkit.ledger
    )["verified"]


@pytest.mark.parametrize(
    "outcome", ["success", "bad_report", "qa_drift", "unselected_link", "nonimage_selection"]
)
def test_final_packet_and_event_fail_closed_through_real_sdk_guardrail(tmp_path, outcome):
    async def scenario():
        runtime = runtime_at(tmp_path)
        events = []

        async def capture(kind, data):
            events.append((kind, data))

        runtime.emit = capture
        entry = runtime.toolkit.ledger.record(
            "local_sql_engine", "SELECT 12", {"ok": True, "count": 12}
        )
        selected = artifact(
            runtime,
            "artifacts/selected.svg",
            '<svg xmlns="http://www.w3.org/2000/svg"><title>Observed events</title></svg>',
        )
        artifact(runtime, "artifacts/unselected.csv")
        report = report_for(runtime, entry, [selected["path"]])
        if outcome == "bad_report":
            report.report += " Unsupported 999."
        elif outcome == "unselected_link":
            report.report += " [data](artifacts/unselected.csv)"
        elif outcome == "nonimage_selection":
            report.deliverable_paths.append(artifact(runtime, "artifacts/forbidden.csv")["path"])

        class Scripted(FixtureModel):
            async def get_response(self, system_instructions, input, *args, **kwargs):
                if self.role == "QA Verifier":
                    if outcome == "qa_drift":
                        artifact(runtime, selected["path"], "changed after binding")
                    return await super().get_response(system_instructions, input, *args, **kwargs)
                assert "Server-provided source catalog" in system_instructions
                assert '"local_files":[]' in system_instructions
                assert '"tool":"run_case_study"' in system_instructions
                return ModelResponse(
                    output=[_message(report.model_dump_json())],
                    usage=Usage(requests=0),
                    response_id="fixture_report",
                )

        runtime.model = lambda role: Scripted(role, runtime)

        async def no_mutation(*args, **kwargs):
            return "{}"

        runtime._call = no_mutation
        result = await runtime.run("Analyze the supplied file and show its chart in chat.")
        assert result["artifacts"] == result["delivery"]["artifacts"]
        assert (
            next(data for kind, data in events if kind == "run_started")[
                "artifact_delivery_version"
            ]
            == 1
        )
        assert (
            next(data for kind, data in events if kind == "report")["delivery"]
            == result["delivery"]
        )
        paths = {item["path"] for item in result["artifacts"]}
        assert "artifacts/unselected.csv" not in paths
        if outcome == "success":
            assert result["status"] == "completed" and paths == {selected["path"]}
        else:
            assert result["status"] == "unverified" and not result["qa"]["verified"]
            expected = set() if outcome == "qa_drift" else {selected["path"]}
            assert paths == expected
            if outcome == "unselected_link":
                assert "Unverified source or download link" in str(result["qa"]["errors"])
            if outcome == "nonimage_selection":
                assert "only registered inline charts" in str(result["qa"]["errors"])
                assert "artifacts/forbidden.csv" not in paths
        assert not (tmp_path / "artifacts/business_report.html").exists()
        assert not (tmp_path / "artifacts/verified_evidence.zip").exists()

    asyncio.run(scenario())
