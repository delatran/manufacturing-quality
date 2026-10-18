"""Container entrypoint with visitor-scoped durable checkpoints.

The private outbound Worker owns credentials and authenticates each Container by
its platform identity. SQLite remains on local disk; consistent backup archives
are transferred through that Worker instead of placing live databases on R2.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import shutil
import sqlite3
import ssl
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing, suppress
from dataclasses import replace
from pathlib import Path, PurePosixPath

import certifi
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings
from .store import SessionStore

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_STATE_BYTES = 256 * 1024 * 1024
MAX_STATE_FILES = 10_000
_RUNTIME_ID = uuid.uuid4().hex
logger = logging.getLogger(__name__)


def _release_upgrade_admission(root: Path) -> None:
    """Remove only the old process's temporary run admission fences on restore."""
    database = root / "workspace.sqlite3"
    if not database.exists():
        return
    with closing(sqlite3.connect(database)) as db:
        with db:
            for name in ("analyst_image_upgrade_runs", "analyst_image_upgrade_steering"):
                db.execute("DROP TRIGGER IF EXISTS " + name)


def prepare_tls_trust(
    certificate: Path = Path("/etc/cloudflare/certs/cloudflare-containers-ca.crt"),
    bundle: Path = Path("/tmp/analyst-tls-ca.pem"),
    *,
    timeout: float = 10.0,
) -> None:
    """Trust this Container's interception CA before constructing HTTPS clients."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            cloudflare_ca = certificate.read_bytes()
            if cloudflare_ca:
                break
        except (FileNotFoundError, PermissionError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Cloudflare HTTPS interception certificate did not become ready")
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    # Rebuild from the pristine public bundle on every start, never append stale CAs.
    bundle.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + cloudflare_ca)
    ssl.create_default_context(cafile=str(bundle))
    os.environ["SSL_CERT_FILE"] = str(bundle)
    os.environ["REQUESTS_CA_BUNDLE"] = str(bundle)


class PersistenceUnavailable(RuntimeError):
    """Durable storage is unavailable; further paid runs must stop."""


class SnapshotBridge:
    """Versioned runtime archives over a private, instance-scoped HTTP bridge."""

    def __init__(
        self,
        root: Path,
        url: str,
        *,
        token: str = "",
        transport: httpx.BaseTransport | None = None,
    ):
        self.root = root.resolve()
        self.url = url
        self.etag: str | None = None
        self.failed = False
        self._fingerprint: str | None = None
        self._lock = threading.RLock()
        self._client = httpx.Client(
            timeout=30,
            trust_env=False,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )

    def _paths(self) -> Iterator[Path]:
        for path in sorted(self.root.rglob("*")):
            parts = path.relative_to(self.root).parts
            if any(
                part.startswith(".pending-") or part in {".matplotlib", "__pycache__"}
                for part in parts
            ):
                continue
            if path.is_symlink():
                raise PersistenceUnavailable("Runtime snapshots cannot contain symbolic links")
            if path.is_file() and not path.name.endswith(("-wal", "-shm")):
                yield path

    def _state_fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path in self._paths():
            stat = path.stat()
            digest.update(
                f"{path.relative_to(self.root).as_posix()}:{stat.st_size}:{stat.st_mtime_ns}".encode()
            )
            # SQLite can commit changes solely to WAL without touching its main file.
            if path.suffix == ".sqlite3":
                wal = Path(str(path) + "-wal")
                if wal.exists():
                    stat = wal.stat()
                    digest.update(f":wal:{stat.st_size}:{stat.st_mtime_ns}".encode())
        return digest.hexdigest()

    def restore(self) -> None:
        """Restore before opening SQLite connections or accepting traffic."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with self._client.stream("GET", self.url) as response:
                if response.status_code == 404:
                    return
                response.raise_for_status()
                self.etag = response.headers.get("etag")
                if not self.etag:
                    raise PersistenceUnavailable("Durable state response is missing its version")
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_ARCHIVE_BYTES:
                        raise PersistenceUnavailable("Durable state exceeds its archive limit")
            with tarfile.open(fileobj=io.BytesIO(chunks), mode="r:gz") as archive:
                members = []
                total = 0
                for member in archive:
                    members.append(member)
                    total += member.size
                    if len(members) > MAX_STATE_FILES or total > MAX_STATE_BYTES:
                        raise PersistenceUnavailable("Durable state exceeds its expanded limit")
                    name = PurePosixPath(member.name)
                    if (
                        name.is_absolute()
                        or not name.parts
                        or any(part in {"", ".", ".."} for part in name.parts)
                        or "\\" in member.name
                        or ":" in member.name
                        or not member.isfile()
                    ):
                        raise PersistenceUnavailable("Durable state contains an unsafe entry")
                    target = self.root.joinpath(*name.parts)
                    if not target.resolve().is_relative_to(self.root):
                        raise PersistenceUnavailable("Durable state escaped the runtime directory")
                for member in members:
                    target = self.root.joinpath(*PurePosixPath(member.name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise PersistenceUnavailable("Durable state has an unreadable entry")
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
            self._fingerprint = self._state_fingerprint()
        except Exception as error:
            self.failed = True
            raise PersistenceUnavailable("Saved workspace could not be restored safely") from error

    def checkpoint(self, *, force: bool = False) -> None:
        """Commit snapshots synchronously before paid work or success acknowledgments."""
        with self._lock:
            if self.failed:
                raise PersistenceUnavailable("Durable storage must recover before another run")
            try:
                fingerprint = self._state_fingerprint()
                if not force and fingerprint == self._fingerprint:
                    return
                with tempfile.TemporaryDirectory(prefix="analyst-checkpoint-") as temporary:
                    staging = Path(temporary)
                    archive_path = staging / "snapshot.tar.gz"
                    total = 0
                    paths = list(self._paths())
                    if len(paths) > MAX_STATE_FILES:
                        raise PersistenceUnavailable("Workspace contains too many files")
                    with tarfile.open(archive_path, mode="w:gz") as archive:
                        for index, path in enumerate(paths):
                            selected = path
                            if path.suffix == ".sqlite3":
                                selected = staging / f"database-{index}.sqlite3"
                                with closing(
                                    sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                                ) as source:
                                    with closing(sqlite3.connect(selected)) as destination:
                                        source.backup(destination)
                            total += selected.stat().st_size
                            if total > MAX_STATE_BYTES:
                                raise PersistenceUnavailable("Workspace exceeds its storage limit")
                            archive.add(
                                selected,
                                arcname=path.relative_to(self.root).as_posix(),
                                recursive=False,
                            )
                    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
                        raise PersistenceUnavailable("Workspace exceeds its archive limit")
                    headers = {"Content-Type": "application/gzip"}
                    headers.update({"If-Match": self.etag} if self.etag else {"If-None-Match": "*"})
                    response = self._client.put(
                        self.url, content=archive_path.read_bytes(), headers=headers
                    )
                    response.raise_for_status()
                    version = response.headers.get("etag")
                    if not version:
                        raise PersistenceUnavailable(
                            "Storage did not acknowledge the snapshot version"
                        )
                    self.etag = version
                self._fingerprint = fingerprint
            except Exception as error:
                self.failed = True
                raise PersistenceUnavailable(
                    "Workspace could not be saved; further runs are paused"
                ) from error

    def ping(self) -> None:
        """Keep the owning Container active without reading or copying its state."""
        if self.failed:
            raise PersistenceUnavailable("Durable storage must recover before another run")
        try:
            response = self._client.head(self.url)
            response.raise_for_status()
        except Exception as error:
            self.failed = True
            raise PersistenceUnavailable("Workspace liveness could not be confirmed") from error

    def close(self) -> None:
        self._client.close()


async def _maintain_workspace(bridge: SnapshotBridge, tasks: dict[str, asyncio.Task[None]]) -> None:
    next_heartbeat = 0.0
    while True:
        await asyncio.sleep(5)
        try:
            if any(not run.done() for run in tasks.values()):
                # A long local tool still needs Durable Object activity after
                # the browser disconnects. Copying its mutable files is unsafe.
                if time.monotonic() >= next_heartbeat:
                    await asyncio.to_thread(bridge.ping)
                    next_heartbeat = time.monotonic() + 60
            else:
                bridge.checkpoint()
        except PersistenceUnavailable:
            logger.error("Durable storage unavailable; new Agent runs are paused")
            return


class CloudSessionStore(SessionStore):
    """Keep spending records and unresolved reservations durable before paid work."""

    def __init__(self, root: Path, bridge: SnapshotBridge):
        self.bridge = bridge
        super().__init__(root)

    def _checkpoint(self) -> None:
        self.bridge.checkpoint(force=True)

    def emit(self, sid: str, run_id: str | None, kind: str, data: dict) -> dict:
        event = super().emit(sid, run_id, kind, data)
        if kind in {
            "managed_session_created",
            "managed_session_rejected",
            "managed_recovery",
            "provider_cancel",
        } or (kind == "model_started" and data.get("provider_api") == "agents"):
            # Save admission intent and provider identity before a generation
            # or acknowledged cancellation can outlive this Container process.
            self._checkpoint()
        return event

    def reserve_budget(
        self, rid: str, run_limit: float | None, dev_limit: float | None
    ) -> float | None:
        if self.bridge.failed:
            raise PersistenceUnavailable("Durable storage is unavailable")
        amount = super().reserve_budget(rid, run_limit, dev_limit)
        self._checkpoint()
        return amount

    def record_cost(self, rid: str, identity: str, amount: float) -> None:
        super().record_cost(rid, identity, amount)
        self._checkpoint()

    def settle_budget(self, rid: str) -> None:
        super().settle_budget(rid)
        self._checkpoint()

    def finish_run(self, rid: str, state: str, result: dict | None = None) -> None:
        super().finish_run(rid, state, result)
        self._checkpoint()


class PersistenceMiddleware:
    """Identify this process and save mutations before acknowledging success."""

    def __init__(self, app: ASGIApp, bridge: SnapshotBridge | None = None):
        self.app = app
        self.bridge = bridge

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        durable_write = scope["type"] == "http" and scope.get("method") not in {"GET", "HEAD"}
        response_started = False

        async def send_checkpointed(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                # API mutations finish before producing their response. Persist
                # them before even a success status can reach the caller.
                if durable_write and self.bridge is not None:
                    self.bridge.checkpoint()
                message["headers"] = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-analyst-runtime"
                ] + [(b"x-analyst-runtime", _RUNTIME_ID.encode("ascii"))]
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_checkpointed)
        except PersistenceUnavailable:
            if scope["type"] != "http" or response_started:
                raise
            response = JSONResponse(
                {
                    "detail": {
                        "code": "persistence_unavailable",
                        "message": "Workspace storage is unavailable; further runs are paused.",
                    }
                },
                status_code=503,
                headers={"X-Analyst-Runtime": _RUNTIME_ID},
            )
            await response(scope, receive, send)


def build_application() -> FastAPI:
    """Create one dashboard process or one isolated anonymous visitor workspace."""
    role = os.environ.get("ANALYST_CLOUD_ROLE", "dashboard")
    if role not in {"dashboard", "agent"}:
        raise ValueError("ANALYST_CLOUD_ROLE must be dashboard or agent")
    prepare_tls_trust()
    root = Path(__file__).resolve().parents[1]
    runtime = Path("/runtime")
    bridge = None
    if role == "agent":
        url = os.environ.get("ANALYST_STATE_URL", "http://state.internal/snapshot")
        if url != "http://state.internal/snapshot":
            raise ValueError("Cloud state must use its private outbound bridge")
        bridge = SnapshotBridge(runtime, url, token=os.environ.get("ANALYST_STATE_TOKEN", ""))
        bridge.restore()
        _release_upgrade_admission(runtime)
    settings = replace(
        Settings.from_env(root),
        host="0.0.0.0",
        port=8000,
        state_dir=str(runtime),
        api_key="cloudflare-proxy" if role == "agent" else "",
        mode="live" if role == "agent" else "auto",
        agent_backend="agents_api",
        unbounded_runs=True,
        memory_enabled=False,
        public_origin="https://tranthiennhan.com",
        openai_base_url="http://provider.internal/v1" if role == "agent" else None,
    )
    settings.validate()
    from . import app as app_module

    application = app_module.create_app(settings)
    application.add_middleware(PersistenceMiddleware, bridge=bridge)
    if bridge is None:
        return application
    application.state.workspace.store.close()
    application.state.workspace.store = CloudSessionStore(runtime, bridge)
    original_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with original_lifespan(app):
                bridge.checkpoint()

                task = asyncio.create_task(_maintain_workspace(bridge, app.state.workspace.tasks))
                try:
                    yield
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            # Workspace.close reconciles cancellation and writes terminal events.
            # Their final archive must be acknowledged before a clean process exit.
            bridge.checkpoint(force=True)
        finally:
            bridge.close()

    application.router.lifespan_context = lifespan
    return application


def main() -> None:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(build_application(), host="0.0.0.0", port=8000, proxy_headers=False)
    )
    server.run()
    # Uvicorn logs lifespan shutdown errors without propagating them from run().
    # The Container monitor must distinguish a failed final save from clean exit.
    if not server.started or getattr(server.lifespan, "shutdown_failed", False):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
