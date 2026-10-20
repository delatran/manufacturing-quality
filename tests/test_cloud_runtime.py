import asyncio
import io
import json
import re
import sqlite3
import subprocess
import sys
import tarfile
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import analyst.cloud_runtime as cloud
import httpx
import pytest
from analyst.config import Settings
from analyst.managed_agents import ManagedAgentsModel
from analyst.store import SessionStore


class BridgeServer:
    def __init__(self):
        self.body = None
        self.etag = None
        self.revision = 0
        self.fail = False
        self.requests = []

    def handle(self, request):
        self.requests.append((request.method, request.read()))
        if self.fail:
            return httpx.Response(503)
        if request.method == "HEAD":
            return httpx.Response(204)
        if request.method == "GET":
            return (
                httpx.Response(404)
                if self.body is None
                else httpx.Response(200, content=self.body, headers={"ETag": self.etag})
            )
        if self.etag:
            assert request.headers.get("If-Match") == self.etag
        else:
            assert request.headers.get("If-None-Match") == "*"
        assert request.headers["Content-Type"] == "application/gzip"
        self.body = request.read()
        self.revision += 1
        self.etag = f'"{self.revision}"'
        return httpx.Response(204, headers={"ETag": self.etag})

    def bridge(self, root):
        return cloud.SnapshotBridge(
            root, "http://state.internal/snapshot", transport=httpx.MockTransport(self.handle)
        )


def test_restore_releases_only_upgrade_run_admission_fences(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create_session()["id"]
    with store.db:
        for name, table in (
            ("analyst_image_upgrade_runs", "runs"),
            ("analyst_image_upgrade_steering", "steering"),
        ):
            store.db.execute(
                f"CREATE TRIGGER {name} BEFORE INSERT ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'Workspace is updating'); END"
            )
        store.db.execute(
            "CREATE TRIGGER unrelated_audit_trigger AFTER INSERT ON costs BEGIN SELECT 1; END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.begin_run(sid)
    with pytest.raises(sqlite3.IntegrityError):
        store.queue_instruction(sid, "pending detail")
    cloud._release_upgrade_admission(tmp_path)
    rid, _ = store.begin_run(sid)
    store.queue_instruction(sid, "detail after restore")
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='unrelated_audit_trigger'"
        ).fetchone()[0]
        == 1
    )
    assert store.session(sid)["runs"][0]["id"] == rid
    store.close()


@pytest.mark.parametrize("final_failure", [False, True])
def test_cloud_shutdown_checkpoints_after_workspace_close_and_propagates_failure(
    monkeypatch, final_failure
):
    from analyst import app as app_module
    from fastapi import FastAPI

    calls = []

    def checkpoint(*, force=False):
        calls.append("final_checkpoint" if force else "checkpoint")
        if force and final_failure:
            raise cloud.PersistenceUnavailable("final save failed")

    bridge = SimpleNamespace(
        restore=lambda: None, checkpoint=checkpoint, close=lambda: calls.append("bridge_closed")
    )

    @asynccontextmanager
    async def original_lifespan(app):
        yield
        calls.append("workspace_closed")

    def create_app(settings):
        app = FastAPI(lifespan=original_lifespan)
        app.state.workspace = SimpleNamespace(store=SimpleNamespace(close=lambda: None), tasks={})
        return app

    monkeypatch.setenv("ANALYST_CLOUD_ROLE", "agent")
    monkeypatch.setattr(cloud, "prepare_tls_trust", lambda: None)
    monkeypatch.setattr(cloud, "SnapshotBridge", lambda *args, **kwargs: bridge)
    monkeypatch.setattr(cloud, "CloudSessionStore", lambda *args: object())
    monkeypatch.setattr(cloud, "_release_upgrade_admission", lambda *args: None)
    monkeypatch.setattr(app_module, "create_app", create_app)
    application = cloud.build_application()

    async def run():
        async with application.router.lifespan_context(application):
            calls.append("serving")

    if final_failure:
        with pytest.raises(cloud.PersistenceUnavailable):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert calls == [
        "checkpoint",
        "serving",
        "workspace_closed",
        "final_checkpoint",
        "bridge_closed",
    ]


@pytest.mark.parametrize("started,shutdown_failed", [(True, False), (False, False), (True, True)])
def test_container_entrypoint_exit_status_preserves_startup_and_final_save_failure(
    monkeypatch, started, shutdown_failed
):
    import uvicorn

    server = SimpleNamespace(
        run=lambda: None,
        started=started,
        lifespan=SimpleNamespace(shutdown_failed=shutdown_failed),
    )
    monkeypatch.setattr(cloud, "build_application", lambda: object())
    monkeypatch.setattr(uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", lambda *args: server)
    if started and not shutdown_failed:
        cloud.main()
    else:
        with pytest.raises(SystemExit) as error:
            cloud.main()
        assert error.value.code != 0


def test_backup_restores_wal_database_and_artifacts(tmp_path):
    server = BridgeServer()
    source = tmp_path / "source"
    source.mkdir()
    database = sqlite3.connect(source / "workspace.sqlite3")
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("CREATE TABLE proof(value TEXT)")
    database.execute("INSERT INTO proof VALUES ('before')")
    database.commit()
    (source / "artifact.txt").write_text("evidence")
    first = server.bridge(source)
    first.restore()
    first.checkpoint()
    database.execute("INSERT INTO proof VALUES ('after')")
    database.commit()
    first.checkpoint()
    second = server.bridge(tmp_path / "restored")
    second.restore()
    with closing(sqlite3.connect(second.root / "workspace.sqlite3")) as recovered:
        assert recovered.execute("SELECT value FROM proof").fetchall() == [("before",), ("after",)]
    assert (second.root / "artifact.txt").read_text() == "evidence"
    with tarfile.open(fileobj=io.BytesIO(server.body), mode="r:gz") as saved_archive:
        assert not any(item.name.endswith(("-wal", "-shm")) for item in saved_archive.getmembers())
    assert server.revision == 2
    database.close()


def test_failed_persistence_stops_reservation_before_permission(tmp_path):
    server = BridgeServer()
    bridge = server.bridge(tmp_path)
    bridge.restore()
    store = cloud.CloudSessionStore(tmp_path, bridge)
    sid = store.create_session()["id"]
    rid, _ = store.begin_run(sid)
    server.fail = True
    with pytest.raises(cloud.PersistenceUnavailable):
        store.reserve_budget(rid, 5, 10)
    assert bridge.failed
    server.fail = False
    with pytest.raises(cloud.PersistenceUnavailable):
        store.reserve_budget(rid, 5, 10)
    store.close()


@pytest.mark.parametrize(
    "kind,data",
    [
        ("model_started", {"provider_api": "agents", "boundary_id": "create-idempotency-key"}),
        ("managed_session_created", {"session_id": "sess_native"}),
        ("managed_session_rejected", {"boundary_id": "rejected-create", "status": 403}),
        ("managed_recovery", {"session_id": "sess_native", "state": "cancelled"}),
        ("provider_cancel", {"session_id": "sess_native", "acknowledged": True}),
    ],
)
def test_managed_provider_lifecycle_events_are_durable_before_acknowledgement(tmp_path, kind, data):
    server = BridgeServer()
    bridge = server.bridge(tmp_path / "source")
    bridge.restore()
    store = cloud.CloudSessionStore(bridge.root, bridge)
    sid = store.create_session()["id"]
    rid, _ = store.begin_run(sid)
    emitted = store.emit(sid, rid, kind, data)
    restored_bridge = server.bridge(tmp_path / "restored")
    restored_bridge.restore()
    restored = cloud.CloudSessionStore(restored_bridge.root, restored_bridge)
    assert restored.events(sid) == [emitted]
    restored.close()
    store.close()


def test_managed_admission_stops_when_lifecycle_checkpoint_fails(tmp_path):
    server = BridgeServer()
    bridge = server.bridge(tmp_path)
    bridge.restore()
    store = cloud.CloudSessionStore(tmp_path, bridge)
    sid = store.create_session()["id"]
    rid, _ = store.begin_run(sid)
    server.fail = True
    with pytest.raises(cloud.PersistenceUnavailable):
        store.emit(sid, rid, "model_started", {"provider_api": "agents"})
    assert bridge.failed
    store.close()


def test_unbounded_migration_restores_all_spending_and_unknown_reservations(tmp_path):
    server = BridgeServer()
    bridge = server.bridge(tmp_path)
    bridge.restore()
    store = cloud.CloudSessionStore(tmp_path, bridge)
    sid = store.create_session()["id"]
    old_settled, _ = store.begin_run(sid)
    old_unknown, _ = store.begin_run(sid)
    recent, _ = store.begin_run(sid)
    with store.db:
        for rid in (old_settled, old_unknown):
            store.db.execute(
                "UPDATE runs SET created='2020-01-01T00:00:00+00:00' WHERE id=?", (rid,)
            )
        store.db.execute(
            "INSERT INTO budget_reservations(run_id,amount,settled) VALUES (?,5,1)", (old_settled,)
        )
        store.db.execute(
            "INSERT INTO budget_reservations(run_id,amount,settled) VALUES (?,5,0)", (old_unknown,)
        )
        store.db.execute(
            "INSERT INTO budget_reservations(run_id,amount,settled) VALUES (?,5,1)", (recent,)
        )
        store.db.execute("INSERT INTO costs VALUES ('old',?,4)", (old_settled,))
        store.db.execute("INSERT INTO costs VALUES ('recent',?,2)", (recent,))
    assert store.remaining_budget(20) == 9
    bridge.checkpoint()
    store.close()
    restored_bridge = server.bridge(tmp_path / "restored")
    restored_bridge.restore()
    restored = cloud.CloudSessionStore(restored_bridge.root, restored_bridge)
    before = [dict(row) for row in restored.db.execute("SELECT * FROM budget_reservations")]
    unbounded, _ = restored.begin_run(sid)
    assert restored.reserve_budget(unbounded, None, None) is None
    assert restored.remaining_budget(None) is None
    restored.record_cost(unbounded, "managed-session-estimate", 0.25)
    assert restored.remaining_budget(20) == 8.75
    for row in before:
        actual = restored.db.execute(
            "SELECT * FROM budget_reservations WHERE run_id=?", (row["run_id"],)
        ).fetchone()
        assert dict(actual) == row
    assert restored.db.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 3
    restored.close()


@pytest.mark.parametrize(
    "name,kind",
    [("../escape", "file"), ("/escape", "file"), ("C:/escape", "file"), ("safe", "link")],
)
def test_restore_rejects_unsafe_archive(tmp_path, name, kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        if kind == "link":
            info.type = tarfile.SYMTYPE
            info.linkname = "/escape"
            archive.addfile(info)
        else:
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bad"))
    server = BridgeServer()
    server.body = buffer.getvalue()
    server.etag = '"bad"'
    bridge = server.bridge(tmp_path / "runtime")
    with pytest.raises(cloud.PersistenceUnavailable):
        bridge.restore()
    assert not (tmp_path / "escape").exists()


def test_checkpoint_skips_unchanged_state(tmp_path):
    server = BridgeServer()
    bridge = server.bridge(tmp_path)
    bridge.restore()
    (tmp_path / "saved.txt").write_text("one")
    bridge.checkpoint()
    bridge.checkpoint()
    assert server.revision == 1
    (tmp_path / "saved.txt").write_text("two")
    bridge.checkpoint()
    assert server.revision == 2


def test_active_runs_keep_container_alive_without_copying_mutable_workspace(tmp_path, monkeypatch):
    server = BridgeServer()
    bridge = server.bridge(tmp_path)
    bridge.restore()
    (tmp_path / "tool-output.txt").write_text("local tool is still writing")
    ticks = 0
    original_sleep = asyncio.sleep

    async def tick(_seconds):
        nonlocal ticks
        ticks += 1
        if ticks > 13:
            raise asyncio.CancelledError
        await original_sleep(0)

    monkeypatch.setattr(cloud.asyncio, "sleep", tick)
    active = SimpleNamespace(done=lambda: ticks >= 13)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cloud._maintain_workspace(bridge, {"run": active}))
    assert [method for method, _ in server.requests] == ["GET", "HEAD", "PUT"]
    assert server.requests[1][1] == b""
    assert server.revision == 1


def test_heartbeat_failure_pauses_new_spending_without_overwriting_saved_state(tmp_path):
    server = BridgeServer()
    bridge = server.bridge(tmp_path)
    bridge.restore()
    (tmp_path / "saved.txt").write_text("retained")
    bridge.checkpoint()
    saved = server.body
    server.fail = True
    with pytest.raises(cloud.PersistenceUnavailable, match="liveness"):
        bridge.ping()
    assert bridge.failed
    assert server.body == saved
    assert server.revision == 1


def test_snapshot_omits_unpublished_contexts_and_plot_caches(tmp_path):
    server = BridgeServer()
    bridge = server.bridge(tmp_path)
    bridge.restore()
    for name in ("analysis/contexts/.pending-build/output.txt", "sessions/run/.matplotlib/cache"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("in progress")
    (tmp_path / "committed.txt").write_text("ready")
    bridge.checkpoint()
    with tarfile.open(fileobj=io.BytesIO(server.body), mode="r:gz") as saved_archive:
        assert [item.name for item in saved_archive.getmembers()] == ["committed.txt"]


def test_checkpoint_rejects_archive_limit(tmp_path, monkeypatch):
    server = BridgeServer()
    bridge = server.bridge(tmp_path)
    bridge.restore()
    (tmp_path / "large.txt").write_text("large")
    monkeypatch.setattr(cloud, "MAX_ARCHIVE_BYTES", 4)
    with pytest.raises(cloud.PersistenceUnavailable):
        bridge.checkpoint()
    assert server.body is None


def test_checkpoint_requires_storage_version(tmp_path):
    bridge = cloud.SnapshotBridge(
        tmp_path,
        "http://state.internal/snapshot",
        transport=httpx.MockTransport(lambda request: httpx.Response(204)),
    )
    with pytest.raises(cloud.PersistenceUnavailable):
        bridge.checkpoint()
    assert bridge.failed


def test_cloud_dashboard_profile_has_no_credentials_and_preserves_public_origin(monkeypatch):
    from analyst import app as app_module
    from fastapi import FastAPI

    captured = []

    def create_app(settings):
        captured.append(settings)
        return FastAPI()

    monkeypatch.setenv("ANALYST_CLOUD_ROLE", "dashboard")
    monkeypatch.setattr(cloud, "prepare_tls_trust", lambda: None)
    monkeypatch.setattr(app_module, "create_app", create_app)
    cloud.build_application()
    settings = captured[0]
    assert settings.run_limit("max_upload_mb") is None
    assert settings.public_origin == "https://tranthiennhan.com"
    assert settings.api_key == ""
    assert settings.openai_base_url is None
    assert not settings.memory_enabled


@pytest.mark.parametrize("fast_mode,tier", [("true", "fast"), ("false", "default")])
def test_cloud_agent_profile_matches_actual_worker_provider_contract(monkeypatch, fast_mode, tier):
    from analyst import app as app_module
    from fastapi import FastAPI

    captured = []
    initial_store = SimpleNamespace(close=lambda: None)
    persisted_store = object()
    bridge = SimpleNamespace(restore=lambda: None)

    def create_app(settings):
        captured.append(settings)
        app = FastAPI()
        app.state.workspace = SimpleNamespace(store=initial_store)
        return app

    monkeypatch.setenv("ANALYST_CLOUD_ROLE", "agent")
    monkeypatch.setenv("OPENAI_FAST_MODE", fast_mode)
    monkeypatch.setattr(cloud, "prepare_tls_trust", lambda: None)
    monkeypatch.setattr(cloud, "SnapshotBridge", lambda *args, **kwargs: bridge)
    monkeypatch.setattr(cloud, "CloudSessionStore", lambda *args: persisted_store)
    monkeypatch.setattr(app_module, "create_app", create_app)
    application = cloud.build_application()
    settings = captured[0]
    assert application.state.workspace.store is persisted_store
    assert settings.live
    assert settings.agent_backend == "agents_api"
    assert settings.unbounded_runs
    assert settings.openai_base_url == "http://provider.internal/v1"
    assert settings.api_key == "cloudflare-proxy"
    assert settings.public_config()["service_tier"] == tier
    for name in (
        "max_upload_mb",
        "run_timeout_seconds",
        "max_output_tokens",
        "max_cost_usd_per_run",
        "dev_api_budget_usd",
        "max_turns",
        "max_tool_calls",
        "tool_timeout_seconds",
        "qa_max_retries",
    ):
        assert settings.run_limit(name) is None
    assert settings.public_config()["conversation_strategy"] == "managed_conversation"

    model = ManagedAgentsModel(settings, lambda *args: None, client=SimpleNamespace())
    request = {
        "environment": {"type": "none"},
        "agent": model._agent_configuration("Inspect the selected scope", [], None, []),
        "input": model._messages([{"role": "user", "content": "Inspect the selected scope"}]),
        "stream": True,
    }
    assert request["agent"]["service_tier"] == tier
    worker = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "import {readFileSync} from 'node:fs';"
            "import {providerPath,validateProviderBody} "
            "from './cloudflare/src/policy.js';"
            "const body=JSON.parse(readFileSync(0,'utf8'));"
            "validateProviderBody(body,'gpt-6-luna','create');"
            "console.log(JSON.stringify({route:providerPath('/v1/responses','POST'),"
            "nativeRoute:providerPath('/v1/agents/sessions','POST')}));",
        ],
        cwd=cloud.Path(cloud.__file__).resolve().parents[1],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert worker.returncode == 0, worker.stderr
    policy = json.loads(worker.stdout)
    assert policy["route"] is None
    assert policy["nativeRoute"] == {"action": "create"}
    assert "spend_control" not in request
    assert "max_output_tokens" not in request["agent"]
    assert Settings().agent_backend == "agents_api"
    assert Settings().unbounded_runs


def test_tls_bundle_preserves_public_trust_and_uses_current_container_ca(tmp_path, monkeypatch):
    import certifi

    original = cloud.Path(certifi.where()).read_bytes()
    certificate = tmp_path / "cloudflare-ca.pem"
    certificate.write_bytes(original)
    bundle = tmp_path / "combined.pem"
    monkeypatch.setenv("SSL_CERT_FILE", "previous-container-ca")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "previous-container-ca")
    cloud.prepare_tls_trust(certificate, bundle, timeout=0)
    assert bundle.read_bytes() == original + b"\n" + original
    assert cloud.os.environ["SSL_CERT_FILE"] == str(bundle)
    assert cloud.os.environ["REQUESTS_CA_BUNDLE"] == str(bundle)


def test_tls_startup_fails_when_container_ca_is_missing(tmp_path):
    with pytest.raises(RuntimeError, match="certificate did not become ready"):
        cloud.prepare_tls_trust(tmp_path / "missing.pem", tmp_path / "combined.pem", timeout=0)


def test_runtime_header_is_opaque_and_stable_for_one_process():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_middleware(cloud.PersistenceMiddleware)

    @app.get("/health")
    def health():
        return {"status": "ready"}

    with TestClient(app) as client:
        first = client.get("/health")
        second = client.get("/health")
    assert first.headers["X-Analyst-Runtime"] == second.headers["X-Analyst-Runtime"]
    assert first.headers["X-Analyst-Runtime"] == cloud._RUNTIME_ID
    assert re.fullmatch(r"[a-f0-9]{32}", first.headers["X-Analyst-Runtime"])


def test_runtime_identity_changes_in_a_new_python_process():
    child_identity = subprocess.check_output(
        [sys.executable, "-c", "from analyst.cloud_runtime import _RUNTIME_ID; print(_RUNTIME_ID)"],
        cwd=cloud.Path(cloud.__file__).resolve().parents[1],
        text=True,
        timeout=30,
    ).strip()
    assert re.fullmatch(r"[a-f0-9]{32}", child_identity)
    assert child_identity != cloud._RUNTIME_ID


def test_runtime_header_does_not_skip_durable_mutation_checkpoint(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    server = BridgeServer()
    bridge = server.bridge(tmp_path)
    bridge.restore()
    app = FastAPI()
    app.add_middleware(cloud.PersistenceMiddleware, bridge=bridge)

    @app.post("/save")
    def save():
        (tmp_path / "committed.txt").write_text("durable")
        return {"saved": True}

    with TestClient(app) as client:
        response = client.post("/save")
    assert response.json() == {"saved": True}
    assert response.headers["X-Analyst-Runtime"] == cloud._RUNTIME_ID
    with tarfile.open(fileobj=io.BytesIO(server.body), mode="r:gz") as archive:
        assert archive.extractfile("committed.txt").read() == b"durable"


def test_mutation_status_waits_for_a_durable_checkpoint():
    order = []
    bridge = SimpleNamespace(checkpoint=lambda: order.append("saved"))

    async def endpoint(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"acknowledged"})

    async def observe(message):
        order.append(message["type"])

    asyncio.run(
        cloud.PersistenceMiddleware(endpoint, bridge)(
            {"type": "http", "method": "POST"}, None, observe
        )
    )
    assert order == ["saved", "http.response.start", "http.response.body"]


def test_failed_mutation_checkpoint_returns_retryable_failure_without_success_status(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    server = BridgeServer()
    bridge = server.bridge(tmp_path)
    bridge.restore()
    server.fail = True
    app = FastAPI()
    app.add_middleware(cloud.PersistenceMiddleware, bridge=bridge)

    @app.post("/save")
    def save():
        (tmp_path / "unconfirmed.txt").write_text("Retained locally")
        return {"saved": True}

    with TestClient(app) as client:
        response = client.post("/save")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "persistence_unavailable"
    assert response.headers["X-Analyst-Runtime"] == cloud._RUNTIME_ID
    assert bridge.failed
    assert server.body is None
    assert (tmp_path / "unconfirmed.txt").read_text() == "Retained locally"
