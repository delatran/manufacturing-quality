"""Financial criteria remain shared, observable and backed by sealed evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from analyst.agent_views import focused_result
from analyst.case_study.analytics import Scope, analysis_report
from analyst.case_study.preparation import build_snapshot
from analyst.case_study.scenarios import SCENARIOS, scenario_snapshot
from analyst.dashboard import AnalysisService
from analyst.evidence import EvidenceLedger
from analyst.report_output import metric_unit
from analyst.runtime import result_at_path

ROOT = Path(__file__).resolve().parents[1]
MASTER = (
    b"product_id,product_name,category,production_line,monthly_output\n1,A,Category 1,Line-1,100\n"
)


def prepared_snapshot():
    rows = [
        {
            "defect_id": index,
            "product_id": 1,
            "defect_type": "Functional",
            "defect_date": "2024-02-01",
            "defect_location": "Component",
            "severity": "Critical",
            "inspection_method": "Automated Testing",
            "repair_cost": value,
        }
        for index, value in enumerate((10, 11, 20, None, -1), start=1)
    ]
    provenance = {
        "source_sha256": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
        "source_url": "https://synthetic.invalid/criteria",
        "fetched_at": "2026-10-17T00:00:00Z",
    }
    return build_snapshot(rows, provenance, MASTER), rows


def test_financial_scenario_exposes_a_numeric_criterion_and_its_meaning():
    parameters = SCENARIOS["exclude_cost_10"]["parameters"]
    assert type(parameters["repair_cost_equal"]) is int
    assert parameters["repair_cost_equal"] == 10
    assert parameters["repair_cost_unit"] == "local currency"
    assert parameters["effect"] == "withhold_financial_availability"


def test_same_parameter_controls_mapping_count_and_independent_cost_arithmetic(monkeypatch):
    snapshot, raw = prepared_snapshot()
    original = copy.deepcopy(snapshot)
    parameters = SCENARIOS["exclude_cost_10"]["parameters"]
    monkeypatch.setitem(parameters, "repair_cost_equal", 11)
    projected = scenario_snapshot(snapshot, "exclude_cost_10")
    expected_available = [
        Decimal(str(row["repair_cost"]))
        for row in raw
        if row["repair_cost"] is not None
        and row["repair_cost"] >= 0
        and row["repair_cost"] != parameters["repair_cost_equal"]
    ]
    report = analysis_report(snapshot, Scope(scenario="exclude_cost_10"))
    assert report["kpis"]["event_count"] == len(raw)
    assert Decimal(str(report["kpis"]["known_repair_cost"])) == sum(expected_available)
    assert report["kpis"]["cost_known_count"] == len(expected_available)
    assert projected.provenance["scenario_affected_clean_events"] == 1
    assert {
        event.defect_id
        for event in projected.events
        if event.cost_status == "scenario_excluded_cost_10"
    } == {2}
    assert next(event for event in projected.events if event.defect_id == 1).repair_cost == 10
    assert snapshot == original


@pytest.mark.asyncio
async def test_criterion_is_claimable_in_the_sealed_comparison_and_model_view(tmp_path):
    snapshot, _ = prepared_snapshot()
    service = AnalysisService(ROOT, tmp_path / "service", snapshot_loader=lambda: snapshot)
    context = await service.get_context()
    result = await service.compare_scenario(context["context"]["context_id"], "exclude_cost_10")
    tool_result = {"ok": True, **result}
    ledger = EvidenceLedger(tmp_path / "tool")
    entry = ledger.record(
        "scenario_compare",
        "service.compare_scenario(context_id, scenario)",
        tool_result,
        execution_kind="bounded_analysis",
    )
    expected_paths = {
        "$.comparison.scenario_definition.parameters.repair_cost_equal",
        "$.selected.analysis.metadata.scenario_definition.parameters.repair_cost_equal",
    }
    measurements = {item["path"]: item["value"] for item in entry["numeric_values"]}
    assert all(measurements[path] == 10 for path in expected_paths)
    view, _ = focused_result(entry)
    assert view["comparison"]["scenario_definition"]["parameters"]["repair_cost_equal"] == 10
    parameter_path = "$.comparison.scenario_definition.parameters.repair_cost_equal"
    unit = metric_unit(entry, parameter_path)
    assert unit is not None
    assert unit["kind"] == "policy_parameter" and unit["unit"] == "local currency"
    assert type(result_at_path(entry["result"], parameter_path)) is int
    selected_definition = result_at_path(
        entry["result"], "$.selected.analysis.metadata.scenario_definition"
    )
    assert selected_definition == view["comparison"]["scenario_definition"]
    assert selected_definition["parameters"]["repair_cost_equal"] == 10
    assert entry["result"]["selected"]["context"]["scenario"] == "exclude_cost_10"
    claim = {
        "execution_id": entry["execution_id"],
        "path": sorted(expected_paths)[0],
        "value": 10,
        "code_sha256": entry["code_sha256"],
    }
    assert ledger.verify_claims([claim])["verified"]
    assert not ledger.verify_claims([{**claim, "value": 11}])["verified"]


@pytest.mark.asyncio
async def test_financial_context_uses_the_same_criterion_as_the_internal_event_projection(
    tmp_path, monkeypatch
):
    snapshot, _ = prepared_snapshot()
    monkeypatch.setitem(SCENARIOS["exclude_cost_10"]["parameters"], "repair_cost_equal", 11)
    service = AnalysisService(ROOT, tmp_path, snapshot_loader=lambda: snapshot)
    payload = await service.get_context({"scenario": "exclude_cost_10"})
    projected = scenario_snapshot(snapshot, payload["context"]["scenario"])
    canonical = {event.defect_id: event for event in snapshot.events}
    interpreted = {event.defect_id: event for event in projected.events}
    assert canonical[2].repair_cost == 11
    assert interpreted[2].repair_cost is None
    assert interpreted[2].cost_status == "scenario_excluded_cost_10"
    assert canonical[1].repair_cost == interpreted[1].repair_cost == 10
    records = await service.records(payload["context"]["context_id"], sort_by="defect_id")
    assert {row["defect_id"]: row["repair_cost"] for row in records["rows"]} == {
        identity: event.repair_cost for identity, event in interpreted.items()
    }
