"""Deterministic cleaning with row-level evidence and lossless count reconciliation.

Slash dates use the explicitly declared source convention M/D/YYYY. Dates that
cannot be M/D but can only be D/M are handled as documented exceptions. Dates
with both components <= 12 are flagged as convention-dependent, not proven.
Missing or invalid cost never removes an otherwise countable defect event.
"""

import csv
import hashlib
import io
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import (
    AUDITED_SOURCE_COMMIT,
    DOMAIN_VALUES,
    MASTER_PATH,
    MONTHS,
    POLICY_VERSION,
    REQUIRED_MASTER_FIELDS,
    SOURCE_REPOSITORY_URL,
)
from .ingestion import SourceError, fetch_events
from .models import AuditEntry, Event, Product, Snapshot

_POSITIVE_INT_RE = re.compile(r"\+?\d+")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SLASH_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_COST_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
_SLASH_AMBIGUOUS_RE = re.compile(r"(?:[1-9]|0[1-9]|1[0-2])/(?:[1-9]|0[1-9]|1[0-2])/\d{4}")
_UNAMBIGUOUS_MONTH_FIRST_RE = re.compile(r"(?:[1-9]|0[1-9]|1[0-2])/(?:1[3-9]|2[0-9]|3[01])/\d{4}")
_ALLOWED_DOMAIN_VALUES: dict[str, dict[str, str]] = {
    field: {value.casefold(): value for value in values} for field, values in DOMAIN_VALUES.items()
}
_MONTHS_SET = frozenset(MONTHS)
_ALL_STUDY_DAYS = tuple(date(2024, 1, 1) + timedelta(days=offset) for offset in range(182))


def _year_month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def positive_integer(value: Any) -> int | None:
    """Accept integer values and integer-form strings, never booleans/fractions."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if _POSITIVE_INT_RE.fullmatch(stripped):
            parsed = int(stripped)
            return parsed if parsed > 0 else None
    return None


def parse_source_date(value: Any) -> tuple[date | None, str | None]:
    """Parse only ISO or declared slash forms and expose every date assumption."""
    if not isinstance(value, str):
        return None, "invalid_date"
    value = value.strip()
    try:
        if _ISO_DATE_RE.fullmatch(value):
            return date.fromisoformat(value), None
        match = _SLASH_DATE_RE.fullmatch(value)
        if not match:
            return None, "invalid_date"
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if first > 12 and second <= 12:
            return date(year, second, first), "day_first_date"
        parsed = date(year, first, second)
        assumption = (
            "ambiguous_date_assumption"
            if first <= 12 and second <= 12 and first != second
            else None
        )
        return parsed, assumption
    except ValueError:
        return None, "invalid_date"


def parse_cost(value: Any) -> tuple[float | None, str]:
    """No separators/currency heuristics or imputation; preserve unknown costs."""
    if value is None:
        return None, "missing"
    if isinstance(value, bool):
        return None, "invalid"
    if isinstance(value, (int, float)):
        if isinstance(value, int) and value < 0:
            return None, "negative"
        try:
            finite = math.isfinite(value)
        except OverflowError:
            return None, "invalid"
        if not finite:
            return None, "invalid"
        if value < 0:
            return None, "negative"
        return float(value), "known"
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None, "missing"
        if not _COST_RE.fullmatch(stripped):
            return None, "invalid"
        value_str = stripped
    elif isinstance(value, Decimal):
        if not value.is_finite():
            return None, "invalid"
        if value < 0:
            return None, "negative"
        number_float = float(value)
        if not math.isfinite(number_float):
            return None, "invalid"
        return number_float, "known"
    else:
        return None, "invalid"
    try:
        number = Decimal(value_str)
    except InvalidOperation:
        return None, "invalid"
    if not number.is_finite():
        return None, "invalid"
    if number < 0:
        return None, "negative"
    number_float = float(number)
    if not math.isfinite(number_float):
        return None, "invalid"
    return number_float, "known"


def _audit(
    audit: list[AuditEntry],
    source: str,
    row: int,
    record_id: Any,
    code: str,
    severity: str,
    field: str,
    raw: Any,
    action: str,
    detail: str,
    processed: Any = None,
    source_rows: tuple[int, ...] = (),
) -> None:
    audit.append(
        AuditEntry(
            source,
            row,
            str(record_id),
            code,
            severity,
            field,
            str(raw),
            action,
            detail,
            None if processed is None else str(processed),
            POLICY_VERSION,
            source_rows or (row,),
        )
    )


def prepare_master(payload: bytes, audit: list[AuditEntry]) -> tuple[tuple[Product, ...], int]:
    """Validate a one-row-per-product master before any event join."""
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        if not set(REQUIRED_MASTER_FIELDS).issubset(reader.fieldnames or []):
            raise SourceError("Master CSV is missing required columns")
        rows = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise SourceError("Master CSV is not valid UTF-8 CSV") from exc
    if not rows:
        raise SourceError("Master CSV has no product rows")
    products: dict[int, Product] = {}
    for index, row in enumerate(rows, start=2):
        product_id = positive_integer(row.get("product_id"))
        if product_id is None:
            raise SourceError(f"Master row {index} has an invalid product_id")
        if row.get("product_id") != product_id:
            _audit(
                audit,
                "master",
                index,
                product_id,
                "master_numeric_parsed",
                "Low",
                "product_id",
                row.get("product_id"),
                "normalized",
                "CSV integer text parsed explicitly; original value is retained.",
                product_id,
            )
        dimensions: dict[str, str] = {}
        for field in ("product_name", "category", "production_line"):
            raw = row.get(field)
            if not isinstance(raw, str) or not raw.strip():
                raise SourceError(f"Master row {index} has an empty {field}")
            dimensions[field] = " ".join(raw.split())
            if raw != dimensions[field]:
                _audit(
                    audit,
                    "master",
                    index,
                    product_id,
                    "master_text_normalized",
                    "Low",
                    field,
                    raw,
                    "corrected",
                    "Trimmed repeated or edge whitespace; no label inference.",
                    dimensions[field],
                )
        output = positive_integer(row.get("monthly_output"))
        if output is None:
            _audit(
                audit,
                "master",
                index,
                product_id,
                "invalid_output",
                "High",
                "monthly_output",
                row.get("monthly_output"),
                "flagged",
                "Product retained; associated cohort intensity is unavailable.",
            )
        elif row.get("monthly_output") != output:
            _audit(
                audit,
                "master",
                index,
                product_id,
                "master_numeric_parsed",
                "Low",
                "monthly_output",
                row.get("monthly_output"),
                "normalized",
                "CSV integer text parsed explicitly; original value is retained.",
                output,
            )
        product = Product(product_id=product_id, monthly_output=output, **dimensions)
        if product_id in products:
            if products[product_id] != product:
                raise SourceError(f"Conflicting master product_id {product_id}; join is unsafe")
            _audit(
                audit,
                "master",
                index,
                product_id,
                "master_duplicate",
                "High",
                "product_id",
                product_id,
                "removed",
                "Duplicate master row collapsed before the join.",
            )
        else:
            products[product_id] = product
    return tuple(sorted(products.values(), key=lambda item: item.product_id)), len(rows)


def _normalize_label(raw: Any, field: str) -> tuple[str, str | None]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "Unknown", "missing_label"
    if not isinstance(raw, str):
        return "Unknown", "unexpected_label"
    clean = " ".join(raw.split())
    canonical = _ALLOWED_DOMAIN_VALUES[field].get(clean.casefold())
    if canonical is None:
        return "Unknown", "unexpected_label"
    return canonical, "label_normalized" if raw != canonical else None


def _event_key(event: Event, include_cost: bool = True) -> tuple[Any, ...]:
    values = (
        event.defect_id,
        event.product_id,
        event.defect_type,
        event.defect_date,
        event.defect_location,
        event.severity,
        event.inspection_method,
    )
    return values + (event.repair_cost,) if include_cost else values


def build_snapshot(
    raw: list[dict[str, Any]],
    provenance: dict[str, Any],
    master_bytes: bytes,
    *,
    scenario: str = "",
) -> Snapshot:
    """Build a complete replacement snapshot; never mutate an active snapshot."""
    if scenario not in {"", "ambiguous_dm", "year_2024"}:
        raise ValueError("Date preparation accepts only canonical, ambiguous_dm or year_2024")

    def apply_date_scenario(
        parsed: date | None, issue: str | None
    ) -> tuple[date | None, str | None]:
        if (
            parsed is not None
            and scenario == "ambiguous_dm"
            and issue == "ambiguous_date_assumption"
        ):
            parsed = date(parsed.year, parsed.day, parsed.month)
        if parsed is not None and scenario == "year_2024":
            parsed = parsed.replace(year=2024)
        return parsed, issue

    immutable_raw = tuple(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for row in raw
    )
    records_sha256 = hashlib.sha256(
        ("[" + ",".join(immutable_raw) + "]").encode("utf-8")
    ).hexdigest()
    expected_records_sha256 = provenance.get(
        "derived_records_sha256", provenance.get("source_records_sha256")
    )
    if expected_records_sha256 is not None and expected_records_sha256 != records_sha256:
        raise SourceError(
            "HTTP-derived row values changed after fetching; the supplied provenance no longer matches"
        )
    audit: list[AuditEntry] = []
    products, master_raw_count = prepare_master(master_bytes, audit)
    product_lookup = {product.product_id: product for product in products}
    seen_exact: dict[str, int] = {}
    candidates: dict[int, list[Event]] = defaultdict(list)
    semantic_exceptions: dict[int, tuple[tuple[str, str], ...]] = {}
    blocked_ids: set[int] = set()
    exact_count = 0
    invalid_count = 0
    raw_monthly: Counter[str] = Counter()
    raw_date_issues: Counter[str] = Counter()
    raw_known_cost = 0.0
    raw_valid_costs: list[float] = []
    raw_cost_status_counts: Counter[str] = Counter()
    for index, row in enumerate(raw, start=1):
        raw_defect_date = row.get("defect_date")
        canonical_date, raw_date_issue = parse_source_date(raw_defect_date)
        raw_date, date_issue = apply_date_scenario(canonical_date, raw_date_issue)
        raw_cost = row.get("repair_cost")
        cost, cost_status = parse_cost(raw_cost)
        if raw_date_issue:
            raw_date_issues[raw_date_issue] += 1
        if raw_date is not None:
            raw_ym = _year_month(raw_date)
            if raw_ym in _MONTHS_SET:
                raw_monthly[raw_ym] += 1
                if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
                    try:
                        finite_raw_cost = math.isfinite(raw_cost)
                    except OverflowError:
                        finite_raw_cost = False
                    if finite_raw_cost:
                        raw_known_cost += raw_cost
                raw_cost_status_counts[cost_status] += 1
                if cost is not None:
                    raw_valid_costs.append(cost)
        exact_key = immutable_raw[index - 1]
        if exact_key in seen_exact:
            exact_count += 1
            _audit(
                audit,
                "defects",
                index,
                row.get("defect_id"),
                "exact_duplicate",
                "High",
                "row",
                row.get("defect_id"),
                "removed",
                f"Identical payload copy of source row {seen_exact[exact_key]}.",
                f"source row {seen_exact[exact_key]}",
                (seen_exact[exact_key], index),
            )
            continue
        seen_exact[exact_key] = index
        defect_id = positive_integer(row.get("defect_id"))
        product_id = positive_integer(row.get("product_id"))
        parsed_date = raw_date
        if defect_id is None or product_id is None or parsed_date is None:
            invalid_count += 1
            if defect_id is not None:
                blocked_ids.add(defect_id)
            if defect_id is None or product_id is None:
                _audit(
                    audit,
                    "defects",
                    index,
                    row.get("defect_id"),
                    "invalid_identifier",
                    "High",
                    "ids",
                    f"{row.get('defect_id')}/{row.get('product_id')}",
                    "quarantined",
                    "Event/product ID must be a positive integer.",
                )
            if parsed_date is None:
                _audit(
                    audit,
                    "defects",
                    index,
                    row.get("defect_id"),
                    "invalid_date",
                    "High",
                    "defect_date",
                    raw_defect_date,
                    "quarantined",
                    "Date is impossible or not an explicitly supported format.",
                )
            continue
        for identifier_field, identifier in (("defect_id", defect_id), ("product_id", product_id)):
            if row.get(identifier_field) != identifier:
                _audit(
                    audit,
                    "defects",
                    index,
                    defect_id,
                    "identifier_normalized",
                    "Low",
                    identifier_field,
                    row.get(identifier_field),
                    "normalized",
                    "Positive integer text parsed explicitly; original value is retained.",
                    identifier,
                )
        parsed_date_iso = parsed_date.isoformat()
        if date_issue:
            _audit(
                audit,
                "defects",
                index,
                defect_id,
                date_issue,
                "Medium",
                "defect_date",
                raw_defect_date,
                "corrected" if date_issue == "day_first_date" else "flagged",
                "Unambiguous D/M/YYYY exception converted to ISO."
                if date_issue == "day_first_date"
                else (
                    "D/M interpretation is a scenario assumption; source ambiguity is not independently resolved."
                    if scenario == "ambiguous_dm"
                    else "M/D/YYYY follows the declared dominant source convention; ambiguity is not independently resolved."
                ),
                parsed_date_iso,
            )
        elif raw_defect_date != parsed_date_iso:
            _audit(
                audit,
                "defects",
                index,
                defect_id,
                "date_representation_normalized",
                "Low",
                "defect_date",
                raw_defect_date,
                "normalized",
                "Explicitly parsed date represented as ISO; the source date is retained.",
                parsed_date_iso,
            )
        if canonical_date != parsed_date:
            _audit(
                audit,
                "defects",
                index,
                defect_id,
                "scenario_date_projection",
                "Medium",
                "defect_date",
                raw_defect_date,
                "scenario",
                f"Scenario {scenario} changes the interpreted date; canonical remains unchanged.",
                parsed_date_iso,
            )
        normalized: dict[str, str] = {}
        exceptions: list[tuple[str, str]] = []
        for field in DOMAIN_VALUES:
            normalized[field], issue = _normalize_label(row.get(field), field)
            if issue in {"missing_label", "unexpected_label"}:
                fingerprint = (
                    "missing"
                    if issue == "missing_label"
                    else json.dumps(row.get(field), sort_keys=True)
                )
                exceptions.append((field, fingerprint))
            if issue:
                _audit(
                    audit,
                    "defects",
                    index,
                    defect_id,
                    issue,
                    "Low" if issue == "label_normalized" else "Medium",
                    field,
                    row.get(field),
                    "corrected" if issue == "label_normalized" else "flagged",
                    f"Mapped to {normalized[field]!r}; missing/unrecognized categories remain Unknown.",
                    normalized[field],
                )
        if cost_status == "known" and raw_cost != cost:
            _audit(
                audit,
                "defects",
                index,
                defect_id,
                "cost_representation_normalized",
                "Low",
                "repair_cost",
                raw_cost,
                "normalized",
                "Numeric cost text parsed without currency or separator inference.",
                cost,
            )
        if cost_status != "known":
            fingerprint = (
                "missing"
                if cost_status == "missing"
                else f"{cost_status}:{json.dumps(raw_cost, sort_keys=True)}"
            )
            exceptions.append(("repair_cost", fingerprint))
            _audit(
                audit,
                "defects",
                index,
                defect_id,
                f"{cost_status}_cost",
                "Medium",
                "repair_cost",
                raw_cost,
                "flagged",
                "Cost is unknown; event retained for nonfinancial counts; no imputation.",
            )
        event = Event(
            defect_id,
            product_id,
            defect_date=parsed_date,
            repair_cost=cost,
            cost_status=cost_status,
            source_row=index,
            defect_type=normalized["defect_type"],
            defect_location=normalized["defect_location"],
            severity=normalized["severity"],
            inspection_method=normalized["inspection_method"],
        )
        candidates[defect_id].append(event)
        semantic_exceptions[index] = tuple(exceptions)

    accepted: list[Event] = []
    normalized_duplicate_count = complementary_count = conflict_count = conflicting_ids = 0
    for defect_id, group in candidates.items():
        if defect_id in blocked_ids:
            conflict_count += len(group)
            conflicting_ids += 1
            for event in group:
                _audit(
                    audit,
                    "defects",
                    event.source_row,
                    defect_id,
                    "conflicting_id",
                    "High",
                    "defect_id",
                    defect_id,
                    "quarantined",
                    "Another row for this ID failed mandatory field validation; no winner selected.",
                )
            continue
        # Converting invalid values to Unknown/None must not erase contradictory
        # source evidence. Only recognized normalization and true missing-value
        # sentinels can be considered semantically equivalent.
        core_keys = {
            (
                _event_key(event, include_cost=False),
                tuple(
                    pair
                    for pair in semantic_exceptions[event.source_row]
                    if pair[0] != "repair_cost"
                ),
            )
            for event in group
        }
        full_keys = {(_event_key(event), semantic_exceptions[event.source_row]) for event in group}
        valid_costs = {event.repair_cost for event in group if event.repair_cost is not None}
        if len(full_keys) == 1:
            winner = min(group, key=lambda event: event.source_row)
            accepted.append(winner)
            for event in group:
                if event is not winner:
                    normalized_duplicate_count += 1
                    _audit(
                        audit,
                        "defects",
                        event.source_row,
                        defect_id,
                        "normalized_duplicate",
                        "High",
                        "defect_id",
                        defect_id,
                        "removed",
                        f"Identical after documented normalization; retained source row {winner.source_row}.",
                        f"source row {winner.source_row}",
                        tuple(item.source_row for item in group),
                    )
        elif (
            len(core_keys) == 1
            and len(valid_costs) == 1
            and all(event.cost_status in {"missing", "known"} for event in group)
        ):
            # Complementary null/known cost only: use observed evidence, not an estimate.
            winner = min(
                (event for event in group if event.repair_cost is not None),
                key=lambda event: event.source_row,
            )
            accepted.append(winner)
            for event in group:
                if event is not winner:
                    complementary_count += 1
                    _audit(
                        audit,
                        "defects",
                        event.source_row,
                        defect_id,
                        "complementary_duplicate",
                        "Medium",
                        "repair_cost",
                        event.repair_cost,
                        "removed",
                        f"Same event attributes; observed cost from source row {winner.source_row} retained, not imputed.",
                        winner.repair_cost,
                        tuple(item.source_row for item in group),
                    )
        else:
            conflict_count += len(group)
            conflicting_ids += 1
            for event in group:
                _audit(
                    audit,
                    "defects",
                    event.source_row,
                    defect_id,
                    "conflicting_id",
                    "High",
                    "defect_id",
                    f"product={event.product_id}, cost={event.repair_cost}",
                    "quarantined",
                    "Same event ID has contradictory attributes; all variants quarantined pending source owner decision.",
                )

    enriched: list[Event] = []
    for event in accepted:
        product = product_lookup.get(event.product_id)
        if product:
            event = replace(
                event,
                product_name=product.product_name,
                category=product.category,
                production_line=product.production_line,
                matched_product=True,
            )
        else:
            _audit(
                audit,
                "defects",
                event.source_row,
                event.defect_id,
                "unmatched_product",
                "High",
                "product_id",
                event.product_id,
                "flagged",
                "Retained in plant totals as Unknown line/category; excluded from linked output intensity.",
            )
        if _year_month(event.defect_date) not in _MONTHS_SET:
            _audit(
                audit,
                "defects",
                event.source_row,
                event.defect_id,
                "outside_study_period",
                "Medium",
                "defect_date",
                event.defect_date.isoformat(),
                "excluded_from_scope",
                "Valid date retained in clean snapshot; excluded from January-June 2024 analysis.",
            )
        enriched.append(event)
    events = tuple(sorted(enriched, key=lambda event: (event.defect_date, event.defect_id)))
    scope = [event for event in events if _year_month(event.defect_date) in _MONTHS_SET]
    by_code: dict[str, list[AuditEntry]] = defaultdict(list)
    for entry in audit:
        if entry.action not in {"normalized", "scenario"}:
            by_code[entry.code].append(entry)
    issues = []
    for code, entries in sorted(by_code.items()):
        issues.append(
            {
                "code": code,
                "source": entries[0].source,
                "severity": entries[0].severity,
                "affected_count": len({(entry.source, entry.source_row) for entry in entries}),
                "description": ISSUE_DESCRIPTIONS.get(code, code.replace("_", " ").capitalize()),
                "handling": entries[0].detail,
            }
        )
    observed_days = {event.defect_date for event in scope}
    no_record_days = [day.isoformat() for day in _ALL_STUDY_DAYS if day not in observed_days]
    issues.append(
        {
            "code": "calendar_reporting_coverage",
            "source": "defects",
            "severity": "Medium",
            "affected_count": len(no_record_days),
            "affected_unit": "calendar dates",
            "description": "Calendar dates without source events do not establish reporting completeness or operating-day exposure.",
            "handling": "Retain observed events; show zero logged events on calendar charts, qualify baseline screening, and request reporting/production calendars.",
        }
    )
    removed = exact_count + normalized_duplicate_count + complementary_count
    quarantined = invalid_count + conflict_count
    if len(raw) != removed + quarantined + len(events):
        raise AssertionError("Raw-row reconciliation failed")
    known = [event for event in scope if event.repair_cost is not None]
    source_dates = [event.defect_date.isoformat() for event in events]
    master_sha256 = hashlib.sha256(master_bytes).hexdigest()
    dataset_version = (
        "dataset-"
        + hashlib.sha256(
            (provenance["source_sha256"] + ":" + master_sha256).encode("ascii")
        ).hexdigest()
    )
    provenance = {
        **provenance,
        "master_sha256": master_sha256,
        "master_path": "data/master_data.csv",
        "source_date_min": min(source_dates, default=None),
        "source_date_max": max(source_dates, default=None),
        "dataset_version": dataset_version,
        "policy_version": POLICY_VERSION,
        "scenario": scenario,
        "prepared_records_sha256": records_sha256,
        "master_schema": {
            "container": "csv",
            "required_fields": list(REQUIRED_MASTER_FIELDS),
            "row_count": master_raw_count,
        },
        "source_repository_url": SOURCE_REPOSITORY_URL,
        "audited_repository_commit": AUDITED_SOURCE_COMMIT,
        "source_commit_note": "Audited repository commit is a reference; the main-branch response is identified by its actual content SHA-256.",
    }
    provenance["snapshot_id"] = hashlib.sha256(
        (provenance["source_sha256"] + provenance["master_sha256"] + POLICY_VERSION).encode()
    ).hexdigest()[:16]
    defect_id_counts = Counter(str(row.get("defect_id")) for row in raw)
    quality = {
        "raw_event_count": len(raw),
        "exact_duplicate_count": exact_count,
        "normalized_duplicate_count": normalized_duplicate_count,
        "complementary_duplicate_count": complementary_count,
        "conflicting_id_count": conflicting_ids,
        "conflicting_row_count": conflict_count,
        "quarantined_count": quarantined,
        "clean_event_count": len(events),
        "in_scope_event_count": len(scope),
        "out_of_scope_count": len(events) - len(scope),
        "linked_event_count": sum(event.matched_product for event in scope),
        "unmatched_event_count": sum(not event.matched_product for event in scope),
        "known_cost_count": len(known),
        "unknown_cost_count": len(scope) - len(known),
        "known_repair_cost": round(
            math.fsum(event.repair_cost for event in known if event.repair_cost is not None), 2
        ),
        "master_raw_count": master_raw_count,
        "master_clean_count": len(products),
        "master_profile": {
            "product_count": len(products),
            "product_ids_contiguous_from_one": [p.product_id for p in products]
            == list(range(1, len(products) + 1)),
            "products_by_line": dict(sorted(Counter(p.production_line for p in products).items())),
            "products_by_category": dict(sorted(Counter(p.category for p in products).items())),
            "category_line_products": [
                {"category": category, "production_line": line, "product_count": count}
                for (category, line), count in sorted(
                    Counter((p.category, p.production_line) for p in products).items()
                )
            ],
            "missing_output_products": sum(p.monthly_output is None for p in products),
        },
        "issues": issues,
        "reconciliation": {
            "raw_rows": len(raw),
            "duplicate_copies_removed": removed,
            "quarantined_rows": quarantined,
            "retained_clean_events": len(events),
            "out_of_scope_events": len(events) - len(scope),
            "analysis_events": len(scope),
            "raw_equals_removed_plus_quarantined_plus_clean": True,
        },
        "counting_note": "Issue counts can overlap and include removed/out-of-scope source rows; do not add issue counts to reconcile rows.",
        "audit_stage_note": "Field checks run after exact-copy removal; raw profile separately counts all received rows. Post-join and date-scope checks run after ID resolution.",
        "raw_profile": {
            "ambiguous_date_rows": raw_date_issues["ambiguous_date_assumption"],
            "slash_order_ambiguous_rows": sum(
                isinstance(row.get("defect_date"), str)
                and bool(_SLASH_AMBIGUOUS_RE.fullmatch(row["defect_date"].strip()))
                for row in raw
            ),
            "day_first_date_rows": raw_date_issues["day_first_date"],
            "iso_date_rows": sum(
                isinstance(row.get("defect_date"), str)
                and bool(_ISO_DATE_RE.fullmatch(row["defect_date"].strip()))
                for row in raw
            ),
            "unambiguous_month_first_rows": sum(
                isinstance(row.get("defect_date"), str)
                and bool(_UNAMBIGUOUS_MONTH_FIRST_RE.fullmatch(row["defect_date"].strip()))
                and parse_source_date(row["defect_date"])[0] is not None
                for row in raw
            ),
            "null_cost_rows": sum(row.get("repair_cost") is None for row in raw),
            "negative_cost_rows": sum(
                parse_cost(row.get("repair_cost"))[1] == "negative" for row in raw
            ),
            "cost_ten_rows": sum(
                parse_cost(row.get("repair_cost")) == (10.0, "known") for row in raw
            ),
            "unmatched_product_rows": sum(
                positive_integer(row.get("product_id")) not in product_lookup for row in raw
            ),
            "label_whitespace_rows": {
                field: sum(
                    isinstance(row.get(field), str) and row[field] != row[field].strip()
                    for row in raw
                )
                for field in DOMAIN_VALUES
            },
            "label_case_rows": {
                field: sum(
                    isinstance(val := row.get(field), str)
                    and (norm := _normalize_label(val, field)[0]) != "Unknown"
                    and (stripped := val.strip()) != norm
                    and stripped.casefold() == norm.casefold()
                    for row in raw
                )
                for field in DOMAIN_VALUES
            },
            "repeated_id_count": sum(count > 1 for count in defect_id_counts.values()),
            "duplicate_id_surplus": len(raw) - len(defect_id_counts),
        },
        "corrected_source_rows": len(
            {
                entry.source_row
                for entry in audit
                if entry.source == "defects" and entry.action == "corrected"
            }
        ),
        "flagged_source_rows": len(
            {
                entry.source_row
                for entry in audit
                if entry.source == "defects"
                and entry.action in {"flagged", "quarantined", "excluded_from_scope"}
            }
        ),
        "temporal_coverage": {
            "calendar_days": 182,
            "days_with_events": len(observed_days),
            "days_without_events": len(no_record_days),
            "dates_without_events": no_record_days,
            "coverage_verified": False,
        },
    }
    kept_rows = {event.source_row for event in scope}
    quality["flagged_in_scope_event_count"] = len(
        {
            entry.source_row
            for entry in audit
            if entry.source == "defects"
            and entry.action == "flagged"
            and entry.source_row in kept_rows
        }
    )
    return Snapshot(
        products,
        events,
        tuple(audit),
        quality,
        provenance,
        {
            "monthly_counts": dict(sorted(raw_monthly.items())),
            "in_period_raw_count": sum(raw_monthly.values()),
            "raw_numeric_cost_including_negatives": round(raw_known_cost, 2),
            "in_period_cost_known_count": len(raw_valid_costs),
            "in_period_cost_unknown_count": sum(raw_monthly.values()) - len(raw_valid_costs),
            "in_period_known_repair_cost": round(math.fsum(raw_valid_costs), 2),
            "in_period_cost_status_counts": dict(sorted(raw_cost_status_counts.items())),
        },
        immutable_raw,
        bytes(master_bytes),
        scenario,
    )


ISSUE_DESCRIPTIONS = {
    "exact_duplicate": "Repeated payload rows would inflate event and cost totals.",
    "normalized_duplicate": "Same event becomes identical after label normalization.",
    "complementary_duplicate": "Same event has a missing cost and one observed cost in otherwise matching rows.",
    "conflicting_id": "Duplicate event ID carries contradictory attributes.",
    "missing_cost": "Repair cost is absent; financial coverage is incomplete.",
    "negative_cost": "Negative repair cost is invalid for this cost-incurred field.",
    "label_normalized": "Case/whitespace variation fragments known category labels.",
    "day_first_date": "Unambiguous day-first dates differ from the predominant source convention.",
    "ambiguous_date_assumption": "Slash date can represent more than one date without a source convention.",
    "unmatched_product": "Product foreign key has no corresponding master row.",
    "outside_study_period": "Valid source date lies outside January-June 2024.",
    "date_representation_normalized": "Source dates are explicitly parsed and represented as ISO; source values remain traceable.",
    "scenario_date_projection": "An explicitly selected date scenario changes interpretation without changing the source.",
}


def load_snapshot(master_path: Path = MASTER_PATH) -> Snapshot:
    """Run the real HTTP pipeline; master data is the only local input."""
    raw, provenance = fetch_events()
    try:
        master_bytes = master_path.read_bytes()
    except OSError as exc:
        raise SourceError("Master CSV could not be read") from exc
    return build_snapshot(raw, provenance, master_bytes)
