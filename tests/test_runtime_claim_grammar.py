"""Typed calendar, identifier and unit grammar over sealed evidence, without a model."""

from __future__ import annotations

import copy
import re
from html import unescape

import pytest
from analyst.case_study.analytics import DEFINITIONS, METRIC_METADATA
from analyst.claim_feedback import numeric_role
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.report_output import (
    evidence_manifest,
    metadata_html,
    metric_unit,
    scope_description,
    visible_scope_header,
)
from analyst.runtime import AnalystReport, NumericClaim, audit_report, render_business_report
from test_report_output import context


def issue(entry, identity, path):
    value = next(row["value"] for row in entry["numeric_values"] if row["path"] == path)
    return NumericClaim(
        id=identity,
        execution_id=entry["execution_id"],
        path=path,
        value=value,
        code_sha256=entry["code_sha256"],
    )


def report(text, claims=(), language="en"):
    by_id = {claim.id: claim for claim in claims}

    def replace(match):
        claim = by_id[match[1]]
        number = (
            f"{claim.value:,.4f}".rstrip("0").rstrip(".")
            if type(claim.value) is float
            else f"{claim.value:,}"
        )
        if language == "vi":
            number = number.translate(str.maketrans({",": ".", ".": ","}))
        return number + " [claim:" + claim.id + "]"

    return AnalystReport(
        report=re.sub(r"\[value:([^\]]+)\]", replace, text),
        claims=list(claims),
        number_format="vi-VN" if language == "vi" else "en-US",
    )


@pytest.fixture
def grammar_subject(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    binding = context("canonical", production_line="")
    value = {
        "ok": True,
        "context": binding,
        "definitions": DEFINITIONS,
        "metric_metadata": copy.deepcopy(METRIC_METADATA),
        "analysis": {
            "scope": binding["filters"],
            "definitions": DEFINITIONS,
            "metric_metadata": copy.deepcopy(METRIC_METADATA),
            "kpis": {
                "event_count": 2024,
                "known_repair_cost": 65488.29,
                "reference_intensity_per_1000": 1.7952082912712832,
                "cohort_product_count": 0,
            },
            "location_severity": {
                "cells": [
                    {
                        "location": "Component",
                        "severity": "Critical",
                        "count": 148,
                        "row_pct": 42.77456647398844,
                    }
                ],
            },
            "calendar_coverage": {
                "scope_kind": "selected_scope",
                "scope": binding["filters"],
                "calendar_days": 182,
                "days_without_events": 14,
                "max_observed_day_of_month": 28,
            },
        },
    }
    entry = ledger.record(
        "analysis_query",
        "# central grammar fixture",
        value,
        execution_kind="bounded_analysis",
        analysis_context=binding,
    )
    claims = {
        name: issue(entry, name, path)
        for name, path in {
            "intensity": "$.analysis.kpis.reference_intensity_per_1000",
            "scale": "$.metric_metadata.reference_intensity_per_1000.scale",
            "percent_scale": "$.metric_metadata.critical_share_pct.scale",
            "count": "$.analysis.kpis.event_count",
            "cost": "$.analysis.kpis.known_repair_cost",
            "products": "$.analysis.kpis.cohort_product_count",
            "cell": "$.analysis.location_severity.cells[0].count",
            "row_pct": "$.analysis.location_severity.cells[0].row_pct",
            "day": "$.analysis.calendar_coverage.max_observed_day_of_month",
            "days": "$.analysis.calendar_coverage.calendar_days",
        }.items()
    }
    return ledger, entry, binding, claims


@pytest.mark.parametrize(
    "text,valid",
    [
        ("Selected January-June 2024 scope.", True),
        ("Selected January 2024 through June 2024 scope.", True),
        ("Selected Jan–Jun 2024 scope.", True),
        ("Phạm vi 01–06/2024.", True),
        ("Phạm vi tháng 1 đến tháng 6 năm 2024.", True),
        ("Repair cost: January-June 2024.", True),
        ("Selected January-July 2024 scope.", False),
        ("Selected January-June 2025 scope.", False),
        ("Selected June-January 2024 scope.", False),
        ("Phạm vi 01–13/2024.", False),
        ("Phạm vi 06–01/2024.", False),
        ("Observed 2024 events.", False),
        ("Observed cost 2024 local currency.", False),
        ("Observed 01–06/2024 events.", False),
        ("The recorded event count is 01–06/2024 events.", False),
        ("The repair cost equals January-June 2024 local currency.", False),
    ],
)
def test_calendar_ranges_bind_whole_frozen_period_without_proving_quantities(
    grammar_subject, text, valid
):
    ledger, _, _, _ = grammar_subject
    proposal = report(text)
    before = proposal.report
    audited = audit_report(proposal, ledger)
    assert audited["verified"] is valid, audited
    assert proposal.report == before


@pytest.mark.parametrize(
    "text,valid",
    [
        (
            "Reference-output intensity: [value:intensity] defect events per 1,000 output units.",
            True,
        ),
        (
            "Reference-output intensity: [value:intensity] defect events per 1,000 [claim:scale] reference-output units.",
            True,
        ),
        (
            "Reference intensity [value:intensity] events per 1,000 [claim:scale] reference output units.",
            True,
        ),
        (
            "Cường độ tham chiếu [value:intensity] sự kiện trên 1.000 [claim:scale] đơn vị sản lượng tham chiếu.",
            True,
        ),
        ("Reference intensity [value:intensity]; count 1,000 [claim:scale] events.", False),
        ("Reference intensity [value:intensity]; cost 1,000 [claim:scale] local currency.", False),
        ("Count [value:count] per 1,000 [claim:scale] reference-output units.", False),
        ("Reference intensity [value:intensity] events per 10,000 reference-output units.", False),
        (
            "Reference intensity [value:intensity] events per 1,000 [claim:percent_scale] reference-output units.",
            False,
        ),
        ("Observed [value:scale] USD; reference intensity [value:intensity].", False),
        ("Observed USD [value:scale]; reference intensity [value:intensity].", False),
        (
            "Reference intensity [value:intensity]. Per 1,000 [claim:scale] reference-output units.",
            False,
        ),
    ],
)
def test_intensity_scale_citations_preserve_same_clause_role_and_declared_metric(
    grammar_subject, text, valid
):
    ledger, _, _, claims = grammar_subject
    language = "vi" if "Cường" in text else "en"
    proposal = report(text, claims.values(), language)
    before = proposal.report
    audit = audit_report(proposal, ledger)
    assert audit["verified"] is valid, audit
    assert proposal.report == before


def test_explicit_scale_cannot_be_borrowed_from_another_context(grammar_subject):
    ledger, entry, binding, claims = grammar_subject
    result = copy.deepcopy(entry["result"])
    result["context"] = context("other", start="2024-02", end="2024-02", production_line="")
    other = ledger.record(
        "analysis_query",
        "# another frozen context",
        result,
        execution_kind="bounded_analysis",
        analysis_context=result["context"],
    )
    scale = issue(other, "other_scale", "$.metric_metadata.reference_intensity_per_1000.scale")
    proposal = report(
        "Reference intensity [value:intensity] per 1,000 [claim:other_scale] reference-output units.",
        [claims["intensity"], scale],
    )
    assert not audit_report(proposal, ledger)["verified"]
    assert binding["context_id"] != result["context"]["context_id"]


@pytest.mark.parametrize(
    "metric,text,kind,valid",
    [
        ("cell", "Cell [value:cell] events.", "count", True),
        ("cell", "Cell [value:cell] local currency.", "count", False),
        ("row_pct", "Within-location share [value:row_pct]%.", "percentage", True),
        ("row_pct", "Share [value:row_pct] events.", "percentage", False),
        ("day", "Last observed day [value:day].", "calendar_day_ordinal", True),
        ("day", "Observed [value:day] events.", "calendar_day_ordinal", False),
        ("day", "Observed cost [value:day] local currency.", "calendar_day_ordinal", False),
        ("day", "Observed [value:day] USD.", "calendar_day_ordinal", False),
        ("days", "Calendar span [value:days] calendar days.", "calendar_day_count", True),
        ("days", "Observed [value:days] events.", "calendar_day_count", False),
        ("products", "Eligible [value:products] master products.", "product_count", True),
        ("products", "Observed [value:products] events.", "product_count", False),
        ("products", "Observed [value:products] VND.", "product_count", False),
        ("products", "Observed cost [value:products] local currency.", "product_count", False),
    ],
)
def test_units_retain_location_calendar_and_master_product_grains(
    grammar_subject, metric, text, kind, valid
):
    ledger, entry, _, claims = grammar_subject
    claim = claims[metric]
    unit = metric_unit(entry, claim.path)
    assert unit["kind"] == kind
    if metric == "row_pct":
        assert "location" in unit["definition"] and "row denominator" in unit["definition"]
    audit = audit_report(report(text, [claim]), ledger)
    assert audit["verified"] is valid, audit


@pytest.mark.parametrize(
    "text,valid",
    [
        ("Top known-cost Product ID remains [value:leader].", True),
        ("Product IDs [value:first] and [value:second] change position.", True),
        (
            "Product IDs [value:first] and [value:second] switch relative order in the displayed ranking to [value:second] and [value:first].",
            True,
        ),
        (
            "Product IDs [value:first] and [value:second] change ranking to [value:second] and [value:first] events.",
            False,
        ),
        ("Product ID [value:first] had 76 events.", False),
        ("Observed [value:first] events.", False),
        ("Cost [value:first] local currency.", False),
        ("Observed [value:first] USD.", False),
        ("Observed USD [value:first].", False),
        ("Observed [value:first] dollars.", False),
        ("Product ID [value:first]; observed cost [value:cost] local currency.", True),
        ("Product ID remains 76 [claim:leader].", False),
    ],
)
def test_scalar_and_declared_rank_array_identifiers_never_become_measurements(
    grammar_subject, text, valid
):
    ledger, original, binding, original_claims = grammar_subject
    result = {
        "ok": True,
        "canonical": {"context": binding, "analysis": original["result"]["analysis"]},
        "selected": {
            "context": context("alternative", scenario="exclude_cost_10", production_line=""),
            "analysis": original["result"]["analysis"],
        },
        "comparison": {
            "priority_signals": {"selected": {"highest_known_cost_product_id": 81}},
            "rankings": {"products_by_known_cost": {"canonical": [76, 27]}},
        },
    }
    entry = ledger.record(
        "scenario_compare",
        "# scenario ranking identities",
        result,
        execution_kind="bounded_analysis",
        analysis_context=binding,
    )
    claims = [
        issue(
            entry, "leader", "$.comparison.priority_signals.selected.highest_known_cost_product_id"
        ),
        issue(entry, "first", "$.comparison.rankings.products_by_known_cost.canonical[0]"),
        issue(entry, "second", "$.comparison.rankings.products_by_known_cost.canonical[1]"),
        original_claims["cost"],
    ]
    assert all(numeric_role(entry, claim.path) == "identifier" for claim in claims[:3])
    audit = audit_report(report(text, claims), ledger)
    assert audit["verified"] is valid, audit


@pytest.mark.parametrize("language", ["en", "vi"])
def test_header_projection_is_the_visible_renderer_context_and_not_authored_prose(
    grammar_subject, language
):
    ledger, entry, binding, claims = grammar_subject
    selected = [claims["count"]]
    manifest = evidence_manifest(
        ledger,
        [claim.model_dump() for claim in selected],
        language=language,
        instructions_version="investigation-v7",
        tools_version="bounded-analysis-v7",
        bound_context=binding,
    )
    header = visible_scope_header(manifest)
    assert header["shared_period"] == {"start_month": "2024-01", "end_month": "2024-06"}
    assert header["source_contexts_sha256"] == sha256(canonical(manifest["contexts"]))
    rendered = metadata_html(manifest)
    assert all(row["text"] in unescape(rendered) for row in header["contexts"])
    prose = report("Count [value:count] events.", selected, language)
    html = render_business_report(
        prose.report, ledger, language=language, claims=selected, bound_context=binding
    )
    assert scope_description(binding["filters"], language) in unescape(html)
    assert prose.report.startswith("Count ")
    assert entry["result"]["context"] == binding


def test_mixed_period_header_never_invents_one_common_period(grammar_subject):
    ledger, original, binding, claims = grammar_subject
    before = copy.deepcopy(original["result"])
    before["context"] = context("january", end="2024-01", production_line="")
    after = copy.deepcopy(original["result"])
    after["context"] = context("june", start="2024-06", production_line="")
    entry = ledger.record(
        "analysis_compare",
        "# exact mixed periods",
        {"ok": True, "baseline": before, "comparison": after},
        execution_kind="bounded_analysis",
        analysis_context=binding,
    )
    claim = issue(entry, "first_count", "$.baseline.analysis.kpis.event_count")
    manifest = evidence_manifest(
        ledger,
        [claim.model_dump()],
        language="en",
        instructions_version="investigation-v7",
        tools_version="bounded-analysis-v7",
        bound_context=binding,
    )
    header = visible_scope_header(manifest)
    assert header["shared_period"] is None
    assert {row["context_id"] for row in header["contexts"]} == {"canonical", "january", "june"}
    assert claims["count"].value == claim.value


@pytest.mark.parametrize("observed_cost", [10, 285])
@pytest.mark.parametrize(
    "text,valid",
    [
        ("Withholding repair costs equal to [value:parameter] changes cost availability.", True),
        (
            "Scenario assumption: repair costs equal to [value:parameter] are financially unavailable.",
            True,
        ),
        ("Criterion: repair costs equal to [value:parameter].", True),
        ("Observed repair cost [value:subtotal] local currency.", True),
        (
            "Scenario assumption: repair costs equal to [value:parameter] are financially unavailable; observed repair cost [value:subtotal] local currency.",
            True,
        ),
        (
            "Under the scenario, observed repair cost equals [value:parameter] local currency.",
            False,
        ),
        (
            "Scenario assumption: recorded repair cost equals [value:parameter] local currency.",
            False,
        ),
        (
            "Observed repair cost [value:subtotal] local currency; under the scenario, observed repair cost equals [value:parameter] local currency.",
            False,
        ),
        (
            "Scenario has [value:parameter] logged defect events; the count is [value:events] events.",
            False,
        ),
    ],
)
def test_parameter_purpose_is_bound_to_each_citation_even_when_a_measurement_has_equal_digits(
    grammar_subject, observed_cost, text, valid
):
    ledger, original, binding, _ = grammar_subject
    analysis = copy.deepcopy(original["result"]["analysis"])
    analysis["kpis"].update(known_repair_cost=observed_cost, event_count=10)
    entry = ledger.record(
        "scenario_compare",
        "# purpose-bound scenario parameter and real measurements",
        {
            "ok": True,
            "canonical": {"context": binding, "analysis": analysis},
            "selected": {
                "context": context("scenario", scenario="exclude_cost_10"),
                "analysis": analysis,
            },
            "comparison": {
                "scenario_definition": {
                    "parameters": {
                        "repair_cost_equal": 10,
                        "repair_cost_unit": "local currency",
                        "effect": "withhold_financial_availability",
                    }
                }
            },
        },
        execution_kind="bounded_analysis",
        analysis_context=binding,
    )
    claims = [
        issue(entry, "parameter", "$.comparison.scenario_definition.parameters.repair_cost_equal"),
        issue(entry, "subtotal", "$.selected.analysis.kpis.known_repair_cost"),
        issue(entry, "events", "$.selected.analysis.kpis.event_count"),
    ]
    proposal = report(text, claims)
    before = proposal.report
    audit = audit_report(proposal, ledger)
    assert audit["verified"] is valid, audit
    assert proposal.report == before
    if not valid:
        assert any("criterion parameter" in error for error in audit["errors"])
