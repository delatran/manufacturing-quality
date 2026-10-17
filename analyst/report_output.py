"""Portable report metadata and display checks over sealed source evidence.

This module describes cited values; it never recalculates measurements or edits
the model's business prose. Comparison scopes remain distinct in the manifest.
"""

from __future__ import annotations

import math
import re
from html import escape
from typing import Any

from .claim_feedback import (
    numeric_role,
    policy_parameter_usage,
    reference_context,
    source_at,
    source_contexts,
    unit_scale_descriptor,
)
from .evidence import EvidenceLedger, canonical, sha256
from .localization import normalize_language, report_number_format

CITATION = re.compile(r"\[claim:((?:[^\[\]]|\[\d+\])+)\]")
NUMBER = re.compile(r"[-+]?\d(?:[\d.,]*\d)?(?:[eE][-+]?\d+)?%?")
_CONTEXT_FIELDS = (
    "context_id",
    "snapshot_id",
    "dataset_version",
    "policy_version",
    "scenario",
    "filters",
    "source_sha256",
    "master_sha256",
    "code_sha256",
    "kpi_version",
)
_PERIOD_SIDES = ("baseline", "comparison")
_SCENARIO_SIDES = ("canonical", "selected")
_DECLARED_TOOLS = {
    "dashboard_context",
    "dashboard_compare_scope",
    "analysis_query",
    "analysis_compare",
    "scenario_compare",
    "analysis_records",
    "analysis_provenance",
    "analysis_export",
    "analysis_sql",
    "run_case_study",
}


def _context(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("filters"), dict):
        return None
    if not all(
        value.get(key) for key in ("context_id", "snapshot_id", "dataset_version", "policy_version")
    ):
        raise ValueError("A report source lacks its context, dataset or policy version")
    if "scenario" not in value or value["filters"].get("scenario", "") != value["scenario"]:
        raise ValueError("A report source has inconsistent scenario metadata")
    return {key: value[key] for key in _CONTEXT_FIELDS if key in value}


def _analyses(entry: dict[str, Any]) -> list[dict[str, Any]]:
    result = entry.get("result", {})
    if not isinstance(result, dict):
        return []
    values = [result, result.get("analysis", {})]
    for side in (*_PERIOD_SIDES, *_SCENARIO_SIDES):
        child = result.get(side, {})
        if isinstance(child, dict):
            values += [child, child.get("analysis", {})]
    return [value for value in values if isinstance(value, dict)]


def _reference_output_unit(entry: dict[str, Any], path: str) -> dict[str, str] | None:
    """Bind the KPI denominator to its own sealed static-intensity declaration."""
    if (
        path != "$.analysis.kpis.reference_output"
        or entry.get("tool") != "analysis_query"
        or entry.get("execution_kind") != "bounded_analysis"
    ):
        return None
    result = entry.get("result")
    if not isinstance(result, dict) or result.get("ok") is not True:
        return None
    analysis = result.get("analysis")
    if not isinstance(analysis, dict) or set(analysis) != {"kpis"}:
        return None
    kpis = analysis["kpis"]
    value = kpis.get("reference_output") if isinstance(kpis, dict) else None
    if value is None or type(value) not in {int, float} or value <= 0:
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    metadata = result.get("metric_metadata")
    if not isinstance(metadata, dict):
        return None
    declaration = metadata.get("reference_intensity_per_1000")
    if not isinstance(declaration, dict) or (
        type(declaration.get("scale")) not in {int, float}
        or declaration["scale"] != 1000
        or declaration.get("unit") != "logged events per reference output units"
        or declaration.get("denominator_kind")
        != "static reference output; not verified actual production"
    ):
        return None
    definitions = result.get("definitions")
    if not isinstance(definitions, dict):
        return None
    definition = definitions.get("reference_intensity_per_1000")
    if not isinstance(definition, str) or not (
        definition.startswith("Reference-output intensity = linked logged defect events / ")
        and "sum of monthly_output for all eligible master products" in definition
        and "selected full months" in definition
        and "Static reference output is not verified actual production" in definition
    ):
        return None
    context = result.get("context")
    semantics = result.get("scope_semantics")
    if not isinstance(context, dict) or not isinstance(semantics, dict):
        return None
    bindings = source_contexts(entry, path)
    if (
        bindings != [context]
        or entry.get("analysis_context") != context
        or semantics.get("kind") != "selected_scope"
        or semantics.get("filters_applied") is not True
        or semantics.get("requested_context_id") != context.get("context_id")
        or semantics.get("requested_filters") != context.get("filters")
        or semantics.get("scenario_applied") is not bool(context.get("scenario"))
    ):
        return None
    code = entry.get("code")
    if not isinstance(code, str):
        return None
    try:
        if (
            sha256(canonical(result)) != entry.get("result_sha256")
            or sha256(code) != entry.get("code_sha256")
            or sha256(
                canonical({key: value for key, value in entry.items() if key != "entry_sha256"})
            )
            != entry.get("entry_sha256")
        ):
            return None
    except (TypeError, ValueError):
        return None
    return {
        "kind": "reference_output",
        "metric": "reference_output",
        "unit": declaration["unit"].removeprefix("logged events per "),
        "definition": declaration["denominator_kind"],
    }


def _selected_cohort_calendar_unit(entry: dict[str, Any], path: str) -> dict[str, str] | None:
    """Bind exact KPI aliases to declarations in their own selected-scope receipt."""
    metric = path.rpartition(".")[2]
    if (
        path != "$.analysis.kpis." + metric
        or entry.get("tool") != "analysis_query"
        or entry.get("execution_kind") != "bounded_analysis"
    ):
        return None
    result = entry.get("result")
    if not isinstance(result, dict) or result.get("ok") is not True:
        return None
    analysis = result.get("analysis")
    if not isinstance(analysis, dict) or set(analysis) not in (
        {"kpis"},
        {"kpis", "trend", "calendar_coverage"},
    ):
        return None
    kpis = analysis.get("kpis")
    if not isinstance(kpis, dict) or {
        "context",
        "scope",
        "definitions",
        "metric_metadata",
    }.intersection(kpis):
        return None
    value = kpis.get(metric)
    if type(value) is not int or value < 0:
        return None
    definitions = result.get("definitions")
    context = result.get("context")
    semantics = result.get("scope_semantics")
    if (
        not isinstance(definitions, dict)
        or not isinstance(context, dict)
        or not isinstance(semantics, dict)
        or source_contexts(entry, path) != [context]
        or entry.get("analysis_context") != context
        or reference_context(entry, path)["scope"]
        != {key: value for key, value in context["filters"].items() if value}
        or semantics.get("kind") != "selected_scope"
        or semantics.get("filters_applied") is not True
        or semantics.get("requested_context_id") != context.get("context_id")
        or semantics.get("requested_filters") != context.get("filters")
        or semantics.get("scenario_applied") is not bool(context.get("scenario"))
    ):
        return None
    if metric in definitions or (
        isinstance(result.get("metric_metadata"), dict) and metric in result["metric_metadata"]
    ):
        return None
    code = entry.get("code")
    if not isinstance(code, str):
        return None
    try:
        if (
            sha256(canonical(result)) != entry.get("result_sha256")
            or sha256(code) != entry.get("code_sha256")
            or sha256(
                canonical({key: value for key, value in entry.items() if key != "entry_sha256"})
            )
            != entry.get("entry_sha256")
        ):
            return None
    except (TypeError, ValueError):
        return None
    if metric == "month_count":
        declaration = definitions.get("event_count")
        if not isinstance(declaration, str) or not declaration.startswith(
            "Unique retained logged defect events in the selected full-month scope,"
        ):
            return None
        return {
            "kind": "calendar_month_count",
            "metric": metric,
            "unit": "calendar months",
            "definition": "Selected full calendar months, including months without selected events; not the count of months containing logged events.",
        }
    declaration = definitions.get("cohort_product_count")
    if not isinstance(declaration, str) or not declaration.startswith(
        "Eligible products in the selected product-master cohort, including products with no selected events."
    ):
        return None
    descriptions = {
        "cohort_products_with_events": "Eligible master products with at least one selected logged defect event; this counts products, not events or distinct defective units.",
        "cohort_products_without_events": "Eligible master products without selected logged defect events; absent logs do not establish perfect quality or zero production.",
    }
    if metric not in descriptions:
        return None
    return {
        "kind": "product_count",
        "metric": metric,
        "unit": "master products",
        "definition": descriptions[metric],
    }


def metric_unit(entry: dict[str, Any], path: str) -> dict[str, str] | None:
    """Read unit semantics only for the central, declared analytical tools."""
    if entry.get("tool") not in _DECLARED_TOOLS or entry.get("result", {}).get("ok") is False:
        return None
    metric = path.rpartition(".")[2]
    if metric == "reference_output":
        return _reference_output_unit(entry, path)
    if metric in {"cohort_products_with_events", "cohort_products_without_events", "month_count"}:
        return _selected_cohort_calendar_unit(entry, path)
    if descriptor := unit_scale_descriptor(entry, path):
        return {
            "kind": "unit_scale",
            "metric": path.rsplit(".", 2)[-2],
            "unit": "unit scale",
            "definition": str(descriptor.get("unit", "Source-declared unit scale")),
        }
    if numeric_role(entry, path) == "policy_parameter":
        return {
            "kind": "policy_parameter",
            "metric": metric,
            "unit": "local currency",
            "definition": "Scenario repair-cost equality criterion; not an observed total or event count.",
        }
    if numeric_role(entry, path) == "identifier":
        return {
            "kind": "identifier",
            "metric": metric,
            "unit": "identifier",
            "definition": "A source identifier; not a measurement.",
        }
    parents = []
    for match in re.finditer(r"\.[A-Za-z_][A-Za-z_0-9]*|\[\d+\]", path):
        try:
            parent = source_at(entry["result"], path[: match.start()])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        if isinstance(parent, dict):
            parents.append(parent)
    if ".cleaning_impact.raw." in path and metric in {
        "row_count",
        "cost_known_count",
        "cost_unknown_count",
    }:
        declaration = next((parent["unit"] for parent in reversed(parents) if "unit" in parent), "")
        if declaration == "received source rows before duplicate resolution":
            return {
                "kind": "source_row_count",
                "metric": metric,
                "unit": "received source rows",
                "definition": declaration,
            }
    if path.endswith(".cleaning_impact.row_count_difference"):
        return {
            "kind": "grain_difference",
            "metric": metric,
            "unit": "retained-event count minus raw-row count",
            "definition": "An arithmetic difference of counts with different grains; not a count of removed distinct defects.",
        }
    if path.endswith(".cleaning_impact.raw_numeric_cost_including_negatives"):
        return {
            "kind": "cost_diagnostic",
            "metric": metric,
            "unit": "local currency",
            "definition": "Raw numeric diagnostic including negative amounts; not a valid repair-cost total.",
        }
    if ".inspection_methods." in path and metric == "event_share_pct":
        declaration = next(
            (parent["share_denominator"] for parent in parents if "share_denominator" in parent), ""
        )
        if declaration.startswith("All selected retained logged defect events;"):
            return {"kind": "percentage", "metric": metric, "unit": "%", "definition": declaration}
    cell = re.search(
        r"\.location_severity\.cells\[\d+\]\.(count|row_pct|col_pct|column_pct)$", path
    )
    if cell:
        row = parents[-1] if parents else {}
        if (
            isinstance(row.get("location"), str)
            and isinstance(row.get("severity"), str)
            and any(
                "event_count" in analysis.get("definitions", {}) for analysis in _analyses(entry)
            )
        ):
            if metric == "count":
                return {
                    "kind": "count",
                    "metric": metric,
                    "unit": "logged defect events",
                    "definition": "Logged defect events in this location and severity cell.",
                }
            return {
                "kind": "percentage",
                "metric": metric,
                "unit": "%",
                "definition": (
                    "Percent of logged defect events at this location (the row denominator)."
                    if metric == "row_pct"
                    else "Percent of logged defect events at this severity (the column denominator)."
                ),
            }
    calendar = re.search(r"\.calendar_coverage(?:\.monthly\[\d+\])?\.([A-Za-z_]+)$", path)
    if calendar:
        coverage_path = path[: path.index(".calendar_coverage")] + ".calendar_coverage"
        coverage = source_at(entry["result"], coverage_path)
        if isinstance(coverage, dict) and coverage.get("scope_kind") == "selected_scope":
            if metric in {"max_observed_day_of_month", "last_observed_day"}:
                return {
                    "kind": "calendar_day_ordinal",
                    "metric": metric,
                    "unit": "day of month",
                    "definition": "An observed calendar day-of-month position, not a count of events or days.",
                }
            if metric in {
                "calendar_days",
                "days_with_events",
                "days_without_events",
                "calendar_days_after_last_observed",
            }:
                return {
                    "kind": "calendar_day_count",
                    "metric": metric,
                    "unit": "calendar days",
                    "definition": "Calendar dates under the selected filters; dates without logs do not establish zero defects or complete reporting.",
                }
            if metric in {"selected_month_count", "months_with_events"}:
                return {
                    "kind": "calendar_month_count",
                    "metric": metric,
                    "unit": "calendar months",
                    "definition": "Reporting months under the selected calendar scope, not logged defect events.",
                }
    if re.search(r"\.kpis\.cohort_product_count$", path) and any(
        "event_count" in analysis.get("definitions", {}) for analysis in _analyses(entry)
    ):
        return {
            "kind": "product_count",
            "metric": metric,
            "unit": "master products",
            "definition": "Eligible products in the selected master-data cohort, not logged events or distinct defective units.",
        }
    delta = re.search(
        r"\.metric_deltas\.([A-Za-z_][A-Za-z_0-9]*)\.(canonical|selected|delta)$", path
    )
    if delta:
        metric = delta[1]
    for analysis in _analyses(entry):
        declared_metric = (
            "reference_intensity_per_1000"
            if metric in {"reference_intensity_change", "reference_intensity_change_contribution"}
            else metric
        )
        declaration = analysis.get("metric_metadata", {}).get(declared_metric, {})
        if isinstance(declaration, dict) and declaration.get("unit"):
            unit = declaration["unit"]
            if declaration.get("scale") == 100 and unit.startswith("percent of "):
                if delta and delta[2] == "delta":
                    return {
                        "kind": "percentage_point",
                        "metric": metric,
                        "unit": "percentage points",
                        "definition": "Selected percentage minus canonical percentage; a difference of percentage levels.",
                    }
                return {"kind": "percentage", "metric": metric, "unit": "%", "definition": unit}
            if (
                declaration.get("scale") == 1000
                and unit == "logged events per reference output units"
            ):
                return {
                    "kind": "intensity",
                    "metric": metric,
                    "unit": "events per thousand reference output units",
                    "definition": unit,
                }
        definition = analysis.get("definitions", {}).get("known_repair_cost", "")
        currency = analysis.get("metadata", {}).get("currency", "")
        amount = (
            "cost" in metric
            or ".cost_distribution." in path
            and metric
            in {
                "lower",
                "upper",
                "minimum",
                "maximum",
                "mean",
                "median",
                "p25",
                "p50",
                "p75",
                "p90",
                "p95",
                "p99",
            }
        )
        if (
            amount
            and not metric.endswith(("_count", "_pct", "_rank"))
            and (
                "local currency" in definition
                or "local currency" in currency
                or analysis.get("unit") == "local currency"
            )
        ):
            return {
                "kind": "cost",
                "metric": metric,
                "unit": "local currency",
                "definition": definition or currency or "local currency",
            }
        if metric in {
            "event_count",
            "event_count_change",
            "critical_count",
            "critical_count_change",
            "cost_known_count",
            "cost_unknown_count",
            "linked_event_count",
            "unmatched_event_count",
            "eligible_event_count",
        } and "event_count" in analysis.get("definitions", {}):
            count_definition = analysis["definitions"].get(metric)
            return {
                "kind": "count",
                "metric": metric,
                "unit": "logged defect events",
                "definition": count_definition
                if isinstance(count_definition, str) and count_definition.strip()
                else analysis["definitions"]["event_count"],
            }
    return None


def evidence_manifest(
    ledger: EvidenceLedger | None,
    claims: list[dict[str, Any]],
    *,
    language: str,
    instructions_version: str,
    tools_version: str,
    source_execution_ids: list[str] | tuple[str, ...] = (),
    bound_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep all actual cited contexts, including both sides of a comparison."""
    normalize_language(language)
    entries = {entry["execution_id"]: entry for entry in ledger.records()} if ledger else {}
    contexts: dict[str, dict[str, Any]] = {}
    sources, references = [], []

    def add_context(value: Any, role: str) -> str | None:
        context = _context(value)
        if context is None:
            return None
        key = context["context_id"]
        if key in contexts:
            if {k: v for k, v in contexts[key].items() if k != "roles"} != context:
                raise ValueError("A report context identifier has conflicting metadata")
        else:
            contexts[key] = {**context, "roles": []}
        if role not in contexts[key]["roles"]:
            contexts[key]["roles"].append(role)
        return key

    add_context(bound_context, "bound")
    for identity in sorted({claim["execution_id"] for claim in claims} | set(source_execution_ids)):
        entry = entries.get(identity)
        if (
            entry is None
            or not isinstance(entry.get("result"), dict)
            or entry["result"].get("ok") is False
        ):
            raise ValueError("A report source is missing or unsuccessful")
        result = entry["result"]
        source_contexts: dict[str, str] = {}
        sides = (
            _PERIOD_SIDES
            if entry["tool"] == "analysis_compare"
            else _SCENARIO_SIDES
            if entry["tool"] == "scenario_compare"
            else ()
        )
        for side in sides:
            container = result.get(side)
            context_id = add_context(
                container.get("context") if isinstance(container, dict) else None, side
            )
            if context_id is None:
                raise ValueError(f"Comparison source requires an explicit valid {side} context")
            source_contexts[side] = context_id
        if not source_contexts:
            context_id = add_context(
                result.get("context") or entry.get("analysis_context"), "scope"
            )
            if context_id:
                source_contexts["scope"] = context_id
        sources.append(
            {
                "execution_id": identity,
                "tool": entry["tool"],
                "code_sha256": entry["code_sha256"],
                "result_sha256": entry["result_sha256"],
                "contexts": source_contexts,
            }
        )
        for claim in [value for value in claims if value["execution_id"] == identity]:
            if (
                claim["code_sha256"] != entry["code_sha256"]
                or source_at(result, claim["path"]) != claim["value"]
            ):
                raise ValueError("A report reference differs from its sealed numeric source")
            selected = [
                value
                for side, value in source_contexts.items()
                if claim["path"].startswith("$." + side + ".")
            ]
            if not selected:
                selected = list(source_contexts.values())
            references.append(
                {
                    **claim,
                    "context_ids": selected,
                    "labels": reference_context(entry, claim["path"])["labels"],
                    "role": numeric_role(entry, claim["path"]),
                    "unit": metric_unit(entry, claim["path"]),
                }
            )
    order = {claim["id"]: index for index, claim in enumerate(claims)}
    references.sort(key=lambda value: order[value["id"]])
    return {
        "version": 1,
        "language": language,
        "locale": report_number_format(language),
        "instructions_version": instructions_version,
        "tools_version": tools_version,
        "contexts": list(contexts.values()),
        "sources": sources,
        "references": references,
    }


def unit_note(claims: list[dict[str, Any]], ledger: EvidenceLedger | None, language: str) -> str:
    """Supply a separately labelled unit legend without rewriting model prose."""
    if ledger is None:
        return ""
    entries = {entry["execution_id"]: entry for entry in ledger.records()}
    units = {
        unit["kind"]
        for claim in claims
        if (unit := metric_unit(entries.get(claim["execution_id"], {}), claim["path"]))
    }
    labels = {
        "en": {
            "count": "Counts reflect logged defect events, not distinct defective units.",
            "percentage": "Shares and coverage rates are percentages (%).",
            "percentage_point": "Differences between percentages use percentage points, not relative percent changes.",
            "cost": "Repair costs retain the source amounts; the source does not specify the currency. No currency conversion is applied.",
            "intensity": "Reference intensity uses logged events per thousand reference output units; static reference output is not verified actual production.",
        },
        "vi": {
            "count": "Số lượng phản ánh sự kiện lỗi đã ghi nhận, không phải số sản phẩm lỗi riêng biệt.",
            "percentage": "Tỷ trọng và độ phủ có đơn vị phần trăm (%).",
            "percentage_point": "Chênh lệch giữa các tỷ lệ tính bằng điểm phần trăm, không phải mức thay đổi phần trăm tương đối.",
            "cost": "Chi phí sửa chữa giữ nguyên giá trị trong nguồn; nguồn không xác định loại tiền. Không quy đổi tiền tệ.",
            "intensity": "Cường độ tham chiếu tính theo số sự kiện trên mỗi nghìn đơn vị sản lượng tham chiếu; sản lượng tham chiếu cố định chưa được xác minh là sản lượng thực tế.",
        },
    }
    notes = [
        labels[language][key]
        for key in ("count", "percentage", "percentage_point", "cost", "intensity")
        if key in units
    ]
    if "policy_parameter" in units:
        notes.append(
            "The scenario repair-cost criterion uses source monetary units, with currency unspecified; it is not an observed total or event count."
            if language == "en"
            else "Tiêu chí chi phí của kịch bản dùng đơn vị tiền tệ của nguồn, chưa xác định loại tiền; đây không phải tổng chi phí hay số sự kiện quan sát được."
        )
    for kind, english, vietnamese in (
        (
            "source_row_count",
            "Raw counts describe received source rows before duplicate resolution.",
            "Số đếm thô là số dòng nguồn nhận được trước khi xử lý trùng lặp.",
        ),
        (
            "grain_difference",
            "Cleaning count differences subtract raw-row counts from retained-event counts; the grains differ.",
            "Chênh lệch làm sạch lấy số sự kiện được giữ lại trừ số dòng thô; hai số đếm có cấp dữ liệu khác nhau.",
        ),
        (
            "cost_diagnostic",
            "Raw numeric cost diagnostics can include negative amounts and are not valid repair-cost totals.",
            "Tổng số học chi phí thô có thể gồm giá trị âm và không phải tổng chi phí sửa chữa hợp lệ.",
        ),
        (
            "calendar_day_ordinal",
            "An observed day of month is a calendar position, not an event count or elapsed-day count.",
            "Ngày quan sát trong tháng là vị trí trên lịch, không phải số sự kiện hay số ngày đã trôi qua.",
        ),
        (
            "calendar_day_count",
            "Calendar-day counts describe dates under the selected filters; missing logs do not prove zero defects or complete reporting.",
            "Số ngày trên lịch mô tả ngày trong phạm vi đã chọn; thiếu nhật ký không chứng minh không có lỗi hay báo cáo đầy đủ.",
        ),
        (
            "calendar_month_count",
            "Calendar-month counts describe selected reporting months, not logged events.",
            "Số tháng trên lịch mô tả tháng báo cáo đã chọn, không phải số sự kiện được ghi nhận.",
        ),
        (
            "reference_output",
            "Reference-output totals use static reference units for the selected full months; they do not verify actual production or inspection exposure.",
            "Tổng sản lượng tham chiếu dùng đơn vị tham chiếu cố định cho các tháng đã chọn; số liệu này không xác minh sản lượng thực tế hay quy mô kiểm tra.",
        ),
        (
            "product_count",
            "Cohort counts describe eligible master products, not logged defect events or distinct defective units.",
            "Số đếm tập sản phẩm là sản phẩm đủ điều kiện trong danh mục gốc, không phải sự kiện lỗi hay sản phẩm lỗi riêng biệt.",
        ),
    ):
        if kind in units:
            notes.append(vietnamese if language == "vi" else english)
    return (
        (("**Unit notes:** " if language == "en" else "**Ghi chú đơn vị:** ") + " ".join(notes))
        if notes
        else ""
    )


def with_unit_note(
    markdown: str, claims: list[dict[str, Any]], ledger: EvidenceLedger | None, language: str
) -> str:
    note = unit_note(claims, ledger, language)
    return markdown if not note or note in markdown else markdown + "\n\n" + note


def _declared_intensity_scale(entry: dict[str, Any], path: str) -> bool:
    """A unit scale belongs to the cited analytical source and comparison side."""
    result = entry.get("result", {})
    if (
        not isinstance(result, dict)
        or result.get("ok") is False
        or entry.get("execution_kind") not in {"bounded_analysis", "canonical_receipt_import"}
        and not (
            entry.get("tool") == "run_case_study"
            and result.get("qa", {}).get("status") == "verified"
        )
        or (metric_unit(entry, path) or {}).get("kind") != "intensity"
    ):
        return False
    side = path.removeprefix("$.").partition(".")[0]
    if side in {*_PERIOD_SIDES, *_SCENARIO_SIDES} and isinstance(
        result.get(side, {}).get("analysis"), dict
    ):
        analyses = [result[side]["analysis"]]
    elif entry.get("tool") in {"analysis_compare", "scenario_compare"}:
        sides = _PERIOD_SIDES if entry["tool"] == "analysis_compare" else _SCENARIO_SIDES
        analyses = [result.get(name, {}).get("analysis", {}) for name in sides]
    else:
        # Section queries keep their source definitions beside the selected
        # analysis; full canonical receipts keep them in the analysis itself.
        analyses = [result if "metric_metadata" in result else result.get("analysis", result)]
    return all(
        isinstance(analysis, dict)
        and analysis.get("metric_metadata", {}).get("reference_intensity_per_1000", {}).get("scale")
        == 1000
        and analysis.get("metric_metadata", {}).get("reference_intensity_per_1000", {}).get("unit")
        == "logged events per reference output units"
        for analysis in analyses
    )


def _formula_scale_text(line, claims, entries, number_format):
    """Recognize only a source-bound reference-intensity equation."""
    by_id = {claim["id"]: claim for claim in claims}

    def atom(name):
        return r"(?P<" + name + r"_value>[-+]?\d[\d.,]*)\s*\[claim:(?P<" + name + r">[^\[\]]+)\]"

    pattern = (
        atom("numerator")
        + r"\s*(?:(?:linked\s+events?|sự\s+kiện\s+liên\s+kết)\s*)?[÷/]\s*"
        + atom("denominator")
        + r"\s*(?:(?:reference\s+output\s+units?|đơn\s+vị\s+sản\s+lượng\s+tham\s+chiếu)\s*)?"
        + r"[×*]\s*(?:unit\s+scale|hệ\s+số\s+(?:quy\s+đổi|đơn\s+vị))\s*"
        + atom("scale")
        + r"\s*=\s*"
        + atom("intensity")
    )
    for match in reversed(list(re.finditer(pattern, line, re.IGNORECASE))):
        bound = [
            by_id.get(match[name]) for name in ("numerator", "denominator", "scale", "intensity")
        ]
        resolved = [claim for claim in bound if claim is not None]
        if len(resolved) != 4:
            continue
        numerator, denominator, scale, intensity = resolved
        if len({claim["execution_id"] for claim in resolved}) != 1:
            continue
        entry = entries.get(intensity["execution_id"], {})
        expected = (
            "$.analysis.kpis.eligible_event_count",
            "$.analysis.kpis.reference_output",
            "$.metric_metadata.reference_intensity_per_1000.scale",
            "$.analysis.kpis.reference_intensity_per_1000",
        )
        actual_paths = tuple(claim["path"] for claim in resolved)
        if actual_paths not in {expected, ("$.analysis.kpis.linked_event_count", *expected[1:])}:
            continue
        if not _declared_intensity_scale(entry, intensity["path"]):
            continue
        contexts = [source_contexts(entry, claim["path"]) for claim in resolved]
        if not contexts[0] or any(context != contexts[0] for context in contexts[1:]):
            continue
        descriptor = unit_scale_descriptor(entry, scale["path"])
        if (
            not descriptor
            or descriptor.get("scale") != 1000
            or scale["value"] != 1000
            or match["scale_value"]
            not in {"1000", "1.000" if number_format == "vi-VN" else "1,000"}
            or descriptor.get("unit") != "logged events per reference output units"
            or denominator["value"] <= 0
        ):
            continue
        if not math.isclose(
            numerator["value"] / denominator["value"] * 1000,
            intensity["value"],
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            continue
        start, end = match.span("scale_value")
        line = line[:start] + "source-defined scale" + line[end:]
    return line


def typed_unit_scale_text(
    line: str,
    claims: list[dict[str, Any]],
    entries: dict[str, dict[str, Any]],
    number_format: str = "en-US",
) -> str:
    """Mask a declared denominator scale in the audit projection, never in prose.

    The nearest citation must describe intensity in the same clause. A unit
    phrase cannot establish an observed count or cost with the same digits.
    """
    line = _formula_scale_text(line, claims, entries, number_format)
    by_id = {claim["id"]: claim for claim in claims}
    citations = list(CITATION.finditer(line))
    boundaries = [
        match
        for match in re.finditer(r"[;!?\n]|[.,](?=\s|$)", line)
        if not any(citation.start() <= match.start() < citation.end() for citation in citations)
    ]
    replacements = []
    for unit in re.finditer(
        r"(?<!\w)(?:per|trên(?:\s+mỗi)?)\s+(?P<scale>1[,.]?000)"
        r"(?:\s*(?P<scale_citation>\[claim:(?:[^\[\]]|\[\d+\])+\]))?\s+"
        r"(?:(?:reference(?:[-\s]+output)?|output)\s+units?|units?|đơn\s+vị(?:\s+(?:sản\s+lượng\s+)?tham\s+chiếu)?)(?!\w)",
        line,
        re.IGNORECASE,
    ):
        start = max((match.end() for match in boundaries if match.end() <= unit.start()), default=0)
        end = min(
            (match.start() for match in boundaries if match.start() >= unit.end()),
            default=len(line),
        )
        if not re.search(r"\breference\b|tham\s+chiếu", line[start:end], re.IGNORECASE):
            continue
        preceding = [citation for citation in citations if start <= citation.end() <= unit.start()]
        if preceding:
            citation = preceding[-1]
            if not re.fullmatch(
                r"\s*(?:(?:(?:logged(?: defect)?|defect)\s+)?events?|sự\s+kiện(?:\s+lỗi)?)?\s*",
                line[citation.end() : unit.start()],
                re.IGNORECASE,
            ):
                continue
        else:
            following = [citation for citation in citations if unit.end() <= citation.start() < end]
            if not following:
                continue
            citation = following[0]
            between = line[unit.end() : citation.start()].strip().strip("*_` ")
            if between:
                # A trailing intensity citation may follow its displayed value
                # outside an emphasized unit phrase. Check that exact display;
                # the independent repetition guard still rejects value echoes.
                candidate = by_id.get(citation[1])
                displayed = NUMBER.fullmatch(between)
                if candidate is None or displayed is None or between.endswith("%"):
                    continue
                decimal, thousands = (",", ".") if number_format == "vi-VN" else (".", ",")
                try:
                    shown = float(between.replace(thousands, "").replace(decimal, "."))
                except ValueError:
                    continue
                places = len(between.partition(decimal)[2])
                tolerance = 0.5 * 10**-places if decimal in between else 0.0000001
                if abs(shown - candidate["value"]) > tolerance + 1e-9:
                    continue
        claim = by_id.get(citation[1])
        if not claim or not _declared_intensity_scale(
            entries.get(claim["execution_id"], {}), claim["path"]
        ):
            continue
        if unit["scale_citation"]:
            scale_match = CITATION.fullmatch(unit["scale_citation"])
            scale_claim = by_id.get(scale_match[1]) if scale_match else None
            if not scale_claim:
                continue
            scale_entry = entries.get(scale_claim["execution_id"], {})
            descriptor = unit_scale_descriptor(scale_entry, scale_claim["path"])
            if (
                descriptor is None
                or descriptor.get("scale") != 1000
                or scale_claim["value"] != 1000
                or descriptor.get("unit") != "logged events per reference output units"
                or not scale_claim["path"].endswith(".reference_intensity_per_1000.scale")
            ):
                continue
            intensity_contexts = source_contexts(entries[claim["execution_id"]], claim["path"])
            scale_contexts = source_contexts(scale_entry, scale_claim["path"])
            if (
                not intensity_contexts
                or not scale_contexts
                or {
                    canonical({key: value[key] for key in _CONTEXT_FIELDS if key in value})
                    for value in intensity_contexts
                }
                != {
                    canonical({key: value[key] for key in _CONTEXT_FIELDS if key in value})
                    for value in scale_contexts
                }
            ):
                continue
        replacements.append(unit.span("scale"))
    for start, end in reversed(replacements):
        line = line[:start] + "source-defined scale" + line[end:]
    for claim in claims:
        descriptor = unit_scale_descriptor(entries.get(claim["execution_id"], {}), claim["path"])
        if (
            descriptor is None
            or descriptor["scale"] != claim["value"]
            or not float(claim["value"]).is_integer()
        ):
            continue
        integer = int(claim["value"])
        grouped = f"{integer:,}".replace(",", ".") if number_format == "vi-VN" else f"{integer:,}"
        line = re.sub(
            r"((?<!\w)(?:unit\s+scale|hệ\s+số\s+đơn\s+vị)\s*(?::|=|is\b|là\b)?\s*)"
            r"(?:" + re.escape(grouped) + "|" + re.escape(str(integer)) + r")"
            r"(\s*\[claim:" + re.escape(claim["id"]) + r"\])(?=\s*(?:$|[.;!?]))",
            r"\1source-defined scale\2",
            line,
            flags=re.IGNORECASE,
        )
    return line


def display_errors(
    line: str, claims: list[dict[str, Any]], entries: dict[str, dict[str, Any]]
) -> list[str]:
    """Reject explicit wrong units; absent units are supplied in a source legend."""
    errors = []
    by_id = {claim["id"]: claim for claim in claims}
    for match in CITATION.finditer(line):
        claim = by_id.get(match[1])
        if not claim:
            continue
        unit = metric_unit(entries.get(claim["execution_id"], {}), claim["path"])
        if unit is None:
            continue
        before = line[: match.start()]
        number = list(NUMBER.finditer(CITATION.sub("", before)))
        tail = line[match.end() :]
        nearest = number[-1] if number else None
        if nearest is None:
            continue
        if unit["kind"] == "policy_parameter" and not policy_parameter_usage(
            line, claim, match.start()
        ):
            errors.append(
                f"Claim {claim['id']} is a scenario criterion parameter, not an observed quantity; bind it to its explicit repair-cost selection or financial-availability assumption and cite a measured subtotal/count separately."
            )
        if (
            claim["value"] < 0
            and (
                claim["path"].rpartition(".")[2].endswith("_change")
                or ".metric_deltas." in claim["path"]
                and claim["path"].endswith(".delta")
            )
            and nearest[0].startswith("-")
            and re.search(
                r"(?:\b(?:decreas(?:e|ed|es)|declin(?:e|ed|es)|dropp?ed|fell|reduc(?:e|ed|es))"
                r"\s*(?:(?:by|of|was|is)\s*)?|giảm\s*(?:(?:đi|là)\s*)?)[:=]?\s*$",
                CITATION.sub("", before)[: nearest.start()],
                re.IGNORECASE,
            )
        ):
            errors.append(
                f"Claim {claim['id']} is already a signed negative change; a decrease/giảm phrase before it is ambiguous. "
                "Keep the signed [value:ID] and use neutral change/thay đổi/chênh lệch wording, or cite the baseline and comparison values; do not negate or calculate a new magnitude."
            )
        prefix = CITATION.sub("", before)[nearest.end() :]
        adjacent = prefix + " " + re.split(r"[.;\n]|\[claim:", tail, maxsplit=1)[0]
        percent = (
            nearest[0].endswith("%")
            or bool(re.match(r"\s*%", tail))
            or bool(re.match(r"\s*(?:%|percent\b|phần trăm\b)", prefix, re.IGNORECASE))
        )
        points = bool(
            re.match(
                r"\s*(?:percentage[- ]points?\b|pp\b|p\.p\.(?=\s|$)|điểm phần trăm\b)",
                prefix + tail,
                re.IGNORECASE,
            )
        )
        event_units = bool(
            re.match(
                r"\s*(?:(?:logged(?: defect)? |defect )?events?|counts?|rows?|sự kiện(?: lỗi)?|dòng(?: nguồn)?)\b",
                prefix + tail,
                re.IGNORECASE,
            )
        )
        monetary_units = bool(
            re.match(
                r"\s*(?:local currency|USD|VND|EUR|dollars?|đồng|nội tệ|[$€])(?:\b|(?<=[$€]))",
                prefix + tail,
                re.IGNORECASE,
            )
            or re.search(
                r"(?:\b(?:local currency|USD|VND|EUR|dollars?|đồng|nội tệ)|[$€])\s*$",
                CITATION.sub("", before)[: nearest.start()],
                re.IGNORECASE,
            )
        )
        if percent and unit["kind"] != "percentage":
            errors.append(
                f"Claim {claim['id']} has unit {unit['unit']}; a percent suffix is not valid"
            )
        if points and unit["kind"] != "percentage_point":
            errors.append(
                f"Claim {claim['id']} has unit {unit['unit']}; percentage points are not valid for this measure"
            )
        if event_units and unit["kind"] in {"cost", "policy_parameter", "cost_diagnostic"}:
            errors.append(
                f"Claim {claim['id']} has unit local currency; an event or row count unit is incompatible"
            )
        if event_units and unit["kind"] in {
            "calendar_day_ordinal",
            "calendar_day_count",
            "calendar_month_count",
            "product_count",
            "reference_output",
        }:
            errors.append(
                f"Claim {claim['id']} has unit {unit['unit']}; an event or row count unit is incompatible"
            )
        if unit["kind"] == "calendar_day_ordinal" and re.match(
            r"\s*(?:days|ngày)\b", prefix + tail, re.IGNORECASE
        ):
            errors.append(
                f"Claim {claim['id']} is a day-of-month position, not a count of elapsed or calendar days"
            )
        if (
            unit["kind"]
            in {
                "calendar_day_ordinal",
                "calendar_day_count",
                "calendar_month_count",
                "product_count",
                "count",
                "reference_output",
            }
            and monetary_units
        ):
            errors.append(
                f"Claim {claim['id']} has unit {unit['unit']}; a monetary unit is incompatible"
            )
        if unit["kind"] == "reference_output" and (
            re.match(
                r"\s*(?:(?:actual|verified) (?:production|output|produced units)|sản lượng thực tế)\b",
                prefix + tail,
                re.IGNORECASE,
            )
            or re.search(
                r"(?:(?:actual|verified) (?:production|output|produced units)|sản lượng thực tế)\s*[:=]?\s*$",
                CITATION.sub("", before)[: nearest.start()],
                re.IGNORECASE,
            )
        ):
            errors.append(
                f"Claim {claim['id']} uses static reference output; verified actual production is unavailable from this quantity"
            )
        if unit["kind"] == "unit_scale" and (event_units or percent or points or monetary_units):
            errors.append(
                f"Claim {claim['id']} is a metadata unit scale, not an observed measurement; use an explicit unit-scale definition or cite the actual count/cost."
            )
        if unit["kind"] == "identifier" and (event_units or percent or points or monetary_units):
            errors.append(
                f"Claim {claim['id']} is a source identifier, not an observed measurement; cite the actual count or cost for that quantity."
            )
        if unit["kind"] in {"cost", "policy_parameter", "cost_diagnostic"} and re.search(
            r"(?:\b(?:USD|VND|EUR|dollars?|đồng)\b|[$€])",
            adjacent + before[max(0, (nearest.start() - 12)) : nearest.start()],
            re.IGNORECASE,
        ):
            errors.append(
                f"Claim {claim['id']} uses unspecified local currency; the displayed currency is unsupported"
            )
        if unit["kind"] == "percentage" and re.match(
            r"\s*(?:local currency|USD|VND|events?|sự kiện)\b", prefix + tail, re.IGNORECASE
        ):
            errors.append(
                f"Claim {claim['id']} has unit percent; the displayed unit is incompatible"
            )
    return list(dict.fromkeys(errors))


def adjacent_value_error(numeric_text: str) -> str | None:
    """Two unseparated measurements are ambiguous, even when their values agree."""
    numbers = list(NUMBER.finditer(numeric_text))
    for left, right in zip(numbers, numbers[1:], strict=False):
        between = numeric_text[left.end() : right.start()]
        if between.isspace():
            return "Adjacent numeric values are ambiguous; write a separate labelled phrase for each measurement or join them with an explicit word/operator. A [value:ID] marker already inserts its number."
        if (
            left[0].rstrip("%") == right[0].rstrip("%")
            and re.fullmatch(r"[\s*_`A-Za-zÀ-ỹĐđ-]{1,160}", between)
            and not re.fullmatch(
                r"(?:of|out\s+of|trên|trong\s+tổng\s+số)",
                between.strip("*_` \t"),
                re.IGNORECASE,
            )
            and not re.search(
                r"\b(?:and|or|with|including|versus|vs|while|whereas|và|hoặc|cùng|còn|nhưng|gồm|so với|source label)\b",
                between,
                re.IGNORECASE,
            )
            and not re.search(
                r"\b(?:count|cost|intensity|numerator|denominator|baseline|comparison|selected|canonical|chi phí|cường độ|tử số|mẫu số)\s*(?:is|là)?\s*$",
                between,
                re.IGNORECASE,
            )
        ):
            return "A repeated numeric value follows an unseparated quantity phrase. Put [value:ID] once in that phrase, or keep its literal value with [claim:ID]; separate distinct measurements with their labels and a word/operator."
    for emphasized in re.finditer(
        r"(?P<mark>\*\*|__)(?P<phrase>(?:(?!(?P=mark))[^\n])+)(?P=mark)\s*(?P<echo>[-+]?\d(?:[\d.,]*\d)?%?)",
        numeric_text,
    ):
        echoed = emphasized["echo"].rstrip("%")
        if any(number[0].rstrip("%") == echoed for number in NUMBER.finditer(emphasized["phrase"])):
            return "An inserted number repeats a value already written in the immediately preceding emphasized phrase. Use the [value:ID] marker once inside that phrase, or keep the literal value with [claim:ID]; use a separate labelled clause/operator for a distinct measurement."
    return None


def structural_heading_text(text: str) -> str:
    """An ATX priority ordinal labels the document section, not a measurement."""
    return re.sub(
        r"^( {0,3}#{1,6}[ \t]+(?:Priority|Ưu tiên)[ \t]+)[1-9]\d*(?=[ \t]*:)",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )


def registered_identifiers(ledger: EvidenceLedger) -> list[str]:
    """Only exact read-back registered paths are source-backed identifiers."""
    found = []
    name_counts: dict[str, int] = {}
    for artifact in ledger.artifacts():
        name_counts[artifact["name"]] = name_counts.get(artifact["name"], 0) + 1
    for artifact in ledger.artifacts():
        try:
            path = ledger.resolve_artifact(artifact["path"])
            if (
                sha256(path.read_bytes()) == artifact["sha256"]
                and path.stat().st_size == artifact["size_bytes"]
            ):
                found.append(artifact["path"])
                if name_counts[artifact["name"]] == 1:
                    found.append(artifact["name"])
        except (KeyError, OSError, ValueError):
            continue
    return sorted(found, key=len, reverse=True)


def scope_description(scope: dict[str, Any], language: str) -> str:
    """One readable description for the portable header and its QA projection."""
    vi = language == "vi"
    labels = {
        "production_line": "Dây chuyền" if vi else "Production line",
        "category": "Nhóm sản phẩm" if vi else "Category",
        "product_id": "Mã sản phẩm" if vi else "Product ID",
        "severity": "Mức độ" if vi else "Severity",
        "defect_type": "Loại lỗi" if vi else "Defect type",
        "defect_location": "Vị trí lỗi" if vi else "Defect location",
        "inspection_method": "Phương pháp kiểm tra" if vi else "Inspection method",
    }
    period = (" đến " if vi else " to ").join(
        str(scope[key]) for key in ("start_month", "end_month") if scope.get(key)
    )
    selections = [label + ": " + str(scope[key]) for key, label in labels.items() if scope.get(key)]
    if not selections:
        selections = ["Toàn nhà máy" if vi else "All plant records"]
    return " · ".join(value for value in [period, *selections] if value)


def visible_scope_header(manifest: dict[str, Any]) -> dict[str, Any]:
    """Project exactly the visible source-context rows without rewriting prose."""
    vi = manifest.get("language") == "vi"
    roles = {
        "bound": "Ngữ cảnh ban đầu" if vi else "Bound context",
        "scope": "Phạm vi" if vi else "Scope",
        "baseline": "Kỳ gốc" if vi else "Baseline",
        "comparison": "Kỳ so sánh" if vi else "Comparison",
        "canonical": "Canonical",
        "selected": "Scenario được chọn" if vi else "Selected scenario",
    }
    contexts = manifest.get("contexts", [])
    rows = [
        {
            "context_id": context["context_id"],
            "label": ", ".join(roles[role] for role in context["roles"]),
            "text": scope_description(context["filters"], manifest["language"])
            + f"; scenario={context['scenario'] or 'canonical'}; context_id={context['context_id']}",
        }
        for context in contexts
    ]
    periods = {
        (context["filters"].get("start_month"), context["filters"].get("end_month"))
        for context in contexts
    }
    shared = next(iter(periods)) if len(periods) == 1 else None
    return {
        "title": "Phạm vi, phiên bản và đơn vị" if vi else "Scope, versions and units",
        "contexts": rows,
        "shared_period": {"start_month": shared[0], "end_month": shared[1]}
        if shared and all(shared)
        else None,
        "source_contexts_sha256": sha256(canonical(contexts)),
    }


def metadata_html(manifest: dict[str, Any]) -> str:
    """Visible portable metadata comes directly from the source manifest."""
    vi = manifest["language"] == "vi"
    rows = [
        ("Định dạng số" if vi else "Locale", manifest["locale"]),
        (
            "Phiên bản dữ liệu" if vi else "Dataset version",
            ", ".join(dict.fromkeys(value["dataset_version"] for value in manifest["contexts"]))
            or ("Không có ngữ cảnh dữ liệu được xác thực" if vi else "No verified dataset context"),
        ),
        (
            "Phiên bản chính sách" if vi else "Policy version",
            ", ".join(dict.fromkeys(value["policy_version"] for value in manifest["contexts"]))
            or ("Không có" if vi else "Unavailable"),
        ),
        (
            "Phiên bản instructions" if vi else "Instructions version",
            manifest["instructions_version"],
        ),
        ("Phiên bản tools" if vi else "Tools version", manifest["tools_version"]),
    ]
    header = visible_scope_header(manifest)
    rows.extend((row["label"], row["text"]) for row in header["contexts"])
    units = list(
        dict.fromkeys(
            value["unit"]["unit"] for value in manifest["references"] if value.get("unit")
        )
    )
    if units:
        rows.append(("Đơn vị" if vi else "Units", "; ".join(units)))
    return (
        '<aside class="report-metadata"><h2>'
        + header["title"]
        + "</h2><dl>"
        + "".join(
            "<dt>" + escape(str(key)) + "</dt><dd>" + escape(str(value)) + "</dd>"
            for key, value in rows
        )
        + "</dl></aside>"
    )


def references_html(manifest: dict[str, Any]) -> str:
    if not manifest["references"]:
        return ""
    rows = []
    for index, reference in enumerate(manifest["references"], 1):
        context = ", ".join(reference["context_ids"]) or (
            "Không có" if manifest["language"] == "vi" else "Unavailable"
        )
        unit = (
            reference["unit"]["unit"]
            if reference.get("unit")
            else ("Chưa khai báo" if manifest["language"] == "vi" else "Not declared")
        )
        labels = ", ".join(
            str(key) + ": " + str(value) for key, value in reference.get("labels", {}).items()
        )
        label_text = (
            "; "
            + ("Nhãn nguồn: " if manifest["language"] == "vi" else "Source labels: ")
            + escape(labels)
            if labels
            else ""
        )
        rows.append(
            '<li id="evidence-'
            + str(index)
            + '"><code>'
            + escape(reference["id"])
            + "</code>: <code>"
            + escape(reference["execution_id"])
            + "</code>, <code>"
            + escape(reference["path"])
            + "</code>; "
            + escape(unit)
            + label_text
            + "; context_id="
            + escape(context)
            + "; code_sha256="
            + escape(reference["code_sha256"])
            + "</li>"
        )
    title = "Bằng chứng có thể đối chiếu" if manifest["language"] == "vi" else "Evidence references"
    # JSON is escaped so source labels cannot terminate the inert data element.
    data = (
        canonical(manifest).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    return (
        '<section class="report-references"><h2>'
        + title
        + "</h2><ol>"
        + "".join(rows)
        + '</ol></section><script type="application/json" id="report-evidence">'
        + data
        + "</script>"
    )
