"""Bind cohort subsets and full-month counts to their own sealed selected query."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analyst.case_study.analytics import DEFINITIONS
from analyst.evidence import EvidenceLedger, canonical, numeric_values, sha256
from analyst.report_output import display_errors, evidence_manifest, metric_unit, with_unit_note

ALIASES = ("cohort_products_with_events", "cohort_products_without_events", "month_count")


def source(ledger, identity="scope", counts=(3, 1, 2), overview=False):
    context = {
        "context_id": identity,
        "snapshot_id": "synthetic-snapshot",
        "dataset_version": "synthetic-dataset",
        "policy_version": "synthetic-policy",
        "scenario": "",
        "filters": {
            "start_month": "2025-02",
            "end_month": "2025-03",
            "production_line": identity,
            "category": "",
            "product_id": "",
            "severity": "",
            "inspection_method": "",
            "defect_type": "",
            "defect_location": "",
            "scenario": "",
        },
    }
    result = {
        "ok": True,
        "context": context,
        "analysis": {
            "kpis": {
                "cohort_product_count": counts[0],
                "cohort_products_with_events": counts[1],
                "cohort_products_without_events": counts[2],
                "month_count": 2,
            }
        },
        "definitions": deepcopy(DEFINITIONS),
        "scope_semantics": {
            "kind": "selected_scope",
            "filters_applied": True,
            "requested_context_id": identity,
            "requested_filters": deepcopy(context["filters"]),
            "scenario_applied": False,
        },
    }
    if overview:
        result["analysis"].update(
            trend=[],
            calendar_coverage={
                "scope_kind": "selected_scope",
                "scope": deepcopy(context["filters"]),
                "selected_month_count": 2,
            },
        )
    entry = ledger.record(
        "analysis_query",
        "# central selected-scope cohort/calendar fixture",
        result,
        execution_kind="bounded_analysis",
        analysis_context=deepcopy(context),
    )
    claims = [
        {
            "id": identity + "_" + metric,
            "execution_id": entry["execution_id"],
            "path": "$.analysis.kpis." + metric,
            "value": value,
            "code_sha256": entry["code_sha256"],
        }
        for metric, value in entry["result"]["analysis"]["kpis"].items()
    ]
    return entry, claims


def reseal(entry):
    entry["numeric_values"] = numeric_values(entry["result"])
    entry["result_sha256"] = sha256(canonical(entry["result"]))
    entry["code_sha256"] = sha256(entry["code"])
    entry["entry_sha256"] = sha256(
        canonical({key: value for key, value in entry.items() if key != "entry_sha256"})
    )


def checked_unit(entry, path):
    before = canonical(entry)
    unit = metric_unit(entry, path)
    assert canonical(entry) == before
    return unit


@pytest.fixture
def subject(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    entry, claims = source(ledger)
    assert ledger.verify_integrity() == []
    return ledger, entry, claims


@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize("counts", [(3, 1, 2), (0, 0, 0)])
@pytest.mark.parametrize("overview", [False, True])
def test_selected_cohort_and_month_units_survive_zero_scope(tmp_path, language, counts, overview):
    ledger = EvidenceLedger(tmp_path)
    entry, claims = source(ledger, counts=counts, overview=overview)
    original = ledger.path.read_bytes()
    for claim in claims:
        unit = checked_unit(entry, claim["path"])
        assert unit is not None
        assert unit["metric"] == claim["path"].rpartition(".")[2]
        if claim["path"].endswith(".month_count"):
            assert unit["kind"] == "calendar_month_count"
            assert unit["unit"] == "calendar months"
            assert "full calendar months" in unit["definition"]
        else:
            assert unit["kind"] == "product_count" and unit["unit"] == "master products"
    descriptions = {
        claim["path"].rpartition(".")[2]: checked_unit(entry, claim["path"])["definition"]
        for claim in claims
    }
    assert (
        "at least one selected logged defect event" in descriptions["cohort_products_with_events"]
    )
    assert "without selected logged defect events" in descriptions["cohort_products_without_events"]
    labels = (
        (
            "Eligible cohort",
            "Products with events",
            "Products without events",
            "Selected full months",
        )
        if language == "en"
        else ("Tập sản phẩm", "Sản phẩm có sự kiện", "Sản phẩm không có sự kiện", "Số tháng đầy đủ")
    )
    body = "\n".join(
        f"{label}: {claim['value']} [claim:{claim['id']}]."
        for label, claim in zip(labels, claims, strict=True)
    )
    entries = {entry["execution_id"]: entry}
    assert all(display_errors(line, claims, entries) == [] for line in body.splitlines())
    shown = with_unit_note(body, claims, ledger, language)
    assert shown.startswith(body + "\n\n")
    assert ("master products" if language == "en" else "danh mục gốc") in shown
    assert ("Calendar-month" if language == "en" else "Số tháng trên lịch") in shown
    manifest = evidence_manifest(
        ledger, claims, language=language, instructions_version="fixture", tools_version="fixture"
    )
    assert all(
        ref["unit"] is not None and ref["context_ids"] == ["scope"]
        for ref in manifest["references"]
    )
    assert ledger.path.read_bytes() == original


@pytest.mark.parametrize("metric", ALIASES)
@pytest.mark.parametrize("wrong_unit", ["events", "local currency"])
def test_new_aliases_reject_event_and_currency_labels(subject, metric, wrong_unit):
    _, entry, claims = subject
    claim = next(claim for claim in claims if claim["path"].endswith("." + metric))
    text = f"{claim['value']} [claim:{claim['id']}] {wrong_unit}."
    assert display_errors(text, [claim], {entry["execution_id"]: entry})


def test_identical_paths_keep_separate_selected_contexts(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    first, first_claims = source(ledger, "nonempty")
    second, second_claims = source(ledger, "empty", (0, 0, 0))
    before = ledger.path.read_bytes()
    manifest = evidence_manifest(
        ledger,
        first_claims + second_claims,
        language="en",
        instructions_version="fixture",
        tools_version="fixture",
    )
    for entry, claims in ((first, first_claims), (second, second_claims)):
        expected = entry["result"]["context"]["context_id"]
        for claim in claims:
            reference = next(ref for ref in manifest["references"] if ref["id"] == claim["id"])
            assert reference["path"] == claim["path"]
            assert reference["context_ids"] == [expected] and reference["unit"] is not None
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize(
    "path",
    [
        "$.analysis.kpis.cohort_products_with_events_extra",
        "$.analysis.other.cohort_products_with_events",
        "$.selected.analysis.kpis.cohort_products_with_events",
    ],
)
def test_unknown_or_other_wrapper_paths_stay_undeclared(subject, path):
    _, entry, _ = subject
    entry["result"]["analysis"]["kpis"]["cohort_products_with_events_extra"] = 1
    entry["result"]["analysis"]["other"] = {"cohort_products_with_events": 1}
    entry["result"]["selected"] = deepcopy(entry["result"])
    reseal(entry)
    assert checked_unit(entry, path) is None


@pytest.mark.parametrize(
    "fault",
    [
        "missing_cohort_declaration",
        "wrong_month_declaration",
        "definitions_list",
        "sibling_only",
        "context_mismatch",
        "request_mismatch",
        "root_scope",
        "direct_definition",
        "direct_metric_metadata",
        "failed",
        "untrusted",
        "authored",
    ],
)
def test_aliases_need_their_own_successful_selected_declaration(subject, fault):
    _, entry, _ = subject
    result = entry["result"]
    metric = "cohort_products_with_events"
    if fault == "missing_cohort_declaration":
        result["definitions"].pop("cohort_product_count")
    elif fault == "wrong_month_declaration":
        metric = "month_count"
        result["definitions"]["event_count"] = "Months containing selected logged events only."
    elif fault == "definitions_list":
        result["definitions"] = []
    elif fault == "sibling_only":
        result["selected"] = {"definitions": result.pop("definitions")}
    elif fault == "context_mismatch":
        entry["analysis_context"]["context_id"] = "another-context"
    elif fault == "request_mismatch":
        result["scope_semantics"]["requested_context_id"] = "another-context"
    elif fault == "root_scope":
        result["scope"] = {**result["context"]["filters"], "production_line": "other"}
    elif fault == "direct_definition":
        result["definitions"][metric] = "Logged events only."
    elif fault == "direct_metric_metadata":
        result["metric_metadata"] = {metric: {"unit": "logged events", "scale": 1}}
    elif fault == "failed":
        result["ok"] = False
    elif fault == "untrusted":
        entry["tool"] = "python"
    else:
        entry["execution_kind"] = "authored_analysis"
    reseal(entry)
    assert checked_unit(entry, "$.analysis.kpis." + metric) is None


@pytest.mark.parametrize("metric", ALIASES)
@pytest.mark.parametrize("owner", ["analysis", "kpis"])
@pytest.mark.parametrize("key", ["context", "scope", "definitions", "metric_metadata"])
def test_nearer_owner_cannot_borrow_root_declarations(subject, metric, owner, key):
    _, entry, _ = subject
    analysis = entry["result"]["analysis"]
    parent = analysis if owner == "analysis" else analysis["kpis"]
    if key == "context":
        parent[key] = {
            **entry["result"]["context"],
            "context_id": "another-context",
            "filters": {**entry["result"]["context"]["filters"], "production_line": "other"},
        }
    elif key == "scope":
        parent[key] = {**entry["result"]["context"]["filters"], "production_line": "other"}
    elif key == "definitions":
        parent[key] = {"cohort_product_count": "Logged events only."}
    else:
        parent[key] = {metric: {"unit": "logged events", "scale": 1}}
    reseal(entry)
    assert checked_unit(entry, "$.analysis.kpis." + metric) is None


@pytest.mark.parametrize("value", [None, True, -1, 1.5])
def test_alias_count_leaf_must_be_a_nonnegative_integer(subject, value):
    _, entry, _ = subject
    entry["result"]["analysis"]["kpis"]["month_count"] = value
    reseal(entry)
    assert checked_unit(entry, "$.analysis.kpis.month_count") is None


@pytest.mark.parametrize("seal", ["code_sha256", "result_sha256", "entry_sha256"])
def test_alias_does_not_read_tampered_source_declarations(subject, seal):
    _, entry, _ = subject
    entry[seal] = "0" * 64
    assert checked_unit(entry, "$.analysis.kpis.cohort_products_without_events") is None
