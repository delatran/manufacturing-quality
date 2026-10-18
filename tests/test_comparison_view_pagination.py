"""Bound comparison previews without skipping a prefix that fits the response."""

import copy
import json

import pytest
from analyst.agent_views import VIEW_BYTES
from analyst.evidence import canonical
from analyst.runtime import result_at_path
from test_measurement_units import actual6 as actual6


@pytest.mark.asyncio
async def test_three_row_overflow_preserves_two_rows_and_exact_continuation(actual6):
    view = actual6.comparison
    entry = next(
        item
        for item in actual6.tools.ledger.records()
        if item["execution_id"] == view["receipt_id"]
    )
    original = canonical(entry)
    rows = view["result"]["decomposition"]["rows"]
    assert [row["name"] for row in rows] == ["6", "63"]
    assert len(canonical(view).encode()) <= VIEW_BYTES
    assert (
        view["result"]["decomposition"]["known_cost_reconciliation"]
        == entry["result"]["decomposition"]["known_cost_reconciliation"]
    )
    for number in view["numeric_evidence"]:
        assert result_at_path(entry["result"], number["path"]) == number["value"]
    navigation = next(
        item for item in view["omitted_collections"] if item["path"] == "$.decomposition.rows"
    )
    assert navigation["returned_count"] == navigation["next_offset"] == 2
    assert navigation["item_count"] == 4
    tail = json.loads(await actual6.runtime._call("evidence_detail", navigation["next_call"]))
    assert tail["status"] == "completed", tail
    assert len(canonical(tail).encode()) <= VIEW_BYTES
    assert tail["page"]["offset"] == 2
    assert tail["page"]["returned_count"] == 2
    assert tail["page"]["item_count"] == 4
    assert tail["page"]["next_offset"] is None
    assert tail["page"]["next_call"] is None
    selected = tail["result"]["selected"]
    assert [item["path"] for item in selected] == [
        "$.decomposition.rows[2]",
        "$.decomposition.rows[3]",
    ]
    assert [row["name"] for row in rows] + [item["value"]["name"] for item in selected] == [
        row["name"] for row in entry["result"]["decomposition"]["rows"]
    ]
    for number in tail["numeric_evidence"]:
        assert result_at_path(entry["result"], number["path"]) == number["value"]
    assert canonical(entry) == original
    assert actual6.tools.ledger.verify_integrity() == []


@pytest.mark.asyncio
async def test_one_oversized_row_still_fails_without_truncating_source(actual6):
    original = next(
        item
        for item in actual6.tools.ledger.records()
        if item["execution_id"] == actual6.comparison["receipt_id"]
    )
    result = copy.deepcopy(original["result"])
    result["decomposition"]["rows"][0]["name"] = "Oversized product " + "x" * VIEW_BYTES
    entry = actual6.tools.ledger.record(
        "analysis_compare",
        original["code"],
        result,
        execution_kind="bounded_analysis",
        analysis_context=original["analysis_context"],
    )
    before = canonical(entry)
    with pytest.raises(ValueError, match="Analysis view exceeds the response bound"):
        actual6.runtime.model_receipt(entry)
    assert entry["execution_id"] not in actual6.runtime.exposed_receipts
    assert canonical(entry) == before
    assert actual6.tools.ledger.verify_integrity() == []
