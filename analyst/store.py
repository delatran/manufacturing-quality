"""Durable session events, inputs and run state for the local workspace."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .localization import normalize_language, translate_text

ACTION_INTENTS = ("none", "draft_create", "draft_update")


def normalize_action_intent(value: str = "none") -> str:
    if not isinstance(value, str) or value not in ACTION_INTENTS:
        raise ValueError("action_intent must be 'none', 'draft_create', or 'draft_update'")
    return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class SessionStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def create_session(self) -> dict[str, Any]:
        sid = uuid.uuid4().hex
        with self._lock, self.db:
            self.db.execute("INSERT INTO sessions(id,created) VALUES (?,?)", (sid, utc_now()))
        return self.session(sid)

    def session(self, sid: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise KeyError(sid)
            runs = [
                dict(r)
                for r in self.db.execute(
                    "SELECT id,state,created,finished,analysis_context_id,language,action_intent FROM runs WHERE session_id=? ORDER BY created",
                    (sid,),
                )
            ]
        with self._lock:
            pending = self.db.execute(
                "SELECT count(*) FROM steering WHERE session_id=? AND state!='completed'", (sid,)
            ).fetchone()[0]
        return {
            "id": sid,
            "created": row["created"],
            "events": self.events(sid),
            "uploads": self.uploads(sid),
            "runs": runs,
            "pending_instructions": pending,
        }

    def history(self, sid: str) -> list[dict[str, Any]]:
        with self._lock:
            row = self.db.execute("SELECT history FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise KeyError(sid)
        return json.loads(row[0])

    def investigation(self, sid: str) -> dict[str, Any]:
        self.session(sid)
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
        with self._lock, self.db:
            current = self.investigation(sid)
            if current["version"] != expected_version:
                raise ValueError("Investigation version changed; reload before saving")
            payload = {key: value for key, value in current.items() if key in allowed}
            payload.update(changes)
            self.db.execute(
                "INSERT INTO investigations(session_id,version,updated,payload) VALUES (?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET version=excluded.version,updated=excluded.updated,payload=excluded.payload",
                (
                    sid,
                    expected_version + 1,
                    utc_now(),
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                ),
            )
        return self.investigation(sid)

    def save_ui_command(self, sid: str, rid: str, command: dict[str, Any]) -> dict[str, Any]:
        self.session(sid)
        with self._lock, self.db:
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

    def events(self, sid: str, after: int = 0) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id", (sid, after)
            ).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def begin_run(
        self,
        sid: str,
        analysis_context_id: str | None = None,
        language: str = "en",
        action_intent: str = "none",
    ) -> tuple[str, Path]:
        language = normalize_language(language)
        action_intent = normalize_action_intent(action_intent)
        self.session(sid)
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
    ) -> None:
        language = normalize_language(language)
        action_intent = normalize_action_intent(action_intent)
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO steering(session_id,text,analysis_context_id,language,action_intent,reply_to) VALUES (?,?,?,?,?,?)",
                (
                    sid,
                    text,
                    analysis_context_id,
                    language,
                    action_intent,
                    json.dumps(reply_to, sort_keys=True) if reply_to is not None else None,
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
                "INSERT INTO steering(session_id,text,state,run_id,target_run_id,analysis_context_id,language,action_intent) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    sid,
                    text,
                    "applied" if applied else "pending",
                    rid,
                    rid,
                    run["analysis_context_id"],
                    run["language"],
                    run["action_intent"],
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
                "SELECT id,analysis_context_id,language,action_intent,reply_to FROM steering WHERE session_id=? AND state='queued' ORDER BY id LIMIT 1",
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
                    "SELECT 1 FROM steering WHERE session_id=? AND state='queued' LIMIT 1", (sid,)
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
            rows = self.db.execute(
                "SELECT id,session_id,language FROM runs WHERE state='running'"
            ).fetchall()
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
        self.session(sid)
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
            rows = self.db.execute(
                "SELECT * FROM uploads WHERE session_id=? ORDER BY rowid", (sid,)
            ).fetchall()
        return [
            {k: row[k] for k in row.keys() if internal or k not in {"path", "session_id"}}
            for row in rows
        ]

    def artifact(self, sid: str, run_id: str, filename: str) -> Path:
        self.session(sid)
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
