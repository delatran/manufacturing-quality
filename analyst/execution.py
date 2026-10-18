"""Persistent analysis subprocess with resource limits and accidental-write guards.

This is defense in depth for local, trusted-owner analysis, not an OS security
sandbox. Never expose this runner as a hostile multi-tenant code service.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

ALLOWED_IMPORTS = {
    "pandas",
    "numpy",
    "scipy",
    "matplotlib",
    "seaborn",
    "math",
    "statistics",
    "json",
    "csv",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "re",
    "decimal",
    "fractions",
    "random",
    "textwrap",
}


def validate_code(code: str) -> None:
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if any(module.split(".")[0] not in ALLOWED_IMPORTS for module in modules):
                raise ValueError("Import is not in the analyst module allowlist")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("Private Python attributes are unavailable")
        if isinstance(node, ast.Name) and (
            node.id.startswith("__")
            or node.id
            in {
                "eval",
                "exec",
                "compile",
                "globals",
                "locals",
                "getattr",
                "setattr",
                "delattr",
                "breakpoint",
                "input",
            }
        ):
            raise ValueError("Dynamic interpreter access is unavailable")


def analysis_environment(artifact_dir: Path) -> dict[str, str]:
    """Build a child environment without provider, proxy or owner credentials."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if os.name == "nt" and key.upper() in {"SYSTEMROOT", "WINDIR", "PATH"}
    }
    temporary = artifact_dir / ".tmp"
    temporary.mkdir(exist_ok=True)
    environment.update(
        {
            "HOME": str(artifact_dir),
            "TMPDIR": str(temporary),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "PYTHONIOENCODING": "utf-8",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(artifact_dir / ".matplotlib"),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        }
    )
    return environment


class PythonSession:
    def __init__(
        self,
        run_dir: str | Path,
        input_paths: Sequence[str | Path],
        *,
        timeout_seconds: float | None = 60,
        memory_mb: int | None = 1024,
        output_bytes: int = 100_000,
    ):
        self.run_dir = Path(run_dir).resolve()
        self.artifact_dir = self.run_dir / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.input_paths = [str(Path(path).resolve()) for path in input_paths]
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb
        self.output_bytes = output_bytes
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def _start(self) -> asyncio.subprocess.Process:
        if self.process and self.process.returncode is None:
            return self.process
        config = json.dumps(
            {
                "artifact_dir": str(self.artifact_dir),
                "input_paths": self.input_paths,
                "memory_mb": self.memory_mb,
                "output_bytes": self.output_bytes,
            }
        )
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            "--worker",
            config,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.artifact_dir,
            env=analysis_environment(self.artifact_dir),
            limit=self.output_bytes * 3,
        )
        return self.process

    async def execute(self, code: str) -> dict[str, Any]:
        validate_code(code)
        async with self.lock:
            reader = None
            try:
                # A cancelled or failed write can already have dispatched code.
                # Cover the complete exchange so its output cannot be mistaken
                # for the next cell's result. None preserves unbounded runs.
                async with asyncio.timeout(self.timeout_seconds):
                    process = await self._start()
                    stdin, stdout = process.stdin, process.stdout
                    if stdin is None or stdout is None:
                        raise RuntimeError("Python worker pipes are unavailable")
                    stdin.write((json.dumps({"code": code}) + "\n").encode())
                    await stdin.drain()
                    monitor = None
                    if self.memory_mb is not None:
                        with suppress(ImportError):
                            import psutil

                            monitor = psutil.Process(process.pid)
                    if monitor is None or self.memory_mb is None:
                        line = await stdout.readline()
                    else:
                        reader = asyncio.create_task(stdout.readline())
                        while not reader.done():
                            if monitor.memory_info().rss > self.memory_mb * 1024 * 1024:
                                raise MemoryError(
                                    "Python execution exceeded its memory limit; session reset"
                                )
                            await asyncio.wait({reader}, timeout=0.1)
                        line = reader.result()
                if not line:
                    raise RuntimeError("Python worker exited; session state was reset")
                return json.loads(line)
            except BaseException as error:
                if reader is not None:
                    reader.cancel()
                try:
                    # Kill before awaiting reader cleanup so a repeated Stop
                    # cannot leave the child executing an abandoned cell.
                    await self.close()
                finally:
                    if reader is not None:
                        await asyncio.gather(reader, return_exceptions=True)
                if isinstance(error, TimeoutError):
                    raise TimeoutError(
                        "Python execution exceeded its time limit; session reset"
                    ) from error
                raise

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def configure(self, run_dir: str | Path, input_paths: Sequence[str | Path]) -> None:
        """Switch output scope without discarding already computed Python variables."""
        async with self.lock:
            self.run_dir = Path(run_dir).resolve()
            self.artifact_dir = self.run_dir / "artifacts"
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            self.input_paths = [str(Path(path).resolve()) for path in input_paths]
            process = self.process
            if process and process.returncode is None:
                try:
                    async with asyncio.timeout(self.timeout_seconds):
                        stdin, stdout = process.stdin, process.stdout
                        if stdin is None or stdout is None:
                            raise RuntimeError("Python worker pipes are unavailable")
                        command = {
                            "configure": {
                                "artifact_dir": str(self.artifact_dir),
                                "input_paths": self.input_paths,
                            }
                        }
                        stdin.write((json.dumps(command) + "\n").encode())
                        await stdin.drain()
                        response = json.loads(await stdout.readline())
                        if not response.get("ok"):
                            raise RuntimeError("Could not switch Python run scope")
                except BaseException:
                    # Scope admission or its acknowledgement is uncertain. A
                    # fresh worker must start in the requested scope next time.
                    await self.close()
                    raise


def _worker(config: dict[str, Any]) -> None:
    import contextlib
    import io
    import mimetypes
    import tempfile

    # Isolated Python ignores PYTHONIOENCODING. The pipe protocol is UTF-8
    # regardless of the Windows console code page or the owner's locale.
    if isinstance(sys.stdin, io.TextIOWrapper):
        sys.stdin.reconfigure(encoding="utf-8")
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    # Import numerical libraries before activating filesystem guards.
    import matplotlib
    import numpy as np
    import pandas as pd
    import scipy

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Workbook readers initialize MIME metadata lazily from system files on
    # POSIX. Resolve it before cell guards without admitting those paths later.
    mimetypes.init()

    artifact = Path(config["artifact_dir"]).resolve()
    inputs = {Path(path).resolve() for path in config["input_paths"]}
    runtime_roots = [Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()]
    fonts = Path(os.environ.get("WINDIR", "/usr/share")) / "Fonts"

    def path_allowed(path, writing=False):
        if isinstance(path, int):
            return True
        target = Path(os.fsdecode(path)).resolve()
        if target.is_relative_to(artifact):
            return True
        if writing:
            return False
        return (
            target in inputs
            or any(target.is_relative_to(root) for root in runtime_roots)
            or target.is_relative_to(fonts)
        )

    def audit(event, args):
        if event == "open":
            path, mode, flags = args
            writing = (
                bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
                if isinstance(flags, int)
                else any(c in str(mode) for c in "wax+")
            )
            if not path_allowed(path, writing):
                raise PermissionError("File access is outside approved inputs or run artifacts")
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate", "os.utime"}:
            if not path_allowed(args[0], True):
                raise PermissionError("Filesystem mutation is outside run artifacts")
        elif event in {"os.rename", "os.link", "os.symlink"}:
            if any(not path_allowed(path, True) for path in args[:2]):
                raise PermissionError("Filesystem mutation is outside run artifacts")
        elif event.startswith(("subprocess.", "socket.", "ctypes.")) or event in {
            "os.system",
            "os.exec",
            "os.spawn",
            "os.fork",
            "os.posix_spawn",
        }:
            raise PermissionError("Process, network and native library operations are disabled")

    sys.addaudithook(audit)
    namespace = {
        "pd": pd,
        "np": np,
        "plt": plt,
        "scipy": scipy,
        "INPUT_PATHS": [str(p) for p in inputs],
        "ARTIFACT_DIR": str(artifact),
    }

    class BoundedOutput(io.StringIO):
        def write(self, text):
            if self.tell() + len(text.encode("utf-8")) > config["output_bytes"]:
                raise ValueError("Python stdout exceeded the output limit")
            return super().write(text)

    def plain(value):
        if isinstance(value, pd.DataFrame):
            return json.loads(value.to_json(orient="records"))
        if isinstance(value, pd.Series):
            return json.loads(value.to_json())
        if isinstance(value, np.generic):
            return plain(value.item())
        if isinstance(value, np.ndarray):
            return plain(value.tolist())
        if isinstance(value, dict):
            return {str(k): plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(v) for v in value]
        if isinstance(value, float) and not np.isfinite(value):
            return None
        return value

    for line in sys.stdin:
        capture = BoundedOutput()
        try:
            command = json.loads(line)
            if "configure" in command:
                artifact = Path(command["configure"]["artifact_dir"]).resolve()
                inputs = {Path(path).resolve() for path in command["configure"]["input_paths"]}
                # Libraries such as openpyxl reuse tempfile's cached directory.
                # Move temporary writes with the artifact scope before ACK; the
                # parent discards this worker if any part of setup fails.
                temporary = artifact / ".tmp"
                temporary.mkdir(exist_ok=True)
                for name in ("TMPDIR", "TEMP", "TMP"):
                    os.environ[name] = str(temporary)
                tempfile.tempdir = None
                namespace["ARTIFACT_DIR"] = str(artifact)
                namespace["INPUT_PATHS"] = [str(path) for path in inputs]
                os.chdir(artifact)
                sys.stdout.write('{"ok":true}\n')
                sys.stdout.flush()
                continue
            code = command["code"]
            validate_code(code)
            namespace.pop("result", None)
            with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
                exec(compile(code, "<analyst-cell>", "exec"), namespace)
            response = {
                "ok": True,
                "stdout": capture.getvalue(),
                "value": plain(namespace.get("result")),
            }
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, default=str)
            if len(encoded.encode()) > config["output_bytes"]:
                raise ValueError(
                    "Python result exceeded the output limit; aggregate or export a file"
                )
        except BaseException as error:
            encoded = json.dumps(
                {
                    "ok": False,
                    "stdout": capture.getvalue()[:2000],
                    "error": f"{type(error).__name__}: {error}",
                }
            )
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--worker":
    _worker(json.loads(sys.argv[2]))
