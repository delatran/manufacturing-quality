"""Independently rebuild a reviewed business-question oracle for one source.

Manual review only; pytest never calls this builder. From the repository root:

    .venv/Scripts/python.exe -B analyst/case_study/oracle_builder.py > business_questions.candidate.json

On macOS/Linux use .venv/bin/python. Compare the candidate with
knowledge/independent_oracle.json, inspect every changed answer,
and only then manually replace the reviewed fixture. Never update an expected
answer merely because the production application returns a different result.

This uses only the Python standard library and does not import production code.
The method follows the independent source audit: exact-copy removal, categorical
normalization, compatible duplicate reconciliation, conflict quarantine, explicit
dates, a unique-master left join, Decimal cost sums, and full-cohort output.
It is intentionally limited to the pinned case bytes, not a second production
cleaning pipeline. A changed source or an unexpected domain fails for re-audit.
HTTP JSON remains in memory. The only output is aggregate oracle JSON on stdout.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
SOURCE_URL = (
    "https://raw.githubusercontent.com/phongtdt/"
    "RFID_Data-Analyst_Case-Study-Oct-2026/main/mock-api/defect_records.json"
)
SOURCE_SHA256 = "863ca40ca37f639c064ce0bb66e679d7c3ab4ceca8cd79569d31310004728dc8"
MASTER_SHA256 = "8772e8f0d0fbe1bf66b259859055e1742b066f91a31777b9f5e4630f606b9a86"
LABELS = {
    "severity": ("Minor", "Moderate", "Critical"),
    "defect_type": ("Cosmetic", "Functional", "Structural"),
    "defect_location": ("Component", "Internal", "Surface"),
    "inspection_method": ("Automated Testing", "Manual Testing", "Visual Inspection"),
}
QUESTIONS = [
    (
        "full_scope",
        "What are total logged workload, observed cost and reference intensity for January-June?",
        {},
    ),
    (
        "critical_component",
        "Do Critical Component events retain the full master output denominator?",
        {"severity": "Critical", "defect_location": "Component"},
    ),
    (
        "product_81",
        "What are product 81 workload and known cost relative to its six-month reference output?",
        {"product_id": "81"},
    ),
    (
        "line_3",
        "What are Line-3 workload, observed cost coverage and reference intensity?",
        {"production_line": "Line-3"},
    ),
    (
        "empty_line_1_category_2",
        "Does Line-1 with Category 2 return an empty cohort and unavailable intensity?",
        {"production_line": "Line-1", "category": "Category 2"},
    ),
    (
        "unknown_product_104",
        "Is product 104 rejected after its only source variant is conflict-quarantined?",
        {"product_id": "104"},
    ),
    (
        "unmatched_product_105",
        "Does unmatched product 105 retain its event and cost without inventing output?",
        {"product_id": "105"},
    ),
    (
        "january_only",
        "What are January-only workload and cost coverage using one month of reference output?",
        {"start_month": "2024-01", "end_month": "2024-01"},
    ),
    (
        "june_only",
        "What are June-only workload and cost coverage using one month of reference output?",
        {"start_month": "2024-06", "end_month": "2024-06"},
    ),
    (
        "line_3_march_may",
        "What are Line-3 results from March through May using exactly three months of output?",
        {"production_line": "Line-3", "start_month": "2024-03", "end_month": "2024-05"},
    ),
    (
        "manual_testing",
        "What workload and cost did Manual Testing record while retaining the full output cohort?",
        {"inspection_method": "Manual Testing"},
    ),
]


def id_digest(ids):
    """Hash ascending decimal event IDs joined by commas with no newline."""
    return hashlib.sha256(",".join(str(value) for value in sorted(ids)).encode("ascii")).hexdigest()


def build_reference():
    request = Request(SOURCE_URL, headers={"User-Agent": "CaseStudyIndependentQuestionAudit/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = response.read()
        assert response.status == 200
    fetched_at = datetime.now(UTC).isoformat()
    master_bytes = (ROOT / "data/master_data.csv").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == SOURCE_SHA256, (
        "Source changed; independently re-audit first"
    )
    assert hashlib.sha256(master_bytes).hexdigest() == MASTER_SHA256, (
        "Master changed; independently re-audit first"
    )
    raw = json.loads(payload)
    master_rows = list(csv.DictReader(io.StringIO(master_bytes.decode("utf-8-sig"))))
    master = {int(row["product_id"]): row for row in master_rows}
    assert len(master) == len(master_rows) == 100, "The master key must remain unique"

    # Independent record dictionaries, not application Event/Snapshot objects.
    seen_raw, seen_canonical, grouped = set(), set(), defaultdict(list)
    removed_exact = removed_canonical = 0
    for original in raw:
        key = json.dumps(original, sort_keys=True, ensure_ascii=False)
        if key in seen_raw:
            removed_exact += 1
            continue
        seen_raw.add(key)
        row: dict[str, Any] = {
            field: unicodedata.normalize("NFKC", value).strip() if isinstance(value, str) else value
            for field, value in original.items()
        }
        for field, labels in LABELS.items():
            row[field] = {label.casefold(): label for label in labels}[row[field].casefold()]
        key = json.dumps(row, sort_keys=True, ensure_ascii=False)
        if key in seen_canonical:
            removed_canonical += 1
            continue
        seen_canonical.add(key)
        grouped[row["defect_id"]].append(row)

    kept: list[dict[str, Any]] = []
    quarantined, coalesced = [], []
    for record_id, group in grouped.items():
        variants = {
            field: {
                json.dumps(row[field], sort_keys=True) for row in group if row[field] is not None
            }
            for field in group[0]
        }
        if any(len(values) > 1 for values in variants.values()):
            quarantined.extend([record_id] * len(group))
            continue
        row = {
            field: next((item[field] for item in group if item[field] is not None), None)
            for field in group[0]
        }
        coalesced.extend([record_id] * (len(group) - 1))
        text = row["defect_date"]
        fmt = (
            "%Y-%m-%d"
            if "-" in text
            else "%d/%m/%Y"
            if int(text.split("/")[0]) > 12
            else "%m/%d/%Y"
        )
        row["month"] = datetime.strptime(text, fmt).strftime("%Y-%m")
        row["cost"] = (
            Decimal(str(row["repair_cost"]))
            if row["repair_cost"] is not None and row["repair_cost"] >= 0
            else None
        )
        row["master"] = master.get(row["product_id"])
        kept.append(row)
    events = [row for row in kept if "2024-01" <= row["month"] <= "2024-06"]
    assert (
        len(raw),
        removed_exact,
        removed_canonical,
        len(coalesced),
        len(quarantined),
        len(kept),
        len(events),
    ) == (1090, 12, 1, 1, 2, 1074, 1071)
    assert quarantined == [133, 133] and coalesced == [152]
    assert sum((row["cost"] for row in events if row["cost"] is not None), Decimal(0)) == Decimal(
        "508864.39"
    )
    available_products = set(master) | {row["product_id"] for row in kept}
    assert 104 not in available_products and 105 in available_products and 105 not in master

    cases = []
    for case_id, question, selected_filters in QUESTIONS:
        filters = {"start_month": "2024-01", "end_month": "2024-06", **selected_filters}
        if filters.get("product_id") and int(filters["product_id"]) not in available_products:
            cases.append(
                {
                    "id": case_id,
                    "question": question,
                    "filters": selected_filters,
                    "expected_status": 422,
                    "expected_detail": f"Unknown product_id: {filters['product_id']}",
                }
            )
            continue
        months = int(filters["end_month"][-2:]) - int(filters["start_month"][-2:]) + 1
        cohort = [
            row
            for row in master.values()
            if all(
                not filters.get(field) or row[field] == filters[field]
                for field in ("production_line", "category", "product_id")
            )
        ]
        selected = []
        for row in events:
            if not filters["start_month"] <= row["month"] <= filters["end_month"]:
                continue
            if any(
                filters.get(field)
                and (str(row["product_id"]) if field == "product_id" else row[field])
                != filters[field]
                for field in (
                    "product_id",
                    "severity",
                    "defect_type",
                    "defect_location",
                    "inspection_method",
                )
            ):
                continue
            if any(
                filters.get(field) and (row["master"] or {}).get(field) != filters[field]
                for field in ("production_line", "category")
            ):
                continue
            selected.append(row)
        known = [row["cost"] for row in selected if row["cost"] is not None]
        linked = sum(row["master"] is not None for row in selected)
        output = sum(int(row["monthly_output"]) for row in cohort) * months if cohort else None
        expected = {
            "event_count": len(selected),
            "linked_event_count": linked,
            "unmatched_event_count": len(selected) - linked,
            "critical_count": sum(row["severity"] == "Critical" for row in selected),
            "cost_known_count": len(known),
            "cost_unknown_count": len(selected) - len(known),
            "known_repair_cost": f"{sum(known, Decimal(0)):.2f}",
            "cost_coverage_pct": len(known) / len(selected) * 100 if selected else None,
            "cost_status": "empty"
            if not selected
            else "unavailable"
            if not known
            else "complete"
            if len(known) == len(selected)
            else "partial",
            "cohort_product_count": len(cohort),
            "month_count": months,
            "reference_output": output,
            "reference_intensity_per_1000": linked / output * 1000 if output else None,
            "defect_ids_sha256": id_digest(row["defect_id"] for row in selected),
        }
        cases.append(
            {
                "id": case_id,
                "question": question,
                "filters": selected_filters,
                "expected_status": 200,
                "expected": expected,
            }
        )
    return {
        "schema_version": 1,
        "scope": "Pinned manufacturing case only; not a model or provider-quality benchmark.",
        "provenance": {
            "source_url": SOURCE_URL,
            "source_sha256": SOURCE_SHA256,
            "master_path": "data/master_data.csv",
            "master_sha256": MASTER_SHA256,
            "fetched_at_utc": fetched_at,
            "builder": "analyst/case_study/oracle_builder.py",
            "builder_sha256_lf_normalized": hashlib.sha256(
                Path(__file__).read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest(),
            "derivation": "Standalone HTTP/CSV parsing with the standard library; no production imports. See builder for explicit rules and manual review instructions.",
            "record_id_digest": "SHA-256 of ascending decimal defect IDs joined by commas, encoded as ASCII, with no newline.",
            "money": "Exact Decimal sum of observed nonnegative costs; unknown costs retained as events.",
            "denominator": "All master products matching line/category/product filters, including zero-event products, multiplied by selected full months. Event filters do not restrict output.",
            "reconciliation": {
                "raw": 1090,
                "exact_copies_removed": 12,
                "normalized_copy_removed": 1,
                "compatible_copy_removed": 1,
                "conflicting_rows_quarantined": 2,
                "clean": 1074,
                "out_of_period": 3,
                "in_scope": 1071,
            },
        },
        "cases": cases,
    }


if __name__ == "__main__":
    print(json.dumps(build_reference(), indent=2, ensure_ascii=False, allow_nan=False))
