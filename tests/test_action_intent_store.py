"""Durable action authorization stays bound to its run and queued scope."""

import itertools
import sqlite3

import pytest
from analyst.store import ACTION_INTENTS, SessionStore

A, B = "a" * 64, "b" * 64
SCOPES = list(itertools.product((None, A, B), ("en", "vi"), ACTION_INTENTS))


@pytest.fixture
def store(tmp_path):
    value = SessionStore(tmp_path)
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("action_intent", ACTION_INTENTS)
def test_run_and_queued_intent_survive_reopen(tmp_path, action_intent):
    store = SessionStore(tmp_path)
    sid = store.create_session()["id"]
    rid, _ = store.begin_run(sid, A, "vi", action_intent)
    store.queue_instruction(sid, "Pending instruction", A, "vi", action_intent)
    store.close()

    restored = SessionStore(tmp_path)
    try:
        run = restored.session(sid)["runs"][0]
        assert (run["id"], run["analysis_context_id"], run["language"], run["action_intent"]) == (
            rid,
            A,
            "vi",
            action_intent,
        )
        assert restored.next_queued_run(sid) == {
            "analysis_context_id": A,
            "language": "vi",
            "action_intent": action_intent,
        }
        assert restored.next_queued_context(sid) == {"analysis_context_id": A}
        assert restored.take_instructions(
            sid, rid, analysis_context_id=A, language="vi", action_intent=action_intent
        ) == ["Pending instruction"]
        restored.finish_run(rid, "completed")
        assert restored.next_queued_run(sid) is None
        assert restored.session(sid)["pending_instructions"] == 0
    finally:
        restored.close()


@pytest.mark.parametrize("had_scope_columns", [False, True])
def test_additive_migration_preserves_legacy_rows_and_defaults_to_no_action(
    tmp_path, had_scope_columns
):
    columns = (
        ", analysis_context_id TEXT, language TEXT NOT NULL DEFAULT 'en'"
        if had_scope_columns
        else ""
    )
    with sqlite3.connect(tmp_path / "workspace.sqlite3") as legacy:
        legacy.executescript(f"""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, created TEXT NOT NULL, history TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, state TEXT NOT NULL,
                created TEXT NOT NULL, finished TEXT, result TEXT{columns}
            );
            CREATE TABLE steering (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', run_id TEXT{columns}
            );
            INSERT INTO sessions VALUES ('legacy-session', 'original-session-time', '[{{"role":"user","content":"Saved history"}}]');
            INSERT INTO runs (id,session_id,state,created,finished,result)
                VALUES ('legacy-run','legacy-session','interrupted','original-run-time','original-finish-time','{{"saved":true}}');
            INSERT INTO steering (session_id,text,state,run_id)
                VALUES ('legacy-session','Applied legacy instruction','applied','legacy-run');
            INSERT INTO steering (session_id,text) VALUES ('legacy-session','Queued legacy instruction');
        """)
        if had_scope_columns:
            legacy.execute("UPDATE runs SET analysis_context_id=?,language='vi'", (A,))
            legacy.execute("UPDATE steering SET analysis_context_id=?,language='vi'", (A,))

    context_id, language = (A, "vi") if had_scope_columns else (None, "en")
    store = SessionStore(tmp_path)
    try:
        snapshot = store.session("legacy-session")
        assert snapshot["created"] == "original-session-time"
        assert store.history("legacy-session") == [{"role": "user", "content": "Saved history"}]
        assert snapshot["runs"] == [
            {
                "id": "legacy-run",
                "state": "interrupted",
                "created": "original-run-time",
                "finished": "original-finish-time",
                "analysis_context_id": context_id,
                "language": language,
                "action_intent": "none",
            }
        ]
        assert (
            store.db.execute("SELECT result FROM runs WHERE id='legacy-run'").fetchone()[0]
            == '{"saved":true}'
        )
        assert snapshot["pending_instructions"] == 2
        assert [
            tuple(row)
            for row in store.db.execute(
                "SELECT text,state,run_id,action_intent FROM steering ORDER BY id"
            )
        ] == [
            ("Applied legacy instruction", "applied", "legacy-run", "none"),
            ("Queued legacy instruction", "queued", None, "none"),
        ]
        assert store.next_queued_run("legacy-session") == {
            "analysis_context_id": context_id,
            "language": language,
            "action_intent": "none",
        }
        rid, _ = store.begin_run("legacy-session", context_id, language)
        assert store.take_instructions(
            "legacy-session",
            rid,
            include_unresolved=True,
            analysis_context_id=context_id,
            language=language,
        ) == ["Applied legacy instruction", "Queued legacy instruction"]
        store.finish_run(rid, "completed")
        authorized, _ = store.begin_run("legacy-session", action_intent="draft_update")
    finally:
        store.close()

    restored = SessionStore(tmp_path)
    try:
        assert restored.session("legacy-session")["pending_instructions"] == 0
        assert (
            next(
                run for run in restored.session("legacy-session")["runs"] if run["id"] == authorized
            )["action_intent"]
            == "draft_update"
        )
        for table in ("runs", "steering"):
            assert [row[1] for row in restored.db.execute(f"PRAGMA table_info({table})")].count(
                "action_intent"
            ) == 1
    finally:
        restored.close()


@pytest.mark.parametrize(
    "action_intent", [None, "", "create", "DRAFT_CREATE", "draft_create ", 1, True, [], {}]
)
def test_invalid_intent_is_rejected_before_any_store_or_filesystem_write(store, action_intent):
    sid = store.create_session()["id"]
    rid, directory = store.begin_run(sid)
    store.queue_instruction(sid, "Keep this queued")
    snapshot = store.session(sid)
    changes = store.db.total_changes
    directories = list(directory.parent.iterdir())

    with pytest.raises(ValueError, match="action_intent"):
        store.begin_run(sid, action_intent=action_intent)
    with pytest.raises(ValueError, match="action_intent"):
        store.queue_instruction(sid, "Unaccepted", action_intent=action_intent)
    with pytest.raises(ValueError, match="action_intent"):
        store.take_instructions(sid, rid, include_unresolved=True, action_intent=action_intent)

    assert store.db.total_changes == changes
    assert store.session(sid) == snapshot
    assert list(directory.parent.iterdir()) == directories
    assert store.take_instructions(sid, rid) == ["Keep this queued"]


@pytest.mark.parametrize("context_id,language,action_intent", SCOPES)
def test_queued_instructions_match_only_the_requested_session_scope_language_and_intent(
    store, context_id, language, action_intent
):
    sid = store.create_session()["id"]
    other = store.create_session()["id"]
    for index, (scope, locale, intent) in enumerate(SCOPES):
        store.queue_instruction(sid, f"Instruction {index}", scope, locale, intent)
    store.queue_instruction(other, "Other session", context_id, language, action_intent)
    store.queue_instruction(sid, "Second matching instruction", context_id, language, action_intent)
    rid, _ = store.begin_run(sid, context_id, language, action_intent)

    selected = SCOPES.index((context_id, language, action_intent))
    assert store.take_instructions(
        sid, rid, analysis_context_id=context_id, language=language, action_intent=action_intent
    ) == [f"Instruction {selected}", "Second matching instruction"]
    assert (
        store.take_instructions(
            sid, rid, analysis_context_id=context_id, language=language, action_intent=action_intent
        )
        == []
    )
    applied = [
        tuple(row)
        for row in store.db.execute(
            "SELECT text,run_id FROM steering WHERE state='applied' ORDER BY id"
        )
    ]
    assert applied == [(f"Instruction {selected}", rid), ("Second matching instruction", rid)]
    store.finish_run(rid, "completed")
    assert store.session(sid)["pending_instructions"] == len(SCOPES) - 1
    assert store.session(other)["pending_instructions"] == 1
    first_remaining = next(
        scope for scope in SCOPES if scope != (context_id, language, action_intent)
    )
    assert store.next_queued_run(sid) == dict(
        zip(("analysis_context_id", "language", "action_intent"), first_remaining, strict=True)
    )


def test_unresolved_action_instructions_remain_isolated_after_restart(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create_session()["id"]
    for intent in ACTION_INTENTS:
        rid, _ = store.begin_run(sid, A, "vi", intent)
        store.queue_instruction(sid, f"Applied {intent}", A, "vi", intent)
        assert store.take_instructions(
            sid, rid, analysis_context_id=A, language="vi", action_intent=intent
        ) == [f"Applied {intent}"]
        store.queue_instruction(sid, f"Queued {intent}", A, "vi", intent)
    store.close()

    restored = SessionStore(tmp_path)
    try:
        assert len(restored.recover_interrupted()) == len(ACTION_INTENTS)
        resumed, _ = restored.begin_run(sid, A, "vi")
        assert restored.take_instructions(
            sid, resumed, include_unresolved=True, analysis_context_id=A, language="vi"
        ) == ["Applied none", "Queued none"]
        restored.finish_run(resumed, "completed")
        assert restored.session(sid)["pending_instructions"] == 4
        assert restored.next_queued_run(sid) == {
            "analysis_context_id": A,
            "language": "vi",
            "action_intent": "draft_create",
        }
        for intent in ("draft_create", "draft_update"):
            resumed, _ = restored.begin_run(sid, A, "vi", intent)
            assert restored.take_instructions(
                sid,
                resumed,
                include_unresolved=True,
                analysis_context_id=A,
                language="vi",
                action_intent=intent,
            ) == [f"Applied {intent}", f"Queued {intent}"]
            restored.finish_run(resumed, "completed")
        assert restored.session(sid)["pending_instructions"] == 0
    finally:
        restored.close()


@pytest.mark.parametrize("table", ["runs", "steering"])
def test_database_constraint_rejects_invalid_persisted_intent(store, table):
    sid = store.create_session()["id"]
    store.begin_run(sid)
    store.queue_instruction(sid, "Read only")
    for value in ("invalid", None):
        with pytest.raises(sqlite3.IntegrityError), store.db:
            store.db.execute(f"UPDATE {table} SET action_intent=?", (value,))
    assert store.session(sid)["runs"][0]["action_intent"] == "none"
    assert store.next_queued_run(sid)["action_intent"] == "none"
