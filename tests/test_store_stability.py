"""Long conversations remain cheap to inspect without weakening durable ownership."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from analyst.evidence import EvidenceLedger
from analyst.store import SessionStore


@pytest.fixture
def store(tmp_path):
    value = SessionStore(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_workspace_mutations_do_not_reload_conversation_events(store):
    sid = store.create_session()["id"]
    rid, directory = store.begin_run(sid)
    for index in range(20):
        store.emit(sid, rid, "reasoning_summary", {"delta": str(index) * 1000})
    artifact = directory / "artifacts" / "answer.txt"
    artifact.parent.mkdir()
    artifact.write_text("Synthetic answer", encoding="utf-8")
    EvidenceLedger(directory).register_artifact(artifact)
    queries = []
    store.db.set_trace_callback(queries.append)
    try:
        assert store.investigation(sid)["version"] == 0
        assert store.save_investigation(sid, 0, {"summary": "Saved"})["version"] == 1
        store.begin_run(sid)
        store.add_upload(sid, "fixture.csv", b"value\n1\n")
        store.save_ui_command(sid, rid, {"command_id": "fixture", "action": "open_panel"})
        assert store.artifact(sid, rid, "artifacts/answer.txt") == artifact
    finally:
        store.db.set_trace_callback(None)
    assert not any("FROM EVENTS" in query.upper() for query in queries)
    assert not any("SELECT * FROM SESSIONS" in query.upper() for query in queries)
    assert len(store.events(sid)) == 20


@pytest.mark.parametrize(
    "operation",
    [
        lambda store: store.investigation("missing"),
        lambda store: store.save_investigation("missing", 0, {"summary": "Rejected"}),
        lambda store: store.begin_run("missing"),
        lambda store: store.add_upload("missing", "fixture.txt", b"Rejected"),
        lambda store: store.save_ui_command("missing", "run", {"command_id": "command"}),
        lambda store: store.artifact("missing", "run", "artifacts/answer.txt"),
    ],
)
def test_cheap_existence_checks_still_reject_unknown_sessions(store, operation):
    with pytest.raises(KeyError):
        operation(store)
    assert not (store.root / "sessions" / "missing").exists()


def test_message_lookups_preserve_session_run_and_language_boundaries(store):
    sid = store.create_session()["id"]
    other = store.create_session()["id"]
    rid, _ = store.begin_run(sid)
    next_rid, _ = store.begin_run(sid)
    assert store.latest_user_language(sid) is None
    store.emit(sid, None, "message", {"role": "user", "language": "vi"})
    store.emit(sid, None, "message", {"role": "user", "language": "unknown"})
    store.emit(sid, rid, "message", {"role": "assistant", "language": "en", "text": "First"})
    store.emit(other, None, "message", {"role": "user", "language": "en"})
    assert store.latest_user_language(sid) == "vi"
    store.emit(sid, rid, "reasoning_summary", {"language": "en"})
    selected = store.emit(sid, rid, "message", {"role": "assistant", "text": "Latest"})
    store.emit(sid, next_rid, "message", {"role": "assistant", "text": "Other run"})
    assert store.latest_run_answer(sid, rid) == selected
    assert store.latest_run_answer(other, rid) is None


@pytest.mark.parametrize("initial_version", [0, 1])
def test_investigation_compare_and_swap_checks_database_version_at_write(
    tmp_path, monkeypatch, initial_version
):
    first = SessionStore(tmp_path)
    second = SessionStore(tmp_path)
    try:
        sid = first.create_session()["id"]
        if initial_version:
            first.save_investigation(sid, 0, {"question": "Retained question"})
        original_read = first.investigation
        reads = 0

        def raced_read(session_id):
            nonlocal reads
            snapshot = original_read(session_id)
            reads += 1
            if reads == 2:
                # Another connection commits after this writer checked its
                # version but before its SQL write. Its lock is independent.
                second.save_investigation(sid, initial_version, {"summary": "Other writer"})
            return snapshot

        monkeypatch.setattr(first, "investigation", raced_read)
        with pytest.raises(ValueError, match="version changed"):
            first.save_investigation(sid, initial_version, {"summary": "Stale overwrite"})
        saved = second.investigation(sid)
        assert saved["version"] == initial_version + 1
        assert saved["summary"] == "Other writer"
        if initial_version:
            assert saved["question"] == "Retained question"
    finally:
        first.close()
        second.close()


def test_paged_events_replay_every_event_once_across_other_sessions(store):
    sid = store.create_session()["id"]
    other = store.create_session()["id"]
    expected = []
    for index in range(7):
        expected.append(store.emit(sid, None, "message", {"text": str(index)}))
        store.emit(other, None, "message", {"text": "Excluded"})
    replayed = []
    cursor = 0
    while page := store.events(sid, cursor, limit=2):
        assert len(page) <= 2
        replayed.extend(page)
        cursor = page[-1]["id"]
    assert replayed == expected == store.events(sid)
    with pytest.raises(ValueError, match="positive size"):
        store.events(sid, limit=0)


def test_session_snapshot_preserves_complete_public_payload_and_order(store):
    original = store.create_session()
    sid = original["id"]
    other = store.create_session()["id"]
    first_run, _ = store.begin_run(sid, language="vi", action_intent="draft_create")
    second_run, _ = store.begin_run(sid)
    store.begin_run(other)
    with store.db:
        store.db.execute("UPDATE runs SET created='same-time' WHERE session_id=?", (sid,))
    first = store.emit(
        sid,
        first_run,
        "message",
        {
            "role": "assistant",
            "text": 'Tiếng Việt, 😀, newline\nquote" and slash\\',
            "values": [True, None, -0.0, 1.7976931348623157e308, 10**100],
            "nested": {"items": [{"id": "evidence", "verified": False}]},
        },
    )
    store.emit(other, None, "message", {"text": "Private to the other session"})
    second = store.emit(sid, second_run, "message", {"role": "user", "text": "Next"})
    # Imported legacy JSON retains the same duplicate-key and escape semantics.
    with store.db:
        store.db.execute(
            "UPDATE events SET data=? WHERE id=?",
            (r'{"text":"old","text":"\u65b0","emoji":"\ud83d\ude00"}', second["id"]),
        )
    second["data"] = {"text": "新", "emoji": "😀"}
    upload = store.add_upload(sid, "input.csv", b"value\n1\n")
    store.add_upload(other, "private.csv", b"private\n2\n")
    store.queue_instruction(sid, "Pending")
    store.queue_instruction(other, "Excluded")

    payload = store.session(sid)
    assert payload == {
        "id": sid,
        "created": original["created"],
        "events": [first, second],
        "uploads": [upload],
        "runs": store.runs(sid),
        "pending_instructions": 1,
    }
    assert [run["id"] for run in payload["runs"]] == [first_run, second_run]
    assert "path" not in payload["uploads"][0]


def test_session_snapshot_is_coherent_and_does_not_block_durable_writer(store, monkeypatch):
    sid = store.create_session()["id"]
    rid, _ = store.begin_run(sid)
    store.emit(sid, rid, "message", {"text": "Before"})
    store.add_upload(sid, "before.csv", b"before")
    store.queue_instruction(sid, "Before")
    expected = store.session(sid)
    entered = threading.Event()
    release = threading.Event()
    readers = []
    connect = sqlite3.connect

    class PausedReader(sqlite3.Connection):
        closed = False

        def execute(self, statement, parameters=()):
            cursor = super().execute(statement, parameters)
            if statement.startswith("SELECT id,created FROM sessions"):
                entered.set()
                assert release.wait(5), "Reader was not released"
            return cursor

        def close(self):
            self.closed = True
            super().close()

    def open_reader(database, **kwargs):
        assert database.endswith("?mode=ro") and kwargs["uri"]
        connection = connect(database, **kwargs, factory=PausedReader)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM sessions WHERE 0")
        connection.rollback()
        readers.append(connection)
        return connection

    def commit_during_read():
        # All tables change in one commit while the snapshot's first read is paused.
        with store._lock, store.db:
            store.db.execute("UPDATE runs SET state='completed' WHERE id=?", (rid,))
            store.db.execute("UPDATE steering SET state='completed' WHERE session_id=?", (sid,))
            store.db.execute(
                "INSERT INTO events(session_id,run_id,type,time,data) VALUES (?,?,?,?,?)",
                (sid, rid, "message", "after", json.dumps({"text": "After"})),
            )
            store.db.execute(
                "INSERT INTO uploads VALUES (?,?,?,?,?,?)",
                ("later-upload", sid, "after.csv", "private-path", "synthetic-hash", 5),
            )

    monkeypatch.setattr(sqlite3, "connect", open_reader)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(store.session, sid)
        try:
            assert entered.wait(5)
            # This must complete before the reader releases its snapshot.
            pool.submit(commit_during_read).result(timeout=5)
        finally:
            release.set()
        assert reader.result(timeout=5) == expected
    assert all(reader.closed for reader in readers)
    current = store.session(sid)
    assert len(current["events"]) == 2
    assert len(current["uploads"]) == 2
    assert current["runs"][0]["state"] == "completed"
    assert current["pending_instructions"] == 0
    assert all(reader.closed for reader in readers)


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"nested":[1e999]}',
        '{"nested":[-1e999]}',
    ],
)
def test_corrupt_event_fails_closed_and_read_connection_is_released(store, monkeypatch, raw):
    sid = store.create_session()["id"]
    event = store.emit(sid, None, "message", {"text": "Valid"})
    readers = []
    connect = sqlite3.connect

    class TrackedReader(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def open_reader(database, **kwargs):
        connection = connect(database, **kwargs, factory=TrackedReader)
        readers.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", open_reader)
    with store.db:
        store.db.execute("UPDATE events SET data=? WHERE id=?", (raw, event["id"]))
    with pytest.raises(ValueError):
        store.session(sid)
    with pytest.raises(ValueError):
        store.events(sid)
    assert len(readers) == 1 and readers[0].closed
    with store.db:
        store.db.execute(
            "UPDATE events SET data=? WHERE id=?", (json.dumps(event["data"]), event["id"])
        )
    assert store.session(sid)["events"] == [event]
    with pytest.raises(KeyError):
        store.session("missing")
    assert all(reader.closed for reader in readers)


def test_closed_store_cannot_reopen_a_session_snapshot(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create_session()["id"]
    store.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        store.session(sid)
