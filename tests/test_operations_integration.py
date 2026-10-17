"""Operations wiring uses disposable state, synthetic receipts and fake runtimes."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from analyst.case_study.preparation import build_snapshot
from analyst.config import Settings
from analyst.dashboard import AnalysisService
from analyst.evidence import canonical, sha256
from analyst.store import SessionStore
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
MASTER = (
    b"product_id,product_name,category,production_line,monthly_output\n"
    b"1,Synthetic Widget,Fixture Category,Fixture Line,100\n"
)


@pytest.fixture
def create_workspace(tmp_path, monkeypatch):
    # app.py also constructs a default ASGI app on import. Keep that incidental
    # database disposable when this integration module is run by itself.
    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls: cls(state_dir=str(tmp_path / "import-state"), mode="mock")),
    )
    from analyst.app import create_app

    return create_app


def settings(tmp_path):
    return Settings(
        root=ROOT,
        state_dir=str(tmp_path / "state"),
        mode="mock",
        memory_enabled=False,
        enable_file_search=False,
    )


def analysis_service(tmp_path):
    rows = [
        {
            "defect_id": 1,
            "product_id": 1,
            "defect_type": "Functional",
            "defect_date": "2024-01-15",
            "defect_location": "Component",
            "severity": "Critical",
            "inspection_method": "Visual Inspection",
            "repair_cost": 10,
        },
        {
            "defect_id": 2,
            "product_id": 1,
            "defect_type": "Functional",
            "defect_date": "2024-02-15",
            "defect_location": "Component",
            "severity": "Minor",
            "inspection_method": "Visual Inspection",
            "repair_cost": 0,
        },
    ]
    snapshot = build_snapshot(
        rows,
        {
            "source_url": "https://synthetic.invalid/operations",
            "source_sha256": sha256(canonical(rows)),
            "fetched_at": "2026-01-01T00:00:00+00:00",
        },
        MASTER,
    )
    return AnalysisService(ROOT, tmp_path / "analysis", snapshot_loader=lambda: snapshot)


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Expected synthetic runtime boundary was not reached")


def completed_runs(client, sid, count=1, state="completed"):
    def finished():
        runs = client.get(f"/api/sessions/{sid}").json()["runs"]
        return len(runs) == count and all(run["state"] == state for run in runs)

    wait_for(finished)
    return client.get(f"/api/sessions/{sid}").json()


def context(client):
    response = client.get("/api/dashboard")
    assert response.status_code == 200, response.text
    return response.json()


def send_rejected(client, sid, transport, body):
    if transport == "http":
        response = client.post(f"/api/sessions/{sid}/prompt", json=body)
        assert response.status_code == 422, response.text
        return response.json()["detail"]
    session = client.get(f"/api/sessions/{sid}").json()
    after = session["events"][-1]["id"] if session["events"] else 0
    with client.websocket_connect(f"/api/sessions/{sid}/ws?after={after}") as socket:
        socket.send_json({"type": "prompt", **body})
        event = socket.receive_json()
        assert event["type"] == "error", event
        return event["data"]["message"]


def assert_no_request_started(client, sid):
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["runs"] == []
    assert session["pending_instructions"] == 0
    assert not any(
        event["type"] == "message" and event["data"].get("role") == "user"
        for event in session["events"]
    )
    assert client.get("/api/operations/cases").json()["cases"] == []


def test_operations_routes_share_the_dashboard_service_and_survive_app_reopen(
    tmp_path,
    monkeypatch,
    create_workspace,
):
    service = analysis_service(tmp_path)
    app = create_workspace(settings(tmp_path), analysis_service=service)
    operations = app.state.workspace.operations
    assert operations.analysis is service
    assert operations.path == tmp_path / "state" / "operations.sqlite3"
    closed = []
    original_close = operations.close

    def record_close():
        closed.append(True)
        original_close()

    monkeypatch.setattr(operations, "close", record_close)
    with TestClient(app) as client:
        payload = context(client)
        cid = payload["context"]["context_id"]
        base = "/api/operations/cases"
        assert client.get(base).json() == {"cases": [], "total": 0}
        body = {
            "context_id": cid,
            "title": "Inspect synthetic component",
            "idempotency_key": "integration-create-1",
        }
        blocked = client.post(base, json=body, headers={"Origin": "https://outside.invalid"})
        assert blocked.status_code == 403
        assert client.get(base).json()["total"] == 0
        created = client.post(base, json=body)
        assert created.status_code == 200, created.text
        case = created.json()
        assert case["context"] == payload["context"]
        assert case["provenance"]["execution_id"] == payload["evidence"]["execution_id"]
        assert case["status"] == "draft"
        case_url = f"{base}/{case['id']}"

        edited = client.patch(
            case_url,
            json={
                "expected_version": case["version"],
                "changes": {"description": "Synthetic review only"},
            },
        )
        assert edited.status_code == 200, edited.text
        case = edited.json()
        noted = client.post(
            case_url + "/notes",
            json={"expected_version": case["version"], "text": "Review recorded locally"},
        )
        assert noted.status_code == 200, noted.text
        case = noted.json()
        transitioned = client.post(
            case_url + "/transitions",
            json={"expected_version": case["version"], "status": "acknowledged", "confirmed": True},
        )
        assert transitioned.status_code == 200, transitioned.text
        case = transitioned.json()
        followed = client.post(
            case_url + "/follow-up",
            json={"expected_version": case["version"], "context_id": cid, "confirmed": True},
        )
        assert followed.status_code == 200, followed.text
        case = followed.json()
        assert case["follow_up"]["status"] == "unchanged"
        assert case["context"] == payload["context"]
        assert [item["kind"] for item in case["history"]] == [
            "created",
            "updated",
            "note_added",
            "transitioned",
            "follow_up_added",
        ]

        checked = client.post("/api/operations/check-source", json={"context_id": cid})
        assert checked.status_code == 200, checked.text
        assert checked.json()["status"] == "unchanged"
        checks = client.get("/api/operations/source-checks", params={"context_id": cid})
        assert checks.status_code == 200, checks.text
        assert [item["id"] for item in checks.json()["checks"]] == [checked.json()["id"]]
        assert client.get(base).json()["total"] == 1
        assert client.get(case_url).json() == case

    assert app.state.workspace.closed is True
    assert closed == [True]
    reopened = create_workspace(settings(tmp_path), analysis_service=analysis_service(tmp_path))
    with TestClient(reopened) as client:
        assert client.get(case_url).json() == case
        assert len(client.get("/api/operations/source-checks").json()["checks"]) == 1


@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_invalid_action_intents_are_rejected_before_user_event_or_run(
    tmp_path,
    create_workspace,
    transport,
):
    app = create_workspace(settings(tmp_path), analysis_service=analysis_service(tmp_path))
    with TestClient(app) as client:
        cid = context(client)["context"]["context_id"]
        sid = client.post("/api/sessions").json()["id"]
        for invalid in (None, True, 1, "", "NONE", "draft_delete", {}, []):
            message = send_rejected(
                client,
                sid,
                transport,
                {"text": "Synthetic request", "analysis_context_id": cid, "action_intent": invalid},
            )
            assert "action_intent" in message
            assert_no_request_started(client, sid)


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("intent", ["draft_create", "draft_update"])
def test_write_intent_requires_an_existing_verified_canonical_context(
    tmp_path,
    create_workspace,
    transport,
    intent,
):
    service = analysis_service(tmp_path)
    app = create_workspace(settings(tmp_path), analysis_service=service)
    with TestClient(app) as client:
        cid = context(client)["context"]["context_id"]
        sid = client.post("/api/sessions").json()["id"]
        for invalid in (None, "f" * 64, "a" * 16, "../escape", {}, ""):
            body = {"text": "Synthetic work item", "action_intent": intent}
            if invalid is not None:
                body["analysis_context_id"] = invalid
            send_rejected(client, sid, transport, body)
            assert_no_request_started(client, sid)

        # A valid identifier alone is insufficient after its evidence changes.
        artifact = service.contexts_dir / cid / "artifacts" / "cleaned_records.csv"
        assert artifact.is_relative_to(tmp_path)
        artifact.write_text("tampered synthetic fixture\n", encoding="utf-8")
        message = send_rejected(
            client,
            sid,
            transport,
            {"text": "Synthetic work item", "analysis_context_id": cid, "action_intent": intent},
        )
        assert "evidence" in message
        assert_no_request_started(client, sid)


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("intent", ["draft_create", "draft_update"])
def test_action_intent_reaches_runtime_user_event_and_durable_run(
    tmp_path,
    create_workspace,
    transport,
    intent,
):
    calls = []

    class Runtime:
        def __init__(self, settings, toolkit, emit, *, steering, initial_items, **options):
            assert settings.live is False
            assert all(path.is_relative_to(tmp_path) for path in toolkit.input_paths)
            self.options, self.steering = options, steering

        async def run(self, prompt):
            calls.append({"prompt": prompt, **self.options, "steering": await self.steering()})
            return {"status": "completed", "usage": [], "cost_usd": 0}

    service = analysis_service(tmp_path)
    app = create_workspace(settings(tmp_path), runtime_factory=Runtime, analysis_service=service)
    with TestClient(app) as client:
        payload = context(client)
        cid = payload["context"]["context_id"]
        sid = client.post("/api/sessions").json()["id"]
        body = {
            "text": "Chuẩn bị bản nháp kiểm thử",
            "analysis_context_id": cid,
            "language": "vi",
            "action_intent": intent,
        }
        if transport == "http":
            response = client.post(f"/api/sessions/{sid}/prompt", json=body)
            assert response.status_code == 200, response.text
            assert response.json() == {"queued": False}
        else:
            with client.websocket_connect(f"/api/sessions/{sid}/ws") as socket:
                socket.send_json({"type": "prompt", **body})
                while True:
                    event = socket.receive_json()
                    if event["type"] == "status" and event["data"]["state"] in {
                        "completed",
                        "failed",
                    }:
                        assert event["data"]["state"] == "completed", event
                        break
        session = completed_runs(client, sid)
        assert len(calls) == 1
        assert calls[0]["action_intent"] == intent
        assert calls[0]["language"] == "vi"
        assert calls[0]["analysis_context"] == payload
        assert calls[0]["analysis_service"] is service
        assert calls[0]["operations_service"] is app.state.workspace.operations
        assert calls[0]["steering"] == []
        user_events = [
            event["data"]
            for event in session["events"]
            if event["type"] == "message" and event["data"]["role"] == "user"
        ]
        assert user_events == [{"role": "user", **body}]
        assert session["runs"][0]["action_intent"] == intent
        assert session["runs"][0]["analysis_context_id"] == cid
        assert session["runs"][0]["language"] == "vi"

    restored = SessionStore(tmp_path / "state")
    try:
        assert restored.session(sid)["runs"] == session["runs"]
        assert [event for event in restored.events(sid) if event["type"] == "message"] == [
            event for event in session["events"] if event["type"] == "message"
        ]
    finally:
        restored.close()


def test_distinct_intents_never_steer_an_unprivileged_run_with_identical_scope_and_language(
    tmp_path,
    create_workspace,
):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Runtime:
        def __init__(
            self,
            settings,
            toolkit,
            emit,
            *,
            steering,
            initial_items,
            analysis_context,
            analysis_service,
            operations_service,
            action_intent,
            language,
        ):
            self.steering, self.intent = steering, action_intent
            self.context_id = analysis_context["context"]["context_id"]
            self.language = language

        async def run(self, prompt):
            call = {
                "prompt": prompt,
                "intent": self.intent,
                "context_id": self.context_id,
                "language": self.language,
            }
            calls.append(call)
            if len(calls) == 1:
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            call["steering"] = await self.steering()
            return {"status": "completed", "usage": [], "cost_usd": 0}

    app = create_workspace(
        settings(tmp_path), runtime_factory=Runtime, analysis_service=analysis_service(tmp_path)
    )
    with TestClient(app) as client:
        cid = context(client)["context"]["context_id"]
        sid = client.post("/api/sessions").json()["id"]
        url = f"/api/sessions/{sid}/prompt"
        common = {"analysis_context_id": cid, "language": "vi"}
        response = client.post(url, json={**common, "text": "READ_ONLY", "action_intent": "none"})
        assert response.status_code == 200, response.text
        wait_for(entered.is_set)
        for intent, text in (
            ("draft_create", "CREATE_DRAFT"),
            ("draft_update", "UPDATE_DRAFT"),
            ("none", "READ_ONLY_STEERING"),
        ):
            queued = client.post(url, json={**common, "text": text, "action_intent": intent})
            assert queued.status_code == 200, queued.text
            assert queued.json() == {"queued": True}
        release.set()
        session = completed_runs(client, sid, count=3)
        assert calls == [
            {
                "prompt": "READ_ONLY",
                "intent": "none",
                "context_id": cid,
                "language": "vi",
                "steering": ["READ_ONLY_STEERING"],
            },
            {
                "prompt": "CREATE_DRAFT",
                "intent": "draft_create",
                "context_id": cid,
                "language": "vi",
                "steering": [],
            },
            {
                "prompt": "UPDATE_DRAFT",
                "intent": "draft_update",
                "context_id": cid,
                "language": "vi",
                "steering": [],
            },
        ]
        assert [run["action_intent"] for run in session["runs"]] == [
            "none",
            "draft_create",
            "draft_update",
        ]
        assert session["pending_instructions"] == 0
        assert app.state.workspace.active_action_intents[sid] == "draft_update"


@pytest.mark.parametrize("scoped", [False, True])
def test_legacy_runtime_factories_keep_read_only_compatibility(tmp_path, create_workspace, scoped):
    calls = []

    class LegacyRuntime:
        def __init__(self, settings, toolkit, emit, *, steering, initial_items):
            self.steering = steering

        async def run(self, prompt):
            calls.append({"prompt": prompt, "steering": await self.steering()})
            return {"status": "completed", "usage": [], "cost_usd": 0}

    class LegacyScopedRuntime(LegacyRuntime):
        def __init__(
            self,
            settings,
            toolkit,
            emit,
            *,
            steering,
            initial_items,
            analysis_context,
            analysis_service,
        ):
            super().__init__(
                settings, toolkit, emit, steering=steering, initial_items=initial_items
            )
            assert analysis_context["context"]["context_id"]

    app = create_workspace(
        settings(tmp_path),
        runtime_factory=LegacyScopedRuntime if scoped else LegacyRuntime,
        analysis_service=analysis_service(tmp_path),
    )
    with TestClient(app) as client:
        sid = client.post("/api/sessions").json()["id"]
        body = {"text": "Legacy read-only request"}
        if scoped:
            body["analysis_context_id"] = context(client)["context"]["context_id"]
        else:
            uploaded = client.post(
                f"/api/sessions/{sid}/files",
                files={"files": ("synthetic.csv", b"value\n1\n", "text/csv")},
            )
            assert uploaded.status_code == 200
        response = client.post(f"/api/sessions/{sid}/prompt", json=body)
        assert response.status_code == 200, response.text
        session = completed_runs(client, sid)
        assert calls == [{"prompt": body["text"], "steering": []}]
        assert session["runs"][0]["action_intent"] == "none"


def test_legacy_runtime_does_not_silently_drop_requested_write_intent(tmp_path, create_workspace):
    calls = []

    class LegacyRuntime:
        def __init__(
            self,
            settings,
            toolkit,
            emit,
            *,
            steering,
            initial_items,
            analysis_context,
            analysis_service,
        ):
            calls.append("constructed")

        async def run(self, prompt):
            calls.append("ran")
            return {"status": "completed", "usage": [], "cost_usd": 0}

    app = create_workspace(
        settings(tmp_path),
        runtime_factory=LegacyRuntime,
        analysis_service=analysis_service(tmp_path),
    )
    with TestClient(app) as client:
        cid = context(client)["context"]["context_id"]
        sid = client.post("/api/sessions").json()["id"]
        response = client.post(
            f"/api/sessions/{sid}/prompt",
            json={
                "text": "Create a synthetic draft",
                "analysis_context_id": cid,
                "action_intent": "draft_create",
            },
        )
        assert response.status_code == 200, response.text
        session = completed_runs(client, sid, state="failed")
        assert calls == []
        assert session["runs"][0]["action_intent"] == "draft_create"
        assert any(
            "does not support work-item requests" in event["data"]["message"]
            for event in session["events"]
            if event["type"] == "error"
        )
        assert client.get("/api/operations/cases").json()["cases"] == []
