"""Interrupted prompt recovery regressions; synthetic inputs and disposable state only."""

import asyncio
import json
import threading
import time

import pytest
from analyst.app import create_app
from analyst.config import Settings
from fastapi.testclient import TestClient


def _wait(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Synthetic runtime did not reach the expected boundary")


def _settings(tmp_path):
    return Settings(
        state_dir=str(tmp_path / "workspace"),
        mode="mock",
        enable_file_search=False,
        memory_enabled=False,
    )


def _upload(client, sid):
    response = client.post(
        f"/api/sessions/{sid}/files",
        files={"files": ("disposable.csv", b"region,value\na,3\nb,7\n", "text/csv")},
    )
    assert response.status_code == 200


def _recorder():
    class Recorder:
        calls = []

        def __init__(self, settings, toolkit, emit, steering=None, initial_items=None):
            self.initial_items = initial_items or []

        async def run(self, prompt):
            self.calls.append({"prompt": prompt, "initial_items": self.initial_items})
            return {
                "status": "completed",
                "history": self.initial_items + [{"role": "user", "content": prompt}],
                "usage": [],
                "cost_usd": 0,
            }

    return Recorder


def test_restart_preserves_original_objective_without_autoexecution(tmp_path):
    entered, applied = threading.Event(), threading.Event()

    class InterruptedRuntime:
        def __init__(self, settings, toolkit, emit, steering=None, initial_items=None):
            self.steering = steering

        async def run(self, prompt):
            entered.set()
            while not await self.steering():
                await asyncio.sleep(0.01)
            applied.set()
            await asyncio.Event().wait()

    settings = _settings(tmp_path)
    original = "ORIGINAL_OBJECTIVE compare regional sales and investigate the decline"
    updated = "STEERING use median"
    with TestClient(create_app(settings, runtime_factory=InterruptedRuntime)) as client:
        sid = client.post("/api/sessions").json()["id"]
        _upload(client, sid)
        assert (
            client.post(f"/api/sessions/{sid}/prompt", json={"text": original}).status_code == 200
        )
        _wait(entered.is_set)
        assert client.post(f"/api/sessions/{sid}/prompt", json={"text": updated}).json()["queued"]
        _wait(applied.is_set)

    recorder = _recorder()
    with TestClient(create_app(settings, runtime_factory=recorder)) as client:
        snapshot = client.get(f"/api/sessions/{sid}").json()
        assert snapshot["runs"][-1]["state"] == "interrupted"
        assert recorder.calls == [], "Restart must not execute an interrupted request automatically"
        assert (
            client.post(f"/api/sessions/{sid}/prompt", json={"text": "Continue"}).status_code == 200
        )
        _wait(lambda: bool(recorder.calls))
        call = recorder.calls[0]
        actual_input = json.dumps(call, ensure_ascii=False)
        assert actual_input.count(original) == 1, (
            "Recovery must include the original objective exactly once"
        )
        assert actual_input.count(updated) == 1, "Applied steering must recover exactly once"
        assert actual_input.count("Continue") == 1
        _wait(lambda: client.get(f"/api/sessions/{sid}").json()["runs"][-1]["state"] == "completed")
        assert client.get(f"/api/sessions/{sid}").json()["pending_instructions"] == 0


def test_completed_turn_is_not_duplicated_when_starting_next_run(tmp_path):
    recorder = _recorder()
    with TestClient(create_app(_settings(tmp_path), runtime_factory=recorder)) as client:
        sid = client.post("/api/sessions").json()["id"]
        _upload(client, sid)
        first = "COMPLETED_OBJECTIVE describe the sample"
        client.post(f"/api/sessions/{sid}/prompt", json={"text": first})
        _wait(lambda: client.get(f"/api/sessions/{sid}").json()["runs"][-1]["state"] == "completed")
        client.post(
            f"/api/sessions/{sid}/prompt", json={"text": "NEXT_OBJECTIVE use the same inputs"}
        )
        _wait(lambda: len(recorder.calls) == 2)
        actual_input = json.dumps(recorder.calls[1], ensure_ascii=False)
        assert actual_input.count(first) == 1
        assert actual_input.count("NEXT_OBJECTIVE") == 1


def test_repeated_interruption_does_not_duplicate_carried_instruction(tmp_path):
    entered, consumed = threading.Event(), threading.Event()

    class InterruptedRuntime:
        def __init__(self, settings, toolkit, emit, steering=None, initial_items=None):
            self.steering = steering

        async def run(self, prompt):
            entered.set()
            while not await self.steering():
                await asyncio.sleep(0.01)
            consumed.set()
            await asyncio.Event().wait()

    settings = _settings(tmp_path)
    original = "REPEATED_ORIGINAL compare the synthetic regions"
    instruction = "REPEATED_STEERING prefer robust summaries"
    with TestClient(create_app(settings, runtime_factory=InterruptedRuntime)) as client:
        sid = client.post("/api/sessions").json()["id"]
        _upload(client, sid)
        client.post(f"/api/sessions/{sid}/prompt", json={"text": original})
        _wait(entered.is_set)
        client.post(f"/api/sessions/{sid}/prompt", json={"text": instruction})
        _wait(consumed.is_set)

    entered.clear()
    with TestClient(create_app(settings, runtime_factory=InterruptedRuntime)) as client:
        client.post(f"/api/sessions/{sid}/prompt", json={"text": "EXPLICIT_RESUME_A"})
        _wait(entered.is_set)
        # Stop this resumed request before it can return any final history.

    recorder = _recorder()
    with TestClient(create_app(settings, runtime_factory=recorder)) as client:
        assert recorder.calls == []
        client.post(f"/api/sessions/{sid}/prompt", json={"text": "EXPLICIT_RESUME_B"})
        _wait(lambda: bool(recorder.calls))
        actual_input = json.dumps(recorder.calls[0], ensure_ascii=False)
        for token in (original, instruction, "EXPLICIT_RESUME_A", "EXPLICIT_RESUME_B"):
            assert actual_input.count(token) == 1, (token, actual_input)


@pytest.mark.parametrize("terminal_state", ["budget_exhausted", "unverified", "failed"])
def test_noncompleted_run_retains_queued_prompt_until_explicit_resume(tmp_path, terminal_state):
    entered, release = threading.Event(), threading.Event()

    class TerminalRuntime:
        calls = []

        def __init__(self, settings, toolkit, emit, steering=None, initial_items=None):
            self.initial_items = initial_items or []

        async def run(self, prompt):
            self.calls.append(prompt)
            first = len(self.calls) == 1
            if first:
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            return {
                "status": terminal_state if first else "completed",
                "history": self.initial_items + [{"role": "user", "content": prompt}],
                "usage": [],
                "cost_usd": 0,
            }

    with TestClient(create_app(_settings(tmp_path), runtime_factory=TerminalRuntime)) as client:
        sid = client.post("/api/sessions").json()["id"]
        _upload(client, sid)
        client.post(f"/api/sessions/{sid}/prompt", json={"text": "INITIAL_REQUEST"})
        _wait(entered.is_set)
        queued = client.post(f"/api/sessions/{sid}/prompt", json={"text": "PENDING_CHANGE"})
        assert queued.json()["queued"] is True
        release.set()
        _wait(
            lambda: client.get(f"/api/sessions/{sid}").json()["runs"][-1]["state"] == terminal_state
        )
        # Give a mistakenly auto-created successor enough time to enter its runtime.
        time.sleep(0.05)
        snapshot = client.get(f"/api/sessions/{sid}").json()
        assert len(snapshot["runs"]) == 1
        assert TerminalRuntime.calls == ["INITIAL_REQUEST"]
        assert snapshot["pending_instructions"] == 1

        response = client.post(f"/api/sessions/{sid}/prompt", json={"text": "EXPLICIT_CONTINUE"})
        assert response.status_code == 200
        _wait(lambda: len(TerminalRuntime.calls) == 2)
        assert TerminalRuntime.calls[1].count("PENDING_CHANGE") == 1
        assert TerminalRuntime.calls[1].count("EXPLICIT_CONTINUE") == 1
        _wait(lambda: client.get(f"/api/sessions/{sid}").json()["runs"][-1]["state"] == "completed")
        assert client.get(f"/api/sessions/{sid}").json()["pending_instructions"] == 0
