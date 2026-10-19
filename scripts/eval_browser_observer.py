"""Eval-only loopback observation for the existing R02/R03 sessions.

No prompts are sent. The caller owns Workspace lifecycle, generation, and timing.
This module is intended for the evaluator harness, never the product application.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _js_string(value: str) -> str:
    # Prevent HTML script termination as well as JavaScript string injection.
    return (
        json.dumps(value, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


_GRACEFUL_SHUTDOWN_SECONDS = 3
_STOP_TIMEOUT_SECONDS = 5.0
_CANCEL_TIMEOUT_SECONDS = 1.0


class _ObserverServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The paid harness retains ownership of signals and run cancellation.
        yield


class EvalBrowserObserver:
    def __init__(
        self,
        app: Any,
        workspace: Any,
        directory: str | Path,
        *,
        marker_evidence_mode: str = "root_cua",
    ) -> None:
        if app.state.workspace is not workspace:
            raise ValueError("The observer must share the exact app Workspace object")
        self.app, self.workspace = app, workspace
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.marker_evidence_mode = marker_evidence_mode
        self.assets = self._asset_identity()
        self._bootstrap_sessions: dict[str, str] = {}
        self._published: dict[str, dict[str, Any]] = {}
        self.server: _ObserverServer | None = None
        self.task: asyncio.Task[None] | None = None
        self.sock: socket.socket | None = None
        self.url: str | None = None
        self.route = Route("/__eval_observer/{token}", self._bootstrap, methods=["GET"])
        # Precede static mounts without changing any production source file.
        app.router.routes.insert(0, self.route)

    def _asset_identity(self) -> dict[str, str]:
        dist = self.workspace.settings.root / "frontend" / "dist"
        paths = [dist / "index.html", *sorted((dist / "assets").glob("*"))]
        if not paths[0].is_file():
            raise ValueError("The actual built product frontend is required")
        return {
            (
                "/" if path.name == "index.html" else "/" + path.relative_to(dist).as_posix()
            ): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
            if path.is_file()
        }

    async def start(self) -> str:
        if self.server:
            raise RuntimeError("Observer already started")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.sock.setblocking(False)
        port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.server = _ObserverServer(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=port,
                lifespan="off",
                timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS,
                log_config=None,
                log_level="warning",
                access_log=False,
            )
        )
        self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        while not self.server.started:
            if self.task.done():
                await self.task
                raise RuntimeError("Observer server stopped before startup")
            await asyncio.sleep(0.01)
        print(
            json.dumps({"event": "observer_started", "url": self.url, "assets": self.assets}),
            flush=True,
        )
        return self.url

    async def _bootstrap(self, request: Request) -> Response:
        sid = self._bootstrap_sessions.get(request.path_params["token"])
        if not sid:
            return Response(status_code=404)
        nonce = secrets.token_urlsafe(24)
        code = f"""(() => {{
            const sessionId = {_js_string(sid)};
            const sessionKey = "analyst.workspace.session.v1";
            const draftKey = "analyst.workspace.draft.v2";
            for (const name of ["sessionStorage", "localStorage"]) {{
                try {{ window[name].setItem(sessionKey, sessionId); }} catch {{}}
                try {{ window[name].removeItem(draftKey); }} catch {{}}
            }}
            // Match the Agent's tab-first selection, including its shared fallback.
            let selected = null;
            try {{ selected = sessionStorage.getItem(sessionKey); }} catch {{}}
            if (!selected) {{
                try {{ selected = localStorage.getItem(sessionKey); }} catch {{}}
            }}
            let staleDraft = false;
            try {{ staleDraft = localStorage.getItem(draftKey) !== null; }} catch {{}}
            if (selected !== sessionId || staleDraft) {{
                document.querySelector("p").textContent =
                    "The eval session could not be restored in this tab. " +
                    "Allow browser storage and open the observer link again. " +
                    "No prompt was sent.";
                return;
            }}
            window.location.replace("/");
        }})();"""
        html = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            "<title>Eval observer bootstrap</title><body>"
            "<p>Opening the existing eval session. No prompt is sent.</p>"
            '<script nonce="' + nonce + '">' + code + "</script></body></html>"
        )
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'nonce-"
                    + nonce
                    + "'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                ),
            },
        )

    def publish(
        self,
        case_id: str,
        session_id: str,
        context: dict[str, Any],
        *,
        phase: str = "predispatch",
        command_id: str | None = None,
    ) -> dict[str, Any]:
        if case_id not in {"R02", "R03"}:
            raise ValueError("Only the frozen R02/R03 observation cases are supported")
        if phase not in {"predispatch", "turn_1_complete", "turn_2_complete"}:
            raise ValueError("Unknown observation boundary")
        if not self.url or self.server is None or not self.server.started:
            raise RuntimeError("Start the observer before publishing a boundary")
        self.workspace.store.session(session_id)
        if "context" in context:
            context = context["context"]
        token = secrets.token_hex(16)
        if phase == "predispatch":
            active = self.workspace.tasks.get(session_id)
            if active and not active.done():
                raise ValueError("Cannot bootstrap a session with an active paid run")
            self._bootstrap_sessions[token] = session_id
        marker = self.directory / ("root_ready_" + token + ".json")
        record = {
            "schema": "eval-browser-observer-v1",
            "barrier_id": token,
            "published_at_utc": _now(),
            "case_id": case_id,
            "phase": phase,
            "session_id": session_id,
            "context": context,
            "required_command_id": command_id,
            "url": self.url + "/",
            "bootstrap_url": self.url + "/__eval_observer/" + token
            if phase == "predispatch"
            else None,
            "assets": self.assets,
            "ready_marker": str(marker),
            "ready_marker_template": {
                "barrier_id": token,
                "session_id": session_id,
                "observed_context_id": context["context_id"],
                "evidence_mode": self.marker_evidence_mode,
                "evidence": ["absolute screenshot or DOM receipt path"],
            },
            "generation_dispatched_by_observer": False,
            "instructions": (
                "Before first Agent mount, align Dashboard filters to this context. "
                "Confirm the export context ID, then open Agent and observe its connection. "
                "Do not reload between turn boundaries. Do not Send or click Undo before a frozen model undo turn."
            ),
        }
        self._published[token] = record
        _write_json(self.directory / ("barrier_" + token + ".json"), record)
        _write_json(self.directory / "observer-current.json", record)
        print(json.dumps({"event": "observer_barrier", **record}), flush=True)
        return record

    async def wait(
        self, record: dict[str, Any], *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if self._published.get(record["barrier_id"]) is not record:
            raise ValueError("Unknown or copied barrier object")
        task = self.task
        if task is None:
            raise RuntimeError("Start the observer before waiting")
        started = time.monotonic()
        reason = "root marker absent"
        while True:
            if task.done():
                await task
                raise RuntimeError("Observer exited while waiting")
            marker = Path(record["ready_marker"])
            if marker.is_file():
                try:
                    ready = json.loads(marker.read_text(encoding="utf-8"))
                except (ValueError, UnicodeError):
                    ready = (
                        None  # An atomic rename is preferable, but partial writes never release.
                    )
                if ready is not None:
                    expected = {
                        "barrier_id": record["barrier_id"],
                        "session_id": record["session_id"],
                        "observed_context_id": record["context"]["context_id"],
                        "evidence_mode": self.marker_evidence_mode,
                    }
                    if any(ready.get(key) != value for key, value in expected.items()):
                        raise ValueError("Root marker does not match this session/context/boundary")
                    if (
                        not isinstance(ready.get("evidence"), list)
                        or not ready["evidence"]
                        or not all(
                            isinstance(item, str) and item.strip() for item in ready["evidence"]
                        )
                    ):
                        raise ValueError(
                            "Root marker must reference its actual observation evidence"
                        )
                    sid = record["session_id"]
                    ui = self.workspace.store.investigation(sid)["ui"]
                    if not self.workspace.listeners.get(sid):
                        reason = "the exact session has no live WebSocket listener"
                    elif ui.get("context_id") != record["context"]["context_id"]:
                        reason = "the actual session UI context differs"
                    elif (
                        record["required_command_id"]
                        and ui.get("last_command_id") != record["required_command_id"]
                    ):
                        reason = "the actual latest UI command differs"
                    elif self._asset_identity() != self.assets:
                        raise RuntimeError("Frontend assets changed during observation")
                    else:
                        receipt = {
                            "event": "observer_released",
                            "barrier_id": record["barrier_id"],
                            "case_id": record["case_id"],
                            "phase": record["phase"],
                            "session_id": sid,
                            "context_id": ui["context_id"],
                            "last_command_id": ui.get("last_command_id"),
                            "listener_count": len(self.workspace.listeners[sid]),
                            "observer_wait_seconds": time.monotonic() - started,
                            "released_at_utc": _now(),
                            "ready_marker": ready,
                        }
                        with (self.directory / "observer-receipts.jsonl").open(
                            "a", encoding="utf-8"
                        ) as stream:
                            stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")
                        self._bootstrap_sessions.pop(record["barrier_id"], None)
                        print(json.dumps(receipt), flush=True)
                        return receipt
            if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
                raise TimeoutError("Observer barrier not released: " + reason)
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        server, task = self.server, self.task
        error: BaseException | None = None
        try:
            if server:
                server.should_exit = True
            if task:
                done, _ = await asyncio.wait({task}, timeout=_STOP_TIMEOUT_SECONDS)
                if task not in done:
                    raise TimeoutError("Observer server exceeded the shutdown deadline")
                task.result()
        except BaseException as exc:
            # Preserve cancellation and server errors after releasing owned resources.
            error = exc
        finally:
            with suppress(ValueError):
                self.app.router.routes.remove(self.route)
            self._bootstrap_sessions.clear()
            self._published.clear()
            # Lifespan stays off: the harness owns this exact shared Workspace.
            owned: set[asyncio.Task[None]] = set()
            if server:
                server.should_exit = True
                server.force_exit = True
                for listener in getattr(server, "servers", []):
                    listener.close()
                for connection in list(server.server_state.connections):
                    connection.transport.abort()
                owned.update(server.server_state.tasks)
            # asyncio owns the socket after startup. Let it unregister the live
            # descriptor before closing our reference, including on forced shutdown.
            if self.sock:
                with suppress(OSError):
                    self.sock.close()
            if task:
                owned.add(task)
            for pending in owned:
                if not pending.done():
                    pending.cancel()
            if owned:
                done, pending_tasks = await asyncio.wait(owned, timeout=_CANCEL_TIMEOUT_SECONDS)
                for completed in done:
                    if not completed.cancelled():
                        exception = completed.exception()
                        if exception and error is None:
                            error = exception
                if pending_tasks:
                    cleanup_error = TimeoutError(
                        f"Observer left {len(pending_tasks)} owned task(s) after cancellation"
                    )
                    if error is None:
                        error = cleanup_error
                    else:
                        error.add_note(str(cleanup_error))
        if error:
            raise error
