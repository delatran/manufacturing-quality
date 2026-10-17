"""Half-period cost changes distinguish missing observations from valid zero cost."""

from datetime import date

import pytest
from analyst.case_study.analytics import kpis, period_halves
from analyst.case_study.models import Event


@pytest.mark.parametrize(
    ("first_cost", "second_cost", "expected"),
    [
        (None, 10, None),
        (10, None, None),
        (None, None, None),
        (10, 15, 50.0),
        (10, 0, -100.0),
        (0, 10, None),
        (0, 0, None),
    ],
)
def test_half_period_cost_change_requires_observed_cost_in_both_halves(
    first_cost, second_cost, expected
):
    months = []
    for month, cost in enumerate((first_cost, second_cost), start=1):
        event = Event(
            defect_id=month,
            product_id=1,
            defect_type="Functional",
            defect_date=date(2024, month, 15),
            defect_location="Component",
            severity="Critical",
            inspection_method="Visual Inspection",
            repair_cost=cost,
            cost_status="missing" if cost is None else "known",
            source_row=month,
        )
        months.append({"month": f"2024-{month:02d}", **kpis([event], (), 1)})

    result = period_halves(months)

    assert result["known_repair_cost_change_pct"] == expected
    assert result["first"]["cost_known_count"] == (first_cost is not None)
    assert result["second"]["cost_known_count"] == (second_cost is not None)
    assert result["event_count_change_pct"] == 0
    assert result["critical_count_change_pct"] == 0
