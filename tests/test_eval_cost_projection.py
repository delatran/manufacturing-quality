"""Check declared financial projections without private receipts or external inputs."""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cost_projection_evaluator_under_test", ROOT / "scripts/run_rebuild_eval.py"
)
assert SPEC is not None and SPEC.loader is not None
EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVAL)

# Three selected events: two known costs of 5 and 7, one unknown cost, one Critical.
# Two master products, including one without events, contribute (30 + 70) * 2 units.
FULL_KPIS = {
    "event_count": 3,
    "linked_event_count": 3,
    "critical_count": 1,
    "cost_known_count": 2,
    "cost_unknown_count": 1,
    "known_repair_cost": 12.0,
    "cost_coverage_pct": 200 / 3,
    "cohort_product_count": 2,
    "month_count": 2,
    "reference_output": 200,
    "reference_intensity_per_1000": 15.0,
}
FINANCIAL = {
    key: FULL_KPIS[key]
    for key in (
        "event_count",
        "known_repair_cost",
        "cost_known_count",
        "cost_unknown_count",
        "cost_coverage_pct",
    )
} | {"cost_available": True, "cost_status": "partial"}
WRAPPERS = ("kpis", "baseline", "comparison", "canonical", "selected", "payload", "result")


@pytest.fixture
def subject(monkeypatch):
    def deny_external_input(*_args, **_kwargs):
        raise AssertionError("This regression uses only synthetic in-memory inputs")

    monkeypatch.setattr(EVAL, "urlopen", deny_external_input)
    monkeypatch.setattr(EVAL, "read_json", deny_external_input)
    products = {
        product_id: {
            "product_id": product_id,
            "production_line": "Example line",
            "category": "Example category",
            "monthly_output": output,
        }
        for product_id, output in ((1, 30), (2, 70))
    }
    events = [
        {
            "defect_id": event_id,
            "product_id": 1,
            "defect_date": day,
            "severity": severity,
            "defect_type": "Functional",
            "defect_location": "Component",
            "inspection_method": "Manual",
            "repair_cost": Decimal(cost) if cost is not None else None,
        }
        for event_id, day, severity, cost in (
            (1, "2025-02-01", "Critical", "5"),
            (2, "2025-03-01", "Minor", "7"),
            (3, "2025-03-02", "Minor", None),
            (4, "2025-01-01", "Critical", "99"),  # Outside the declared period.
        )
    ]
    scope = {
        "start_month": "2025-02",
        "end_month": "2025-03",
        "production_line": "Example line",
        "category": "",
        "product_id": "",
        "inspection_method": "",
        "severity": "",
        "defect_type": "",
        "defect_location": "",
        "scenario": "",
    }
    context = {
        "context_id": "synthetic-context",
        "snapshot_id": "synthetic-snapshot",
        "dataset_version": "synthetic-dataset",
        "policy_version": "synthetic-policy",
        "scenario": "",
        "filters": scope,
    }
    entry = {
        "execution_id": "synthetic-query",
        "tool": "analysis_query",
        "execution_kind": "bounded_analysis",
        "analysis_context": deepcopy(context),
        "result": {
            "ok": True,
            "context": context,
            "scope_semantics": {
                "kind": "selected_scope",
                "filters_applied": True,
                "scenario_applied": False,
                "requested_context_id": context["context_id"],
                "requested_filters": deepcopy(scope),
                "scope_total_path": "$.analysis.kpis",
                "grouped_detail_path": "$.analysis.costs",
                "grain_note": (
                    "kpis describe all logged defect events in the returned context; "
                    "costs rows describe their named groups within that context."
                ),
            },
            "analysis": {
                "kpis": deepcopy(FINANCIAL),
                "costs": {
                    "by_type": [{"name": "Functional", **FINANCIAL}],
                    "by_location": [{"name": "Component", **FINANCIAL}],
                    "by_severity": [
                        {"name": "Critical", "event_count": 1, "known_repair_cost": 5.0},
                        {"name": "Minor", "event_count": 2, "known_repair_cost": 7.0},
                    ],
                },
            },
        },
    }
    return products, events, entry


def checked(subject):
    """Every success and rejection must leave the nested source and raw inputs intact."""
    before = deepcopy(subject)
    products, events, entry = subject
    checks = EVAL.receipt_kpi_checks(entry, products, events)
    assert subject == before
    return checks


def add_full_kpis(entry, wrapper):
    if wrapper == "kpis":
        entry["result"][wrapper] = deepcopy(FULL_KPIS)
        return entry["result"][wrapper]
    entry["result"][wrapper] = {
        "context": deepcopy(entry["result"]["context"]),
        "kpis": deepcopy(FULL_KPIS),
    }
    return entry["result"][wrapper]["kpis"]


def ordinary_checks(checks):
    return [
        row
        for row in checks
        if row["name"] != "oracle.cost_projection_binding" and "projection" not in row
    ]


def test_declared_seven_field_cost_projection_passes(subject):
    checks = checked(subject)
    assert len(checks) == 8
    assert {row["name"] for row in checks} == {
        "oracle.cost_projection_binding",
        *("oracle." + field for field in FINANCIAL),
    }
    assert all(row["status"] == "Passed" for row in checks)


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("event_count", 4),
        ("known_repair_cost", 13.0),
        ("cost_known_count", 1),
        ("cost_unknown_count", 2),
        ("cost_coverage_pct", 50.0),
        ("cost_available", False),
        ("cost_status", "complete"),
    ],
)
def test_wrong_financial_value_fails(subject, field, wrong):
    subject[2]["result"]["analysis"]["kpis"][field] = wrong
    checks = checked(subject)
    assert any(row["name"] == "oracle." + field and row["status"] == "Failed" for row in checks)


def test_valid_full_kpi_sibling_keeps_all_eleven_checks(subject):
    add_full_kpis(subject[2], "payload")
    checks = checked(subject)
    ordinary = ordinary_checks(checks)
    assert len(checks) == 19 and len(ordinary) == 11
    assert {row["name"] for row in ordinary} == {"oracle." + field for field in FULL_KPIS}
    assert all(row["status"] == "Passed" for row in checks)


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_wrong_full_count_at_every_ordinary_wrapper_fails(subject, wrapper):
    add_full_kpis(subject[2], wrapper)["event_count"] = -1
    checks = checked(subject)
    failures = [row for row in ordinary_checks(checks) if row["status"] == "Failed"]
    assert len(failures) == 1
    assert failures[0]["name"] == "oracle.event_count"
    assert failures[0]["observed"] == -1 and failures[0]["expected"] == 3


@pytest.mark.parametrize("declaration", ["malformed", "absent"])
def test_invalid_or_absent_binding_preserves_full_original_traversal(subject, declaration):
    products, events, entry = subject
    add_full_kpis(entry, "baseline")["event_count"] = -1
    if declaration == "malformed":
        entry["result"]["scope_semantics"]["grouped_detail_path"] = "$.wrong.costs"
    else:
        entry["result"].pop("scope_semantics")
    expected = EVAL.kpi_checks(entry["result"], products, events, entry["execution_id"])
    checks = checked(subject)
    if declaration == "malformed":
        assert checks[0]["name"] == "oracle.cost_projection_binding"
        assert checks[0]["status"] == "Failed"
        assert checks[1:] == expected
    else:
        assert checks == expected
    assert any(row["name"] == "oracle.event_count" and row["status"] == "Failed" for row in checks)


@pytest.mark.parametrize("fault", ["missing", "null"])
def test_nonfinancial_scalar_in_full_sibling_cannot_be_skipped(subject, fault):
    values = add_full_kpis(subject[2], "payload")
    if fault == "missing":
        values.pop("critical_count")
    else:
        values["critical_count"] = None
    checks = checked(subject)
    assert any(
        row["name"] == "oracle.critical_count" and row["status"] == "Failed"
        for row in ordinary_checks(checks)
    )
