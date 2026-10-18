"""Shared case dashboard and agent workspace with durable, scoped evidence."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import mimetypes
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint

from .case_study.ingestion import SourceError
from .config import ACTIVE_MODEL, Settings
from .localization import (
    LANGUAGES,
    REPORT_NUMBER_FORMAT,
    normalize_language,
    report_number_format,
    request_language,
    translate_text,
)
from .store import SessionStore, normalize_action_intent

if TYPE_CHECKING:
    from .dashboard import AnalysisService
    from .tools import AnalystTools


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


class Workspace:
    def __init__(
        self,
        settings: Settings,
        runtime_factory: Callable[..., Any] | None = None,
        analysis_service: AnalysisService | None = None,
    ):
        self.settings = settings
        state = Path(settings.state_dir)
        self.state_dir = state if state.is_absolute() else settings.root / state
        self.store = SessionStore(self.state_dir)
        self.listeners: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.active_run_ids: dict[str, str] = {}
        self.active_runtimes: dict[str, Any] = {}
        self.steering_closed: set[str] = set()
        self.toolkits: dict[str, AnalystTools] = {}
        self.toolkit_contexts: dict[str, str | None] = {}
        self.active_contexts: dict[str, str | None] = {}
        self.active_languages: dict[str, str] = {}
        self.active_action_intents: dict[str, str] = {}
        self.run_languages: dict[str, str] = {}
        if analysis_service is None:
            from .dashboard import AnalysisService

            analysis_service = AnalysisService(settings.root, self.state_dir / "analysis")
        self.analysis = analysis_service
        from .operations import OperationsService

        self.operations = OperationsService(settings, self.analysis)
        self.lock = asyncio.Lock()
        self.runtime_factory = runtime_factory
        self.closed = False
        self.ui_lock = asyncio.Lock()
        self.ui_bindings: dict[str, str | None] = {}
        self.ui_authorities: dict[str, dict] = {}
        self.user_cancelled: set[str] = set()

    def _bind_ui_scope(self, sid: str, payload: dict) -> dict:
        current = self.store.investigation(sid)
        context = payload["context"]
        ui = {**current["ui"], "context_id": context["context_id"], "last_command_id": None}
        if current["ui"].get("context_id") == context["context_id"]:
            return current
        findings = [
            {**item, "status": "stale"} if item.get("context_id") != context["context_id"] else item
            for item in current["findings"]
        ]
        return self.store.save_investigation(
            sid, current["version"], {"ui": ui, "findings": findings}
        )

    async def set_ui_scope(self, sid: str, context_id: str) -> dict:
        payload = await self.analysis.context_by_id(context_id)
        async with self.ui_lock:
            saved = self._bind_ui_scope(sid, payload)
        await self.emit(sid, None, "investigation", saved)
        return saved

    async def execute_ui_command(
        self, sid: str, rid: str, run_context_id: str, arguments: dict
    ) -> dict:
        from .evidence import sha256

        async with self.ui_lock:
            if not self.ui_authorities.get(rid, {}).get("view"):
                raise ValueError(
                    "This run did not receive direct user authority to change the view"
                )
            current = self.store.investigation(sid)
            expected = self.ui_bindings.get(rid, run_context_id)
            if expected is None:
                raise ValueError("UI commands require a bound analysis context")
            if current["ui"].get("context_id") != expected:
                raise ValueError(
                    "The UI scope changed during this run; this stale command was not applied"
                )
            action = arguments["action"]
            if action == "undo":
                if not self.ui_authorities.get(rid, {}).get("undo"):
                    raise ValueError(
                        "This run did not receive a direct request to undo a UI command"
                    )
                command = await self._undo_ui(sid, arguments.get("command_id"), expected)
            else:
                if arguments.get("command_id") is not None:
                    raise ValueError("command_id is supplied only for undo")
                payload = await self.analysis.context_by_id(expected)
                target = payload
                if action == "apply_scope":
                    from .investigation import validate_scope_change

                    validate_scope_change(
                        self.ui_authorities.get(rid, {}),
                        payload["context"]["filters"],
                        arguments["filters"],
                    )
                    filters = {
                        **payload["context"]["filters"],
                        **{
                            key: value
                            for key, value in arguments["filters"].items()
                            if value is not None
                        },
                    }
                    target = await self.analysis.get_context(
                        filters=filters, snapshot_id=payload["context"]["snapshot_id"]
                    )
                elif any(value is not None for value in arguments["filters"].values()):
                    raise ValueError("Opening a panel cannot implicitly change filters")
                command = {
                    "command_id": sha256(rid + canonical_json(arguments) + str(current["version"]))[
                        :32
                    ],
                    "action": action,
                    "panel": arguments["panel"],
                    "context_id": target["context"]["context_id"],
                    "previous_context_id": expected,
                    "expected_context_id": expected,
                    "run_context_id": run_context_id,
                    "filters": target["context"]["filters"],
                    "previous_panel": current["ui"]["panel"],
                    "reversible": True,
                }
                self.store.save_ui_command(sid, rid, command)
                self._save_command_state(sid, command)
            self.ui_bindings[rid] = command["context_id"]
        await self.emit(sid, rid, "ui_command", command)
        # Availability is read-only enrichment after the command has committed.
        # Its failure cannot turn a successful Undo into a failed command/retry.
        try:
            retained_reports = self._retained_reports(sid)
        except Exception as error:
            retained_reports = {
                "status": "unavailable",
                "items": [],
                "error": "retained_report_lookup_failed",
                "error_type": type(error).__name__,
            }
        return {
            "command": command,
            "effect": "Local reversible UI command issued; the running snapshot remains unchanged",
            "retained_reports": retained_reports,
        }

    def _retained_reports(self, sid: str) -> dict:
        """Check bounded historical report metadata without adopting its scope."""
        from .evidence import EvidenceLedger, sha256

        saved = self.store.investigation(sid).get("artifacts", [])
        reports = [
            item
            for item in saved
            if isinstance(item, dict) and item.get("kind") == "business_report"
        ]
        items = []
        fields = (
            "path",
            "name",
            "sha256",
            "size_bytes",
            "mime_type",
            "kind",
            "context_id",
            "scope",
            "dataset_version",
            "policy_version",
            "scenario",
            "export_locale",
            "value_locale",
        )
        for report in reversed(reports[-5:]):
            item: dict[str, Any] = {
                "status": "unavailable",
                "error": "retained_report_verification_failed",
            }
            try:
                rid = report["run_id"]
                relative = report["path"]
                if (
                    not isinstance(rid, str)
                    or not re.fullmatch(r"[a-f0-9]{32}", rid)
                    or relative != "artifacts/business_report.html"
                ):
                    raise ValueError("Invalid retained report identity")
                item.update(run_id=rid, path=relative)
                # Guard existing anchored paths before either resolver can mkdir.
                owning_root = self.store.root / "sessions" / sid / "runs" / rid
                artifacts_root = owning_root / "artifacts"
                if (
                    not owning_root.is_dir()
                    or owning_root.resolve(strict=True) != owning_root
                    or not artifacts_root.is_dir()
                    or artifacts_root.resolve(strict=True) != artifacts_root
                ):
                    raise ValueError("Retained report directories are absent or redirected")
                for name in ("business_report.html", "verification_scope.json"):
                    candidate = (artifacts_root / name).resolve(strict=True)
                    if not candidate.is_relative_to(artifacts_root) or not candidate.is_file():
                        raise ValueError("Retained report target escapes its owning artifacts")
                target = self.store.artifact(sid, rid, relative)
                ledger = EvidenceLedger(self.store.root / "sessions" / sid / "runs" / rid)
                matches = [
                    entry
                    for entry in ledger.artifacts()
                    if isinstance(entry, dict) and entry.get("path") == relative
                ]
                if len(matches) != 1:
                    raise ValueError("Retained report registration is ambiguous")
                registered = matches[0]
                if any(report.get(key) != registered.get(key) for key in fields):
                    raise ValueError("Retained report metadata changed")
                size = registered.get("size_bytes")
                if (
                    type(size) is not int
                    or size < 0
                    or target.stat().st_size != size
                    or not isinstance(registered.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", registered["sha256"])
                    or sha256(target.read_bytes()) != report.get("sha256")
                ):
                    raise ValueError("Retained report bytes changed")
                # This server export binds the original report's historical sources.
                scope_path = self.store.artifact(sid, rid, "artifacts/verification_scope.json")
                if scope_path.stat().st_size > 1_000_000:
                    raise ValueError("Historical source metadata exceeds its bound")
                original = json.loads(scope_path.read_text(encoding="utf-8"))
                delivery = original["delivery"]
                manifest = delivery["source_manifest"]
                if (
                    not isinstance(manifest, dict)
                    or manifest != report.get("source_manifest")
                    or manifest.get("status") == "unavailable"
                    or registered not in delivery["artifacts"]
                ):
                    raise ValueError("Historical source metadata changed")
                contexts = manifest["contexts"]
                if (
                    not isinstance(contexts, list)
                    or not 1 <= len(contexts) <= 16
                    or any(
                        not isinstance(context, dict)
                        or not isinstance(context.get("context_id"), str)
                        or not re.fullmatch(r"[a-f0-9]{64}", context["context_id"])
                        or not isinstance(context.get("filters"), dict)
                        for context in contexts
                    )
                    or len({context["context_id"] for context in contexts}) != len(contexts)
                ):
                    raise ValueError("Historical contexts are unavailable")
                if registered.get("context_id") is not None:
                    context = next(
                        context
                        for context in contexts
                        if context["context_id"] == registered["context_id"]
                    )
                    if any(
                        registered.get(key) != context.get(source)
                        for key, source in (
                            ("scope", "filters"),
                            ("dataset_version", "dataset_version"),
                            ("policy_version", "policy_version"),
                            ("scenario", "scenario"),
                        )
                    ):
                        raise ValueError("Registered context differs from historical sources")
                item = {
                    "status": "available",
                    "session_id": sid,
                    "run_id": rid,
                    "path": relative,
                    "url": f"/api/sessions/{sid}/runs/{rid}/artifacts/{quote(relative, safe='/')}",
                    "sha256": registered["sha256"],
                    "size_bytes": size,
                    "kind": "business_report",
                    "historical_context_ids": sorted(
                        {context["context_id"] for context in contexts}
                    ),
                    "verification": {
                        "same_session": True,
                        "registry_and_file": True,
                        "saved_metadata": True,
                        "historical_source_manifest": True,
                    },
                }
            except (KeyError, ValueError, TypeError, AttributeError, StopIteration, OSError):
                pass
            items.append(item)
        return {
            "status": "checked",
            "items": items,
            "omitted_count": max(0, len(reports) - len(items)),
            "scope_note": "Historical report availability only. Its recorded sources do not become the current UI scope; no prior claims or numeric identifiers are imported.",
        }

    def _save_command_state(self, sid: str, command: dict) -> dict:
        current = self.store.investigation(sid)
        findings = [
            {**item, "status": "stale"} if item.get("context_id") != command["context_id"] else item
            for item in current["findings"]
        ]
        return self.store.save_investigation(
            sid,
            current["version"],
            {
                "findings": findings,
                "ui": {
                    "context_id": command["context_id"],
                    "panel": command["panel"],
                    "last_command_id": command["command_id"],
                },
            },
        )

    async def _undo_ui(
        self, sid: str, command_id: str | None, expected_context_id: str | None
    ) -> dict:
        from .evidence import sha256

        current = self.store.investigation(sid)
        original = self.store.ui_command(sid, command_id or current["ui"].get("last_command_id"))
        if current["ui"].get("context_id") != expected_context_id:
            raise ValueError("The UI scope changed; reload before undo")
        inverse_id = sha256("undo:" + original["command_id"])[:32]
        if original["undone"]:
            inverse = self.store.ui_command(sid, inverse_id)
            if inverse["context_id"] != expected_context_id:
                raise ValueError("This command was already undone and the view changed again")
            inverse.pop("undone", None)
            return inverse
        if (
            original["context_id"] != expected_context_id
            or current["ui"].get("last_command_id") != original["command_id"]
        ):
            raise ValueError("Only the latest command in its unchanged UI context can be undone")
        target = await self.analysis.context_by_id(original["previous_context_id"])
        command = {
            "command_id": inverse_id,
            "action": "undo",
            "panel": original["previous_panel"],
            "context_id": target["context"]["context_id"],
            "previous_context_id": expected_context_id,
            "expected_context_id": expected_context_id,
            "run_context_id": original["run_context_id"],
            "filters": target["context"]["filters"],
            "previous_panel": current["ui"]["panel"],
            "reversible": True,
        }
        self.store.save_ui_command(sid, "undo:" + original["command_id"], command)
        self._save_command_state(sid, command)
        self.store.mark_command_undone(sid, original["command_id"])
        return command

    async def undo_ui_command(self, sid: str, command_id: str, expected_context_id: str) -> dict:
        async with self.ui_lock:
            command = await self._undo_ui(sid, command_id, expected_context_id)
        await self.emit(sid, None, "ui_command", command)
        return {"command": command, "investigation": self.store.investigation(sid)}

    async def cancel(self, sid: str, run_id: str | None = None) -> dict:
        session = self.store.session(sid)
        active = self.tasks.get(sid)
        requested = active is not None and not active.done()
        current_run = self.active_run_ids.get(sid) if requested else None
        if current_run is None:
            current_run = next(
                (run["id"] for run in reversed(session["runs"]) if run["state"] == "running"),
                None,
            )
        if run_id is not None and current_run != run_id:
            raise ValueError("The requested run is not the active run")
        if requested and active is not None:
            self.user_cancelled.add(sid)
            active.cancel()
        return {"cancel_requested": requested, "run_id": current_run}

    async def emit(
        self, sid: str, rid: str | None, kind: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        # Keep the browser event contract independent from SDK event variants.
        data = dict(data)
        language = data.get("language") or (
            self.run_languages.get(rid) if rid is not None else None
        )
        if language is None and rid is not None:
            language = self.store.run_language(rid)
        language = language or "en"
        if kind == "usage" and data.get("response_id") and self.settings.live:
            if rid is None:
                raise ValueError("Usage events require a run id")
            self.store.record_cost(rid, data["response_id"], float(data["cost_usd"]))
            data["elapsed_ms"] = data.get("elapsed_seconds", 0) * 1000
            data["estimated"] = True
        elif kind == "usage" and data.get("provider_api") == "agents" and self.settings.live:
            if rid is None:
                raise ValueError("Managed usage events require a run id")
            identity, cost = data.get("boundary_id"), data.get("cost_usd")
            if (
                identity
                and isinstance(cost, (int, float))
                and not isinstance(cost, bool)
                and math.isfinite(cost)
                and cost >= 0
            ):
                self.store.record_cost(rid, "managed:" + str(identity), float(cost))
            data["elapsed_ms"] = data.get("elapsed_seconds", 0) * 1000
            data["estimated"] = True
        if kind == "reasoning_summary":
            # Keep the provider's summary-part identity so the agent flow can
            # group deltas into the same section the model actually emitted.
            kind, data = (
                "reasoning",
                {
                    "text": data.get("delta", data.get("text", "")),
                    "simulation": not self.settings.live,
                    "final": bool(data.get("done")),
                    **{
                        key: data[key]
                        for key in (
                            "item_id",
                            "summary_index",
                            "output_index",
                            "response_id",
                            "provider_event",
                            "sequence_number",
                            "source",
                            "reconciled",
                            "provider_api",
                            "provider_session_id",
                            "provider_turn_id",
                            "session_id",
                            "turn_id",
                            "event_id",
                            "display_text",
                            "display_mode",
                            "display_localized",
                            "display_source",
                        )
                        if data.get(key) is not None
                    },
                },
            )
        elif kind in {"tool_started", "tool_completed"}:
            completed = kind == "tool_completed"
            entry = data.get("entry", {})
            arguments = data.get("arguments", {})
            data = {
                "name": data.get("name"),
                "tool_id": data.get("tool_id"),
                "call_id": data.get("call_id"),
                "status": "failed"
                if entry.get("result", {}).get("ok") is False
                else "completed"
                if completed
                else "running",
                "code": entry.get(
                    "code", arguments.get("code", arguments.get("query", json.dumps(arguments)))
                ),
                "result": entry.get("result"),
                "execution_id": entry.get("execution_id"),
                "code_sha256": entry.get("code_sha256"),
                "duration_ms": data.get("elapsed_seconds", 0) * 1000,
            }
            kind = "tool"
        elif kind == "memory":
            result = data.get("result", {})
            data.update(
                action=data.get("operation", "recalled"), content=result, key=result.get("key", "")
            )
        elif kind == "qa":
            data.update(
                status="verified" if data.get("verified") else "unverified",
                issues=[translate_text(str(error), language) for error in data.get("errors", [])],
            )
        elif kind == "qa_retry":
            kind = "qa"
            data.update(
                status="retrying",
                issues=[translate_text(str(error), language) for error in data.get("errors", [])],
            )
        elif kind == "report":
            if (data.get("qa") or {}).get("verified") is False:
                await self.emit(sid, rid, "qa", data["qa"])
            report = data.get("report", "")
            for artifact in data.get("artifacts", []):
                relative = artifact.get("path", "")
                if relative:
                    url = f"/api/sessions/{sid}/runs/{rid}/artifacts/{quote(relative, safe='/')}"
                    for link in {relative, artifact.get("name", "")} - {""}:
                        report = report.replace("](" + link + ")", "](" + url + ")")
            delivery_metadata = {}
            if "delivery" in data:
                delivery = data["delivery"]
                if isinstance(delivery, dict) and isinstance(delivery.get("artifacts"), list):
                    delivery = {**delivery, "artifacts": []}
                    for selected in data["delivery"]["artifacts"]:
                        if isinstance(selected, dict):
                            selected = dict(selected)
                            selected.pop("url", None)
                            filename = selected.get("path")
                            if isinstance(filename, str) and filename and sid and rid:
                                path = Path(filename)
                                run_root = (
                                    self.state_dir / "sessions" / sid / "runs" / rid
                                ).resolve()
                                try:
                                    relative = (
                                        path.resolve().relative_to(run_root)
                                        if path.is_absolute()
                                        else path
                                    )
                                    if (run_root / relative).resolve().is_relative_to(run_root):
                                        selected["url"] = (
                                            f"/api/sessions/{sid}/runs/{rid}/artifacts/{quote(relative.as_posix(), safe='/')}"
                                        )
                                except (OSError, ValueError):
                                    pass
                        delivery["artifacts"].append(selected)
                # Presence distinguishes an authoritative empty selection from a
                # legacy report. Invalid packets remain present for fail-closed UI.
                delivery_metadata["delivery"] = delivery
            kind, data = (
                "message",
                {
                    "role": "assistant",
                    "agent": "Lead Analyst",
                    "text": report,
                    "claims": data.get("claims", []),
                    "qa": data.get("qa"),
                    "simulation": data.get("simulation"),
                    "language": language,
                    "report_number_format": data.get(
                        "report_number_format", report_number_format(language)
                    ),
                    **delivery_metadata,
                },
            )
        if kind == "status" and data.get("label"):
            data["label"] = translate_text(data["label"], language)
        data.setdefault("language", language)
        # Runtime artifacts are always served through the owning session/run.
        if kind == "artifact":
            data = dict(data)
            data["mime"] = data.get("mime_type", data.get("mime", "application/octet-stream"))
            data["size"] = data.get("size_bytes", data.get("size"))
            filename = data.get("path") or data.get("name")
            if filename:
                if rid is None:
                    raise ValueError("Artifact events require a run id")
                path = Path(str(filename))
                run_root = (self.state_dir / "sessions" / sid / "runs" / rid).resolve()
                try:
                    relative = path.resolve().relative_to(run_root) if path.is_absolute() else path
                    target = (run_root / relative).resolve()
                    if target.is_relative_to(run_root):
                        data["url"] = (
                            f"/api/sessions/{sid}/runs/{rid}/artifacts/{quote(relative.as_posix(), safe='/')}"
                        )
                except ValueError:
                    pass
        event = self.store.emit(sid, rid, kind, data)
        for queue in tuple(self.listeners.get(sid, set())):
            queue.put_nowait(event)
        return event

    def _steering_target(self, sid: str, rid: str) -> dict[str, Any]:
        task = self.tasks.get(sid)
        runtime = self.active_runtimes.get(sid)
        if sid in self.user_cancelled or (task is not None and task.cancelling()):
            raise ValueError("This analysis is stopping and cannot accept additional details")
        if (
            task is None
            or task.done()
            or self.active_run_ids.get(sid) != rid
            or rid in self.steering_closed
            or not getattr(runtime, "accepting_steering", True)
        ):
            raise ValueError("This analysis has finished; send a new question instead")
        run = next((run for run in self.store.session(sid)["runs"] if run["id"] == rid), None)
        if run is None or run["state"] != "running":
            raise ValueError("This analysis has finished; send a new question instead")
        return run

    def _reply_target(self, sid: str, reply_to: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if (
            not isinstance(reply_to, dict)
            or set(reply_to) != {"run_id", "message_id"}
            or not isinstance(reply_to["run_id"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", reply_to["run_id"])
            or type(reply_to["message_id"]) is not int
            or reply_to["message_id"] <= 0
        ):
            raise ValueError("Invalid answer reference for this follow-up")
        session = self.store.session(sid)
        run = next((run for run in session["runs"] if run["id"] == reply_to["run_id"]), None)
        messages = [
            event
            for event in session["events"]
            if event["run_id"] == reply_to["run_id"]
            and event["type"] == "message"
            and event["data"].get("role") == "assistant"
        ]
        if run is None or run["state"] != "completed" or not messages:
            raise ValueError("The referenced answer is unavailable or has not completed")
        message = messages[-1]
        if (
            message["id"] != reply_to["message_id"]
            or not isinstance(message["data"].get("qa"), dict)
            or message["data"]["qa"].get("verified") is not True
            or not isinstance(message["data"].get("text"), str)
            or not message["data"]["text"].strip()
        ):
            raise ValueError("The referenced answer is stale, unavailable or unverified")
        return run, message

    async def prompt(
        self,
        sid: str,
        text: str,
        analysis_context_id: str | None = None,
        language: str = "en",
        action_intent: str = "none",
        steer_run_id: str | None = None,
        reply_to: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self.store.session(sid)
        if reply_to is not None:
            if steer_run_id is not None:
                raise ValueError("An answer follow-up cannot also steer an active analysis")
            if action_intent != "none":
                raise ValueError("An answer follow-up is read-only; request a work item separately")
            source_run, _ = self._reply_target(sid, reply_to)
            reply_to = dict(reply_to)
            source_context = source_run["analysis_context_id"]
            if analysis_context_id is not None and analysis_context_id != source_context:
                raise ValueError("The follow-up context differs from the referenced answer")
            analysis_context_id = source_context
            language = source_run["language"]
        language_change_from_steering = False
        if steer_run_id is not None:
            if not isinstance(steer_run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", steer_run_id):
                raise ValueError("Invalid active analysis for this detail")
            target = self._steering_target(sid, steer_run_id)
            analysis_context_id = target["analysis_context_id"]
            language = target["language"]
            action_intent = target["action_intent"]
            from .localization import explicit_response_language

            requested_language = explicit_response_language(text)
            if requested_language is not None and requested_language != language:
                # A language change needs a separate run, since the active
                # managed session keeps its original configuration and authority.
                steer_run_id = None
                language = requested_language
                action_intent = "none"
                language_change_from_steering = True
        if self.settings.agent_mode == "unavailable" and self.runtime_factory is None:
            raise ValueError("AI is unavailable because no API key is configured")
        if (
            analysis_context_id is None
            and reply_to is None
            and self.runtime_factory is None
            and self.settings.mode != "mock"
        ):
            existing_context = self.store.investigation(sid)["ui"].get("context_id")
            payload = (
                await self.analysis.context_by_id(existing_context)
                if existing_context
                else await self.analysis.get_context()
            )
            analysis_context_id = payload["context"]["context_id"]
        from .investigation import response_language

        previous_language = next(
            (
                event["data"]["language"]
                for event in reversed(session["events"])
                if event["type"] == "message"
                and event["data"].get("role") == "user"
                and event["data"].get("language") in {"en", "vi"}
            ),
            normalize_language(language),
        )
        if reply_to is not None:
            language = response_language(text, language)
        else:
            language = (
                normalize_language(language)
                if steer_run_id is not None
                else response_language(text, previous_language)
            )
        action_intent = normalize_action_intent(action_intent)
        if action_intent != "none" and analysis_context_id is None:
            raise ValueError(
                "Choose a dashboard view before asking the agent to prepare a work item"
            )
        if self.store.stopped(sid):
            raise ValueError(
                "The provider stopped this conversation. Its execution is retained for review; this conversation cannot be retried."
            )
        text = text.strip()
        if not text:
            raise ValueError("Enter a prompt")
        prompt_limit = self.settings.run_limit("max_prompt_chars")
        if prompt_limit is not None and len(text) > prompt_limit:
            raise ValueError("The prompt is too long; attach a document instead")
        verified_context = None
        if analysis_context_id is not None:
            from .case_study.ingestion import SourceError
            from .dashboard import EvidenceIntegrityError

            if not isinstance(analysis_context_id, str) or not re.fullmatch(
                r"[a-f0-9]{16,64}", analysis_context_id
            ):
                raise ValueError(
                    "Invalid dashboard context; reload the Dashboard before sending this prompt"
                )
            try:
                verified_context = await self.analysis.context_by_id(analysis_context_id)
            except (
                LookupError,
                ValueError,
                FileNotFoundError,
                EvidenceIntegrityError,
                SourceError,
            ) as error:
                raise ValueError(
                    "This dashboard snapshot is unavailable or its evidence could not be verified. Reload the Dashboard and review its scope before retrying."
                ) from error
        steering_delivery: tuple[str, int, str, Any] | None = None
        async with self.lock:
            if reply_to is not None:
                self._reply_target(sid, reply_to)
            if steer_run_id is not None:
                self._steering_target(sid, steer_run_id)
            if (
                analysis_context_id is not None
                and steer_run_id is None
                and reply_to is None
                and not language_change_from_steering
            ):
                self._bind_ui_scope(sid, await self.analysis.context_by_id(analysis_context_id))
            active = self.tasks.get(sid)
            active_rid = self.active_run_ids.get(sid)
            changed_scope = self.active_contexts.get(sid) != analysis_context_id
            changed_language = self.active_languages.get(sid, "en") != language
            changed_action = self.active_action_intents.get(sid, "none") != action_intent
            same_run = bool(
                steer_run_id is not None
                and active
                and not active.done()
                and active_rid
                and not (changed_scope or changed_language or changed_action)
            )
            if same_run and active_rid is not None:
                self._steering_target(sid, active_rid)
            await self.emit(
                sid,
                active_rid if same_run else None,
                "message",
                {
                    "role": "user",
                    "text": text,
                    "analysis_context_id": analysis_context_id,
                    "language": language,
                    "action_intent": action_intent,
                    **({"steer_run_id": active_rid} if same_run else {}),
                    **({"reply_to": reply_to} if reply_to is not None else {}),
                },
            )
            if active and not active.done():
                # Prohibitions take effect even when a language or scope change
                # queues the remaining instruction for a separate run.
                from .investigation import merge_scope_grants, scope_grants

                restrictions = scope_grants(text, {}) if reply_to is None else {}
                if restrictions.get("revoke_filters") or restrictions.get("revoke_view"):
                    if active_rid in self.ui_authorities:
                        self.ui_authorities[active_rid] = merge_scope_grants(
                            self.ui_authorities[active_rid], restrictions
                        )
                if same_run and active_rid is not None:
                    if verified_context is not None:
                        incoming = scope_grants(text, verified_context.get("options", {}))
                        self.ui_authorities[active_rid] = merge_scope_grants(
                            self.ui_authorities.get(active_rid, {"filters": {}, "undo": False}),
                            incoming,
                        )
                    runtime = self.active_runtimes.get(sid)
                    steer = getattr(runtime, "steer", None)
                    instruction_row = self.store.record_run_instruction(
                        sid, active_rid, text, applied=callable(steer)
                    )
                    instruction_id = f"steering-{active_rid}-{instruction_row}"
                    await self.emit(
                        sid,
                        active_rid,
                        "steering_received",
                        {"message": text, "instruction_id": instruction_id},
                    )
                    steering_delivery = (active_rid, instruction_row, instruction_id, steer)
                else:
                    self.store.queue_instruction(
                        sid, text, analysis_context_id, language, action_intent, reply_to
                    )
                    await self.emit(
                        sid,
                        None,
                        "status",
                        {
                            "state": "queued",
                            "language": language,
                            "label": (
                                (
                                    "Yêu cầu hỏi tiếp về câu trả lời được xếp cho phân tích riêng sau lượt hiện tại."
                                    if language == "vi"
                                    else "Your answer follow-up is queued for a separate analysis after the current run."
                                )
                                if reply_to is not None
                                else "Your new dashboard scope is queued for a separate run after the current analysis."
                                if changed_scope
                                else "Your new response language is queued for a separate run after the current analysis."
                                if changed_language
                                else "Your work-item request is queued for a separate run after the current analysis."
                                if changed_action
                                else "Your instruction is queued for the next safe agent boundary."
                            ),
                        },
                    )
                    return {"queued": True}
            else:
                self.active_contexts[sid] = analysis_context_id
                self.active_languages[sid] = language
                self.active_action_intents[sid] = action_intent
                self.tasks[sid] = asyncio.create_task(
                    self._run(sid, text, analysis_context_id, language, action_intent, reply_to)
                )
                return {"queued": False}
        # Provider admission can take arbitrarily long. The target and durable
        # input are already captured; other sessions and terminal cleanup must
        # remain free to acquire the workspace admission lock while it runs.
        assert steering_delivery is not None
        active_rid, instruction_row, instruction_id, steer = steering_delivery
        result = {"accepted": True, "mode": "same_run_boundary"}
        if callable(steer):
            try:
                result = await steer(text, instruction_id=instruction_id)
                if not result.get("accepted"):
                    raise ValueError("The active runtime did not accept this detail")
            except Exception as error:
                self.store.mark_instruction_unconfirmed(instruction_row, active_rid)
                await self.emit(
                    sid,
                    active_rid,
                    "steering_failed",
                    {
                        "instruction_id": instruction_id,
                        "error_type": type(error).__name__,
                    },
                )
                raise ValueError(
                    "Delivery of this detail could not be confirmed; its saved input is retained"
                ) from error
        return {
            **result,
            "queued": False,
            "steering": True,
            "run_id": active_rid,
            "instruction_id": instruction_id,
        }

    def _preflight_settlement_evidence(
        self, sid: str, rid: str, result: dict, budget: dict, knowledge_cost: float
    ) -> dict | None:
        """Prove a token-count failure left no unaccounted generation liability."""
        if (
            not self.settings.live
            or result.get("status") != "failed"
            or not isinstance(budget, dict)
        ):
            return None
        failure = budget.get("preflight_failure")
        if (
            not isinstance(failure, dict)
            or failure.get("phase") != "input_token_count"
            or failure.get("generation_dispatched") is not False
            or budget.get("blocked") is not True
            or budget.get("provider_stopped")
        ):
            return None
        for accounting in (failure, budget):
            if (
                type(accounting.get("unresolved_generation_requests")) is not int
                or accounting["unresolved_generation_requests"] != 0
            ):
                return None
        accounted = failure.get("accounted_response_ids")
        if (
            not isinstance(accounted, list)
            or any(not isinstance(identity, str) or not identity for identity in accounted)
            or len(set(accounted)) != len(accounted)
            or budget.get("accounted_response_ids") != accounted
        ):
            return None

        return self._terminal_settlement_evidence(
            sid, rid, result, budget, knowledge_cost, checkpoint=failure
        )

    def _terminal_settlement_evidence(
        self,
        sid: str,
        rid: str,
        result: dict,
        budget: dict,
        knowledge_cost: float,
        *,
        checkpoint: dict | None = None,
    ) -> dict | None:
        """Match every dispatched generation to durable terminal usage and cost."""
        if (
            not self.settings.live
            or result.get("status")
            not in {
                "failed",
                "timeout",
                "incomplete",
                "refused",
                "budget_exhausted",
                "cancelled",
                "interrupted",
            }
            or not isinstance(budget, dict)
            or type(budget.get("unresolved_generation_requests")) is not int
            or budget["unresolved_generation_requests"] != 0
            or (checkpoint is None and result.get("usage_complete") is not True)
        ):
            return None
        accounted = budget.get("accounted_response_ids")
        if (
            not isinstance(accounted, list)
            or any(not isinstance(identity, str) or not identity for identity in accounted)
            or len(set(accounted)) != len(accounted)
        ):
            return None
        checkpoint = checkpoint if checkpoint is not None else budget

        def valid_cost(value):
            return type(value) in {int, float} and math.isfinite(value) and value >= 0

        if not all(
            valid_cost(value)
            for value in (
                checkpoint.get("cost_usd"),
                budget.get("cost_usd"),
                result.get("cost_usd"),
                knowledge_cost,
            )
        ):
            return None
        # Read only this run's accounting rows. The transport lock has finished;
        # a failed run is never resumed automatically after this check.
        with self.store._lock:
            run = self.store.db.execute(
                "SELECT session_id,state FROM runs WHERE id=?", (rid,)
            ).fetchone()
            reservation = self.store.db.execute(
                "SELECT amount,settled FROM budget_reservations WHERE run_id=?", (rid,)
            ).fetchone()
            event_rows = self.store.db.execute(
                "SELECT id,type,data FROM events WHERE session_id=? AND run_id=? "
                "AND type IN ('model_started','response_created','usage') ORDER BY id",
                (sid, rid),
            ).fetchall()
            cost_rows = self.store.db.execute(
                "SELECT id,amount FROM costs WHERE run_id=?", (rid,)
            ).fetchall()
        if (
            run is None
            or run["session_id"] != sid
            or run["state"] not in {"running", "failed", "cancelled", "interrupted"}
            or reservation is None
            or reservation["settled"]
            or not valid_cost(reservation["amount"])
        ):
            return None
        events = [{**dict(row), "data": json.loads(row["data"])} for row in event_rows]
        if any(not isinstance(event["data"], dict) for event in events):
            return None
        started = [event for event in events if event["type"] == "model_started"]
        created = [event for event in events if event["type"] == "response_created"]
        usage = [
            event
            for event in events
            if event["type"] == "usage" and event["data"].get("response_id")
        ]
        if not len(started) == len(created) == len(usage) == len(accounted):
            return None
        identities, observed_costs, event_ids = [], {}, []
        previous_usage = -1
        for start, creation, terminal in zip(started, created, usage, strict=True):
            record = terminal["data"]
            identity = creation["data"].get("response_id")
            if (
                not previous_usage < start["id"] < creation["id"] < terminal["id"]
                or start["data"].get("model") != ACTIVE_MODEL
                or identity not in accounted
                or record.get("response_id") != identity
                or record.get("provider_terminal") is not True
                or record.get("provider_status")
                not in {"completed", "failed", "incomplete", "cancelled"}
                or record.get("model") != ACTIVE_MODEL
                or record.get("requested_model") != ACTIVE_MODEL
                or any(
                    type(record.get(key)) is not int or record[key] < 0
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cached_tokens",
                        "cache_write_tokens",
                    )
                )
                or record["cached_tokens"] + record["cache_write_tokens"] > record["input_tokens"]
                or not valid_cost(record.get("cost_usd"))
            ):
                return None
            previous_usage = terminal["id"]
            identities.append(identity)
            observed_costs[identity] = record["cost_usd"]
            event_ids.extend([start["id"], creation["id"], terminal["id"]])
        if identities != accounted:
            return None
        durable_costs = {row["id"]: row["amount"] for row in cost_rows}
        if any(not valid_cost(value) for value in durable_costs.values()):
            return None
        if any(durable_costs.get(identity) != cost for identity, cost in observed_costs.items()):
            return None
        storage = {
            identity: cost
            for identity, cost in durable_costs.items()
            if identity not in observed_costs
        }
        if any(not identity.startswith("storage:") for identity in storage) or not math.isclose(
            sum(storage.values()), knowledge_cost, rel_tol=0, abs_tol=1e-12
        ):
            return None
        known_cost = sum(durable_costs.values())
        if not all(
            math.isclose(value, known_cost, rel_tol=0, abs_tol=1e-12)
            for value in (checkpoint["cost_usd"], budget["cost_usd"], result["cost_usd"])
        ):
            return None
        return {
            "eligible": True,
            "reason": "all_prior_generations_accounted",
            "phase": checkpoint.get("phase", "terminal_response"),
            "generation_dispatched": checkpoint.get("generation_dispatched", bool(started)),
            "unresolved_generation_requests": 0,
            "accounted_response_ids": identities,
            "accounting_event_ids": event_ids,
            "known_cost_usd": known_cost,
            "reservation_usd": reservation["amount"],
            "unused_reservation_usd": max(0.0, reservation["amount"] - known_cost),
        }

    async def _run(
        self,
        sid: str,
        prompt: str,
        analysis_context_id: str | None = None,
        language: str = "en",
        action_intent: str = "none",
        reply_to: dict[str, Any] | None = None,
        queued_instruction_id: int | None = None,
    ) -> None:
        from .memory import AnalystMemory
        from .runtime import AnalystRuntime
        from .tools import AnalystTools

        language = normalize_language(language)
        action_intent = normalize_action_intent(action_intent)
        rid, directory = self.store.begin_run(sid, analysis_context_id, language, action_intent)
        self.active_run_ids[sid] = rid
        self.ui_bindings[rid] = analysis_context_id
        self.active_contexts[sid] = analysis_context_id
        self.active_languages[sid] = language
        self.active_action_intents[sid] = action_intent
        self.run_languages[rid] = language
        initial_items = self.store.history(sid)
        referenced_presentation = None
        # Preserve the original request before any awaited work. A restart resumes
        # only on a new user prompt, with this exact request still in its context.
        # Steering has its own durable state, so it is not duplicated here.
        if prompt:
            self.store.save_history(sid, initial_items + [{"role": "user", "content": prompt}])
        carried = (
            self.store.take_instructions(
                sid,
                rid,
                include_unresolved=True,
                analysis_context_id=analysis_context_id,
                language=language,
                action_intent=action_intent,
                reply_to=reply_to,
                queued_instruction_id=queued_instruction_id,
            )
            if reply_to is None or queued_instruction_id is not None
            else []
        )
        if carried:
            prompt = "\n\n".join(carried + ([prompt] if prompt else []))

        async def emit(kind: str, data: dict[str, Any]) -> dict[str, Any]:
            return await self.emit(sid, rid, kind, data)

        await emit(
            "status",
            {
                "state": "running",
                "label": "Lead Analyst is preparing the run",
                **({"reply_to": reply_to} if reply_to is not None else {}),
            },
        )

        async def steering() -> list[str]:
            messages = self.store.take_instructions(
                sid,
                rid,
                analysis_context_id=analysis_context_id,
                language=language,
                action_intent=action_intent,
            )
            if messages:
                await emit(
                    "status", {"state": "running", "label": "Applying your updated instruction"}
                )
                from .investigation import merge_scope_grants, scope_grants

                if analysis_context_id:
                    bound = await self.analysis.context_by_id(analysis_context_id)
                    for message in messages:
                        incoming = scope_grants(message, bound.get("options", {}))
                        prior = self.ui_authorities.get(rid, {"filters": {}, "undo": False})
                        self.ui_authorities[rid] = merge_scope_grants(prior, incoming)
            return messages

        allow_followup = False
        runtime = None
        knowledge_cost = 0.0
        try:
            if reply_to is not None:
                source_run, source_message = self._reply_target(sid, reply_to)
                if source_run["analysis_context_id"] != analysis_context_id:
                    raise ValueError("The referenced answer context changed before this run")
                source_text = re.sub(
                    r"\[claim:((?:[^\[\]]|\[\d+\])+)\]",
                    "",
                    source_message["data"]["text"],
                )
                source_text = re.sub(
                    r"^<!-- evidence-table:[a-f0-9]{24}:(?:start|end) -->\r?$",
                    "",
                    source_text,
                    flags=re.MULTILINE,
                )
                historical_presentation = []
                delivery = source_message["data"].get("delivery")
                source_charts = (
                    delivery.get("source_charts", []) if isinstance(delivery, dict) else []
                )
                for source_chart in source_charts if isinstance(source_charts, list) else []:
                    chart = source_chart.get("chart") if isinstance(source_chart, dict) else None
                    if not isinstance(chart, dict) or chart.get("kind") not in {"bar", "line"}:
                        continue
                    selection = chart.get("selection")
                    axes = chart.get("axis_labels")
                    if not isinstance(selection, dict) or not isinstance(axes, dict):
                        continue
                    if (
                        any(not isinstance(selection.get(key), str) for key in ("path", "x", "y"))
                        or any(type(selection.get(key)) is not int for key in ("offset", "limit"))
                        or any(not isinstance(axes.get(key), str) for key in ("x", "y"))
                    ):
                        continue
                    contexts = chart.get("analysis_contexts", [])
                    historical_presentation.append(
                        {
                            "kind": chart["kind"],
                            "selection": {
                                key: selection[key]
                                for key in ("path", "x", "y", "offset", "limit")
                                if key in selection
                            },
                            "axis_labels": {key: axes[key] for key in ("x", "y") if key in axes},
                            "unit_note": chart.get("unit_note", "")
                            if isinstance(chart.get("unit_note", ""), str)
                            else "",
                            "language": chart.get("language")
                            if isinstance(chart.get("language"), str)
                            and chart.get("language") in {"en", "vi"}
                            else source_run["language"],
                            "analysis_contexts": [
                                {
                                    "filters": {
                                        key: value
                                        for key, value in context.get("filters", {}).items()
                                        if isinstance(value, str) or value is None
                                    }
                                    if isinstance(context.get("filters"), dict)
                                    else {},
                                    "scenario": context.get("scenario", "")
                                    if isinstance(context.get("scenario", ""), str)
                                    else "",
                                }
                                for context in (contexts if isinstance(contexts, list) else [])
                                if isinstance(context, dict)
                            ],
                        }
                    )
                # Historical content stays separate from the exact current request.
                # It imports neither numeric claims nor the source run's authority.
                referenced_presentation = historical_presentation or None
                initial_items = initial_items + [
                    {
                        "role": "user",
                        "content": canonical_json(
                            {
                                "referenced_answer": reply_to,
                                "historical_text": source_text,
                                **(
                                    {"historical_presentation": historical_presentation}
                                    if historical_presentation
                                    else {}
                                ),
                                "context_id": analysis_context_id,
                                "source_language": source_run["language"],
                                "evidence_status": "Historical conversation only; retrieve current-run tool evidence for measurements. No action authority is inherited.",
                            }
                        ),
                    }
                ]
                self.store.save_history(sid, initial_items + [{"role": "user", "content": prompt}])
            analysis_context = None
            if analysis_context_id:
                analysis_context = await self.analysis.context_by_id(analysis_context_id)
                from .investigation import scope_grants

                self.ui_authorities[rid] = scope_grants(prompt, analysis_context.get("options", {}))
                await emit(
                    "analysis_context",
                    {
                        **analysis_context["context"],
                        "evidence": {
                            key: analysis_context["evidence"][key]
                            for key in ("execution_id", "code_sha256", "result_sha256")
                            if key in analysis_context["evidence"]
                        },
                        "source_status": analysis_context.get("status", {}),
                        "links": analysis_context["links"],
                    },
                )
            effective = self.settings
            if effective.mode != "mock":
                effective = replace(
                    effective,
                    enable_file_search=False,
                    enable_code_interpreter=False,
                    enable_web_search=False,
                    enable_mcp=False,
                    enable_tool_search=False,
                    vector_store_id="",
                )
            if effective.live:
                allowance = self.store.reserve_budget(
                    rid,
                    effective.run_limit("max_cost_usd_per_run"),
                    effective.run_limit("dev_api_budget_usd"),
                )
                if allowance is not None:
                    effective = replace(effective, max_cost_usd_per_run=allowance)
            uploads = self.store.uploads(sid, internal=True)
            inputs = [Path(item["path"]) for item in uploads]
            input_sources = [
                {
                    "path": str(Path(item["path"]).resolve()),
                    "origin": "upload",
                    "name": item["name"],
                }
                for item in uploads
            ]
            if analysis_context_id:
                context_inputs = await self.analysis.input_paths_for_context(analysis_context_id)
                inputs = context_inputs + inputs
                input_sources = [
                    {
                        "path": str(path.resolve()),
                        "origin": "dashboard_export",
                        "name": path.name,
                        "context_id": analysis_context_id,
                    }
                    for path in context_inputs
                ] + input_sources
            # The bundled case has immutable reference data. Other inputs are explicit uploads.
            master = self.settings.root / "data" / "master_data.csv"
            if not inputs and master.exists():
                inputs = [master]
                input_sources = [
                    {
                        "path": str(master.resolve()),
                        "origin": "bundled_reference",
                        "name": master.name,
                    }
                ]
            toolkit = self.toolkits.get(sid)
            # Variables derived from another dashboard scope must not leak into
            # this scope. Within one scope, the Python session stays persistent.
            if toolkit is not None and self.toolkit_contexts.get(sid) != analysis_context_id:
                await toolkit.close()
                toolkit = None
            if toolkit is None:
                memory = (
                    AnalystMemory(self.state_dir / "memory")
                    if self.settings.memory_enabled
                    else None
                )
                toolkit = AnalystTools(
                    directory,
                    inputs,
                    session_id=sid,
                    memory=memory,
                    timeout_seconds=self.settings.run_limit("tool_timeout_seconds"),
                    memory_mb=self.settings.run_limit("tool_memory_mb"),
                    memory_enabled=self.settings.memory_enabled,
                )
                self.toolkits[sid] = toolkit
                self.toolkit_contexts[sid] = analysis_context_id
            else:
                await toolkit.begin_run(directory, inputs)
            from .knowledge import prepare_knowledge

            prepared = await prepare_knowledge(effective, toolkit.memory, emit)
            knowledge_cost = 0.0
            if isinstance(prepared, tuple):
                effective = prepared[0]
                knowledge_status = prepared[1]
                knowledge_cost = float(knowledge_status.get("storage_reservation_usd", 0))
                if effective.live and knowledge_cost:
                    day = datetime.now(UTC).date().isoformat()
                    self.store.record_cost(
                        rid,
                        f"storage:{knowledge_status.get('vector_store_id')}:{day}",
                        knowledge_cost,
                    )
            elif isinstance(prepared, Settings):
                effective = prepared
            runtime_type = self.runtime_factory or AnalystRuntime
            runtime_options = (
                {"analysis_context": analysis_context, "analysis_service": self.analysis}
                if analysis_context is not None
                else {}
            )
            if language != "en":
                runtime_options["language"] = language
            parameters = inspect.signature(runtime_type).parameters
            accepts_options = any(
                item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
            )
            if accepts_options or "operations_service" in parameters:
                runtime_options["operations_service"] = self.operations
            if accepts_options or "input_sources" in parameters:
                runtime_options["input_sources"] = input_sources
            if accepts_options or "action_intent" in parameters:
                runtime_options["action_intent"] = action_intent
            elif action_intent != "none":
                raise ValueError("The configured runtime does not support work-item requests")
            if accepts_options or "state_store" in parameters:
                runtime_options["state_store"] = self.store
            if referenced_presentation and (
                accepts_options or "referenced_presentation" in parameters
            ):
                runtime_options["referenced_presentation"] = referenced_presentation
            if accepts_options or "ui_handler" in parameters:

                async def ui_handler(arguments: dict[str, Any]) -> dict[str, Any]:
                    if analysis_context_id is None:
                        raise ValueError("UI commands require a bound analysis context")
                    return await self.execute_ui_command(sid, rid, analysis_context_id, arguments)

                runtime_options["ui_handler"] = ui_handler
            runtime = runtime_type(
                effective,
                toolkit,
                emit,
                steering=steering,
                initial_items=initial_items,
                **runtime_options,
            )
            self.active_runtimes[sid] = runtime
            if effective.live and hasattr(runtime, "budget"):
                runtime.budget["cost_usd"] = knowledge_cost
            result = await runtime.run(prompt)
            self.steering_closed.add(rid)
            current_investigation = self.store.investigation(sid)
            selected_artifacts = [
                {
                    **artifact,
                    "run_id": rid,
                    "context_id": artifact.get("context_id")
                    if artifact.get("kind") == "business_report"
                    else artifact.get("context_id", analysis_context_id),
                    **(
                        {"source_manifest": result["delivery"]["source_manifest"]}
                        if artifact.get("kind") == "business_report"
                        and isinstance(result.get("delivery"), dict)
                        and isinstance(result["delivery"].get("source_manifest"), dict)
                        else {}
                    ),
                    "url": f"/api/sessions/{sid}/runs/{rid}/artifacts/{quote(artifact['path'], safe='/')}",
                }
                for artifact in result.get("artifacts", [])
                if isinstance(artifact, dict) and artifact.get("path")
            ]
            if selected_artifacts:
                existing = current_investigation["artifacts"]
                selected = {
                    (item["run_id"], item["path"]): item
                    for item in [*existing, *selected_artifacts]
                }
                saved = self.store.save_investigation(
                    sid,
                    current_investigation["version"],
                    {"artifacts": list(selected.values())[-40:]},
                )
                await emit("investigation", saved)
            result.setdefault("language", language)
            result.setdefault("report_number_format", report_number_format(language))
            history = result.get("history")
            if history is not None:
                self.store.save_history(sid, history)
            state = result.get("status", "unverified")
            allow_followup = state == "completed"
            reconciliation = None
            if state not in {"completed", "unverified"}:
                try:
                    reconciliation = self._preflight_settlement_evidence(
                        sid, rid, result, getattr(runtime, "budget", {}), knowledge_cost
                    )
                    if reconciliation is None and not getattr(runtime, "budget", {}).get(
                        "preflight_failure"
                    ):
                        reconciliation = self._terminal_settlement_evidence(
                            sid, rid, result, getattr(runtime, "budget", {}), knowledge_cost
                        )
                    if reconciliation is not None:
                        result["budget_reconciliation"] = reconciliation
                except Exception as error:
                    result["budget_reconciliation"] = {
                        "eligible": False,
                        "reason": "accounting_check_failed",
                        "error_type": type(error).__name__,
                    }
            # Persist the failed report and eligibility proof before releasing
            # any reservation. Eligibility never claims settlement succeeded.
            self.store.finish_run(rid, state, result)
            if state in {"completed", "unverified"}:
                self.store.settle_budget(rid)
            elif reconciliation is not None:
                try:
                    self.store.settle_budget(rid)
                except Exception as error:
                    reconciliation["settlement_error_type"] = type(error).__name__
                    # A failed release must not replace the original QA failure
                    # or replay a saved draft. The reservation remains authoritative.
                    with suppress(Exception):
                        self.store.finish_run(rid, state, result)
            elif state == "provider_stopped":
                self.store.stop_session(sid, "Provider policy stop")

            def token_total(field: str) -> int | None:
                values = [item.get(field) for item in result.get("usage", [])]
                return (
                    sum(values)
                    if result.get("usage_complete", True)
                    and all(value is not None for value in values)
                    else None
                )

            await emit(
                "usage",
                {
                    "input_tokens": token_total("input_tokens"),
                    "output_tokens": token_total("output_tokens"),
                    "cached_tokens": token_total("cached_tokens"),
                    "cost_usd": result.get("cost_usd", 0),
                    "elapsed_ms": result.get("elapsed_seconds", 0) * 1000,
                    "estimated": self.settings.live,
                    "simulation": not self.settings.live,
                    "scope": "run",
                },
            )
            await emit(
                "status",
                {
                    "state": state,
                    "label": {
                        "completed": "Analysis completed and evidence checked",
                        "unverified": "Some claims remain unverified; inspect the QA result",
                        "budget_exhausted": "Run stopped at its configured cost limit; pending instructions are retained",
                    }.get(state, state.replace("_", " ")),
                },
            )
        except asyncio.CancelledError:
            completed_before_cancel = allow_followup
            allow_followup = False
            cancelled = sid in self.user_cancelled
            self.user_cancelled.discard(sid)
            if completed_before_cancel:
                await emit(
                    "status",
                    {
                        "state": "completed",
                        "label": "Analysis completed; queued instructions are retained after Stop",
                    },
                )
                raise
            state = "cancelled" if cancelled else "interrupted"
            cancellation_message = (
                "Analysis cancelled; saved evidence is retained"
                if cancelled
                else "The app is stopping; saved evidence is retained"
            )
            accounting = getattr(runtime, "budget", {})
            usage = [
                entry
                for model in getattr(runtime, "models", [])
                for entry in getattr(model, "usage", [])
            ]
            usage_complete = accounting.get("unresolved_generation_requests") == 0 and all(
                entry.get("usage_complete", True) for entry in usage
            )
            result = {
                "status": state,
                "report": "",
                "qa": {
                    "verified": False,
                    "errors": [translate_text(cancellation_message, language)],
                },
                "usage": usage,
                "usage_complete": usage_complete,
                "cost_usd": accounting.get("cost_usd") if usage_complete else None,
                "known_accounted_cost_usd": accounting.get("cost_usd"),
                "language": language,
                "report_number_format": report_number_format(language),
                "provider_sessions": [
                    {"session_id": model.session_id, "turn_id": getattr(model, "turn_id", None)}
                    for model in getattr(runtime, "models", [])
                    if getattr(model, "session_id", None)
                ],
            }
            reconciliation = self._terminal_settlement_evidence(
                sid, rid, result, accounting, knowledge_cost
            )
            if reconciliation is not None:
                result["budget_reconciliation"] = reconciliation
            self.store.finish_run(rid, state, result)
            if reconciliation is not None:
                try:
                    self.store.settle_budget(rid)
                except Exception as error:
                    reconciliation["settlement_error_type"] = type(error).__name__
                    self.store.finish_run(rid, state, result)
            await emit(
                "status",
                {
                    "state": state,
                    "label": "Analysis cancelled; saved evidence is retained"
                    if cancelled
                    else "The app is stopping; saved evidence is retained",
                },
            )
            raise
        except Exception as error:
            self.store.finish_run(rid, "failed")
            message = str(error)
            if self.settings.api_key:
                message = message.replace(self.settings.api_key, "[redacted]")
            await emit("error", {"message": f"{type(error).__name__}: {message}"})
            await emit(
                "status",
                {
                    "state": "failed",
                    "label": "Run stopped. Correct the reported issue and send another prompt.",
                },
            )
        finally:
            async with self.lock:
                self.steering_closed.discard(rid)
                if self.active_runtimes.get(sid) is runtime:
                    self.active_runtimes.pop(sid, None)
                if self.active_run_ids.get(sid) == rid:
                    self.active_run_ids.pop(sid)
                if (
                    allow_followup
                    and self.store.has_queued_instruction(sid)
                    and not self.closed
                    and not self.store.stopped(sid)
                ):
                    pending = self.store.next_queued_run(sid)
                    if pending is not None:
                        context_id = pending["analysis_context_id"]
                        next_language = pending["language"]
                        next_action_intent = pending["action_intent"]
                        self.active_contexts[sid] = context_id
                        self.active_languages[sid] = next_language
                        self.active_action_intents[sid] = next_action_intent
                        self.tasks[sid] = asyncio.create_task(
                            self._run(
                                sid,
                                "",
                                context_id,
                                next_language,
                                next_action_intent,
                                pending.get("reply_to"),
                                pending.get("queued_instruction_id"),
                            )
                        )

    async def close(self) -> None:
        self.closed = True
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for toolkit in self.toolkits.values():
            result = toolkit.close()
            if inspect.isawaitable(result):
                await result
        self.store.close()
        self.operations.close()


def create_app(
    settings: Settings | None = None,
    *,
    runtime_factory: Callable[..., Any] | None = None,
    analysis_service: AnalysisService | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    workspace = Workspace(
        settings, runtime_factory=runtime_factory, analysis_service=analysis_service
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        workspace.store.recover_interrupted()
        from .managed_recovery import recover_managed_sessions

        await recover_managed_sessions(workspace.store, workspace.settings)
        yield
        await workspace.close()

    application = FastAPI(title="Analyst Workspace", version="2.0.0", lifespan=lifespan)
    application.add_middleware(GZipMiddleware, minimum_size=1000)
    application.state.workspace = workspace
    from .dashboard import register_routes

    register_routes(application, workspace.analysis)
    from .operations import register_operations_routes

    register_operations_routes(application, workspace.operations)

    def origin_allowed(origin: str | None) -> bool:
        return settings.allows_origin(origin)

    @application.middleware("http")
    async def protect_local_workspace(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if (
            request.url.path.startswith("/api/")
            and request.method != "GET"
            and not origin_allowed(request.headers.get("origin"))
        ):
            return JSONResponse(
                {"detail": "Cross-origin workspace writes are unavailable"}, status_code=403
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @application.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ready",
            "mode": settings.agent_mode,
            "model": settings.model,
            "ai_available": settings.agent_mode != "unavailable",
        }

    @application.get("/api/config")
    async def config() -> dict[str, Any]:
        result = settings.public_config()
        result["dev_budget_remaining_usd"] = workspace.store.remaining_budget(
            settings.run_limit("dev_api_budget_usd")
        )
        available = settings.agent_mode != "unavailable"
        reason = "missing_api_key" if not available else None
        if (
            settings.live
            and result["dev_budget_remaining_usd"] is not None
            and result["dev_budget_remaining_usd"] <= 0
        ):
            available, reason = False, "budget_exhausted"
        result.update(ai_available=available, ai_unavailable_reason=reason)
        result["limits"] = {
            "max_upload_mb": settings.run_limit("max_upload_mb"),
            "max_cost_usd_per_run": settings.run_limit("max_cost_usd_per_run"),
            "qa_max_retries": settings.run_limit("qa_max_retries"),
        }
        result.update(
            languages=list(LANGUAGES),
            default_language="en",
            report_number_format=REPORT_NUMBER_FORMAT,
            report_number_formats={"en": "en-US", "vi": "vi-VN"},
        )
        return result

    @application.post("/api/sessions")
    async def new_session() -> dict[str, Any]:
        return workspace.store.create_session()

    @application.get("/api/sessions/{sid}")
    async def get_session(sid: str) -> dict[str, Any]:
        try:
            return workspace.store.session(sid)
        except KeyError as error:
            raise HTTPException(404, "Session not found") from error

    @application.get("/api/sessions/{sid}/investigation")
    async def get_investigation(sid: str) -> dict[str, Any]:
        try:
            return workspace.store.investigation(sid)
        except KeyError as error:
            raise HTTPException(404, "Session not found") from error

    @application.get("/api/sessions/{sid}/investigation/export.json")
    async def export_investigation(
        sid: str, version: int = Query(..., ge=0), language: str = "en"
    ) -> FileResponse:
        from .investigation import export_saved_investigation

        try:
            path, artifact = export_saved_investigation(workspace.store, sid, version, language)
        except KeyError as error:
            raise HTTPException(404, "Session not found") from error
        except ValueError as error:
            raise HTTPException(
                422 if "language must" in str(error) else 409, str(error)
            ) from error
        return FileResponse(
            path,
            media_type="application/json",
            filename=path.name,
            headers={
                "ETag": '"' + artifact["sha256"] + '"',
                "X-Investigation-Version": str(version),
                "Cache-Control": "no-store",
            },
        )

    @application.patch("/api/sessions/{sid}/investigation")
    async def edit_investigation(sid: str, request: Request) -> dict[str, Any]:
        from .investigation import user_changes

        body = await request.json()
        try:
            current = workspace.store.investigation(sid)
            changes = user_changes(current, body)
            if current["context_id"] is None and current["ui"].get("context_id"):
                payload = await workspace.analysis.context_by_id(current["ui"]["context_id"])
                context = payload["context"]
                changes.update(
                    context_id=context["context_id"],
                    scope=context["filters"],
                    dataset_version=context.get("dataset_version", context["snapshot_id"]),
                    policy_version=context.get("policy_version"),
                )
            saved = workspace.store.save_investigation(sid, body["expected_version"], changes)
        except KeyError as error:
            raise HTTPException(404, "Session not found") from error
        except ValueError as error:
            raise HTTPException(
                409 if "version changed" in str(error) else 422, str(error)
            ) from error
        await workspace.emit(sid, None, "investigation", saved)
        return saved

    @application.post("/api/sessions/{sid}/scope")
    async def set_scope(sid: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"context_id"}:
            raise HTTPException(422, "Scope requires only context_id")
        try:
            return await workspace.set_ui_scope(sid, body["context_id"])
        except KeyError as error:
            raise HTTPException(404, "Session not found") from error
        except (LookupError, ValueError) as error:
            raise HTTPException(409, str(error)) from error

    @application.post("/api/sessions/{sid}/ui-commands/{command_id}/undo")
    async def undo_command(sid: str, command_id: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"expected_context_id"}:
            raise HTTPException(422, "Undo requires expected_context_id")
        try:
            return await workspace.undo_ui_command(sid, command_id, body["expected_context_id"])
        except KeyError as error:
            raise HTTPException(404, "Session or command not found") from error
        except (LookupError, ValueError) as error:
            raise HTTPException(409, str(error)) from error

    @application.post("/api/sessions/{sid}/cancel")
    async def cancel(sid: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"run_id"}:
            raise HTTPException(422, "Cancel accepts only an optional run_id")
        try:
            return await workspace.cancel(sid, body.get("run_id"))
        except KeyError as error:
            raise HTTPException(404, "Session not found") from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @application.post("/api/sessions/{sid}/files")
    async def upload(sid: str, files: Annotated[list[UploadFile], File()]) -> dict[str, Any]:
        try:
            workspace.store.session(sid)
        except KeyError as error:
            raise HTTPException(404, "Session not found") from error
        accepted = []
        upload_limit = settings.run_limit("max_upload_mb")
        limit = upload_limit * 1024 * 1024 if upload_limit is not None else None
        for file in files:
            if Path(file.filename or "").suffix.lower() not in {
                ".csv",
                ".json",
                ".jsonl",
                ".ndjson",
                ".xlsx",
                ".pdf",
                ".txt",
                ".md",
            }:
                raise HTTPException(415, "Use CSV, JSON, JSONL, XLSX, PDF or text inputs")
            content = await file.read(limit + 1 if limit is not None else -1)
            if limit is not None and len(content) > limit:
                raise HTTPException(413, f"Each input must be at most {settings.max_upload_mb} MB")
            accepted.append(workspace.store.add_upload(sid, file.filename or "input", content))
            await file.close()
        return {"files": accepted}

    @application.post("/api/sessions/{sid}/prompt")
    async def prompt_http(sid: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(422, "Prompt payload must be an object")
        if settings.agent_mode == "unavailable" and workspace.runtime_factory is None:
            raise HTTPException(
                503,
                {
                    "code": "ai_unavailable",
                    "reason": "missing_api_key",
                    "message": "AI is unavailable. Dashboard, scenarios, evidence and exports remain available.",
                },
            )
        try:
            return await workspace.prompt(
                sid,
                str(body.get("text", "")),
                body.get("analysis_context_id"),
                request_language(body),
                body.get("action_intent", "none"),
                body.get("steer_run_id"),
                body.get("reply_to"),
            )
        except SourceError as error:
            raise HTTPException(
                503, {"code": "source_unavailable", "message": str(error)}
            ) from error
        except KeyError as error:
            raise HTTPException(404, "Session not found") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @application.websocket("/api/sessions/{sid}/ws")
    async def stream(websocket: WebSocket, sid: str) -> None:
        if not origin_allowed(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        try:
            workspace.store.session(sid)
            after = max(0, int(websocket.query_params.get("after", "0")))
        except (KeyError, ValueError):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue()
        workspace.listeners.setdefault(sid, set()).add(queue)
        # Subscribe before replay: a duplicate is harmless and is deduplicated by ID.
        sender: asyncio.Task[None] | None = None

        async def send() -> None:
            while True:
                await websocket.send_json(await queue.get())

        try:
            for event in workspace.store.events(sid, after):
                await websocket.send_json(event)
            sender = asyncio.create_task(send())
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "prompt":
                    try:
                        await workspace.prompt(
                            sid,
                            str(message.get("text", "")),
                            message.get("analysis_context_id"),
                            request_language(message),
                            message.get("action_intent", "none"),
                            message.get("steer_run_id"),
                            message.get("reply_to"),
                        )
                    except ValueError as error:
                        await workspace.emit(sid, None, "error", {"message": str(error)})
        except WebSocketDisconnect:
            pass
        finally:
            workspace.listeners[sid].discard(queue)
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    @application.get("/api/sessions/{sid}/runs/{rid}/artifacts/{filename:path}")
    async def artifact(sid: str, rid: str, filename: str) -> FileResponse:
        try:
            target = workspace.store.artifact(sid, rid, filename)
        except (KeyError, FileNotFoundError) as error:
            raise HTTPException(404, "Artifact not found") from error
        media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        safe_image = media in {"image/png", "image/jpeg", "image/webp"}
        return FileResponse(
            target,
            media_type=media,
            filename=target.name,
            content_disposition_type="inline" if safe_image else "attachment",
        )

    distribution = settings.root / "frontend" / "dist"
    if (distribution / "assets").exists():
        application.mount("/assets", StaticFiles(directory=distribution / "assets"), name="assets")

    @application.get("/")
    async def index() -> FileResponse:
        target = distribution / "index.html"
        if not target.exists():
            raise HTTPException(503, "Build the frontend with npm run build")
        return FileResponse(
            target,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @application.get("/favicon.svg")
    async def favicon() -> FileResponse:
        target = distribution / "favicon.svg"
        if not target.is_file():
            raise HTTPException(404, "Icon not found")
        return FileResponse(target, media_type="image/svg+xml")

    return application


app = create_app()
