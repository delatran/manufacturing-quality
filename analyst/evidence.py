"""Append-only execution evidence and independently checkable numeric claims."""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import numbers
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def sha256(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def numeric_values(value: Any, path: str = "$") -> list[dict]:
    found: list[dict] = []
    _collect_numeric_values(value, path, found)
    return found


def _collect_numeric_values(value: Any, path: str, found: list[dict]) -> None:
    # Exact JSON container/scalar types take a fast path; anything else falls back
    # to the original numbers.Real / dict / sequence checks in the same order.
    kind = type(value)
    if kind is dict:
        for key, item in value.items():
            _collect_numeric_values(item, f"{path}.{key}", found)
    elif kind is list or kind is tuple:
        for index, item in enumerate(value):
            _collect_numeric_values(item, f"{path}[{index}]", found)
    elif kind is str or kind is bool or value is None:
        return
    elif (
        isinstance(value, numbers.Real)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ):
        found.append({"path": path, "value": value})
    elif isinstance(value, dict):
        for key, item in value.items():
            _collect_numeric_values(item, f"{path}.{key}", found)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _collect_numeric_values(item, f"{path}[{index}]", found)


def ledger_text(data: bytes) -> str:
    """Decode ledger bytes exactly as Path.read_text(encoding="utf-8") would."""
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


class EvidenceLedger:
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "ledger.jsonl"
        self.manifest_path = self.run_dir / "artifacts.json"
        self._lock = self._locks.setdefault(str(self.run_dir), threading.RLock())

    @staticmethod
    def parse_records(text: str) -> list[dict]:
        # JSONL uses physical line endings; Unicode separators inside JSON
        # strings are source content. read_text already normalizes CRLF.
        return [json.loads(line) for line in text.split("\n") if line]

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return self.parse_records(self.path.read_text(encoding="utf-8"))

    @property
    def entries(self) -> list[dict]:
        return self.records()

    read_entries = records

    def record(
        self, tool: str, code: str, result: Any, sources: list[dict] | None = None, **metadata: Any
    ) -> dict:
        with self._lock:
            return self._record(tool, code, result, sources, **metadata)

    def _record(
        self, tool: str, code: str, result: Any, sources: list[dict] | None = None, **metadata: Any
    ) -> dict:
        # Round trip forbids non-finite values and removes implementation-specific scalar types.
        result_text = canonical(result)
        result = json.loads(result_text)
        entries = self.records()
        entry = {
            "execution_id": uuid.uuid4().hex,
            "created_at": datetime.now(UTC).isoformat(),
            "tool": tool,
            "code": code,
            "code_sha256": sha256(code),
            "result": result,
            "result_sha256": sha256(canonical(result)),
            "numeric_values": numeric_values(result),
            "sources": sources or [],
            "previous_sha256": entries[-1]["entry_sha256"] if entries else None,
            **metadata,
        }
        entry["entry_sha256"] = sha256(canonical(entry))
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(canonical(entry) + "\n")
        return entry

    def verify_integrity(self, entries: list[dict] | None = None) -> list[str]:
        errors = []
        previous = None
        for entry in self.records() if entries is None else entries:
            payload = {k: v for k, v in entry.items() if k != "entry_sha256"}
            identity = entry.get("execution_id", "unknown")
            if sha256(canonical(payload)) != entry.get("entry_sha256"):
                errors.append(f"{identity}: entry hash mismatch")
            if sha256(entry.get("code", "")) != entry.get("code_sha256"):
                errors.append(f"{identity}: code hash mismatch")
            if sha256(canonical(entry.get("result"))) != entry.get("result_sha256"):
                errors.append(f"{identity}: result hash mismatch")
            if entry.get("previous_sha256") != previous:
                errors.append(f"{identity}: chain mismatch")
            previous = entry.get("entry_sha256")
        return errors

    def verify_claims(self, claims: list[dict]) -> dict:
        """Claims require execution_id, path, value and optional absolute tolerance."""
        errors = self.verify_integrity()
        entries = {e["execution_id"]: e for e in self.records()}
        for index, claim in enumerate(claims):
            entry = entries.get(claim.get("execution_id"))
            values = {v["path"]: v["value"] for v in entry["numeric_values"]} if entry else {}
            expected = values.get(claim.get("path"))
            value = claim.get("value")
            tolerance = claim.get("tolerance", 0)
            if expected is None or not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"claim[{index}]: missing numeric evidence")
            elif (
                not isinstance(tolerance, (int, float))
                or not math.isfinite(tolerance)
                or tolerance < 0
                or tolerance > 0.005001
            ):
                errors.append(
                    f"claim[{index}]: tolerance must be between zero and half-cent rounding"
                )
            elif not math.isfinite(value) or abs(expected - value) > tolerance:
                errors.append(f"claim[{index}]: value differs from evidence")
            if entry and claim.get("code_sha256") != entry["code_sha256"]:
                errors.append(f"claim[{index}]: code hash mismatch")
            if entry and isinstance(entry["result"], dict) and entry["result"].get("ok") is False:
                errors.append(f"claim[{index}]: execution did not succeed")
        return {"verified": not errors, "checked_claims": len(claims), "errors": errors}

    def register_artifact(
        self,
        path: str | Path,
        *,
        kind: str | None = None,
        context: dict[str, Any] | None = None,
        locale: str | None = None,
        content_sha256: str | None = None,
        size_bytes: int | None = None,
        manifest: list[dict] | None = None,
        write_manifest: bool = True,
    ) -> dict:
        target = Path(path).resolve()
        artifact_root = (self.run_dir / "artifacts").resolve()
        if not target.is_relative_to(artifact_root) or not target.is_file() or target.is_symlink():
            raise ValueError(
                "Artifact must be a regular file inside this run's artifacts directory"
            )
        entry = {
            "name": target.name,
            "path": target.relative_to(self.run_dir).as_posix(),
            "sha256": content_sha256 if content_sha256 is not None else sha256(target.read_bytes()),
            "size_bytes": size_bytes if size_bytes is not None else target.stat().st_size,
            "mime_type": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        }
        if kind:
            entry["kind"] = kind
        if context is not None:
            entry.update(
                {
                    "context_id": context["context_id"],
                    "scope": context["filters"],
                    "dataset_version": context["dataset_version"],
                    "policy_version": context["policy_version"],
                    "scenario": context.get("scenario", context["filters"].get("scenario", "")),
                }
            )
        if locale is not None:
            if locale not in {"en", "vi"}:
                raise ValueError("Artifact locale must be en or vi")
            entry.update(export_locale=locale, value_locale="machine")
        current_items = self.artifacts() if manifest is None else manifest
        items = [item for item in current_items if item["path"] != entry["path"]]
        items.append(entry)
        if manifest is not None:
            manifest.clear()
            manifest.extend(items)
        if write_manifest:
            self.manifest_path.write_text(canonical(items), encoding="utf-8")
        return entry

    def artifacts(self) -> list[dict]:
        return (
            json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if self.manifest_path.exists()
            else []
        )

    def resolve_artifact(self, relative_path: str, *, manifest: list[dict] | None = None) -> Path:
        items = self.artifacts() if manifest is None else manifest
        entry = next((item for item in items if item["path"] == relative_path), None)
        target = (self.run_dir / relative_path).resolve()
        if (
            not entry
            or not target.is_relative_to((self.run_dir / "artifacts").resolve())
            or not target.is_file()
        ):
            raise FileNotFoundError("Artifact is not in the manifest")
        if sha256(target.read_bytes()) != entry["sha256"]:
            raise ValueError("Artifact hash mismatch")
        return target
