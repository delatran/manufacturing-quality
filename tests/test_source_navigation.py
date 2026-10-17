"""Independent source navigation counterexamples over explicitly synthetic sealed receipts.

No provider client is allowed. Actual task acceptance and historical live
failures are reviewed separately from these deterministic behavior controls.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from analyst.agent_views import source_projection
from analyst.case_study.analytics import DEFINITIONS, METRIC_METADATA, Scope
from analyst.case_study.preparation import build_snapshot
from analyst.config import Settings
from analyst.dashboard import AnalysisService
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    NumericClaim,
    ReportDraft,
    audit_report,
    result_at_path,
)
from analyst.tools import AnalystTools

WARNING = "No selected reporting month has logged events after day28; completeness is unknown."
OTHER_WARNING = "Only the comparison scope has a different reporting limitation."


def binding(identity: str, month: str = "2024-01", scenario: str = "") -> dict[str, Any]:
    return {
        "context_id": identity,
        "snapshot_id": "synthetic-independent-v8-snapshot",
        "dataset_version": "synthetic-independent-v8-dataset",
        "policy_version": "canonical-v2",
        "source_sha256": "synthetic-source",
        "master_sha256": "synthetic-master",
        "code_sha256": "synthetic-engine",
        "kpi_version": "synthetic-kpis",
        "scenario": scenario,
        "filters": asdict(Scope(start_month=month, end_month=month, scenario=scenario)),
    }


def payload(context: dict[str, Any], count: int, cost: float, coverage: float) -> dict[str, Any]:
    return {
        "context": context,
        "analysis": {
            "scope": context["filters"],
            "kpis": {
                "event_count": count,
                "known_repair_cost": cost,
                "cost_coverage_pct": coverage,
            },
            "definitions": deepcopy(DEFINITIONS),
            "metric_metadata": deepcopy(METRIC_METADATA),
            "metadata": {"currency": "local currency (unspecified)"},
        },
    }


def claim_for(entry: dict[str, Any], path: str, identity: str) -> NumericClaim:
    value = next(row["value"] for row in entry["numeric_values"] if row["path"] == path)
    return NumericClaim(
        id=identity,
        execution_id=entry["execution_id"],
        path=path,
        value=value,
        code_sha256=entry["code_sha256"],
    )


async def no_events(*_args, **_kwargs):
    pass


@pytest.fixture
def subject8(tmp_path, monkeypatch, request):
    def deny_provider(*_args, **_kwargs):
        raise AssertionError("Independent v8 review must not construct a provider client")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", deny_provider)
    ledger = EvidenceLedger(tmp_path)
    tools = SimpleNamespace(ledger=ledger, run_dir=tmp_path, input_paths=[], schemas=[], todos=[])
    base = binding("independent-v8-base")
    runtime = AnalystRuntime(
        Settings(mode="live", api_key="offline-invalid", memory_enabled=False),
        tools,
        no_events,
        analysis_context={"context": base, "analysis": {"definitions": deepcopy(DEFINITIONS)}},
        language=request.node.callspec.params.get("language", "en")
        if hasattr(request.node, "callspec")
        else "en",
    )
    return SimpleNamespace(ledger=ledger, runtime=runtime, base=base)


def issue(subject, tool, result, **metadata):
    return subject.ledger.record(
        tool,
        "# Explicitly synthetic independent v8 source, not a live model outcome",
        result,
        execution_kind="bounded_analysis",
        **metadata,
    )


@pytest.fixture
def numeric8(subject8):
    value = payload(subject8.base, 1071, 508864.39, 1049 / 1071 * 100)
    value["ok"] = True
    value["definitions"] = deepcopy(DEFINITIONS)
    value["metric_metadata"] = deepcopy(METRIC_METADATA)
    value["analysis"]["kpis"].update(
        cost_known_count=1049,
        cost_unknown_count=22,
        eligible_event_count=1067,
        reference_output=594360,
        reference_intensity_per_1000=1067 / 594360 * 1000,
    )
    entry = issue(subject8, "analysis_query", value, analysis_context=subject8.base)
    claims = {
        name: claim_for(entry, "$.analysis.kpis." + field, name)
        for name, field in {
            "count": "event_count",
            "known": "cost_known_count",
            "unknown": "cost_unknown_count",
            "cost": "known_repair_cost",
            "linked": "eligible_event_count",
            "output": "reference_output",
            "intensity": "reference_intensity_per_1000",
        }.items()
    }
    claims["scale"] = claim_for(
        entry, "$.metric_metadata.reference_intensity_per_1000.scale", "scale"
    )
    subject8.runtime.claim_catalog.update(claims)
    return SimpleNamespace(**vars(subject8), entry=entry, claims=claims)


def proposal(subject, text, language="en"):
    assert subject.runtime.language == language
    return subject.runtime.resolve_report(ReportDraft(report=text))


@pytest.mark.parametrize(
    "language,emphasized", [("en", True), ("en", False), ("vi", True), ("vi", False)]
)
def test_actual_r06_repeated_value_shape_is_rejected_without_rewriting(
    numeric8, language, emphasized
):
    phrases = (
        [
            ("1,071 total defect events", "count"),
            ("508,864.39 local currency", "cost"),
            ("1,049 of 1,071", "fraction"),
            ("22 unknown-cost events", "unknown"),
            ("1,067 linked events", "linked"),
            ("594,360 reference-output units", "output"),
        ]
        if language == "en"
        else [
            ("1.071 sự kiện lỗi", "count"),
            ("508.864,39 local currency", "cost"),
            ("1.049 trên 1.071", "fraction"),
            ("22 sự kiện chưa biết chi phí", "unknown"),
            ("1.067 sự kiện liên kết", "linked"),
            ("594.360 đơn vị sản lượng tham chiếu", "output"),
        ]
    )
    lines = []
    for phrase, name in phrases:
        shown = "**" + phrase + "**" if emphasized else phrase
        inserted = "[value:known] / [value:count]" if name == "fraction" else "[value:" + name + "]"
        lines.append(shown + " " + inserted + ".")
    draft = "\n".join(lines)
    resolved = proposal(numeric8, draft, language)
    original = resolved.model_dump()
    audit = audit_report(resolved, numeric8.ledger)
    assert not audit["verified"], audit
    assert resolved.model_dump() == original
    assert "[value:" not in resolved.report


@pytest.mark.parametrize(
    "language,emphasized", [("en", True), ("en", False), ("vi", True), ("vi", False)]
)
def test_one_quantity_echo_cannot_hide_behind_other_errors(numeric8, language, emphasized):
    phrase = "1,071 total defect events" if language == "en" else "1.071 sự kiện lỗi"
    if emphasized:
        phrase = "**" + phrase + "**"
    text = phrase + " [value:count]."
    resolved = proposal(numeric8, text, language)
    result = audit_report(resolved, numeric8.ledger)
    assert not result["verified"], result


@pytest.mark.parametrize("language", ["en", "vi"])
def test_literal_with_noninserting_claim_and_real_fraction_remain_valid(numeric8, language):
    text = (
        "**1,071 total defect events [claim:count]**. "
        "Known costs: **1,049 [claim:known] of 1,071 [claim:count]**; "
        "unknown costs: **22 [claim:unknown] events**."
        if language == "en"
        else "**1.071 sự kiện lỗi [claim:count]**. "
        "Chi phí đã biết: **1.049 [claim:known] trên 1.071 [claim:count]**; "
        "chưa biết: **22 [claim:unknown] sự kiện**."
    )
    resolved = proposal(numeric8, text, language)
    assert audit_report(resolved, numeric8.ledger)["verified"]
    assert resolved.report == text


@pytest.mark.parametrize(
    "text",
    [
        "Count **[value:first] events**; cost **[value:cost] local currency**.",
        "January **[value:first] events**; June **[value:second] events**.",
        "January [value:first] events and June [value:second] events.",
        "**[value:first] events**. Summary: [value:first] events.",
        "**[value:first] events** + [value:second] events.",
        "Known-cost events: **[value:known] of [value:first] events**.",
        "Known-cost events: [value:known] out of [value:first] events.",
        "Sự kiện có chi phí: **[value:known] trên [value:first] sự kiện**.",
        "Of [value:first] events, [value:known] have known costs.",
        "There are [value:first] events, including [value:known] known-cost events.",
        "Tháng một **[value:first] sự kiện**; tháng sáu **[value:second] sự kiện**.",
        "Số sự kiện: **[value:first]**; chi phí: **[value:cost] local currency**.",
    ],
)
def test_equal_values_for_distinct_metrics_populations_and_summary_are_not_echoes(subject8, text):
    first = {"ok": True, **payload(binding("jan"), 20, 20, 100)}
    first["analysis"]["kpis"]["cost_known_count"] = 20
    entries = [
        issue(subject8, "analysis_query", first),
        issue(
            subject8,
            "analysis_query",
            {"ok": True, **payload(binding("jun", "2024-06"), 20, 15, 100)},
        ),
    ]
    claims = {
        "first": claim_for(entries[0], "$.analysis.kpis.event_count", "first"),
        "second": claim_for(entries[1], "$.analysis.kpis.event_count", "second"),
        "cost": claim_for(entries[0], "$.analysis.kpis.known_repair_cost", "cost"),
        "known": claim_for(entries[0], "$.analysis.kpis.cost_known_count", "known"),
    }
    subject8.runtime.claim_catalog.update(claims)
    assert audit_report(proposal(subject8, text), subject8.ledger)["verified"]


@pytest.mark.parametrize(
    "text,valid",
    [
        ("### Priority 1: Validate actual exposure", True),
        ("## Ưu tiên 3: Kiểm tra nguồn chi phí", True),
        ("### Priority 2: Observe [value:count] logged events", True),
        ("### Priority 2: Observe 999 defect events", False),
        ("### Ưu tiên 3: Ghi nhận 999 sự kiện", False),
        ("### Priority 3 events: Review", False),
        ("The selected priority reports 3 events.", False),
    ],
)
def test_structural_priority_ordinal_does_not_hide_a_measurement(numeric8, text, valid):
    result = audit_report(proposal(numeric8, text), numeric8.ledger)
    assert result["verified"] is valid, result


@pytest.mark.parametrize(
    "text,language,valid",
    [
        (
            "Reference intensity: **[value:intensity] events per 1,000 [claim:scale] reference-output units**.",
            "en",
            True,
        ),
        (
            "Reference intensity: [value:intensity] events per 1,000 [claim:scale] reference-output units.",
            "en",
            True,
        ),
        (
            "Cường độ tham chiếu: **[value:intensity] sự kiện trên 1.000 [claim:scale] đơn vị tham chiếu**.",
            "vi",
            True,
        ),
        (
            "Cường độ tham chiếu: [value:intensity] sự kiện trên 1.000 đơn vị tham chiếu.",
            "vi",
            True,
        ),
        (
            "Reference count: [value:count] events per 1,000 [claim:scale] reference-output units.",
            "en",
            False,
        ),
        (
            "Reference cost: [value:cost] local currency per 1,000 [claim:scale] reference-output units.",
            "en",
            False,
        ),
        (
            "Reference intensity: [value:intensity] events per 1,000 [claim:missing] reference-output units.",
            "en",
            False,
        ),
        (
            "Reference intensity: [value:intensity] events per 1,000 [claim:count] reference-output units.",
            "en",
            False,
        ),
        ("Observed [value:scale] logged events.", "en", False),
    ],
)
def test_inline_scale_needs_the_actual_intensity_and_trusted_scale_binding(
    numeric8, text, language, valid
):
    result = audit_report(proposal(numeric8, text, language), numeric8.ledger)
    assert result["verified"] is valid, result


def test_same_metric_scale_from_another_scope_cannot_qualify_this_intensity(numeric8):
    other = deepcopy(numeric8.entry["result"])
    other["context"]["context_id"] = "independent-v8-other-scope"
    other["context"]["filters"]["severity"] = "Critical"
    other["analysis"]["scope"]["severity"] = "Critical"
    entry = issue(numeric8, "analysis_query", other, analysis_context=other["context"])
    scale = claim_for(entry, "$.metric_metadata.reference_intensity_per_1000.scale", "other_scale")
    numeric8.runtime.claim_catalog[scale.id] = scale
    result = audit_report(
        proposal(
            numeric8,
            "Reference intensity: [value:intensity] events per 1,000 [claim:other_scale] reference-output units.",
        ),
        numeric8.ledger,
    )
    assert not result["verified"], result


def compare_result():
    first = payload(binding("january"), 20, 20, 100)
    last = payload(binding("june", "2024-06"), 20, 15, 100)
    first["analysis"]["metadata"]["limitations"] = [WARNING]
    first["analysis"]["anomalies"] = {"limitations": [WARNING]}
    last["analysis"]["metadata"]["limitations"] = [OTHER_WARNING]
    return {
        "ok": True,
        "baseline": first,
        "comparison": last,
        "decomposition": {
            "dimension": "product_id",
            "event_count_change": 0,
            "definitions": {"contribution": "An observed contribution, not a cause."},
            "rows": [],
        },
    }


def path_value(result, path):
    value = result
    for part in re.finditer(r"\.([A-Za-z_][A-Za-z_0-9]*)|\[(\d+)\]", path):
        value = value[part[1]] if part[1] is not None else value[int(part[2])]
    return value


def test_declared_warning_reaches_both_readers_with_only_its_own_scope(subject8):
    entry = issue(subject8, "analysis_compare", compare_result())
    before = subject8.ledger.path.read_bytes()
    model = subject8.runtime.model_receipt(entry)
    qa, errors = subject8.runtime.qa_evidence(
        AnalystReport(report="Reporting completeness is not established.", claims=[]),
        delivery={"source_tables": [], "artifacts": []},
    )
    assert not errors
    projected = model["source_projection"]
    assert projected == qa["qualitative_support"][0]["source_projection"]
    warnings = [row for row in projected["declared_metadata"] if row.get("value") == [WARNING]]
    assert len(warnings) == 1
    assert {source["path"] for source in warnings[0]["sources"]} == {
        "$.baseline.analysis.metadata.limitations",
        "$.baseline.analysis.anomalies.limitations",
    }
    assert all(source["context_ids"] == ["january"] for source in warnings[0]["sources"])
    for row in projected["declared_metadata"]:
        for source in row["sources"]:
            original = path_value(entry["result"], source["path"])
            if "value" in row:
                assert original == row["value"]
            else:
                assert row["value_reference"] == "canonical_definitions"
                assert original == qa["canonical_definitions"]
                assert sha256(canonical(original)) == row["value_sha256"]
    assert projected["source"]["entry_sha256"] == entry["entry_sha256"]
    assert subject8.ledger.path.read_bytes() == before


@pytest.mark.parametrize(
    "declarations", [None, {"event_count": "A different untrusted definition."}]
)
def test_missing_or_mismatched_definition_map_keeps_original_source_text(subject8, declarations):
    entry = issue(subject8, "analysis_compare", compare_result())
    projected = source_projection(entry, canonical_definitions=declarations)
    rows = [row for row in projected["declared_metadata"] if row["field"] == "definitions"]
    assert rows
    canonical_rows = [
        row
        for row in rows
        if any(source["path"].endswith(".analysis.definitions") for source in row["sources"])
    ]
    assert canonical_rows and all(row["value"] == DEFINITIONS for row in canonical_rows)
    for row in rows:
        assert all(
            path_value(entry["result"], source["path"]) == row["value"] for source in row["sources"]
        )
    assert all("value_reference" not in row for row in rows)


@pytest.mark.parametrize("damage", ["source_text", "context", "code"])
def test_source_or_scope_tampering_cannot_create_declared_support(subject8, damage):
    entry = issue(subject8, "analysis_compare", compare_result())
    changed = deepcopy(entry)
    if damage == "source_text":
        changed["result"]["baseline"]["analysis"]["metadata"]["limitations"] = [OTHER_WARNING]
    elif damage == "context":
        changed["result"]["baseline"]["context"]["filters"]["severity"] = "Critical"
    else:
        changed["code"] = "forged code"
    with pytest.raises(ValueError):
        source_projection(changed)


@pytest.mark.parametrize("kind", ["failed", "owner_text", "detail_preview"])
def test_failed_authored_or_partial_navigation_text_cannot_be_declared_analysis(subject8, kind):
    tool = {
        "failed": "analysis_query",
        "owner_text": "investigation_update",
        "detail_preview": "evidence_detail",
    }[kind]
    result = {"ok": kind != "failed", "context": subject8.base, "limitations": [WARNING]}
    entry = issue(subject8, tool, result)
    assert source_projection(entry) == {}


def scenario_result(before, after):
    return {
        "ok": True,
        "canonical": payload(binding("canonical"), 20, 20, 100),
        "selected": payload(binding("financial", scenario="exclude_cost_10"), 20, 10, 95),
        "comparison": {
            "rankings": {
                "products_by_known_cost": {
                    "canonical": before,
                    "selected": after,
                    "changed": before != after,
                }
            },
            "limitations": ["The assumption changes financial availability, not event identity."],
        },
    }


@pytest.mark.parametrize(
    "change", ["two_separate_swaps", "rotation", "add_and_remove", "unchanged"]
)
def test_complete_changed_positions_retain_every_original_identity_and_path(subject8, change):
    before = list(range(101, 121))
    after = before.copy()
    if change == "two_separate_swaps":
        after[7], after[8] = after[8], after[7]
        after[18], after[19] = after[19], after[18]
    elif change == "rotation":
        after[5:9] = [before[8], *before[5:8]]
    elif change == "add_and_remove":
        after = [*before[:10], 999, *before[11:], 1001]
    result = scenario_result(before, after)
    entry = issue(subject8, "scenario_compare", result)
    original = canonical(entry)
    view = subject8.runtime.model_receipt(entry)
    groups = view["source_projection"]["ranking_changes"]
    assert len(groups) == 1
    group = groups[0]
    assert group["complete_source_arrays"] is True
    assert group["canonical_size"] == len(before)
    assert group["selected_size"] == len(after)
    assert group["context_ids"] == ["canonical", "financial"]
    expected = []
    for offset in range(max(len(before), len(after))):
        left = before[offset] if offset < len(before) else None
        right = after[offset] if offset < len(after) else None
        if left != right:
            expected.append([offset + 1, left, right])
    assert group["columns"] == ["position", "canonical", "selected"]
    assert group["changed_positions"] == expected
    for position, left, right in group["changed_positions"]:
        for side, value in (("canonical", left), ("selected", right)):
            if value is not None:
                path = group["path"] + "." + side + f"[{position - 1}]"
                assert path_value(result, path) == value
    assert all(
        path_value(result, item["path"]) == item["value"]
        and "source_projection" not in item["path"]
        and "changed_positions" not in item["path"]
        for item in view["numeric_evidence"]
    )
    assert len(json.dumps(view, ensure_ascii=False).encode("utf-8")) <= 20000
    assert canonical(entry) == original


def test_a_selected_preview_cannot_hide_the_other_change_in_the_original_source(subject8):
    before = list(range(501, 521))
    after = before.copy()
    after[1], after[2] = after[2], after[1]
    after[17], after[18] = after[18], after[17]
    entry = issue(subject8, "scenario_compare", scenario_result(before, after))
    primary = subject8.runtime.model_receipt(entry)
    preview = subject8.runtime.model_receipt(
        entry, selected=[("$.comparison.rankings.products_by_known_cost.selected[1]", after[1])]
    )
    assert "ranking_changes" not in preview["source_projection"]
    assert preview["source_projection"]["source"] == primary["source_projection"]["source"]
    support, errors = subject8.runtime.qa_evidence(
        AnalystReport(report="Only a labelled subset was requested for navigation.", claims=[]),
        delivery={"source_tables": [], "artifacts": []},
    )
    assert not errors
    qa = support["qualitative_support"][0]["source_projection"]
    assert qa == primary["source_projection"]
    projected = qa["ranking_changes"][0]
    assert [row[0] for row in projected["changed_positions"]] == [2, 3, 18, 19]
    assert projected["canonical_size"] == projected["selected_size"] == 20
    assert projected["complete_source_arrays"] is True
    detail = issue(
        subject8,
        "evidence_detail",
        {"ok": True, "selected": [{"path": projected["path"], "value": after[:3]}]},
    )
    assert source_projection(detail) == {}


@pytest.mark.parametrize("damage", ["missing_side", "duplicate_identity", "wrong_changed_flag"])
def test_malformed_rank_source_cannot_claim_complete_exclusivity(subject8, damage):
    result = scenario_result([11, 22, 33], [11, 33, 22])
    rank = result["comparison"]["rankings"]["products_by_known_cost"]
    if damage == "missing_side":
        del rank["selected"]
    elif damage == "duplicate_identity":
        rank["selected"] = [11, 11, 22]
    else:
        rank["changed"] = False
    entry = issue(subject8, "scenario_compare", result)
    with pytest.raises(ValueError):
        source_projection(entry)


def diagnostic_result(subject, counts):
    return {
        "ok": True,
        "section": "quality",
        "context": subject.base,
        "definitions": deepcopy(DEFINITIONS),
        "analysis": {"quality": {"master_profile": {"products_by_category": counts}}},
    }


@pytest.mark.parametrize("label", ["Category A", "Category-A"])
def test_opaque_diagnostic_key_stays_readable_without_an_invalid_claim_id(subject8, label):
    counts = {label: 7, "safe_key": 7}
    entry = issue(subject8, "analysis_query", diagnostic_result(subject8, counts))
    original = subject8.ledger.path.read_bytes()
    view = subject8.runtime.model_receipt(entry)
    assert view["result"]["analysis"]["quality"]["master_profile"]["products_by_category"] == counts
    path = "$.analysis.quality.master_profile.products_by_category."
    numbers = view["numeric_evidence"]
    assert not any(number["path"] == path + label for number in numbers)
    assert any(number["path"] == path + "safe_key" and number["value"] == 7 for number in numbers)
    assert all(
        result_at_path(entry["result"], number["path"]) == number["value"] for number in numbers
    )
    note = view["numeric_evidence_note"].lower()
    assert "no direct" in note and "scoped" in note and "query" in note
    assert subject8.ledger.path.read_bytes() == original


def test_dotted_opaque_key_cannot_impersonate_same_value_at_a_safe_nested_path(subject8):
    counts = {"A.B": 10, "A": {"B": 10}}
    entry = issue(subject8, "analysis_query", diagnostic_result(subject8, counts))
    original = subject8.ledger.path.read_bytes()
    view = subject8.runtime.model_receipt(entry)
    assert view["result"]["analysis"]["quality"]["master_profile"]["products_by_category"] == counts
    path = "$.analysis.quality.master_profile.products_by_category.A.B"
    numbers = [number for number in view["numeric_evidence"] if number["path"] == path]
    # A conservative absence is valid when the legacy flattened path aliases
    # two ancestries. A published ID may refer only to the safe nested leaf.
    assert len(numbers) <= 1
    if numbers:
        assert result_at_path(entry["result"], path) == numbers[0]["value"] == 10
    assert len({number["id"] for number in view["numeric_evidence"]}) == len(
        view["numeric_evidence"]
    )
    assert "no direct" in view["numeric_evidence_note"].lower()
    claims = [
        NumericClaim(
            id=number["id"],
            execution_id=entry["execution_id"],
            path=number["path"],
            value=number["value"],
            code_sha256=entry["code_sha256"],
        ).model_dump()
        for number in numbers
    ]
    if claims:
        assert subject8.ledger.verify_claims(claims)["verified"]
    assert subject8.ledger.path.read_bytes() == original


@pytest.mark.parametrize("changed", [7.0, 8])
def test_changed_native_numeric_type_or_value_cannot_publish_any_new_id(subject8, changed):
    entry = issue(
        subject8, "analysis_query", diagnostic_result(subject8, {"safe_key": 7, "other": 9})
    )
    base = "$.analysis.quality.master_profile.products_by_category."
    before = subject8.runtime.available_claims()
    with pytest.raises(ValueError):
        subject8.runtime.model_receipt(
            entry, selected=[(base + "other", 9), (base + "safe_key", changed)]
        )
    assert subject8.runtime.available_claims() == before


def test_absent_original_numeric_path_is_a_projection_error_not_a_diagnostic_key(subject8):
    entry = issue(subject8, "analysis_query", diagnostic_result(subject8, {"safe_key": 7}))
    path = "$.analysis.quality.master_profile.products_by_category."
    before = subject8.runtime.available_claims()
    original = subject8.ledger.path.read_bytes()
    with pytest.raises(ValueError):
        subject8.runtime.model_receipt(
            entry, selected=[(path + "safe_key", 7), (path + "not_in_the_source", 7)]
        )
    assert subject8.runtime.available_claims() == before
    assert subject8.ledger.path.read_bytes() == original


@pytest.mark.asyncio
async def test_scoped_cohort_query_is_a_real_citable_route_for_an_opaque_category(
    subject8, tmp_path
):
    rows = [
        {
            "defect_id": 1,
            "product_id": 1,
            "defect_date": "2024-01-15",
            "defect_type": "Functional",
            "severity": "Critical",
            "defect_location": "Component",
            "inspection_method": "Visual Inspection",
            "repair_cost": 10,
        },
        {
            "defect_id": 2,
            "product_id": 2,
            "defect_date": "2024-01-16",
            "defect_type": "Functional",
            "severity": "Minor",
            "defect_location": "Surface",
            "inspection_method": "Visual Inspection",
            "repair_cost": 20,
        },
    ]
    master = (
        b"product_id,product_name,category,production_line,monthly_output\n"
        b"1,First product,Category A,Line-1,100\n"
        b"2,Second product,Category_A,Line-1,100\n"
    )

    def loader():
        return build_snapshot(
            rows,
            {
                "source_url": "https://synthetic.invalid/independent-v8-local-only",
                "source_sha256": sha256(canonical(rows)),
                "fetched_at": "2026-10-16T00:00:00Z",
            },
            master,
        )

    service = AnalysisService(
        Path(__file__).resolve().parents[1], tmp_path / "analysis", snapshot_loader=loader
    )
    bound = await service.get_context()
    tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)
    runtime = AnalystRuntime(
        Settings(mode="live", api_key="offline-invalid-key", memory_enabled=False),
        tools,
        no_events,
        analysis_context=bound,
        analysis_service=service,
    )
    selected: dict[str, Any] = {name: None for name in asdict(Scope())}
    try:
        quality = json.loads(
            await runtime._call(
                "analysis_query", {"section": "quality", "filters": selected, "plan": None}
            )
        )
        assert quality["status"] == "completed", quality
        count_map = quality["result"]["analysis"]["quality"]["master_profile"][
            "products_by_category"
        ]
        assert count_map == {"Category A": 1, "Category_A": 1}
        assert not any(
            number["path"].endswith(".Category A") for number in quality["numeric_evidence"]
        )
        selected["category"] = "Category A"
        view = json.loads(
            await runtime._call(
                "analysis_query", {"section": "kpis", "filters": selected, "plan": None}
            )
        )
        assert view["status"] == "completed", view
        cohort = next(
            number
            for number in view["numeric_evidence"]
            if number["path"] == "$.analysis.kpis.cohort_product_count"
        )
        assert cohort["value"] == 1
        assert view["scope"]["category"] == "Category A"
        assert view["result"]["context"]["context_id"] != bound["context"]["context_id"]
        report = runtime.resolve_report(
            ReportDraft(
                report="The selected master cohort contains [value:" + cohort["id"] + "] products."
            )
        )
        assert audit_report(report, tools.ledger)["verified"]
        assert runtime.analysis_context == bound
    finally:
        await tools.close()
