"""No-provider smoke for the eval-only observer; not browser/model evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fingerprint(root):
    paths = []
    for directory, extension in (
        ("analyst", "*.py"),
        ("scripts", "*.py"),
        ("frontend/src", "*"),
        ("frontend/scripts", "*"),
    ):
        paths.extend(path for path in (root / directory).rglob(extension) if path.is_file())
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(set(paths))
    }


async def main(root, output):
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "scripts"))
    import eval_browser_observer
    import httpx
    from analyst.app import create_app
    from analyst.case_study.preparation import build_snapshot
    from analyst.config import Settings
    from analyst.dashboard import AnalysisService
    from eval_browser_observer import EvalBrowserObserver, _js_string
    from websockets.asyncio.client import connect

    assert (
        Path(eval_browser_observer.__file__).resolve()
        == (root / "scripts/eval_browser_observer.py").resolve()
    )

    before = fingerprint(root)
    checks, flows, network = [], [], []
    generation_attempts = []

    def no_generation(*args, **kwargs):
        generation_attempts.append("blocked")
        raise AssertionError("A provider/runtime constructor was reached")

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    loop = asyncio.get_running_loop()
    real_async_connect = loop.create_connection
    real_sock_connect = loop.sock_connect

    def local_only(address):
        if not isinstance(address, tuple) or address[0] not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise AssertionError("Non-loopback network denied: " + repr(address))
        network.append(list(address))

    def guarded_connect(sock, address):
        local_only(address)
        return real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        local_only(address)
        return real_connect_ex(sock, address)

    def guarded_dns(host, *args, **kwargs):
        if host not in {"127.0.0.1", "::1", "localhost", b"127.0.0.1"}:
            raise AssertionError("Non-loopback DNS denied: " + repr(host))
        return real_getaddrinfo(host, *args, **kwargs)

    async def guarded_async_connect(factory, host=None, port=None, **kwargs):
        if host is not None:
            local_only((host, port))
        elif kwargs.get("sock"):
            local_only(kwargs["sock"].getpeername())
        else:
            raise AssertionError("Async TCP connection has no checked loopback address")
        return await real_async_connect(factory, host, port, **kwargs)

    async def guarded_sock_connect(sock, address):
        local_only(address)
        return await real_sock_connect(sock, address)

    master = (
        b"product_id,product_name,category,production_line,monthly_output\n"
        b"1,Offline Widget,Category 1,Line-3,100\n"
        b"2,Offline Part,Category 2,Line-2,80\n"
    )

    def event(identifier, **changes):
        return {
            "defect_id": identifier,
            "product_id": 1,
            "defect_type": "Functional",
            "defect_date": "2024-01-15",
            "defect_location": "Component",
            "severity": "Critical",
            "inspection_method": "Visual Inspection",
            "repair_cost": 10,
            **changes,
        }

    rows = [
        event(1),
        event(2, defect_date="2024-06-02", severity="Minor", repair_cost=120),
        event(3, defect_date="2024-06-03", repair_cost=80),
        event(4, product_id=2, repair_cost=200),
    ]
    encoded = json.dumps(rows, sort_keys=True).encode()
    snapshot = build_snapshot(
        rows,
        {
            "source_url": "https://synthetic.invalid/offline-observer",
            "source_sha256": hashlib.sha256(encoded).hexdigest(),
            "fetched_at": "2026-10-17T00:00:00+00:00",
        },
        master,
    )
    analysis = AnalysisService(root, output / "analysis", snapshot_loader=lambda: snapshot)
    settings = Settings(
        root=root,
        mode="auto",
        api_key="",
        state_dir=str(output / "workspace"),
        memory_enabled=False,
    )
    settings.validate()
    app = create_app(settings, runtime_factory=no_generation, analysis_service=analysis)
    workspace = app.state.workspace
    observer = EvalBrowserObserver(
        app,
        workspace,
        output / "observer",
        marker_evidence_mode="offline_simulated_client",
    )
    sessions = []

    def marker(record, **changes):
        ready = {
            **record["ready_marker_template"],
            "evidence": [str(output / "smoke-proof.json")],
            **changes,
        }
        write(Path(record["ready_marker"]), ready)

    async def must_timeout(record, reason):
        try:
            await observer.wait(record, timeout_seconds=0.15)
        except TimeoutError as error:
            assert reason in str(error), str(error)
            checks.append({"check": "fail_closed_" + reason, "status": "Passed"})
        else:
            raise AssertionError("Barrier released without " + reason)

    async def receive_ui(ws, client, browser):
        while True:
            event_value = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if event_value["type"] != "ui_command":
                continue
            command = event_value["data"]
            assert command["expected_context_id"] == browser["context"]["context_id"], (
                "Client CAS failed"
            )
            response = await client.get("/api/dashboard/contexts/" + command["context_id"])
            response.raise_for_status()
            browser["context"] = response.json()["context"]
            browser["commands"].append(command)
            synced = await client.post(
                "/api/sessions/" + browser["sid"] + "/scope",
                json={"context_id": command["context_id"]},
            )
            synced.raise_for_status()
            # Initial/same-context synchronization must not clear the actual command.
            assert synced.json()["ui"]["last_command_id"] == command["command_id"]
            return command

    with ExitStack() as stack:
        stack.enter_context(patch.object(socket.socket, "connect", guarded_connect))
        stack.enter_context(patch.object(socket.socket, "connect_ex", guarded_connect_ex))
        stack.enter_context(patch.object(socket, "getaddrinfo", guarded_dns))
        stack.enter_context(patch.object(loop, "create_connection", guarded_async_connect))
        stack.enter_context(patch.object(loop, "sock_connect", guarded_sock_connect))
        stack.enter_context(patch("openai.AsyncOpenAI", no_generation))
        stack.enter_context(patch("openai.OpenAI", no_generation))
        try:
            await observer.start()
            assert app.state.workspace is workspace and observer.server.config.app is app
            checks.append(
                {
                    "check": "same_app_workspace_object_same_loop_lifespan_off",
                    "status": "Passed",
                }
            )
            assert observer.server.config.lifespan == "off"
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1",
                    trust_env=False,
                ) as asgi,
                httpx.AsyncClient(base_url=observer.url, timeout=3, trust_env=False) as tcp,
            ):
                config = (await tcp.get("/api/config")).json()
                assert config["mode"] == "unavailable" and config["ai_available"] is False
                for uri, digest in observer.assets.items():
                    served = await tcp.get(uri)
                    served.raise_for_status()
                    assert hashlib.sha256(served.content).hexdigest() == digest
                checks.append(
                    {
                        "check": "actual_product_index_and_all_assets_served_hashes",
                        "status": "Passed",
                        "count": len(observer.assets),
                    }
                )

                for case_id, initial_filters, change in (
                    (
                        "R02",
                        {"production_line": "Line-3"},
                        {"severity": "Critical"},
                    ),
                    ("R03", {}, {"scenario": "exclude_cost_10"}),
                ):
                    initial = (await asgi.get("/api/dashboard", params=initial_filters)).json()
                    sid = (await asgi.post("/api/sessions")).json()["id"]
                    sessions.append(sid)
                    cid = initial["context"]["context_id"]
                    await workspace.set_ui_scope(sid, cid)
                    record = observer.publish(case_id, sid, initial)
                    html = await tcp.get(record["bootstrap_url"])
                    html.raise_for_status()
                    assert "no-store" in html.headers["cache-control"]
                    nonce = re.search(r'<script nonce="([^"]+)">', html.text)[1]
                    assert "nonce-" + nonce in html.headers["content-security-policy"]
                    assert (await tcp.get("/__eval_observer/not-a-token")).status_code == 404
                    script = re.search(r'<script nonce="[^"]+">(.*?)</script>', html.text)[1]
                    node = shutil.which("node")
                    assert node, "Existing Node runtime required; no installation attempted"
                    driver = (
                        'const fs=require("node:fs"),vm=require("node:vm");'
                        'const code=fs.readFileSync(0,"utf8");const state={"analyst.workspace.draft.v2":"old"};'
                        "let redirect;vm.runInNewContext(code,{localStorage:{setItem:(k,v)=>state[k]=v,removeItem:k=>delete state[k]},"
                        "window:{location:{replace:v=>redirect=v}}});process.stdout.write(JSON.stringify({state,redirect}));"
                    )
                    executed = await asyncio.to_thread(
                        subprocess.run,
                        [node, "-e", driver],
                        input=script,
                        text=True,
                        capture_output=True,
                        check=True,
                    )
                    bootstrap = json.loads(executed.stdout)
                    assert bootstrap == {
                        "state": {"analyst.workspace.session.v1": sid},
                        "redirect": "/",
                    }
                    assert "</script" not in _js_string('</script><img src=x onerror="bad">')
                    assert workspace.store.session(sid)["runs"] == []
                    marker(record)
                    if case_id == "R02":
                        await must_timeout(record, "no live WebSocket")
                    cursor = workspace.store.events(sid)[-1]["id"]
                    ws_url = (
                        observer.url.replace("http:", "ws:")
                        + f"/api/sessions/{sid}/ws?after={cursor}"
                    )
                    async with connect(ws_url, origin=observer.url) as ws:
                        for _ in range(30):
                            if workspace.listeners.get(sid):
                                break
                            await asyncio.sleep(0.01)
                        assert workspace.listeners.get(sid)
                        assert (await tcp.get("/api/sessions/" + sid)).json()["id"] == sid
                        if case_id == "R02":
                            Path(record["ready_marker"]).unlink()
                            await must_timeout(record, "root marker absent")
                            marker(record)
                            other = (await asgi.get("/api/dashboard", params={})).json()
                            await workspace.set_ui_scope(sid, other["context"]["context_id"])
                            await must_timeout(record, "UI context differs")
                            await workspace.set_ui_scope(sid, cid)
                            marker(record, barrier_id="wrong")
                            try:
                                await observer.wait(record, timeout_seconds=0.15)
                            except ValueError:
                                checks.append(
                                    {
                                        "check": "fail_closed_wrong_root_marker",
                                        "status": "Passed",
                                    }
                                )
                            else:
                                raise AssertionError("Wrong marker accepted")
                            marker(record)
                        released = await observer.wait(record, timeout_seconds=2)
                        assert released["listener_count"] == 1
                        assert (await tcp.get(record["bootstrap_url"])).status_code == 404
                        browser = {
                            "sid": sid,
                            "context": initial["context"],
                            "commands": [],
                        }
                        rid = "offline-simulated-" + case_id
                        workspace.run_languages[rid] = "en"
                        workspace.ui_authorities[rid] = {
                            "view": True,
                            "undo": True,
                            "filters": change,
                        }
                        workspace.ui_bindings[rid] = cid
                        command = (
                            await workspace.execute_ui_command(
                                sid,
                                rid,
                                cid,
                                {
                                    "action": "apply_scope",
                                    "panel": "records",
                                    "filters": change,
                                    "command_id": None,
                                },
                            )
                        )["command"]
                        adopted = await receive_ui(ws, tcp, browser)
                        assert all(adopted.get(key) == value for key, value in command.items())
                        expected_filters = {
                            **initial["context"]["filters"],
                            **change,
                        }
                        assert browser["context"]["filters"] == expected_filters
                        boundary = observer.publish(
                            case_id,
                            sid,
                            browser["context"],
                            phase="turn_1_complete",
                            command_id=command["command_id"],
                        )
                        marker(boundary)
                        await observer.wait(boundary, timeout_seconds=2)
                        if case_id == "R02":
                            response = await tcp.post(
                                f"/api/sessions/{sid}/ui-commands/{command['command_id']}/undo",
                                json={"expected_context_id": command["context_id"]},
                            )
                            response.raise_for_status()
                            inverse = response.json()["command"]
                            undo_path = "actual_REST_undo_endpoint"
                        else:
                            inverse = (
                                await workspace.execute_ui_command(
                                    sid,
                                    rid,
                                    cid,
                                    {
                                        "action": "undo",
                                        "panel": "dashboard",
                                        "filters": {},
                                        "command_id": None,
                                    },
                                )
                            )["command"]
                            undo_path = "actual_model_UI_handler_with_simulated_command"
                        received_inverse = await receive_ui(ws, tcp, browser)
                        assert all(
                            received_inverse.get(key) == value for key, value in inverse.items()
                        )
                        assert inverse["action"] == "undo"
                        assert browser["context"]["context_id"] == cid
                        assert browser["context"]["filters"] == initial["context"]["filters"]
                        complete = observer.publish(
                            case_id,
                            sid,
                            browser["context"],
                            phase="turn_2_complete",
                            command_id=inverse["command_id"],
                        )
                        marker(complete)
                        await observer.wait(complete, timeout_seconds=2)
                        flows.append(
                            {
                                "case": case_id,
                                "evidence_mode": "offline_simulated_client",
                                "session_id": sid,
                                "initial_context_id": cid,
                                "adopted_context_id": command["context_id"],
                                "restored_context_id": browser["context"]["context_id"],
                                "adopted_filters": expected_filters,
                                "command_id": command["command_id"],
                                "undo_command_id": inverse["command_id"],
                                "undo_path": undo_path,
                                "bootstrap_js_executed_in": "Node vm, not a browser",
                                "model_generation": False,
                                "real_React_DOM": False,
                                "status": "Passed",
                            }
                        )
                    assert workspace.store.session(sid)["runs"] == []
            last_sid = sessions[-1]
            shutdown_ws = await connect(
                observer.url.replace("http:", "ws:") + f"/api/sessions/{last_sid}/ws"
            )
            for _ in range(30):
                if workspace.listeners.get(last_sid):
                    break
                await asyncio.sleep(0.01)
            assert workspace.listeners.get(last_sid)
            await asyncio.wait_for(observer.stop(), timeout=5)
            await asyncio.wait_for(shutdown_ws.wait_closed(), timeout=2)
            assert not workspace.listeners.get(last_sid)
            assert not workspace.closed
            assert not workspace.tasks and not generation_attempts
            assert network, "Real TCP connections must pass a checked loopback guard"
            checks.append(
                {
                    "check": "observer_shutdown_preserves_workspace_and_zero_generation",
                    "status": "Passed",
                }
            )
        finally:
            if observer.task and not observer.task.done():
                await observer.stop()
            await workspace.close()
    after = fingerprint(root)
    assert before == after, "Frozen functional source changed during smoke"
    proof = {
        "schema": "eval-observer-offline-smoke-v1",
        "status": "Passed",
        "evidence_mode": "offline_simulated_client",
        "limitations": [
            "No actual CUA/browser execution",
            "No real model-issued command",
            "No React DOM adoption assertion",
            "Offline synthetic data is test-only and is not product source fallback",
        ],
        "checks": checks,
        "flows": flows,
        "generation_attempts": generation_attempts,
        "provider_spend_usd": 0,
        "network_scope": "loopback_only_enforced",
        "loopback_connection_count": len(network),
        "functional_source_files_unchanged": len(before),
        "assets": observer.assets,
        "observer_url_was": observer.url,
        "observer_stopped": observer.task.done(),
        "helper_sha256": hashlib.sha256(
            (root / "scripts/eval_browser_observer.py").read_bytes()
        ).hexdigest(),
    }
    write(output / "smoke-proof.json", proof)
    print(
        json.dumps(
            {
                "event": "offline_smoke_complete",
                "proof": str(output / "smoke-proof.json"),
                "flows": len(flows),
                "checks": len(checks),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(args.root.resolve(), args.output.resolve()))
