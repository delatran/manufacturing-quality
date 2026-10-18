"""Small model views over unchanged, sealed analysis receipts.

Views copy original values and preserve their JSON paths. Navigation is separate
from measurement evidence; the complete result remains in the local ledger.
"""

from __future__ import annotations

import re
from typing import Any

from .claim_feedback import source_contexts
from .evidence import canonical, numeric_values, sha256

VIEW_BYTES = 20000
HEADLINES = {
    "event_count",
    "critical_count",
    "critical_share_pct",
    "eligible_event_count",
    "reference_output",
    "reference_intensity_per_1000",
    "reference_intensity_status",
    "known_repair_cost",
    "cost_known_count",
    "cost_unknown_count",
    "cost_coverage_pct",
    "cost_available",
    "cost_status",
    "unmatched_event_count",
}
LABELS = {
    "name",
    "label",
    "month",
    "production_line",
    "category",
    "product_id",
    "product_name",
    "defect_type",
    "defect_location",
    "severity",
    "inspection_method",
}
METADATA = {
    "dataset_version",
    "policy_version",
    "scenario",
    "scenario_definition",
    "currency",
    "limitations",
    "reference_output_note",
    "source_url",
    "source_repository_url",
    "stale",
}


def is_product_entry(entry: dict[str, Any]) -> bool:
    result = entry.get("result", {})
    return (
        isinstance(result, dict)
        and result.get("ok") is not False
        and (
            entry.get("execution_kind") == "bounded_analysis"
            or entry.get("execution_kind") == "canonical_receipt_import"
            and isinstance(result.get("metadata"), dict)
            and "dataset_version" in result["metadata"]
        )
    )


def headlines(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key in HEADLINES}


def focused_row(value: Any) -> Any:
    """Remove repeated KPI populations from comparison rows, never alter values."""
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("baseline"), dict) and isinstance(value.get("comparison"), dict):
        row = {key: item for key, item in value.items() if key not in {"baseline", "comparison"}}
        for side in ("baseline", "comparison"):
            row[side] = {
                key: item
                for key, item in value[side].items()
                if key
                in {"event_count", "critical_count", "known_repair_cost", "cost_coverage_pct"}
            }
        return row
    if "event_count" in value and len(value) > 16:
        return {
            key: item
            for key, item in value.items()
            if key in HEADLINES | LABELS
            or "share" in key
            or "cumulative" in key
            or key.endswith("_rank")
        }
    return value


def context_view(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key
        in {"context_id", "snapshot_id", "dataset_version", "policy_version", "scenario", "filters"}
    }


def source_projection(
    entry: dict[str, Any], *, canonical_definitions: dict[str, str] | None = None
) -> dict[str, Any]:
    """Share declared source prose and complete rank changes with analyst and QA.

    Section-query declarations live on their wrapper, not inside its selected
    section. Comparison sides have separate contexts. Copy the actual strings
    with their original paths; do not promote authored work into source data.
    Rank positions describe the original arrays and are navigation metadata,
    not new measurements or numeric-claim paths.
    """
    if not is_product_entry(entry) or entry.get("tool") not in {
        "analysis_query",
        "analysis_sql",
        "analysis_compare",
        "scenario_compare",
        "dashboard_context",
        "dashboard_compare_scope",
    }:
        return {}
    result = entry["result"]
    if (
        sha256(canonical(result)) != entry.get("result_sha256")
        or sha256(entry.get("code", "")) != entry.get("code_sha256")
        or sha256(canonical({key: value for key, value in entry.items() if key != "entry_sha256"}))
        != entry.get("entry_sha256")
    ):
        raise ValueError("Declared source projection requires an unchanged result seal")
    sides = (
        ("baseline", "comparison")
        if entry["tool"] == "analysis_compare"
        else ("canonical", "selected")
        if entry["tool"] == "scenario_compare"
        else ()
    )
    contexts: dict[str, dict[str, Any]] = {}

    def bind(path: str) -> list[str]:
        bindings = source_contexts(entry, path)
        expected = (
            1
            if not sides or any(path.startswith("$." + side + ".") for side in sides)
            else len(sides)
        )
        # Identical comparison scopes may share one context identifier.
        if sides and expected == len(sides):
            bindings = [
                binding for side in sides for binding in source_contexts(entry, "$." + side + ".")
            ]
        if len(bindings) != expected:
            raise ValueError("Declared source projection requires explicit valid contexts: " + path)
        for binding in bindings:
            identity = binding["context_id"]
            value = context_view(binding)
            if identity in contexts and contexts[identity] != value:
                raise ValueError("A declared source context has conflicting scope metadata")
            contexts[identity] = value
        if (
            len(
                {
                    (item["snapshot_id"], item["dataset_version"], item["policy_version"])
                    for item in bindings
                }
            )
            != 1
        ):
            raise ValueError("A declared comparison requires one frozen dataset and policy")
        return list(dict.fromkeys(binding["context_id"] for binding in bindings))

    containers = [("$", result)]
    if isinstance(result.get("analysis"), dict):
        containers.append(("$.analysis", result["analysis"]))
    if isinstance(result.get("decomposition"), dict):
        containers.append(("$.decomposition", result["decomposition"]))
    for side in sides:
        wrapper = result.get(side)
        if isinstance(wrapper, dict):
            containers.append(("$." + side, wrapper))
            if isinstance(wrapper.get("analysis"), dict):
                containers.append(("$." + side + ".analysis", wrapper["analysis"]))
    if entry["tool"] == "scenario_compare" and isinstance(result.get("comparison"), dict):
        containers.append(("$.comparison", result["comparison"]))
    declarations: dict[tuple[str, str], dict[str, Any]] = {}
    for path, value in containers:
        declared = [(path, value)]
        for key in ("metadata", "anomalies"):
            if isinstance(value.get(key), dict):
                declared.append((path + "." + key, value[key]))
        for prefix, declaration in declared:
            for field in (
                "definitions",
                "limitations",
                "interpretation",
                "currency",
                "reference_output_note",
            ):
                texts = declaration.get(field)
                source_path = prefix + "." + field
                valid = (
                    isinstance(texts, str)
                    or isinstance(texts, list)
                    and all(isinstance(text, str) for text in texts)
                    or field == "definitions"
                    and isinstance(texts, dict)
                    and all(isinstance(text, str) for text in texts.values())
                )
                if not valid or not texts:
                    continue
                record = declarations.setdefault(
                    (field, canonical(texts)), {"field": field, "value": texts, "sources": []}
                )
                if field == "definitions" and texts == canonical_definitions:
                    record.pop("value", None)
                    record["value_reference"] = "canonical_definitions"
                    record["value_sha256"] = sha256(canonical(texts))
                record["sources"].append({"path": source_path, "context_ids": bind(source_path)})

    rankings = []
    if entry["tool"] == "scenario_compare":
        groups = result.get("comparison", {}).get("rankings", {})
        for metric in (
            "lines_by_reference_intensity",
            "defect_types_by_known_cost",
            "products_by_known_cost",
        ):
            if metric not in groups:
                continue
            ranking = groups[metric]
            path = "$.comparison.rankings." + metric
            if not isinstance(ranking, dict) or not all(
                isinstance(ranking.get(side), list)
                and all(type(value) in {str, int} for value in ranking[side])
                and len(set(ranking[side])) == len(ranking[side])
                for side in ("canonical", "selected")
            ):
                raise ValueError(
                    "Ranking projection requires both complete original identity arrays: " + path
                )
            before, after = ranking["canonical"], ranking["selected"]
            changes = []
            for index in range(max(len(before), len(after))):
                left = before[index] if index < len(before) else None
                right = after[index] if index < len(after) else None
                if left != right:
                    changes.append([index + 1, left, right])
            if ranking.get("changed") is not (before != after):
                raise ValueError("Ranking change flag conflicts with its original arrays: " + path)
            rankings.append(
                {
                    "path": path,
                    "context_ids": bind(path),
                    "canonical_size": len(before),
                    "selected_size": len(after),
                    "complete_source_arrays": True,
                    "columns": ["position", "canonical", "selected"],
                    "changed_positions": changes,
                }
            )
    if not declarations and not rankings:
        return {}
    return {
        "source": {
            key: entry[key]
            for key in ("execution_id", "result_sha256", "code_sha256", "entry_sha256")
        },
        "contexts": list(contexts.values()),
        "declared_metadata": list(declarations.values()),
        "ranking_changes": rankings,
        **(
            {
                "derivation": {
                    "query": result["query"],
                    "query_sha256": result["query_sha256"],
                    "inputs": result["tables"],
                    "interpretation": "Review the retained SQL expressions to interpret derived columns. A column alias alone does not establish equivalence to a canonical KPI or unit.",
                }
            }
            if entry["tool"] == "analysis_sql"
            else {}
        ),
        **(
            {
                "ranking_method": "Every differing position in the complete declared source arrays, including additions/removals. columns names each row; position is one-based navigation. Original path = group path + '.' + side + '[' + (position-1) + ']'. Numeric IDs cover preview/first changed leaves only; read other original leaves with evidence_detail. A preview alone cannot establish exclusivity."
            }
            if rankings
            else {}
        ),
    }


def ranking_values(projection: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """Cite visible changed identities at their original sealed array paths."""
    values = {}
    for group in projection.get("ranking_changes", []):
        for position, before, after in group["changed_positions"][:limit]:
            for side, value in (("canonical", before), ("selected", after)):
                if type(value) is int:
                    values[f"{group['path']}.{side}[{position - 1}]"] = value
    return [{"path": path, "value": value} for path, value in values.items()]


def detail_navigation(
    receipt_id: str, path: str, offset: int, count: int, item_count: int, limit: int
) -> dict[str, Any]:
    """The exact structural page metadata counted in the SDK response bound."""
    next_offset = offset + count
    return {
        "path": path,
        "offset": offset,
        "returned_count": count,
        "item_count": item_count,
        "next_offset": next_offset if next_offset < item_count else None,
        "next_call": {
            "receipt_id": receipt_id,
            "path": path,
            "offset": next_offset,
            "limit": min(limit, max(1, count)),
        }
        if next_offset < item_count
        else None,
    }


def focused_result(
    entry: dict[str, Any], page_size: int = 5, *, projection: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Expose the requested analysis, rather than every nested report it contains."""
    result = entry["result"]
    tool = entry["tool"]
    if tool in {"dashboard_context", "dashboard_compare_scope"}:
        value = {
            key: item
            for key, item in result.items()
            if key
            in {
                "scope",
                "kpis",
                "monthly",
                "trend",
                "definitions",
                "metric_metadata",
                "metadata",
                "line_comparison_note",
            }
        }
    elif tool == "analysis_compare":
        value = {
            key: result[key] for key in ("ok", "decomposition", "source_status") if key in result
        }
        value["decomposition"] = {
            key: item
            for key, item in result["decomposition"].items()
            if key not in {"baseline", "comparison", "metadata", "source_contexts"}
        }
        for side in ("baseline", "comparison"):
            source = result[side]
            value[side] = {
                "context": context_view(source["context"]),
                "analysis": {"kpis": headlines(source["analysis"]["kpis"])},
            }
    elif tool == "scenario_compare":
        value = {key: result[key] for key in ("ok", "comparison", "source_status") if key in result}
        value["comparison"] = {
            **result["comparison"],
            "metric_deltas": {
                metric: {"delta": values["delta"]}
                for metric, values in result["comparison"].get("metric_deltas", {}).items()
            },
        }
        for side in ("canonical", "selected"):
            source = result[side]
            value[side] = {
                "context": context_view(source["context"]),
                "analysis": {
                    "kpis": headlines(source["analysis"]["kpis"]),
                    "metadata": {
                        key: item
                        for key, item in source["analysis"].get("metadata", {}).items()
                        if key
                        not in {
                            "currency",
                            "limitations",
                            "reference_output_note",
                            "scenario_definition",
                        }
                    },
                },
            }
    else:
        value = result
        if tool == "analysis_query" and isinstance(
            result.get("analysis", {}).get("decomposition"), dict
        ):
            dimensions = result["analysis"]["decomposition"]
            if "production_line" in dimensions:
                value = {
                    **result,
                    "analysis": {
                        **result["analysis"],
                        "decomposition": {"production_line": dimensions["production_line"]},
                    },
                }
    omitted: list[dict[str, Any]] = []
    declared_paths = {
        source["path"]
        for record in (projection or {}).get("declared_metadata", [])
        for source in record["sources"]
    }

    def visit(item: Any, path: str) -> Any:
        if isinstance(item, dict):
            if "dimension" in item and isinstance(item.get("rows"), list):
                item = {
                    key: {"scope": child.get("scope"), "kpis": headlines(child.get("kpis", {}))}
                    if key in {"baseline", "comparison"} and isinstance(child, dict)
                    else child
                    for key, child in item.items()
                    if key not in {"metadata", "source_contexts"}
                }
            if path.endswith(".context"):
                item = context_view(item)
            if path.endswith(".metadata"):
                item = {key: child for key, child in item.items() if key in METADATA}
            if path.endswith(".top_product_cluster.kpis"):
                item = headlines(item)
            if path.endswith(".products"):
                # The sorted top lists answer the common ranking questions;
                # full cohort lists stay accessible by their original paths.
                item = {
                    key: child
                    for key, child in item.items()
                    if key not in {"all", "intensity_all", "cost_all"}
                }
            # The shared projection preserves each declaration once, including
            # its original paths and contexts. Do not repeat the same prose in
            # the data preview; detail retrieval still reads the sealed value.
            return {
                key: visit(child, path + "." + key)
                for key, child in item.items()
                if path + "." + key not in declared_paths
            }
        if isinstance(item, list) and re.fullmatch(
            r"\$\.comparison\.rankings\.(?:products_by_known_cost|lines_by_reference_intensity|defect_types_by_known_cost)\.(?:canonical|selected)",
            path,
        ):
            count = len(item) if len(item) <= 12 else page_size
            if count < len(item):
                omitted.append(
                    {
                        "path": path,
                        "item_count": len(item),
                        "returned_count": count,
                        "next_offset": count,
                    }
                )
            return item[:count]
        if not isinstance(item, list) or not item or not all(isinstance(row, dict) for row in item):
            return item
        # The primary monthly series remains together. Nested product-cluster
        # series retain their own original paths and page like other detail.
        count = min(
            len(item),
            max(6, page_size) if path in {"$.monthly", "$.analysis.monthly"} else page_size,
        )
        if count < len(item):
            omitted.append(
                {
                    "path": path,
                    "item_count": len(item),
                    "returned_count": count,
                    "next_offset": count,
                }
            )
        return [
            visit(focused_row(row), f"{path}[{index}]") for index, row in enumerate(item[:count])
        ]

    return visit(value, "$"), omitted


def focused_detail(selected: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    return [(path, focused_row(value)) for path, value in selected]


def view_weight(
    value: Any, numbers: list[dict[str, Any]], omitted: list[dict[str, Any]] | None = None
) -> int:
    """Include citation/path overhead when choosing a page, not only raw values."""
    return len(
        canonical(
            {
                "result": value,
                "numeric_evidence": [{"id": "r00000000_n999999", **number} for number in numbers],
                "omitted_collections": omitted or [],
            }
        ).encode("utf-8")
    )


def bounded_result(
    entry: dict[str, Any], *, projection: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    projection = source_projection(entry) if projection is None else projection
    projection_bytes = len(canonical(projection).encode("utf-8")) if projection else 0
    for page_size in range(5, 0, -1):
        value, omitted = focused_result(entry, page_size, projection=projection)
        numbers = {
            number["path"]: number
            for number in [*numeric_values(value), *ranking_values(projection, page_size)]
        }
        if view_weight(value, list(numbers.values()), omitted) + projection_bytes <= VIEW_BYTES:
            return value, omitted
    return value, omitted


def qa_comparisons(entry: dict[str, Any], claim_paths: list[str]) -> list[dict[str, Any]]:
    """Give QA actual rows and whole-population extrema without hidden catalogs.

    Extrema select original records, including ties. No metric is recalculated.
    Large groups contain their first rows, cited rows and exact extrema paths.
    """
    visible, _ = focused_result(entry)
    paths: set[str] = set()

    def collections(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                collections(child, path + "." + key)
        elif isinstance(item, list) and item and all(isinstance(row, dict) for row in item):
            paths.add(path)

    def at(path: str) -> Any:
        value: Any = entry["result"]
        for key, index in re.findall(r"\.([A-Za-z_][A-Za-z_0-9]*)|\[(\d+)\]", path):
            value = value[key] if key else value[int(index)]
        return value

    collections(visible, "$")
    for path in claim_paths:
        match = re.match(r"(.*)\[\d+\]", path)
        if match:
            paths.add(match[1])
    output = []
    for path in sorted(paths):
        rows = at(path)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            continue
        selected = set(range(min(len(rows), 12 if len(rows) <= 12 else 5)))
        for claim in claim_paths:
            match = re.match(re.escape(path) + r"\[(\d+)\]", claim)
            if match:
                selected.add(int(match[1]))
        extrema = []
        fields: dict[str, list[tuple[int, Any]]] = {}
        for index, row in enumerate(rows):
            for number in numeric_values(focused_row(row)):
                fields.setdefault(number["path"], []).append((index, number["value"]))
        for field, values in fields.items():
            low, high = min(value for _, value in values), max(value for _, value in values)

            def extreme(
                value: Any,
                *,
                path: str = path,
                field: str = field,
                values: list[tuple[int, Any]] = values,
            ) -> dict[str, Any]:
                matches = [f"{path}[{index}]{field[1:]}" for index, item in values if item == value]
                return {"value": value, "matching_rows": len(matches), "example_paths": matches[:3]}

            extrema.append(
                {
                    "field": field,
                    "minimum": extreme(low),
                    "maximum": extreme(high),
                }
            )
        output.append(
            {
                "execution_id": entry["execution_id"],
                "path": path,
                "scope": entry.get("analysis_context", {}).get("filters"),
                "population_size": len(rows),
                "rows": [
                    {**focused_row(rows[index]), "source_path": f"{path}[{index}]"}
                    for index in sorted(selected)
                ],
                "extrema": extrema if len(rows) > 12 else [],
                "method": "Original rows; extrema select exact values over the complete source collection. matching_rows counts all ties; example_paths shows up to three exact matching source paths. Display order is the backend order.",
            }
        )
    return output
