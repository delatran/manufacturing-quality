"""Isolated cross-platform observer lifecycle proof. No provider requests are permitted."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import struct
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import CodeType
from typing import Any

import httpx
import pytest
import pytest_asyncio
from analyst.app import Workspace, create_app
from analyst.config import Settings
from analyst.managed_agents import ManagedAgentsModel
from fastapi import FastAPI, WebSocket
from starlette.routing import WebSocketRoute
from websockets.asyncio.client import ClientConnection, connect

ROOT = Path(__file__).resolve().parents[1]
HELPER = (
    ROOT
    / "scripts"
    / (
        "eval_browser_observer.original.py"
        if os.environ.get("OBSERVER_BASELINE") == "1"
        else "eval_browser_observer.py"
    )
)
spec = importlib.util.spec_from_file_location("shutdown_helper_under_test", HELPER)
assert spec and spec.loader
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
MEASUREMENTS: list[dict[str, Any]] = []


@dataclass
class Context:
    app: FastAPI
    workspace: Workspace
    observer: Any
    sid: str
    lifecycle: list[str]
    provider_attempts: list[str]


@pytest_asyncio.fixture
async def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Context]:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    attempts: list[str] = []

    def no_provider(*args: Any, **kwargs: Any) -> None:
        attempts.append("provider_initialization")
        raise AssertionError("Provider access is forbidden in this lifecycle proof")

    monkeypatch.setattr(ManagedAgentsModel, "__init__", no_provider)
    application = create_app(Settings(root=ROOT, state_dir=str(tmp_path / "state"), api_key=""))
    workspace = application.state.workspace
    lifecycle: list[str] = []
    original_close = workspace.close
    original_recover = workspace.store.recover_interrupted

    async def tracked_close() -> None:
        lifecycle.append("workspace.close")
        await original_close()

    def tracked_recover() -> Any:
        lifecycle.append("store.recover_interrupted")
        return original_recover()

    monkeypatch.setattr(workspace, "close", tracked_close)
    monkeypatch.setattr(workspace.store, "recover_interrupted", tracked_recover)
    observer = helper.EvalBrowserObserver(application, workspace, tmp_path / "observer")
    sid = workspace.store.create_session()["id"]
    current = Context(application, workspace, observer, sid, lifecycle, attempts)
    try:
        yield current
    finally:
        # This test owns the instance. Finish cleanup even when the historical helper fails.
        server = observer.server
        if server:
            server.should_exit = server.force_exit = True
            for listener in getattr(server, "servers", []):
                listener.close()
            for connection in list(server.server_state.connections):
                connection.transport.abort()
            tasks = set(server.server_state.tasks)
        else:
            tasks = set()
        if observer.task:
            tasks.add(observer.task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=1)
            assert not pending, "The isolated verifier left a task running"
            for task in done:
                if not task.cancelled():
                    task.exception()
        if observer.sock:
            observer.sock.close()
        await original_close()


async def eventually(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Expected lifecycle condition did not become true")
        await asyncio.sleep(0.01)


async def start(context: Context) -> str:
    url = await asyncio.wait_for(context.observer.start(), timeout=5)
    assert context.app.state.workspace is context.workspace
    assert context.lifecycle == []
    assert context.observer.server.config.lifespan == "off"
    async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["ai_available"] is False
        response = await client.post(
            f"/api/sessions/{context.sid}/prompt", json={"text": "No-key guard only"}
        )
        assert response.status_code == 503
        assert response.json()["detail"]["reason"] == "missing_api_key"
    return url


async def assert_released(context: Context, elapsed: float, name: str) -> None:
    observer, workspace = context.observer, context.workspace
    await eventually(lambda: not any(workspace.listeners.values()))
    assert context.app.state.workspace is workspace
    assert not workspace.closed
    assert context.lifecycle == []
    assert context.provider_attempts == []
    assert observer.route not in context.app.router.routes
    assert observer._bootstrap_sessions == {}
    assert observer._published == {}
    if observer.sock:
        assert observer.sock.fileno() == -1
    if observer.task:
        assert observer.task.done()
    if observer.server:
        assert not observer.server.server_state.tasks
        await eventually(lambda: not observer.server.server_state.connections)
    assert workspace.store.session(context.sid)["runs"] == []
    assert workspace.store.db.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0
    assert workspace.store.db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0] == 0
    # Actual ASGI HTTP after observer shutdown proves the caller's app/store remain usable.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=context.app), base_url="http://127.0.0.1"
    ) as client:
        assert (await client.get(f"/api/sessions/{context.sid}")).status_code == 200
        assert (await client.post("/api/sessions")).status_code == 200
        assert (await client.get("/__eval_observer/synthetic-unreleased-token")).status_code == 404
    if observer.url:
        address = httpx.URL(observer.url)
        assert address.port is not None
        with socket.socket() as probe:
            probe.settimeout(0.5)
            assert probe.connect_ex((str(address.host), int(address.port))) != 0
    MEASUREMENTS.append(
        {
            "case": name,
            "stop_seconds": elapsed,
            "provider_initializations": 0,
            "runs": 0,
            "shared_workspace_usable_after_stop": True,
            "owned_tasks": 0,
            "port_closed": True,
        }
    )

    if destination := os.environ.get("OBSERVER_TEST_MEASUREMENTS"):
        Path(destination).write_text(json.dumps(MEASUREMENTS, indent=2) + "\n", encoding="utf-8")


async def open_ws(context: Context, url: str) -> ClientConnection:
    client = await connect(
        url.replace("http://", "ws://") + f"/api/sessions/{context.sid}/ws",
        proxy=None,
        close_timeout=0.5,
    )
    await eventually(lambda: len(context.workspace.listeners.get(context.sid, set())) == 1)
    return client


@pytest.mark.asyncio
async def test_http_without_active_connections(context: Context) -> None:
    await start(context)
    await eventually(lambda: not context.observer.server.server_state.connections)
    started = time.monotonic()
    await asyncio.wait_for(context.observer.stop(), timeout=7)
    await asyncio.wait_for(context.observer.stop(), timeout=2)
    await assert_released(context, time.monotonic() - started, "no_active_connections")


@pytest.mark.asyncio
@pytest.mark.parametrize("client_action", ["idle", "close", "reset"])
async def test_real_websocket_lifecycle(context: Context, client_action: str) -> None:
    url = await start(context)
    client = await open_ws(context, url)
    context.observer._bootstrap_sessions["synthetic-unreleased-token"] = context.sid
    context.observer._published["synthetic-unreleased-token"] = {"session_id": context.sid}
    async with httpx.AsyncClient(base_url=url, trust_env=False) as client_http:
        bootstrap = await client_http.get("/__eval_observer/synthetic-unreleased-token")
        assert bootstrap.status_code == 200
        assert context.sid in bootstrap.text
    if client_action == "close":
        await client.close()
        await eventually(lambda: not context.workspace.listeners[context.sid])
    elif client_action == "reset":
        tcp = client.transport.get_extra_info("socket")
        linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        client.transport.abort()
        await client.wait_closed()
        await eventually(lambda: not context.workspace.listeners[context.sid])
    started = time.monotonic()
    await asyncio.wait_for(context.observer.stop(), timeout=7)
    elapsed = time.monotonic() - started
    await asyncio.wait_for(client.wait_closed(), timeout=2)
    await assert_released(context, elapsed, "websocket_" + client_action)


@pytest.mark.asyncio
async def test_uvicorn_grace_limit_cancels_blocked_asgi(context: Context) -> None:
    # A real connected ASGI fixture never reads disconnect. Uvicorn must bound its task wait.
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(websocket: WebSocket) -> None:
        await websocket.accept()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    context.app.router.routes.insert(0, WebSocketRoute("/__blocked_shutdown", blocked))
    url = await start(context)
    client = await connect(url.replace("http://", "ws://") + "/__blocked_shutdown", proxy=None)
    await asyncio.wait_for(entered.wait(), timeout=2)
    started = time.monotonic()
    await asyncio.wait_for(context.observer.stop(), timeout=7)
    elapsed = time.monotonic() - started
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await client.wait_closed()
    assert elapsed < 6.5
    await assert_released(context, elapsed, "blocked_asgi_grace_limit")


@pytest.mark.asyncio
async def test_server_exception_still_releases_everything(
    context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = await start(context)
    client = await open_ws(context, url)

    async def fail_tick(counter: int) -> bool:
        raise RuntimeError("Controlled observer server failure")

    monkeypatch.setattr(context.observer.server, "on_tick", fail_tick)
    await eventually(context.observer.task.done)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="Controlled observer server failure"):
        await asyncio.wait_for(context.observer.stop(), timeout=7)
    elapsed = time.monotonic() - started
    await client.wait_closed()
    await assert_released(context, elapsed, "server_exception")


@pytest.mark.asyncio
async def test_outer_deadline_still_releases_everything(
    context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A controlled server-shutdown stall exercises the outer watchdog on a real listener.
    url = await start(context)
    client = await open_ws(context, url)

    async def stalled_shutdown(sockets: Any = None) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(context.observer.server, "shutdown", stalled_shutdown)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(context.observer.stop(), timeout=7)
    elapsed = time.monotonic() - started
    assert elapsed < 6.5
    await client.wait_closed()
    await assert_released(context, elapsed, "outer_shutdown_deadline")


@pytest.mark.asyncio
async def test_stop_cancellation_preserves_cancel_and_releases_resources(
    context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = await start(context)
    client = await open_ws(context, url)

    async def stalled_shutdown(sockets: Any = None) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(context.observer.server, "shutdown", stalled_shutdown)
    started = time.monotonic()
    stopping = asyncio.create_task(context.observer.stop())
    await asyncio.sleep(0.05)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopping, timeout=2)
    elapsed = time.monotonic() - started
    await client.wait_closed()
    await assert_released(context, elapsed, "caller_cancellation")


@pytest.mark.asyncio
async def test_stop_before_start_is_idempotent(context: Context) -> None:
    started = time.monotonic()
    await context.observer.stop()
    await context.observer.stop()
    await assert_released(context, time.monotonic() - started, "never_started_idempotent")


@pytest.mark.asyncio
async def test_replay_disconnect_keeps_other_client_and_live_event_ids(context: Context) -> None:
    # Disable compression so retained replay exercises actual TCP backpressure.
    for index in range(2048):
        context.workspace.store.emit(
            context.sid,
            None,
            "reasoning_summary",
            {"delta": f"Synthetic retained event {index}: " + "0123456789abcdef" * 512},
        )
    before = context.workspace.store.events(context.sid)
    last = before[-1]["id"]
    url = await start(context)
    ws_url = url.replace("http://", "ws://") + f"/api/sessions/{context.sid}/ws"
    surviving = await connect(ws_url + f"?after={last}", proxy=None, compression=None)
    await eventually(lambda: len(context.workspace.listeners.get(context.sid, set())) == 1)
    replaying = await connect(ws_url, proxy=None, compression=None, max_queue=1, close_timeout=0.5)
    first = json.loads(await asyncio.wait_for(replaying.recv(), timeout=3))
    assert first == before[0]
    await eventually(lambda: len(context.workspace.listeners[context.sid]) == 2)
    await replaying.close()
    await replaying.wait_closed()
    await eventually(lambda: len(context.workspace.listeners[context.sid]) == 1)
    # The unrelated subscriber must still receive the next exact persisted event.
    event = await context.workspace.emit(
        context.sid, None, "status", {"state": "synthetic_replay_check"}
    )
    assert json.loads(await asyncio.wait_for(surviving.recv(), timeout=2)) == event
    assert event["id"] == last + 1
    assert context.workspace.store.events(context.sid) == before + [event]
    await surviving.close()
    started = time.monotonic()
    await context.observer.stop()
    await assert_released(context, time.monotonic() - started, "mid_replay_disconnect_other_client")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["error", "cancel"])
async def test_replay_failure_or_cancellation_releases_subscription(
    context: Context, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    context.workspace.store.emit(context.sid, None, "status", {"state": "synthetic_retained"})
    original_send = WebSocket.send_json
    entered, release = asyncio.Event(), asyncio.Event()

    async def controlled_send(websocket: WebSocket, data: Any, mode: str = "text") -> None:
        if websocket.url.path == f"/api/sessions/{context.sid}/ws":
            entered.set()
            await release.wait()
            raise RuntimeError("Controlled replay send failure")
        await original_send(websocket, data, mode=mode)

    monkeypatch.setattr(WebSocket, "send_json", controlled_send)
    url = await start(context)
    client = await connect(
        url.replace("http://", "ws://") + f"/api/sessions/{context.sid}/ws", proxy=None
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert len(context.workspace.listeners[context.sid]) == 1
    if failure_kind == "cancel":
        tasks = tuple(context.observer.server.server_state.tasks)
        assert len(tasks) == 1
        tasks[0].cancel()
    else:
        release.set()
    await asyncio.wait_for(client.wait_closed(), timeout=2)
    await eventually(lambda: not context.workspace.listeners[context.sid])
    started = time.monotonic()
    await context.observer.stop()
    await assert_released(context, time.monotonic() - started, "replay_" + failure_kind)


@pytest.mark.asyncio
async def test_reconnect_cursor_and_ordinary_stream_preserve_events(context: Context) -> None:
    for index in range(3):
        context.workspace.store.emit(context.sid, None, "status", {"state": f"synthetic_{index}"})
    retained = context.workspace.store.events(context.sid)
    url = await start(context)
    ws_url = url.replace("http://", "ws://") + f"/api/sessions/{context.sid}/ws"
    client = await connect(ws_url + f"?after={retained[0]['id']}", proxy=None)
    assert json.loads(await asyncio.wait_for(client.recv(), timeout=2)) == retained[1]
    assert json.loads(await asyncio.wait_for(client.recv(), timeout=2)) == retained[2]
    live = await context.workspace.emit(context.sid, None, "status", {"state": "synthetic_live"})
    assert json.loads(await asyncio.wait_for(client.recv(), timeout=2)) == live
    await client.close()
    await eventually(lambda: not context.workspace.listeners[context.sid])
    reconnected = await connect(ws_url + f"?after={retained[2]['id']}", proxy=None)
    assert json.loads(await asyncio.wait_for(reconnected.recv(), timeout=2)) == live
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(reconnected.recv(), timeout=0.1)
    next_event = await context.workspace.emit(
        context.sid, None, "status", {"state": "synthetic_after_reconnect"}
    )
    assert json.loads(await asyncio.wait_for(reconnected.recv(), timeout=2)) == next_event
    assert [row["id"] for row in context.workspace.store.events(context.sid)] == [1, 2, 3, 4, 5]
    await reconnected.close()
    started = time.monotonic()
    await context.observer.stop()
    await assert_released(context, time.monotonic() - started, "reconnect_cursor_and_live_stream")


@pytest.mark.asyncio
async def test_live_stream_cancellation_releases_sender_and_subscription(context: Context) -> None:
    url = await start(context)
    client = await open_ws(context, url)
    tasks = tuple(context.observer.server.server_state.tasks)
    assert len(tasks) == 1
    tasks[0].cancel()
    await asyncio.wait_for(client.wait_closed(), timeout=2)
    await eventually(lambda: not context.workspace.listeners[context.sid])
    pending_senders = []
    for task in asyncio.all_tasks():
        code = getattr(task.get_coro(), "cr_code", None)
        if (
            isinstance(code, CodeType)
            and code.co_filename == str(ROOT / "analyst/app.py")
            and code.co_name == "send"
        ):
            pending_senders.append(task)
    assert pending_senders == []
    started = time.monotonic()
    await context.observer.stop()
    await assert_released(context, time.monotonic() - started, "live_stream_cancellation")
