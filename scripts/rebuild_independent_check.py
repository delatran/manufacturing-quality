"""Recompute the case from live HTTP bytes without importing application code.

The expected file is frozen before API comparison. Raw defect JSON is held only
in memory. Derived counts, source hashes, and reconciliation receipts are saved.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026"
SUPPLIED_COMMIT = "f6adf2451ac6297c1b749abc131cff8a46020dc9"
RAW_BASE = f"https://raw.githubusercontent.com/{REPOSITORY}"
DEFECT_PATH = "mock-api/defect_records.json"
MASTER_PATH = "data/master_data.csv"
FIELDS = (
    "defect_id",
    "product_id",
    "defect_type",
    "defect_date",
    "defect_location",
    "severity",
    "inspection_method",
    "repair_cost",
)
SEVERITIES = ("Minor", "Moderate", "Critical")
EXPECTED_FILE = ROOT / "evals/rebuild_independent_expected.json"
RESULT_FILE = ROOT / "evals/rebuild_independent_run.json"
SUPPLEMENTAL_FILE = ROOT / "evals/rebuild_independent_supplemental_expected.json"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, default=json_value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def fetch(url: str) -> tuple[bytes, dict[str, Any]]:
    requested_at = utc_now()
    request = Request(
        url,
        headers={
            "User-Agent": "RFID-independent-numerics/1",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urlopen(request, timeout=40) as response:
        body = response.read()
        metadata = {
            "url": url,
            "final_url": response.url,
            "requested_at": requested_at,
            "fetched_at": utc_now(),
            "status": response.status,
            "sha256": digest(body),
            "etag": response.headers.get("ETag"),
            "content_type": response.headers.get("Content-Type"),
            "byte_count": len(body),
        }
    return body, metadata


def source_bundle() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    urls = {
        "defects": f"{RAW_BASE}/main/{DEFECT_PATH}",
        "master_remote": f"{RAW_BASE}/main/{MASTER_PATH}",
        "defects_at_supplied_commit": f"{RAW_BASE}/{SUPPLIED_COMMIT}/{DEFECT_PATH}",
        "master_at_supplied_commit": f"{RAW_BASE}/{SUPPLIED_COMMIT}/{MASTER_PATH}",
        "assignment_readme": f"{RAW_BASE}/main/README.md",
        "head": f"https://api.github.com/repos/{REPOSITORY}/commits/main",
    }
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        downloads = dict(zip(urls, pool.map(fetch, urls.values()), strict=True))
    defects = json.loads(downloads["defects"][0], parse_float=Decimal)
    if not isinstance(defects, list) or not all(isinstance(row, dict) for row in defects):
        raise ValueError("Live defect response must be an array of objects")
    local_master_path = ROOT / MASTER_PATH
    local_master = local_master_path.read_bytes()
    master = list(csv.DictReader(io.StringIO(local_master.decode("utf-8-sig"))))
    head = json.loads(downloads["head"][0])["sha"]
    sources: dict[str, Any] = {key: metadata for key, (_, metadata) in downloads.items()}
    sources["master_local"] = {
        "path": str(local_master_path),
        "read_at": utc_now(),
        "sha256": digest(local_master),
        "byte_count": len(local_master),
    }
    sources["supplied_commit"] = SUPPLIED_COMMIT
    sources["observed_head"] = head
    sources["commit_status"] = "Passed" if head == SUPPLIED_COMMIT else "Failed"
    sources["defects_match_supplied_commit"] = (
        downloads["defects"][0] == downloads["defects_at_supplied_commit"][0]
    )
    sources["master_local_matches_remote"] = local_master == downloads["master_remote"][0]
    sources["master_matches_supplied_commit"] = (
        local_master == downloads["master_at_supplied_commit"][0]
    )
    sources["defects"]["schema"] = schema(defects)
    sources["master_local"]["schema"] = schema(master)
    readme = downloads["assignment_readme"][0].decode("utf-8")
    sources["assignment_readme"]["schema"] = {
        "format": "Markdown",
        "lines": len(readme.splitlines()),
    }
    sources["assignment_readme"]["content"] = readme
    return defects, master, sources


def schema(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        for key, value in row.items():
            fields[key]["null" if value is None else type(value).__name__] += 1
    return {
        "row_count": len(rows),
        "fields": {key: dict(types) for key, types in sorted(fields.items())},
    }


def parsed_date(text: str, scenario: str = "") -> tuple[date, str]:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed, kind = date.fromisoformat(text), "iso"
    else:
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if match is None:
            raise ValueError(f"Unsupported date syntax: {text!r}")
        first, second, year = map(int, match.groups())
        if 1 <= first <= 12 and 1 <= second <= 12:
            kind = "ambiguous_equal_parts" if first == second else "ambiguous_different_parts"
            day, month = (first, second) if scenario == "ambiguous_dm" else (second, first)
        elif 1 <= first <= 12 and second > 12:
            month, day, kind = first, second, "unambiguous_md"
        elif first > 12 and 1 <= second <= 12:
            day, month, kind = first, second, "unambiguous_dm"
        else:
            raise ValueError(f"Impossible slash date: {text!r}")
        parsed = date(year, month, day)
    if scenario == "year_2024" and parsed.year == 2026:
        parsed = parsed.replace(year=2024)
    return parsed, kind


def normalize(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("defect_type", "defect_location", "severity", "inspection_method"):
        result[key] = result[key].strip()
    result["severity"] = result["severity"].title()
    if result["repair_cost"] is not None:
        result["repair_cost"] = Decimal(str(result["repair_cost"]))
    return result


def unique_dicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    distinct: list[dict[str, Any]] = []
    for row in rows:
        if row not in distinct:
            distinct.append(row)
    return distinct


def independent_clean(raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[int, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for row_number, row in enumerate(raw, 1):
        if set(row) != set(FIELDS):
            raise ValueError(f"Unexpected raw schema in source row {row_number}")
        if not isinstance(row["defect_id"], int) or not isinstance(row["product_id"], int):
            raise ValueError(f"Non-integer identity in source row {row_number}")
        grouped[row["defect_id"]].append((row_number, row))
    clean, duplicate_details = [], []
    for event_id, variants in sorted(grouped.items()):
        original = [row for _, row in variants]
        normalized = [normalize(row) for row in original]
        resolved = dict(normalized[0])
        if len(variants) > 1:
            differing = [
                field for field in FIELDS if len({str(row[field]) for row in original}) > 1
            ]
            if len(unique_dicts(original)) == 1:
                subtype = "exact"
            elif len(unique_dicts(normalized)) == 1:
                subtype = "representation_equivalent"
            else:
                without_cost = [
                    {k: v for k, v in row.items() if k != "repair_cost"} for row in normalized
                ]
                costs = {row["repair_cost"] for row in normalized if row["repair_cost"] is not None}
                if (
                    len(unique_dicts(without_cost)) == 1
                    and len(costs) == 1
                    and any(row["repair_cost"] is None for row in normalized)
                ):
                    subtype = "complementary_missing_cost"
                    resolved["repair_cost"] = next(iter(costs))
                else:
                    subtype = "contradictory_quarantined"
            duplicate_details.append(
                {
                    "defect_id": event_id,
                    "source_rows": [number for number, _ in variants],
                    "subtype": subtype,
                    "differing_raw_fields": differing,
                    "raw_variant_count": len(variants),
                }
            )
            if subtype == "contradictory_quarantined":
                continue
        resolved["source_rows"] = [number for number, _ in variants]
        resolved["raw_date"] = resolved["defect_date"]
        resolved["defect_date"], resolved["date_kind"] = parsed_date(resolved["raw_date"])
        clean.append(resolved)
    return clean, {
        "raw_row_count": len(raw),
        "unique_defect_ids": len(grouped),
        "canonical_events_all_years": len(clean),
        "raw_rows_not_represented_as_separate_events": len(raw) - len(clean),
        "duplicate_groups": duplicate_details,
        "duplicate_group_subtypes": dict(Counter(group["subtype"] for group in duplicate_details)),
    }


def cost_stats(events: list[dict[str, Any]], scenario: str = "") -> dict[str, Any]:
    valid, missing, negative, excluded = [], 0, 0, 0
    for event in events:
        value = event["repair_cost"]
        if value is None:
            missing += 1
        elif value < 0:
            negative += 1
        elif scenario == "exclude_cost_10" and value == 10:
            excluded += 1
        else:
            valid.append(value)
    valid.sort()

    def quantile(fraction: Decimal) -> Decimal | None:
        if not valid:
            return None
        position = fraction * (len(valid) - 1)
        lower = int(position)
        upper = min(lower + 1, len(valid) - 1)
        return valid[lower] + (valid[upper] - valid[lower]) * (position - lower)

    total = sum(valid, Decimal(0))
    return {
        "valid_count": len(valid),
        "missing_count": missing,
        "negative_count": negative,
        "scenario_excluded_count": excluded,
        "unknown_count": len(events) - len(valid),
        "coverage_pct": 100 * len(valid) / len(events) if events else None,
        "known_total": total,
        "mean_valid": total / len(valid) if valid else None,
        "median_valid": quantile(Decimal("0.5")),
        "minimum_valid": min(valid) if valid else None,
        "maximum_valid": max(valid) if valid else None,
        "p25_valid": quantile(Decimal("0.25")),
        "p75_valid": quantile(Decimal("0.75")),
        "observed_ten_count": sum(event["repair_cost"] == 10 for event in events),
        "unit": "local currency",
    }


def masters(rows: list[dict[str, str]]) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    products: dict[int, dict[str, Any]] = {
        int(row["product_id"]): {
            **row,
            "product_id": int(row["product_id"]),
            "monthly_output": int(row["monthly_output"]),
        }
        for row in rows
    }
    line_category: dict[str, Counter[str]] = defaultdict(Counter)
    for product in products.values():
        line_category[product["production_line"]][product["category"]] += 1
    return products, {
        "row_count": len(rows),
        "unique_product_ids": len(products),
        "ids_contiguous_1_to_100": sorted(products) == list(range(1, 101)),
        "product_name_first": products[min(products)]["product_name"],
        "product_name_last": products[max(products)]["product_name"],
        "blank_cell_count": sum(value == "" for row in rows for value in row.values()),
        "trim_change_cell_count": sum(
            value != value.strip() for row in rows for value in row.values()
        ),
        "nonpositive_output_product_ids": [
            product["product_id"] for product in products.values() if product["monthly_output"] <= 0
        ],
        "line_product_counts": dict(
            Counter(product["production_line"] for product in products.values())
        ),
        "category_product_counts": dict(
            Counter(product["category"] for product in products.values())
        ),
        "line_category_product_counts": {
            line: dict(counts) for line, counts in sorted(line_category.items())
        },
        "monthly_reference_output": sum(product["monthly_output"] for product in products.values()),
    }


def quality_scan(
    raw: list[dict[str, Any]], events: list[dict[str, Any]], products: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    date_kinds = Counter(parsed_date(row["defect_date"])[1] for row in raw)
    ambiguous = [row for row in raw if parsed_date(row["defect_date"])[1].startswith("ambiguous")]
    return {
        "raw_dates": dict(date_kinds),
        "raw_ambiguous_total": len(ambiguous),
        "raw_ambiguous_dm_outside_jan_june": sum(
            parsed_date(row["defect_date"], "ambiguous_dm")[0].month > 6 for row in ambiguous
        ),
        "raw_invalid_date_count": 0,
        "raw_outside_2024_ids": sorted(
            row["defect_id"] for row in raw if parsed_date(row["defect_date"])[0].year != 2024
        ),
        "raw_orphans": [
            {"source_row": number, "defect_id": row["defect_id"], "product_id": row["product_id"]}
            for number, row in enumerate(raw, 1)
            if row["product_id"] not in products
        ],
        "canonical_orphan_ids": [
            event["defect_id"] for event in events if event["product_id"] not in products
        ],
        "canonical_orphan_product_ids": [
            event["product_id"] for event in events if event["product_id"] not in products
        ],
        "raw_cost_null_count": sum(row["repair_cost"] is None for row in raw),
        "raw_cost_negative_ids": sorted(
            row["defect_id"]
            for row in raw
            if row["repair_cost"] is not None and row["repair_cost"] < 0
        ),
        "raw_cost_ten_count": sum(row["repair_cost"] == 10 for row in raw),
        "raw_cost_zero_count": sum(row["repair_cost"] == 0 for row in raw),
        "canonical_cost_ten_count": sum(event["repair_cost"] == 10 for event in events),
        "raw_label_trim_rows": {
            field: sum(
                isinstance(row[field], str) and row[field] != row[field].strip() for row in raw
            )
            for field in FIELDS
        },
        "raw_severity_case_change_count": sum(
            row["severity"].strip() != row["severity"].strip().title() for row in raw
        ),
        "raw_labels": {
            field: dict(Counter(row[field] for row in raw))
            for field in ("defect_type", "severity", "defect_location", "inspection_method")
        },
        "canonical_date_kinds": dict(Counter(event["date_kind"] for event in events)),
    }


def months_between(start: str, end: str) -> list[str]:
    year, month = map(int, start.split("-"))
    stop = tuple(map(int, end.split("-")))
    result = []
    while (year, month) <= stop:
        result.append(f"{year:04}-{month:02}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def scoped(
    events: list[dict[str, Any]], products: dict[int, dict[str, Any]], scope: dict[str, str]
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], list[str]]:
    months = months_between(scope.get("start_month", "2024-01"), scope.get("end_month", "2024-06"))
    cohort = {
        key: product
        for key, product in products.items()
        if all(
            not scope.get(field) or str(product[field]) == scope[field]
            for field in ("production_line", "category", "product_id")
        )
    }
    selected = []
    for original in events:
        event = dict(original)
        event["defect_date"], _ = parsed_date(event["raw_date"], scope.get("scenario", ""))
        if event["defect_date"].strftime("%Y-%m") not in months:
            continue
        if scope.get("product_id") and str(event["product_id"]) != scope["product_id"]:
            continue
        product = products.get(event["product_id"], {})
        if any(
            scope.get(field) and product.get(field) != scope[field]
            for field in ("production_line", "category")
        ):
            continue
        if any(
            scope.get(field) and event[field] != scope[field]
            for field in ("severity", "defect_type", "defect_location", "inspection_method")
        ):
            continue
        selected.append(event)
    return selected, cohort, months


def scope_metrics(
    events: list[dict[str, Any]], products: dict[int, dict[str, Any]], scope: dict[str, str]
) -> dict[str, Any]:
    selected, cohort, months = scoped(events, products, scope)
    eligible = [event for event in selected if event["product_id"] in cohort]
    monthly_output = sum(product["monthly_output"] for product in cohort.values())
    denominator = monthly_output * len(months)
    event_products = {event["product_id"] for event in eligible}
    financial = cost_stats(selected, scope.get("scenario", ""))
    by_month = []
    for month in months:
        subset = [event for event in selected if event["defect_date"].strftime("%Y-%m") == month]
        linked = [event for event in subset if event["product_id"] in cohort]
        by_month.append(
            {
                "month": month,
                "event_count": len(subset),
                "eligible_event_count": len(linked),
                "reference_output": monthly_output,
                "events_per_1000_output": 1000 * len(linked) / monthly_output
                if monthly_output
                else None,
                "critical_count": sum(event["severity"] == "Critical" for event in subset),
                "zero_event_product_ids": sorted(
                    set(cohort) - {event["product_id"] for event in linked}
                ),
                "costs": cost_stats(subset, scope.get("scenario", "")),
            }
        )
    locations = []
    for location in sorted({event["defect_location"] for event in selected}):
        subset = [event for event in selected if event["defect_location"] == location]
        counts = Counter(event["severity"] for event in subset)
        locations.append(
            {
                "location": location,
                "event_count": len(subset),
                "share_of_all_events_pct": 100 * len(subset) / len(selected) if selected else None,
                "severity_counts": {severity: counts[severity] for severity in SEVERITIES},
                "severity_within_location_pct": {
                    severity: 100 * counts[severity] / len(subset) for severity in SEVERITIES
                },
                "costs": cost_stats(subset, scope.get("scenario", "")),
            }
        )
    dimensions: dict[str, list[dict[str, Any]]] = {}
    for dimension in ("production_line", "category", "product_id"):
        rows = []
        values = sorted(
            {product[dimension] for product in cohort.values()},
            key=lambda value: value if dimension == "product_id" else str(value),
        )
        for value in values:
            population = {
                key: product for key, product in cohort.items() if product[dimension] == value
            }
            subset = [event for event in eligible if event["product_id"] in population]
            output = sum(product["monthly_output"] for product in population.values()) * len(months)
            rows.append(
                {
                    "key": value,
                    "event_count": len(subset),
                    "reference_output": output,
                    "events_per_1000_output": 1000 * len(subset) / output if output else None,
                    "critical_count": sum(event["severity"] == "Critical" for event in subset),
                    "costs": cost_stats(subset, scope.get("scenario", "")),
                }
            )
        dimensions[dimension] = rows
    return {
        "scope": {"start_month": "2024-01", "end_month": "2024-06", **scope},
        "event_count": len(selected),
        "eligible_event_count": len(eligible),
        "orphan_event_count": len(selected) - len(eligible),
        "critical_count": sum(event["severity"] == "Critical" for event in selected),
        "critical_share_pct": 100
        * sum(event["severity"] == "Critical" for event in selected)
        / len(selected)
        if selected
        else None,
        "cohort_product_count": len(cohort),
        "zero_event_product_ids": sorted(set(cohort) - event_products),
        "month_count": len(months),
        "monthly_reference_output": monthly_output,
        "reference_output": denominator,
        "events_per_1000_output": 1000 * len(eligible) / denominator if denominator else None,
        "costs": financial,
        "monthly": by_month,
        "locations": locations,
        "dimension_groups": dimensions,
        "severity_counts": dict(Counter(event["severity"] for event in selected)),
        "inspection_method_counts": dict(Counter(event["inspection_method"] for event in selected)),
        "defect_type_counts": dict(Counter(event["defect_type"] for event in selected)),
        "event_ids": sorted(event["defect_id"] for event in selected),
    }


def association(locations: list[dict[str, Any]]) -> dict[str, Any]:
    counts = [row["severity_counts"] for row in locations]
    total = sum(sum(row.values()) for row in counts)
    column_totals = {severity: sum(row[severity] for row in counts) for severity in SEVERITIES}
    active_columns = [severity for severity in SEVERITIES if column_totals[severity]]
    degrees = (len(counts) - 1) * (len(active_columns) - 1)
    if not total or degrees <= 0:
        return {"available": False}
    chi2, expected_counts = 0.0, []
    for row in counts:
        row_total = sum(row.values())
        for severity in active_columns:
            expected = row_total * column_totals[severity] / total
            expected_counts.append(expected)
            chi2 += (row[severity] - expected) ** 2 / expected
    result = {
        "available": True,
        "chi2": chi2,
        "degrees_of_freedom": degrees,
        "cramers_v": math.sqrt(chi2 / (total * min(len(counts) - 1, len(active_columns) - 1))),
        "minimum_expected": min(expected_counts),
        "low_expected_cell_pct": 100
        * sum(value < 5 for value in expected_counts)
        / len(expected_counts),
    }
    if degrees % 2 == 0:
        x = chi2 / 2
        result["p_value"] = math.exp(-x) * sum(
            x**k / math.factorial(k) for k in range(degrees // 2)
        )
    return result


def record_projection(events: list[dict[str, Any]], scenario: str = "") -> list[dict[str, Any]]:
    return [
        {
            **{field: event[field] for field in FIELDS if field != "repair_cost"},
            "repair_cost": event["repair_cost"]
            if event["repair_cost"] is not None
            and event["repair_cost"] >= 0
            and not (scenario == "exclude_cost_10" and event["repair_cost"] == 10)
            else None,
        }
        for event in sorted(events, key=lambda item: item["defect_id"])
    ]


def value_hash(value: Any) -> str:
    return digest(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=json_value).encode("utf-8")
    )


def distribution_stats(events: list[dict[str, Any]], scenario: str = "") -> dict[str, Any]:
    values = sorted(
        event["repair_cost"]
        for event in events
        if event["repair_cost"] is not None
        and event["repair_cost"] >= 0
        and not (scenario == "exclude_cost_10" and event["repair_cost"] == 10)
    )
    fractions = {
        "min": "0",
        "p25": ".25",
        "median": ".5",
        "p75": ".75",
        "p90": ".9",
        "p95": ".95",
        "max": "1",
    }
    quantiles = {}
    for name, fraction in fractions.items():
        position = Decimal(fraction) * (len(values) - 1)
        lower = int(position)
        quantiles[name] = (
            values[lower]
            + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (position - lower)
            if values
            else None
        )
    histogram = []
    for lower, upper in ((0, 100), (100, 250), (250, 500), (500, 750), (750, 1000), (1000, None)):
        members = [value for value in values if value >= lower and (upper is None or value < upper)]
        histogram.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(members),
                "known_cost": sum(members, Decimal(0)),
                "share_of_known_cost_events_pct": 100 * len(members) / len(values)
                if values
                else None,
            }
        )
    return {"quantiles": quantiles, "histogram": histogram}


def supplemental_derive(
    raw: list[dict[str, Any]], master: list[dict[str, str]], initial: dict[str, Any]
) -> dict[str, Any]:
    products, _ = masters(master)
    events, _ = independent_clean(raw)
    scopes = {
        "empty_product_january_critical": {
            "start_month": "2024-01",
            "end_month": "2024-01",
            "product_id": "1",
            "severity": "Critical",
        },
        "orphan_product_101": {"product_id": "101"},
        "manual_testing": {"inspection_method": "Manual Testing"},
        "functional": {"defect_type": "Functional"},
    }
    scopes.update(
        {
            f"category4_line{number}": {
                "category": "Category 4",
                "production_line": f"Line-{number}",
            }
            for number in range(1, 5)
        }
    )
    scopes.update(
        {
            f"line3_critical_{scenario}": {
                "production_line": "Line-3",
                "severity": "Critical",
                "scenario": scenario,
            }
            for scenario in ("exclude_cost_10", "ambiguous_dm", "year_2024")
        }
    )
    extra_cases = {name: scope_metrics(events, products, scope) for name, scope in scopes.items()}
    all_cases = {**initial["cases"], **extra_cases}
    details = {}
    for name, case in all_cases.items():
        selected, cohort, months = scoped(events, products, case["scope"])
        cost_groups = {}
        for field in ("defect_location", "severity", "defect_type"):
            cost_groups[field] = [
                {
                    "key": value,
                    "event_count": sum(event[field] == value for event in selected),
                    "costs": cost_stats(
                        [event for event in selected if event[field] == value],
                        case["scope"].get("scenario", ""),
                    ),
                }
                for value in sorted({event[field] for event in selected})
            ]
        details[name] = {
            "products_with_events": len({event["product_id"] for event in selected}),
            "location_severity_association": association(case["locations"]),
            "cost_groups": cost_groups,
            "record_projection_sha256": value_hash(
                record_projection(selected, case["scope"].get("scenario", ""))
            ),
            "cost_distribution": distribution_stats(selected, case["scope"].get("scenario", "")),
            "line_category": [
                {
                    "production_line": line,
                    "category": category,
                    "event_count": sum(
                        products.get(event["product_id"], {}).get("production_line") == line
                        and products.get(event["product_id"], {}).get("category") == category
                        for event in selected
                    ),
                    "reference_output": len(months)
                    * sum(
                        product["monthly_output"]
                        for product in cohort.values()
                        if product["production_line"] == line and product["category"] == category
                    ),
                }
                for line, category in sorted(
                    {
                        (product["production_line"], product["category"])
                        for product in cohort.values()
                    }
                )
            ],
        }
    canonical_events, _, _ = scoped(events, products, {})
    first, last = date(2024, 1, 1), date(2024, 6, 30)
    dates_with_events = {event["defect_date"] for event in canonical_events}
    dates_without = [
        first + timedelta(days=offset)
        for offset in range((last - first).days + 1)
        if first + timedelta(days=offset) not in dates_with_events
    ]
    cluster_scenarios = {}
    for scenario in ("", "exclude_cost_10", "ambiguous_dm", "year_2024"):
        selected, _, _ = scoped(events, products, {"scenario": scenario})
        cluster = [event for event in selected if 81 <= event["product_id"] <= 85]
        ranked = sorted(
            scope_metrics(events, products, {"scenario": scenario})["dimension_groups"][
                "product_id"
            ],
            key=lambda row: (-(row["events_per_1000_output"] or 0), row["key"]),
        )
        cluster_scenarios[scenario or "canonical"] = {
            "event_count": len(cluster),
            "critical_count": sum(event["severity"] == "Critical" for event in cluster),
            "costs": cost_stats(cluster, scenario),
            "intensity_ranks": {
                str(row["key"]): rank
                for rank, row in enumerate(ranked, 1)
                if 81 <= row["key"] <= 85
            },
        }
    return {
        "independence_note": "Initial expectations were frozen before the baseline API schema was inspected. These extra raw-derived boundary cases were frozen before their scoped API outcomes were observed.",
        "cases": extra_cases,
        "case_details": details,
        "cluster_scenarios": cluster_scenarios,
        "canonical_calendar": {
            "calendar_days": (last - first).days + 1,
            "days_with_events": len(dates_with_events),
            "days_without_events": len(dates_without),
            "dates_without_events": [value.isoformat() for value in dates_without],
            "last_observed_day_each_month": {
                month: max(
                    event["defect_date"].day
                    for event in canonical_events
                    if event["defect_date"].strftime("%Y-%m") == month
                )
                for month in months_between("2024-01", "2024-06")
            },
        },
    }


def derive(raw: list[dict[str, Any]], master: list[dict[str, str]]) -> dict[str, Any]:
    products, master_quality = masters(master)
    events, duplicate_quality = independent_clean(raw)
    case_scopes = {
        "canonical": {},
        "january": {"start_month": "2024-01", "end_month": "2024-01"},
        "june": {"start_month": "2024-06", "end_month": "2024-06"},
        "critical": {"severity": "Critical"},
        "january_critical": {
            "start_month": "2024-01",
            "end_month": "2024-01",
            "severity": "Critical",
        },
        "line3": {"production_line": "Line-3"},
        "line3_critical": {"production_line": "Line-3", "severity": "Critical"},
        "category4": {"category": "Category 4"},
        "component": {"defect_location": "Component"},
        "exclude_cost_10": {"scenario": "exclude_cost_10"},
        "ambiguous_dm": {"scenario": "ambiguous_dm"},
        "year_2024": {"scenario": "year_2024"},
    }
    case_scopes.update(
        {f"product_{product_id}": {"product_id": str(product_id)} for product_id in range(81, 86)}
    )
    cases = {name: scope_metrics(events, products, scope) for name, scope in case_scopes.items()}
    cluster_events = [
        event
        for event in events
        if event["product_id"] in range(81, 86) and event["defect_date"].year == 2024
    ]
    cluster = {
        "product_ids": list(range(81, 86)),
        "event_count": len(cluster_events),
        "share_of_canonical_events_pct": 100
        * len(cluster_events)
        / cases["canonical"]["event_count"],
        "monthly_reference_output": sum(products[key]["monthly_output"] for key in range(81, 86)),
        "reference_output": 6 * sum(products[key]["monthly_output"] for key in range(81, 86)),
        "severity_counts": dict(Counter(event["severity"] for event in cluster_events)),
        "monthly_counts": dict(
            sorted(
                Counter(event["defect_date"].strftime("%Y-%m") for event in cluster_events).items()
            )
        ),
        "costs": cost_stats(cluster_events),
    }
    cluster["events_per_1000_output"] = 1000 * cluster["event_count"] / cluster["reference_output"]
    jan = cases["january"]["event_count"]
    june = cases["june"]["event_count"]
    jan_counts = Counter(
        event["product_id"] for event in scoped(events, products, case_scopes["january"])[0]
    )
    june_counts = Counter(
        event["product_id"] for event in scoped(events, products, case_scopes["june"])[0]
    )
    return {
        "policy_version": "canonical-v2",
        "implementation": "Independent Python standard library; no application imports",
        "master_quality": master_quality,
        "duplicate_quality": duplicate_quality,
        "data_quality": quality_scan(raw, events, products),
        "cases": cases,
        "product_81_85_cluster": cluster,
        "january_to_june": {
            "absolute_change": june - jan,
            "relative_change_pct": 100 * (june - jan) / jan,
            "product_contributions": [
                {
                    "product_id": key,
                    "january": jan_counts[key],
                    "june": june_counts[key],
                    "change": june_counts[key] - jan_counts[key],
                }
                for key in sorted(set(jan_counts) | set(june_counts))
            ],
        },
        "limits": [
            "Reference monthly output is constant by assumption; no actual monthly output history is present.",
            "Events do not identify individual defective units.",
            "Line/category overlap prevents a causal interpretation of aggregate line ranking.",
            "Inspection method counts lack inspected-unit and ground-truth denominators.",
        ],
    }


def compare_values(expected: Any, actual: Any, path: str = "") -> list[dict[str, Any]]:
    differences = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            current = f"{path}.{key}" if path else key
            if key not in expected or key not in actual:
                differences.append(
                    {
                        "path": current,
                        "expected": expected.get(key),
                        "actual": actual.get(key),
                        "reason": "missing key",
                    }
                )
            else:
                differences.extend(compare_values(expected[key], actual[key], current))
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            differences.append(
                {"path": path, "expected_length": len(expected), "actual_length": len(actual)}
            )
        for index, (left, right) in enumerate(zip(expected, actual, strict=False)):
            differences.extend(compare_values(left, right, f"{path}[{index}]"))
    elif isinstance(expected, (int, float, Decimal)) and isinstance(actual, (int, float, Decimal)):
        if not math.isclose(float(expected), float(actual), rel_tol=1e-10, abs_tol=1e-8):
            differences.append({"path": path, "expected": expected, "actual": actual})
    elif expected != actual:
        differences.append({"path": path, "expected": expected, "actual": actual})
    return differences


def expected_kpis(case: dict[str, Any]) -> dict[str, Any]:
    costs = case["costs"]
    return {
        "event_count": case["event_count"],
        "linked_event_count": case["eligible_event_count"],
        "eligible_event_count": case["eligible_event_count"],
        "unmatched_event_count": case["orphan_event_count"],
        "critical_count": case["critical_count"],
        "critical_share_pct": case["critical_share_pct"],
        "reference_output": case["reference_output"] or None,
        "reference_intensity_per_1000": case["events_per_1000_output"],
        "cohort_product_count": case["cohort_product_count"],
        "cohort_products_without_events": len(case["zero_event_product_ids"]),
        "cohort_products_with_events": case["cohort_product_count"]
        - len(case["zero_event_product_ids"]),
        "month_count": case["month_count"],
        "cost_known_count": costs["valid_count"],
        "cost_unknown_count": costs["unknown_count"],
        "cost_coverage_pct": costs["coverage_pct"],
        "cost_available": bool(costs["valid_count"]),
        "average_known_repair_cost": costs["mean_valid"],
        "known_repair_cost": costs["known_total"],
    }


def group_kpis(group: dict[str, Any]) -> dict[str, Any]:
    costs = group["costs"]
    return {
        "event_count": group["event_count"],
        "critical_count": group["critical_count"],
        "reference_output": group["reference_output"] or None,
        "reference_intensity_per_1000": group["events_per_1000_output"],
        "cost_known_count": costs["valid_count"],
        "cost_unknown_count": costs["unknown_count"],
        "cost_coverage_pct": costs["coverage_pct"],
        "cost_available": bool(costs["valid_count"]),
        "average_known_repair_cost": costs["mean_valid"],
        "known_repair_cost": costs["known_total"],
    }


def check_fields(
    expected: dict[str, Any], actual: dict[str, Any], path: str, differences: list[dict[str, Any]]
) -> None:
    differences.extend(
        compare_values(expected, {key: actual[key] for key in expected if key in actual}, path)
    )


def api_reconciliation(
    api_base: str, expected: dict[str, Any], supplemental: dict[str, Any], sources: dict[str, Any]
) -> dict[str, Any]:
    all_cases = {**expected["cases"], **supplemental["cases"]}
    results, canonical_snapshot, dataset_version = [], None, None
    for name, case in all_cases.items():
        item: dict[str, Any] = {"name": name, "scope": case["scope"], "status": "Not run"}
        differences: list[dict[str, Any]] = []
        query = dict(case["scope"])
        if canonical_snapshot:
            query["snapshot_id"] = canonical_snapshot
        url = api_base.rstrip("/") + "/api/dashboard?" + urlencode(query)
        item["request_url"] = url
        try:
            content, receipt = fetch(url)
            payload = json.loads(content)
            analysis, context = payload["analysis"], payload["context"]
            check_fields({"stale": False}, payload["status"], "source_status", differences)
            if canonical_snapshot is None:
                canonical_snapshot = context["snapshot_id"]
                dataset_version = context.get("dataset_version")
            item["http"] = receipt
            item["context"] = context
            check_fields(expected_kpis(case), analysis["kpis"], "kpis", differences)
            check_fields(
                {
                    "products_with_events": supplemental["case_details"][name][
                        "products_with_events"
                    ]
                },
                analysis["kpis"],
                "kpis",
                differences,
            )
            check_fields(
                {
                    "source_sha256": sources["defects"]["sha256"],
                    "master_sha256": sources["master_local"]["sha256"],
                    "policy_version": "canonical-v2",
                    "scenario": case["scope"].get("scenario", ""),
                },
                context,
                "context",
                differences,
            )
            if (
                not isinstance(context.get("dataset_version"), str)
                or not context["dataset_version"]
            ):
                differences.append(
                    {
                        "path": "context.dataset_version",
                        "expected": "nonempty dataset version",
                        "actual": context.get("dataset_version"),
                    }
                )
            elif context["dataset_version"] != dataset_version:
                differences.append(
                    {
                        "path": "context.dataset_version",
                        "expected": dataset_version,
                        "actual": context["dataset_version"],
                    }
                )
            check_fields(
                {
                    "policy_version": "canonical-v2",
                    "dataset_version": dataset_version,
                    "scenario": case["scope"].get("scenario", ""),
                },
                analysis["metadata"],
                "metadata",
                differences,
            )
            check_fields(
                {**case["scope"], "scenario": case["scope"].get("scenario", "")},
                context["filters"],
                "context.filters",
                differences,
            )
            monthly_by_key = {row["month"]: row for row in analysis["monthly"]}
            differences.extend(
                compare_values(
                    [row["month"] for row in case["monthly"]],
                    sorted(monthly_by_key),
                    "monthly_axis",
                )
            )
            for month in case["monthly"]:
                check_fields(
                    group_kpis(month),
                    monthly_by_key.get(month["month"], {}),
                    f"monthly.{month['month']}",
                    differences,
                )
            for dimension, actual_rows, key in (
                ("production_line", analysis["lines"], "production_line"),
                ("product_id", analysis["products"]["all"], "product_id"),
            ):
                actual_groups = {row[key]: row for row in actual_rows}
                if dimension == "product_id":
                    differences.extend(
                        compare_values(
                            sorted(group["key"] for group in case["dimension_groups"][dimension]),
                            sorted(actual_groups),
                            "product_cohort_ids",
                        )
                    )
                for group in case["dimension_groups"][dimension]:
                    check_fields(
                        group_kpis(group),
                        actual_groups.get(group["key"], {}),
                        f"{dimension}.{group['key']}",
                        differences,
                    )
            location_cells = {
                (row["location"], row["severity"]): row
                for row in analysis["location_severity"]["cells"]
            }
            declared_severities = set(analysis["location_severity"]["severities"])
            expected_positive_severities = {
                severity
                for location in case["locations"]
                for severity in SEVERITIES
                if location["severity_counts"][severity]
            }
            if not expected_positive_severities <= declared_severities <= set(SEVERITIES):
                differences.append(
                    {
                        "path": "location_severity.severities",
                        "expected_positive_axes": sorted(expected_positive_severities),
                        "actual_axes": sorted(declared_severities),
                    }
                )
            for location in case["locations"]:
                for severity in SEVERITIES:
                    if (
                        severity not in declared_severities
                        and location["severity_counts"][severity] == 0
                    ):
                        continue
                    check_fields(
                        {
                            "count": location["severity_counts"][severity],
                            "row_pct": location["severity_within_location_pct"][severity],
                        },
                        location_cells.get((location["location"], severity), {}),
                        f"location_severity.{location['location']}.{severity}",
                        differences,
                    )
            check_fields(
                supplemental["case_details"][name]["location_severity_association"],
                analysis["location_severity"]["test"],
                "association",
                differences,
            )
            details = supplemental["case_details"][name]
            distribution = analysis["cost_distribution"]
            check_fields(
                details["cost_distribution"]["quantiles"],
                distribution["quantiles"],
                "cost_distribution.quantiles",
                differences,
            )
            actual_bins = [
                {
                    key: row[key]
                    for key in (
                        "lower",
                        "upper",
                        "count",
                        "known_cost",
                        "share_of_known_cost_events_pct",
                    )
                    if key in row
                }
                for row in distribution["histogram"]
            ]
            differences.extend(
                compare_values(
                    details["cost_distribution"]["histogram"],
                    actual_bins,
                    "cost_distribution.histogram",
                )
            )
            unknown_status = {
                key: value
                for key, value in {
                    "missing": case["costs"]["missing_count"],
                    "negative": case["costs"]["negative_count"],
                    "scenario_excluded_cost_10": case["costs"]["scenario_excluded_count"],
                }.items()
                if value
            }
            differences.extend(
                compare_values(
                    unknown_status,
                    distribution["unknown_by_status"],
                    "cost_distribution.unknown_by_status",
                )
            )
            check_fields(
                {
                    key: value
                    for key, value in expected_kpis(case).items()
                    if key
                    in (
                        "event_count",
                        "cost_known_count",
                        "cost_unknown_count",
                        "known_repair_cost",
                        "cost_coverage_pct",
                        "average_known_repair_cost",
                    )
                },
                distribution,
                "cost_distribution",
                differences,
            )
            actual_line_category = {
                (row["production_line"], row["category"]): row for row in analysis["line_category"]
            }
            for group in details["line_category"]:
                required = {
                    **group,
                    "reference_intensity_per_1000": 1000
                    * group["event_count"]
                    / group["reference_output"]
                    if group["reference_output"]
                    else None,
                }
                check_fields(
                    required,
                    actual_line_category.get((group["production_line"], group["category"]), {}),
                    f"line_category.{group['production_line']}.{group['category']}",
                    differences,
                )
            for dimension, api_key in (
                ("defect_location", "by_location"),
                ("severity", "by_severity"),
                ("defect_type", "by_type"),
            ):
                actual_groups = {row["name"]: row for row in analysis["costs"][api_key]}
                for group in supplemental["case_details"][name]["cost_groups"][dimension]:
                    costs = group["costs"]
                    required = {
                        "event_count": group["event_count"],
                        "cost_known_count": costs["valid_count"],
                        "cost_unknown_count": costs["unknown_count"],
                        "cost_coverage_pct": costs["coverage_pct"],
                        "known_repair_cost": costs["known_total"],
                    }
                    check_fields(
                        required,
                        actual_groups.get(group["key"], {}),
                        f"costs.{api_key}.{group['key']}",
                        differences,
                    )
            item["differences"] = differences
            item["status"] = "Failed" if differences else "Passed"
            if name in (
                "canonical",
                "exclude_cost_10",
                "ambiguous_dm",
                "year_2024",
                "empty_product_january_critical",
                "orphan_product_101",
            ):
                records: list[dict[str, Any]] = []
                pages: list[dict[str, Any]] = []
                page, page_count = 1, 1
                while page <= page_count:
                    record_url = (
                        api_base.rstrip("/")
                        + payload["links"]["records"]
                        + "?"
                        + urlencode(
                            {
                                "page": page,
                                "page_size": 100,
                                "sort_by": "defect_id",
                                "sort_order": "asc",
                            }
                        )
                    )
                    body, record_receipt = fetch(record_url)
                    record_payload = json.loads(body)
                    check_fields(
                        {"context_id": context["context_id"]},
                        record_payload["context"],
                        f"record_page.{page}.context",
                        differences,
                    )
                    page_count = record_payload["page_count"]
                    records.extend(
                        {field: row[field] for field in FIELDS} for row in record_payload["rows"]
                    )
                    pages.append(
                        {
                            "page": page,
                            "sha256": record_receipt["sha256"],
                            "row_count": len(record_payload["rows"]),
                        }
                    )
                    page += 1
                actual_hash = value_hash(sorted(records, key=lambda row: row["defect_id"]))
                expected_hash = supplemental["case_details"][name]["record_projection_sha256"]
                item["record_readback"] = {
                    "status": "Passed" if actual_hash == expected_hash else "Failed",
                    "record_count": len(records),
                    "expected_sha256": expected_hash,
                    "actual_sha256": actual_hash,
                    "pages": pages,
                }
                if actual_hash != expected_hash:
                    item["status"] = "Failed"
                csv_body, csv_receipt = fetch(api_base.rstrip("/") + payload["links"]["export_csv"])
                exported = []
                for row in csv.DictReader(io.StringIO(csv_body.decode("utf-8-sig"))):
                    exported.append(
                        {
                            **{field: row[field] for field in FIELDS},
                            "defect_id": int(row["defect_id"]),
                            "product_id": int(row["product_id"]),
                            "repair_cost": float(row["repair_cost"])
                            if row["repair_cost"]
                            else None,
                        }
                    )
                export_hash = value_hash(sorted(exported, key=lambda row: row["defect_id"]))
                item["csv_readback"] = {
                    "status": "Passed" if export_hash == expected_hash else "Failed",
                    "row_count": len(exported),
                    "expected_projection_sha256": expected_hash,
                    "actual_projection_sha256": export_hash,
                    "http": csv_receipt,
                }
                if export_hash != expected_hash:
                    item["status"] = "Failed"
            if differences:
                item["status"] = "Failed"
        except Exception as error:
            item["status"] = "Failed"
            item["error"] = {"type": type(error).__name__, "message": str(error)}
        results.append(item)
    return {
        "name": "application_api_reconciliation",
        "status": "Passed" if all(item["status"] == "Passed" for item in results) else "Failed",
        "api_base": api_base,
        "case_count": len(results),
        "passed_cases": sum(item["status"] == "Passed" for item in results),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="Freeze independent expectations from live sources before API comparison",
    )
    parser.add_argument(
        "--freeze-supplemental",
        action="store_true",
        help="Freeze additional raw-derived boundary cases before scoped API comparison",
    )
    parser.add_argument("--output", type=Path, default=RESULT_FILE)
    parser.add_argument("--api-base", default="", help="Optional running application origin")
    args = parser.parse_args()
    report: dict[str, Any] = {
        "started_at": utc_now(),
        "status": "Not run",
        "verifier": {
            "path": str(Path(__file__).resolve()),
            "sha256": digest(Path(__file__).read_bytes()),
            "initial_expected_sha256": digest(EXPECTED_FILE.read_bytes())
            if EXPECTED_FILE.exists()
            else None,
            "supplemental_expected_sha256": digest(SUPPLEMENTAL_FILE.read_bytes())
            if SUPPLEMENTAL_FILE.exists()
            else None,
        },
        "checks": [],
    }
    try:
        raw, master, sources = source_bundle()
        result = derive(raw, master)
        supplemental = supplemental_derive(raw, master, result)
        report["sources"] = sources
        report["independent"] = result
        report["supplemental"] = supplemental
        source_pass = all(
            (
                sources["commit_status"] == "Passed",
                sources["defects_match_supplied_commit"],
                sources["master_local_matches_remote"],
                sources["master_matches_supplied_commit"],
            )
        )
        report["checks"].append(
            {"name": "source_identity", "status": "Passed" if source_pass else "Failed"}
        )
        if args.freeze:
            if args.api_base:
                raise ValueError(
                    "Freeze must precede API comparison; run --freeze without --api-base"
                )
            if not source_pass:
                raise ValueError("Cannot freeze expectations after source identity drift")
            if EXPECTED_FILE.exists():
                frozen = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
                if compare_values(frozen["independent"], result):
                    raise ValueError(
                        "Refusing to overwrite frozen expectations with changed calculations"
                    )
            else:
                save_json(
                    EXPECTED_FILE,
                    {"frozen_at": utc_now(), "sources": sources, "independent": result},
                )
            report["checks"].append(
                {
                    "name": "independent_expected_frozen",
                    "status": "Passed",
                    "path": str(EXPECTED_FILE),
                }
            )
        elif EXPECTED_FILE.exists():
            frozen = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
            differences = compare_values(frozen["independent"], result)
            frozen_hashes = {
                key: value["sha256"]
                for key, value in frozen["sources"].items()
                if isinstance(value, dict) and "sha256" in value and key != "head"
            }
            current_hashes = {
                key: value["sha256"]
                for key, value in sources.items()
                if isinstance(value, dict) and "sha256" in value and key != "head"
            }
            source_differences = compare_values(frozen_hashes, current_hashes)
            report["checks"].append(
                {
                    "name": "frozen_independent_reproduction",
                    "status": "Failed" if differences or source_differences else "Passed",
                    "differences": differences,
                    "source_differences": source_differences,
                }
            )
        else:
            report["checks"].append(
                {
                    "name": "frozen_independent_reproduction",
                    "status": "Not run",
                    "reason": "No independent expected file has been frozen",
                }
            )
        if args.freeze_supplemental:
            if args.api_base:
                raise ValueError("Supplemental freeze must precede scoped API comparison")
            if not source_pass:
                raise ValueError(
                    "Cannot freeze supplemental expectations after source identity drift"
                )
            if SUPPLEMENTAL_FILE.exists():
                frozen = json.loads(SUPPLEMENTAL_FILE.read_text(encoding="utf-8"))
                if compare_values(frozen["supplemental"], supplemental):
                    raise ValueError(
                        "Refusing to overwrite supplemental expectations with changed calculations"
                    )
            else:
                save_json(
                    SUPPLEMENTAL_FILE,
                    {"frozen_at": utc_now(), "sources": sources, "supplemental": supplemental},
                )
            report["checks"].append(
                {
                    "name": "supplemental_expected_frozen",
                    "status": "Passed",
                    "path": str(SUPPLEMENTAL_FILE),
                }
            )
        elif SUPPLEMENTAL_FILE.exists():
            frozen = json.loads(SUPPLEMENTAL_FILE.read_text(encoding="utf-8"))
            differences = compare_values(frozen["supplemental"], supplemental)
            report["checks"].append(
                {
                    "name": "supplemental_independent_reproduction",
                    "status": "Failed" if differences else "Passed",
                    "differences": differences,
                }
            )
        if args.api_base:
            if not EXPECTED_FILE.exists() or not SUPPLEMENTAL_FILE.exists():
                raise ValueError(
                    "Freeze initial and supplemental expectations before API comparison"
                )
            if any(check["status"] == "Failed" for check in report["checks"]):
                report["checks"].append(
                    {
                        "name": "application_api_reconciliation",
                        "status": "Blocked",
                        "reason": "Source or independent expected-value drift invalidates the frozen comparison",
                    }
                )
            else:
                frozen_expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))[
                    "independent"
                ]
                frozen_supplemental = json.loads(SUPPLEMENTAL_FILE.read_text(encoding="utf-8"))[
                    "supplemental"
                ]
                report["checks"].append(
                    api_reconciliation(args.api_base, frozen_expected, frozen_supplemental, sources)
                )
        else:
            report["checks"].append(
                {
                    "name": "application_api_reconciliation",
                    "status": "Not run",
                    "reason": "No --api-base supplied",
                }
            )
        report["status"] = next(
            (
                status
                for status in ("Failed", "Blocked", "Not run")
                if any(check["status"] == status for check in report["checks"])
            ),
            "Passed",
        )
    except Exception as error:
        report["status"] = "Failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    report["completed_at"] = utc_now()
    save_json(args.output, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(args.output),
                "checks": [
                    {
                        key: check[key]
                        for key in ("name", "status", "case_count", "passed_cases")
                        if key in check
                    }
                    for check in report["checks"]
                ],
            },
            default=json_value,
            indent=2,
        )
    )
    return 1 if report["status"] == "Failed" else 0


if __name__ == "__main__":
    sys.exit(main())
