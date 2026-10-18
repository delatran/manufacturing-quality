"""Source-bound identifier labels and compact, actionable citation feedback."""

from __future__ import annotations

import re
from calendar import month_abbr, month_name
from collections.abc import Callable
from datetime import date
from typing import Any

from .evidence import canonical

_PART = re.compile(r"\.([A-Za-z_][A-Za-z_0-9]*)|\[(\d+)\]")
_LABELS = {
    "name",
    "label",
    "month",
    "product_id",
    "product_name",
    "production_line",
    "category",
    "defect_type",
    "defect_location",
    "location",
    "severity",
    "inspection_method",
    "defect_id",
}
_MEASURES = {
    "event_count",
    "event_count_change",
    "critical_count",
    "critical_count_change",
    "known_repair_cost",
    "known_repair_cost_change",
    "cost_coverage_pct",
    "cost_unknown_count",
    "reference_intensity_per_1000",
    "reference_intensity_change",
}
_MEASURE_ORDER = (
    "event_count",
    "event_count_change",
    "known_repair_cost",
    "cost_coverage_pct",
    "cost_unknown_count",
    "critical_count",
    "critical_count_change",
    "reference_intensity_per_1000",
    "known_repair_cost_change",
    "reference_intensity_change",
)


def source_at(value: Any, path: str) -> Any:
    position = 1
    if not path.startswith("$"):
        raise ValueError("Invalid source path")
    for match in _PART.finditer(path):
        if match.start() != position:
            raise ValueError("Invalid source path")
        value = value[match[1]] if match[1] is not None else value[int(match[2])]
        position = match.end()
    if position != len(path):
        raise ValueError("Invalid source path")
    return value


def unit_scale_descriptor(entry: dict[str, Any], path: str) -> dict[str, Any] | None:
    """Resolve an actual analytical unit declaration, never a measured quantity."""
    if (
        entry.get("execution_kind") not in {"bounded_analysis", "canonical_receipt_import"}
        or entry.get("tool")
        not in {
            "analysis_query",
            "analysis_compare",
            "scenario_compare",
            "dashboard_context",
            "dashboard_compare_scope",
        }
        or not re.search(r"\.metric_metadata\.[A-Za-z_][A-Za-z_0-9]*\.scale$", path)
    ):
        return None
    try:
        descriptor = source_at(entry["result"], path.rpartition(".")[0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return (
        descriptor
        if isinstance(descriptor, dict) and type(descriptor.get("scale")) in {int, float}
        else None
    )


def numeric_role(entry: dict[str, Any], path: str) -> str:
    """Distinguish declared identities and numeric settings from measures."""
    if entry.get("execution_kind") not in {"bounded_analysis", "canonical_receipt_import"}:
        return "measurement"
    if (
        entry.get("tool") == "analysis_compare"
        and path == "$.decomposition.known_cost_reconciliation.display_decimal_places"
    ):
        return "display_precision"
    if unit_scale_descriptor(entry, path) is not None:
        return "unit_scale"
    if path.rpartition(".")[2] in {"product_id", "defect_id"} or re.fullmatch(
        r"\$\.comparison\.priority_signals\.(?:canonical|selected)\.highest_known_cost_product_id",
        path,
    ):
        return "identifier"
    # This declared ranking stores product identities, not their cost values.
    # Other scalar arrays retain their original numeric role.
    if entry.get("tool") == "scenario_compare" and re.fullmatch(
        r"\$\.comparison\.rankings\.products_by_known_cost\.(?:canonical|selected)\[\d+\]",
        path,
    ):
        try:
            identifier = source_at(entry["result"], path)
        except (KeyError, IndexError, TypeError, ValueError):
            return "measurement"
        if type(identifier) is int and identifier > 0:
            return "identifier"
    if entry.get("tool") in {"scenario_compare", "analysis_query"} and re.search(
        r"\.scenario_definition\.parameters\.repair_cost_equal$", path
    ):
        try:
            parameters = source_at(entry["result"], path.rpartition(".")[0])
        except (KeyError, IndexError, TypeError, ValueError):
            return "measurement"
        if isinstance(parameters, dict) and (
            parameters.get("repair_cost_unit") == "local currency"
            and parameters.get("effect") == "withhold_financial_availability"
        ):
            return "policy_parameter"
    return "measurement"


def source_contexts(entry: dict[str, Any], path: str = "") -> list[dict[str, Any]]:
    """Select explicit frozen contexts; comparison metadata belongs to both sides."""
    if (
        entry.get("execution_kind") not in {"bounded_analysis", "canonical_receipt_import"}
        or entry.get("tool")
        not in {
            "analysis_query",
            "analysis_compare",
            "scenario_compare",
            "dashboard_context",
            "dashboard_compare_scope",
            "analysis_records",
            "analysis_provenance",
            "analysis_export",
            "analysis_sql",
        }
        or not isinstance(entry.get("result"), dict)
        or entry["result"].get("ok") is False
    ):
        return []
    result = entry["result"]
    sides = (
        ("baseline", "comparison")
        if entry["tool"] == "analysis_compare"
        else ("canonical", "selected")
        if entry["tool"] == "scenario_compare"
        else ()
    )
    selected = [side for side in sides if path.startswith("$." + side + ".")]
    candidates = [
        child.get("context") if isinstance(child, dict) else None
        for side in (selected or sides)
        for child in [result.get(side)]
    ]
    if not sides:
        candidates = [result.get("context") or entry.get("analysis_context")]
    if not path:
        candidates += [result.get("context"), entry.get("analysis_context")]
    contexts = []
    for value in candidates:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("filters"), dict)
            or not all(
                value.get(key)
                for key in ("context_id", "snapshot_id", "dataset_version", "policy_version")
            )
            or "scenario" not in value
            or value["scenario"] != value["filters"].get("scenario", "")
        ):
            continue
        if value not in contexts:
            contexts.append(value)
    return contexts


def _half_period_ranges(entry: dict[str, Any]) -> set[tuple[str, str]]:
    """Read only an enabled query's exact, contiguous partition of its frozen scope."""
    result = entry.get("result")
    if (
        entry.get("tool") != "analysis_query"
        or entry.get("execution_kind") != "bounded_analysis"
        or not isinstance(result, dict)
        or result.get("ok") is not True
    ):
        return set()
    analysis = result.get("analysis")
    halves = analysis.get("period_halves") if isinstance(analysis, dict) else None
    if not isinstance(halves, dict) or halves.get("enabled") is not True:
        return set()
    first, second = halves.get("first"), halves.get("second")
    if not isinstance(first, dict) or not isinstance(second, dict):
        return set()
    first_months, second_months = first.get("months"), second.get("months")
    if (
        not isinstance(first_months, list)
        or not isinstance(second_months, list)
        or not first_months
        or len(first_months) != len(second_months)
    ):
        return set()
    contexts = source_contexts(entry)
    if len(contexts) != 1:
        return set()
    scope = contexts[0]["filters"]
    months = first_months + second_months
    if months[0] != scope.get("start_month") or months[-1] != scope.get("end_month"):
        return set()
    positions = []
    for month in months:
        if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
            return set()
        try:
            value = date.fromisoformat(month + "-01")
        except ValueError:
            return set()
        positions.append(value.year * 12 + value.month)
    if any(last != first + 1 for first, last in zip(positions, positions[1:], strict=False)):
        return set()
    return {(first_months[0], first_months[-1]), (second_months[0], second_months[-1])}


def calendar_text_checker(
    entries: dict[str, dict[str, Any]],
) -> Callable[[str], tuple[str, list[dict[str, Any]]]]:
    """Prepare calendar matching from one audit's frozen evidence snapshot.

    The returned checker holds only derived periods and patterns, never the
    ledger or its mutable entries. A new audit must prepare a new checker.
    """
    periods = {
        (scope["start_month"], scope["end_month"])
        for entry in entries.values()
        for context in source_contexts(entry)
        for scope in [context["filters"]]
        if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(scope.get("start_month", "")))
        and re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(scope.get("end_month", "")))
        and scope["start_month"] <= scope["end_month"]
    }
    for entry in entries.values():
        periods.update(_half_period_ranges(entry))
    months = {
        word.casefold(): index
        for index in range(1, 13)
        for word in (month_name[index], month_abbr[index])
    }
    month = "(?:" + "|".join(sorted(months, key=len, reverse=True)) + ")"
    patterns = (
        re.compile(
            r"(?<!\w)(?P<first>" + month + r")\s*(?P<first_year>\d{4})?\s*"
            r"(?:-|–|\bto\b|\bthrough\b)\s*(?P<last>" + month + r")\s*,?\s*(?P<year>\d{4})(?!\w)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?<![\w/])(?:tháng\s+)?(?P<first>\d{1,2})\s*(?:-|–|\bđến\b)\s*"
            r"(?:tháng\s+)?(?P<last>\d{1,2})\s*(?:/\s*|năm\s+)(?P<year>\d{4})(?![\w/])",
            re.IGNORECASE,
        ),
    )

    def check(text: str) -> tuple[str, list[dict[str, Any]]]:
        issues: list[dict[str, Any]] = []

        def replace(match: re.Match[str]) -> str:
            numeric = match["first"].isdigit()
            first = int(match["first"]) if numeric else months[match["first"].casefold()]
            last = int(match["last"]) if numeric else months[match["last"].casefold()]
            start_year = match.groupdict().get("first_year") or match["year"]
            period = (f"{start_year}-{first:02}", f"{match['year']}-{last:02}")
            wrong_quantity = bool(
                re.search(
                    r"(?:\b(?:event\s+count|count|repair\s+cost|cost)|số\s+sự\s+kiện|chi\s+phí)\s*(?:is|equals?|=|là|bằng)\s*$",
                    text[: match.start()],
                    re.IGNORECASE,
                )
                or re.match(
                    r"\s*(?:local currency|USD|VND|EUR|dollars?|đồng|nội tệ)\b",
                    text[match.end() :],
                    re.IGNORECASE,
                )
                or numeric
                and re.match(
                    r"\s*(?:events?|counts?|rows?|sự kiện)\b",
                    text[match.end() :],
                    re.IGNORECASE,
                )
            )
            if (
                not 1 <= first <= 12
                or not 1 <= last <= 12
                or period not in periods
                or wrong_quantity
            ):
                issues.append(
                    {
                        "kind": "unsupported_calendar_label",
                        "label": match[0],
                        "reason": "A calendar range labels a period, not a measured count or cost."
                        if wrong_quantity
                        else "The whole range must match an explicit frozen source period.",
                        "available_periods": [list(value) for value in sorted(periods)],
                    }
                )
            # Invalid ranges are also removed from the numeric projection, but their
            # explicit error prevents a partial date label or equal count passing.
            return " source-bound calendar range "

        for pattern in patterns:
            text = pattern.sub(replace, text)

        def replace_months(match: re.Match[str]) -> str:
            first, last = int(match["first"]), int(match["last"])
            matches = [
                period
                for period in periods
                if period[0][:4] == period[1][:4]
                and int(period[0][5:]) == first
                and int(period[1][5:]) == last
            ]
            if (
                len(matches) != 1
                or re.match(
                    r"\s*(?:events?|counts?|rows?|sự kiện)\b", text[match.end() :], re.IGNORECASE
                )
                or re.search(
                    r"(?:\b(?:count|repair\s+cost|cost)|số\s+sự\s+kiện|chi\s+phí)\s*"
                    r"(?:is|equals?|=|là|bằng)\s*$",
                    text[: match.start()],
                    re.IGNORECASE,
                )
            ):
                issues.append(
                    {
                        "kind": "unsupported_calendar_label",
                        "label": match[0],
                        "reason": "A yearless month range must identify one explicit frozen source period.",
                        "available_periods": [list(value) for value in sorted(periods)],
                    }
                )
            return " source-bound calendar range "

        text = re.sub(
            r"(?<!\w)tháng\s+(?P<first>\d{1,2})\s*(?:-|–|\bđến\b)\s*"
            r"(?:tháng\s+)?(?P<last>\d{1,2})(?![\d/])",
            replace_months,
            text,
            flags=re.IGNORECASE,
        )
        return text, issues

    return check


def typed_calendar_text(
    text: str, entries: dict[str, dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Bind a whole natural calendar range before partial date labels are masked."""
    return calendar_text_checker(entries)(text)


_REPAIR_CRITERION = (
    r"((?:repair costs?\s+(?:equal(?:s)?(?:\s+to)?|=)|"
    r"chi phí(?:\s+sửa chữa)?\s+(?:đúng\s+)?(?:bằng|=))\s*)"
)
_CLAIM = re.compile(r"\[claim:(?:[^\[\]]|\[\d+\])+\]")


def _policy_purpose(text: str, start: int, end: int) -> bool:
    """A selector or availability definition cannot stand in for a measured total."""
    before, after = _CLAIM.sub(" ", text[:start]), _CLAIM.sub(" ", text[end:])
    if re.search(
        r"(?:\b(?:observed|recorded|actual|aggregate|total|subtotal|known)|quan\s+sát(?:\s+được)?|ghi\s+nhận|tổng)\s*$",
        before,
        re.IGNORECASE,
    ):
        return False
    selector = re.search(
        r"(?:\b(?:withhold(?:s|ing)?|exclud(?:e|es|ing))|"
        r"(?:scenario\s+)?(?:assumption|criterion|threshold)\s*(?::|=|is)|"
        r"(?:giả\s+định(?:\s+kịch\s+bản)?|tiêu\s+chí(?:\s+kịch\s+bản)?|ngưỡng)\s*(?::|=|là)|"
        r"loại\s+trừ|tạm\s+loại)\s*$",
        before,
        re.IGNORECASE,
    )
    availability = re.match(
        r"\s*(?:\(?(?:local currency|nội tệ)\)?\s*)?"
        r"(?:(?P<copula>are|is|will be|as)\s+)?(?:financially unavailable|withheld(?:\s+financially)?|"
        r"excluded\s+from\s+(?:financial\s+availability|cost\s+availability)|"
        r"không\s+được\s+dùng\s+để\s+tính\s+chi\s+phí)",
        after,
        re.IGNORECASE,
    )
    return bool(
        selector
        or availability
        and (
            (availability["copula"] or "").casefold() != "as"
            or re.search(r"\btreats?\s*$", before, re.IGNORECASE)
        )
    )


def policy_parameter_usage(line: str, claim: dict[str, Any], marker_start: int) -> bool:
    """Bind this parameter citation to its own local criterion, including collisions."""
    value = str(int(claim["value"])) if float(claim["value"]).is_integer() else str(claim["value"])
    for match in re.finditer(
        _REPAIR_CRITERION + re.escape(value) + r"(?![\d.,])", line[:marker_start], re.IGNORECASE
    ):
        if re.fullmatch(
            r"\s*(?:local currency|nội tệ)?\s*", line[match.end() : marker_start], re.IGNORECASE
        ) and _policy_purpose(line, match.start(), match.end()):
            return True
    return False


def typed_policy_text(
    text: str, claims: list[dict[str, Any]], entries: dict[str, dict[str, Any]]
) -> str:
    """Only a cited typed criterion can support its explicit assumption phrase."""
    for entry in entries.values():
        if (
            entry.get("tool") not in {"scenario_compare", "analysis_query"}
            or entry.get("result", {}).get("ok") is False
        ):
            continue
        result = entry["result"]
        candidates = [
            result.get("context", {}),
            result.get("comparison", {}),
            result.get("selected", {}).get("context", {}),
        ]
        for source in candidates:
            scenario = source.get("scenario")
            if isinstance(scenario, str) and scenario:
                text = re.sub(
                    r"(?<![\w-])" + re.escape(scenario) + r"(?![\w-])", " scenario ", text
                )
    for claim in claims:
        entry = entries.get(claim["execution_id"], {})
        if numeric_role(entry, claim["path"]) != "policy_parameter":
            continue
        try:
            if source_at(entry["result"], claim["path"]) != claim["value"]:
                continue
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        value = str(claim["value"])
        if float(claim["value"]).is_integer():
            value = str(int(claim["value"]))
        text = re.sub(
            _REPAIR_CRITERION + re.escape(value) + r"(?![\d.,])",
            lambda match: (
                match[1] + "[cited policy criterion]"
                if _policy_purpose(match.string, match.start(), match.end())
                else match[0]
            ),
            text,
            flags=re.IGNORECASE,
        )
    return text


def reference_context(entry: dict[str, Any], path: str) -> dict[str, Any]:
    """Keep the actual period and row label next to an issued measurement."""
    result = entry["result"]
    scope = entry.get("analysis_context", {}).get("filters", {})
    labels: dict[str, Any] = {}
    for match in _PART.finditer(path):
        try:
            parent = source_at(result, path[: match.start()])
        except (KeyError, IndexError, TypeError, ValueError):
            break
        if not isinstance(parent, dict):
            continue
        binding = parent.get("context", {})
        if isinstance(binding, dict) and isinstance(binding.get("filters"), dict):
            scope = binding["filters"]
        if isinstance(parent.get("scope"), dict):
            scope = parent["scope"]
        labels.update(
            {
                key: item
                for key, item in parent.items()
                if key in _LABELS and isinstance(item, (str, int))
            }
        )
    if entry.get("tool") == "analysis_compare" and path.startswith("$.decomposition.rows["):
        decomposition = result.get("decomposition", {})
        labels["dimension"] = decomposition.get("dimension")
        if decomposition.get("dimension") == "product_id" and str(labels.get("name", "")).isdigit():
            labels["product_id"] = int(labels["name"])
        for side in ("baseline", "comparison"):
            if f"].{side}." in path:
                scope = result[side]["context"]["filters"]
    if numeric_role(entry, path) == "identifier":
        key = "defect_id" if path.endswith(".defect_id") else "product_id"
        identifier = source_at(result, path)
        if type(identifier) is int and identifier > 0:
            labels[key] = identifier
    return {"scope": {key: item for key, item in scope.items() if item}, "labels": labels}


def claim_reference(claim: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": claim["id"],
        "value": claim["value"],
        "path": claim["path"],
        "marker": "[value:" + claim["id"] + "]",
        "role": numeric_role(entry, claim["path"]),
        **reference_context(entry, claim["path"]),
    }


def typed_identifier_text(
    text: str, claims: list[dict[str, Any]], entries: dict[str, dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Bind typed product/event labels to identifiers in a cited original row."""
    identifiers: dict[str, set[int]] = {"product_id": set(), "defect_id": set()}
    for claim in claims:
        entry = entries.get(claim["execution_id"])
        if entry is None or entry.get("result", {}).get("ok") is False:
            continue
        if entry.get("execution_kind") not in {"bounded_analysis", "canonical_receipt_import"}:
            continue
        labels = reference_context(entry, claim["path"])["labels"]
        for kind in identifiers:
            identifier = labels.get(kind)
            if type(identifier) is int and identifier > 0:
                identifiers[kind].add(identifier)
    product_context = bool(
        re.search(r"\bproduct(?:_id|\s+ID|s)?\b|sản\s+phẩm", text, re.IGNORECASE)
    )
    issues: list[dict[str, Any]] = []

    def check(prefix: str, number: str, label: str) -> None:
        identifier = int(number)
        kind = (
            "defect_id"
            if re.search(r"defect|event|record|sự\s+kiện", prefix, re.IGNORECASE)
            else "product_id"
        )
        explicit = bool(
            re.search(r"product|defect|event|record|sản\s+phẩm|sự\s+kiện", prefix, re.IGNORECASE)
        )
        if identifier not in identifiers[kind] or not (explicit or product_context):
            issues.append(
                {
                    "kind": "unbound_identifier",
                    "label": label,
                    "value": identifier,
                    "identifier_type": kind,
                    "cited_identifiers": sorted(identifiers[kind]),
                }
            )

    def replace_list(match: re.Match[str]) -> str:
        for number in re.findall(r"[1-9]\d*", match[2]):
            check(match[1], number, match[1] + number)
        return " "

    # Plural labels require every displayed identity to have its own cited
    # source membership. A nearby measured count cannot authorize an ID.
    text = re.sub(
        r"(?<!\w)((?:product_ids|product\s+IDs)\s*[:=#]?\s*)"
        r"([1-9]\d*(?:\s*(?:,|\band\b|\bvà\b)\s*[1-9]\d*)+)(?!\w|[.,]\d)",
        replace_list,
        text,
        flags=re.IGNORECASE,
    )
    if product_context and any(
        entries.get(claim["execution_id"], {}).get("tool") == "scenario_compare"
        and re.fullmatch(
            r"\$\.comparison\.rankings\.products_by_known_cost\.(?:canonical|selected)\[\d+\]",
            claim["path"],
        )
        for claim in claims
    ):
        text = re.sub(
            r"(?<!\w)((?:(?:product(?:\s+(?:known[- ]cost|cost))?|displayed)\s+)?"
            r"(?:ranking|ordering|order)\s+(?:to|is|becomes?|changes?\s+to)\s*)"
            r"([1-9]\d*(?:\s*(?:,|\band\b|\bvà\b)\s*[1-9]\d*)+)(?!\w|[.,]\d)",
            replace_list,
            text,
            flags=re.IGNORECASE,
        )

    def replace(match: re.Match[str]) -> str:
        check(match[1], match[2], match[0])
        return " "

    text = re.sub(
        r"(?<!\w)((?:product_id|product\s+ID|mã\s+sản\s+phẩm|defect_id|(?:defect|event|record)\s+ID|mã\s+sự\s+kiện|ID)"
        r"\s*(?:(?:remains?|is|was|là|vẫn(?:\s+là)?)\s+)?[:=#]?\s*)([1-9]\d*)(?!\w|[.,]\d)",
        replace,
        text,
        flags=re.IGNORECASE,
    )
    return text, issues


def citation_rows(
    entry: dict[str, Any], evidence: list[dict[str, Any]], max_bytes: int = 2200
) -> list[dict[str, Any]]:
    """Offer copyable markers beside the relevant row and period, within a budget."""
    groups: dict[str, dict[str, Any]] = {}
    for claim in evidence:
        parent, _, metric = claim["path"].rpartition(".")
        role = numeric_role(entry, claim["path"])
        if metric not in _MEASURES and role == "measurement":
            continue
        if parent not in groups:
            groups[parent] = {
                "path": parent,
                **reference_context(entry, claim["path"]),
                "measures": {},
            }
        groups[parent]["measures"][metric] = {
            "value": claim["value"],
            "marker": "[value:" + claim["id"] + "]",
            "role": role,
        }

    def priority(path: str) -> tuple[int, str]:
        return (
            -1
            if path.endswith(".parameters") or ".priority_signals." in path
            else 0
            if path.endswith(".analysis.kpis")
            else 1
            if path == "$.decomposition"
            else 2
            if ".products.top[" in path
            else 3
            if re.search(r"\.rows\[\d+\]$", path)
            else 4,
            path,
        )

    output: list[dict[str, Any]] = []
    for path in sorted(groups, key=priority):
        group = groups[path]
        group["measures"] = {
            metric: group["measures"][metric]
            for metric in [
                *[name for name in _MEASURE_ORDER if name in group["measures"]],
                *[name for name in group["measures"] if name not in _MEASURE_ORDER],
            ][:4]
        }
        if len(canonical([*output, group]).encode("utf-8")) <= max_bytes:
            output.append(group)
        if len(output) >= 8:
            break
    return output


def field_feedback(
    field: str,
    raw_text: str,
    audit: dict[str, Any],
    claims: dict[str, dict[str, Any]],
    entries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Explain a rejection; candidates remain suggestions requiring semantic fit."""
    identities = list(dict.fromkeys(re.findall(r"\[(?:claim|value):([^:\]]+)", raw_text)))
    referenced = [
        claim_reference(claims[identity], entries[claims[identity]["execution_id"]])
        for identity in identities
        if identity in claims
    ]
    candidates = []
    criterion = bool(
        re.search(
            r"repair costs?\s+(?:equal|=)|chi phí(?:\s+sửa chữa)?\s+(?:bằng|=)|criterion|threshold|tiêu chí|ngưỡng",
            raw_text,
            re.IGNORECASE,
        )
    )
    for issue in audit.get("numeric_issues", []):
        if issue.get("kind") != "unbound_measurement":
            continue
        value, tolerance = issue["value"], issue["tolerance"]
        date_number = bool(re.search(r"(?:ngày|day)\s*$", issue.get("prefix", ""), re.IGNORECASE))
        ranked = []
        for claim in claims.values():
            metric = claim["path"].rpartition(".")[2]
            if (
                metric not in _MEASURES
                and not (
                    criterion
                    and numeric_role(entries[claim["execution_id"]], claim["path"])
                    == "policy_parameter"
                )
                or date_number
                or abs(claim["value"] - value) > tolerance + 1e-9
            ):
                continue
            score = (
                4
                if numeric_role(entries[claim["execution_id"]], claim["path"]) == "policy_parameter"
                else 3
                if ".products.top[0]." in claim["path"]
                else 2
                if metric == "event_count_change"
                else 1
            )
            ranked.append((score, claim))
        for _, claim in sorted(ranked, key=lambda pair: -pair[0])[:2]:
            reference = claim_reference(claim, entries[claim["execution_id"]])
            if reference not in candidates:
                candidates.append(reference)
    return {
        "field": field,
        "errors": audit["errors"][:6],
        "unbound_numbers": [
            item["token"]
            for item in audit.get("numeric_issues", [])
            if item.get("kind") == "unbound_measurement"
        ],
        "unbound_identifiers": [
            item
            for item in audit.get("numeric_issues", [])
            if item.get("kind") == "unbound_identifier"
        ],
        "calendar_labels": [
            item
            for item in audit.get("numeric_issues", [])
            if item.get("kind") == "unsupported_calendar_label"
        ],
        "referenced_evidence": referenced[:4],
        "candidate_evidence": candidates[:4],
        "repair": "Prefer the exact [value:ID] marker in place of a literal measurement. Copy the exact returned scenario identifier; an invented numeric alias is not a source label. A scenario threshold needs the typed policy-parameter marker in an explicit criterion phrase. Calendar ranges must match an offered frozen period. Signed changes already include their sign: use neutral change/thay đổi/chênh lệch wording instead of decrease/giảm before a negative marker. Each repeated measurement needs evidence on its own line. Candidates must match the intended metric, row and period; equal numeric values alone do not establish meaning. If none matches, retrieve the needed section or omit the unsupported claim.",
        "scenario_identifiers": sorted(
            {
                source["scenario"]
                for entry in entries.values()
                if entry.get("tool") in {"scenario_compare", "analysis_query"}
                and entry.get("result", {}).get("ok") is not False
                for source in source_contexts(entry)
                if isinstance(source.get("scenario"), str) and source["scenario"]
            }
        ),
    }
