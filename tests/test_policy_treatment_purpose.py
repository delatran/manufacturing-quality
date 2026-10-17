"""Treatment-as-availability defines a policy criterion, never a measurement."""

from __future__ import annotations

import copy

import pytest
from analyst.claim_feedback import numeric_role, policy_parameter_usage, typed_policy_text
from analyst.runtime import audit_report
from test_report_output import context
from test_runtime_claim_grammar import grammar_subject as grammar_subject
from test_runtime_claim_grammar import issue, report


@pytest.fixture(params=[10, 11])
def treatment_source(grammar_subject, request):
    ledger, original, binding, _ = grammar_subject
    analysis = copy.deepcopy(original["result"]["analysis"])
    # A real amount/count can have the same digits as the criterion.
    analysis["kpis"].update(known_repair_cost=10, event_count=10)
    entry = ledger.record(
        "scenario_compare",
        "# sealed central fixture for treatment-purpose grammar",
        {
            "ok": True,
            "canonical": {"context": binding, "analysis": analysis},
            "selected": {
                "context": context("treatment", scenario="withheld_cost", production_line=""),
                "analysis": analysis,
            },
            "comparison": {
                "scenario_definition": {
                    "parameters": {
                        "repair_cost_equal": request.param,
                        "repair_cost_unit": "local currency",
                        "effect": "withhold_financial_availability",
                    }
                },
                "priority_signals": {"selected": {"highest_known_cost_product_id": 10}},
                "metric_deltas": {"event_count": {"delta": -1}},
            },
        },
        execution_kind="bounded_analysis",
        analysis_context=binding,
    )
    claims = {
        name: issue(entry, name, path)
        for name, path in {
            "parameter": "$.comparison.scenario_definition.parameters.repair_cost_equal",
            "cost": "$.selected.analysis.kpis.known_repair_cost",
            "events": "$.selected.analysis.kpis.event_count",
            "product": "$.comparison.priority_signals.selected.highest_known_cost_product_id",
            "delta": "$.comparison.metric_deltas.event_count.delta",
        }.items()
    }
    return ledger, entry, claims


@pytest.mark.parametrize(
    "template",
    [
        "The assumption treats repair costs equal to [value:parameter] as financially unavailable.",
        "The scenario treats repair costs equal to [value:parameter] as financially unavailable; it does not establish that those records are incorrect.",
        "Treat repair costs equal to [value:parameter] as financially unavailable.",
        "The assumption TREATS repair costs equal to [value:parameter] AS financially unavailable.",
        "**The assumption treats repair costs equal to [value:parameter] as financially unavailable.**",
        "The assumption treats\trepair costs equal to [value:parameter]\tas financially unavailable.",
        "The assumption treats repair costs equal to [value:parameter] local currency as financially unavailable.",
        "The assumption treats repair costs equal to [value:parameter] as financially unavailable. Separately, observed cost is [value:cost] local currency and the event count is [value:events] events.",
    ],
)
def test_treatment_availability_stays_bound_to_the_parameter(treatment_source, template):
    ledger, entry, claims = treatment_source
    original = copy.deepcopy(entry)
    proposal = report(template, claims.values())
    audited = audit_report(proposal, ledger)
    assert audited["verified"], audited
    assert ledger.verify_integrity() == []
    assert numeric_role(entry, claims["parameter"].path) == "policy_parameter"
    assert entry == original


@pytest.mark.parametrize(
    "template",
    [
        "Observed repair cost equals [value:parameter] local currency.",
        "The scenario has [value:parameter] events; the actual count is [value:events] events.",
        "Observed repair costs equal to [value:parameter] as financially unavailable.",
        "Total repair costs equal to [value:parameter] as financially unavailable.",
        "The ledger lists repair costs equal to [value:parameter] as financially unavailable.",
        "The assumption treats repair costs equal to [value:parameter] as observed repair cost.",
        "The assumption treats repair costs equal to [value:parameter] USD as financially unavailable.",
        "The assumption treats repair costs equal to [value:parameter]% as financially unavailable.",
        "The assumption treats repair costs equal to [value:parameter] pp as financially unavailable.",
        "The assumption treats repair costs equal to [value:cost] as financially unavailable. Observed cost: [value:parameter].",
        "The assumption treats repair costs equal to [value:events] as financially unavailable. Event count: [value:parameter] events.",
        "Product ID [value:parameter] is the priority; criterion [value:product].",
        "The assumption treats repair costs equal to [value:parameter]. Another sentence describes them as financially unavailable.",
        "The assumption treats repair costs equal to [value:parameter] as financially unavailable; event count decreased by [value:delta].",
    ],
)
def test_treatment_phrase_does_not_exempt_other_purposes_or_units(treatment_source, template):
    ledger, _, claims = treatment_source
    proposal = report(template, claims.values())
    audited = audit_report(proposal, ledger)
    assert not audited["verified"], audited


@pytest.mark.parametrize("language", ["en", "vi"])
def test_existing_explicit_selector_forms_keep_their_meaning(treatment_source, language):
    ledger, _, claims = treatment_source
    text = (
        "Scenario assumption: repair costs equal to [value:parameter] are financially unavailable."
        if language == "en"
        else "Giả định kịch bản: chi phí sửa chữa bằng [value:parameter] không được dùng để tính chi phí."
    )
    assert audit_report(report(text, claims.values(), language), ledger)["verified"]


def test_wrong_bound_value_and_detached_marker_are_rejected(treatment_source):
    ledger, _, claims = treatment_source
    claim = claims["parameter"]
    wrong_value = int(claim.value) + 100
    for text in (
        f"The assumption treats repair costs equal to {wrong_value} [claim:parameter] as financially unavailable.",
        f"The assumption treats repair costs equal to {int(claim.value)} as financially unavailable. Separately, [claim:parameter].",
        "The assumption treats repair costs equal to [value:parameter]\nas financially unavailable.",
    ):
        audited = audit_report(report(text, claims.values()), ledger)
        assert not audited["verified"], audited


def test_only_a_typed_source_equal_to_the_claim_is_masked(treatment_source):
    _, original, claims = treatment_source
    claim = claims["parameter"].model_dump()
    line = f"The assumption treats repair costs equal to {int(claim['value'])} as financially unavailable."
    entries = {original["execution_id"]: original}
    assert "[cited policy criterion]" in typed_policy_text(line, [claim], entries)
    for fault in ("value", "role", "tool", "effect"):
        entry, changed = copy.deepcopy(original), dict(claim)
        if fault == "value":
            changed["value"] += 100
        elif fault == "role":
            changed["path"] = claims["events"].path
        elif fault == "tool":
            entry["tool"] = "python"
        elif fault == "effect":
            entry["result"]["comparison"]["scenario_definition"]["parameters"]["effect"] = "other"
        assert "[cited policy criterion]" not in typed_policy_text(
            line, [changed], {entry["execution_id"]: entry}
        )
    marker = f"The assumption treats repair costs equal to {int(claim['value'])} [claim:parameter] as financially unavailable."
    assert policy_parameter_usage(marker, claim, marker.index("[claim:parameter]"))
    assert not policy_parameter_usage(
        marker.replace(" [claim:parameter]", " events [claim:parameter]"),
        claim,
        marker.replace(" [claim:parameter]", " events [claim:parameter]").index(
            "[claim:parameter]"
        ),
    )


def test_availability_grammar_does_not_make_failed_evidence_successful(treatment_source):
    ledger, original, _ = treatment_source
    result = copy.deepcopy(original["result"])
    result["ok"] = False
    failed = ledger.record(
        "scenario_compare",
        original["code"],
        result,
        execution_kind="bounded_analysis",
        analysis_context=original["analysis_context"],
    )
    parameter = issue(
        failed, "parameter", "$.comparison.scenario_definition.parameters.repair_cost_equal"
    )
    audited = audit_report(
        report(
            "The assumption treats repair costs equal to [value:parameter] as financially unavailable.",
            [parameter],
        ),
        ledger,
    )
    assert not audited["verified"]
    assert any("execution did not succeed" in error for error in audited["errors"])
