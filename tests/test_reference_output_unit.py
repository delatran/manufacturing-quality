"""Bind a static denominator unit only to its successful sealed KPI section."""

from __future__ import annotations

import copy
from dataclasses import fields
from types import SimpleNamespace

import pytest
import pytest_asyncio
from analyst.case_study.analytics import Scope
from analyst.evidence import EvidenceLedger, canonical, numeric_values, sha256
from analyst.product_tools import INSTRUCTIONS_VERSION, TOOLS_VERSION, ProductTools, tool_code
from analyst.report_output import evidence_manifest, metric_unit, unit_note, with_unit_note
from analyst.runtime import AnalystReport, NumericClaim, audit_report
from test_dashboard import service

PATH = "$.analysis.kpis.reference_output"
INTENSITY = "reference_intensity_per_1000"


@pytest_asyncio.fixture
async def denominator_source(tmp_path):
    # Use the existing offline snapshot loader and real central section tool.
    # This needs no model, network, app server or session database.
    backend = service(tmp_path)
    bound = await backend.get_context()

    async def read_context(filters=None):
        return await backend.get_context(filters, snapshot_id=bound["context"]["snapshot_id"])

    runtime = SimpleNamespace(
        state_store=None,
        analysis_service=backend,
        analysis_context=bound,
        context_entry=bound["evidence"],
        read_dashboard_context=read_context,
    )
    result, context = await ProductTools(runtime).call(
        "analysis_query",
        {"section": "kpis", "filters": {field.name: None for field in fields(Scope)}},
    )
    ledger = EvidenceLedger(tmp_path / "section-ledger")
    entry = ledger.record(
        "analysis_query",
        tool_code(),
        result,
        execution_kind="bounded_analysis",
        analysis_context=context,
    )
    claims = {
        metric: NumericClaim(
            id=metric,
            execution_id=entry["execution_id"],
            path=f"$.analysis.kpis.{metric}",
            value=result["analysis"]["kpis"][metric],
            code_sha256=entry["code_sha256"],
        )
        for metric in (
            "event_count",
            "known_repair_cost",
            "cost_known_count",
            "cost_unknown_count",
            INTENSITY,
            "eligible_event_count",
            "reference_output",
        )
    }
    assert ledger.verify_integrity() == []
    return ledger, entry, claims


def reseal(entry):
    entry["numeric_values"] = numeric_values(entry["result"])
    entry["result_sha256"] = sha256(canonical(entry["result"]))
    entry["code_sha256"] = sha256(entry["code"])
    entry["entry_sha256"] = sha256(
        canonical({key: value for key, value in entry.items() if key != "entry_sha256"})
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
async def test_same_section_denominator_has_units_and_preserves_mixed_report(
    denominator_source, language
):
    ledger, entry, claims = denominator_source
    original = canonical(entry)
    claim_rows = [claim.model_dump() for claim in claims.values()]

    def shown(metric):
        value = claims[metric].value
        text = f"{value:,.4f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
        if language == "vi":
            text = text.translate(str.maketrans({",": ".", ".": ","}))
        return text + " [claim:" + metric + "]"

    if language == "en":
        body = (
            f"Events: {shown('event_count')} events; observed cost: {shown('known_repair_cost')} local currency.\n"
            f"Known-cost events / total events: {shown('cost_known_count')} / {shown('event_count')}; unknown: {shown('cost_unknown_count')} events.\n"
            f"Reference-output intensity: {shown(INTENSITY)} events per 1,000 reference-output units, using {shown('eligible_event_count')} linked events and {shown('reference_output')} reference-output units. This is static reference output, not verified actual production."
        )
    else:
        body = (
            f"Số sự kiện: {shown('event_count')} sự kiện; chi phí: {shown('known_repair_cost')} local currency.\n"
            f"Sự kiện có chi phí / tổng sự kiện: {shown('cost_known_count')} / {shown('event_count')}; chưa rõ chi phí: {shown('cost_unknown_count')} sự kiện.\n"
            f"Cường độ tham chiếu: {shown(INTENSITY)} sự kiện trên 1.000 đơn vị tham chiếu, dùng {shown('eligible_event_count')} sự kiện liên kết và {shown('reference_output')} đơn vị tham chiếu. Sản lượng tham chiếu chưa được xác minh là sản lượng thực tế."
        )
    enriched = with_unit_note(body, claim_rows, ledger, language)
    assert enriched.startswith(body + "\n\n")
    report = AnalystReport(
        report=enriched,
        claims=list(claims.values()),
        number_format="vi-VN" if language == "vi" else "en-US",
    )
    audited = audit_report(report, ledger)
    assert audited["verified"], audited
    assert audited["checked_claims"] == 7
    manifest = evidence_manifest(
        ledger,
        claim_rows,
        language=language,
        instructions_version=INSTRUCTIONS_VERSION,
        tools_version=TOOLS_VERSION,
        bound_context=entry["analysis_context"],
    )
    refs = manifest["references"]
    ref = next(ref for ref in refs if ref["path"] == PATH)
    assert ref["role"] == "measurement"
    assert ref["unit"]["kind"] == "reference_output"
    source_unit = entry["result"]["metric_metadata"][INTENSITY]["unit"]
    assert ref["unit"]["unit"] == source_unit.removeprefix("logged events per ")
    assert all(ref["unit"] is not None for ref in refs)
    assert ref["context_ids"] == [entry["result"]["context"]["context_id"]]
    note = unit_note([claims["reference_output"].model_dump()], ledger, language)
    assert ("static reference units" if language == "en" else "đơn vị tham chiếu cố định") in note
    assert ("inspection exposure" if language == "en" else "quy mô kiểm tra") in note
    assert report.report == enriched
    assert canonical(entry) == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing_declaration",
        "metadata_null",
        "definitions_list",
        "wrong_scale",
        "wrong_denominator",
        "failed",
        "untrusted_tool",
        "authored_kind",
        "entry_context_mismatch",
        "requested_context_mismatch",
        "missing_leaf",
        "boolean_leaf",
        "sibling_metadata",
        "registry_only",
        "broken_seal",
    ],
)
async def test_reference_output_alias_fails_closed(denominator_source, fault):
    _, original, _ = denominator_source
    entry = copy.deepcopy(original)
    result = entry["result"]
    if fault == "missing_declaration":
        result["metric_metadata"].pop(INTENSITY)
    elif fault == "metadata_null":
        result["metric_metadata"] = None
    elif fault == "definitions_list":
        result["definitions"] = []
    elif fault == "wrong_scale":
        result["metric_metadata"][INTENSITY]["scale"] = 100
    elif fault == "wrong_denominator":
        result["metric_metadata"][INTENSITY]["denominator_kind"] = "actual production"
    elif fault == "failed":
        result["ok"] = False
    elif fault == "untrusted_tool":
        entry["tool"] = "python"
    elif fault == "authored_kind":
        entry["execution_kind"] = "authored_analysis"
    elif fault == "entry_context_mismatch":
        entry["analysis_context"]["context_id"] = "other-context"
    elif fault == "requested_context_mismatch":
        result["scope_semantics"]["requested_context_id"] = "other-context"
    elif fault == "missing_leaf":
        result["analysis"]["kpis"]["equal_sibling"] = result["analysis"]["kpis"].pop(
            "reference_output"
        )
    elif fault == "boolean_leaf":
        result["analysis"]["kpis"]["reference_output"] = True
    elif fault == "sibling_metadata":
        result["selected"] = {
            "context": {**result["context"], "context_id": "other-context"},
            "metric_metadata": result.pop("metric_metadata"),
            "definitions": result.pop("definitions"),
            "analysis": copy.deepcopy(result["analysis"]),
        }
    elif fault == "registry_only":
        result.pop("metric_metadata")
        entry["sources"] = [{"path": "knowledge/kpi_registry.yaml", "unit": "reference units"}]
        result["caption"] = "Reference output units"
    elif fault != "broken_seal":
        raise AssertionError(fault)
    reseal(entry)
    if fault == "broken_seal":
        entry["entry_sha256"] = "0" * 64
    assert metric_unit(entry, PATH) is None
    if fault == "sibling_metadata":
        assert metric_unit(entry, "$.selected.analysis.kpis.reference_output") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template",
    [
        "{value} [claim:reference_output] events.",
        "{value} [claim:reference_output] rows.",
        "{value} [claim:reference_output] local currency.",
        "{value}% [claim:reference_output].",
        "{value} [claim:reference_output] percentage points.",
        "Actual production: {value} [claim:reference_output].",
        "{value} [claim:reference_output] sản lượng thực tế.",
    ],
)
async def test_reference_denominator_cannot_be_displayed_as_other_units(
    denominator_source, template
):
    ledger, _, claims = denominator_source
    claim = claims["reference_output"]
    body = template.format(value=f"{claim.value:,.0f}")
    audit = audit_report(AnalystReport(report=body, claims=[claim]), ledger)
    assert not audit["verified"], audit
    assert any("unit" in error or "actual production" in error for error in audit["errors"])


@pytest.mark.asyncio
async def test_reference_alias_does_not_type_other_output_metrics(denominator_source):
    _, original, _ = denominator_source
    entry = copy.deepcopy(original)
    entry["result"]["analysis"]["kpis"].update(
        reference_output=None, known_monthly_output_subtotal=0, output_known_product_count=0
    )
    reseal(entry)
    assert metric_unit(entry, PATH) is None
    for metric in ("known_monthly_output_subtotal", "output_known_product_count"):
        assert metric_unit(entry, f"$.analysis.kpis.{metric}") is None
