"""Public provider summaries retain their source text without extra model work."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from analyst import runtime as runtime_module
from analyst.config import Settings
from analyst.managed_agents import ManagedAgentsModel
from analyst.managed_recovery import RECOVERY_STOP, recover_managed_sessions
from analyst.public_summary import PublicSummaryDisplay
from analyst.runtime import AnalystRuntime
from analyst.store import SessionStore
from test_managed_agents import Sessions, Stream, event, message, request, session
from test_managed_recovery import Provider

IDENTITY = {
    "provider_api": "agents",
    "session_id": "summary-source",
    "turn_id": "source-turn",
    "item_id": "summary-original",
    "summary_index": 0,
    "event_id": "native-summary-event",
}
ENGLISH = "Reviewing the request and checking 12 records in `event_count`."
VIETNAMESE = "Đang kiểm tra yêu cầu và 12 dòng dữ liệu trong `event_count`."


@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize(
    "source", [ENGLISH, VIETNAMESE, "Validate totals.", "", "CSV `event_count`"]
)
@pytest.mark.parametrize("stop", [None, "blocked", "provider_stopped"])
def test_completed_provider_summary_never_creates_a_model_or_changes_accounting(
    monkeypatch, language, source, stop
):
    def unexpected_model(*args, **kwargs):
        pytest.fail("Displaying a provider summary must not create another model")

    monkeypatch.setattr(runtime_module, "ManagedAgentsModel", unexpected_model)

    async def run():
        events = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language=language,
        )
        runtime.budget.update({"cost_usd": 0.42, "unresolved_generation_requests": 1})
        if stop:
            runtime.budget[stop] = True
        original_budget = deepcopy(runtime.budget)
        raw = {**IDENTITY, "text": source, "done": True}
        await runtime.emit("reasoning_summary", raw)
        await runtime.emit("reasoning_part", {**raw, "phase": "done"})
        await runtime.emit("reasoning_summary", raw)
        assert raw == {**IDENTITY, "text": source, "done": True}
        assert len(events) == 3
        for _, data in events:
            assert data["text"] == data["display_text"] == source
            assert data["display_source"] == "provider_summary"
            assert data["display_mode"] == "snapshot"
            assert data["display_localized"] is False
            assert data["event_id"] == IDENTITY["event_id"]
            assert data["language"] == language
            assert "translation_state" not in data
            assert "source_provider_event_id" not in data
        assert runtime.models == []
        assert runtime.budget == original_budget
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(run())


@pytest.mark.parametrize("language", ["en", "vi"])
def test_managed_response_uses_only_the_analyst_session_and_usage(monkeypatch, language):
    final = session()
    final.spend_control = None
    sessions = Sessions(
        [final],
        [[message()]],
        [
            Stream(
                [
                    event("agent.session.created", session=final),
                    event(
                        "agent.session.turn.reasoning_summary_text.done",
                        session_id=final.id,
                        turn_id="turn_real",
                        item_id="reasoning-original",
                        summary_index=0,
                        text=ENGLISH,
                    ),
                    event("agent.session.turn.completed", session_id=final.id, turn_id="turn_real"),
                    event("agent.session.idle", session=final),
                ]
            )
        ],
    )

    async def close():
        pass

    def model(settings, emit, **kwargs):
        client = SimpleNamespace(
            beta=SimpleNamespace(agents=SimpleNamespace(sessions=sessions)), close=close
        )
        return ManagedAgentsModel(settings, emit, client=client, **kwargs)

    monkeypatch.setattr(runtime_module, "ManagedAgentsModel", model)

    async def run():
        events = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language=language,
        )
        analyst = runtime.model("Lead Analyst")
        try:
            result = await request(analyst)
            assert result.output[0].content[0].text == "Verified result"
            assert len(sessions.creates) == 1
            assert sessions.creates[0]["agent"]["model"] == "gpt-6-luna"
            assert "spend_control" not in sessions.creates[0]
            assert runtime.models == [analyst]
            assert analyst.role == "Lead Analyst"
            assert len(analyst.usage) == 1
            assert runtime.budget["cost_usd"] > 0
            assert runtime.budget["unresolved_generation_requests"] == 0
            summaries = [data for kind, data in events if kind == "reasoning_summary"]
            assert len(summaries) == 1
            assert summaries[0]["text"] == summaries[0]["display_text"] == ENGLISH
            assert summaries[0]["display_language"] == "en"
            assert summaries[0]["display_localized"] is False
            assert "translation_state" not in summaries[0]
            assert not any(data.get("role") == "Summary Translator" for _, data in events)
        finally:
            await analyst.client.close()

    asyncio.run(run())


def test_streamed_source_snapshots_preserve_every_raw_fragment_and_protected_token():
    display = PublicSummaryDisplay()
    source = (
        "Reviewing 12 records and 123,456.90 in `event_count`. "
        "[claim:analysis.monthly[0].known_repair_cost] "
        "https://example.test/report2024.md"
    )
    fragments = [source[:2], source[2:11], source[11:]]
    events = [
        {**IDENTITY, "event_id": f"delta-{index}", "delta": delta}
        for index, delta in enumerate(fragments)
    ]
    original = deepcopy(events)
    projected = [display.project(data) for data in events]
    assert events == original
    assert [data["delta"] for data in projected] == fragments
    assert [data["event_id"] for data in projected] == ["delta-0", "delta-1", "delta-2"]
    assert [data["display_text"] for data in projected] == [source[:2], source[:11], source]
    done = display.project({**IDENTITY, "phase": "done", "text": source})
    assert done["text"] == done["display_text"] == source
    assert done["display_language"] == "en"


def test_summary_parts_keep_session_turn_item_and_index_boundaries():
    display = PublicSummaryDisplay()
    for index, changes in enumerate(
        (
            {},
            {"session_id": "other-session"},
            {"turn_id": "other-turn"},
            {"item_id": "other-item"},
            {"summary_index": 1},
        )
    ):
        identity = {**IDENTITY, **changes}
        display.project({**identity, "delta": f"Part {index}"})
        result = display.project({**identity, "delta": "."})
        assert result["display_text"] == f"Part {index}."
    anonymous = PublicSummaryDisplay()
    assert anonymous.project({"delta": "First"})["display_text"] == "First"
    assert anonymous.project({"delta": "Second"})["display_text"] == "Second"
    assert anonymous.parts == {}


def test_alias_keeps_completed_source_snapshot_for_later_native_parts():
    display = PublicSummaryDisplay()
    display.project({**IDENTITY, "text": ENGLISH, "done": True})
    receipt = {
        **IDENTITY,
        "canonical_item_id": "durable",
        "canonical_summary_indices": [2],
        "reconciled": True,
    }
    original = deepcopy(receipt)
    display.alias(receipt)
    canonical = {**IDENTITY, "item_id": "durable", "summary_index": 2}
    result = display.project({**canonical, "phase": "done"})
    assert result["display_text"] == ENGLISH
    assert display.project({**IDENTITY, "delta": " Next."})["display_text"] == ENGLISH + " Next."
    assert display.project({**canonical, "phase": "done"})["display_text"] == ENGLISH + " Next."
    assert receipt == original


def test_alias_does_not_replace_a_durable_snapshot_or_another_sessions_text():
    display = PublicSummaryDisplay()
    display.project({**IDENTITY, "text": "Transient"})
    display.project({**IDENTITY, "item_id": "durable", "text": "Durable"})
    other = {**IDENTITY, "session_id": "other-session"}
    display.project({**other, "text": "Other session"})
    display.alias({**IDENTITY, "canonical_item_id": "durable", "reconciled": True})
    assert display.project({**IDENTITY, "phase": "done"})["display_text"] == "Durable"
    assert display.project({**other, "delta": "."})["display_text"] == "Other session."


@pytest.mark.parametrize("indices", [[], [0, 0], [-1], [True], ["0"], "0"])
def test_invalid_index_alias_keeps_source_identity_and_text(indices):
    display = PublicSummaryDisplay()
    display.project({**IDENTITY, "text": ENGLISH})
    display.alias(
        {
            **IDENTITY,
            "canonical_item_id": "durable",
            "canonical_summary_indices": indices,
            "reconciled": True,
        }
    )
    assert display.key(IDENTITY)[2:] == (("item", IDENTITY["item_id"]), 0)
    assert display.project({**IDENTITY, "phase": "done"})["display_text"] == ENGLISH


@pytest.mark.parametrize("state", ["active", "unknown", "rejected", "settled"])
def test_historical_translator_receipts_retain_original_storage_and_recovery(tmp_path, state):
    store = SessionStore(tmp_path)
    with store.db:
        store.db.execute("INSERT INTO sessions(id,created) VALUES ('visitor','fixture')")
    run_id, _ = store.begin_run("visitor")
    role = {"provider_api": "agents", "role": "Summary Translator"}
    store.emit(
        "visitor",
        run_id,
        "reasoning_summary",
        {
            **IDENTITY,
            "text": ENGLISH,
            "display_text": VIETNAMESE,
            "display_source": "translated_provider_summary",
            "display_localized": True,
            "translation_state": "translated",
            "language": "vi",
        },
    )
    store.emit(
        "visitor",
        run_id,
        "model_started",
        {**role, "boundary_id": "historical-boundary", "session_id": None},
    )
    if state in {"active", "settled"}:
        store.emit(
            "visitor",
            run_id,
            "managed_session_created",
            {**role, "session_id": "asess_known"},
        )
    if state == "rejected":
        store.emit(
            "visitor",
            run_id,
            "managed_session_rejected",
            {**role, "generation_dispatched": False},
        )
    elif state == "settled":
        store.emit(
            "visitor",
            run_id,
            "usage",
            {
                **role,
                "provider_session_id": "asess_known",
                "provider_terminal": True,
                "cost_usd": 0.0123,
                "input_tokens": 12,
                "output_tokens": 4,
            },
        )
    original_events = store.events("visitor")
    store.close()

    reopened = SessionStore(tmp_path)
    try:
        reopened.recover_interrupted()
        provider = Provider()
        asyncio.run(recover_managed_sessions(reopened, Settings(), client=provider))
        persisted = {event["id"]: event for event in reopened.events("visitor")}
        assert all(persisted[event["id"]] == event for event in original_events)
        if state == "active":
            assert [call[0] for call in provider.calls] == ["cancel", "retrieve", "turns"]
        else:
            assert provider.calls == []
        assert reopened.stopped("visitor") == (RECOVERY_STOP if state == "unknown" else None)
    finally:
        reopened.close()
