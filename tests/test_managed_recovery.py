"""Restart recovery must stop existing managed work without repeating inputs."""

import asyncio
from types import SimpleNamespace

import pytest
from analyst.config import Settings
from analyst.managed_recovery import RECOVERY_STOP, recover_managed_sessions
from analyst.store import SessionStore


class Page:
    def __init__(self, turns):
        self.turns = turns

    async def __aiter__(self):
        for turn in self.turns:
            yield turn


class Provider:
    def __init__(self, *, error=None, turn_status="cancelled", session_id="asess_known"):
        self.calls = []
        self.error = error
        self.turn_status = turn_status
        self.session_id = session_id
        self.beta = SimpleNamespace(
            agents=SimpleNamespace(
                sessions=SimpleNamespace(
                    events=SimpleNamespace(create=self.cancel),
                    retrieve=self.retrieve,
                    turns=SimpleNamespace(list=self.turns),
                )
            )
        )

    async def cancel(self, identity, **kwargs):
        self.calls.append(("cancel", identity, kwargs))
        if self.error:
            raise self.error

    async def retrieve(self, identity):
        self.calls.append(("retrieve", identity))
        return SimpleNamespace(id=self.session_id, status="idle")

    async def turns(self, identity, **kwargs):
        self.calls.append(("turns", identity, kwargs))
        return Page(
            [SimpleNamespace(id="turn_known", session_id=identity, status=self.turn_status)]
        )


def interrupted(store, *, backend="agents", created=True):
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO sessions(id,created) VALUES ('visitor','fixture')")
    rid, _ = store.begin_run("visitor")
    store.emit(
        "visitor",
        rid,
        "model_started",
        {
            "provider_api": backend,
            "boundary_id": "boundary_known",
            "session_id": None,
        },
    )
    if backend == "agents" and created:
        store.emit(
            "visitor",
            rid,
            "managed_session_created",
            {
                "provider_api": "agents",
                "session_id": "asess_known",
            },
        )
    store.recover_interrupted()
    return rid


def test_restart_cancels_known_native_session_and_confirms_turn_without_replay(tmp_path):
    store = SessionStore(tmp_path)
    rid = interrupted(store)
    provider = Provider()
    asyncio.run(recover_managed_sessions(store, Settings(), client=provider))
    assert [call[0] for call in provider.calls] == ["cancel", "retrieve", "turns"]
    assert provider.calls[0][2] == {
        "events": [{"type": "agent.session.input.cancel"}],
        "idempotency_key": f"restart-cancel-{rid}-asess_known",
    }
    records = [
        event["data"] for event in store.events("visitor") if event["type"] == "managed_recovery"
    ]
    assert records[-1]["terminal_confirmed"] is True
    assert records[-1]["turn_status"] == "cancelled"
    assert records[-1]["input_replayed"] is False
    assert not store.stopped("visitor")
    asyncio.run(recover_managed_sessions(store, Settings(), client=provider))
    assert len(provider.calls) == 3
    assert (
        store.db.execute("SELECT state FROM runs WHERE id=?", (rid,)).fetchone()[0] == "interrupted"
    )
    store.close()


@pytest.mark.parametrize(
    "provider",
    [
        Provider(error=ConnectionError("private detail must not be persisted")),
        Provider(turn_status="waiting"),
        Provider(session_id="asess_wrong"),
    ],
)
def test_ambiguous_recovery_blocks_conversation_and_records_only_error_type(tmp_path, provider):
    store = SessionStore(tmp_path)
    interrupted(store)
    asyncio.run(recover_managed_sessions(store, Settings(), client=provider))
    assert store.stopped("visitor") == RECOVERY_STOP
    last = store.events("visitor")[-1]["data"]
    assert last["terminal_confirmed"] is False
    assert last["error_type"] in {"ConnectionError", "ValueError"}
    assert "private detail" not in str(last)
    store.close()


def test_uncertain_creation_never_retries_create_or_guesses_session(tmp_path):
    store = SessionStore(tmp_path)
    interrupted(store, created=False)
    provider = Provider()
    asyncio.run(recover_managed_sessions(store, Settings(), client=provider))
    assert provider.calls == []
    assert store.stopped("visitor") == RECOVERY_STOP
    assert store.events("visitor")[-1]["data"]["reason"] == "creation_admission_unknown"
    store.close()


def test_responses_history_and_rejected_native_creation_do_not_use_provider(tmp_path):
    store = SessionStore(tmp_path)
    interrupted(store, backend="responses")
    rid = interrupted(store, created=False)
    store.emit(
        "visitor",
        rid,
        "managed_session_rejected",
        {
            "provider_api": "agents",
            "generation_dispatched": False,
        },
    )
    provider = Provider()
    asyncio.run(recover_managed_sessions(store, Settings(), client=provider))
    assert provider.calls == [] and not store.stopped("visitor")
    store.close()


def test_retry_reuses_cancel_identity_and_preserves_owner_stop(tmp_path):
    store = SessionStore(tmp_path)
    interrupted(store)
    failed = Provider(error=ConnectionError())
    asyncio.run(recover_managed_sessions(store, Settings(), client=failed))
    successful = Provider()
    asyncio.run(recover_managed_sessions(store, Settings(), client=successful))
    assert failed.calls[0] == successful.calls[0]
    assert not store.stopped("visitor")
    rid = interrupted(store)
    store.stop_session("visitor", "Provider policy stop")
    asyncio.run(recover_managed_sessions(store, Settings(), client=Provider()))
    assert store.stopped("visitor") == "Provider policy stop"
    assert rid
    store.close()


def test_terminal_usage_skips_paid_connection_and_policy_failure_stays_stopped(tmp_path):
    store = SessionStore(tmp_path)
    rid = interrupted(store)
    store.emit(
        "visitor",
        rid,
        "usage",
        {
            "provider_api": "agents",
            "provider_session_id": "asess_known",
            "provider_terminal": True,
        },
    )
    provider = Provider()
    asyncio.run(recover_managed_sessions(store, Settings(), client=provider))
    assert not provider.calls
    interrupted(store)

    class PolicyProvider(Provider):
        async def retrieve(self, identity):
            return SimpleNamespace(
                id=identity,
                status="failed",
                error={
                    "code": "misalignment_policy_violation",
                    "message": "private provider text",
                },
            )

    asyncio.run(
        recover_managed_sessions(store, Settings(), client=PolicyProvider(turn_status="failed"))
    )
    assert store.stopped("visitor") == "Provider policy stop"
    assert store.events("visitor")[-1]["data"]["provider_stopped"] is True
    assert "private provider text" not in str(store.events("visitor"))
    store.close()


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_restart_reconciles_unconfirmed_cancel_from_prior_terminal_local_run(tmp_path, state):
    store = SessionStore(tmp_path)
    rid = interrupted(store)
    store.finish_run(rid, state)
    store.emit(
        "visitor",
        rid,
        "provider_cancel",
        {
            "provider_api": "agents",
            "session_id": "asess_known",
            "terminal_confirmed": False,
        },
    )
    provider = Provider()
    asyncio.run(recover_managed_sessions(store, Settings(), client=provider))
    assert [call[0] for call in provider.calls] == ["cancel", "retrieve", "turns"]
    assert not store.stopped("visitor")
    store.close()


def test_restart_after_terminal_checkpoint_clears_only_its_recovery_guard(tmp_path):
    store = SessionStore(tmp_path)
    rid = interrupted(store)
    store.stop_session("visitor", RECOVERY_STOP)
    store.emit(
        "visitor",
        rid,
        "managed_recovery",
        {
            "provider_api": "agents",
            "session_id": "asess_known",
            "terminal_confirmed": True,
        },
    )
    provider = Provider()
    asyncio.run(recover_managed_sessions(store, Settings(), client=provider))
    assert not provider.calls and not store.stopped("visitor")
    assert store.events("visitor")[-1]["data"]["phase"] == "conversation_reconciled"
    store.close()


def test_restart_reads_only_the_latest_durable_turn(tmp_path):
    class LatestTurnPage:
        async def __aiter__(self):
            yield SimpleNamespace(id="turn_known", session_id="asess_known", status="cancelled")
            raise AssertionError("Historical turn pages must not be requested")

    class LatestTurnProvider(Provider):
        async def turns(self, identity, **kwargs):
            assert identity == "asess_known"
            assert kwargs == {"order": "desc", "limit": 1}
            return LatestTurnPage()

    store = SessionStore(tmp_path)
    interrupted(store)
    asyncio.run(recover_managed_sessions(store, Settings(), client=LatestTurnProvider()))
    assert not store.stopped("visitor")
    assert store.events("visitor")[-1]["data"]["terminal_confirmed"] is True
    store.close()


def test_restart_reads_each_conversation_history_once(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    for _ in range(6):
        rid = interrupted(store)
        store.emit(
            "visitor",
            rid,
            "usage",
            {
                "provider_api": "agents",
                "provider_session_id": "asess_known",
                "provider_terminal": True,
            },
        )
    calls = []
    events = store.events

    def read_events(sid, after=0):
        calls.append(sid)
        return events(sid, after)

    monkeypatch.setattr(store, "events", read_events)
    provider = Provider()
    asyncio.run(recover_managed_sessions(store, Settings(), client=provider))
    assert calls == ["visitor"]
    assert not provider.calls
    store.close()
