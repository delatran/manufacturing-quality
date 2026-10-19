"""Offline regressions for captured native public-summary regrouping."""

import asyncio
import copy
import json
from pathlib import Path

import pytest
from analyst.config import Settings
from analyst.public_summary import PublicSummaryDisplay
from analyst.runtime import AnalystRuntime
from test_managed_agents import (
    Node,
    Sessions,
    Stream,
    model_for,
    reasoning_events,
    reasoning_item,
)


def captured_regrouping():
    path = Path(__file__).parent / "fixtures" / "native_summary_regrouping.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert fixture["evidence_type"] == "captured_native_public_summary_regrouping_offline_fixture"
    return fixture["events"]


def native_summary_event(row):
    data = row["data"]
    return Node(
        type=data["provider_event"],
        **{
            key: data[key]
            for key in ("event_id", "session_id", "turn_id", "item_id", "summary_index", "text")
        },
    )


@pytest.mark.parametrize("language", ["en", "vi"])
def test_captured_native_regrouping_preserves_original_summaries_without_another_request(language):
    rows = captured_regrouping()
    source = [row for row in rows if row["id"] in {5098, 5195}]
    replay = [row for row in rows if row["id"] in {11170, 11172}]
    item = next(row["data"] for row in rows if row["id"] == 11174)
    records = [
        {
            "id": item["item_id"],
            "turn_id": item["turn_id"],
            "type": item["item_type"],
            "status": item["status"],
            "summary": [{"type": "summary_text", "text": row["data"]["text"]} for row in replay],
        }
    ]
    original_records = copy.deepcopy(records)
    assert records[0]["status"] == "incomplete"

    async def run():
        emitted = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-regrouping-fixture"),
            object(),
            lambda kind, data: emitted.append((kind, data)),
            language=language,
        )
        model, _ = model_for(Sessions([], [], []))
        model.session_id, model.turn_id = item["session_id"], item["turn_id"]
        model.emit = runtime.emit
        await model._observe(Stream([native_summary_event(row) for row in source]))
        await model._observe(Stream([native_summary_event(row) for row in replay]))
        await model._reconcile_reasoning_aliases(records)
        aliases = [data for kind, data in emitted if kind == "reasoning_alias"]
        assert [data["canonical_summary_indices"] for data in aliases] == [[0], [1]]
        assert {data["canonical_item_id"] for data in aliases} == {item["item_id"]}
        assert runtime.models == [], "Source summaries never start another model request"
        assert runtime.budget == {"cost_usd": 0.0}
        assert len([kind for kind, _ in emitted if kind == "reasoning_summary"]) == len(
            source + replay
        )
        latest = {
            data["summary_index"]: data
            for kind, data in emitted
            if kind == "reasoning_summary" and data.get("item_id") == item["item_id"]
        }
        assert set(latest) == {0, 1}
        for index, row in enumerate(replay):
            final = latest[index]
            assert final["text"] == row["data"]["text"]
            assert final["display_text"] == final["text"]
            assert final["display_source"] == "provider_summary"
            assert final["display_localized"] is False
            assert final["display_language"] == "en"
            assert "translation_state" not in final
            assert final["event_id"] == row["data"]["event_id"]
        assert records == original_records, "Identity aliases do not upgrade incomplete items"

    asyncio.run(run())


@pytest.mark.parametrize("status", ["completed", "incomplete"])
def test_summary_subsequence_alias_maps_all_source_indices(status):
    model, observed = model_for(Sessions([], [], []))
    model.session_id, model.turn_id = "sess_real", "turn_real"
    canonical = reasoning_item("canonical", status=status, summary=["Before", "First", "Second"])
    source = reasoning_item("transient", summary=["First", "Second"])

    async def run():
        await model._observe(Stream(reasoning_events(source, "subsequence")))
        await model._reconcile_reasoning_aliases([canonical.model_dump()])

    asyncio.run(run())
    aliases = [data for kind, data in observed if kind == "reasoning_alias"]
    assert len(aliases) == 1
    assert aliases[0]["canonical_summary_indices"] == [1, 2]


@pytest.mark.parametrize("case", ["ambiguous_position", "other_turn", "in_progress", "persisted"])
def test_summary_subsequence_alias_preserves_distinct_or_ambiguous_items(case):
    model, observed = model_for(Sessions([], [], []))
    model.session_id, model.turn_id = "sess_real", "turn_real"
    canonical = reasoning_item("canonical", summary=["Before", "Same", "After"])
    source = reasoning_item("transient", summary=["Same"])
    if case == "ambiguous_position":
        canonical.summary.append(Node(type="summary_text", text="Same"))
    elif case == "other_turn":
        canonical.turn_id = "other-turn"
    elif case == "in_progress":
        canonical.status = "in_progress"
    records = [canonical.model_dump()]
    if case == "persisted":
        records.append(source.model_dump())

    async def run():
        await model._observe(Stream(reasoning_events(source, case)))
        await model._reconcile_reasoning_aliases(records)

    asyncio.run(run())
    assert not [data for kind, data in observed if kind == "reasoning_alias"]


def test_partial_alias_keeps_future_unmapped_summary_parts_separate():
    display = PublicSummaryDisplay()
    base = {"session_id": "session", "turn_id": "turn", "item_id": "source"}
    display.alias(
        {
            **base,
            "reconciled": True,
            "canonical_item_id": "durable",
            "canonical_summary_indices": [2],
        }
    )
    assert display.key({**base, "summary_index": 0}) == ("session", "turn", ("item", "durable"), 2)
    assert display.key({**base, "summary_index": 1}) == ("session", "turn", ("item", "source"), 1)
