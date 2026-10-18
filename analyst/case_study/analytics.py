"""Pure case-study metrics, analytical scopes and ranks over a complete snapshot.

The event grain is a logged defect, not a distinct defective unit. Static
monthly_output is reference exposure, never verified actual production.
"""

import math
from calendar import monthrange
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from fractions import Fraction
from typing import Any

import numpy as np
from scipy.stats import chi2_contingency

from .config import MONTHS, POLICY_VERSION, SEVERITIES
from .contracts import FinancialMetrics, Metrics
from .models import Event, Product, Snapshot
from .scenarios import SCENARIOS, scenario_snapshot, validate_scenario

REFERENCE_NOTE = (
    "Reference-output intensity = linked logged defect events / "
    "(sum of monthly_output for all eligible master products × selected full months) × 1,000. "
    "Static reference output is not verified actual production; this is not a defect probability. "
    "Event filters change the numerator only."
)
LIMITATIONS = [
    "The supplied records are a historical January-June 2024 case study; refresh time is not event freshness.",
    "One row represents a logged defect event; multiple events may concern one unit, and no unit identifier is available.",
    "Actual monthly/daily production, inspected units, inspection coverage and operating days are unavailable.",
    "Absent calendar dates mean zero logged events, not verified zero defects or zero production; calendar coverage is described for the current snapshot below.",
    "Ambiguous slash dates use M/D/YYYY inferred from the predominant unambiguous source rows. Daily candidate dates depend on this convention; confirm ambiguous dates with the source owner before operational prioritization.",
    "Unknown repair costs are excluded from financial sums and reported through explicit coverage; currency name is unspecified.",
    "Product-to-line mapping and reference output are static; historical reassignment and production mix cannot be verified.",
]
DEFINITIONS = {
    "event_count": "Unique retained logged defect events in the selected full-month scope, including unmatched products at plant level.",
    "products_with_events": "Distinct product identifiers in the selected logged events, including identifiers unmatched to the product master. This is not the count of eligible master products with events.",
    "cohort_product_count": "Eligible products in the selected product-master cohort, including products with no selected events. Event filters do not shrink this reference cohort.",
    "linked_event_count": "Selected logged events linked to the product master. This counts events, not distinct products.",
    "unmatched_event_count": "Selected logged events without a product-master match. Multiple events can share an unmatched product identifier; this event count cannot be subtracted from a distinct-product count to obtain eligible products with events.",
    "known_repair_cost": "Sum of observed nonnegative repair costs only, in unspecified local currency; not a full cost estimate.",
    "cost_coverage_pct": "Events with known valid repair cost / all selected events × 100.",
    "reference_intensity_per_1000": REFERENCE_NOTE,
    "eligible_event_count": "Selected events linked to a product in the complete master cohort; this is the reference-intensity numerator.",
    "output_coverage_pct": "Master products with a valid positive monthly_output / all master products in the selected cohort × 100. Partial output is never used as the total denominator.",
    "cost_distribution": "Distribution of observed valid repair costs among selected events with available costs. Unknown or scenario-withheld costs stay outside the histogram and quantiles; coverage uses all selected events.",
    "inspection_methods": "Recorded defect-event composition by inspection method, with within-method severity and observed-cost coverage. No inspected-unit exposure or ground truth is available to estimate effectiveness.",
    "cleaning_impact": "Raw received rows compared with retained unique events for the complete canonical January-June 2024 scope. Dashboard filters and scenarios do not change this comparison. Cleaning differences do not measure manufacturing improvement.",
    "decomposition": "Group contributions to the comparison-period minus baseline-period difference. Contributions describe accounting changes, not causes. Each group partitions the selected events once.",
    "critical_share_pct": "Critical events / all selected events × 100; Unknown severity remains in the denominator.",
    "anomalies": "Exploratory daily logged-count candidates exceeding a fixed January mean + 3√mean reference. No actual daily exposure or reporting completeness is available; this is not validated statistical process control.",
    "category_summary": "Reference-output intensity by product category over its complete master cohort, with linked events per cohort product as a check that does not depend on the static output denominator.",
    "shared_category_controls": "For a category carried by several lines: its intensity against the rest of the same line, and its observed cost per known-cost event against the same category on other lines. Descriptive mix controls, not causal tests.",
    "line_cost_comparison": "Average observed repair cost per known-cost linked event on each line against all other lines combined, with each line's share of linked known cost and of linked known-cost events.",
    "period_halves": "First half against second half of an even number of selected full months: logged events, Critical events and observed repair cost.",
}
METRIC_METADATA = {
    "reference_intensity_per_1000": {
        "scale": 1000,
        "unit": "logged events per reference output units",
        "denominator_kind": "static reference output; not verified actual production",
    },
    "critical_share_pct": {"scale": 100, "unit": "percent of logged events"},
    "cost_coverage_pct": {
        "scale": 100,
        "unit": "percent of logged events with known valid repair cost",
    },
}


_MONTHS_SET = frozenset(MONTHS)


def _year_month(day: date) -> str:
    """Equivalent to day.strftime("%Y-%m") for the 4-digit years used here, ~10x faster."""
    return f"{day.year:04d}-{day.month:02d}"


def calendar_coverage(events: list[Event], filters: "Scope") -> dict[str, Any]:
    """Describe actual retained dates under the exact selected analytical scope."""
    days_by_month: dict[str, set[date]] = defaultdict(set)
    for event in events:
        days_by_month[_year_month(event.defect_date)].add(event.defect_date)
    monthly: list[dict[str, Any]] = []
    for month in filters.months():
        observed = sorted(days_by_month.get(month, ()))
        year, month_number = map(int, month.split("-"))
        calendar_days = monthrange(year, month_number)[1]
        last = observed[-1] if observed else None
        monthly.append(
            {
                "month": month,
                "calendar_days": calendar_days,
                "days_with_events": len(observed),
                "days_without_events": calendar_days - len(observed),
                "last_observed_date": last.isoformat() if last else None,
                "last_observed_day": last.day if last else None,
                "calendar_days_after_last_observed": calendar_days - last.day if last else None,
            }
        )
    return {
        "scope": asdict(filters),
        "scope_kind": "selected_scope",
        "calendar_days": sum(row["calendar_days"] for row in monthly),
        "days_with_events": sum(row["days_with_events"] for row in monthly),
        "days_without_events": sum(row["days_without_events"] for row in monthly),
        "selected_month_count": len(monthly),
        "months_with_events": sum(row["last_observed_day"] is not None for row in monthly),
        "max_observed_day_of_month": max(
            (row["last_observed_day"] for row in monthly if row["last_observed_day"] is not None),
            default=None,
        ),
        "coverage_verified": False,
        "monthly": monthly,
        "interpretation": "Observed dates describe retained logged events under the selected filters. Missing dates do not establish zero defects, zero production, or reporting completeness.",
    }


def calendar_coverage_note(coverage: dict[str, Any]) -> str:
    """Bind narrative calendar quantities to the same structured selected result."""
    last_day = coverage["max_observed_day_of_month"]
    if (
        coverage["months_with_events"] == coverage["selected_month_count"]
        and last_day is not None
        and last_day <= 28
    ):
        return f"In the selected scope, no reporting month has logged events after day {last_day}. Unverified month-end reporting can distort trends and candidate screening."
    return (
        f"In the selected scope, {coverage['days_without_events']} of {coverage['calendar_days']} calendar dates have no logged events. "
        "Operational/reporting completeness remains unverified."
    )


@dataclass(frozen=True)
class Scope:
    start_month: str = "2024-01"
    end_month: str = "2024-06"
    production_line: str = ""
    category: str = ""
    severity: str = ""
    defect_type: str = ""
    defect_location: str = ""
    inspection_method: str = ""
    product_id: str = ""
    scenario: str = ""

    def months(self) -> list[str]:
        return [month for month in MONTHS if self.start_month <= month <= self.end_month]


def ratio(numerator: float, denominator: float, scale: float = 1) -> float | None:
    return numerator / denominator * scale if denominator > 0 else None


def options(snapshot: Snapshot) -> dict[str, list[str]]:
    return {
        "months": list(MONTHS),
        "production_lines": sorted({product.production_line for product in snapshot.products}),
        "categories": sorted({product.category for product in snapshot.products}),
        "severities": [
            level
            for level in SEVERITIES
            if any(event.severity == level for event in snapshot.events)
        ],
        "defect_types": sorted({event.defect_type for event in snapshot.events}),
        "defect_locations": sorted({event.defect_location for event in snapshot.events}),
        "inspection_methods": sorted({event.inspection_method for event in snapshot.events}),
        "product_ids": sorted(
            {str(product.product_id) for product in snapshot.products}
            | {str(event.product_id) for event in snapshot.events},
            key=int,
        ),
        "scenarios": list(SCENARIOS),
    }


EVENT_FILTER_OPTIONS = {
    "severity": "severities",
    "defect_type": "defect_types",
    "defect_location": "defect_locations",
    "inspection_method": "inspection_methods",
}


def missing_event_filter_values(snapshot: Snapshot, filters: Scope) -> dict[str, str]:
    """Describe selected event values absent from this snapshot's vocabulary."""
    choices = options(snapshot)
    return {
        field: getattr(filters, field)
        for field, option_key in EVENT_FILTER_OPTIONS.items()
        if getattr(filters, field) and getattr(filters, field) not in choices[option_key]
    }


def validate_filters(
    snapshot: Snapshot,
    filters: Scope,
    *,
    permitted_missing_event_filters: dict[str, str] | None = None,
) -> None:
    if any(not isinstance(value, str) for value in asdict(filters).values()):
        raise ValueError("Every scope value must be a string")
    validate_scenario(filters.scenario)
    if filters.start_month not in MONTHS or filters.end_month not in MONTHS:
        raise ValueError("Month bounds must be full months between 2024-01 and 2024-06")
    if filters.start_month > filters.end_month:
        raise ValueError("start_month cannot be later than end_month")
    permitted = (
        permitted_missing_event_filters if permitted_missing_event_filters is not None else {}
    )
    if (
        not isinstance(permitted, dict)
        or set(permitted) - set(EVENT_FILTER_OPTIONS)
        or any(value != getattr(filters, field) for field, value in permitted.items())
    ):
        raise ValueError("Inherited event-filter values must match the exact saved scope")
    choices = options(snapshot)
    mapping = {
        "production_line": "production_lines",
        "category": "categories",
        **EVENT_FILTER_OPTIONS,
    }
    for field, option_key in mapping.items():
        selected = getattr(filters, field)
        if selected and selected not in choices[option_key] and permitted.get(field) != selected:
            raise ValueError(f"Unknown {field}: {selected}")
    if filters.product_id and filters.product_id not in choices["product_ids"]:
        raise ValueError(f"Unknown product_id: {filters.product_id}")


def filter_products(snapshot: Snapshot, filters: Scope) -> tuple[Product, ...]:
    """Master cohort includes zero-event products and ignores event attributes."""
    return tuple(
        product
        for product in snapshot.products
        if (not filters.production_line or product.production_line == filters.production_line)
        and (not filters.category or product.category == filters.category)
        and (not filters.product_id or str(product.product_id) == filters.product_id)
    )


def filter_events(snapshot: Snapshot, filters: Scope, *, ignore_dates: bool = False) -> list[Event]:
    snapshot = scenario_snapshot(snapshot, filters.scenario)
    fields = (
        "production_line",
        "category",
        "severity",
        "defect_type",
        "defect_location",
        "inspection_method",
    )
    # Only the selected dimensions are compared; semantics match the full check.
    active = [(field, getattr(filters, field)) for field in fields if getattr(filters, field)]
    product_id = filters.product_id
    start, end = filters.start_month, filters.end_month
    result = []
    for event in snapshot.events:
        month = _year_month(event.defect_date)
        if month not in _MONTHS_SET:
            continue
        if not ignore_dates and not (start <= month <= end):
            continue
        if product_id and str(event.product_id) != product_id:
            continue
        if active and not all(getattr(event, field) == value for field, value in active):
            continue
        result.append(event)
    return result


def _known_cost(events: Iterable[Event]) -> float:
    return round(
        math.fsum(event.repair_cost for event in events if event.repair_cost is not None), 2
    )


def _financial(events: list[Event]) -> dict[str, Any]:
    known_count = sum(event.repair_cost is not None for event in events)
    total = _known_cost(events)
    values = {
        "known_repair_cost": total,
        "cost_known_count": known_count,
        "cost_unknown_count": len(events) - known_count,
        "cost_coverage_pct": ratio(known_count, len(events), 100),
        "average_known_repair_cost": ratio(total, known_count),
        "cost_available": known_count > 0,
        "cost_status": "empty"
        if not events
        else "unavailable"
        if not known_count
        else "complete"
        if known_count == len(events)
        else "partial",
    }
    return FinancialMetrics.model_validate(values).model_dump()


def _output(products: Iterable[Product], month_count: int) -> int | None:
    cohort = list(products)
    if not cohort or month_count <= 0 or any(product.monthly_output is None for product in cohort):
        return None
    return (
        sum(product.monthly_output for product in cohort if product.monthly_output is not None)
        * month_count
    )


def _intensity(count: int, output: int | None) -> float | None:
    return ratio(count, output, 1000) if output is not None else None


def kpis(events: list[Event], products: tuple[Product, ...], month_count: int) -> dict[str, Any]:
    critical = sum(event.severity == "Critical" for event in events)
    linked = sum(event.matched_product for event in events)
    output = _output(products, month_count)
    product_ids = {product.product_id for product in products}
    eligible = sum(event.matched_product and event.product_id in product_ids for event in events)
    known_output_count = sum(product.monthly_output is not None for product in products)
    products_with_selected_events = {
        event.product_id for event in events if event.product_id in product_ids
    }
    intensity_status = (
        "no_eligible_products"
        if not products
        else "no_full_months"
        if month_count <= 0
        else "incomplete_output"
        if output is None
        else "available"
    )
    values = {
        "event_count": len(events),
        "linked_event_count": linked,
        "unmatched_event_count": len(events) - linked,
        "critical_count": critical,
        "critical_share_pct": ratio(critical, len(events), 100),
        **_financial(events),
        "eligible_event_count": eligible,
        "reference_output": output,
        "reference_intensity_per_1000": _intensity(eligible, output),
        "reference_intensity_status": intensity_status,
        "output_known_product_count": known_output_count,
        "output_unknown_product_count": len(products) - known_output_count,
        "output_coverage_pct": ratio(known_output_count, len(products), 100),
        "known_monthly_output_subtotal": sum(product.monthly_output or 0 for product in products),
        "cohort_products_with_events": len(products_with_selected_events),
        "cohort_products_without_events": len(products) - len(products_with_selected_events),
        "products_with_events": len({event.product_id for event in events}),
        "cohort_product_count": len(products),
        "month_count": month_count,
    }
    return Metrics.model_validate(values).model_dump()


def monthly(
    events: list[Event],
    products: tuple[Product, ...],
    filters: Scope,
    choices: dict[str, list[str]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        grouped[_year_month(event.defect_date)].append(event)
    result = []
    for month in filters.months():
        rows = grouped[month]
        values = kpis(rows, products, 1)
        severity_counter = Counter(event.severity for event in rows)
        inspection_counter = Counter(event.inspection_method for event in rows)
        result.append(
            {
                "month": month,
                "label": date.fromisoformat(month + "-01").strftime("%b %Y"),
                **values,
                "severity_counts": {
                    level: severity_counter[level] for level in choices["severities"]
                },
                "inspection_counts": {
                    method: inspection_counter[method] for method in choices["inspection_methods"]
                },
            }
        )
    return result


def trend(months: list[dict[str, Any]]) -> dict[str, Any]:
    first, last = months[0], months[-1]
    change = last["event_count"] - first["event_count"]
    change_pct = ratio(change, first["event_count"], 100)
    if len(months) == 1:
        interpretation = "One month is selected; there is no between-month trend comparison."
    else:
        direction = "increased" if change > 0 else "decreased" if change < 0 else "were unchanged"
        signal = (
            "This is an adverse signal: the logged defect workload is rising. "
            if change > 0
            else "This is a favourable signal: the logged defect workload is falling. "
            if change < 0
            else ""
        )
        interpretation = (
            f"Logged events {direction} from {first['event_count']} in {first['label']} to {last['event_count']} in {last['label']}. "
            f"{signal}Confirm actual output and inspection coverage before treating it as a defect-rate change."
        )
    return {
        "first_month": first["month"],
        "last_month": last["month"],
        "first_count": first["event_count"],
        "last_count": last["event_count"],
        "absolute_change": change,
        "change_pct": change_pct,
        "first_critical_share_pct": first["critical_share_pct"],
        "last_critical_share_pct": last["critical_share_pct"],
        "interpretation": interpretation,
    }


def period_halves(months: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the first and second half of an even number of selected full months."""
    if len(months) < 2 or len(months) % 2:
        return {
            "enabled": False,
            "reason": "Half-period comparison requires an even number of at least two selected months.",
        }
    middle = len(months) // 2
    halves = []
    for rows in (months[:middle], months[middle:]):
        cost = round(math.fsum(row["known_repair_cost"] for row in rows), 2)
        known = sum(row["cost_known_count"] for row in rows)
        halves.append(
            {
                "months": [row["month"] for row in rows],
                "event_count": sum(row["event_count"] for row in rows),
                "critical_count": sum(row["critical_count"] for row in rows),
                "known_repair_cost": cost,
                "cost_known_count": known,
                "average_known_repair_cost": ratio(cost, known),
            }
        )
    first, second = halves
    return {
        "enabled": True,
        "first": first,
        "second": second,
        "event_count_change": second["event_count"] - first["event_count"],
        "event_count_change_pct": ratio(
            second["event_count"] - first["event_count"], first["event_count"], 100
        ),
        "critical_count_change_pct": ratio(
            second["critical_count"] - first["critical_count"], first["critical_count"], 100
        ),
        "known_repair_cost_change_pct": (
            ratio(
                second["known_repair_cost"] - first["known_repair_cost"],
                first["known_repair_cost"],
                100,
            )
            if first["cost_known_count"] > 0 and second["cost_known_count"] > 0
            else None
        ),
    }


def lines(
    events: list[Event], products: tuple[Product, ...], month_count: int
) -> list[dict[str, Any]]:
    names = sorted(
        {product.production_line for product in products}
        | {event.production_line for event in events}
    )
    result = []
    for name in names:
        rows = [event for event in events if event.production_line == name]
        cohort = tuple(product for product in products if product.production_line == name)
        result.append(
            {
                "production_line": name,
                **kpis(rows, cohort, month_count),
                "product_count": len(cohort),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            item["reference_intensity_per_1000"] is None,
            -(item["reference_intensity_per_1000"] or 0),
            item["production_line"],
        ),
    )


def categories(
    events: list[Event], products: tuple[Product, ...], month_count: int
) -> list[dict[str, Any]]:
    """Category reference intensity, plus events per cohort product as an output-free check."""
    names = sorted(
        {product.category for product in products} | {event.category for event in events}
    )
    result = []
    for name in names:
        rows = [event for event in events if event.category == name]
        cohort = tuple(product for product in products if product.category == name)
        values = kpis(rows, cohort, month_count)
        result.append(
            {
                "category": name,
                **values,
                "product_count": len(cohort),
                "production_lines": sorted({product.production_line for product in cohort}),
                "eligible_events_per_cohort_product": ratio(
                    values["eligible_event_count"], len(cohort)
                ),
                "average_monthly_output_per_product": ratio(
                    values["known_monthly_output_subtotal"], values["output_known_product_count"]
                ),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            item["reference_intensity_per_1000"] is None,
            -(item["reference_intensity_per_1000"] or 0),
            item["category"],
        ),
    )


def shared_category_controls(
    events: list[Event], products: tuple[Product, ...], month_count: int
) -> list[dict[str, Any]]:
    """Use a category carried by several lines as a within-scope comparison.

    Within each line the category is compared with the rest of that line (same line,
    different products). Across lines its observed repair cost is compared with the same
    category elsewhere. Both are descriptive mix controls, not causal tests.
    """
    lines_by_category: dict[str, set[str]] = defaultdict(set)
    for product in products:
        lines_by_category[product.category].add(product.production_line)
    result = []
    for category, line_names in sorted(lines_by_category.items()):
        if len(line_names) < 2:
            continue
        rows = []
        for line in sorted(line_names):
            inside = kpis(
                [e for e in events if e.production_line == line and e.category == category],
                tuple(p for p in products if p.production_line == line and p.category == category),
                month_count,
            )
            rest_products = tuple(
                p for p in products if p.production_line == line and p.category != category
            )
            rest = (
                kpis(
                    [e for e in events if e.production_line == line and e.category != category],
                    rest_products,
                    month_count,
                )
                if rest_products
                else None
            )
            elsewhere = _financial(
                [e for e in events if e.category == category and e.production_line != line]
            )
            rest_intensity = rest["reference_intensity_per_1000"] if rest else None
            inside_intensity = inside["reference_intensity_per_1000"]
            rows.append(
                {
                    "production_line": line,
                    "event_count": inside["event_count"],
                    "eligible_event_count": inside["eligible_event_count"],
                    "reference_output": inside["reference_output"],
                    "reference_intensity_per_1000": inside_intensity,
                    "rest_of_line_categories": sorted({p.category for p in rest_products}),
                    "rest_of_line_eligible_event_count": rest["eligible_event_count"]
                    if rest
                    else None,
                    "rest_of_line_reference_output": rest["reference_output"] if rest else None,
                    "rest_of_line_reference_intensity_per_1000": rest_intensity,
                    "intensity_ratio_to_rest_of_line": ratio(inside_intensity, rest_intensity)
                    if inside_intensity is not None and rest_intensity is not None
                    else None,
                    "average_known_repair_cost": inside["average_known_repair_cost"],
                    "cost_known_count": inside["cost_known_count"],
                    "same_category_other_lines_average_known_repair_cost": elsewhere[
                        "average_known_repair_cost"
                    ],
                    "same_category_other_lines_cost_known_count": elsewhere["cost_known_count"],
                    "cost_ratio_to_same_category_other_lines": ratio(
                        inside["average_known_repair_cost"],
                        elsewhere["average_known_repair_cost"],
                    )
                    if inside["average_known_repair_cost"] is not None
                    and elsewhere["average_known_repair_cost"] is not None
                    else None,
                }
            )
        compared = [
            row["intensity_ratio_to_rest_of_line"]
            for row in rows
            if row["intensity_ratio_to_rest_of_line"] is not None
        ]
        result.append(
            {
                "category": category,
                "line_count": len(line_names),
                "lines_compared": len(compared),
                "lines_above_rest_of_line": sum(value > 1 for value in compared),
                "min_intensity_ratio_to_rest_of_line": min(compared, default=None),
                "max_intensity_ratio_to_rest_of_line": max(compared, default=None),
                "lines": rows,
            }
        )
    return result


def line_cost_comparison(events: list[Event]) -> list[dict[str, Any]]:
    """Observed repair cost per known-cost event on each line against all other lines."""
    linked = [event for event in events if event.matched_product]
    total = _financial(linked)
    rows = []
    for name in sorted({event.production_line for event in linked}):
        inside = _financial([event for event in linked if event.production_line == name])
        others = _financial([event for event in linked if event.production_line != name])
        rows.append(
            {
                "production_line": name,
                "event_count": sum(event.production_line == name for event in linked),
                "known_repair_cost": inside["known_repair_cost"],
                "cost_known_count": inside["cost_known_count"],
                "cost_coverage_pct": inside["cost_coverage_pct"],
                "average_known_repair_cost": inside["average_known_repair_cost"],
                "other_lines_average_known_repair_cost": others["average_known_repair_cost"],
                "other_lines_cost_known_count": others["cost_known_count"],
                "ratio_to_other_lines": ratio(
                    inside["average_known_repair_cost"], others["average_known_repair_cost"]
                )
                if inside["average_known_repair_cost"] is not None
                and others["average_known_repair_cost"] is not None
                else None,
                "known_cost_share_pct": ratio(
                    inside["known_repair_cost"], total["known_repair_cost"], 100
                ),
                "known_cost_event_share_pct": ratio(
                    inside["cost_known_count"], total["cost_known_count"], 100
                ),
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            item["ratio_to_other_lines"] is None,
            -(item["ratio_to_other_lines"] or 0),
            item["production_line"],
        ),
    )


def location_severity(events: list[Event]) -> dict[str, Any]:
    locations = sorted({event.defect_location for event in events})
    severities = [
        severity for severity in SEVERITIES if any(event.severity == severity for event in events)
    ]
    counter = Counter((event.defect_location, event.severity) for event in events)
    location_counts = Counter(event.defect_location for event in events)
    totals: list[dict[str, Any]] = [
        {
            "location": location,
            "event_count": location_counts[location],
            "critical_count": counter[(location, "Critical")],
            "critical_share_pct": ratio(
                counter[(location, "Critical")], location_counts[location], 100
            ),
            "share_pct": ratio(location_counts[location], len(events), 100),
        }
        for location in locations
    ]
    totals.sort(key=lambda item: (-item["event_count"], item["location"]))
    cells = [
        {
            "location": location,
            "severity": severity,
            "count": counter[(location, severity)],
            "row_pct": ratio(counter[(location, severity)], location_counts[location], 100),
        }
        for location in locations
        for severity in severities
    ]
    known_events = [
        event
        for event in events
        if event.severity != "Unknown" and event.defect_location != "Unknown"
    ]
    excluded = len(events) - len(known_events)
    known_locations = sorted({event.defect_location for event in known_events})
    known_severities = [
        severity
        for severity in SEVERITIES[:-1]
        if any(event.severity == severity for event in known_events)
    ]
    test: dict[str, Any] = {
        "available": False,
        "chi2": None,
        "p_value": None,
        "degrees_of_freedom": None,
        "cramers_v": None,
        "minimum_expected": None,
        "low_expected_cell_pct": None,
        "reliable": False,
        "excluded_unknown_count": excluded,
        "interpretation": "At least two nonempty locations and severity levels are required for an association test.",
    }
    if len(known_locations) >= 2 and len(known_severities) >= 2:
        table = np.array(
            [
                [counter[(location, severity)] for severity in known_severities]
                for location in known_locations
            ]
        )
        statistic, p_value, degrees, expected = chi2_contingency(table, correction=False)
        minimum = float(expected.min())
        low_fraction = float((expected < 5).mean())
        reliable = minimum >= 1 and low_fraction <= 0.2
        n = int(table.sum())
        effect = math.sqrt(float(statistic) / (n * min(table.shape[0] - 1, table.shape[1] - 1)))
        test.update(
            available=True,
            chi2=float(statistic),
            p_value=float(p_value),
            degrees_of_freedom=int(degrees),
            cramers_v=effect,
            minimum_expected=minimum,
            low_expected_cell_pct=low_fraction * 100,
            reliable=reliable,
            interpretation=(
                "Exploratory association, not causation. Cramer's V describes the magnitude; repeated units and inspection mix are unobserved."
                if reliable
                else "Sparse expected cells make the chi-square approximation unreliable; do not use this p-value as evidence of association."
            ),
        )
    return {
        "locations": locations,
        "severities": severities,
        "cells": cells,
        "location_totals": totals,
        "test": test,
    }


def _pareto(
    rows: list[dict[str, Any]], value_field: str, cumulative_field: str
) -> tuple[list[dict[str, Any]], int | None]:
    ordered = sorted(rows, key=lambda item: (-item[value_field], item["product_id"]))
    total = math.fsum(item[value_field] for item in ordered)
    running = 0.0
    threshold = None
    result = []
    for rank, item in enumerate(ordered, start=1):
        running += item[value_field]
        share = ratio(running, total, 100)
        result.append({**item, "rank": rank, cumulative_field: share})
        if threshold is None and share is not None and share >= 80:
            threshold = rank
    return result, threshold


def product_metrics(
    events: list[Event], products: tuple[Product, ...], month_count: int
) -> dict[str, Any]:
    grouped: dict[int, list[Event]] = defaultdict(list)
    for event in events:
        grouped[event.product_id].append(event)
    metrics = []
    for product in products:
        rows = grouped[product.product_id]
        metrics.append(
            {
                "product_id": product.product_id,
                "product_name": product.product_name,
                "production_line": product.production_line,
                "category": product.category,
                **kpis(rows, (product,), month_count),
            }
        )
    count_rows, count_threshold = _pareto(metrics, "event_count", "cumulative_share_pct")
    cost_rows, cost_threshold = _pareto(
        metrics, "known_repair_cost", "cumulative_known_cost_share_pct"
    )
    # Rank the complete master cohort, not only the leading event-count rows.
    # Reference intensity is not additive, so it has no cumulative-share field.
    intensity_rows = [
        {
            **item,
            "rank": rank,
            "intensity_available": item["reference_intensity_per_1000"] is not None,
        }
        for rank, item in enumerate(
            sorted(
                metrics,
                key=lambda item: (
                    item["reference_intensity_per_1000"] is None,
                    -(item["reference_intensity_per_1000"] or 0),
                    item["product_id"],
                ),
            ),
            start=1,
        )
    ]
    return {
        "top": count_rows[:10],
        "all": count_rows,
        "pareto_product_count_80pct": count_threshold,
        "linked_event_total": sum(item["event_count"] for item in count_rows),
        "intensity_top": intensity_rows[:10],
        "intensity_all": intensity_rows,
        "cost_top": cost_rows[:10],
        "cost_all": cost_rows,
        "cost_pareto_product_count_80pct": cost_threshold,
        "known_cost_total": round(math.fsum(item["known_repair_cost"] for item in cost_rows), 2),
        "unmatched_known_cost": _known_cost(
            [event for event in events if not event.matched_product]
        ),
    }


def cost_groups(events: list[Event], field: str) -> list[dict[str, Any]]:
    names = {getattr(event, field) for event in events}
    total = _known_cost(events)
    result = []
    for name in names:
        group = [event for event in events if getattr(event, field) == name]
        financial = _financial(group)
        result.append(
            {
                "name": name,
                "event_count": len(group),
                **financial,
                "known_cost_share_pct": ratio(financial["known_repair_cost"], total, 100),
            }
        )
    return sorted(result, key=lambda item: (-item["known_repair_cost"], item["name"]))


def inspection_methods(events: list[Event], filters: Scope) -> dict[str, Any]:
    """Describe recorded-event mix without inventing inspection exposure."""
    rows = []
    for group in sorted(cost_groups(events, "inspection_method"), key=lambda item: item["name"]):
        method = group["name"]
        method_events = [event for event in events if event.inspection_method == method]
        critical_count = sum(event.severity == "Critical" for event in method_events)
        rows.append(
            {
                **{key: value for key, value in group.items() if key != "name"},
                "inspection_method": method,
                "event_share_pct": ratio(len(method_events), len(events), 100),
                "critical_count": critical_count,
                "critical_share_pct": ratio(critical_count, len(method_events), 100),
                "severity_counts": {
                    severity: sum(event.severity == severity for event in method_events)
                    for severity in SEVERITIES
                },
            }
        )
    return {
        "scope": asdict(filters),
        "event_count": len(events),
        "rows": rows,
        "financial_summary": _financial(events),
        "share_denominator": "All selected retained logged defect events; within-method Critical share uses that method's recorded events.",
        "unavailable_metrics": [
            "inspected_units",
            "ground_truth",
            "sensitivity",
            "miss_rate",
            "effectiveness_rank",
        ],
        "measurement_requirements": {
            "sensitivity": "True positives divided by ground-truth-positive units assessed by that method.",
            "false_negative_rate": "False negatives divided by ground-truth-positive units assessed by that method.",
            "specificity": "True negatives divided by ground-truth-negative units assessed by that method.",
            "false_positive_rate": "False positives divided by ground-truth-negative units assessed by that method.",
            "additional_data": [
                "Unit-level method outcomes linked to independently assessed ground truth, including missed defects and false positives.",
                "Sampling design and tested-unit exposure; produced-unit exposure and comparable operating conditions for an operational comparison.",
            ],
            "availability": "The supplied defect-event records lack these measurements. No effectiveness value or method ranking is estimated.",
        },
        "interpretation": "Counts, shares, severity mix and repair costs describe recorded defect events. Inspected-unit exposure, matched ground truth and missed defects are unavailable, so these observations cannot rank inspection effectiveness. Rows are ordered by method label.",
    }


def cleaning_impact(snapshot: Snapshot) -> dict[str, Any]:
    """Compare whole-case source rows and retained events on one fixed period."""
    quality, raw = snapshot.quality, snapshot.raw_summary
    raw_count = raw.get("in_period_raw_count")
    raw_cost = raw.get("in_period_known_repair_cost")
    retained_count = quality["in_scope_event_count"]
    retained_cost = quality["known_repair_cost"]
    return {
        "scope": asdict(Scope()),
        "quality_scope": {
            "kind": "canonical_full_snapshot",
            "filters_applied": False,
            "scenario_applied": False,
        },
        "metadata": {
            "dataset_version": snapshot.provenance.get("dataset_version"),
            "policy_version": snapshot.provenance.get("policy_version"),
            "scenario": "",
        },
        "raw": {
            "unit": "received source rows before duplicate resolution",
            "row_count": raw_count,
            "cost_known_count": raw.get("in_period_cost_known_count"),
            "cost_unknown_count": raw.get("in_period_cost_unknown_count"),
            "known_repair_cost": raw_cost,
            "cost_status_counts": dict(raw.get("in_period_cost_status_counts", {})),
        },
        "retained": {
            "unit": "retained unique nonconflicting defect events",
            "event_count": retained_count,
            "cost_known_count": quality["known_cost_count"],
            "cost_unknown_count": quality["unknown_cost_count"],
            "known_repair_cost": retained_cost,
        },
        "comparison_direction": "retained minus raw",
        "row_count_difference": retained_count - raw_count if raw_count is not None else None,
        "known_cost_difference": round(retained_cost - raw_cost, 2)
        if raw_cost is not None
        else None,
        "currency_unit": "local currency",
        "raw_numeric_cost_including_negatives": raw.get("raw_numeric_cost_including_negatives"),
        "full_source_reconciliation": quality["reconciliation"],
        "full_source_scope": "All received source rows, including years outside the canonical analysis period.",
        "warnings": [
            "Raw rows can repeat the same event; raw rows and retained events have different grains.",
            "Raw valid-cost totals exclude missing, negative and invalid costs. The separate raw numeric diagnostic includes negative amounts and is not a valid repair-cost total.",
            "Full-source dispositions include out-of-period rows and must not be added to explain only the fixed-period comparison.",
            "Dashboard filters and selected scenarios do not alter this canonical whole-case comparison.",
        ],
        "interpretation": "The differences reflect duplicate resolution, quarantine and cost validation within one canonical period. They do not establish manufacturing improvement, reduced defective units or realized savings.",
    }


def cost_distribution(events: list[Event]) -> dict[str, Any]:
    """Use fixed bins and declared interpolation on available observed costs."""
    known = sorted(event.repair_cost for event in events if event.repair_cost is not None)
    bounds = (0, 100, 250, 500, 750, 1000)
    histogram = []
    for index, lower in enumerate(bounds):
        upper = bounds[index + 1] if index + 1 < len(bounds) else None
        values = [value for value in known if value >= lower and (upper is None or value < upper)]
        histogram.append(
            {
                "lower": lower,
                "upper": upper,
                "label": f"{lower} to <{upper}" if upper is not None else f"{lower} or more",
                "count": len(values),
                "known_cost": round(math.fsum(values), 2),
                "share_of_known_cost_events_pct": ratio(len(values), len(known), 100),
            }
        )

    def quantile(probability: float) -> float | None:
        if not known:
            return None
        position = (len(known) - 1) * probability
        lower = math.floor(position)
        upper = math.ceil(position)
        return known[lower] + (known[upper] - known[lower]) * (position - lower)

    return {
        "unit": "local currency",
        "event_count": len(events),
        **_financial(events),
        "unknown_by_status": dict(
            sorted(
                Counter(event.cost_status for event in events if event.repair_cost is None).items()
            )
        ),
        "quantile_method": "linear interpolation at (n-1)*p",
        "quantiles": {
            name: quantile(probability)
            for name, probability in (
                ("min", 0),
                ("p25", 0.25),
                ("median", 0.5),
                ("p75", 0.75),
                ("p90", 0.9),
                ("p95", 0.95),
                ("max", 1),
            )
        },
        "histogram": histogram,
        "interpretation": "Histogram and quantiles describe only selected events with available valid observed cost. The unknown-cost count and coverage use all selected events; no unknown cost is imputed.",
    }


COMPARISON_DIMENSIONS = (
    "production_line",
    "category",
    "product_id",
    "defect_type",
    "defect_location",
    "severity",
    "inspection_method",
)


def _period_inputs(
    snapshot: Snapshot,
    baseline_scope: Scope,
    comparison_scope: Scope,
    permitted_missing_event_filters: dict[str, str] | None,
) -> tuple[Any, ...]:
    """Validate and filter both periods once; reusable across comparison dimensions."""
    for scope in (baseline_scope, comparison_scope):
        inherited = {
            field: value
            for field, value in (permitted_missing_event_filters or {}).items()
            if getattr(scope, field) == value
        }
        validate_filters(snapshot, scope, permitted_missing_event_filters=inherited)
    baseline_events = filter_events(snapshot, baseline_scope)
    comparison_events = filter_events(snapshot, comparison_scope)
    baseline_products = filter_products(snapshot, baseline_scope)
    comparison_products = filter_products(snapshot, comparison_scope)
    baseline_values = kpis(baseline_events, baseline_products, len(baseline_scope.months()))
    comparison_values = kpis(comparison_events, comparison_products, len(comparison_scope.months()))
    return (
        baseline_events,
        comparison_events,
        baseline_products,
        comparison_products,
        baseline_values,
        comparison_values,
    )


def compare_periods(
    snapshot: Snapshot,
    baseline_scope: Scope,
    comparison_scope: Scope,
    dimension: str = "production_line",
    *,
    permitted_missing_event_filters: dict[str, str] | None = None,
    _prepared: tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    """Partition the selected difference exactly once across a declared field."""
    if dimension not in COMPARISON_DIMENSIONS:
        raise ValueError("Unknown comparison dimension")
    (
        baseline_events,
        comparison_events,
        baseline_products,
        comparison_products,
        baseline_values,
        comparison_values,
    ) = (
        _prepared
        if _prepared is not None
        else _period_inputs(
            snapshot, baseline_scope, comparison_scope, permitted_missing_event_filters
        )
    )
    # KPI dicts are shared between dimensions; hand each result its own copy.
    baseline_values = dict(baseline_values)
    comparison_values = dict(comparison_values)
    total_change = comparison_values["event_count"] - baseline_values["event_count"]
    names = {str(getattr(event, dimension)) for event in baseline_events + comparison_events}
    product_dimension = dimension in {"production_line", "category", "product_id"}
    if product_dimension:
        names.update(
            str(getattr(product, dimension)) for product in baseline_products + comparison_products
        )

    def grouped_events(events: list[Event]) -> dict[str, list[Event]]:
        result: dict[str, list[Event]] = defaultdict(list)
        for event in events:
            result[str(getattr(event, dimension))].append(event)
        return result

    def grouped_products(products: tuple[Product, ...]) -> dict[str, list[Product]]:
        result: dict[str, list[Product]] = defaultdict(list)
        for product in products:
            result[str(getattr(product, dimension))].append(product)
        return result

    baseline_by_name = grouped_events(baseline_events)
    comparison_by_name = grouped_events(comparison_events)
    baseline_cohorts = grouped_products(baseline_products) if product_dimension else {}
    comparison_cohorts = grouped_products(comparison_products) if product_dimension else {}

    def group_values(
        by_name: dict[str, list[Event]],
        cohorts: dict[str, list[Product]],
        products: tuple[Product, ...],
        scope: Scope,
        name: str,
    ) -> dict[str, Any]:
        cohort = tuple(cohorts.get(name, ())) if product_dimension else products
        return kpis(by_name.get(name, []), cohort, len(scope.months()))

    rows = []
    for name in sorted(names, key=(int if dimension == "product_id" else str)):
        baseline = group_values(
            baseline_by_name, baseline_cohorts, baseline_products, baseline_scope, name
        )
        comparison = group_values(
            comparison_by_name, comparison_cohorts, comparison_products, comparison_scope, name
        )
        delta = comparison["event_count"] - baseline["event_count"]
        before_contribution = _intensity(
            baseline["eligible_event_count"], baseline_values["reference_output"]
        )
        after_contribution = _intensity(
            comparison["eligible_event_count"], comparison_values["reference_output"]
        )
        rows.append(
            {
                "name": name,
                "baseline": baseline,
                "comparison": comparison,
                "event_count_change": delta,
                "critical_count_change": comparison["critical_count"] - baseline["critical_count"],
                "known_repair_cost_change": round(
                    comparison["known_repair_cost"] - baseline["known_repair_cost"], 2
                ),
                "contribution_pct_of_event_change": delta / total_change * 100
                if total_change != 0
                else None,
                "reference_intensity_change_contribution": (
                    after_contribution - before_contribution
                    if before_contribution is not None and after_contribution is not None
                    else None
                ),
            }
        )
    rows.sort(key=lambda row: (-abs(row["event_count_change"]), row["name"]))
    cost_change = round(
        comparison_values["known_repair_cost"] - baseline_values["known_repair_cost"], 2
    )
    baseline_intensity = baseline_values["reference_intensity_per_1000"]
    comparison_intensity = comparison_values["reference_intensity_per_1000"]
    intensity_change = (
        comparison_intensity - baseline_intensity
        if comparison_intensity is not None and baseline_intensity is not None
        else None
    )
    event_reconciles = sum(row["event_count_change"] for row in rows) == total_change

    def exact_cost(events: list[Event]) -> Fraction:
        # Binary float denominators are powers of two, so one common denominator
        # preserves every retained value without repeated Fraction reductions.
        ratios = [
            event.repair_cost.as_integer_ratio()
            for event in events
            if event.repair_cost is not None
        ]
        denominator = max((divisor for _, divisor in ratios), default=1)
        return Fraction(
            sum(numerator * (denominator // divisor) for numerator, divisor in ratios), denominator
        )

    cost_reconciles = True
    for side, events, groups, values in (
        ("baseline", baseline_events, baseline_by_name, baseline_values),
        ("comparison", comparison_events, comparison_by_name, comparison_values),
    ):
        whole_cost = exact_cost(events)
        group_costs = {name: exact_cost(groups.get(name, [])) for name in names}
        cost_reconciles = cost_reconciles and (
            sum(group_costs.values(), Fraction()) == whole_cost
            and values["known_repair_cost"] == round(float(whole_cost), 2)
            and all(
                row[side]["known_repair_cost"] == round(float(group_costs[row["name"]]), 2)
                for row in rows
            )
        )
    if not event_reconciles or not cost_reconciles:
        raise AssertionError("Comparison contribution reconciliation failed")
    return {
        "dimension": dimension,
        "baseline": {"scope": asdict(baseline_scope), "kpis": baseline_values},
        "comparison": {"scope": asdict(comparison_scope), "kpis": comparison_values},
        "event_count_change": total_change,
        "known_repair_cost_change": cost_change,
        "reference_intensity_change": intensity_change,
        "rows": rows,
        "reconciliation": {
            "event_change_reconciles": event_reconciles,
            "known_cost_change_reconciles": cost_reconciles,
            "reference_intensity_change_reconciles": (
                math.isclose(
                    math.fsum(row["reference_intensity_change_contribution"] for row in rows),
                    intensity_change,
                    abs_tol=1e-12,
                )
                if intensity_change is not None
                else None
            ),
        },
        "known_cost_reconciliation": {
            "basis": "Exact sums of unrounded retained repair_cost values before display rounding.",
            "display_decimal_places": 2,
            "displayed_change_residual": round(
                math.fsum(row["known_repair_cost_change"] for row in rows) - cost_change, 2
            ),
            "interpretation": "Displayed period costs and changes use independent two-decimal rounding. Displayed group changes may not add to the displayed total change; the residual is the group-change sum minus the total change. No rounding difference is assigned to an event or group.",
        },
        "interpretation": "Comparison minus baseline. Group contributions are arithmetic contributions, not causes. Signed percentage contributions can exceed 100% when other groups offset them; a zero net change has no percentage decomposition.",
        "definitions": {
            "reference_intensity": REFERENCE_NOTE,
            "intensity_contribution": "Group eligible events divided by the complete period's reference output; these contributions add to the overall intensity change. Group-specific rates are reported separately inside each group's KPIs.",
            "known_cost_contribution": "Cost reconciliation checks exact unrounded retained costs. Displayed group and total changes are rounded independently to two decimals, so their difference is reported as known_cost_reconciliation.displayed_change_residual rather than assigned to a group.",
        },
    }


def top_product_cluster(
    events: list[Event],
    products: tuple[Product, ...],
    filters: Scope,
    choices: dict[str, list[str]],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    chosen = metrics["all"][:5]
    identifiers = {row["product_id"] for row in chosen}
    rows = [event for event in events if event.product_id in identifiers]
    cohort = tuple(product for product in products if product.product_id in identifiers)
    return {
        "selection_rule": "The first five eligible master products ranked by selected event count, with product_id as the tie-breaker; no high-count product is removed.",
        "product_ids": sorted(identifiers),
        "scope": asdict(filters),
        "products": chosen,
        "kpis": kpis(rows, cohort, len(filters.months())),
        "monthly": monthly(rows, cohort, filters, choices),
    }


def compare_reports(canonical: dict[str, Any], selected: dict[str, Any]) -> dict[str, Any]:
    """Compare two backend results without changing either immutable context."""
    for field in ("dataset_version", "policy_version"):
        if canonical["metadata"].get(field) != selected["metadata"].get(field):
            raise ValueError(
                "Scenario comparison requires one dataset and canonical policy version"
            )
    if canonical["scope"]["scenario"] or {
        key: value for key, value in canonical["scope"].items() if key != "scenario"
    } != {key: value for key, value in selected["scope"].items() if key != "scenario"}:
        raise ValueError("Scenario comparison requires identical non-scenario scope")
    scenario = selected["scope"]["scenario"]
    validate_scenario(scenario)
    fields = (
        "event_count",
        "linked_event_count",
        "eligible_event_count",
        "critical_count",
        "known_repair_cost",
        "cost_known_count",
        "cost_unknown_count",
        "cost_coverage_pct",
        "reference_output",
        "reference_intensity_per_1000",
    )
    deltas = {
        field: {
            "canonical": canonical["kpis"][field],
            "selected": selected["kpis"][field],
            "delta": (
                round(selected["kpis"][field] - canonical["kpis"][field], 2)
                if field == "known_repair_cost"
                else selected["kpis"][field] - canonical["kpis"][field]
            )
            if canonical["kpis"][field] is not None and selected["kpis"][field] is not None
            else None,
        }
        for field in fields
    }

    def ranking(report: dict[str, Any]) -> dict[str, list[Any]]:
        return {
            "lines_by_reference_intensity": [
                row["production_line"]
                for row in report["lines"]
                if row["reference_intensity_per_1000"] is not None
            ],
            "defect_types_by_known_cost": [
                row["name"] for row in report["costs"]["by_type"] if row["cost_available"]
            ],
            "products_by_known_cost": [
                row["product_id"] for row in report["products"]["cost_all"] if row["cost_available"]
            ],
        }

    before, after = ranking(canonical), ranking(selected)

    def signals(report: dict[str, Any]) -> dict[str, Any]:
        locations = sorted(
            report["location_severity"]["location_totals"],
            key=lambda row: (-row["critical_count"], -row["event_count"], row["location"]),
        )
        ranks = ranking(report)
        return {
            "critical_location": locations[0]["location"]
            if locations and locations[0]["critical_count"]
            else None,
            "reference_intensity_line": next(iter(ranks["lines_by_reference_intensity"]), None),
            "known_cost_defect_type": next(iter(ranks["defect_types_by_known_cost"]), None),
            "highest_known_cost_product_id": next(iter(ranks["products_by_known_cost"]), None),
            "reference_intensity_category": next(
                (
                    row["category"]
                    for row in report.get("category_summary", [])
                    if row["reference_intensity_per_1000"] is not None and row["event_count"]
                ),
                None,
            ),
            "repair_cost_line": next(
                (
                    row["production_line"]
                    for row in report.get("line_cost_comparison", [])
                    if row["ratio_to_other_lines"] is not None
                ),
                None,
            ),
        }

    before_signals, after_signals = signals(canonical), signals(selected)
    return {
        "scenario": scenario,
        "scope": selected["scope"],
        "metadata": {
            key: selected["metadata"][key]
            for key in ("dataset_version", "policy_version", "scenario")
        },
        "scenario_definition": SCENARIOS[scenario],
        "metric_deltas": deltas,
        "priority_order_changed": before_signals != after_signals,
        "priority_signals": {"canonical": before_signals, "selected": after_signals},
        "rankings": {
            key: {
                "canonical": before[key],
                "selected": after[key],
                "changed": before[key] != after[key],
            }
            for key in before
        },
        "limitations": [
            "A scenario changes declared assumptions; it does not establish corrected truth, confidence bounds, manufacturing improvement or realized savings.",
            "Quality audit and workbook continue to describe the complete canonical source snapshot.",
        ],
    }


def scenario_comparison(snapshot: Snapshot, filters: Scope, scenario: str) -> dict[str, Any]:
    validate_scenario(scenario)
    canonical = analysis_report(snapshot, replace(filters, scenario=""))
    selected = analysis_report(snapshot, replace(filters, scenario=scenario))
    return {
        "canonical": canonical,
        "selected": selected,
        "comparison": compare_reports(canonical, selected),
    }


def anomalies(
    snapshot: Snapshot, filters: Scope, *, coverage: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Freeze January at 31 calendar days; never refit against evaluation data."""
    rows = filter_events(snapshot, filters, ignore_dates=True)
    counts = Counter(event.defect_date for event in rows)
    january = [counts[date(2024, 1, day)] for day in range(1, 32)]
    baseline_count = sum(january)
    mean = baseline_count / 31
    limit = mean + 3 * math.sqrt(mean) if baseline_count else None
    variance = sum((count - mean) ** 2 for count in january) / 30
    days = []
    candidates = []
    day = date(2024, 1, 1)
    while day <= date(2024, 6, 30):
        month = _year_month(day)
        if filters.start_month <= month <= filters.end_month:
            candidate = day.month > 1 and limit is not None and counts[day] > limit
            entry = {
                "date": day.isoformat(),
                "count": counts[day],
                "phase": "baseline" if day.month == 1 else "evaluation",
                "candidate": candidate,
            }
            days.append(entry)
            if candidate:
                candidates.append(
                    {"date": day.isoformat(), "count": counts[day], "upper_reference_limit": limit}
                )
        day += timedelta(days=1)
    return {
        "method": "Frozen January daily logged-count screen (mean + 3√mean)",
        "baseline_start": "2024-01-01",
        "baseline_end": "2024-01-31",
        "baseline_days": 31,
        "baseline_count": baseline_count,
        "baseline_daily_mean": mean,
        "upper_reference_limit": limit,
        "baseline_variance_to_mean": ratio(variance, mean),
        "enabled": baseline_count > 0,
        "candidates": candidates,
        "daily": days,
        "limitations": [
            "Exploratory candidates for investigation, not confirmed defects, process alarms or validated SPC limits.",
            "The baseline uses the same non-date filters; the January baseline remains fixed when month filters change.",
            "Calendar days with no records contribute zero logged events; actual reporting completeness and operating days are unknown.",
            calendar_coverage_note(
                coverage
                if coverage is not None
                else calendar_coverage(filter_events(snapshot, filters), filters)
            ),
            "A Poisson-like equal-exposure assumption is unverified. No actual daily output, inspected units, or adjustment for multiple screening comparisons is available.",
        ],
    }


LINE_COST_GAP_THRESHOLD = 1.2
"""A line repair-cost priority needs average known cost at least 20% above other lines."""


def recommendations(
    values: dict[str, Any],
    line_rows: list[dict[str, Any]],
    location: dict[str, Any],
    costs: dict[str, list[dict[str, Any]]],
    category_rows: list[dict[str, Any]] | None = None,
    category_controls: list[dict[str, Any]] | None = None,
    line_costs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Three scope-aware priorities: severity concentration, frequency, then repair cost.

    Frequency prefers the highest-intensity category when the scope compares at least two
    categories; repair cost prefers a line whose observed cost per event is materially above
    the other lines. Narrower scopes fall back to the line-intensity and defect-type views.
    """
    if values["event_count"] == 0:
        return []
    result: list[dict[str, Any]] = []
    focus = sorted(
        location["location_totals"],
        key=lambda item: (-item["critical_count"], -item["event_count"], item["location"]),
    )
    if focus and focus[0]["critical_count"] > 0:
        item = focus[0]
        others = [row for row in location["location_totals"] if row["location"] != item["location"]]
        other_events = sum(row["event_count"] for row in others)
        other_critical = sum(row["critical_count"] for row in others)
        result.append(
            {
                "priority": len(result) + 1,
                "kind": "critical_location",
                "title": f"Investigate Critical defects at {item['location']}",
                "focus": {"dimension": "defect_location", "value": item["location"]},
                "filters": {"defect_location": item["location"], "severity": "Critical"},
                "evidence": f"{item['critical_count']} Critical events among {item['event_count']} logged events at {item['location']} ({item['critical_share_pct']:.1f}%).",
                "action": "Review these defect records with operators, inspect representative failures and verify causes before choosing corrective actions.",
                "owner": "Quality Engineer and relevant Production Team Leader",
                "success_measure": "Track Critical events and their share at this location with actual inspected/produced units and comparable inspection coverage; set a target after baseline validation.",
                "metrics": {
                    "location": item["location"],
                    "critical_count": item["critical_count"],
                    "event_count": item["event_count"],
                    "critical_share_pct": item["critical_share_pct"],
                    "share_of_all_critical_pct": ratio(
                        item["critical_count"], values["critical_count"], 100
                    ),
                    "other_locations_critical_share_pct": ratio(other_critical, other_events, 100),
                },
            }
        )
    rated = [row for row in category_rows or [] if row["reference_intensity_per_1000"] is not None]
    leader = rated[0] if len(rated) >= 2 and rated[0]["event_count"] > 0 else None
    rest_output = (
        values["reference_output"] - leader["reference_output"]
        if leader is not None and values["reference_output"] is not None
        else None
    )
    rest_intensity = (
        _intensity(values["eligible_event_count"] - leader["eligible_event_count"], rest_output)
        if leader is not None and rest_output
        else None
    )
    comparable = [
        item
        for item in line_rows
        if item["reference_intensity_per_1000"] is not None and item["event_count"] > 0
    ]
    if (
        leader is not None
        and rest_intensity is not None
        and (leader["reference_intensity_per_1000"] > rest_intensity)
    ):
        control = next(
            (row for row in category_controls or [] if row["category"] == leader["category"]),
            None,
        )
        within = (
            f"; higher than the rest of the line on {control['lines_above_rest_of_line']} of {control['lines_compared']} lines"
            if control and control["lines_compared"]
            else ""
        )
        result.append(
            {
                "priority": len(result) + 1,
                "kind": "category_intensity",
                "title": f"Investigate high-intensity {leader['category']} products",
                "focus": {"dimension": "category", "value": leader["category"]},
                "filters": {"category": leader["category"]},
                "evidence": f"{leader['category']}: {leader['eligible_event_count']} events over {leader['reference_output']:,} reference units ({leader['reference_intensity_per_1000']:.3f} per 1,000 vs {rest_intensity:.3f} elsewhere in scope){within}.",
                "action": "Compare design, materials and process steps of the highest-intensity products in this category with lower-intensity products on the same line; review representative failures before changing the process.",
                "owner": "Process Engineering with Quality Engineer and Production Team Leaders",
                "success_measure": "Track this category's reference intensity on each line and its ratio to the rest of the line, with Critical share; set a reduction target after actual output is confirmed.",
                "metrics": {
                    "category": leader["category"],
                    "eligible_event_count": leader["eligible_event_count"],
                    "reference_output": leader["reference_output"],
                    "reference_intensity_per_1000": leader["reference_intensity_per_1000"],
                    "rest_of_scope_reference_intensity_per_1000": rest_intensity,
                    "intensity_gap_per_1000": leader["reference_intensity_per_1000"]
                    - rest_intensity,
                    "product_count": leader["product_count"],
                    "eligible_events_per_cohort_product": leader[
                        "eligible_events_per_cohort_product"
                    ],
                    "average_monthly_output_per_product": leader[
                        "average_monthly_output_per_product"
                    ],
                    "lines_above_rest_of_line": control["lines_above_rest_of_line"]
                    if control
                    else None,
                    "lines_compared": control["lines_compared"] if control else None,
                    "min_intensity_ratio_to_rest_of_line": control[
                        "min_intensity_ratio_to_rest_of_line"
                    ]
                    if control
                    else None,
                    "max_intensity_ratio_to_rest_of_line": control[
                        "max_intensity_ratio_to_rest_of_line"
                    ]
                    if control
                    else None,
                },
            }
        )
    elif comparable:
        item = comparable[0]
        result.append(
            {
                "priority": len(result) + 1,
                "kind": "line_intensity",
                "title": f"Validate the burden on {item['production_line']}",
                "focus": {"dimension": "production_line", "value": item["production_line"]},
                "filters": {"production_line": item["production_line"]},
                "evidence": f"{item['event_count']} events; {item['reference_intensity_per_1000']:.3f} per 1,000 reference output units across the selected full months.",
                "action": "Compare product and defect mix, confirm actual output and inspection coverage, then investigate recurring failures with the line team.",
                "owner": f"{item['production_line']} Team Leader with Production Planning",
                "success_measure": "Recalculate events per actual produced/inspected units and repeat Critical-event counts at matched exposure; no reduction target is inferred from this case.",
                "metrics": {
                    "production_line": item["production_line"],
                    "event_count": item["event_count"],
                    "eligible_event_count": item["eligible_event_count"],
                    "reference_output": item["reference_output"],
                    "reference_intensity_per_1000": item["reference_intensity_per_1000"],
                },
            }
        )
    costly = [row for row in line_costs or [] if row["ratio_to_other_lines"] is not None]
    top_cost = costs["by_type"]
    if costly and costly[0]["ratio_to_other_lines"] >= LINE_COST_GAP_THRESHOLD:
        item = costly[0]
        result.append(
            {
                "priority": len(result) + 1,
                "kind": "line_repair_cost",
                "title": f"Investigate repair cost per event on {item['production_line']}",
                "focus": {"dimension": "production_line", "value": item["production_line"]},
                "filters": {"production_line": item["production_line"]},
                "evidence": f"{item['production_line']} averages {item['average_known_repair_cost']:,.2f} known repair cost per event vs {item['other_lines_average_known_repair_cost']:,.2f} on other lines ({item['ratio_to_other_lines']:.2f}x); {item['known_cost_share_pct']:.1f}% of linked known cost from {item['known_cost_event_share_pct']:.1f}% of linked known-cost events.",
                "action": "Break down repair cost by repair route, labour, parts and rework with Maintenance; confirm cost-recording rules with Finance; compare the same category on other lines before attributing the gap to the line.",
                "owner": f"{item['production_line']} Team Leader with Maintenance and Finance",
                "success_measure": "Track average known repair cost per event on this line against other lines for the same category, with cost coverage; set a savings target only after repair-cost records are verified.",
                "metrics": {
                    "production_line": item["production_line"],
                    "average_known_repair_cost": item["average_known_repair_cost"],
                    "other_lines_average_known_repair_cost": item[
                        "other_lines_average_known_repair_cost"
                    ],
                    "cost_gap_per_event": item["average_known_repair_cost"]
                    - item["other_lines_average_known_repair_cost"],
                    "ratio_to_other_lines": item["ratio_to_other_lines"],
                    "known_repair_cost": item["known_repair_cost"],
                    "known_cost_share_pct": item["known_cost_share_pct"],
                    "known_cost_event_share_pct": item["known_cost_event_share_pct"],
                },
            }
        )
    elif top_cost and top_cost[0]["known_repair_cost"] > 0:
        item = top_cost[0]
        result.append(
            {
                "priority": len(result) + 1,
                "kind": "defect_type_cost",
                "title": f"Review {item['name']} repair burden and cost capture",
                "focus": {"dimension": "defect_type", "value": item["name"]},
                "filters": {"defect_type": item["name"]},
                "evidence": f"{item['name']} accounts for {item['known_repair_cost']:,.2f} known local-currency repair cost ({item['known_cost_share_pct']:.1f}% of known costs); {values['cost_unknown_count']} selected events have unknown cost.",
                "action": "Review costly recurring repairs with Maintenance, reconcile missing/invalid costs with Finance, and verify corrective-action economics.",
                "owner": "Maintenance Lead, Quality Engineer and Finance",
                "success_measure": "Track known repair cost, mean observed cost per event with known cost, and cost coverage together; compare verified repair costs at comparable exposure before setting a savings target.",
                "metrics": {
                    "defect_type": item["name"],
                    "known_repair_cost": item["known_repair_cost"],
                    "known_cost_share_pct": item["known_cost_share_pct"],
                    "cost_unknown_count": values["cost_unknown_count"],
                },
            }
        )
    return result[:3]


def metadata(
    snapshot: Snapshot,
    state: dict[str, Any] | None = None,
    *,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    coverage = (
        coverage
        if coverage is not None
        else calendar_coverage(filter_events(snapshot, Scope()), Scope())
    )
    return {
        **snapshot.provenance,
        "currency": "local currency (unspecified)",
        "stale": False,
        "last_error": None,
        "last_attempt_at": snapshot.provenance["fetched_at"],
        **(state or {}),
        "reference_output_note": REFERENCE_NOTE,
        "limitations": [*LIMITATIONS, calendar_coverage_note(coverage)],
    }


def analysis_report(
    snapshot: Snapshot,
    filters: Scope,
    state: dict[str, Any] | None = None,
    *,
    permitted_missing_event_filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    validate_filters(
        snapshot, filters, permitted_missing_event_filters=permitted_missing_event_filters
    )
    internal_event_filters = {
        field: getattr(filters, field) for field in EVENT_FILTER_OPTIONS if getattr(filters, field)
    }
    canonical_snapshot = snapshot
    canonical_quality = snapshot.quality
    snapshot = scenario_snapshot(snapshot, filters.scenario)
    events = filter_events(snapshot, filters)
    products = filter_products(snapshot, filters)
    month_count = len(filters.months())
    choices = options(snapshot)
    values = kpis(events, products, month_count)
    month_rows = monthly(events, products, filters, choices)
    line_rows = lines(events, products, month_count)
    category_rows = categories(events, products, month_count)
    category_controls = shared_category_controls(events, products, month_count)
    line_costs = line_cost_comparison(events)
    location = location_severity(events)
    costs = {
        "by_severity": cost_groups(events, "severity"),
        "by_type": cost_groups(events, "defect_type"),
        "by_location": cost_groups(events, "defect_location"),
    }
    coverage = calendar_coverage(events, filters)
    product_rows = product_metrics(events, products, month_count)
    baseline_scope = replace(
        filters, start_month=filters.start_month, end_month=filters.start_month
    )
    comparison_scope = replace(filters, start_month=filters.end_month, end_month=filters.end_month)
    period_events = filter_events(
        snapshot,
        Scope(
            start_month=filters.start_month, end_month=filters.end_month, scenario=filters.scenario
        ),
    )
    pairs = sorted(
        {(product.production_line, product.category) for product in products}
        | {(event.production_line, event.category) for event in events}
    )
    events_by_pair: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for event in events:
        events_by_pair[(event.production_line, event.category)].append(event)
    products_by_pair: dict[tuple[str, str], list[Product]] = defaultdict(list)
    for product in products:
        products_by_pair[(product.production_line, product.category)].append(product)
    line_category = [
        {
            "production_line": line,
            "category": category,
            **kpis(
                events_by_pair.get((line, category), []),
                tuple(products_by_pair.get((line, category), ())),
                month_count,
            ),
        }
        for line, category in pairs
    ]
    decomposition_inputs = _period_inputs(
        snapshot, baseline_scope, comparison_scope, internal_event_filters
    )
    return {
        "metadata": {
            **metadata(snapshot, state, coverage=coverage),
            "policy_version": POLICY_VERSION,
            "scenario": filters.scenario,
            "scenario_definition": SCENARIOS[filters.scenario],
        },
        "scope": asdict(filters),
        "kpis": values,
        "monthly": month_rows,
        "trend": trend(month_rows),
        "period_halves": period_halves(month_rows),
        "lines": line_rows,
        "line_category": line_category,
        "category_summary": category_rows,
        "shared_category_controls": category_controls,
        "line_cost_comparison": line_costs,
        "line_comparison_note": "Aggregate line ranks reflect the static category and product mix. Compare category-specific reference intensities before attributing a difference to the line; actual historical exposure is unavailable.",
        "location_severity": location,
        "products": product_rows,
        "costs": costs,
        "anomalies": anomalies(snapshot, filters, coverage=coverage),
        "cost_distribution": cost_distribution(events),
        "inspection_methods": inspection_methods(events, filters),
        "cleaning_impact": cleaning_impact(canonical_snapshot),
        "top_product_cluster": top_product_cluster(
            events, products, filters, choices, product_rows
        ),
        "decomposition": {
            dimension: compare_periods(
                snapshot,
                baseline_scope,
                comparison_scope,
                dimension,
                permitted_missing_event_filters=internal_event_filters,
                _prepared=decomposition_inputs,
            )
            for dimension in COMPARISON_DIMENSIONS
        },
        "event_populations": {
            "raw_source_rows": canonical_quality["raw_event_count"],
            "quarantined_source_rows": canonical_quality["quarantined_count"],
            "canonical_retained_all_years_events": canonical_quality["clean_event_count"],
            "scenario_retained_all_years_events": len(snapshot.events),
            "period_events_before_dimension_filters": len(period_events),
            "selected_events": len(events),
            "selected_linked_events": values["linked_event_count"],
            "reference_eligible_events": values["eligible_event_count"],
            "selected_known_cost_events": values["cost_known_count"],
            "selected_unknown_cost_events": values["cost_unknown_count"],
        },
        "recommendations": recommendations(
            values, line_rows, location, costs, category_rows, category_controls, line_costs
        ),
        "quality": canonical_quality,
        "quality_scope": {
            "kind": "canonical_full_snapshot",
            "filters_applied": False,
            "scenario_applied": False,
        },
        "definitions": DEFINITIONS,
        "metric_metadata": METRIC_METADATA,
        "calendar_coverage": coverage,
    }
