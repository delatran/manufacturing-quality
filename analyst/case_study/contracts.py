"""Validated numerical contracts shared by the engine and generated frontend."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Count = Annotated[int, Field(ge=0)]
Amount = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Percentage = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class FinancialMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    known_repair_cost: Amount
    cost_known_count: Count
    cost_unknown_count: Count
    cost_coverage_pct: Percentage | None
    average_known_repair_cost: Amount | None
    cost_available: bool
    cost_status: Literal["empty", "unavailable", "complete", "partial"]


class Metrics(FinancialMetrics):
    event_count: Count
    linked_event_count: Count
    unmatched_event_count: Count
    critical_count: Count
    critical_share_pct: Percentage | None
    eligible_event_count: Count
    reference_output: Count | None
    reference_intensity_per_1000: Amount | None
    reference_intensity_status: Literal[
        "no_eligible_products", "no_full_months", "incomplete_output", "available"
    ]
    output_known_product_count: Count
    output_unknown_product_count: Count
    output_coverage_pct: Percentage | None
    known_monthly_output_subtotal: Count
    cohort_products_with_events: Count
    cohort_products_without_events: Count
    products_with_events: Count
    cohort_product_count: Count
    month_count: Count
