"""Custom analysis and Dashboard drilldowns use the exact frozen source inputs."""

import asyncio
import json

import pytest
from analyst.analysis_sql import execute_snapshot_query, query_snapshot
from analyst.claim_feedback import source_contexts
from analyst.evidence import sha256
from analyst.product_tools import SECTIONS, product_schemas
from analyst.report_output import evidence_manifest, metric_unit
from analyst.tools import AnalystTools
from test_agent_dashboard_sections import filters
from test_agent_dashboard_sections import runtime as dashboard_runtime

# Reuse the canonical Dashboard fixture under its public pytest fixture name.
runtime = dashboard_runtime


@pytest.mark.asyncio
async def test_every_canonical_report_section_is_reachable(runtime):
    assert set(runtime.analysis_context["analysis"]) <= set(SECTIONS)
    for section in (
        "top_product_cluster",
        "event_populations",
        "metadata",
        "metric_metadata",
        "scope",
    ):
        actual, context = await runtime.product_tools.call(
            "analysis_query", {"section": section, "filters": filters()}
        )
        assert actual["analysis"][section] == runtime.analysis_context["analysis"][section]
        assert context == runtime.analysis_context["context"]


@pytest.mark.asyncio
async def test_exact_day_record_drilldown_does_not_change_analysis_scope(runtime):
    original = runtime.analysis_context
    arguments = {
        "page": 1,
        "page_size": 100,
        "sort_by": "defect_id",
        "sort_order": "asc",
        "defect_date": "2024-01-15",
        "filters": filters(),
    }
    actual, context = await runtime.product_tools.call("analysis_records", arguments)
    expected = await runtime.analysis_service.records(
        original["context"]["context_id"],
        **{key: value for key, value in arguments.items() if key != "filters"},
    )
    assert actual == {"ok": True, **expected}
    assert actual["rows"]
    assert all(row["defect_date"] == "2024-01-15" for row in actual["rows"])
    assert context == original["context"]
    assert runtime.analysis_context == original
    arguments["defect_date"] = "2024-02-30"
    with pytest.raises(ValueError, match="valid ISO date"):
        await runtime.product_tools.call("analysis_records", arguments)


@pytest.mark.asyncio
async def test_original_source_row_can_trace_quarantined_duplicate(runtime):
    arguments = {"defect_id": None, "source_row": 8, "limit": 100, "filters": filters()}
    actual, context = await runtime.product_tools.call("analysis_provenance", arguments)
    expected = await runtime.analysis_service.trace_records(
        context["context_id"], source_row=8, limit=100
    )
    assert actual == {"ok": True, **expected}
    assert any(row["source_row"] == 8 for row in actual["raw_records"])
    with pytest.raises(ValueError, match="Choose defect_id or source_row"):
        await runtime.product_tools.call("analysis_provenance", {**arguments, "defect_id": 7})


@pytest.mark.asyncio
@pytest.mark.parametrize("export_format", ["csv", "zip", "brief", "quality_workbook"])
async def test_agent_result_export_is_not_advertised_or_dispatched(runtime, export_format):
    assert "analysis_export" not in {schema["name"] for schema in product_schemas()}
    before = runtime.toolkit.ledger.artifacts()
    with pytest.raises(ValueError, match="Unsupported product tool"):
        await runtime.product_tools.call(
            "analysis_export", {"format": export_format, "filters": filters()}
        )
    assert runtime.toolkit.ledger.artifacts() == before


@pytest.mark.asyncio
async def test_dashboard_brief_remains_available_without_agent_export(runtime):
    context = runtime.analysis_context["context"]
    content = await runtime.analysis_service.export_brief(context["context_id"], runtime.language)
    assert content and isinstance(content, bytes)
    assert runtime.toolkit.ledger.artifacts() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{}, {"scenario": "exclude_cost_10"}, {"production_line": ""}])
async def test_custom_sql_matches_canonical_scope_and_retains_code_results(runtime, changes):
    original = runtime.analysis_context
    selected = await runtime.read_dashboard_context(filters(**changes))
    query = (
        "SELECT count(*) AS event_count, sum(repair_cost) AS known_repair_cost, "
        "count(*) FILTER (WHERE repair_cost IS NULL) AS cost_unknown_count FROM events"
    )
    view = json.loads(
        await runtime._call("analysis_sql", {"query": query, "filters": filters(**changes)})
    )
    assert view["status"] == "completed", view["result"]
    entry = next(
        item
        for item in runtime.toolkit.ledger.records()
        if item["execution_id"] == view["receipt_id"]
    )
    result = entry["result"]
    assert result["rows"] == [
        {
            key: selected["analysis"]["kpis"][key]
            for key in ("event_count", "known_repair_cost", "cost_unknown_count")
        }
    ]
    assert entry["analysis_context"] == selected["context"]
    assert result["query"] == query
    assert result["query_sha256"] == sha256(query)
    files = {
        item["name"].rsplit(".", 1)[-1]: runtime.toolkit.ledger.resolve_artifact(item["path"])
        for item in result["artifacts"]
    }
    assert files["sql"].read_text(encoding="utf-8") == query
    retained = json.loads(files["json"].read_text(encoding="utf-8"))
    assert retained["rows"] == result["rows"]
    assert retained["context"] == selected["context"]
    assert view["numeric_evidence"]
    assert source_contexts(entry, "$.rows[0].event_count") == [selected["context"]]
    assert view["source_projection"]["derivation"]["query"] == query
    event_claim = next(
        item for item in view["numeric_evidence"] if item["path"] == "$.rows[0].event_count"
    )
    manifest = evidence_manifest(
        runtime.toolkit.ledger,
        [runtime.claim_catalog[event_claim["id"]].model_dump()],
        language="en",
        instructions_version="test",
        tools_version="test",
    )
    assert manifest["references"][0]["context_ids"] == [selected["context"]["context_id"]]
    # A custom expression cannot acquire a canonical unit merely by alias.
    assert metric_unit(entry, "$.rows[0].event_count") is None
    assert runtime.analysis_context == original
    assert runtime.toolkit.ledger.verify_integrity() == []


@pytest.mark.asyncio
async def test_custom_sql_supports_join_cte_window_and_full_local_results(runtime):
    query = (
        "WITH joined AS (SELECT e.defect_id, p.product_name, e.repair_cost "
        "FROM events e JOIN products p USING(product_id)) "
        "SELECT *, row_number() OVER (ORDER BY defect_id, i) AS ordinal "
        "FROM joined CROSS JOIN range(0, 300) AS repetition(i)"
    )
    actual, _ = await runtime.product_tools.call(
        "analysis_sql", {"query": query, "filters": filters()}
    )
    assert len(actual["rows"]) > 1000
    assert max(row["ordinal"] for row in actual["rows"]) == len(actual["rows"])
    assert any(row["repair_cost"] is None for row in actual["rows"])
    result_file = next(item for item in actual["artifacts"] if item["name"].endswith(".json"))
    assert (
        json.loads(
            runtime.toolkit.ledger.resolve_artifact(result_file["path"]).read_text(encoding="utf-8")
        )["rows"]
        == actual["rows"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "SELECT count(*) AS n FROM events -- query total; load the results",
        "SELECT count(*) AS n /* read_csv('unrelated.csv') */ FROM events",
        "SELECT 'load' AS note, count(*) AS n FROM events",
        'WITH "copy" AS (SELECT * FROM events) SELECT count(*) AS "query" FROM "copy"',
    ],
)
async def test_custom_sql_treats_comments_literals_and_aliases_as_data(runtime, query):
    result, _ = await runtime.product_tools.call(
        "analysis_sql", {"query": query, "filters": filters()}
    )
    assert (
        result["rows"][0].get("n", result["rows"][0].get("query"))
        == (runtime.analysis_context["analysis"]["kpis"]["event_count"])
    )


@pytest.mark.asyncio
async def test_repeated_sql_retains_different_results_without_overwriting(runtime, monkeypatch):
    import analyst.product_tools as product_tools

    execute = product_tools.execute_snapshot_query
    samples = iter([0.25, 0.75, 0.25])

    async def sampled_result(*args, **kwargs):
        result = await execute(*args, **kwargs)
        # Control sampling to exercise both changed and identical SQL results.
        result["rows"][0]["sample"] = next(samples)
        return result

    monkeypatch.setattr(product_tools, "execute_snapshot_query", sampled_result)
    arguments = {
        "query": "SELECT count(*) AS n, random() AS sample FROM events",
        "filters": filters(),
    }
    results = [(await runtime.product_tools.call("analysis_sql", arguments))[0] for _ in range(3)]
    assert results[0]["artifacts"] != results[1]["artifacts"]
    assert results[0]["artifacts"] == results[2]["artifacts"]
    for result in results:
        retained = next(item for item in result["artifacts"] if item["name"].endswith(".json"))
        content = json.loads(
            runtime.toolkit.ledger.resolve_artifact(retained["path"]).read_text(encoding="utf-8")
        )
        assert content["query"] == arguments["query"]
        assert content["rows"] == result["rows"]
    assert runtime.toolkit.ledger.verify_integrity() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM events",
        "SELECT 1; SELECT 2",
        "COPY events TO 'overwrite.csv'",
        "SELECT * FROM read_csv_auto('C:/Windows/win.ini')",
        """SELECT * FROM events CROSS JOIN "read_csv_auto"('C:/Windows/win.ini')""",
        """SELECT * FROM events CROSS JOIN "read_parquet"('outside.parquet')""",
        """SELECT * FROM events CROSS JOIN "query"('SELECT * FROM events')""",
        """SELECT * FROM events CROSS JOIN "query_table"('events')""",
        "SELECT * FROM events CROSS JOIN 'C:/Windows/win.ini'",
        "SELECT * FROM query('SELECT * FROM events')",
        "SELECT * FROM 'C:/Windows/win.ini'",
        "SELECT 1 AS same_name, 2 AS same_name FROM events",
        "SELECT 999 AS fabricated_total",
        "WITH events AS (SELECT 999 AS fabricated_total) SELECT * FROM events",
    ],
)
async def test_custom_sql_cannot_mutate_or_read_independent_sources(runtime, query):
    paths = await runtime.analysis_service.input_paths_for_context(
        runtime.analysis_context["context"]["context_id"]
    )
    before = {str(path): sha256(path.read_bytes()) for path in paths}
    actual = json.loads(await runtime._call("analysis_sql", {"query": query, "filters": filters()}))
    assert actual["status"] == "failed", actual
    assert {str(path): sha256(path.read_bytes()) for path in paths} == before
    assert runtime.toolkit.ledger.artifacts() == []


@pytest.mark.asyncio
async def test_custom_sql_rechecks_bytes_consumed_against_canonical_receipt(runtime):
    paths = await runtime.analysis_service.input_paths_for_context(
        runtime.analysis_context["context"]["context_id"]
    )
    hashes = {path.name: sha256(path.read_bytes()) for path in paths}
    hashes[paths[0].name] = "0" * 64
    with pytest.raises(ValueError, match="canonical receipt"):
        query_snapshot("SELECT count(*) FROM events", paths, hashes)


@pytest.mark.asyncio
async def test_owner_cancellation_interrupts_active_snapshot_sql(runtime):
    paths = await runtime.analysis_service.input_paths_for_context(
        runtime.analysis_context["context"]["context_id"]
    )
    hashes = {path.name: sha256(path.read_bytes()) for path in paths}
    task = asyncio.create_task(
        execute_snapshot_query(
            "SELECT sum(sin(i)) FROM events CROSS JOIN range(0, 1000000000) AS work(i)",
            paths,
            hashes,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert runtime.toolkit.ledger.artifacts() == []


@pytest.mark.asyncio
async def test_unbounded_local_tool_resources_use_none_without_reset_failure(tmp_path):
    tools = AnalystTools(tmp_path / "first", [], timeout_seconds=None, memory_mb=None)
    try:
        assert tools.python.timeout_seconds is None
        assert tools.python.memory_mb is None
        await tools.begin_run(tmp_path / "second")
        result = await tools.call("local_sql_engine", {"query": "SELECT 1 AS verified"})
        assert result["result"]["rows"] == [{"verified": 1}]
    finally:
        await tools.close()


def test_sql_schema_keeps_explicit_scope_and_strict_arguments():
    schema = next(item for item in product_schemas() if item["name"] == "analysis_sql")
    assert schema["strict"]
    assert set(schema["parameters"]["required"]) == {"query", "filters", "rationale"}


def test_analysis_tools_have_nullable_rationale_and_strict_decision_boundaries():
    schemas = {item["name"]: item for item in product_schemas()}
    analysis_tools = [
        "analysis_query",
        "analysis_compare",
        "scenario_compare",
        "analysis_records",
        "analysis_provenance",
        "analysis_sql",
    ]
    expected_desc = (
        "One concise sentence explaining why this tool is chosen and which analytical "
        "hypothesis or decision question it answers."
    )
    for name in analysis_tools:
        tool = schemas[name]
        props = tool["parameters"]["properties"]
        assert "rationale" in props, f"Missing rationale in {name}"
        assert props["rationale"]["description"] == expected_desc
        assert props["rationale"]["type"] == ["string", "null"]
        assert set(tool["parameters"]["required"]) == set(props)

    # Decision boundaries check
    assert "analysis_query" in schemas["analysis_sql"]["description"]
    assert "analysis_sql" in schemas["analysis_query"]["description"]
    assert "standard KPIs" in schemas["analysis_query"]["description"]


@pytest.mark.asyncio
async def test_analysis_tools_preserve_rationale_in_output(runtime):
    test_rationale = "Hypothesis: line 2 defect spike driving warranty impact."
    actual, context = await runtime.product_tools.call(
        "analysis_query",
        {"section": "kpis", "filters": filters(), "rationale": test_rationale},
    )
    assert actual.get("rationale") == test_rationale
