"""Promised analysis sections preserve selected events and canonical audit scope."""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal

import pytest
from analyst.case_study.analytics import Scope, analysis_report
from analyst.case_study.preparation import build_snapshot

MASTER = b"product_id,product_name,category,production_line,monthly_output\n1,A,Category 1,Line-1,100\n2,B,Category 2,Line-2,200\n3,C,Category 1,Line-1,300\n"


def event(defect_id, **fields):
    return {
        "defect_id": defect_id,
        "product_id": 1,
        "defect_type": "Functional",
        "defect_date": "2024-01-20",
        "defect_location": "Component",
        "severity": "Critical",
        "inspection_method": "Automated Testing",
        "repair_cost": 100,
        **fields,
    }


def prepared(rows):
    return build_snapshot(
        rows,
        {
            "source_sha256": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
            "source_url": "https://synthetic.invalid/sections",
            "fetched_at": "2026-10-17T00:00:00Z",
        },
        MASTER,
    )


@pytest.fixture
def data():
    first = event(1, defect_date="1/2/2024")
    rows = [
        first,
        event(2, inspection_method="Manual Testing", severity="Moderate", repair_cost=10),
        event(
            3,
            inspection_method="Visual Inspection",
            severity="Minor",
            repair_cost=None,
            defect_date="2024-06-20",
        ),
        event(4, severity="Minor", repair_cost=-5, defect_date="2024-06-20"),
        event(5, inspection_method="Manual Testing", repair_cost=20, defect_date="2026-06-20"),
        copy.deepcopy(first),
        event(6, product_id=2, inspection_method="Visual Inspection", repair_cost=30),
        event(6, product_id=2, inspection_method="Visual Inspection", repair_cost=None),
        event(7, repair_cost=40),
        event(7, product_id=2, repair_cost=40),
    ]
    return prepared(rows)


def test_method_summary_uses_recorded_events_and_available_costs_without_inspection_exposure(data):
    report = analysis_report(data, Scope())
    section = report["inspection_methods"]
    rows = {row["inspection_method"]: row for row in section["rows"]}
    assert section["event_count"] == sum(row["event_count"] for row in rows.values()) == 5
    assert {name: row["event_count"] for name, row in rows.items()} == {
        "Automated Testing": 2,
        "Manual Testing": 1,
        "Visual Inspection": 2,
    }
    assert sum(row["event_share_pct"] for row in rows.values()) == pytest.approx(100)
    assert rows["Automated Testing"]["critical_count"] == 1
    assert rows["Automated Testing"]["critical_share_pct"] == 50
    assert rows["Automated Testing"]["cost_coverage_pct"] == 50
    assert rows["Visual Inspection"]["severity_counts"] == {
        "Minor": 1,
        "Moderate": 0,
        "Critical": 1,
        "Unknown": 0,
    }
    assert sum(Decimal(str(row["known_repair_cost"])) for row in rows.values()) == Decimal("140")
    assert section["financial_summary"]["cost_known_count"] == 3
    assert section["financial_summary"]["cost_unknown_count"] == 2
    assert "effectiveness_rank" in section["unavailable_metrics"]
    requirements = section["measurement_requirements"]
    assert "ground-truth-positive" in requirements["sensitivity"]
    assert "ground-truth-positive" in requirements["false_negative_rate"]
    assert "ground-truth-negative" in requirements["specificity"]
    assert "ground-truth-negative" in requirements["false_positive_rate"]
    assert all(
        "reference_output" not in row
        and "reference_intensity_per_1000" not in row
        and "rank" not in row
        for row in rows.values()
    )


@pytest.mark.parametrize("scenario", ["", "exclude_cost_10", "ambiguous_dm", "year_2024"])
def test_method_section_tracks_filtered_severity_and_selected_scenario(data, scenario):
    filters = Scope(severity="Critical", scenario=scenario)
    report = analysis_report(data, filters)
    section = report["inspection_methods"]
    assert section["scope"] == report["scope"]
    assert section["event_count"] == report["kpis"]["event_count"]
    assert section["financial_summary"]["known_repair_cost"] == report["kpis"]["known_repair_cost"]
    assert sum(row["critical_count"] for row in section["rows"]) == report["kpis"]["critical_count"]
    assert all(row["severity_counts"]["Critical"] == row["event_count"] for row in section["rows"])
    assert all(row["critical_share_pct"] == 100 for row in section["rows"])


def test_financial_withholding_changes_method_cost_coverage_without_removing_events(data):
    canonical = analysis_report(data, Scope())["inspection_methods"]
    selected = analysis_report(data, Scope(scenario="exclude_cost_10"))["inspection_methods"]
    assert selected["event_count"] == canonical["event_count"]
    method = next(row for row in selected["rows"] if row["inspection_method"] == "Manual Testing")
    assert method["event_count"] == 1 and method["cost_known_count"] == 0
    assert method["known_repair_cost"] == 0 and method["cost_available"] is False
    assert method["cost_coverage_pct"] == 0


def test_cleaning_impact_compares_the_same_canonical_period_and_valid_cost_population(data):
    report = analysis_report(data, Scope())
    impact = report["cleaning_impact"]
    assert impact["scope"] == report["scope"]
    assert impact["quality_scope"] == {
        "kind": "canonical_full_snapshot",
        "filters_applied": False,
        "scenario_applied": False,
    }
    assert impact["raw"]["row_count"] == 9
    assert impact["raw"]["cost_known_count"] == 6 and impact["raw"]["cost_unknown_count"] == 3
    assert impact["raw"]["cost_status_counts"] == {"known": 6, "missing": 2, "negative": 1}
    assert impact["raw"]["known_repair_cost"] == 320
    assert impact["raw_numeric_cost_including_negatives"] == 315
    assert impact["retained"]["event_count"] == report["kpis"]["event_count"] == 5
    assert impact["retained"]["known_repair_cost"] == report["kpis"]["known_repair_cost"] == 140
    assert (
        impact["retained"]["cost_known_count"] == 3
        and impact["retained"]["cost_unknown_count"] == 2
    )
    assert impact["row_count_difference"] == -4 and impact["known_cost_difference"] == -180
    assert impact["full_source_reconciliation"]["raw_rows"] == 10
    assert impact["full_source_reconciliation"]["out_of_scope_events"] == 1
    assert impact["metadata"]["scenario"] == ""
    assert "realized savings" in impact["interpretation"]


@pytest.mark.parametrize(
    "scope",
    [
        Scope(severity="Minor"),
        Scope(production_line="Line-2"),
        Scope(start_month="2024-06", end_month="2024-06"),
        Scope(scenario="exclude_cost_10"),
        Scope(scenario="ambiguous_dm"),
        Scope(scenario="year_2024"),
        Scope(product_id="3"),
    ],
)
def test_global_cleaning_impact_is_not_relabelled_as_selected_scope(data, scope):
    canonical = analysis_report(data, Scope())["cleaning_impact"]
    selected = analysis_report(data, scope)
    assert selected["cleaning_impact"] == canonical
    assert selected["scope"] != canonical["scope"]


@pytest.mark.parametrize(
    "scope", [Scope(product_id="3"), Scope(production_line="Line-1", category="Category 2")]
)
def test_empty_method_sections_do_not_impute_cost_or_drop_zero_event_products(data, scope):
    report = analysis_report(data, scope)
    section = report["inspection_methods"]
    assert section["event_count"] == 0 and section["rows"] == []
    assert section["financial_summary"]["cost_available"] is False
    assert section["financial_summary"]["cost_coverage_pct"] is None
    assert report["cleaning_impact"]["retained"]["event_count"] == 5
    if scope.product_id:
        assert report["kpis"]["cohort_product_count"] == 1
        assert report["kpis"]["cohort_products_without_events"] == 1
        assert report["kpis"]["reference_output"] == 1800


def test_unrecognized_method_and_invalid_cost_remain_visible_with_unknowns():
    report = analysis_report(
        prepared([event(1, inspection_method="Unrecognized method", repair_cost="bad")]), Scope()
    )
    row = report["inspection_methods"]["rows"][0]
    assert row["inspection_method"] == "Unknown" and row["event_count"] == 1
    assert row["cost_known_count"] == 0 and row["cost_unknown_count"] == 1
    assert report["cleaning_impact"]["raw"]["cost_status_counts"] == {"invalid": 1}
