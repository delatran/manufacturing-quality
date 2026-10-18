"""Code syntax and backend briefs remain bound to their exact original sources."""

import json

import pytest
from analyst.claim_feedback import typed_calendar_text
from analyst.evidence import EvidenceLedger, sha256
from analyst.runtime import AnalystReport, ReportDraft, audit_exported_reports, audit_report
from retained_dashboard_export_fixture import retained_dashboard_export
from test_claim_feedback import evidence_subject as evidence_subject
from test_claim_feedback import filters


def test_executed_sql_syntax_is_not_numeric_prose_and_modified_query_is_rejected(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    query = "SELECT 100.0 * COUNT(repair_cost) / NULLIF(COUNT(*), 0) FROM events GROUP BY 1"
    ledger.record(
        "analysis_sql",
        "# read-only scoped SQL",
        {
            "ok": True,
            "query": query,
            "query_sha256": sha256(query),
            "rows": [],
        },
        execution_kind="bounded_analysis",
    )
    report = AnalystReport(report="```sql\n" + query + ";\n```", claims=[])
    assert audit_report(report, ledger)["verified"]
    report.report = report.report.replace("100.0", "999.0")
    assert not audit_report(report, ledger)["verified"]
    report.report = "```text\nObserved count is 100.0\n```"
    assert not audit_report(report, ledger)["verified"]


def test_translated_catalog_labels_are_identifiers_but_unknown_categories_are_not(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    ledger.record(
        "analysis_query",
        "# canonical category",
        {
            "ok": True,
            "analysis": {"rows": [{"category": "Category 4", "production_line": "Line-2"}]},
        },
        execution_kind="bounded_analysis",
    )
    assert audit_report(AnalystReport(report="Nhóm 4 | Chuyền 2", claims=[]), ledger)["verified"]
    assert not audit_report(AnalystReport(report="Nhóm 9 | Chuyền 2", claims=[]), ledger)[
        "verified"
    ]
    assert not audit_report(AnalystReport(report="Nhóm 4 có 4 sự kiện", claims=[]), ledger)[
        "verified"
    ]


def test_yearless_calendar_ranges_require_one_exact_source_period():
    context = {
        "context_id": "context",
        "snapshot_id": "snapshot",
        "dataset_version": "dataset",
        "policy_version": "policy",
        "scenario": "",
        "filters": {"start_month": "2024-01", "end_month": "2024-03", "scenario": ""},
    }
    entry = {
        "tool": "analysis_query",
        "execution_kind": "bounded_analysis",
        "result": {"ok": True, "context": context},
    }
    assert not typed_calendar_text("| Tháng 1–3 |", {"one": entry})[1]
    assert typed_calendar_text("| Tháng 1–4 |", {"one": entry})[1]
    assert typed_calendar_text("Tháng 1–3 sự kiện", {"one": entry})[1]
    assert typed_calendar_text("Số sự kiện là tháng 1–3", {"one": entry})[1]
    other = json.loads(json.dumps(entry))
    other["result"]["context"]["filters"].update(start_month="2025-01", end_month="2025-03")
    assert typed_calendar_text("| Tháng 1–3 |", {"one": entry, "two": other})[1]


@pytest.fixture
def period_halves_entry():
    return {
        "tool": "analysis_query",
        "execution_kind": "bounded_analysis",
        "result": {
            "ok": True,
            "context": {
                "context_id": "context",
                "snapshot_id": "snapshot",
                "dataset_version": "dataset",
                "policy_version": "policy",
                "scenario": "",
                "filters": {
                    "start_month": "2024-01",
                    "end_month": "2024-06",
                    "scenario": "",
                },
            },
            "analysis": {
                "period_halves": {
                    "enabled": True,
                    "first": {"months": ["2024-01", "2024-02", "2024-03"]},
                    "second": {"months": ["2024-04", "2024-05", "2024-06"]},
                },
            },
        },
    }


@pytest.mark.parametrize(
    "text",
    [
        "| Metric | Jan–Mar 2024 | Apr–Jun 2024 |",
        "| Metric | January through March 2024 | April through June 2024 |",
        "| Chỉ tiêu | tháng 1–3/2024 | tháng 4–6/2024 |",
        "| Chỉ tiêu | Tháng 1–3 | Tháng 4–6 |",
    ],
)
def test_explicit_half_periods_authorize_their_exact_calendar_labels(period_halves_entry, text):
    assert not typed_calendar_text(text, {"query": period_halves_entry})[1]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("tool",), "python"),
        (("tool",), "dashboard_context"),
        (("execution_kind",), "canonical_receipt_import"),
        (("result", "ok"), False),
        (("result", "ok"), None),
        (("result", "context", "context_id"), None),
        (("result", "context", "snapshot_id"), None),
        (("result", "context", "scenario"), "unbound-scenario"),
        (("result", "analysis", "period_halves", "enabled"), False),
        (("result", "analysis", "period_halves", "enabled"), "true"),
        (("result", "analysis", "period_halves", "first", "months"), []),
        (("result", "analysis", "period_halves", "second", "months"), None),
    ],
)
def test_half_period_labels_require_successful_frozen_query(period_halves_entry, path, value):
    target = period_halves_entry
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert typed_calendar_text("Jan–Mar 2024", {"query": period_halves_entry})[1]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (["2024-01", "2024-03"], ["2024-04", "2024-06"]),
        (["2024-01", "2024-02", "2024-03"], ["2024-03", "2024-05", "2024-06"]),
        (["2024-01", "2024-03", "2024-02"], ["2024-04", "2024-05", "2024-06"]),
        (["2024-01", "2024-02"], ["2024-03", "2024-04", "2024-05", "2024-06"]),
        (["2023-12", "2024-01", "2024-02"], ["2024-03", "2024-04", "2024-05"]),
        (["2024-01", "2024-13", "2024-03"], ["2024-04", "2024-05", "2024-06"]),
        (["2024-01", "2024-2", "2024-03"], ["2024-04", "2024-05", "2024-06"]),
        (["2024-01", 2, "2024-03"], ["2024-04", "2024-05", "2024-06"]),
    ],
)
def test_half_periods_must_partition_the_whole_context_contiguously(
    period_halves_entry, first, second
):
    halves = period_halves_entry["result"]["analysis"]["period_halves"]
    halves["first"]["months"] = first
    halves["second"]["months"] = second
    issues = typed_calendar_text("Jan–Mar 2024", {"query": period_halves_entry})[1]
    assert issues
    assert issues[0]["available_periods"] == [["2024-01", "2024-06"]]


def test_half_periods_can_partition_contiguous_months_across_years(period_halves_entry):
    period_halves_entry["result"]["context"]["filters"].update(
        start_month="2023-11", end_month="2024-02"
    )
    halves = period_halves_entry["result"]["analysis"]["period_halves"]
    halves["first"]["months"] = ["2023-11", "2023-12"]
    halves["second"]["months"] = ["2024-01", "2024-02"]
    assert not typed_calendar_text(
        "| Nov–Dec 2023 | Jan–Feb 2024 |", {"query": period_halves_entry}
    )[1]


def test_half_periods_cannot_bind_conflicting_frozen_contexts(period_halves_entry):
    context = json.loads(json.dumps(period_halves_entry["result"]["context"]))
    period_halves_entry["analysis_context"] = context
    assert not typed_calendar_text("Jan–Mar 2024", {"query": period_halves_entry})[1]
    context["context_id"] = "another-context"
    assert typed_calendar_text("Jan–Mar 2024", {"query": period_halves_entry})[1]


def test_half_periods_are_not_inferred_from_context_or_arbitrary_month_lists(period_halves_entry):
    analysis = period_halves_entry["result"].pop("analysis")
    assert typed_calendar_text("Jan–Mar 2024", {"query": period_halves_entry})[1]
    period_halves_entry["result"]["narrative"] = "Jan–Mar 2024; Apr–Jun 2024"
    period_halves_entry["result"]["other_analysis"] = analysis
    assert typed_calendar_text("Jan–Mar 2024", {"query": period_halves_entry})[1]
    assert not typed_calendar_text("Jan–Jun 2024", {"query": period_halves_entry})[1]


@pytest.mark.parametrize(
    "text",
    [
        "Jan–Apr 2024",
        "Feb–Mar 2024",
        "Jan–Mar 2025",
        "Event count is Jan–Mar 2024",
        "Tháng 1–3 sự kiện",
        "Tháng 1–3/2024 local currency",
    ],
)
def test_half_period_evidence_does_not_authorize_other_ranges_or_measures(
    period_halves_entry, text
):
    assert typed_calendar_text(text, {"query": period_halves_entry})[1]


def test_yearless_half_periods_remain_ambiguous_across_years(period_halves_entry):
    other = json.loads(json.dumps(period_halves_entry).replace("2024-", "2025-"))
    assert typed_calendar_text("Tháng 1–3", {"one": period_halves_entry, "two": other})[1]


@pytest.mark.asyncio
async def test_half_period_table_uses_available_receipt_and_still_requires_numeric_citations(
    evidence_subject,
):
    runtime, tools, *_ = evidence_subject
    view = json.loads(
        await runtime._call("analysis_query", {"section": "period_halves", "filters": filters()})
    )
    ids = {item["path"]: item["id"] for item in view["numeric_evidence"]}
    text = "| Metric | Jan–Mar 2024 | Apr–Jun 2024 |\n|---|---|---|\n| Events |"
    text += " | ".join(
        "[value:" + ids[f"$.analysis.period_halves.{half}.event_count"] + "]"
        for half in ("first", "second")
    )
    text += " |"
    report = runtime.resolve_report(ReportDraft(report=text))
    # Calendar headers use all successful frozen receipts without inline claims.
    # Numeric table cells still require their own citations to that receipt.
    checked = audit_report(report, tools.ledger)
    assert checked["verified"], checked
    unsupported = report.model_copy(update={"report": report.report.replace("Jan–Mar", "Jan–Apr")})
    assert not audit_report(unsupported, tools.ledger)["verified"]
    halves = view["result"]["analysis"]["period_halves"]
    uncited = AnalystReport(
        report="| Metric | Jan–Mar 2024 | Apr–Jun 2024 |\n|---|---|---|\n"
        f"| Events | {halves['first']['event_count']} | {halves['second']['event_count']} |",
        claims=[],
    )
    assert not audit_report(uncited, tools.ledger)["verified"]


def test_forged_half_period_receipt_still_fails_ledger_integrity(tmp_path, period_halves_entry):
    ledger = EvidenceLedger(tmp_path)
    receipt = ledger.record(
        "analysis_query",
        "# frozen half-period query",
        period_halves_entry["result"],
        execution_kind="bounded_analysis",
    )
    receipt["result"]["context"]["filters"]["end_month"] = "2024-04"
    halves = receipt["result"]["analysis"]["period_halves"]
    halves["first"]["months"] = ["2024-01", "2024-02"]
    halves["second"]["months"] = ["2024-03", "2024-04"]
    ledger.path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    report = AnalystReport(report="| Metric | Jan–Feb 2024 | Mar–Apr 2024 |", claims=[])
    checked = audit_report(report, ledger)
    assert not checked["verified"]
    assert any("entry hash mismatch" in error for error in checked["errors"])


@pytest.mark.asyncio
async def test_comparison_column_period_aliases_cannot_authorize_numeric_values(evidence_subject):
    runtime, tools, *_ = evidence_subject
    header = "| Method | Chênh lệch H2 so với H1 |\n|---|---|\n| Visual | pending |"
    assert audit_report(AnalystReport(report=header, claims=[]), tools.ledger)["verified"]
    assert not audit_report(AnalystReport(report="H2 so với H1", claims=[]), tools.ledger)[
        "verified"
    ]
    assert not audit_report(
        AnalystReport(report=header.replace("pending", "2 events"), claims=[]), tools.ledger
    )["verified"]


@pytest.mark.asyncio
async def test_backend_brief_exact_bytes_pass_and_modified_copy_fails(evidence_subject):
    runtime, tools, *_ = evidence_subject
    view = await retained_dashboard_export(runtime, "brief", filters())
    artifact = view["result"]["artifact"]
    report = AnalystReport(
        report="Export attached.", claims=[], deliverable_paths=[artifact["path"]]
    )
    checked = audit_exported_reports(report, tools.ledger)
    assert checked["verified"], checked
    proof = checked["artifacts"][0]
    assert proof["derived_from_canonical_dashboard"]
    assert not proof["source_tables"]
    assert proof["canonical_source"]["execution_id"] in proof["source_execution_ids"]
    runtime.begin_delivery_proposal(report)
    delivery, delivery_errors = runtime.delivery_packet(report, narrative_audit=checked)
    assert not delivery_errors, delivery_errors
    qa_input, qa_errors = runtime.qa_evidence(report, delivery=delivery)
    assert not qa_errors, qa_errors
    assert qa_input["verified_artifacts"][0]["sha256"] == artifact["sha256"]
    path = tools.ledger.resolve_artifact(artifact["path"])
    path.write_text(
        path.read_text(encoding="utf-8") + "\nInvented 999999 events.\n", encoding="utf-8"
    )
    tools.ledger.register_artifact(path)
    assert not audit_exported_reports(report, tools.ledger)["verified"]


@pytest.mark.asyncio
async def test_source_bound_linked_event_formula_accepts_explicit_unit_words(evidence_subject):
    runtime, tools, *_ = evidence_subject
    view = json.loads(
        await runtime._call("analysis_query", {"section": "kpis", "filters": filters()})
    )
    ids = {item["path"]: item["id"] for item in view["numeric_evidence"]}
    keys = [
        "$.analysis.kpis.linked_event_count",
        "$.analysis.kpis.reference_output",
        "$.metric_metadata.reference_intensity_per_1000.scale",
        "$.analysis.kpis.reference_intensity_per_1000",
    ]

    def value(path):
        return "[value:" + ids[path] + "]"

    text = (
        value(keys[0])
        + " sự kiện liên kết / "
        + value(keys[1])
        + " đơn vị sản lượng tham chiếu × hệ số đơn vị "
        + value(keys[2])
        + " = "
        + value(keys[3])
        + " sự kiện trên mỗi hệ số đơn vị sản lượng tham chiếu."
    )
    report = runtime.resolve_report(ReportDraft(report=text))
    assert audit_report(report, tools.ledger)["verified"]


def test_large_narrative_has_no_arbitrary_size_rejection(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    path = tmp_path / "artifacts" / "large.md"
    path.parent.mkdir()
    path.write_text("# Source notes\n" + "e" * (10 * 1024 * 1024), encoding="utf-8")
    artifact = ledger.register_artifact(path)
    report = AnalystReport(report="Notes.", claims=[], deliverable_paths=[artifact["path"]])
    assert audit_exported_reports(report, ledger)["verified"]
