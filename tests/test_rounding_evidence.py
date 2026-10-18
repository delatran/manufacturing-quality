"""Rounding metadata cannot supply observed counts in deterministic report audits."""

from __future__ import annotations

import json
from dataclasses import fields

import pytest
from analyst.case_study.analytics import Scope
from analyst.claim_feedback import numeric_role
from analyst.report_output import metric_unit, with_unit_note
from analyst.runtime import ReportDraft, audit_report
from test_measurement_units import actual6 as actual6

_ROOT = "$.decomposition.known_cost_reconciliation."


def rounding_report(subject, metric, template):
    evidence = subject.evidence[_ROOT + metric]
    report = subject.runtime.resolve_report(
        ReportDraft(report=template.replace("VALUE", "[value:" + evidence["id"] + "]"))
    )
    entry = next(
        item
        for item in subject.tools.ledger.records()
        if item["execution_id"] == report.claims[0].execution_id
    )
    return report, entry


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize("metric", ["display_decimal_places", "displayed_change_residual"])
async def test_rounding_metadata_cannot_supply_event_counts(actual6, language, metric):
    template = (
        "Observed VALUE logged defect events."
        if language == "en"
        else "Đã ghi nhận VALUE sự kiện lỗi."
    )
    report, entry = rounding_report(actual6, metric, template)
    audit = audit_report(report, actual6.tools.ledger)
    assert not audit["verified"], audit
    assert any(
        "observed measurement" in error or "incompatible" in error for error in audit["errors"]
    )
    raw = entry["result"]["decomposition"]["known_cost_reconciliation"]
    assert raw["display_decimal_places"] == 2
    assert raw["displayed_change_residual"] == 0
    assert entry["result"]["decomposition"]["reconciliation"]["known_cost_change_reconciles"]
    assert {item["path"] for item in entry["numeric_values"]} >= {
        _ROOT + "display_decimal_places",
        _ROOT + "displayed_change_residual",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
async def test_display_precision_cannot_supply_an_unlabelled_observation(actual6, language):
    template = "Observed count: VALUE." if language == "en" else "Số đếm quan sát: VALUE."
    report, entry = rounding_report(actual6, "display_decimal_places", template)
    audit = audit_report(report, actual6.tools.ledger)
    assert not audit["verified"], audit
    assert numeric_role(entry, report.claims[0].path) == "display_precision"
    assert metric_unit(entry, report.claims[0].path)["unit"] == "decimal places"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
async def test_displayed_rounding_difference_keeps_its_monetary_semantics(actual6, language):
    template = (
        "The displayed cost-change rounding difference is VALUE local currency."
        if language == "en"
        else "Chênh lệch do làm tròn mức thay đổi chi phí hiển thị là VALUE nội tệ."
    )
    report, entry = rounding_report(actual6, "displayed_change_residual", template)
    audit = audit_report(report, actual6.tools.ledger)
    assert audit["verified"], audit
    unit = metric_unit(entry, report.claims[0].path)
    assert unit["kind"] == "cost_diagnostic"
    assert unit["unit"] == "local currency"
    assert "rounding" in unit["definition"]
    described = with_unit_note(
        report.report,
        [claim.model_dump() for claim in report.claims],
        actual6.tools.ledger,
        language,
    )
    note = described.removeprefix(report.report)
    assert "rounding" in note if language == "en" else "làm tròn" in note


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
async def test_rounding_difference_does_not_invent_a_currency(actual6, language):
    template = (
        "The displayed cost-change rounding difference is VALUE USD."
        if language == "en"
        else "Chênh lệch do làm tròn mức thay đổi chi phí hiển thị là VALUE đồng."
    )
    report, _ = rounding_report(actual6, "displayed_change_residual", template)
    audit = audit_report(report, actual6.tools.ledger)
    assert not audit["verified"], audit
    assert any("currency is unsupported" in error for error in audit["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize(
    ("tool", "prefix"),
    [
        ("analysis_query", "$.analysis.decomposition.production_line"),
        ("dashboard_context", "$.decomposition.production_line"),
        ("dashboard_compare_scope", "$.decomposition.production_line"),
        ("analysis_compare", "$.baseline.analysis.decomposition.production_line"),
        ("analysis_compare", "$.comparison.analysis.decomposition.production_line"),
        ("scenario_compare", "$.canonical.analysis.decomposition.production_line"),
        ("scenario_compare", "$.selected.analysis.decomposition.production_line"),
    ],
)
async def test_nested_rounding_metadata_retains_roles_after_detail_navigation(
    actual6, language, tool, prefix
):
    empty_filters = {field.name: None for field in fields(Scope)}
    if tool == "analysis_compare":
        receipt_id = actual6.comparison["receipt_id"]
    else:
        arguments = (
            {"section": "decomposition", "filters": empty_filters}
            if tool == "analysis_query"
            else {"scenario": "exclude_cost_10"}
            if tool == "scenario_compare"
            else {"filters": empty_filters}
            if tool == "dashboard_compare_scope"
            else {}
        )
        response = json.loads(await actual6.runtime._call(tool, arguments))
        assert response["status"] == "completed", response
        receipt_id = response["receipt_id"]
    detail = json.loads(
        await actual6.runtime._call(
            "evidence_detail",
            {
                "receipt_id": receipt_id,
                "path": prefix + ".known_cost_reconciliation",
                "offset": 0,
                "limit": 20,
            },
        )
    )
    assert detail["status"] == "completed", detail
    evidence = {item["path"]: item for item in detail["numeric_evidence"]}
    entry = next(
        item for item in actual6.tools.ledger.records() if item["execution_id"] == receipt_id
    )
    for metric in ("display_decimal_places", "displayed_change_residual"):
        path = prefix + ".known_cost_reconciliation." + metric
        marker = "[value:" + evidence[path]["id"] + "]"
        text = (
            "Observed " + marker + " logged defect events."
            if language == "en"
            else "Đã ghi nhận " + marker + " sự kiện lỗi."
        )
        report = actual6.runtime.resolve_report(ReportDraft(report=text))
        audit = audit_report(report, actual6.tools.ledger)
        assert not audit["verified"], (tool, path, audit)
        expected_kind = (
            "display_precision" if metric == "display_decimal_places" else "cost_diagnostic"
        )
        assert metric_unit(entry, path)["kind"] == expected_kind
        if metric == "display_decimal_places":
            assert numeric_role(entry, path) == "display_precision"
        else:
            text = (
                "The displayed cost-change rounding difference is " + marker + " local currency."
                if language == "en"
                else "Chênh lệch do làm tròn mức thay đổi chi phí hiển thị là "
                + marker
                + " nội tệ."
            )
            valid = actual6.runtime.resolve_report(ReportDraft(report=text))
            assert audit_report(valid, actual6.tools.ledger)["verified"]
    assert actual6.tools.ledger.verify_integrity() == []
