"""Local investigation cases linked to existing immutable analysis receipts.

This module records human work, not plant activity or delivered assignments.
Source checks and follow-up comparisons never alter the analytical receipts.
No background polling, external messages or production write actions run here.
"""

from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .case_study import analytics
from .case_study.analytics import Scope
from .case_study.ingestion import SourceError
from .config import Settings
from .dashboard import AnalysisService, ContextNotFound, EvidenceIntegrityError, SnapshotUnavailable
from .evidence import canonical, sha256

STATUSES = ("draft", "acknowledged", "investigating", "action_planned", "resolved", "dismissed")
TRANSITIONS = {
    "draft": ("acknowledged", "dismissed"),
    "acknowledged": ("investigating", "dismissed"),
    "investigating": ("action_planned", "dismissed"),
    "action_planned": ("investigating", "resolved", "dismissed"),
    "resolved": ("investigating",),
    "dismissed": ("investigating",),
}
CATEGORIES = ("investigation", "corrective_action", "data_quality")
PRIORITIES = ("low", "medium", "high")
EDITABLE_FIELDS = frozenset(
    {
        "title",
        "description",
        "category",
        "priority",
        "proposed_owner",
        "assigned_owner",
        "due_date",
        "action_plan",
    }
)
NARRATIVE_FIELDS = frozenset({"title", "description", "proposed_owner", "action_plan"})
CONTENT_EVIDENCE_FIELDS = frozenset(
    {
        "validation_version",
        "context_id",
        "run_id",
        "annotated_text",
        "annotated_sha256",
        "content_sha256",
        "validator_code_sha256",
        "claims",
        "receipts",
    }
)
_CITATION = re.compile(r"\[claim:((?:[^\[\]]|\[\d+\])+)\]")
_EVIDENCE_TOKEN = re.compile(r"\[(?:claim|value)(?::|\])", re.IGNORECASE)
METRIC_KEYS = (
    "event_count",
    "critical_count",
    "known_repair_cost",
    "cost_unknown_count",
    "reference_output",
    "reference_intensity_per_1000",
)
CONTEXT_ID = re.compile(r"[a-f0-9]{64}\Z")
CASE_ID = re.compile(r"[a-f0-9]{32}\Z")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_.:-]{8,128}\Z")
COMPARISON_LIMITATIONS = [
    "This source contains the January to June 2024 historical case. A successful source check does not establish current plant conditions.",
    "Differences describe recorded events and known costs, not a causal effect of a corrective action or a verified defect rate.",
    "Actual produced and inspected units, operating days and reporting completeness have not been supplied. Missing records do not prove that production stopped or reporting failed.",
    "Ownership and outcomes are local user records. No assignment, message or acknowledgement is delivered to another person.",
]


class OperationNotFound(LookupError):
    """The requested local case does not exist."""


class OperationConflict(ValueError):
    """A write would overwrite another change or reuse a request incorrectly."""


class OperationForbidden(ValueError):
    """The caller cannot make this kind of operational change."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: Any, pattern: re.Pattern, field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"Invalid {field}")
    return value


def _text(
    value: Any, field: str, maximum: int, *, nullable: bool = False, required: bool = False
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text" + (" or null" if nullable else ""))
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise ValueError(f"{field} must contain {'1' if required else '0'} to {maximum} characters")
    return value or (None if nullable else "")


def _fields(values: Any, *, partial: bool) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("changes must be an object")
    unknown = set(values) - EDITABLE_FIELDS
    if unknown:
        raise ValueError("Unknown or immutable fields: " + ", ".join(sorted(unknown)))
    result = (
        {}
        if partial
        else {
            "title": "",
            "description": "",
            "category": "investigation",
            "priority": "medium",
            "proposed_owner": None,
            "assigned_owner": None,
            "due_date": None,
            "action_plan": None,
        }
    )
    result.update(values)
    for key, value in result.items():
        if key in {"category", "priority"}:
            if not isinstance(value, str) or value not in (
                CATEGORIES if key == "category" else PRIORITIES
            ):
                raise ValueError(f"Invalid {key}")
        elif key == "due_date":
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError("due_date must be an ISO date (YYYY-MM-DD) or null")
                try:
                    date.fromisoformat(value)
                except ValueError as error:
                    raise ValueError("due_date must be a valid calendar date") from error
        else:
            maximum = 180 if key == "title" else 160 if key.endswith("owner") else 4000
            result[key] = _text(
                value,
                key,
                maximum,
                nullable=key in {"proposed_owner", "assigned_owner", "action_plan"},
                required=key == "title",
            )
    return result


def _version(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected_version must be a positive integer")
    return value


def _actor(actor: str, authorized: bool = False) -> None:
    if actor not in {"user", "agent"}:
        raise OperationForbidden("Unsupported case actor")
    if actor == "agent" and authorized is not True:
        raise OperationForbidden(
            "An explicit user request is required for the Agent to save or edit a draft"
        )


def _content_evidence(
    values: dict[str, Any], context_id: str, actor: str, evidence: Any
) -> dict[str, Any]:
    """Bind runtime-issued proof to exact stored prose, not re-audit its analysis.

    Only the runtime supplies this service argument after numeric receipt QA.
    Public case fields cannot set it. These checks prevent accidental loss or
    substitution between the audited narrative and the transaction that stores it.
    """
    if actor != "agent":
        if evidence is not None:
            raise OperationForbidden("Only the Agent runtime may supply content_evidence")
        return {}
    expected = {field for field in NARRATIVE_FIELDS & values.keys() if values[field] is not None}
    if not isinstance(evidence, dict) or set(evidence) != expected:
        raise ValueError("content_evidence must cover exactly the non-null Agent narrative fields")
    for field, proof in evidence.items():
        if not isinstance(proof, dict) or set(proof) not in (
            CONTENT_EVIDENCE_FIELDS,
            CONTENT_EVIDENCE_FIELDS | {"number_format"},
        ):
            raise ValueError(f"Invalid content_evidence for {field}")
        if proof.get("number_format", "en-US") not in {"en-US", "vi-VN"}:
            raise ValueError(f"Unsupported number format for {field}")
        if proof["validation_version"] != "numeric_claims_v1" or proof["context_id"] != context_id:
            raise ValueError(
                f"content_evidence for {field} has a different context or validation version"
            )
        if not isinstance(proof["run_id"], str) or not proof["run_id"].strip():
            raise ValueError(f"content_evidence for {field} requires a run_id")
        annotated = proof["annotated_text"]
        if not isinstance(annotated, str):
            raise ValueError(f"content_evidence for {field} requires annotated_text")
        for key in ("annotated_sha256", "content_sha256", "validator_code_sha256"):
            _identifier(proof[key], CONTEXT_ID, key)
        if proof["annotated_sha256"] != sha256(annotated) or proof["content_sha256"] != sha256(
            values[field]
        ):
            raise ValueError(f"content_evidence for {field} does not match the exact text hashes")
        # Keep this deterministic cleaner aligned with the runtime's proof builder.
        clean = "\n".join(
            re.sub(r"[^\S\r\n]+", " ", line) for line in _CITATION.sub("", annotated).splitlines()
        ).strip()
        if clean != values[field] or _EVIDENCE_TOKEN.search(clean):
            raise ValueError(f"content_evidence for {field} does not produce clean stored text")
        if not isinstance(proof["claims"], list) or not isinstance(proof["receipts"], list):
            raise ValueError(f"content_evidence for {field} requires claims and receipt lists")
        receipts = {}
        for receipt in proof["receipts"]:
            if not isinstance(receipt, dict) or set(receipt) != {
                "execution_id",
                "entry_sha256",
                "result_sha256",
                "code_sha256",
            }:
                raise ValueError(f"Invalid content_evidence receipt for {field}")
            execution_id = receipt["execution_id"]
            if (
                not isinstance(execution_id, str)
                or not execution_id.strip()
                or execution_id in receipts
            ):
                raise ValueError(f"Invalid or duplicate content_evidence receipt for {field}")
            for key in ("entry_sha256", "result_sha256", "code_sha256"):
                _identifier(receipt[key], CONTEXT_ID, key)
            receipts[execution_id] = receipt
        claims = {}
        for claim in proof["claims"]:
            if not isinstance(claim, dict) or set(claim) != {
                "id",
                "execution_id",
                "path",
                "value",
                "code_sha256",
            }:
                raise ValueError(f"Invalid content_evidence claim for {field}")
            for key in ("id", "execution_id", "path"):
                if not isinstance(claim[key], str) or not claim[key].strip():
                    raise ValueError(f"Invalid content_evidence claim {key} for {field}")
            value = claim["value"]
            try:
                finite = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite or not claim["path"].startswith("$") or claim["id"] in claims:
                raise ValueError(f"Invalid or duplicate content_evidence claim for {field}")
            _identifier(claim["code_sha256"], CONTEXT_ID, "code_sha256")
            receipt = receipts.get(claim["execution_id"])
            if receipt is None or receipt["code_sha256"] != claim["code_sha256"]:
                raise ValueError(f"content_evidence claim for {field} has no matching receipt")
            claims[claim["id"]] = claim
        if set(_CITATION.findall(annotated)) - claims.keys():
            raise ValueError(f"content_evidence for {field} contains an unbound claim marker")
    return copy.deepcopy(evidence)


def _updated_content_evidence(record, changes, actor, supplied, effective):
    """Keep unaffected proofs and audit every replacement or human invalidation."""
    before = record.get("content_evidence", {})
    after = copy.deepcopy(before)
    for field in NARRATIVE_FIELDS & changes.keys():
        if actor == "agent" and changes[field] is not None:
            after[field] = supplied[field]
        elif actor == "agent" or field in effective:
            after.pop(field, None)
    if after != before:
        effective["content_evidence"] = {"before": before, "after": after}
    return after


class OperationsService:
    """Durable local cases with transactional audit and optimistic concurrency."""

    def __init__(self, settings: Settings, analysis_service: AnalysisService):
        self.analysis = analysis_service
        state = Path(settings.state_dir)
        self.state_dir = (state if state.is_absolute() else settings.root / state).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "operations.sqlite3"
        self.code_sha256 = sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n"))
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operation_cases (
                    id TEXT PRIMARY KEY, context_id TEXT NOT NULL, source_key TEXT UNIQUE,
                    status TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS operation_cases_context ON operation_cases(context_id, updated_at);
                CREATE TABLE IF NOT EXISTS operation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
                    at TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
                    from_status TEXT, to_status TEXT, changes TEXT NOT NULL, note TEXT,
                    FOREIGN KEY(case_id) REFERENCES operation_cases(id)
                );
                CREATE INDEX IF NOT EXISTS operation_history_case ON operation_history(case_id, id);
                CREATE TABLE IF NOT EXISTS operation_requests (
                    key TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, case_id TEXT NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES operation_cases(id)
                );
                CREATE TABLE IF NOT EXISTS operation_checks (
                    id TEXT PRIMARY KEY, baseline_context_id TEXT NOT NULL,
                    checked_at TEXT NOT NULL, payload TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def close(self) -> None:
        """Connections are transaction-scoped; this hook matches Workspace shutdown."""

    @staticmethod
    def _row(db: sqlite3.Connection, case_id: str) -> dict[str, Any]:
        _identifier(case_id, CASE_ID, "case id")
        row = db.execute("SELECT payload FROM operation_cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise OperationNotFound("The requested follow-up case does not exist")
        return json.loads(row["payload"])

    @staticmethod
    def _detail(db: sqlite3.Connection, record: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(record)
        result.setdefault("origin_report_id", None)
        result.setdefault("content_evidence", {})
        result["history"] = [
            {**dict(row), "changes": json.loads(row["changes"])}
            for row in db.execute(
                "SELECT id,at,actor,kind,from_status,to_status,changes,note FROM operation_history WHERE case_id=? ORDER BY id",
                (record["id"],),
            )
        ]
        result["notes"] = [
            {
                "id": item["id"],
                "text": item["note"],
                "created_at": item["at"],
                "actor": item["actor"],
            }
            for item in result["history"]
            if item["note"]
        ]
        result["allowed_transitions"] = list(TRANSITIONS[record["status"]])
        result["is_overdue"] = bool(
            record["due_date"]
            and record["due_date"] < datetime.now(UTC).date().isoformat()
            and record["status"] not in {"resolved", "dismissed"}
        )
        return result

    @staticmethod
    def _save(db: sqlite3.Connection, record: dict[str, Any]) -> None:
        db.execute(
            "UPDATE operation_cases SET status=?,version=?,updated_at=?,payload=? WHERE id=?",
            (
                record["status"],
                record["version"],
                record["updated_at"],
                canonical(record),
                record["id"],
            ),
        )

    @staticmethod
    def _audit(
        db: sqlite3.Connection,
        record: dict[str, Any],
        actor: str,
        kind: str,
        before: str | None,
        changes: dict[str, Any],
        note: str | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO operation_history(case_id,at,actor,kind,from_status,to_status,changes,note) VALUES (?,?,?,?,?,?,?,?)",
            (
                record["id"],
                record["updated_at"],
                actor,
                kind,
                before,
                record["status"],
                canonical(changes),
                note,
            ),
        )

    @staticmethod
    def _expect(record: dict[str, Any], expected_version: int) -> None:
        if record["version"] != _version(expected_version):
            raise OperationConflict(
                "This case changed since it was opened. Reload it before saving your change."
            )

    @staticmethod
    def _transition_fields(
        record: dict[str, Any], status: str, note: str | None, outcome: str | None
    ) -> dict[str, Any]:
        before = record["status"]
        if status not in TRANSITIONS[before]:
            raise OperationConflict(f"Cannot move a case from {before} to {status}")
        if status == "action_planned" and not record["action_plan"]:
            raise ValueError("Record an action_plan before marking an action as planned")
        if status == "resolved" and not outcome:
            raise ValueError("Describe the observed outcome before resolving this case")
        if (status == "dismissed" or before in {"resolved", "dismissed"}) and not note:
            raise ValueError("A reason is required to dismiss or reopen a case")
        if outcome is not None and status != "resolved":
            raise ValueError("An outcome is only recorded when resolving a case")
        result: dict[str, Any] = {"status": status}
        if status == "resolved":
            result["outcome"] = outcome
        elif before in {"resolved", "dismissed"}:
            # Preserve previous results in audit, not as a reopened investigation's result.
            result["outcome"] = None
        return result

    @staticmethod
    def _binding(payload: dict[str, Any]) -> dict[str, Any]:
        context, evidence = payload["context"], payload["evidence"]
        return {
            "context_id": context["context_id"],
            "context": copy.deepcopy(context),
            "provenance": {
                key: evidence[key] for key in ("execution_id", "code_sha256", "result_sha256")
            }
            | {key: context[key] for key in ("source_sha256", "master_sha256")},
        }

    def _source(self, payload: dict[str, Any], values: Any) -> dict[str, Any]:
        values = values if values is not None else {"kind": "manual"}
        if not isinstance(values, dict) or set(values) - {"kind", "key"}:
            raise ValueError("source must contain kind and optional key")
        kind, key = values.get("kind", "manual"), values.get("key")
        if kind == "manual":
            if key is not None:
                raise ValueError("A manual case cannot supply a source key")
            return {
                "kind": kind,
                "key": None,
                "summary": "User-created investigation linked to the selected historical context.",
                "receipt_path": None,
            }
        if not isinstance(key, str):
            raise ValueError(
                "A recommendation or historical candidate source requires its string key"
            )
        if kind == "recommendation":
            for index, item in enumerate(payload["analysis"]["recommendations"]):
                if str(item["priority"]) == key:
                    return {
                        "kind": kind,
                        "key": key,
                        "summary": item["evidence"],
                        "receipt_path": f"$.recommendations[{index}]",
                        "facts": copy.deepcopy(item),
                    }
            raise ValueError("The selected recommendation does not belong to this context")
        if kind == "historical_candidate":
            for index, item in enumerate(payload["analysis"]["anomalies"]["candidates"]):
                if item["date"] == key:
                    return {
                        "kind": kind,
                        "key": key,
                        "summary": f"Historical screening candidate on {key}; investigation required, not a confirmed process alarm.",
                        "receipt_path": f"$.anomalies.candidates[{index}]",
                        "facts": copy.deepcopy(item),
                    }
            raise ValueError("The selected historical candidate does not belong to this context")
        if kind == "source_check":
            _identifier(key, CASE_ID, "source check id")
            with self._connect() as db:
                row = db.execute(
                    "SELECT payload FROM operation_checks WHERE id=?", (key,)
                ).fetchone()
            comparison = json.loads(row["payload"]) if row else None
            if comparison is None or not comparison["review_required"]:
                raise ValueError("The source check does not contain an item that requires review")
            bound_context = comparison["current_context_id"] or comparison["baseline_context_id"]
            if bound_context != payload["context"]["context_id"]:
                raise ValueError("The source check does not belong to this context")
            return {
                "kind": kind,
                "key": key,
                "summary": comparison["summary"],
                "receipt_path": None,
                "facts": comparison,
            }
        raise ValueError("Invalid source kind")

    def get_case(self, case_id: str) -> dict[str, Any]:
        with self._connect() as db:
            return self._detail(db, self._row(db, case_id))

    def list_cases(
        self, context_id: str | None = None, status: str | None = None
    ) -> dict[str, Any]:
        where, params = [], []
        if context_id is not None:
            _identifier(context_id, CONTEXT_ID, "context_id")
            where.append("context_id=?")
            params.append(context_id)
        if status is not None:
            if status not in STATUSES:
                raise ValueError("Invalid status")
            where.append("status=?")
            params.append(status)
        query = (
            "SELECT payload FROM operation_cases"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY updated_at DESC,id"
        )
        with self._connect() as db:
            cases = [
                self._detail(db, json.loads(row["payload"]))
                for row in db.execute(query, params).fetchall()
            ]
        return {"cases": cases, "total": len(cases)}

    def _create_verified(
        self,
        payload,
        business,
        source,
        idempotency_key,
        request_hash,
        actor,
        *,
        origin_report_id=None,
        content_evidence=None,
        before_commit=None,
    ):
        context_id = payload["context"]["context_id"]
        source_key = (
            None
            if source["kind"] == "manual"
            else sha256(canonical([context_id, source["kind"], source["key"]]))
        )
        with self._connect(write=True) as db:
            if before_commit is not None:
                before_commit()
            prior = db.execute(
                "SELECT request_sha256,case_id FROM operation_requests WHERE key=?",
                (idempotency_key,),
            ).fetchone()
            if prior:
                if prior["request_sha256"] != request_hash:
                    raise OperationConflict(
                        "This idempotency key was already used with different case details"
                    )
                return self._detail(db, self._row(db, prior["case_id"]))
            existing = (
                db.execute(
                    "SELECT id FROM operation_cases WHERE source_key=?", (source_key,)
                ).fetchone()
                if source_key
                else None
            )
            if existing:
                case_id = existing["id"]
            else:
                case_id, timestamp = uuid.uuid4().hex, _now()
                record = {
                    "id": case_id,
                    "version": 1,
                    **business,
                    **self._binding(payload),
                    "source": copy.deepcopy(source),
                    "status": "draft",
                    "outcome": None,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "resolved_at": None,
                    "follow_up": None,
                    "origin_report_id": origin_report_id,
                    "content_evidence": content_evidence or {},
                }
                db.execute(
                    "INSERT INTO operation_cases(id,context_id,source_key,status,version,created_at,updated_at,payload) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        case_id,
                        context_id,
                        source_key,
                        "draft",
                        1,
                        timestamp,
                        timestamp,
                        canonical(record),
                    ),
                )
                self._audit(
                    db,
                    record,
                    actor,
                    "created",
                    None,
                    {
                        "fields": business,
                        "source": source,
                        "origin_report_id": origin_report_id,
                        "content_evidence": record["content_evidence"],
                    },
                )
            db.execute(
                "INSERT INTO operation_requests(key,request_sha256,case_id) VALUES (?,?,?)",
                (idempotency_key, request_hash, case_id),
            )
            return self._detail(db, self._row(db, case_id))

    async def create_case(
        self,
        values: dict[str, Any],
        *,
        actor: str = "user",
        authorized: bool = False,
        content_evidence: dict[str, Any] | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        _actor(actor, authorized)
        if not isinstance(values, dict):
            raise ValueError("Case details must be an object")
        unknown = (
            set(values)
            - EDITABLE_FIELDS
            - {"context_id", "source", "idempotency_key", "origin_report_id"}
        )
        if unknown:
            raise ValueError("Unknown or immutable fields: " + ", ".join(sorted(unknown)))
        context_id = _identifier(values.get("context_id"), CONTEXT_ID, "context_id")
        key = _identifier(values.get("idempotency_key"), IDEMPOTENCY_KEY, "idempotency_key")
        # A UI navigation reference only. It is not a verified report receipt and
        # cannot replace or modify the case's validated context and provenance.
        origin_report_id = _text(
            values.get("origin_report_id"), "origin_report_id", 160, nullable=True
        )
        business = _fields(
            {key: value for key, value in values.items() if key in EDITABLE_FIELDS}, partial=False
        )
        if actor == "agent" and business["assigned_owner"] is not None:
            raise OperationForbidden(
                "The Agent may propose an owner; a human must record an assignment"
            )
        proof = _content_evidence(business, context_id, actor, content_evidence)
        payload = await self.analysis.context_by_id(context_id)
        source = self._source(payload, values.get("source"))
        # Hash the submitted selector, not a check record's later observation time.
        request_identity = {
            "context_id": context_id,
            "fields": business,
            "source": {"kind": source["kind"], "key": source["key"]},
            "actor": actor,
        }
        if origin_report_id is not None:
            request_identity["origin_report_id"] = origin_report_id
        request_hash = sha256(canonical(request_identity))
        return self._create_verified(
            payload,
            business,
            source,
            key,
            request_hash,
            actor,
            origin_report_id=origin_report_id,
            content_evidence=proof,
            before_commit=before_commit,
        )

    async def update_case(
        self,
        case_id: str,
        expected_version: int,
        changes: dict[str, Any],
        *,
        actor: str = "user",
        authorized: bool = False,
        content_evidence: dict[str, Any] | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        _actor(actor, authorized)
        changes = _fields(changes, partial=True)
        if not changes:
            raise ValueError("At least one editable field is required")
        if actor == "agent" and "assigned_owner" in changes:
            raise OperationForbidden(
                "The Agent may propose an owner; a human must record an assignment"
            )
        with self._connect(write=True) as db:
            record = self._row(db, case_id)
            self._expect(record, expected_version)
            if actor == "agent" and record["status"] != "draft":
                raise OperationForbidden("The Agent can only edit draft cases")
            if (
                record["status"] == "action_planned"
                and changes.get("action_plan", record["action_plan"]) is None
            ):
                raise ValueError("An action-planned case must retain its action_plan")
            if record["status"] in {"resolved", "dismissed"}:
                raise OperationConflict("Reopen this case before changing its working details")
            proof = _content_evidence(changes, record["context_id"], actor, content_evidence)
            effective = {
                key: {"before": record[key], "after": value}
                for key, value in changes.items()
                if value != record[key]
            }
            evidence = _updated_content_evidence(record, changes, actor, proof, effective)
            if before_commit is not None:
                before_commit()
            if effective:
                before = record["status"]
                record.update(
                    changes,
                    content_evidence=evidence,
                    version=record["version"] + 1,
                    updated_at=_now(),
                )
                self._save(db, record)
                self._audit(db, record, actor, "updated", before, effective)
            return self._detail(db, record)

    async def transition_case(
        self,
        case_id: str,
        expected_version: int,
        status: str,
        *,
        note: str | None = None,
        outcome: str | None = None,
        confirmed: bool = False,
        actor: str = "user",
    ) -> dict[str, Any]:
        if actor != "user" or confirmed is not True:
            raise OperationForbidden(
                "A human must explicitly confirm this status change in the interface"
            )
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError("Invalid status")
        note = _text(note, "note", 4000, nullable=True)
        outcome = _text(outcome, "outcome", 4000, nullable=True)
        with self._connect(write=True) as db:
            record = self._row(db, case_id)
            self._expect(record, expected_version)
            before = record["status"]
            fields = self._transition_fields(record, status, note, outcome)
            changes = {
                key: {"before": record[key], "after": value} for key, value in fields.items()
            }
            record.update(fields, version=record["version"] + 1, updated_at=_now())
            record["resolved_at"] = record["updated_at"] if status == "resolved" else None
            self._save(db, record)
            self._audit(
                db,
                record,
                actor,
                "reopened" if before in {"resolved", "dismissed"} else "transitioned",
                before,
                changes,
                note,
            )
            return self._detail(db, record)

    async def save_case(
        self,
        case_id: str,
        expected_version: int,
        *,
        changes: dict[str, Any] | None = None,
        status: str | None = None,
        note: str | None = None,
        outcome: str | None = None,
        confirmed: bool = False,
        actor: str = "user",
    ) -> dict[str, Any]:
        """Validate a human's complete edit before committing one version and audit."""
        if actor != "user" or confirmed is not True:
            raise OperationForbidden(
                "A human must explicitly confirm this case update in the interface"
            )
        changes = _fields(changes if changes is not None else {}, partial=True)
        if status is not None and (not isinstance(status, str) or status not in STATUSES):
            raise ValueError("Invalid status")
        note = _text(note, "note", 4000, nullable=True)
        outcome = _text(outcome, "outcome", 4000, nullable=True)
        with self._connect(write=True) as db:
            record = self._row(db, case_id)
            self._expect(record, expected_version)
            before = record["status"]
            target = status if status is not None else before
            effective = {
                key: {"before": record[key], "after": value}
                for key, value in changes.items()
                if value != record[key]
            }
            if before in {"resolved", "dismissed"} and effective:
                raise OperationConflict("Reopen this case before changing its working details")
            candidate = {**record, **changes}
            if target == "action_planned" and not candidate["action_plan"]:
                raise ValueError("An action-planned case must retain its action_plan")
            if target != before:
                fields = self._transition_fields(candidate, target, note, outcome)
                effective.update(
                    {key: {"before": record[key], "after": value} for key, value in fields.items()}
                )
                candidate.update(fields)
            elif outcome is not None:
                raise ValueError("An outcome is only recorded when resolving a case")
            candidate["content_evidence"] = _updated_content_evidence(
                record, changes, actor, {}, effective
            )
            if effective or note:
                candidate.update(version=record["version"] + 1, updated_at=_now())
                if target != before:
                    candidate["resolved_at"] = (
                        candidate["updated_at"] if target == "resolved" else None
                    )
                if target != before:
                    kind = "reopened" if before in {"resolved", "dismissed"} else "transitioned"
                else:
                    kind = "updated" if effective else "note_added"
                self._save(db, candidate)
                self._audit(db, candidate, actor, kind, before, effective, note)
            return self._detail(db, candidate)

    async def add_note(
        self, case_id: str, expected_version: int, text: str, *, actor: str = "user"
    ) -> dict[str, Any]:
        if actor != "user":
            raise OperationForbidden("Only a human can add operational notes")
        clean_text = _text(text, "text", 4000, required=True)
        with self._connect(write=True) as db:
            record = self._row(db, case_id)
            self._expect(record, expected_version)
            record.update(version=record["version"] + 1, updated_at=_now())
            self._save(db, record)
            self._audit(db, record, actor, "note_added", record["status"], {}, clean_text)
            return self._detail(db, record)

    async def _compare(self, baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        left, right = baseline["context"], current["context"]
        if left["filters"] != right["filters"]:
            raise ValueError(
                "Follow-up comparisons require exactly the same filters as the saved baseline"
            )
        if (
            left["kpi_version"] != right["kpi_version"]
            or left["code_sha256"] != right["code_sha256"]
        ):
            raise ValueError(
                "Follow-up comparisons require the same KPI definitions and analysis code"
            )
        snapshots = [
            await self.analysis.snapshot_for_context(item["context_id"]) for item in (left, right)
        ]
        maps = []
        for snapshot in snapshots:
            maps.append(
                {
                    event.defect_id: {
                        key: value
                        for key, value in event.as_record().items()
                        if key != "source_row"
                    }
                    for event in analytics.filter_events(snapshot, Scope(**left["filters"]))
                }
            )
        old, new = maps
        changed = left["snapshot_id"] != right["snapshot_id"]
        # Coverage belongs to the same retained records used in this comparison.
        # The source can contain out-of-period rows, which cannot describe this view.
        latest = max((event["defect_date"] for event in new.values()), default=None)
        metrics = []
        for key in METRIC_KEYS:
            before, after = (
                baseline["analysis"]["kpis"].get(key),
                current["analysis"]["kpis"].get(key),
            )
            delta = after - before if before is not None and after is not None else None
            metrics.append({"key": key, "before": before, "after": after, "delta": delta})
        return {
            "id": uuid.uuid4().hex,
            "checked_at": _now(),
            "baseline_context_id": left["context_id"],
            "current_context_id": right["context_id"],
            "status": "changed" if changed else "unchanged",
            "source_changed": left["source_sha256"] != right["source_sha256"],
            "scope_unchanged": True,
            "summary": "The historical source or reference data changed; review the recorded differences."
            if changed
            else "The saved historical source and reference data are unchanged; this check provides no new evidence of an action's effectiveness.",
            "metrics": metrics,
            "data_changes": {
                "added_events": len(new.keys() - old.keys()),
                "removed_events": len(old.keys() - new.keys()),
                "changed_events": sum(old[key] != new[key] for key in old.keys() & new.keys()),
            },
            "coverage": {
                "study_start": "2024-01-01",
                "study_end": "2024-06-30",
                "latest_event_date": latest,
                "actual_produced_units": None,
                "inspected_units": None,
                "operating_days": None,
                "reporting_complete": None,
            },
            "baseline_provenance": self._binding(baseline),
            "current_provenance": self._binding(current),
            "calculation": {
                "method": "Same-scope recorded-event and KPI comparison",
                "code_sha256": self.code_sha256,
            },
            "limitations": COMPARISON_LIMITATIONS.copy(),
        }

    async def add_follow_up(
        self,
        case_id: str,
        expected_version: int,
        context_id: str,
        *,
        note: str | None = None,
        confirmed: bool = False,
        actor: str = "user",
    ) -> dict[str, Any]:
        if actor != "user" or confirmed is not True:
            raise OperationForbidden("A human must confirm the follow-up comparison")
        _identifier(context_id, CONTEXT_ID, "context_id")
        _version(expected_version)
        note = _text(note, "note", 4000, nullable=True)
        record = self.get_case(case_id)
        self._expect(record, expected_version)
        baseline = await self.analysis.context_by_id(record["context_id"])
        current = await self.analysis.context_by_id(context_id)
        comparison = await self._compare(baseline, current)
        with self._connect(write=True) as db:
            record = self._row(db, case_id)
            self._expect(record, expected_version)
            record.update(follow_up=comparison, version=record["version"] + 1, updated_at=_now())
            self._save(db, record)
            self._audit(
                db,
                record,
                "user",
                "follow_up_added",
                record["status"],
                {"follow_up": comparison},
                note,
            )
            return self._detail(db, record)

    async def check_source(self, context_id: str) -> dict[str, Any]:
        """Explicit, bounded source refresh; no interpretation as a live alarm."""
        _identifier(context_id, CONTEXT_ID, "context_id")
        baseline = await self.analysis.context_by_id(context_id)
        try:
            current = await self.analysis.refresh_for_context(context_id)
            if current.get("status", {}).get("stale"):
                raise SourceError(
                    "The source check failed; the last complete snapshot is still retained"
                )
            comparison = await self._compare(baseline, current)
        except (SourceError, OSError) as error:
            comparison = {
                "id": uuid.uuid4().hex,
                "checked_at": _now(),
                "baseline_context_id": context_id,
                "current_context_id": None,
                "status": "unavailable",
                "source_changed": None,
                "scope_unchanged": True,
                "summary": "The source could not be checked. The saved baseline remains available.",
                "metrics": [],
                "data_changes": {
                    "added_events": None,
                    "removed_events": None,
                    "changed_events": None,
                },
                "coverage": {
                    "study_start": "2024-01-01",
                    "study_end": "2024-06-30",
                    "latest_event_date": None,
                    "actual_produced_units": None,
                    "inspected_units": None,
                    "operating_days": None,
                    "reporting_complete": None,
                },
                "limitations": COMPARISON_LIMITATIONS.copy(),
                "error": str(error),
            }
            current = baseline
        # Rechecking the same pair updates one review record. Work is created only
        # by a separate human Add action, never by an observation or refresh.
        comparison["id"] = sha256(
            canonical([context_id, comparison["current_context_id"], comparison["status"]])
        )[:32]
        comparison["review_required"] = comparison["status"] in {"changed", "unavailable"}
        with self._connect(write=True) as db:
            db.execute(
                "INSERT INTO operation_checks(id,baseline_context_id,checked_at,payload) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at,payload=excluded.payload",
                (comparison["id"], context_id, comparison["checked_at"], canonical(comparison)),
            )
        return {**comparison, "review_cases": []}

    def list_checks(self, context_id: str | None = None) -> dict[str, Any]:
        if context_id is not None:
            _identifier(context_id, CONTEXT_ID, "context_id")
        query = (
            "SELECT payload FROM operation_checks"
            + (" WHERE baseline_context_id=?" if context_id else "")
            + " ORDER BY checked_at DESC,id LIMIT 100"
        )
        with self._connect() as db:
            return {
                "checks": [
                    json.loads(row["payload"])
                    for row in db.execute(query, (context_id,) if context_id else ()).fetchall()
                ]
            }


async def _http(
    operation: Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]], *args: Any, **kwargs: Any
) -> dict[str, Any]:
    try:
        result = operation(*args, **kwargs)
        return await result if isinstance(result, Awaitable) else result
    except OperationForbidden as error:
        raise HTTPException(403, str(error)) from error
    except (OperationNotFound, ContextNotFound) as error:
        raise HTTPException(404, str(error)) from error
    except (OperationConflict, EvidenceIntegrityError, SnapshotUnavailable) as error:
        raise HTTPException(409, str(error)) from error
    except SourceError as error:
        raise HTTPException(503, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


async def _body(request: Request, allowed: set[str], required: set[str]) -> dict[str, Any]:
    try:
        values = await request.json()
    except (ValueError, UnicodeError) as error:
        raise HTTPException(422, "Request body must be a JSON object") from error
    if not isinstance(values, dict) or set(values) - allowed or required - set(values):
        raise HTTPException(422, "Request body has missing, unknown or immutable fields")
    return values


def register_operations_routes(app: FastAPI, service: OperationsService) -> None:
    """Register business-only routes; the host owns origin and local access checks."""

    @app.get("/api/operations/cases")
    async def cases(context_id: str | None = None, status: str | None = None) -> dict[str, Any]:
        return await _http(service.list_cases, context_id, status)

    @app.post("/api/operations/cases")
    async def create_case(request: Request) -> dict[str, Any]:
        values = await _body(
            request,
            set(EDITABLE_FIELDS) | {"context_id", "source", "idempotency_key", "origin_report_id"},
            {"context_id", "title", "idempotency_key"},
        )
        return await _http(service.create_case, values)

    @app.get("/api/operations/cases/{case_id}")
    async def case(case_id: str) -> dict[str, Any]:
        return await _http(service.get_case, case_id)

    @app.patch("/api/operations/cases/{case_id}")
    async def edit_case(case_id: str, request: Request) -> dict[str, Any]:
        values = await _body(
            request, {"expected_version", "changes"}, {"expected_version", "changes"}
        )
        return await _http(service.update_case, case_id, **values)

    @app.post("/api/operations/cases/{case_id}/transitions")
    async def transition(case_id: str, request: Request) -> dict[str, Any]:
        values = await _body(
            request,
            {"expected_version", "status", "note", "outcome", "confirmed"},
            {"expected_version", "status", "confirmed"},
        )
        return await _http(service.transition_case, case_id, **values)

    @app.post("/api/operations/cases/{case_id}/updates")
    async def save_case(case_id: str, request: Request) -> dict[str, Any]:
        values = await _body(
            request,
            {"expected_version", "changes", "status", "note", "outcome", "confirmed"},
            {"expected_version", "confirmed"},
        )
        return await _http(service.save_case, case_id, **values)

    @app.post("/api/operations/cases/{case_id}/notes")
    async def note(case_id: str, request: Request) -> dict[str, Any]:
        values = await _body(request, {"expected_version", "text"}, {"expected_version", "text"})
        return await _http(service.add_note, case_id, **values)

    @app.post("/api/operations/cases/{case_id}/follow-up")
    async def follow_up(case_id: str, request: Request) -> dict[str, Any]:
        values = await _body(
            request,
            {"expected_version", "context_id", "note", "confirmed"},
            {"expected_version", "context_id", "confirmed"},
        )
        return await _http(service.add_follow_up, case_id, **values)

    @app.post("/api/operations/check-source")
    async def check_source(request: Request) -> dict[str, Any]:
        values = await _body(request, {"context_id"}, {"context_id"})
        return await _http(service.check_source, **values)

    @app.get("/api/operations/source-checks")
    async def source_checks(context_id: str | None = None) -> dict[str, Any]:
        return await _http(service.list_checks, context_id)
