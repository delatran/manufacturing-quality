"""Check consequential boundaries in the independent numerical verifier."""

from __future__ import annotations

import importlib.util
import json
import random
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rebuild_independent_check", ROOT / "scripts/rebuild_independent_check.py"
)
assert SPEC is not None and SPEC.loader is not None
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def raw_event(event_id: int, **overrides):
    return {
        "defect_id": event_id,
        "product_id": 1,
        "defect_type": "Functional",
        "defect_date": "1/11/2024",
        "defect_location": "Component",
        "severity": "Critical",
        "inspection_method": "Manual Testing",
        "repair_cost": Decimal("812.51"),
        **overrides,
    }


def test_duplicate_resolution_is_invariant_to_source_order():
    rows = [
        raw_event(1),
        raw_event(1),
        raw_event(2, severity="critical"),
        raw_event(2),
        raw_event(3, repair_cost=None),
        raw_event(3),
        raw_event(4, product_id=104),
        raw_event(4, product_id=86),
    ]
    projections = []
    for seed in range(12):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        events, quality = CHECK.independent_clean(shuffled)
        projections.append(CHECK.record_projection(events))
        assert quality["duplicate_group_subtypes"] == {
            "exact": 1,
            "representation_equivalent": 1,
            "complementary_missing_cost": 1,
            "contradictory_quarantined": 1,
        }
        assert [event["defect_id"] for event in events] == [1, 2, 3]
        assert events[2]["repair_cost"] == Decimal("812.51")
    assert all(projection == projections[0] for projection in projections)


def test_complementary_merge_requires_every_other_attribute_to_agree():
    events, quality = CHECK.independent_clean(
        [raw_event(1, repair_cost=None), raw_event(1, product_id=2)]
    )
    assert events == []
    assert quality["duplicate_group_subtypes"] == {"contradictory_quarantined": 1}


def test_date_scenarios_keep_unambiguous_dates_and_actual_years():
    assert CHECK.parsed_date("1/11/2024") == (date(2024, 1, 11), "ambiguous_different_parts")
    assert CHECK.parsed_date("1/11/2024", "ambiguous_dm")[0] == date(2024, 11, 1)
    assert CHECK.parsed_date("1/1/2024", "ambiguous_dm") == (
        date(2024, 1, 1),
        "ambiguous_equal_parts",
    )
    assert CHECK.parsed_date("27/5/2024", "ambiguous_dm")[0] == date(2024, 5, 27)
    assert CHECK.parsed_date("2024-03-20", "ambiguous_dm")[0] == date(2024, 3, 20)
    assert CHECK.parsed_date("6/20/2026")[0] == date(2026, 6, 20)
    assert CHECK.parsed_date("6/20/2026", "year_2024")[0] == date(2024, 6, 20)
    with pytest.raises(ValueError):
        CHECK.parsed_date("2/30/2024")


def test_event_filters_keep_zero_event_master_products_in_denominator():
    products = {
        1: {
            "product_id": 1,
            "production_line": "Line-1",
            "category": "Category 1",
            "monthly_output": 100,
        },
        2: {
            "product_id": 2,
            "production_line": "Line-1",
            "category": "Category 1",
            "monthly_output": 900,
        },
    }
    events, _ = CHECK.independent_clean(
        [raw_event(1), raw_event(2, product_id=2, severity="Minor"), raw_event(3, product_id=101)]
    )
    period = {"start_month": "2024-01", "end_month": "2024-01"}
    whole = CHECK.scope_metrics(events, products, period)
    critical = CHECK.scope_metrics(events, products, {**period, "severity": "Critical"})
    orphan = CHECK.scope_metrics(events, products, {**period, "product_id": "101"})
    empty = CHECK.scope_metrics(
        events, products, {**period, "product_id": "2", "severity": "Critical"}
    )
    assert whole["event_count"] == 3 and whole["eligible_event_count"] == 2
    assert critical["event_count"] == 2 and critical["eligible_event_count"] == 1
    assert whole["reference_output"] == critical["reference_output"] == 1000
    assert critical["events_per_1000_output"] == 1
    assert critical["zero_event_product_ids"] == [2]
    assert orphan["event_count"] == 1 and orphan["eligible_event_count"] == 0
    assert orphan["reference_output"] == 0 and orphan["events_per_1000_output"] is None
    assert empty["event_count"] == 0 and empty["cohort_product_count"] == 1
    assert empty["reference_output"] == 900
    empty_api_projection = CHECK.expected_kpis(empty)
    assert empty_api_projection["known_repair_cost"] == 0
    assert empty_api_projection["cost_available"] is False
    assert empty_api_projection["average_known_repair_cost"] is None
    assert CHECK.expected_kpis(orphan)["reference_output"] is None


def test_financial_scenario_preserves_events_and_unknown_costs():
    rows = [
        raw_event(1, repair_cost=10),
        raw_event(2, repair_cost=None),
        raw_event(3, repair_cost=-100),
        raw_event(4, repair_cost=0),
        raw_event(5, repair_cost=20),
    ]
    events, _ = CHECK.independent_clean(rows)
    canonical = CHECK.cost_stats(events)
    scenario = CHECK.cost_stats(events, "exclude_cost_10")
    assert canonical["known_total"] == 30 and canonical["valid_count"] == 3
    assert canonical["unknown_count"] == 2
    assert scenario["known_total"] == 20 and scenario["valid_count"] == 2
    assert scenario["unknown_count"] == 3 and scenario["scenario_excluded_count"] == 1
    assert len(events) == 5


def test_frozen_live_source_oracle_reconciles_populations_and_scopes():
    frozen = json.loads(
        (ROOT / "evals/rebuild_independent_expected.json").read_text(encoding="utf-8")
    )
    data = frozen["independent"]
    canonical = data["cases"]["canonical"]
    duplicate_quality = data["duplicate_quality"]
    assert frozen["sources"]["commit_status"] == "Passed"
    assert frozen["sources"]["defects_match_supplied_commit"] is True
    assert frozen["sources"]["master_matches_supplied_commit"] is True
    assert (
        duplicate_quality["raw_row_count"]
        - duplicate_quality["raw_rows_not_represented_as_separate_events"]
        == duplicate_quality["canonical_events_all_years"]
    )
    assert (
        duplicate_quality["canonical_events_all_years"]
        - len(data["data_quality"]["raw_outside_2024_ids"])
        == canonical["event_count"]
    )
    assert (
        canonical["event_count"]
        == canonical["eligible_event_count"] + canonical["orphan_event_count"]
    )
    assert (
        canonical["costs"]["valid_count"] + canonical["costs"]["unknown_count"]
        == canonical["event_count"]
    )
    assert sum(row["event_count"] for row in canonical["monthly"]) == canonical["event_count"]
    assert (
        sum(row["event_count"] for row in canonical["dimension_groups"]["production_line"])
        == canonical["eligible_event_count"]
    )
    assert (
        sum(row["change"] for row in data["january_to_june"]["product_contributions"])
        == data["january_to_june"]["absolute_change"]
    )
    assert data["cases"]["critical"]["reference_output"] == canonical["reference_output"]
    assert (
        data["cases"]["january_critical"]["reference_output"]
        == data["cases"]["january"]["reference_output"]
    )
    assert len(data["cases"]["january"]["zero_event_product_ids"]) > 0
    assert len(data["cases"]["january_critical"]["zero_event_product_ids"]) > len(
        data["cases"]["january"]["zero_event_product_ids"]
    )
    assert data["cases"]["exclude_cost_10"]["event_ids"] == canonical["event_ids"]
    assert (
        data["data_quality"]["raw_ambiguous_total"]
        == data["data_quality"]["raw_dates"]["ambiguous_equal_parts"]
        + data["data_quality"]["raw_dates"]["ambiguous_different_parts"]
    )


def test_verifier_has_no_application_import_or_local_defect_json_source():
    source = (ROOT / "scripts/rebuild_independent_check.py").read_text(encoding="utf-8")
    assert "from analyst" not in source and "import analyst" not in source
    assert "oracle_builder" not in source and "preparation" not in source
    assert 'defects = json.loads(downloads["defects"][0]' in source


def test_saved_monthly_populations_match_frozen_independent_values():
    frozen = json.loads(
        (ROOT / "evals/rebuild_independent_expected.json").read_text(encoding="utf-8")
    )["independent"]
    report = json.loads((ROOT / "reports/canonical_snapshot.json").read_text(encoding="utf-8"))
    rows = report["analysis"]["monthly"]
    assert [row["month"] for row in rows] == [f"2024-{month:02d}" for month in range(1, 7)]
    for row, expected in zip(rows, frozen["cases"]["canonical"]["monthly"], strict=True):
        assert row["month"] == expected["month"]
        assert [
            row["event_count"],
            row["eligible_event_count"],
            row["critical_count"],
            row["cohort_products_without_events"],
        ] == [
            expected["event_count"],
            expected["eligible_event_count"],
            expected["critical_count"],
            len(expected["zero_event_product_ids"]),
        ]
