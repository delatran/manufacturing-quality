"""Reproduce aggregate date-assumption sensitivity using runtime HTTP evidence.

Call evaluate_date_sensitivity with freshly fetched HTTP records.
Only the master CSV is a local data input. Raw and transformed defect rows stay
in memory. No Git checkout or private work directory is required.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import platform
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .analytics import Scope, analysis_report, filter_events
from .config import MASTER_PATH, MONTHS
from .ingestion import fetch_events
from .preparation import build_snapshot

PROJECT_ROOT = Path(__file__).resolve().parents[2]

HISTORICAL_BASELINE_COMMIT = "e01c9498f6c3133885f451550c0ab8015b3ad14c"
EXPECTED_SOURCE_SHA256 = "863ca40ca37f639c064ce0bb66e679d7c3ab4ceca8cd79569d31310004728dc8"
EXPECTED_MASTER_SHA256 = "8772e8f0d0fbe1bf66b259859055e1742b066f91a31777b9f5e4630f606b9a86"
EXPECTED_ROWS_SHA256 = "90dd22bdbe9f28ceb6aa703a065bed090aba70ea2baba634261d7d13e18b687b"
CALCULATION_FILES = (
    "analyst/case_study/config.py",
    "analyst/case_study/models.py",
    "analyst/case_study/ingestion.py",
    "analyst/case_study/preparation.py",
    "analyst/case_study/analytics.py",
    "analyst/case_study/sensitivity.py",
)
LABELS = {
    "primary_md": "Primary M/D",
    "ambiguous_only_dm_stress": "Ambiguous-only D/M stress",
    "withhold_ambiguous_raw_rows": "Withhold ambiguous raw rows",
}
RULES = {
    "primary_md": "Adopted preparation policy: slash M/D/YYYY; unambiguous D/M exceptions and ISO dates remain supported.",
    "ambiguous_only_dm_stress": "Before duplicate resolution, change all and only 426 valid slash dates with distinct components both in 1..12 to their D/M interpretation, encoded as ISO. Leave every other field and date unchanged, then apply the same preparation and study scope.",
    "withhold_ambiguous_raw_rows": "Before duplicate resolution, withhold all 426 rows satisfying that ambiguity rule and process the remaining 664 with the same pipeline. This changes the evidence population; it does not estimate withheld events.",
    "cohort": "All 100 master products and six full months retain 594360 reference output units in every scenario. The study window remains January-June 2024. No year correction, cost imputation or product reassignment is performed.",
    "baseline": "Each scenario recalculates its own January mean over the same 31 calendar days. February-June counts are screened against that January mean + 3*sqrt(mean), without fitting to evaluation dates.",
}
LIMITATIONS = [
    "M/D remains the adopted convention, supported by predominant unambiguous month-first source rows. The scenarios do not establish D/M truth or equal probabilities.",
    "Deterministic stress scenarios are not confidence intervals, probabilistic bounds, or corrected plant totals.",
    "Alternative counts and costs reflect changed in-scope evidence, not manufacturing improvement or estimates of absent observations.",
    "Candidate counts are not detector accuracy. Actual daily exposure, reporting completeness, and a validated process baseline are unavailable.",
    "No records on a calendar date means zero logged events, not verified zero defects or zero production.",
    "This analysis reuses the preparation and analysis_report implementation to isolate the date assumption. Independent aggregate arithmetic does not independently validate cleaning.",
    "Confirm ambiguous dates with the source owner before operational prioritization by exact date; retain actual production and inspection exposure for future analysis.",
]
EXPECTED: dict[str, dict[str, Any]] = {
    "primary_md": {
        "events": 1071,
        "linked": 1067,
        "critical": 349,
        "known_count": 1049,
        "known_cost": "508864.39",
        "monthly": [147, 119, 148, 195, 210, 252],
        "critical_monthly": [50, 38, 50, 57, 80, 74],
        "cost_monthly": ["65488.29", "54376.13", "67147.84", "94773.56", "103148.47", "123930.10"],
        "reconciliation": [1090, 14, 2, 1074, 3, 1071],
        "candidate_dates": [
            "2024-04-09",
            "2024-05-25",
            "2024-06-03",
            "2024-06-10",
            "2024-06-20",
            "2024-06-21",
            "2024-06-27",
        ],
    },
    "ambiguous_only_dm_stress": {
        "events": 837,
        "linked": 833,
        "critical": 284,
        "known_count": 822,
        "known_cost": "398151.06",
        "monthly": [114, 102, 132, 142, 167, 180],
        "critical_monthly": [46, 29, 42, 50, 60, 57],
        "cost_monthly": ["52128.27", "46991.45", "62825.33", "67687.05", "81926.76", "86592.20"],
        "reconciliation": [1090, 14, 2, 1074, 237, 837],
        "candidate_dates": [
            "2024-02-05",
            "2024-03-06",
            "2024-04-22",
            "2024-04-24",
            "2024-04-28",
            "2024-05-06",
            "2024-05-13",
            "2024-05-16",
            "2024-05-18",
            "2024-05-25",
            "2024-06-15",
            "2024-06-18",
            "2024-06-20",
            "2024-06-21",
            "2024-06-26",
            "2024-06-27",
        ],
    },
    "withhold_ambiguous_raw_rows": {
        "events": 653,
        "linked": 649,
        "critical": 230,
        "known_count": 639,
        "known_cost": "309838.21",
        "monthly": [90, 74, 94, 116, 132, 147],
        "critical_monthly": [34, 21, 35, 43, 48, 49],
        "cost_monthly": ["39762.82", "32747.03", "44738.29", "56251.43", "64259.31", "72079.33"],
        "reconciliation": [664, 10, 0, 654, 1, 653],
        "candidate_dates": [
            "2024-02-23",
            "2024-04-22",
            "2024-04-24",
            "2024-04-26",
            "2024-04-28",
            "2024-05-13",
            "2024-05-16",
            "2024-05-18",
            "2024-05-22",
            "2024-05-25",
            "2024-05-27",
            "2024-06-15",
            "2024-06-17",
            "2024-06-18",
            "2024-06-19",
            "2024-06-20",
            "2024-06-21",
            "2024-06-26",
            "2024-06-27",
            "2024-06-28",
        ],
    },
}


def _require(condition: bool, detail: str) -> None:
    """Keep scientific gates active even when Python is run with -O."""
    if not condition:
        raise ValueError(detail)


def ambiguous_parts(row: dict[str, Any]) -> tuple[int, int, int] | None:
    """Return components only when the slash value represents two valid dates."""
    value = row.get("defect_date")
    match = (
        re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", value.strip())
        if isinstance(value, str)
        else None
    )
    if match is None:
        return None
    first, second, year = map(int, match.groups())
    if not (1 <= first <= 12 and 1 <= second <= 12 and first != second):
        return None
    try:
        date(year, first, second)
        date(year, second, first)
    except ValueError:
        return None
    return first, second, year


def make_scenarios(raw_events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Create isolated scenario rows; never mutate the caller's HTTP records."""
    primary = copy.deepcopy(raw_events)
    alternate = copy.deepcopy(raw_events)
    for row in alternate:
        parts = ambiguous_parts(row)
        if parts is not None:
            first, second, year = parts
            row["defect_date"] = f"{year:04d}-{second:02d}-{first:02d}"
    withheld = [copy.deepcopy(row) for row in raw_events if ambiguous_parts(row) is None]
    return {
        "primary_md": primary,
        "ambiguous_only_dm_stress": alternate,
        "withhold_ambiguous_raw_rows": withheld,
    }


def _row_digest(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _calculation_hashes() -> dict[str, str]:
    return {
        relative: hashlib.sha256(
            (PROJECT_ROOT / relative).read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for relative in CALCULATION_FILES
    }


def _verify_scenario(
    name: str, report: dict[str, Any], events: list, reconciliation: dict[str, Any]
) -> None:
    """Check fixed source-specific expectations and independent arithmetic."""
    expected = EXPECTED[name]
    kpis = report["kpis"]
    for key, expected_key in (
        ("event_count", "events"),
        ("linked_event_count", "linked"),
        ("critical_count", "critical"),
        ("cost_known_count", "known_count"),
    ):
        _require(
            kpis[key] == expected[expected_key],
            f"{name}: {key} changed; re-audit before updating expectations",
        )
    _require(
        kpis["reference_output"] == 594360 and kpis["cohort_product_count"] == 100,
        f"{name}: cohort changed",
    )
    independent_months = Counter(event.defect_date.strftime("%Y-%m") for event in events)
    independent_critical = Counter(
        event.defect_date.strftime("%Y-%m") for event in events if event.severity == "Critical"
    )
    month_counts = [row["event_count"] for row in report["monthly"]]
    _require(
        month_counts == expected["monthly"] == [independent_months[month] for month in MONTHS],
        f"{name}: monthly count mismatch",
    )
    _require(
        [row["critical_count"] for row in report["monthly"]]
        == expected["critical_monthly"]
        == [independent_critical[month] for month in MONTHS],
        f"{name}: Critical monthly mismatch",
    )
    independent_cost = sum(
        (Decimal(str(event.repair_cost)) for event in events if event.repair_cost is not None),
        Decimal(0),
    )
    _require(
        Decimal(str(kpis["known_repair_cost"]))
        == independent_cost
        == Decimal(expected["known_cost"]),
        f"{name}: total cost mismatch",
    )
    for index, month in enumerate(MONTHS):
        cost = sum(
            (
                Decimal(str(event.repair_cost))
                for event in events
                if event.repair_cost is not None and event.defect_date.strftime("%Y-%m") == month
            ),
            Decimal(0),
        )
        _require(
            Decimal(str(report["monthly"][index]["known_repair_cost"]))
            == cost
            == Decimal(expected["cost_monthly"][index]),
            f"{name}: {month} cost mismatch",
        )
    disposition_fields = (
        "raw_rows",
        "duplicate_copies_removed",
        "quarantined_rows",
        "retained_clean_events",
        "out_of_scope_events",
        "analysis_events",
    )
    _require(
        [reconciliation[key] for key in disposition_fields] == expected["reconciliation"],
        f"{name}: row disposition changed",
    )
    _require(
        reconciliation["raw_rows"] == sum(reconciliation[key] for key in disposition_fields[1:4]),
        f"{name}: row conservation failed",
    )
    counts = Counter(event.defect_date for event in events)
    daily = [counts[date(2024, 1, 1) + timedelta(days=offset)] for offset in range(182)]
    mean = sum(daily[:31]) / 31
    limit = mean + 3 * math.sqrt(mean)
    screen = report["anomalies"]
    _require(
        screen["baseline_days"] == 31 and screen["baseline_count"] == sum(daily[:31]),
        f"{name}: January baseline mismatch",
    )
    _require(
        math.isclose(screen["upper_reference_limit"], limit, rel_tol=0, abs_tol=1e-12),
        f"{name}: threshold mismatch",
    )
    _require([row["count"] for row in screen["daily"]] == daily, f"{name}: daily count mismatch")
    candidate_dates = [
        (date(2024, 1, 1) + timedelta(days=offset)).isoformat()
        for offset, count in enumerate(daily)
        if offset >= 31 and count > limit
    ]
    _require(
        [row["date"] for row in screen["candidates"]]
        == candidate_dates
        == expected["candidate_dates"],
        f"{name}: candidate dates changed",
    )
    _require(
        kpis["cost_unknown_count"] == len(events) - expected["known_count"]
        and math.isclose(kpis["cost_coverage_pct"], expected["known_count"] / len(events) * 100),
        f"{name}: cost coverage mismatch",
    )


def evaluate_date_sensitivity(
    raw_events: list[dict[str, Any]],
    http_provenance: dict[str, Any],
    master_bytes: bytes,
) -> dict[str, Any]:
    """Return aggregates using the notebook's same HTTP-derived inputs.

    This function does not fetch data or write files. The caller supplies the
    unmodified records/provenance returned by fetch_events() and master bytes.
    Source drift is a hard failure, not an automatic replacement of conclusions.
    """
    _require(
        http_provenance.get("source_sha256") == EXPECTED_SOURCE_SHA256,
        "HTTP source drifted; independently re-audit before reusing these conclusions",
    )
    _require(
        hashlib.sha256(master_bytes).hexdigest() == EXPECTED_MASTER_SHA256,
        "Master data drifted; re-audit the reference cohort",
    )
    _require(len(raw_events) == 1090, "Expected 1090 raw HTTP rows")
    ambiguous_count = sum(ambiguous_parts(row) is not None for row in raw_events)
    _require(ambiguous_count == 426, "Expected 426 genuinely ambiguous raw dates")
    original_row_digest = _row_digest(raw_events)
    _require(
        original_row_digest == EXPECTED_ROWS_SHA256,
        "HTTP-derived records changed after fetching; pass unmodified source rows",
    )
    results = {}
    monthly_fields = (
        "month",
        "event_count",
        "linked_event_count",
        "critical_count",
        "critical_share_pct",
        "known_repair_cost",
        "cost_known_count",
        "cost_unknown_count",
        "cost_coverage_pct",
        "reference_output",
        "reference_intensity_per_1000",
    )
    for name, rows in make_scenarios(raw_events).items():
        derived = name != "primary_md"
        provenance = dict(http_provenance)
        if derived:
            provenance.update(
                source_sha256=_row_digest(rows),
                source_url="in-memory scenario derived from audited HTTP response",
            )
            provenance["derived_records_sha256"] = hashlib.sha256(
                json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                    "utf-8"
                )
            ).hexdigest()
        snapshot = build_snapshot(rows, provenance, master_bytes)
        report = analysis_report(snapshot, Scope())
        events = filter_events(snapshot, Scope())
        quality = snapshot.quality
        _verify_scenario(name, report, events, quality["reconciliation"])
        line_count_rank = sorted(
            report["lines"], key=lambda row: (-row["event_count"], row["production_line"])
        )
        critical_location = sorted(
            report["location_severity"]["location_totals"],
            key=lambda row: (-row["critical_count"], -row["event_count"], row["location"]),
        )[0]
        screen = report["anomalies"]
        priority_signals = {
            "critical_location": critical_location["location"],
            "reference_intensity_line": report["lines"][0]["production_line"],
            "known_cost_defect_type": report["costs"]["by_type"][0]["name"],
            "highest_event_line": line_count_rank[0]["production_line"],
            "highest_known_cost_product_id": report["products"]["cost_top"][0]["product_id"],
        }
        _require(
            priority_signals
            == {
                "critical_location": "Component",
                "reference_intensity_line": "Line-3",
                "known_cost_defect_type": "Functional",
                "highest_event_line": "Line-4",
                "highest_known_cost_product_id": 81,
            },
            f"{name}: priority identity changed; re-audit interpretation",
        )
        results[name] = {
            "input_row_count": len(rows),
            "transformed_ambiguous_row_count": ambiguous_count
            if name == "ambiguous_only_dm_stress"
            else 0,
            "withheld_ambiguous_row_count": ambiguous_count
            if name == "withhold_ambiguous_raw_rows"
            else 0,
            "derived_input_sha256": provenance["source_sha256"] if derived else None,
            "snapshot_id": snapshot.provenance["snapshot_id"],
            "reconciliation": quality["reconciliation"],
            "quality_disposition": {
                key: quality[key]
                for key in (
                    "exact_duplicate_count",
                    "normalized_duplicate_count",
                    "complementary_duplicate_count",
                    "conflicting_id_count",
                    "conflicting_row_count",
                    "clean_event_count",
                    "out_of_scope_count",
                )
            },
            "kpis": report["kpis"],
            "trend": report["trend"],
            "monthly": [{key: row[key] for key in monthly_fields} for row in report["monthly"]],
            "priority_signals": priority_signals,
            "recommendation_titles": [row["title"] for row in report["recommendations"]],
            "rankings": {
                "lines_by_reference_intensity": [row["production_line"] for row in report["lines"]],
                "lines_by_event_count": [row["production_line"] for row in line_count_rank],
                "top_ten_product_ids_by_known_cost": [
                    row["product_id"] for row in report["products"]["cost_top"]
                ],
                "top_ten_product_ids_by_events": [
                    row["product_id"] for row in report["products"]["top"]
                ],
                "products_for_80pct_matched_known_cost": report["products"][
                    "cost_pareto_product_count_80pct"
                ],
            },
            "line_metrics": [
                {
                    key: row[key]
                    for key in (
                        "production_line",
                        "event_count",
                        "critical_count",
                        "known_repair_cost",
                        "cost_coverage_pct",
                        "reference_output",
                        "reference_intensity_per_1000",
                    )
                }
                for row in report["lines"]
            ],
            "location_totals": report["location_severity"]["location_totals"],
            "known_cost_by_defect_type": [
                {
                    key: row[key]
                    for key in ("name", "event_count", "known_repair_cost", "cost_coverage_pct")
                }
                for row in report["costs"]["by_type"]
            ],
            "location_severity_test": report["location_severity"]["test"],
            "historical_screen": {
                **{
                    key: screen[key]
                    for key in (
                        "method",
                        "baseline_start",
                        "baseline_end",
                        "baseline_days",
                        "baseline_count",
                        "baseline_daily_mean",
                        "upper_reference_limit",
                        "baseline_variance_to_mean",
                        "enabled",
                        "candidates",
                    )
                },
                "daily_start_date": "2024-01-01",
                "daily_end_date": "2024-06-30",
                "daily_counts": [row["count"] for row in screen["daily"]],
                "zero_logged_event_days": sum(row["count"] == 0 for row in screen["daily"]),
            },
        }
    _require(_row_digest(raw_events) == original_row_digest, "Input rows were mutated")
    dates = {
        name: {row["date"] for row in result["historical_screen"]["candidates"]}
        for name, result in results.items()
    }
    return {
        "schema_version": "1.0",
        "analysis_type": "date-assumption sensitivity; not confidence bounds, detector validation, or model comparison",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "historical_comparison_baseline_commit": HISTORICAL_BASELINE_COMMIT,
        "current_calculation_source_sha256_lf_normalized": _calculation_hashes(),
        "runtime": {"python": platform.python_version(), "scipy": version("scipy")},
        "source": dict(http_provenance),
        "master_sha256": EXPECTED_MASTER_SHA256,
        "original_rows_canonical_json_sha256": original_row_digest,
        "derived_hash_encoding": "UTF-8 json.dumps(rows, sort_keys=True, ensure_ascii=True, allow_nan=False); original HTTP byte hash is separate",
        "raw_rows": len(raw_events),
        "ambiguous_raw_rows": ambiguous_count,
        "rules": dict(RULES),
        "limitations": list(LIMITATIONS),
        "scenarios": results,
        "candidate_date_comparison": {
            "shared_by_all_scenarios": sorted(set.intersection(*dates.values())),
            "per_scenario": {
                name: {
                    "candidate_count": len(value),
                    "dates": sorted(value),
                    "primary_candidates_retained": sorted(value & dates["primary_md"]),
                    "primary_candidates_absent": sorted(dates["primary_md"] - value),
                }
                for name, value in dates.items()
            },
        },
    }


def fetch_date_sensitivity() -> dict[str, Any]:
    """Fetch the real source once and evaluate without persisting raw records."""
    raw, provenance = fetch_events()
    return evaluate_date_sensitivity(raw, provenance, MASTER_PATH.read_bytes())
