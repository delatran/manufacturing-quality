"""Durable session events, inputs and run state for the local workspace."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .localization import normalize_language, translate_text

ACTION_INTENTS = ("none", "draft_create", "draft_update")
_RUNS_QUERY = (
    "SELECT id,state,created,finished,analysis_context_id,language,action_intent "
    "FROM runs WHERE session_id=? ORDER BY created"
)
_EVENTS_QUERY = "SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id"
_UPLOADS_QUERY = "SELECT * FROM uploads WHERE session_id=? ORDER BY rowid"
_ADMITTED_INSTRUCTION = (
    "(steering.client_request_id IS NULL OR EXISTS ("
    "SELECT 1 FROM prompt_requests WHERE prompt_requests.session_id=steering.session_id "
    "AND prompt_requests.client_request_id=steering.client_request_id "
    "AND prompt_requests.state='accepted'))"
)


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Stored event contains a non-finite number")
    return number


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Stored event contains an invalid JSON constant: {value}")


_EVENT_DECODER = json.JSONDecoder(
    parse_float=_finite_json_float, parse_constant=_reject_json_constant
)


def _event_data(value: str) -> Any:
    # The writer rejects non-finite values. Do not silently turn corrupt
    # persisted evidence into null when a response serializer encounters it.
    return _EVENT_DECODER.decode(value)


def normalize_action_intent(value: str = "none") -> str:
    if not isinstance(value, str) or value not in ACTION_INTENTS:
        raise ValueError("action_intent must be 'none', 'draft_create', or 'draft_update'")
    return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class PromptRequestConflict(ValueError):
    """A caller reused a durable request identity for different input."""


def validate_client_request_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value
    ):
        raise ValueError("client_request_id must be a canonical lowercase UUID")
    return value


class SessionStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self.db = sqlite3.connect(self.root / "workspace.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, created TEXT NOT NULL, history TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, state TEXT NOT NULL,
                created TEXT NOT NULL, finished TEXT, result TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                run_id TEXT, type TEXT NOT NULL, time TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
            CREATE INDEX IF NOT EXISTS events_messages ON events(session_id, id)
                WHERE type='message';
            CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id, created);
            CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, name TEXT NOT NULL,
                path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS steering (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', run_id TEXT
            );
            CREATE TABLE IF NOT EXISTS budget_reservations (
                run_id TEXT PRIMARY KEY, amount REAL NOT NULL, settled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS costs (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, amount REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_stops (
                session_id TEXT PRIMARY KEY, reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS investigations (
                session_id TEXT PRIMARY KEY, version INTEGER NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ui_commands (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
                created TEXT NOT NULL, payload TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS steering_session ON steering(session_id, state, id);
            CREATE INDEX IF NOT EXISTS costs_run ON costs(run_id);
            CREATE TABLE IF NOT EXISTS prompt_requests (
                session_id TEXT NOT NULL, client_request_id TEXT NOT NULL,
                request TEXT NOT NULL, receipt TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('reserved','accepted','rejected','uncertain')),
                effects_started INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id, client_request_id)
            );
        """)
        # Additive migrations preserve existing local conversations.
        reservation_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(budget_reservations)")
        }
        if "unbounded" not in reservation_columns:
            self.db.execute(
                "ALTER TABLE budget_reservations ADD COLUMN unbounded INTEGER NOT NULL DEFAULT 0"
            )
        for table in ("runs", "steering"):
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if "analysis_context_id" not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN analysis_context_id TEXT")
            if "language" not in columns:
                self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN language TEXT NOT NULL DEFAULT 'en'"
                )
            if "action_intent" not in columns:
                self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN action_intent TEXT NOT NULL DEFAULT 'none' "
                    "CHECK(action_intent IN ('none','draft_create','draft_update'))"
                )
        steering_columns = {row[1] for row in self.db.execute("PRAGMA table_info(steering)")}
        if "target_run_id" not in steering_columns:
            self.db.execute("ALTER TABLE steering ADD COLUMN target_run_id TEXT")
            # Earlier same-run submissions are identified by their durable
            # receipt, without changing the resume contract of legacy rows.
            self.db.execute(
                "UPDATE steering SET target_run_id=run_id "
                "WHERE run_id IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM events WHERE events.session_id=steering.session_id "
                "AND events.run_id=steering.run_id AND events.type='steering_received' "
                "AND json_extract(events.data,'$.instruction_id')="
                "'steering-' || steering.run_id || '-' || steering.id)"
            )
        if "reply_to" not in steering_columns:
            self.db.execute("ALTER TABLE steering ADD COLUMN reply_to TEXT")
        if "client_request_id" not in steering_columns:
            self.db.execute("ALTER TABLE steering ADD COLUMN client_request_id TEXT")
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()
            self._closed = True

    def _checkpoint(self) -> None:
        """CloudSessionStore synchronously snapshots admission before paid work."""

    def create_session(self) -> dict[str, Any]:
        sid = uuid.uuid4().hex
        with self._lock, self.db:
            self.db.execute("INSERT INTO sessions(id,created) VALUES (?,?)", (sid, utc_now()))
        return self.session(sid)

    def require_session(self, sid: str) -> None:
        """Validate ownership without loading a conversation's growing event history."""
        with self._lock:
            found = self.db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        if found is None:
            raise KeyError(sid)

    def reserve_prompt_request(
        self, sid: str, client_request_id: str, request: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        """Claim once across connections before any admission side effect."""
        self.require_session(sid)
        validate_client_request_id(client_request_id)
        encoded = json.dumps(
            request, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        receipt = {
            "client_request_id": client_request_id,
            "session_id": sid,
            "status": "reserved",
            "run_id": None,
            "action": "steer" if request.get("steer_run_id") else "start",
            "request": request,
            "request_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "created_at": utc_now(),
            "replayed": False,
        }
        # A unique database key arbitrates even when another process has its
        # own Python lock. Never replace a winning reservation or its payload.
        with self._lock, self.db:
            inserted = self.db.execute(
                "INSERT INTO prompt_requests(session_id,client_request_id,request,receipt,state) "
                "VALUES (?,?,?,?,'reserved') ON CONFLICT(session_id,client_request_id) DO NOTHING",
                (sid, client_request_id, encoded, json.dumps(receipt, ensure_ascii=False)),
            ).rowcount
            row = self.db.execute(
                "SELECT request,receipt FROM prompt_requests "
                "WHERE session_id=? AND client_request_id=?",
                (sid, client_request_id),
            ).fetchone()
            if row["request"] != encoded:
                raise PromptRequestConflict(
                    "This client_request_id already belongs to different prompt input"
                )
        self._checkpoint()
        return bool(inserted), json.loads(row["receipt"])

    def prompt_request(self, sid: str, client_request_id: str) -> dict[str, Any]:
        self.require_session(sid)
        validate_client_request_id(client_request_id)
        with self._lock:
            row = self.db.execute(
                "SELECT receipt FROM prompt_requests WHERE session_id=? AND client_request_id=?",
                (sid, client_request_id),
            ).fetchone()
        if row is None:
            raise KeyError(client_request_id)
        return json.loads(row["receipt"])

    def mark_prompt_request_effects(self, sid: str, client_request_id: str, **details: Any) -> None:
        """Record uncertainty before writing input or contacting a runtime."""
        with self._lock, self.db:
            receipt = {**self.prompt_request(sid, client_request_id), **details}
            self.db.execute(
                "UPDATE prompt_requests SET effects_started=1,receipt=? "
                "WHERE session_id=? AND client_request_id=? AND state='reserved'",
                (json.dumps(receipt, ensure_ascii=False), sid, client_request_id),
            )
        self._checkpoint()

    def prompt_request_has_effects(self, sid: str, client_request_id: str) -> bool:
        with self._lock:
            row = self.db.execute(
                "SELECT effects_started FROM prompt_requests "
                "WHERE session_id=? AND client_request_id=?",
                (sid, client_request_id),
            ).fetchone()
        if row is None:
            raise KeyError(client_request_id)
        return bool(row["effects_started"])

    def finish_prompt_request(
        self, sid: str, client_request_id: str, status: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        if status not in {"accepted", "rejected", "uncertain"}:
            raise ValueError("Invalid prompt admission result")
        with self._lock, self.db:
            receipt = self.prompt_request(sid, client_request_id)
            if receipt["status"] != "reserved":
                return receipt
            receipt.update(result, status=status)
            self.db.execute(
                "UPDATE prompt_requests SET state=?,receipt=? "
                "WHERE session_id=? AND client_request_id=? AND state='reserved'",
                (status, json.dumps(receipt, ensure_ascii=False), sid, client_request_id),
            )
            receipt = self.prompt_request(sid, client_request_id)
        self._checkpoint()
        return receipt

    def runs(self, sid: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.db.execute(_RUNS_QUERY, (sid,))]

    def session(self, sid: str) -> dict[str, Any]:
        """Read one coherent WAL snapshot without holding the shared writer lock."""
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        uri = (self.root / "workspace.sqlite3").as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as snapshot:
            snapshot.row_factory = sqlite3.Row
            snapshot.execute("BEGIN")
            row = snapshot.execute("SELECT id,created FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise KeyError(sid)
            return {
                "id": sid,
                "created": row["created"],
                # Iterate the cursor instead of retaining a second full list
                # of raw rows alongside the decoded conversation.
                "events": [
                    {**dict(event), "data": _event_data(event["data"])}
                    for event in snapshot.execute(_EVENTS_QUERY, (sid, 0))
                ],
                "uploads": [
                    {key: upload[key] for key in upload.keys() if key not in {"path", "session_id"}}
                    for upload in snapshot.execute(_UPLOADS_QUERY, (sid,))
                ],
                "runs": [dict(run) for run in snapshot.execute(_RUNS_QUERY, (sid,))],
                "pending_instructions": snapshot.execute(
                    "SELECT count(*) FROM steering WHERE session_id=? AND state!='completed'",
                    (sid,),
                ).fetchone()[0],
            }

    def history(self, sid: str) -> list[dict[str, Any]]:
        with self._lock:
            row = self.db.execute("SELECT history FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise KeyError(sid)
        return json.loads(row[0])

    def investigation(self, sid: str) -> dict[str, Any]:
        self.require_session(sid)
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM investigations WHERE session_id=?", (sid,)
            ).fetchone()
        if row:
            payload = json.loads(row["payload"])
            payload.setdefault("content_status", "unverified")
            payload.setdefault("semantic_review", None)
            return {
                "session_id": sid,
                "version": row["version"],
                "updated_at": row["updated"],
                **payload,
            }
        return {
            "session_id": sid,
            "version": 0,
            "updated_at": None,
            "question": "",
            "summary": "",
            "context_id": None,
            "scope": {},
            "dataset_version": None,
            "policy_version": None,
            "content_status": "unverified",
            "semantic_review": None,
            "unit_notes": {},
            "instructions_version": None,
            "tools_version": None,
            "findings": [],
            "hypotheses": [],
            "next_steps": [],
            "priorities": [],
            "artifacts": [],
            "ui": {"context_id": None, "panel": "dashboard", "last_command_id": None},
        }

    def save_investigation(
        self, sid: str, expected_version: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Compare-and-swap work state, independently of serialized model history."""
        with self._lock, self.db:
            return self._save_investigation(sid, expected_version, changes)

    def _save_investigation(
        self, sid: str, expected_version: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Save within the caller's locked transaction."""
        if (
            type(expected_version) is not int
            or expected_version < 0
            or not isinstance(changes, dict)
        ):
            raise ValueError("An investigation requires its current nonnegative version")
        allowed = (set(self.investigation(sid)) | {"unit_notes"}) - {
            "session_id",
            "version",
            "updated_at",
        }
        if set(changes) - allowed:
            raise ValueError("Unsupported investigation fields")
        encoded = json.dumps(changes, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 100_000:
            raise ValueError("Investigation content exceeds its bounded size")
        current = self.investigation(sid)
        if current["version"] != expected_version:
            raise ValueError("Investigation version changed; reload before saving")
        payload = {key: value for key, value in current.items() if key in allowed}
        payload.update(changes)
        updated = utc_now()
        encoded_payload = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        saved = self.db.execute(
            "INSERT INTO investigations(session_id,version,updated,payload) VALUES (?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET version=excluded.version,"
            "updated=excluded.updated,payload=excluded.payload WHERE investigations.version=?",
            (
                sid,
                expected_version + 1,
                updated,
                encoded_payload,
                expected_version,
            ),
        )
        if saved.rowcount != 1:
            raise ValueError("Investigation version changed; reload before saving")
        return {
            "session_id": sid,
            "version": expected_version + 1,
            "updated_at": updated,
            **json.loads(encoded_payload),
        }

    def save_ui_command(self, sid: str, rid: str, command: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self.db:
            return self._save_ui_command(sid, rid, command)

    def _save_ui_command(self, sid: str, rid: str, command: dict[str, Any]) -> dict[str, Any]:
        self.require_session(sid)
        prior = self.db.execute(
            "SELECT payload FROM ui_commands WHERE id=?", (command["command_id"],)
        ).fetchone()
        encoded = json.dumps(command, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if prior and prior["payload"] != encoded:
            raise ValueError("A UI command identity cannot be reused with different parameters")
        self.db.execute(
            "INSERT OR IGNORE INTO ui_commands(id,session_id,run_id,created,payload) VALUES (?,?,?,?,?)",
            (command["command_id"], sid, rid, utc_now(), encoded),
        )
        return command

    def commit_ui_command(
        self,
        sid: str,
        rid: str,
        command: dict[str, Any],
        *,
        expected_version: int,
        undo_command_id: str | None = None,
    ) -> dict[str, Any]:
        """Commit a command, its view state and optional undo marker together."""
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.investigation(sid)
            if current["version"] != expected_version:
                raise ValueError("Investigation version changed; reload before saving")
            if current["ui"].get("context_id") != command["expected_context_id"]:
                raise ValueError("The UI scope changed; this stale command was not applied")
            if undo_command_id is not None:
                original = self.ui_command(sid, undo_command_id)
                if (
                    original["undone"]
                    or original["context_id"] != command["expected_context_id"]
                    or current["ui"].get("last_command_id") != undo_command_id
                ):
                    raise ValueError(
                        "Only the latest command in its unchanged UI context can be undone"
                    )
            self._save_ui_command(sid, rid, command)
            saved = self._save_investigation(
                sid,
                expected_version,
                {
                    "findings": [
                        {**item, "status": "stale"}
                        if item.get("context_id") != command["context_id"]
                        else item
                        for item in current["findings"]
                    ],
                    "ui": {
                        "context_id": command["context_id"],
                        "panel": command["panel"],
                        "last_command_id": command["command_id"],
                    },
                },
            )
            if undo_command_id is not None:
                self.db.execute(
                    "UPDATE ui_commands SET undone=1 WHERE id=? AND session_id=?",
                    (undo_command_id, sid),
                )
            return saved

    def ui_command(self, sid: str, command_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM ui_commands WHERE id=? AND session_id=?", (command_id, sid)
            ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return {**json.loads(row["payload"]), "undone": bool(row["undone"])}

    def mark_command_undone(self, sid: str, command_id: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE ui_commands SET undone=1 WHERE id=? AND session_id=?", (command_id, sid)
            )

    def save_history(self, sid: str, items: list) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE sessions SET history=? WHERE id=?", (json.dumps(items), sid))

    def emit(self, sid: str, run_id: str | None, kind: str, data: dict) -> dict:
        timestamp = utc_now()
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, default=str)
        with self._lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO events(session_id,run_id,type,time,data) VALUES (?,?,?,?,?)",
                (sid, run_id, kind, timestamp, encoded),
            )
            event_id = cursor.lastrowid
        return {
            "id": event_id,
            "session_id": sid,
            "run_id": run_id,
            "type": kind,
            "time": timestamp,
            "data": json.loads(encoded),
        }

    def events(self, sid: str, after: int = 0, *, limit: int | None = None) -> list[dict]:
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("An event page requires a positive size")
        parameters = (sid, after) if limit is None else (sid, after, limit)
        with self._lock:
            rows = self.db.execute(
                _EVENTS_QUERY if limit is None else _EVENTS_QUERY + " LIMIT ?", parameters
            ).fetchall()
        return [{**dict(row), "data": _event_data(row["data"])} for row in rows]

    def latest_user_language(self, sid: str) -> str | None:
        with self._lock:
            row = self.db.execute(
                "SELECT json_extract(data,'$.language') FROM events "
                "WHERE session_id=? AND type='message' AND json_extract(data,'$.role')='user' "
                "AND json_extract(data,'$.language') IN ('en','vi') ORDER BY id DESC LIMIT 1",
                (sid,),
            ).fetchone()
        return row[0] if row else None

    def latest_run_answer(self, sid: str, rid: str) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM events WHERE session_id=? AND run_id=? AND type='message' "
                "AND json_extract(data,'$.role')='assistant' ORDER BY id DESC LIMIT 1",
                (sid, rid),
            ).fetchone()
        return {**dict(row), "data": _event_data(row["data"])} if row else None

    def begin_run(
        self,
        sid: str,
        analysis_context_id: str | None = None,
        language: str = "en",
        action_intent: str = "none",
    ) -> tuple[str, Path]:
        language = normalize_language(language)
        action_intent = normalize_action_intent(action_intent)
        self.require_session(sid)
        rid = uuid.uuid4().hex
        directory = self.root / "sessions" / sid / "runs" / rid
        directory.mkdir(parents=True)
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO runs(id,session_id,state,created,analysis_context_id,language,action_intent) VALUES (?,?,?,?,?,?,?)",
                (rid, sid, "running", utc_now(), analysis_context_id, language, action_intent),
            )
        return rid, directory

    def run_language(self, rid: str) -> str:
        with self._lock:
            row = self.db.execute("SELECT language FROM runs WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise KeyError(rid)
        return row["language"]

    def finish_run(self, rid: str, state: str, result: dict | None = None) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE runs SET state=?,finished=?,result=? WHERE id=?",
                (state, utc_now(), json.dumps(result, default=str), rid),
            )
            if state == "completed":
                self.db.execute(
                    "UPDATE steering SET state='completed' WHERE run_id=? AND state='applied'",
                    (rid,),
                )

    def queue_instruction(
        self,
        sid: str,
        text: str,
        analysis_context_id: str | None = None,
        language: str = "en",
        action_intent: str = "none",
        reply_to: dict[str, Any] | None = None,
        *,
        client_request_id: str | None = None,
    ) -> None:
        language = normalize_language(language)
        action_intent = normalize_action_intent(action_intent)
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO steering(session_id,text,analysis_context_id,language,action_intent,reply_to,client_request_id) VALUES (?,?,?,?,?,?,?)",
                (
                    sid,
                    text,
                    analysis_context_id,
                    language,
                    action_intent,
                    json.dumps(reply_to, sort_keys=True) if reply_to is not None else None,
                    client_request_id,
                ),
            )

    def take_instructions(
        self,
        sid: str,
        rid: str,
        *,
        include_unresolved: bool = False,
        analysis_context_id: str | None = None,
        language: str = "en",
        action_intent: str = "none",
        reply_to: dict[str, Any] | None = None,
        queued_instruction_id: int | None = None,
    ) -> list[str]:
        language = normalize_language(language)
        action_intent = normalize_action_intent(action_intent)
        condition = (
            "state!='completed'"
            if include_unresolved
            else "(state='queued' OR (state='pending' AND run_id=?))"
        )
        parameters = (sid,) if include_unresolved else (sid, rid)
        source = json.dumps(reply_to, sort_keys=True) if reply_to is not None else None
        selection = " AND id=?" if queued_instruction_id is not None else ""
        with self._lock, self.db:
            rows = self.db.execute(
                f"SELECT id,text FROM steering WHERE session_id=? AND {condition} "
                f"AND {_ADMITTED_INSTRUCTION} "
                "AND (target_run_id IS NULL OR target_run_id=?) "
                "AND analysis_context_id IS ? AND language=? AND action_intent=? "
                f"AND reply_to IS ?{selection} ORDER BY id",
                (
                    *parameters,
                    rid,
                    analysis_context_id,
                    language,
                    action_intent,
                    source,
                    *((queued_instruction_id,) if queued_instruction_id is not None else ()),
                ),
            ).fetchall()
            self.db.executemany(
                "UPDATE steering SET state='applied',run_id=? WHERE id=?",
                [(rid, row["id"]) for row in rows],
            )
        return [row["text"] for row in rows]

    def record_run_instruction(
        self,
        sid: str,
        rid: str,
        text: str,
        *,
        applied: bool,
        client_request_id: str | None = None,
    ) -> int:
        """Persist same-run input without adding a future-run queue entry."""
        with self._lock, self.db:
            run = self.db.execute(
                "SELECT analysis_context_id,language,action_intent FROM runs "
                "WHERE id=? AND session_id=? AND state='running'",
                (rid, sid),
            ).fetchone()
            if run is None:
                raise ValueError("This analysis has finished; send a new question instead")
            cursor = self.db.execute(
                "INSERT INTO steering(session_id,text,state,run_id,target_run_id,analysis_context_id,language,action_intent,client_request_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    text,
                    "applied" if applied else "pending",
                    rid,
                    rid,
                    run["analysis_context_id"],
                    run["language"],
                    run["action_intent"],
                    client_request_id,
                ),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def mark_instruction_unconfirmed(self, instruction_id: int, rid: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE steering SET state='unconfirmed' WHERE id=? AND run_id=?",
                (instruction_id, rid),
            )

    def next_queued_context(self, sid: str) -> dict | None:
        queued = self.next_queued_run(sid)
        return (
            {"analysis_context_id": queued["analysis_context_id"]} if queued is not None else None
        )

    def next_queued_run(self, sid: str) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT id,analysis_context_id,language,action_intent,reply_to FROM steering "
                f"WHERE session_id=? AND state='queued' AND {_ADMITTED_INSTRUCTION} ORDER BY id LIMIT 1",
                (sid,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        identity = result.pop("id")
        reference = result.pop("reply_to")
        if reference is not None:
            result.update(reply_to=json.loads(reference), queued_instruction_id=identity)
        return result

    def has_queued_instruction(self, sid: str) -> bool:
        with self._lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM steering WHERE session_id=? AND state='queued' "
                    f"AND {_ADMITTED_INSTRUCTION} LIMIT 1",
                    (sid,),
                ).fetchone()
                is not None
            )

    def _committed_cost(self) -> float:
        rows = self.db.execute("""SELECT r.amount,r.settled,COALESCE(SUM(c.amount),0) AS spent
            FROM budget_reservations r LEFT JOIN costs c ON c.run_id=r.run_id GROUP BY r.run_id""").fetchall()
        return sum(
            row["spent"] if row["settled"] else max(row["amount"], row["spent"]) for row in rows
        )

    def remaining_budget(self, limit: float | None) -> float | None:
        if limit is None:
            return None
        with self._lock:
            return max(0.0, limit - self._committed_cost())

    def reserve_budget(
        self, rid: str, run_limit: float | None, dev_limit: float | None
    ) -> float | None:
        for value in (run_limit, dev_limit):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("Budget limits must be nonnegative finite values or None")
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if run_limit is None and dev_limit is None:
                self.db.execute(
                    "INSERT INTO budget_reservations(run_id,amount,unbounded) VALUES (?,0,1)",
                    (rid,),
                )
                return None
            remaining = (
                max(0.0, dev_limit - self._committed_cost()) if dev_limit is not None else None
            )
            amount = min(value for value in (run_limit, remaining) if value is not None)
            if amount <= 0:
                raise ValueError("DEV_API_BUDGET_USD has no unreserved balance")
            self.db.execute(
                "INSERT INTO budget_reservations(run_id,amount) VALUES (?,?)", (rid, amount)
            )
        return amount

    def record_cost(self, rid: str, identity: str, amount: float) -> None:
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Cost must be finite and nonnegative")
        with self._lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO costs(id,run_id,amount) VALUES (?,?,?)",
                (identity, rid, amount),
            )

    def settle_budget(self, rid: str) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE budget_reservations SET settled=1 WHERE run_id=?", (rid,))

    def stop_session(self, sid: str, reason: str) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO session_stops VALUES (?,?)", (sid, reason))

    def stopped(self, sid: str) -> str | None:
        with self._lock:
            row = self.db.execute(
                "SELECT reason FROM session_stops WHERE session_id=?", (sid,)
            ).fetchone()
        return row[0] if row else None

    def recover_interrupted(self) -> list[dict]:
        with self._lock:
            pending = self.db.execute(
                "SELECT session_id,client_request_id FROM prompt_requests WHERE state='reserved'"
            ).fetchall()
            rows = self.db.execute(
                "SELECT id,session_id,language FROM runs WHERE state='running'"
            ).fetchall()
        for request in pending:
            self.finish_prompt_request(
                request["session_id"],
                request["client_request_id"],
                "uncertain",
                {
                    "code": "admission_interrupted",
                    "message": "The app restarted before admission was confirmed. This request will not be replayed automatically.",
                },
            )
        events = []
        for row in rows:
            self.finish_run(row["id"], "interrupted")
            events.append(
                self.emit(
                    row["session_id"],
                    row["id"],
                    "status",
                    {
                        "state": "interrupted",
                        "language": row["language"],
                        "label": translate_text(
                            "The app restarted before this run finished. Send a prompt to continue; saved evidence is retained.",
                            row["language"],
                        ),
                    },
                )
            )
        return events

    def add_upload(self, sid: str, name: str, content: bytes) -> dict:
        self.require_session(sid)
        file_id = uuid.uuid4().hex
        clean_name = Path(name.replace("\\", "/")).name
        if not clean_name or clean_name in {".", ".."}:
            raise ValueError("A file name is required")
        folder = self.root / "sessions" / sid / "inputs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (file_id + Path(clean_name).suffix.lower())
        path.write_bytes(content)
        record = {
            "id": file_id,
            "session_id": sid,
            "name": clean_name,
            "path": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO uploads VALUES (:id,:session_id,:name,:path,:sha256,:size)", record
            )
        return {k: v for k, v in record.items() if k not in {"path", "session_id"}}

    def uploads(self, sid: str, *, internal: bool = False) -> list[dict]:
        with self._lock:
            rows = self.db.execute(_UPLOADS_QUERY, (sid,)).fetchall()
        return [
            {k: row[k] for k in row.keys() if internal or k not in {"path", "session_id"}}
            for row in rows
        ]

    def artifact(self, sid: str, run_id: str, filename: str) -> Path:
        self.require_session(sid)
        with self._lock:
            row = self.db.execute(
                "SELECT id FROM runs WHERE id=? AND session_id=?", (run_id, sid)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        root = (self.root / "sessions" / sid / "runs" / run_id).resolve()
        from .evidence import EvidenceLedger

        try:
            return EvidenceLedger(root).resolve_artifact(filename)
        except ValueError as error:
            raise FileNotFoundError(filename) from error
