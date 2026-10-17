"""Workspace integration gates use disposable state and synthetic data only."""

import asyncio
import hashlib
from dataclasses import replace

import pytest
from analyst.app import create_app
from analyst.config import Settings
from analyst.evidence import EvidenceLedger
from analyst.store import SessionStore
from fastapi.testclient import TestClient


def settings(tmp_path, **changes):
    return replace(Settings(), state_dir=str(tmp_path / "state"), mode="mock", **changes)


class FixtureRuntime:
    def __init__(self, settings, toolkit, emit, steering=None, initial_items=None):
        self.toolkit, self.emit, self.steering = toolkit, emit, steering

    async def run(self, prompt):
        await asyncio.sleep(0.1)
        queued = await self.steering()
        await self.emit("reasoning_summary", {"delta": "Synthetic integration check"})
        await self.emit("qa", {"verified": True, "checked_claims": 0, "errors": []})
        return {
            "status": "completed",
            "history": [{"role": "user", "content": prompt}],
            "usage": [],
            "cost_usd": 0,
            "elapsed_seconds": 0.1,
            "queued": queued,
        }


def test_durable_instructions_survive_process_reopen(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create_session()["id"]
    rid, _ = store.begin_run(sid)
    store.queue_instruction(sid, "Use median instead of mean")
    store.close()
    restored = SessionStore(tmp_path)
    assert restored.recover_interrupted()[0]["data"]["state"] == "interrupted"
    assert restored.session(sid)["pending_instructions"] == 1
    next_id, _ = restored.begin_run(sid)
    assert restored.take_instructions(sid, next_id, include_unresolved=True) == [
        "Use median instead of mean"
    ]
    assert restored.take_instructions(sid, next_id) == []
    restored.finish_run(next_id, "completed")
    assert restored.session(sid)["pending_instructions"] == 0
    restored.close()


def test_dev_budget_reservation_idempotent_cost_and_uncertain_run(tmp_path):
    store = SessionStore(tmp_path)
    assert store.reserve_budget("first", 5, 10) == 5
    store.record_cost("first", "response-one", 1.25)
    store.record_cost("first", "response-one", 1.25)
    assert store.remaining_budget(10) == 5
    assert store.reserve_budget("second", 8, 10) == 5
    with pytest.raises(ValueError, match="no unreserved"):
        store.reserve_budget("third", 1, 10)
    store.settle_budget("first")
    assert store.remaining_budget(10) == 3.75
    store.close()
    restored = SessionStore(tmp_path)
    # An interrupted run keeps its entire uncertain reservation after restart.
    assert restored.remaining_budget(10) == 3.75
    with pytest.raises(ValueError):
        restored.record_cost("second", "bad", float("nan"))
    restored.close()


def test_api_upload_websocket_replay_and_origin(tmp_path):
    application = create_app(settings(tmp_path), runtime_factory=FixtureRuntime)
    with TestClient(application) as client:
        sid = client.post("/api/sessions").json()["id"]
        uploaded = client.post(
            f"/api/sessions/{sid}/files",
            files={"files": ("disposable.csv", b"group,value\na,2\nb,4\n", "text/csv")},
        )
        assert uploaded.status_code == 200
        assert (
            uploaded.json()["files"][0]["sha256"]
            == hashlib.sha256(b"group,value\na,2\nb,4\n").hexdigest()
        )
        assert "path" not in uploaded.json()["files"][0]
        with client.websocket_connect(f"/api/sessions/{sid}/ws") as socket:
            socket.send_json({"type": "prompt", "text": "Explore this disposable sample"})
            seen = []
            while True:
                event = socket.receive_json()
                seen.append(event)
                if event["type"] == "status" and event["data"]["state"] == "completed":
                    break
        assert any(item["type"] == "qa" and item["data"]["status"] == "verified" for item in seen)
        after = seen[-2]["id"]
        with client.websocket_connect(f"/api/sessions/{sid}/ws?after={after}") as socket:
            assert socket.receive_json()["id"] == seen[-1]["id"]
        assert (
            client.post("/api/sessions", headers={"Origin": "https://outside.invalid"}).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/sessions/{sid}/files",
                files={"files": ("script.exe", b"fake", "application/octet-stream")},
            ).status_code
            == 415
        )
        assert client.get("/api/config").json()["mode"] == "mock"
        assert "api_key" not in client.get("/api/config").text


def test_manifest_only_artifacts_and_hash_readback(tmp_path):
    application = create_app(settings(tmp_path))
    with TestClient(application) as client:
        store = application.state.workspace.store
        sid = store.create_session()["id"]
        rid, directory = store.begin_run(sid)
        folder = directory / "artifacts"
        folder.mkdir()
        report = folder / "report.txt"
        report.write_text("Synthetic report", encoding="utf-8")
        ledger = EvidenceLedger(directory)
        ledger.register_artifact(report)
        base = f"/api/sessions/{sid}/runs/{rid}/artifacts/"
        assert client.get(base + "artifacts/report.txt").status_code == 200
        (directory / "internal.txt").write_text("private state")
        assert client.get(base + "internal.txt").status_code == 404
        report.write_text("changed")
        assert client.get(base + "artifacts/report.txt").status_code == 404
        other = store.create_session()["id"]
        assert (
            client.get(
                f"/api/sessions/{other}/runs/{rid}/artifacts/artifacts/report.txt"
            ).status_code
            == 404
        )


def test_provider_stop_cannot_reenter_affected_conversation(tmp_path):
    application = create_app(settings(tmp_path), runtime_factory=FixtureRuntime)
    with TestClient(application) as client:
        sid = client.post("/api/sessions").json()["id"]
        application.state.workspace.store.stop_session(sid, "Provider stop")
        response = client.post(f"/api/sessions/{sid}/prompt", json={"text": "try again"})
        assert response.status_code == 422
        assert "cannot be retried" in response.json()["detail"]


def test_terminal_report_exposes_unverified_reason(tmp_path):
    import asyncio

    application = create_app(settings(tmp_path))
    with TestClient(application):
        workspace = application.state.workspace
        sid = workspace.store.create_session()["id"]
        asyncio.run(
            workspace.emit(
                sid,
                None,
                "report",
                {
                    "report": "Analysis could not be verified.",
                    "qa": {
                        "verified": False,
                        "errors": ["Remaining budget cannot cover another request"],
                    },
                    "claims": [],
                    "simulation": True,
                },
            )
        )
        events = workspace.store.events(sid)
        assert [event["type"] for event in events] == ["qa", "message"]
        assert events[0]["data"]["status"] == "unverified"
        assert events[0]["data"]["issues"] == ["Remaining budget cannot cover another request"]


def test_retired_provider_webhook_endpoint_is_unavailable(tmp_path):
    application = create_app(settings(tmp_path), runtime_factory=FixtureRuntime)
    with TestClient(application) as client:
        assert client.post("/api/webhooks/openai", json={}).status_code == 404
