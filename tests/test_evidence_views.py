"""Compact model views retain complete numeric and exported-report verification."""

import asyncio
from types import SimpleNamespace

import pytest
from analyst.config import Settings
from analyst.evidence import EvidenceLedger, canonical
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    ReportDraft,
    audit_exported_reports,
    audit_report,
    render_business_report,
)


async def emit(*_):
    pass


def runtime_with_receipt(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    result = {
        "kpis": {"event_count": 12},
        "scope": {"production_line": "Line-4"},
        "monthly": [{"month": "2024-01", "event_count": 12}],
        "products": {
            "top": [{"product_name": "Widget A", "event_count": 12}],
            "all": [
                {
                    "product_name": "Widget A",
                    "event_count": index,
                    "description": "Source product detail " * 15,
                }
                for index in range(120)
            ],
        },
    }
    entry = ledger.record(
        "dashboard_context",
        "# exact canonical implementation\n" * 1000,
        result,
        analysis_context={
            "filters": result["scope"],
            "context_id": "fixture-context",
            "snapshot_id": "fixture-snapshot",
            "dataset_version": "fixture-dataset",
            "policy_version": "fixture-policy",
            "scenario": "",
        },
        execution_kind="canonical_receipt_import",
    )
    runtime = AnalystRuntime(
        Settings(mode="live", api_key="test"),
        SimpleNamespace(ledger=ledger, run_dir=tmp_path),
        emit,
    )
    runtime.context_entry = entry
    return runtime, entry


def test_compact_receipts_issue_exact_server_bound_claims(tmp_path):
    runtime, entry = runtime_with_receipt(tmp_path)
    view = runtime.model_receipt(entry)
    assert len(canonical(view)) < len(canonical(entry)) / 4
    assert "code" not in view and "all" not in view["result"]["products"]
    number = next(item for item in view["numeric_evidence"] if item["path"] == "$.kpis.event_count")
    report = runtime.resolve_report(
        ReportDraft(report=f"Line-4 has 12 events [claim:{number['id']}].")
    )
    assert audit_report(report, runtime.toolkit.ledger)["verified"]
    assert report.claims[0].execution_id == entry["execution_id"]
    assert report.claims[0].code_sha256 == entry["code_sha256"]
    assert not audit_report(
        runtime.resolve_report(ReportDraft(report="12 [claim:invented]")), runtime.toolkit.ledger
    )["verified"]


def test_detail_pages_keep_original_numeric_paths(tmp_path):
    runtime, entry = runtime_with_receipt(tmp_path)
    base, selected = asyncio.run(
        runtime.dashboard_detail(
            {"receipt_id": None, "path": "$.products.all", "offset": 100, "limit": 2}
        )
    )
    view = runtime.model_receipt(base, selected)
    paths = [item["path"] for item in view["numeric_evidence"]]
    assert paths == ["$.products.all[100].event_count", "$.products.all[101].event_count"]
    value = view["numeric_evidence"][0]
    report = runtime.resolve_report(ReportDraft(report=f"Observed 100 [claim:{value['id']}]."))
    assert audit_report(report, runtime.toolkit.ledger)["verified"]


def test_exact_labels_do_not_exempt_measurements_or_near_matches(tmp_path):
    runtime, _ = runtime_with_receipt(tmp_path)
    ledger = runtime.toolkit.ledger
    assert audit_report(AnalystReport(report="Line-4 in 2024-01.", claims=[]), ledger)["verified"]
    assert audit_report(
        AnalystReport(report="Tháng 1/2024 và tháng một năm 2024.", claims=[]), ledger
    )["verified"]
    for text in ("Line-40", "Line-4 has 40 events", "2024-02", "USD999"):
        assert not audit_report(AnalystReport(report=text, claims=[]), ledger)["verified"]
    ledger.record(
        "source", "# untrusted metadata", {"note": "profit 999", "production_line": "999"}
    )
    assert not audit_report(AnalystReport(report="profit 999", claims=[]), ledger)["verified"]
    ledger.record(
        "local_python_repl",
        "# arbitrary computed strings",
        {"ok": True, "result": {"product_name": "999 events"}},
    )
    assert not audit_report(
        AnalystReport(report="The plant recorded 999 events.", claims=[]), ledger
    )["verified"]
    ledger.record(
        "dashboard_context",
        "# arbitrary source text",
        {"product_name": "999 events"},
        execution_kind="canonical_receipt_import",
    )
    assert not audit_report(
        AnalystReport(report="The plant recorded 999 events.", claims=[]), ledger
    )["verified"]


@pytest.mark.parametrize("path", ["$", "$.products"])
def test_detail_parent_cannot_bypass_collection_page_limit(tmp_path, path):
    runtime, entry = runtime_with_receipt(tmp_path)
    for name in ("dashboard_detail", "evidence_detail"):
        with pytest.raises(ValueError, match="unpaged or oversized"):
            asyncio.run(
                getattr(runtime, name)(
                    {"receipt_id": entry["execution_id"], "path": path, "offset": 0, "limit": 1}
                )
            )


def test_claim_identifiers_are_stable_within_run_and_isolated_between_runs(tmp_path):
    first, entry = runtime_with_receipt(tmp_path / "first")
    second, other = runtime_with_receipt(tmp_path / "second")

    def ids(view):
        return [value["id"] for value in view["numeric_evidence"]]

    assert ids(first.model_receipt(entry)) == ids(first.model_receipt(entry))
    assert set(ids(first.model_receipt(entry))).isdisjoint(ids(second.model_receipt(other)))


def test_business_html_is_exactly_derived_and_tamper_checked(tmp_path):
    runtime, entry = runtime_with_receipt(tmp_path)
    number = next(
        item
        for item in runtime.model_receipt(entry)["numeric_evidence"]
        if item["path"] == "$.kpis.event_count"
    )
    report = runtime.resolve_report(
        ReportDraft(
            report=f"# Findings\n\nLine-4 has 12 events [claim:{number['id']}].\n\n<script>alert('x')</script>"
        )
    )
    path = tmp_path / "artifacts" / "business_report.html"
    path.parent.mkdir()
    path.write_text(
        render_business_report(report.report, runtime.toolkit.ledger, claims=report.claims),
        encoding="utf-8",
    )
    assert "<script>" not in path.read_text(encoding="utf-8") and "[claim:" not in path.read_text(
        encoding="utf-8"
    )
    artifact = runtime.toolkit.ledger.register_artifact(path, kind="business_report")
    assert audit_exported_reports(report, runtime.toolkit.ledger, artifacts=[artifact])["verified"]
    path.write_text(
        path.read_text(encoding="utf-8").replace("12 events", "999 events"), encoding="utf-8"
    )
    artifact = runtime.toolkit.ledger.register_artifact(path, kind="business_report")
    assert not audit_exported_reports(report, runtime.toolkit.ledger, artifacts=[artifact])[
        "verified"
    ]


def test_failed_tool_receipt_never_issues_claims(tmp_path):
    runtime, _ = runtime_with_receipt(tmp_path)
    entry = runtime.toolkit.ledger.record(
        "local_python_repl", "raise ValueError", {"ok": False, "result": 999}
    )
    assert runtime.model_receipt(entry)["numeric_evidence"] == []


def test_structured_dates_are_exact_valid_labels_not_unverified_prose(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    ledger.record(
        "local_sql_engine",
        "SELECT observed_month",
        {"ok": True, "rows": [{"month": "2024-02", "date": "2024-02-29"}]},
    )
    ledger.record("local_python_repl", "# failed", {"ok": False, "value": {"month": "2024-03"}})
    ledger.record(
        "dataset_profiler",
        "# schema",
        {"ok": True, "notes": "2024-04", "month": "2024-13", "date": "2024-02-30"},
    )
    assert audit_report(AnalystReport(report="2024-02 and 2024-02-29", claims=[]), ledger)[
        "verified"
    ]
    for text in (
        "2024-03",
        "2024-04",
        "2024-13",
        "2024-02-30",
        "2024-02-28",
        "2024-02 has 999 events",
        "x2024-02",
    ):
        assert not audit_report(AnalystReport(report=text, claims=[]), ledger)["verified"], text


def test_ordered_list_marker_is_not_a_measurement(tmp_path):
    runtime, entry = runtime_with_receipt(tmp_path)
    number = next(
        item
        for item in runtime.model_receipt(entry)["numeric_evidence"]
        if item["path"] == "$.kpis.event_count"
    )
    report = runtime.resolve_report(
        ReportDraft(report=f"1. Observed 12 events [claim:{number['id']}].\n2) Review the source.")
    )
    assert audit_report(report, runtime.toolkit.ledger)["verified"]
    report.report += "\n3. Observed 999 events."
    assert not audit_report(report, runtime.toolkit.ledger)["verified"]


def test_qa_receives_ancestor_scope_limitations_and_real_artifact_receipts(tmp_path):
    runtime, _ = runtime_with_receipt(tmp_path)
    ledger = runtime.toolkit.ledger
    path = tmp_path / "artifacts" / "summary.csv"
    path.parent.mkdir()
    path.write_text("month,count\n2024-01,12\n", encoding="utf-8")
    artifact = ledger.register_artifact(path)
    entry = ledger.record(
        "local_python_repl",
        "# exported summary",
        {"ok": True, "value": {"count": 12}},
        artifacts=[artifact],
    )
    assert runtime.model_receipt(entry)["artifacts"][0]["sha256"] == artifact["sha256"]
    scenario = ledger.record(
        "dashboard_date_sensitivity",
        "# actual scenarios",
        {
            "ok": True,
            "limitations": ["Scenarios do not establish corrected truth."],
            "scenarios": [{"name": "withhold_ambiguous_raw_rows", "kpis": {"event_count": 12}}],
        },
    )
    claim = runtime.model_receipt(scenario)["numeric_evidence"][0]
    report = runtime.resolve_report(
        ReportDraft(report=f"Withholding gives 12 events [claim:{claim['id']}].")
    )
    # Retained artifacts remain auditable without exposing a live delivery selector.
    report.deliverable_paths = [artifact["path"]]
    support, errors = runtime.qa_evidence(report)
    assert not errors
    contexts = {item["id"]: item for item in support["ancestor_contexts"]}
    labels = {}
    for identity in support["numeric_support"][0]["ancestor_context_ids"]:
        labels.update(contexts[identity]["labels"])
    assert labels["name"] == "withhold_ambiguous_raw_rows"
    scenario_support = next(
        item
        for item in support["qualitative_support"]
        if item["tool"] == "dashboard_date_sensitivity"
    )
    assert scenario_support["limitations"] == ["Scenarios do not establish corrected truth."]
    assert support["verified_artifacts"][0]["file_hash_verified"]
    path.write_text("changed", encoding="utf-8")
    support, errors = runtime.qa_evidence(report)
    assert errors and support["verified_artifacts"] == []


def test_business_document_uses_captured_language_and_canonical_scope(tmp_path):
    from analyst.localization import VI_NUMBER_NOTE

    ledger = EvidenceLedger(tmp_path)
    scope = {
        "start_month": "2024-01",
        "end_month": "2024-06",
        "production_line": "Line-4",
        "severity": "Critical",
    }
    entry = ledger.record(
        "dashboard_context",
        "# canonical receipt",
        {"scope": scope},
        execution_kind="canonical_receipt_import",
        analysis_context={
            "filters": scope,
            "snapshot_id": "snapshot",
            "source_sha256": "source",
            "master_sha256": "master",
            "kpi_version": "version",
        },
    )
    from analyst.runtime import business_scope

    captured_scope = business_scope(ledger, source_execution_ids=[entry["execution_id"]])
    html = render_business_report("Kết quả quan sát.", ledger, language="vi", scope=captured_scope)
    assert '<html lang="vi">' in html
    assert (
        "2024-01 đến 2024-06" in html
        and "Dây chuyền: Line-4" in html
        and "Mức độ: Critical" in html
    )
    assert '<html lang="en">' in render_business_report(VI_NUMBER_NOTE, ledger, language="en")
    assert 'report-scope"><strong>' not in render_business_report("Independent upload.")


def test_declared_unit_scales_exempt_only_the_explicit_unit_phrase(tmp_path):
    from analyst.case_study.analytics import DEFINITIONS, METRIC_METADATA

    runtime, _ = runtime_with_receipt(tmp_path)
    entry = runtime.toolkit.ledger.record(
        "dashboard_context",
        "# canonical metric definitions",
        {
            "kpis": {"reference_intensity_per_1000": 2.566},
            "definitions": DEFINITIONS,
            "metric_metadata": METRIC_METADATA,
        },
        execution_kind="canonical_receipt_import",
    )
    claim = next(
        item
        for item in runtime.model_receipt(entry)["numeric_evidence"]
        if item["path"] == "$.kpis.reference_intensity_per_1000"
    )
    for phrase in (
        "per 1,000 reference units",
        "per 1000 reference output units",
        "trên mỗi 1,000 đơn vị tham chiếu",
        "trên 1,000 đơn vị sản lượng tham chiếu",
    ):
        report = runtime.resolve_report(
            ReportDraft(report=f"2.566 [claim:{claim['id']}] {phrase}.")
        )
        assert audit_report(report, runtime.toolkit.ledger)["verified"], phrase
        report.report += " Recorded 1,000 events."
        assert not audit_report(report, runtime.toolkit.ledger)["verified"], phrase
    for text in (
        "1,000 events",
        "100% complete",
        "100 events",
        "per 1,000 actual production units",
        "per 10,000 reference units",
        "xper 1,000 reference units",
        "per 1,000 reference unitsx",
    ):
        assert not audit_report(AnalystReport(report=text, claims=[]), runtime.toolkit.ledger)[
            "verified"
        ], text
    assert audit_report(
        AnalystReport(report="per 100 logged events", claims=[]), runtime.toolkit.ledger
    )["verified"]


@pytest.mark.parametrize(
    "tool,kind,failed,metadata",
    [
        ("local_python_repl", "python", False, True),
        ("dashboard_context", "canonical_receipt_import", True, True),
        ("dashboard_context", "canonical_receipt_import", False, False),
    ],
)
def test_unit_scales_require_successful_canonical_metric_metadata(
    tmp_path, tool, kind, failed, metadata
):
    from analyst.case_study.analytics import DEFINITIONS, METRIC_METADATA

    ledger = EvidenceLedger(tmp_path)
    ledger.record(
        tool,
        "# result",
        {
            "ok": not failed,
            "definitions": DEFINITIONS,
            "metric_metadata": METRIC_METADATA if metadata else {},
        },
        execution_kind=kind,
    )
    assert not audit_report(AnalystReport(report="per 1,000 reference units", claims=[]), ledger)[
        "verified"
    ]


def test_value_placeholders_format_exact_bound_values_without_changing_prose(tmp_path):
    runtime, _ = runtime_with_receipt(tmp_path)
    values = {
        "count": 1234,
        "fraction": 0.0000125,
        "negative": -1234.56789,
        "percent": 96.3234567,
        "zero": -0.0,
    }
    entry = runtime.toolkit.ledger.record(
        "local_python_repl", "# actual calculated values", {"ok": True, "values": values}
    )
    evidence = {
        item["path"].split(".")[-1]: item["id"]
        for item in runtime.model_receipt(entry)["numeric_evidence"]
    }
    text = (
        f"There are [value:{evidence['count']}] events; fraction [value:{evidence['fraction']}].\n"
        f"Change [value:{evidence['negative']}:2dp]; coverage [value:{evidence['percent']}:3dp]%.\n"
        f"Zero [value:{evidence['zero']}]. End of the original report."
    )
    report = runtime.resolve_report(ReportDraft(report=text))
    assert "There are 1,234 [claim:" in report.report
    assert "fraction 0.0000125 [claim:" in report.report
    assert "Change -1,234.57 [claim:" in report.report
    assert "coverage 96.323% [claim:" in report.report
    assert "Zero 0 [claim:" in report.report and report.report.endswith(
        "End of the original report."
    )
    assert audit_report(report, runtime.toolkit.ledger)["verified"]
    assert all(
        claim.execution_id == entry["execution_id"] and claim.code_sha256 == entry["code_sha256"]
        for claim in report.claims
    )
    assert {claim.path: claim.value for claim in report.claims} == {
        f"$.values.{name}": value for name, value in values.items()
    }
    html = render_business_report(report.report, runtime.toolkit.ledger, claims=report.claims)
    assert "[value:" not in html and "[claim:" not in html
    plain = runtime.resolve_report(ReportDraft(report=f"Count 1,234 [claim:{evidence['count']}]."))
    assert audit_report(plain, runtime.toolkit.ledger)["verified"]


def test_value_placeholders_reject_unknown_cross_run_and_malformed_tokens(tmp_path):
    runtime, entry = runtime_with_receipt(tmp_path / "first")
    other, other_entry = runtime_with_receipt(tmp_path / "second")
    identity = runtime.model_receipt(entry)["numeric_evidence"][0]["id"]
    cross_run = other.model_receipt(other_entry)["numeric_evidence"][0]["id"]
    for token in (
        "[value:invented]",
        f"[value:{cross_run}]",
        f"[value:{identity}:0dp]",
        f"[value:{identity}:7dp]",
        f"[value:{identity}:,.2f]",
        f"[value:{identity}:2dp:extra]",
        f"[value:{identity}",
        f"[VALUE:{identity}:2dp\n",
        f"[value:{identity}[value:invented]",
    ):
        report = runtime.resolve_report(ReportDraft(report="Before " + token + "\nAfter"))
        assert "[value:" not in report.report.lower(), token
        assert "Before " in report.report and report.report.endswith("After"), token
        assert not audit_report(report, runtime.toolkit.ledger)["verified"], token
    with pytest.raises(ValueError, match="placeholder"):
        render_business_report(f"Unresolved [value:{identity}].")
    assert not audit_report(
        AnalystReport(report=f"[value:{identity}]", claims=[]), runtime.toolkit.ledger
    )["verified"]


def test_default_numeric_display_preserves_small_values_and_rounding_tolerance(tmp_path):
    runtime, _ = runtime_with_receipt(tmp_path)
    values = {
        "cross_integer": 1.999999,
        "negative_cross": -1.999999,
        "probability": 0.000462123,
        "negative_probability": -0.000462123,
        "tiny": 1.23456e-12,
        "money": 12345678.9012345,
        "percent": 42.774566474,
        "count": 1234567,
        "zero": -0.0,
    }
    entry = runtime.toolkit.ledger.record("local_python_repl", "# structured measures", values)
    ids = {
        item["path"].split(".")[-1]: item["id"]
        for item in runtime.model_receipt(entry)["numeric_evidence"]
    }
    text = "\n".join(
        f"{name}: [value:{identity}]" + ("%" if name == "percent" else "")
        for name, identity in ids.items()
    )
    report = runtime.resolve_report(ReportDraft(report=text))
    assert audit_report(report, runtime.toolkit.ledger)["verified"]
    assert "cross_integer: 2.0000 " in report.report and "negative_cross: -2.0000 " in report.report
    assert (
        "probability: 0.0004621 " in report.report
        and "negative_probability: -0.0004621 " in report.report
    )
    assert "tiny: 0.000000000001235 " in report.report
    assert "money: 12,345,678.9012 " in report.report and "percent: 42.7746% " in report.report
    assert "count: 1,234,567 " in report.report and "zero: 0 " in report.report
    assert {claim.path[2:]: claim.value for claim in report.claims} == values
    explicit = runtime.resolve_report(
        ReportDraft(report=f"Explicit [value:{ids['probability']}:2dp].")
    )
    assert (
        "Explicit 0.00 " in explicit.report
        and audit_report(explicit, runtime.toolkit.ledger)["verified"]
    )


def test_custom_export_claims_bind_independently_without_changing_registered_bytes(tmp_path):
    from analyst.evidence import sha256

    runtime, entry = runtime_with_receipt(tmp_path)
    identity = runtime.model_receipt(entry)["numeric_evidence"][0]["id"]
    report = runtime.resolve_report(
        ReportDraft(report="The requested detail is in the exported document.")
    )
    report.deliverable_paths = ["artifacts/detail.md"]
    path = tmp_path / "artifacts" / "detail.md"
    path.parent.mkdir()
    path.write_text(f"Observed 12 events [claim:{identity}].", encoding="utf-8")
    original = path.read_bytes()
    runtime.toolkit.ledger.register_artifact(path)
    audit = audit_exported_reports(
        report, runtime.toolkit.ledger, claim_catalog=runtime.claim_catalog
    )
    assert audit["verified"] and path.read_bytes() == original
    other, other_entry = runtime_with_receipt(tmp_path / "another_run")
    stale = other.model_receipt(other_entry)["numeric_evidence"][0]["id"]
    for body in (
        f"Observed [value:{identity}].",
        "Observed 12 [claim:unknown].",
        f"Observed 12 [claim:{stale}].",
    ):
        path.write_text(body, encoding="utf-8")
        artifact = runtime.toolkit.ledger.register_artifact(path)
        assert not audit_exported_reports(
            report, runtime.toolkit.ledger, claim_catalog=runtime.claim_catalog
        )["verified"]
        assert (
            path.read_text(encoding="utf-8") == body
            and sha256(path.read_bytes()) == artifact["sha256"]
        )
    path.write_text(f"Observed 12 events [claim:{identity}].", encoding="utf-8")
    runtime.toolkit.ledger.register_artifact(path)
    assert audit_exported_reports(
        report, runtime.toolkit.ledger, claim_catalog=runtime.claim_catalog
    )["verified"]
    path.write_text("Tampered 999", encoding="utf-8")
    assert not audit_exported_reports(
        report, runtime.toolkit.ledger, claim_catalog=runtime.claim_catalog
    )["verified"]


def test_qa_receives_actual_zero_null_availability_and_workbook_delivery(tmp_path):
    runtime, _ = runtime_with_receipt(tmp_path)
    kpis = {
        "event_count": 0,
        "cohort_product_count": 3,
        "reference_output": 300,
        "reference_intensity_per_1000": 0,
        "cost_status": "empty",
        "cost_coverage_pct": None,
        "known_repair_cost": 0,
        "cost_unknown_count": 0,
    }
    delivery = {"mode": "completed_copy_from_supplied_template", "readback": {"status": "verified"}}
    runtime.toolkit.ledger.record(
        "run_case_study",
        "# actual result",
        {"ok": True, "analysis": {"kpis": kpis}, "workbook_delivery": delivery},
    )
    support, errors = runtime.qa_evidence(AnalystReport(report="No matching events.", claims=[]))
    assert not errors
    result = next(
        item for item in support["qualitative_support"] if item["tool"] == "run_case_study"
    )
    assert (
        next(
            item for item in result["aggregate_availability"] if item["path"] == "$.analysis.kpis"
        )["values"]
        == kpis
    )
    assert result["workbook_delivery"] == delivery


def test_business_html_changes_only_the_exact_known_number_note(tmp_path):
    from analyst.localization import VI_NUMBER_NOTE

    prose = VI_NUMBER_NOTE + "\n\nNội dung gốc vẫn được giữ nguyên."
    report = AnalystReport(report=prose, claims=[])
    html = render_business_report(report.report, language="vi")
    assert "Số trong báo cáo dùng dấu chấm tách hàng nghìn và dấu phẩy cho phần thập phân." in html
    assert VI_NUMBER_NOTE not in html and report.report == prose
