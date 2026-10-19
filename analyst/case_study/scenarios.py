"""Isolated policy assumptions over the immutable, HTTP-derived snapshot."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from .models import Snapshot

SCENARIOS: dict[str, dict[str, Any]] = {
    "": {
        "id": "",
        "name": "Canonical policy",
        "assumption": "Use the versioned conservative cleaning policy without scenario overrides.",
        "affects": [],
        "observed_improvement": False,
    },
    "exclude_cost_10": {
        "id": "exclude_cost_10",
        "name": "Withhold costs equal to 10",
        "assumption": "Treat observed repair costs equal to 10 as financially unavailable. Preserve every canonical event and its original value in the source snapshot.",
        "affects": ["financial availability", "cost rankings"],
        "parameters": {
            "repair_cost_equal": 10,
            "repair_cost_unit": "local currency",
            "effect": "withhold_financial_availability",
        },
        "observed_improvement": False,
    },
    "ambiguous_dm": {
        "id": "ambiguous_dm",
        "name": "Read ambiguous slash dates as D/M",
        "assumption": "Interpret only slash dates with distinct components both between 1 and 12 as D/M. Reapply duplicate resolution and the same study period. Unambiguous dates and source years stay unchanged.",
        "affects": [
            "event dates",
            "in-period population",
            "duplicate resolution",
            "historical screening",
        ],
        "observed_improvement": False,
    },
    "year_2024": {
        "id": "year_2024",
        "name": "Assume source event years are 2024",
        "assumption": "Set interpreted event years to 2024 while preserving month and day. This is an unverified year-correction assumption; the canonical source snapshot preserves the original years.",
        "affects": [
            "event dates",
            "in-period population",
            "duplicate resolution",
            "historical screening",
        ],
        "observed_improvement": False,
    },
}


def validate_scenario(scenario: str) -> None:
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise ValueError(
            "Unknown scenario; use canonical (empty), exclude_cost_10, ambiguous_dm or year_2024"
        )


def scenario_snapshot(snapshot: Snapshot, scenario: str) -> Snapshot:
    """Return a separate interpretation while retaining canonical source identity.

    Date assumptions run before duplicate resolution, so a scenario cannot hide
    a conflict by merely changing a final chart. Raw rows are reconstructed only
    from immutable strings held in this HTTP-derived in-memory snapshot.
    """
    validate_scenario(scenario)
    current = snapshot.applied_scenario
    if current == scenario:
        return snapshot
    if current:
        raise ValueError("Scenario calculation requires the canonical source snapshot")
    if not scenario:
        return snapshot
    if scenario == "exclude_cost_10":
        repair_cost_equal: float = SCENARIOS[scenario]["parameters"]["repair_cost_equal"]
        events = tuple(
            replace(event, repair_cost=None, cost_status="scenario_excluded_cost_10")
            if event.repair_cost == repair_cost_equal
            else event
            for event in snapshot.events
        )
        affected = sum(event.repair_cost == repair_cost_equal for event in snapshot.events)
        return replace(
            snapshot,
            events=events,
            applied_scenario=scenario,
            provenance={
                **snapshot.provenance,
                "scenario": scenario,
                "scenario_definition": SCENARIOS[scenario],
                "scenario_affected_clean_events": affected,
            },
        )
    if not snapshot.raw_records or not snapshot.master_bytes:
        raise ValueError(
            "Date scenarios require the original HTTP-derived rows in memory; refresh the source and retry"
        )
    from .preparation import build_snapshot

    rows = [json.loads(row) for row in snapshot.raw_records]
    projected = build_snapshot(rows, snapshot.provenance, snapshot.master_bytes, scenario=scenario)
    canonical_dates = {event.defect_id: event.defect_date for event in snapshot.events}
    affected = sum(
        canonical_dates.get(event.defect_id) != event.defect_date for event in projected.events
    )
    return replace(
        projected,
        provenance={
            **snapshot.provenance,
            "scenario": scenario,
            "scenario_definition": SCENARIOS[scenario],
            "scenario_affected_clean_events": affected,
        },
    )
