"""Period comparisons preserve retained filter values and source cost precision."""

import math

import pytest
from analyst.case_study import analytics
from analyst.case_study.analytics import Scope, analysis_report, compare_periods
from test_dashboard import Loader, event, make_snapshot, service

JANUARY = Scope(start_month="2024-01", end_month="2024-01")
JUNE = Scope(start_month="2024-06", end_month="2024-06")


@pytest.mark.parametrize(
    "rows",
    [
        [event(1, repair_cost=0.005), event(2, product_id=3, repair_cost=0.005)],
        [event(1, repair_cost=0.005), event(2, repair_cost=0.005)],
        [
            event(1, repair_cost=0.005),
            event(2, product_id=3, repair_cost=0.015),
            event(3, repair_cost=0.015, defect_date="2024-06-15"),
            event(4, product_id=3, repair_cost=0.005, defect_date="2024-06-15"),
        ],
        [
            event(1, repair_cost=0.123456789),
            event(2, product_id=3, repair_cost=None),
            event(3, repair_cost=0.0, defect_date="2024-06-15"),
        ],
        [
            event(1, repair_cost=1e100),
            event(2, product_id=3, repair_cost=1e-100),
            event(3, repair_cost=1e100, defect_date="2024-06-15"),
            event(4, product_id=3, repair_cost=0.005, defect_date="2024-06-15"),
        ],
    ],
)
def test_fractional_costs_remain_available_and_reconcile_before_rounding(rows):
    snapshot = make_snapshot(rows)
    source_costs = [row["repair_cost"] for row in rows]
    assert [item.repair_cost for item in snapshot.events] == source_costs
    report = analysis_report(snapshot, Scope())
    assert report["kpis"]["cost_known_count"] == sum(cost is not None for cost in source_costs)
    for dimension in analytics.COMPARISON_DIMENSIONS:
        result = compare_periods(snapshot, JANUARY, JUNE, dimension)
        assert result["reconciliation"]["known_cost_change_reconciles"] is True
        assert result["reconciliation"]["event_change_reconciles"] is True
        displayed_sum = math.fsum(row["known_repair_cost_change"] for row in result["rows"])
        rounding = result["known_cost_reconciliation"]
        assert rounding["display_decimal_places"] == 2
        assert rounding["displayed_change_residual"] == round(
            displayed_sum - result["known_repair_cost_change"], 2
        )
        assert "unrounded" in rounding["basis"]
        assert "rounding" in rounding["interpretation"]
    assert [item.repair_cost for item in snapshot.events] == source_costs


def test_rounding_difference_is_explicit_without_allocating_a_fabricated_penny():
    snapshot = make_snapshot(
        [event(1, repair_cost=0.005), event(2, product_id=3, repair_cost=0.005)]
    )
    result = compare_periods(snapshot, JANUARY, JUNE, "production_line")
    assert result["baseline"]["kpis"]["known_repair_cost"] == 0.01
    assert result["known_repair_cost_change"] == -0.01
    assert [row["known_repair_cost_change"] for row in result["rows"]] == [-0.01, -0.01]
    assert result["known_cost_reconciliation"]["displayed_change_residual"] == -0.01


def test_offsetting_cost_changes_and_invalid_negative_cost_remain_distinct():
    snapshot = make_snapshot(
        [
            event(1, repair_cost=0.004),
            event(2, product_id=3, repair_cost=0.016),
            event(3, repair_cost=0.016, defect_date="2024-06-15"),
            event(4, product_id=3, repair_cost=0.004, defect_date="2024-06-15"),
            event(5, product_id=3, repair_cost=-0.005),
        ]
    )
    invalid_cost = next(row for row in snapshot.events if row.defect_id == 5)
    assert invalid_cost.repair_cost is None
    assert invalid_cost.cost_status == "negative"
    result = compare_periods(snapshot, JANUARY, JUNE, "production_line")
    assert result["baseline"]["kpis"]["cost_known_count"] == 2
    assert result["baseline"]["kpis"]["cost_unknown_count"] == 1
    assert result["known_repair_cost_change"] == 0
    assert {row["known_repair_cost_change"] for row in result["rows"]} == {-0.02, 0.02}
    assert result["reconciliation"]["known_cost_change_reconciles"] is True
    assert result["known_cost_reconciliation"]["displayed_change_residual"] == 0


@pytest.mark.parametrize("corrupt_group", [False, True])
def test_wrong_cost_output_still_fails_reconciliation(monkeypatch, corrupt_group):
    snapshot = make_snapshot([event(1, repair_cost=20), event(2, product_id=3, repair_cost=30)])
    calculate = analytics.kpis

    def altered_values(events, products, month_count):
        values = calculate(events, products, month_count)
        if len(events) == (1 if corrupt_group else 2):
            values["known_repair_cost"] += 0.01
        return values

    monkeypatch.setattr(analytics, "kpis", altered_values)
    with pytest.raises(AssertionError, match="Comparison contribution reconciliation failed"):
        compare_periods(snapshot, JANUARY, JUNE, "production_line")


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline_severity", [None, "Minor", ""])
async def test_period_comparison_preserves_only_exact_inherited_missing_filters(
    tmp_path, baseline_severity
):
    before = make_snapshot([event(1, severity="Critical"), event(2, severity="Minor")])
    after = make_snapshot([event(2, severity="Minor")], fetched_at="2026-01-02T00:00:00+00:00")
    subject = service(tmp_path, Loader(before, after))
    original = await subject.get_context({"severity": "Critical"})
    updated = await subject.refresh_for_context(original["context"]["context_id"])
    context_id = updated["context"]["context_id"]
    baseline = {"start_month": "2024-01", "end_month": "2024-01"}
    if baseline_severity is not None:
        baseline["severity"] = baseline_severity
    compared = await subject.compare_periods(
        context_id, baseline, {"start_month": "2024-06", "end_month": "2024-06"}
    )
    assert compared["comparison"]["context"]["filters"]["severity"] == "Critical"
    assert compared["comparison"]["evidence"]["scope_validation"] == {"severity": "Critical"}
    assert compared["comparison"]["analysis"]["kpis"]["event_count"] == 0
    assert compared["baseline"]["analysis"]["kpis"]["event_count"] == (
        0 if baseline_severity is None else 1
    )
    assert compared["baseline"]["evidence"]["scope_validation"] == (
        {"severity": "Critical"} if baseline_severity is None else {}
    )
    assert compared["decomposition"]["reconciliation"]["event_change_reconciles"]
    assert await subject.context_by_id(context_id) == updated
    for side in ("baseline", "comparison"):
        arguments = {"baseline_filters": {}, "comparison_filters": {}}
        arguments[side + "_filters"] = {"severity": "New unknown value"}
        with pytest.raises(ValueError, match="Unknown severity"):
            await subject.compare_periods(context_id, **arguments)
    with pytest.raises(ValueError, match="Unknown severity"):
        await subject.get_context({"severity": "Critical"})
