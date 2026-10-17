"""Dashboard analytics exposed as frozen, receipted Agent reads without a provider."""

import json
from dataclasses import fields

import pytest
import pytest_asyncio
from analyst.agent_views import VIEW_BYTES
from analyst.case_study.analytics import Scope
from analyst.case_study.preparation import build_snapshot
from analyst.config import Settings
from analyst.evidence import canonical, numeric_values, sha256
from analyst.runtime import AnalystRuntime, result_at_path
from analyst.tools import AnalystTools
from test_dashboard import Loader, service
from test_mix_and_brief import MASTER as MIX_MASTER
from test_mix_and_brief import rows as mix_rows


def filters(**changes):
    return {**{field.name: None for field in fields(Scope)}, **changes}


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch, request):
    def deny_provider(*args, **kwargs):
        raise AssertionError("Dashboard section checks must remain offline")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", deny_provider)
    monkeypatch.setattr("analyst.runtime.Runner.run", deny_provider)
    if getattr(request, "param", "") == "shared_mix":
        rows = mix_rows()
        snapshot = build_snapshot(
            rows,
            {
                "source_url": "https://synthetic.invalid/agent-mix",
                "source_sha256": sha256(canonical(rows)),
                "fetched_at": "2026-10-17T00:00:00Z",
            },
            MIX_MASTER,
        )
        analysis = service(tmp_path, Loader(snapshot))
        bound = await analysis.get_context()
    else:
        analysis = service(tmp_path)
        bound = await analysis.get_context(filters={"production_line": "Line-1"})
    toolkit = AnalystTools(tmp_path / "run", [], memory_enabled=False)

    async def emit(*args):
        pass

    subject = AnalystRuntime(
        Settings(mode="live", api_key="offline-invalid-key", memory_enabled=False),
        toolkit,
        emit,
        analysis_context=bound,
        analysis_service=analysis,
    )
    try:
        yield subject
    finally:
        await toolkit.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section",
    [
        "period_halves",
        "category_summary",
        "shared_category_controls",
        "line_cost_comparison",
        "line_comparison_note",
        "inspection_methods",
        "decomposition",
    ],
)
@pytest.mark.parametrize("changes", [{}, {"scenario": "exclude_cost_10", "severity": "Critical"}])
async def test_dashboard_sections_preserve_canonical_values_scope_and_evidence(
    runtime, section, changes
):
    original = runtime.analysis_context
    expected = await runtime.read_dashboard_context(filters(**changes))
    view = json.loads(
        await runtime._call("analysis_query", {"section": section, "filters": filters(**changes)})
    )
    assert view["status"] == "completed", view
    assert len(canonical(view).encode("utf-8")) <= VIEW_BYTES
    entry = next(
        item
        for item in runtime.toolkit.ledger.records()
        if item["execution_id"] == view["receipt_id"]
    )
    assert entry["result"]["analysis"][section] == expected["analysis"][section]
    assert entry["analysis_context"] == expected["context"]
    assert view["scope"] == expected["context"]["filters"]
    assert runtime.analysis_context == original
    if section in {"category_summary", "shared_category_controls", "line_cost_comparison"}:
        assert (
            entry["result"]["analysis"]["line_comparison_note"]
            == expected["analysis"]["line_comparison_note"]
        )
    if numeric_values(expected["analysis"][section]):
        assert any(
            item["path"].startswith(f"$.analysis.{section}.")
            or item["path"].startswith(f"$.analysis.{section}[")
            for item in view["numeric_evidence"]
        )
    for item in view["numeric_evidence"]:
        assert result_at_path(entry["result"], item["path"]) == item["value"]
        assert runtime.toolkit.ledger.verify_claims(
            [runtime.claim_catalog[item["id"]].model_dump()]
        )["verified"]
    assert runtime.toolkit.ledger.verify_integrity() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["shared_mix"], indirect=True)
async def test_nonempty_shared_category_controls_have_bounded_citable_evidence(runtime):
    await test_dashboard_sections_preserve_canonical_values_scope_and_evidence(
        runtime, "shared_category_controls", {}
    )
    assert runtime.analysis_context["analysis"]["shared_category_controls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("end_month", ["2024-01", "2024-05"])
async def test_unavailable_half_period_is_not_replaced_with_endpoint_change(runtime, end_month):
    view = json.loads(
        await runtime._call(
            "analysis_query", {"section": "period_halves", "filters": filters(end_month=end_month)}
        )
    )
    assert view["status"] == "completed", view
    assert view["result"]["analysis"] == {
        "period_halves": {
            "enabled": False,
            "reason": "Half-period comparison requires an even number of at least two selected months.",
        }
    }


@pytest.mark.asyncio
async def test_overview_stays_bounded_and_halves_have_distinct_periods(runtime):
    view = json.loads(
        await runtime._call("analysis_query", {"section": "overview", "filters": filters()})
    )
    assert view["status"] == "completed", view
    assert set(view["result"]["analysis"]) == {"kpis", "trend", "calendar_coverage"}
    halves = json.loads(
        await runtime._call("analysis_query", {"section": "period_halves", "filters": filters()})
    )["result"]["analysis"]["period_halves"]
    assert halves["first"]["months"] == ["2024-01", "2024-02", "2024-03"]
    assert halves["second"]["months"] == ["2024-04", "2024-05", "2024-06"]
    assert halves["event_count_change"] != view["result"]["analysis"]["trend"]["absolute_change"]
