"""Scenario QA keeps actual group methods and limitations across receipt shapes."""

from copy import deepcopy

import pytest
from analyst.case_study.sensitivity import LIMITATIONS, RULES
from analyst.runtime import AnalystReport, scenario_evidence
from test_report_tables import runtime_at


def actual_group_metadata():
    return {
        "analysis_type": "date-assumption sensitivity; not confidence bounds, detector validation, or model comparison",
        "rules": deepcopy(RULES),
        "limitations": list(LIMITATIONS),
    }


def test_standalone_qa_keeps_sensitivity_metadata_outside_analysis(tmp_path):
    runtime = runtime_at(tmp_path)
    group = actual_group_metadata()
    result = {
        "qa": {"status": "verified"},
        "analysis": {"metadata": {"limitations": ["Analysis-only limitation"]}},
        "sensitivity": {
            **group,
            "scenarios": {
                "primary_md": {
                    "kpis": {"event_count": 12},
                    "priority_signals": {"top_line": "Line-4"},
                    "rankings": {"products": [81, 2]},
                },
                "alternative": {
                    "kpis": {"event_count": 12},
                    "priority_signals": {"top_line": "Line-4"},
                    "rankings": {"products": [2, 81]},
                },
            },
        },
    }
    before = deepcopy(result)
    runtime.toolkit.ledger.record(
        "run_case_study", "# synthetic scenario outcomes with actual method definitions", result
    )
    evidence, errors = runtime.qa_evidence(
        AnalystReport(report="Compare the supplied date assumptions.", claims=[])
    )
    assert not errors
    support = evidence["qualitative_support"][0]
    assert support["limitations"] == ["Analysis-only limitation"]
    scenarios = support["scenario_evidence"]
    assert len(scenarios) == 2
    assert all(
        item["group_metadata"] == group and item["group_path"] == "$.sensitivity"
        for item in scenarios
    )
    assert scenarios[0]["rankings"]["products"] != scenarios[1]["rankings"]["products"]
    assert scenarios[0]["priority_signals"] == scenarios[1]["priority_signals"]
    assert result == before


def test_dashboard_scenario_group_preserves_own_scope_and_interpretation(tmp_path):
    runtime = runtime_at(tmp_path)
    interpretation = "Deterministic date-assumption scenarios under the same selected filters. These are not corrected truth, uncertainty intervals, or measured improvement."
    result = {
        "ok": True,
        "limitations": list(LIMITATIONS),
        "interpretation": interpretation,
        "scenarios": [
            {
                "name": "primary_md",
                "scope": {"production_line": "Line-4"},
                "kpis": {"event_count": 12},
            }
        ],
    }
    runtime.toolkit.ledger.record("dashboard_date_sensitivity", "# actual receipt shape", result)
    evidence, errors = runtime.qa_evidence(
        AnalystReport(report="Review date assumptions.", claims=[])
    )
    assert not errors
    scenario = evidence["qualitative_support"][0]["scenario_evidence"][0]
    assert scenario["scope"] == {"production_line": "Line-4"}
    assert scenario["group_metadata"] == {
        "limitations": LIMITATIONS,
        "interpretation": interpretation,
    }
    assert scenario["path"] == "$.scenarios[0]" and scenario["group_path"] == "$"


def test_nested_groups_keep_their_actual_nearest_metadata_without_inventing_fields():
    result = {
        "analysis_type": "unrelated outer method",
        "analysis": {
            "method": "group method",
            "scenarios": {"a": {"name": "A", "kpis": {"count": 1}}},
        },
        "sensitivity": {"limitations": ["different group"], "scenarios": [{"name": "B"}]},
    }
    scenarios = scenario_evidence(result)
    assert scenarios[0]["group_metadata"] == {"method": "group method"}
    assert scenarios[1]["group_metadata"] == {"limitations": ["different group"]}
    assert all("unrelated outer method" not in str(item["group_metadata"]) for item in scenarios)


def test_qa_keeps_actual_screen_decisions_and_nearest_candidate_comparison(tmp_path):
    runtime = runtime_at(tmp_path)
    comparison = {
        "common_to_all": ["2024-03-12"],
        "by_scenario": {
            "primary": {"candidate_dates": ["2024-03-12", "2024-04-10"]},
            "alternative": {"candidate_dates": ["2024-03-12", "2024-05-11"]},
        },
    }
    screen = {
        "method": "Fixed historical baseline",
        "enabled": True,
        "baseline_start": "2024-01-01",
        "baseline_end": "2024-01-31",
        "baseline_days": 31,
        "baseline_count": 62,
        "baseline_daily_mean": 2,
        "upper_reference_limit": 6.25,
        "baseline_variance_to_mean": 1.1,
        "candidates": [{"date": "2024-03-12", "count": 7, "upper_reference_limit": 6.25}],
        "daily": [{"date": "2024-01-01", "count": 2}],
        "daily_counts": [2] * 31,
        "daily_start_date": "2024-01-01",
        "daily_end_date": "2024-01-31",
    }
    group = {
        **actual_group_metadata(),
        "candidate_date_comparison": comparison,
        "scenarios": {"primary": {"kpis": {"event_count": 62}, "historical_screen": screen}},
    }
    result = {
        "analysis": {"metadata": {"limitations": ["Other analysis context"]}},
        "candidate_date_comparison": {"unrelated": ["2024-06-30"]},
        "sensitivity": group,
    }
    before = deepcopy(result)
    runtime.toolkit.ledger.record("run_case_study", "# actual comparative fields", result)
    evidence, errors = runtime.qa_evidence(
        AnalystReport(report="Compare supplied candidate decisions.", claims=[])
    )
    assert not errors
    item = evidence["qualitative_support"][0]["scenario_evidence"][0]
    assert item["historical_screen"] == {
        key: value for key, value in screen.items() if key not in {"daily", "daily_counts"}
    }
    assert item["group_metadata"]["candidate_date_comparison"] == comparison
    assert item["group_path"] == "$.sensitivity"
    assert not {"daily", "daily_counts"} & item["historical_screen"].keys()
    assert result == before


@pytest.mark.parametrize(
    "screen",
    [
        None,
        {},
        {
            "enabled": False,
            "baseline_count": 0,
            "upper_reference_limit": None,
            "candidates": [],
            "daily": [],
        },
        {"ok": False, "state": "failed", "error": "Screen could not be calculated"},
    ],
)
def test_screen_unavailability_and_failed_states_are_preserved_exactly(screen):
    result = {"scenarios": {"a": {"historical_screen": screen}}}
    item = scenario_evidence(result)[0]
    assert item["historical_screen"] == (
        {key: value for key, value in screen.items() if key != "daily"}
        if isinstance(screen, dict)
        else screen
    )
    missing = scenario_evidence({"scenarios": {"a": {"name": "No screen was supplied"}}})[0]
    assert "historical_screen" not in missing


@pytest.mark.parametrize("failed", ["result", "group", "scenario"])
def test_failed_scenario_evidence_is_not_promoted_to_qa(failed):
    result = {"sensitivity": {"method": "method", "scenarios": {"a": {"kpis": {"count": 999}}}}}
    target = (
        result
        if failed == "result"
        else result["sensitivity"]
        if failed == "group"
        else result["sensitivity"]["scenarios"]["a"]
    )
    target["ok"] = False
    assert scenario_evidence(result) == []
