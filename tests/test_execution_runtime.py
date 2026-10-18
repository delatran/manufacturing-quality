"""Failed worker exchanges cannot leak a response into the next execution."""

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from analyst import execution
from analyst.execution import PythonSession, analysis_environment


class WorkerProcess:
    def __init__(self, *, block_drain=False, response=b'{"ok":true,"value":42}\n'):
        self.returncode = None
        self.pid = 1
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block_drain = block_drain
        self.response = response
        self.writes = []
        self.killed = False
        self.waited = False
        self.stdin = SimpleNamespace(write=self.writes.append, drain=self.drain)
        self.stdout = SimpleNamespace(readline=self.readline)

    async def drain(self):
        self.started.set()
        if self.block_drain:
            await self.release.wait()

    async def readline(self):
        self.started.set()
        if self.response is None:
            await self.release.wait()
        return self.response

    def kill(self):
        self.killed = True
        self.returncode = -1

    async def wait(self):
        self.waited = True
        return self.returncode


@pytest.mark.parametrize("operation", ["execute", "configure"])
def test_cancelled_pipe_write_stops_worker_before_next_exchange(tmp_path, operation):
    async def run():
        worker = PythonSession(tmp_path / "first", [], timeout_seconds=None, memory_mb=None)
        process = WorkerProcess(block_drain=True)
        worker.process = process
        command = (
            worker.execute("result = 42")
            if operation == "execute"
            else worker.configure(tmp_path / "second", [])
        )
        task = asyncio.create_task(command)
        await process.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.killed and process.waited
        assert worker.process is None

    asyncio.run(run())


@pytest.mark.parametrize("response", [None, b'{"ok":false}\n', b"malformed\n"])
def test_failed_scope_change_discards_worker_with_uncertain_pipe_state(tmp_path, response):
    async def run():
        worker = PythonSession(tmp_path / "first", [], timeout_seconds=0.01, memory_mb=None)
        process = WorkerProcess(response=response)
        worker.process = process
        with pytest.raises((TimeoutError, RuntimeError, ValueError)):
            await worker.configure(tmp_path / "second", [])
        assert process.killed and process.waited
        assert worker.process is None

    asyncio.run(run())


def test_unbounded_worker_awaits_output_without_resource_polling(tmp_path, monkeypatch):
    async def unexpected_poll(*args, **kwargs):
        raise AssertionError("A worker without resource limits does not need timed polling")

    async def run():
        worker = PythonSession(tmp_path, [], timeout_seconds=None, memory_mb=None)
        worker.process = WorkerProcess()
        monkeypatch.setattr(asyncio, "wait", unexpected_poll)
        try:
            assert await worker.execute("result = 42") == {"ok": True, "value": 42}
        finally:
            await worker.close()

    asyncio.run(run())


def test_bounded_worker_memory_limit_still_stops_execution(tmp_path, monkeypatch):
    psutil = pytest.importorskip("psutil")
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda pid: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=2 * 1024 * 1024)),
    )

    async def run():
        worker = PythonSession(tmp_path, [], timeout_seconds=None, memory_mb=1)
        process = WorkerProcess(response=None)
        worker.process = process
        with pytest.raises(MemoryError, match="memory limit"):
            await worker.execute("while True: pass")
        assert process.killed and process.waited
        assert worker.process is None

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["execute", "configure"])
def test_bounded_worker_timeout_covers_blocked_pipe_writes(tmp_path, operation):
    async def run():
        worker = PythonSession(tmp_path, [], timeout_seconds=0.01, memory_mb=None)
        process = WorkerProcess(block_drain=True)
        worker.process = process
        command = (
            worker.execute("result = 42")
            if operation == "execute"
            else worker.configure(tmp_path / "second", [])
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(command, timeout=1)
        assert process.killed and process.waited
        assert worker.process is None

    asyncio.run(run())


def test_scope_change_relocates_temporary_files_and_preserves_worker_state(tmp_path):
    async def run():
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        worker = PythonSession(first_dir, [], timeout_seconds=30, memory_mb=None)
        try:
            first = await worker.execute(
                "retained = 41\n"
                "pd.DataFrame({'value': [retained]}).to_excel("
                "ARTIFACT_DIR + '/first.xlsx', engine='openpyxl', index=False)\n"
                "plt.figure()\nplt.plot([1, 2], [3, 4])\n"
                "plt.savefig(ARTIFACT_DIR + '/first.png')\n"
                "result = retained"
            )
            assert first["ok"], first
            process = worker.process
            prior_files = {
                path.relative_to(first_dir): path.read_bytes()
                for path in first_dir.rglob("*")
                if path.is_file()
            }
            await worker.configure(second_dir, [])
            assert worker.process is process
            second = await worker.execute(
                "pd.DataFrame({'value': [retained + 1]}).to_excel("
                "ARTIFACT_DIR + '/second.xlsx', engine='openpyxl', index=False)\n"
                "plt.savefig(ARTIFACT_DIR + '/second.png')\n"
                "result = retained + 1"
            )
            assert second["ok"], second
            assert second["value"] == 42
            assert worker.process is process
            assert (second_dir / "artifacts" / "second.xlsx").is_file()
            assert (second_dir / "artifacts" / "second.png").read_bytes() == (
                first_dir / "artifacts" / "first.png"
            ).read_bytes()
            denied = await worker.execute(
                f"open({str(first_dir / 'artifacts' / 'forbidden.txt')!r}, 'w').write('no')"
            )
            assert not denied["ok"] and "PermissionError" in denied["error"]
            assert prior_files == {
                path.relative_to(first_dir): path.read_bytes()
                for path in first_dir.rglob("*")
                if path.is_file()
            }
        finally:
            await worker.close()

    asyncio.run(run())


def test_failed_library_scope_setup_discards_worker_before_retrying_new_scope(tmp_path):
    async def run():
        worker = PythonSession(tmp_path / "first", [], timeout_seconds=30, memory_mb=None)
        second_dir = tmp_path / "second"
        conflict = second_dir / "artifacts" / ".tmp"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("A file cannot be used as the temporary directory")
        try:
            assert (await worker.execute("retained = 41\nresult = retained"))["ok"]
            old_process = worker.process
            with pytest.raises(RuntimeError, match="switch Python run scope"):
                await worker.configure(second_dir, [])
            assert worker.process is None
            assert old_process.returncode is not None
            conflict.unlink()
            fresh = await worker.execute(
                "open(ARTIFACT_DIR + '/fresh.txt', 'w').write('new scope')\nresult = ARTIFACT_DIR"
            )
            assert fresh["ok"] and fresh["value"] == str(second_dir / "artifacts")
            assert worker.process is not old_process
            assert (second_dir / "artifacts" / "fresh.txt").read_text() == "new scope"
            assert not (tmp_path / "first" / "artifacts" / "fresh.txt").exists()
            assert not (await worker.execute("result = retained"))["ok"]
        finally:
            await worker.close()

    asyncio.run(run())


def test_workbook_mime_metadata_is_initialized_before_file_guards(tmp_path):
    metadata = tmp_path / "system-mime.types"
    metadata.write_text("application/x-example example\n", encoding="utf-8")
    artifact = tmp_path / "artifacts"
    artifact.mkdir()
    config = {
        "artifact_dir": str(artifact),
        "input_paths": [],
        "memory_mb": None,
        "output_bytes": 100_000,
    }
    # Reproduce POSIX's external MIME database on every platform. The synthetic
    # metadata is never admitted as a dataset or an artifact.
    launcher = (
        "import mimetypes, runpy, sys; "
        "mimetypes.knownfiles = [sys.argv.pop(1)]; "
        "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    commands = [
        {
            "code": "pd.DataFrame({'value': [41]}).to_excel("
            "ARTIFACT_DIR + '/table.xlsx', engine='openpyxl', index=False)\n"
            "result = int(pd.read_excel(ARTIFACT_DIR + '/table.xlsx').value.sum())"
        },
        {"code": f"result = open({str(metadata)!r}).read()"},
    ]
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            launcher,
            str(metadata),
            execution.__file__,
            "--worker",
            json.dumps(config),
        ],
        input="".join(json.dumps(command) + "\n" for command in commands),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=artifact,
        env=analysis_environment(artifact),
        timeout=30,
        check=True,
    )
    first, denied = [json.loads(line) for line in completed.stdout.splitlines()]
    assert first["ok"] and first["value"] == 41, first
    assert not denied["ok"] and "PermissionError" in denied["error"]
    assert metadata.read_text(encoding="utf-8") == "application/x-example example\n"
