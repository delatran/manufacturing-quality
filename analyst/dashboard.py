"""Shared, immutable case-study contexts for the Dashboard and Agent.

The existing HTTP preparation and analytical functions own all business rules.
Only cleaned scoped CSVs, aggregate reports and their evidence are persisted.
"""

from __future__ import annotations

import asyncio
import copy
import csv
import inspect
import io
import json
import math
import platform
import re
import shutil
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

from .case_study import analytics
from .case_study.analytics import Scope
from .case_study.brief import render_brief
from .case_study.ingestion import SourceError
from .case_study.models import Event, Product, Snapshot
from .case_study.preparation import load_snapshot, positive_integer
from .case_study.scenarios import SCENARIOS, scenario_snapshot, validate_scenario
from .evidence import EvidenceLedger, canonical, numeric_values, sha256

ID_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
FILTER_KEYS = tuple(field.name for field in fields(Scope))
RECORD_FIELDS = tuple(field.name for field in fields(Event))
PRODUCT_FIELDS = tuple(field.name for field in fields(Product))
SORT_FIELDS = frozenset(
    {
        "defect_id",
        "product_id",
        "product_name",
        "production_line",
        "category",
        "defect_type",
        "defect_date",
        "defect_location",
        "severity",
        "inspection_method",
        "repair_cost",
        "source_row",
    }
)
_SEVERITY_ORDER = {sev: idx for idx, sev in enumerate(analytics.SEVERITIES)}
QUALITY_SCOPE = {
    "kind": "full_snapshot",
    "filters_applied": False,
    "description": "Quality describes the complete source snapshot, including removed and out-of-period rows. Dashboard filters do not change this audit; linkage and cost-availability counts cover January to June 2024.",
}
QUALITY_WORKBOOK_SCOPE = {
    "kind": "completed_full_case",
    "filters_applied": False,
    "description": "Completed workbook for the supplied full January-June 2024 case and complete source audit. This separate artifact does not follow dashboard filters or identify the currently selected context; its source lineage and retrieval date are recorded inside the workbook.",
}


class ContextNotFound(LookupError):
    """No receipt exists for the requested context."""


class SnapshotUnavailable(LookupError):
    """The requested immutable snapshot cannot be reconstructed here."""


class EvidenceIntegrityError(RuntimeError):
    """Persisted evidence or a registered artifact no longer matches its seal."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _scope(values: dict[str, Any] | None) -> Scope:
    if values is None:
        return Scope()
    if not isinstance(values, dict):
        raise ValueError("filters must be an object")
    unknown = set(values) - set(FILTER_KEYS)
    if unknown:
        raise ValueError("Unknown filters: " + ", ".join(sorted(map(str, unknown))))
    if any(not isinstance(value, str) for value in values.values()):
        raise ValueError("Every filter value must be a string")
    selected = Scope(**values)
    validate_scenario(selected.scenario)
    if selected.start_month not in analytics.MONTHS or selected.end_month not in analytics.MONTHS:
        raise ValueError("Month bounds must be full months between 2024-01 and 2024-06")
    if selected.start_month > selected.end_month:
        raise ValueError("start_month cannot be later than end_month")
    return selected


def _csv_bytes(rows: list[dict[str, Any]], names: tuple[str, ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=names, lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    writer.writerows(
        {
            key: (
                "'" + value
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                else value
            )
            for key, value in row.items()
        }
        for row in rows
    )
    return stream.getvalue().encode("utf-8-sig")


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()


def _export_metadata(locale: str, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "export_locale": locale,
        "value_locale": "machine",
        "currency_unit": "local currency",
        "number_convention": "Decimal point without thousands separators; missing numeric values are blank in CSV and null in JSON.",
        "date_convention": "ISO 8601",
        "data_labels": "Original source labels and canonical backend field names are preserved for traceability.",
        "context_id": context["context_id"],
        "dataset_version": context["dataset_version"],
        "policy_version": context["policy_version"],
        "scenario": context["scenario"],
        "scope": context["filters"],
    }


@dataclass(frozen=True)
class _FrozenSnapshot:
    snapshot: Snapshot
    clean_sha256: str


@dataclass(frozen=True)
class _ContextReceipt:
    payload: str
    directory: Path
    descriptor: str
    sealed_receipt_bytes: bytes = b""
    sealed_ledger_bytes: bytes = b""
    manifest_sha256: str = ""


class AnalysisService:
    def __init__(
        self,
        root: str | Path,
        state_dir: str | Path,
        *,
        snapshot_loader: Callable[[], Any] | None = None,
    ):
        self.root = Path(root).resolve()
        state = Path(state_dir)
        self.state_dir = (
            (state if state.is_absolute() else self.root / state) / "dashboard"
        ).resolve()
        self.contexts_dir = self.state_dir / "contexts"
        self._loader = snapshot_loader or (
            lambda: load_snapshot(self.root / "data" / "master_data.csv")
        )
        self._lock = asyncio.Lock()
        self._snapshots: dict[str, _FrozenSnapshot] = {}
        self._contexts: dict[str, _ContextReceipt] = {}
        self._latest: str | None = None
        self._records_cache: dict[str, list[dict[str, Any]]] = {}
        self._status: dict[str, Any] = {"stale": False, "last_error": None, "last_attempt_at": None}
        self.code, self.code_manifest, self.registry = self._code_bundle()
        self.code_sha256 = sha256(self.code)
        self.kpi_version = "kpi-v1-" + sha256(self.registry)[:16]

    def _code_bundle(self) -> tuple[str, dict[str, str], bytes]:
        package = Path(__file__).resolve().parent
        paths = [
            package / "dashboard.py",
            package / "evidence.py",
            *sorted((package / "case_study").glob("*.py")),
        ]
        registry_path = self.root / "knowledge" / "kpi_registry.yaml"
        registry = registry_path.read_bytes().replace(b"\r\n", b"\n")
        sources = {
            path.relative_to(package.parent).as_posix(): path.read_bytes().replace(b"\r\n", b"\n")
            for path in paths
        }
        sources["knowledge/kpi_registry.yaml"] = registry
        requirements = self.root / "requirements.txt"
        if requirements.is_file():
            sources["requirements.txt"] = requirements.read_bytes().replace(b"\r\n", b"\n")
        environment = {
            "python": platform.python_version(),
            "numpy": version("numpy"),
            "scipy": version("scipy"),
        }
        header = canonical(
            {
                "schema": "dashboard-analysis-v1",
                "call": "analysis_report(snapshot, Scope(**filters), permitted_missing_event_filters=scope_validation)",
                "environment": environment,
            }
        )
        code = (
            header
            + "\n"
            + "\n".join(
                f"\n# FILE {name}\n{content.decode('utf-8-sig')}"
                for name, content in sorted(sources.items())
            )
        )
        return code, {name: sha256(content) for name, content in sorted(sources.items())}, registry

    async def _fetch(self) -> None:
        self._status["last_attempt_at"] = _now()
        try:
            if inspect.iscoroutinefunction(self._loader):
                snapshot = await self._loader()
            else:
                snapshot = await asyncio.to_thread(self._loader)
                if inspect.isawaitable(snapshot):
                    snapshot = await snapshot
            if not isinstance(snapshot, Snapshot):
                raise SourceError("Snapshot loader did not return a prepared case-study snapshot")
            snapshot = copy.deepcopy(snapshot)
            for name in ("source_sha256", "master_sha256"):
                if not isinstance(snapshot.provenance.get(name), str) or not ID_PATTERN.fullmatch(
                    snapshot.provenance[name]
                ):
                    raise SourceError(f"Prepared snapshot has an invalid {name}")
            if not snapshot.provenance.get("fetched_at") or not snapshot.provenance.get(
                "source_url"
            ):
                raise SourceError("Prepared snapshot is missing HTTP provenance")
            clean_hash = sha256(
                canonical(
                    {
                        "products": [asdict(product) for product in snapshot.products],
                        "events": [event.as_record() for event in snapshot.events],
                        "audit": [asdict(entry) for entry in snapshot.audit],
                        "quality": snapshot.quality,
                        "raw_summary": snapshot.raw_summary,
                    }
                )
            )
            snapshot_id = sha256(
                canonical(
                    {
                        "source_sha256": snapshot.provenance["source_sha256"],
                        "master_sha256": snapshot.provenance["master_sha256"],
                        "clean_sha256": clean_hash,
                        "code_sha256": self.code_sha256,
                        "kpi_version": self.kpi_version,
                    }
                )
            )
            provenance = {
                **snapshot.provenance,
                "pipeline_snapshot_id": snapshot.provenance.get("snapshot_id"),
                "snapshot_id": snapshot_id,
            }
            self._snapshots.setdefault(
                snapshot_id, _FrozenSnapshot(replace(snapshot, provenance=provenance), clean_hash)
            )
            self._latest = snapshot_id
            self._status.update(stale=False, last_error=None)
        except (SourceError, OSError) as error:
            self._status.update(stale=True, last_error=str(error))
            if self._latest is None:
                raise SourceError(
                    f"The HTTP source is unavailable and no complete snapshot is loaded: {error}"
                ) from error

    async def _snapshot(self, snapshot_id: str | None = None) -> _FrozenSnapshot:
        if snapshot_id is not None:
            _identifier(snapshot_id, "snapshot_id")
        if snapshot_id in self._snapshots:
            return self._snapshots[snapshot_id]
        if self._latest is None:
            await self._fetch()
        selected = snapshot_id or self._latest
        if selected not in self._snapshots:
            raise SnapshotUnavailable(
                "The requested frozen snapshot is not available in this server. The current HTTP source or analysis code has a different identity; the request was not switched to the latest snapshot."
            )
        return self._snapshots[selected]

    @staticmethod
    def _context_id(snapshot_id: str, scope: Scope) -> str:
        return sha256(canonical({"snapshot_id": snapshot_id, "filters": asdict(scope)}))

    def _links(self, context_id: str) -> dict[str, str]:
        base = f"/api/dashboard/contexts/{context_id}"
        return {
            "records": base + "/records",
            "evidence": base + "/evidence",
            "export_csv": base + "/export.csv",
            "export_zip": base + "/export.zip",
            "brief": base + "/brief.md",
            "quality_workbook": "/api/dashboard/quality-workbook",
            "trace": base + "/trace",
            "compare_periods": base + "/compare-periods",
        }

    def _directory(self, context_id: str) -> Path:
        _identifier(context_id, "context_id")
        path = self.contexts_dir / context_id
        if path.is_symlink() or path.resolve().parent != self.contexts_dir.resolve():
            raise EvidenceIntegrityError(
                "Context directory is outside the dashboard evidence store"
            )
        return path

    def _read_descriptor(self, context_id: str) -> dict[str, Any]:
        directory = self._directory(context_id)
        descriptor_path = directory / "receipt.json"
        if not descriptor_path.is_file():
            raise ContextNotFound("The requested analysis context does not exist")
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            if descriptor["context"]["context_id"] != context_id:
                raise ValueError("context identity mismatch")
            scope = _scope(descriptor["context"]["filters"])
            if (
                self._context_id(
                    _identifier(descriptor["context"]["snapshot_id"], "snapshot_id"), scope
                )
                != context_id
            ):
                raise ValueError("scope identity mismatch")
            return descriptor
        except (ValueError, TypeError, KeyError, OSError) as error:
            raise EvidenceIntegrityError("Saved context metadata failed validation") from error

    def _verify(self, saved: _ContextReceipt) -> dict[str, Any]:
        try:
            receipt_bytes = (saved.directory / "receipt.json").read_bytes().replace(b"\r\n", b"\n")
            if saved.sealed_receipt_bytes and receipt_bytes == saved.sealed_receipt_bytes:
                descriptor = json.loads(saved.descriptor)
            else:
                descriptor = json.loads(receipt_bytes.decode("utf-8"))
                if canonical(descriptor) != saved.descriptor:
                    raise ValueError("context metadata changed")
            ledger = EvidenceLedger(saved.directory)
            manifest_bytes = ledger.manifest_path.read_bytes()
            if sha256(manifest_bytes) != descriptor["manifest_sha256"]:
                raise ValueError("artifact manifest changed")

            ledger_bytes = (saved.directory / "ledger.jsonl").read_bytes().replace(b"\r\n", b"\n")
            if not (saved.sealed_ledger_bytes and ledger_bytes == saved.sealed_ledger_bytes):
                entries = ledger.records()
                if (
                    len(entries) != 1
                    or not isinstance(entries[0], dict)
                    or ledger.verify_integrity(entries)
                ):
                    raise ValueError("ledger integrity failed")
                entry = entries[0]
                payload = json.loads(saved.payload)
                if (
                    entry != payload["evidence"]
                    or entry["entry_sha256"] != descriptor["entry_sha256"]
                ):
                    raise ValueError("canonical receipt changed")
                if entry.get("scope_validation") != descriptor.get("scope_validation"):
                    raise ValueError("scope validation policy changed")
                if entry["code"] != self.code or entry["code_sha256"] != self.code_sha256:
                    raise ValueError("analysis code identity differs")
                if entry["result"] != payload["analysis"] or entry[
                    "numeric_values"
                ] != numeric_values(entry["result"]):
                    raise ValueError("analysis does not match its receipt")

            manifest = json.loads(manifest_bytes.decode("utf-8"))
            for artifact in manifest:
                ledger.resolve_artifact(artifact["path"], manifest=manifest)
            return json.loads(saved.payload)
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise EvidenceIntegrityError(
                "Analysis evidence or a registered artifact failed integrity verification"
            ) from error

    def _restore(
        self, context_id: str, frozen: _FrozenSnapshot, descriptor: dict[str, Any]
    ) -> _ContextReceipt:
        directory = self._directory(context_id)
        try:
            context = descriptor["context"]
            snapshot = frozen.snapshot
            if (
                context["code_sha256"] != self.code_sha256
                or context["kpi_version"] != self.kpi_version
                or context["source_sha256"] != snapshot.provenance["source_sha256"]
                or context["master_sha256"] != snapshot.provenance["master_sha256"]
                or context["dataset_version"] != snapshot.provenance["dataset_version"]
                or context["policy_version"] != snapshot.provenance["policy_version"]
                or context["scenario"] != context["filters"]["scenario"]
                or descriptor["clean_sha256"] != frozen.clean_sha256
            ):
                raise ValueError("frozen source identity differs")
            ledger = EvidenceLedger(directory)
            entries = ledger.records()
            if (
                len(entries) != 1
                or not isinstance(entries[0], dict)
                or ledger.verify_integrity(entries)
            ):
                raise ValueError("invalid ledger")
            entry = entries[0]
            if entry["entry_sha256"] != descriptor["entry_sha256"]:
                raise ValueError("canonical receipt changed")
            if entry["code"] != self.code or entry["code_sha256"] != self.code_sha256:
                raise ValueError("analysis code identity differs")
            report = entry["result"]
            if entry["numeric_values"] != numeric_values(report):
                raise ValueError("analysis numeric evidence differs")
            if entry["context"] != context or report["scope"] != context["filters"]:
                raise ValueError("receipt scope differs")
            if any(
                report["metadata"][key] != context[key]
                for key in (
                    "snapshot_id",
                    "fetched_at",
                    "source_sha256",
                    "master_sha256",
                    "dataset_version",
                    "policy_version",
                    "scenario",
                )
            ):
                raise ValueError("receipt snapshot differs")
            for key in (
                "source_url",
                "source_date_min",
                "source_date_max",
                "pipeline_snapshot_id",
                "master_path",
            ):
                if report["metadata"].get(key) != snapshot.provenance.get(key):
                    raise ValueError("receipt source lineage differs")
            # A fresh HTTP fetch may have a later timestamp. Preserve the original
            # receipt's provenance while independently checking the same content.
            original = replace(snapshot, provenance=copy.deepcopy(report["metadata"]))
            scope = _scope(context["filters"])
            validation = descriptor["scope_validation"]
            if (
                validation != analytics.missing_event_filter_values(snapshot, scope)
                or entry.get("scope_validation") != validation
            ):
                raise ValueError("saved scope validation policy differs")
            expected = analytics.analysis_report(
                original, scope, permitted_missing_event_filters=validation
            )
            if canonical(expected) != canonical(report):
                raise ValueError("saved analysis differs from the prepared snapshot")
            payload = {
                "context": context,
                "analysis": report,
                "options": analytics.options(snapshot),
                "evidence": entry,
                "links": self._links(context_id),
                "quality_scope": QUALITY_SCOPE,
                "quality_workbook_scope": QUALITY_WORKBOOK_SCOPE,
            }
            receipt_text = canonical(descriptor)
            saved = _ContextReceipt(
                canonical(payload),
                directory,
                receipt_text,
                sealed_receipt_bytes=receipt_text.encode("utf-8"),
                sealed_ledger_bytes=(canonical(entry) + "\n").encode("utf-8"),
                manifest_sha256=descriptor["manifest_sha256"],
            )
            self._contexts[context_id] = saved
            return saved
        except (ValueError, TypeError, KeyError, OSError) as error:
            raise EvidenceIntegrityError(
                "Saved context cannot be verified against its frozen source and analysis code"
            ) from error

    def _create(
        self, frozen: _FrozenSnapshot, scope: Scope, scope_validation: dict[str, str]
    ) -> _ContextReceipt:
        snapshot = frozen.snapshot
        snapshot_id = snapshot.provenance["snapshot_id"]
        context_id = self._context_id(snapshot_id, scope)
        directory = self._directory(context_id)
        if directory.exists():
            return self._restore(context_id, frozen, self._read_descriptor(context_id))
        context = {
            "context_id": context_id,
            "snapshot_id": snapshot_id,
            "filters": asdict(scope),
            "source_sha256": snapshot.provenance["source_sha256"],
            "master_sha256": snapshot.provenance["master_sha256"],
            "code_sha256": self.code_sha256,
            "kpi_version": self.kpi_version,
            "fetched_at": snapshot.provenance["fetched_at"],
            "dataset_version": snapshot.provenance["dataset_version"],
            "policy_version": snapshot.provenance["policy_version"],
            "scenario": scope.scenario,
        }
        report = analytics.analysis_report(
            snapshot, scope, permitted_missing_event_filters=scope_validation
        )
        record_rows = [event.as_record() for event in analytics.filter_events(snapshot, scope)]
        records = _csv_bytes(record_rows, RECORD_FIELDS)
        products = _csv_bytes(
            [asdict(product) for product in analytics.filter_products(snapshot, scope)],
            PRODUCT_FIELDS,
        )
        self.contexts_dir.mkdir(parents=True, exist_ok=True)
        staging = self.contexts_dir / (".pending-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            artifacts = staging / "artifacts"
            artifacts.mkdir()
            ledger = EvidenceLedger(staging)
            input_hashes = {
                "cleaned_records.csv": sha256(records),
                "reference_products.csv": sha256(products),
            }
            sources = [
                {
                    "kind": "http",
                    "url": snapshot.provenance["source_url"],
                    "sha256": context["source_sha256"],
                },
                {
                    "kind": "master",
                    "path": "data/master_data.csv",
                    "sha256": context["master_sha256"],
                },
                *[
                    {"kind": "cleaned_context", "path": "artifacts/" + name, "sha256": digest}
                    for name, digest in input_hashes.items()
                ],
            ]
            receipt = ledger.record(
                "dashboard_analysis",
                self.code,
                report,
                sources=sources,
                context=context,
                input_sha256=input_hashes,
                snapshot_clean_sha256=frozen.clean_sha256,
                code_manifest=self.code_manifest,
                scope_validation=scope_validation,
            )
            payload = {
                "context": context,
                "analysis": receipt["result"],
                "options": analytics.options(snapshot),
                "evidence": receipt,
                "links": self._links(context_id),
                "quality_scope": QUALITY_SCOPE,
                "quality_workbook_scope": QUALITY_WORKBOOK_SCOPE,
            }
            files: dict[str, bytes] = {
                "context.json": canonical(
                    {
                        key: payload[key]
                        for key in (
                            "context",
                            "options",
                            "links",
                            "quality_scope",
                            "quality_workbook_scope",
                        )
                    }
                ).encode("utf-8"),
                "analysis.json": canonical(receipt["result"]).encode("utf-8"),
                "evidence.json": canonical(receipt).encode("utf-8"),
                "ledger.jsonl": ledger.path.read_bytes(),
                "cleaned_records.csv": records,
                "reference_products.csv": products,
                "definitions.json": canonical(
                    {
                        "kpi_version": self.kpi_version,
                        "definitions": report["definitions"],
                        "limitations": report["metadata"]["limitations"],
                        "quality_scope": QUALITY_SCOPE,
                    }
                ).encode("utf-8"),
                "kpi_registry.yaml": self.registry,
                "analysis_code.txt": self.code.encode("utf-8"),
                "export_metadata.json": canonical(_export_metadata("en", context)).encode("utf-8"),
                "README.md": (
                    b"# Frozen dashboard analysis\n\n"
                    b"context.json identifies the exact snapshot and full-month filters. analysis.json is the unchanged result of the successful receipt in evidence.json and ledger.jsonl. Numeric paths begin at $.kpis, $.monthly and the other analysis fields.\n\n"
                    b"cleaned_records.csv contains only retained events matching this context. reference_products.csv contains the eligible master cohort, including products with zero events; event-attribute filters do not shrink this denominator. Unknown costs remain blank. Raw defect JSON is neither stored nor included.\n\n"
                    b"Quality covers the complete canonical source snapshot and does not follow dashboard filters or scenarios. Reference output is static, the currency unit is local currency, and this historical case does not establish current production quality. See definitions.json and kpi_registry.yaml.\n\n"
                    b"export_metadata.json records the requested language, machine number/date convention, currency unit, complete scope and versions. Download CSV variants include that metadata in columns. Source labels remain traceable.\n\n"
                    b"bundle_manifest.json lists SHA-256 hashes of the included files. The execution receipt binds the analysis, code, source hashes and both cleaned input hashes.\n"
                ),
            }
            for locale in ("en", "vi"):
                files[f"records.{locale}.csv"] = _csv_bytes(
                    [
                        {
                            **row,
                            "export_locale": locale,
                            "currency_unit": "local currency",
                            "value_locale": "machine",
                            "dataset_version": context["dataset_version"],
                            "policy_version": context["policy_version"],
                            "scenario": scope.scenario,
                        }
                        for row in record_rows
                    ],
                    RECORD_FIELDS
                    + (
                        "export_locale",
                        "currency_unit",
                        "value_locale",
                        "dataset_version",
                        "policy_version",
                        "scenario",
                    ),
                )
            audit_rows = [asdict(entry) for entry in snapshot.audit]
            files["normalization_audit.csv"] = _csv_bytes(
                audit_rows,
                tuple(audit_rows[0])
                if audit_rows
                else (
                    "source",
                    "source_row",
                    "record_id",
                    "code",
                    "severity",
                    "field",
                    "raw_value",
                    "action",
                    "detail",
                    "processed_value",
                    "policy_version",
                    "source_rows",
                ),
            )
            bundle_manifest = {
                "context_id": context_id,
                "execution_id": receipt["execution_id"],
                "files": {name: sha256(content) for name, content in sorted(files.items())},
            }
            files["bundle_manifest.json"] = canonical(bundle_manifest).encode("utf-8")
            manifest_items: list[dict] = []
            for name, content in files.items():
                path = artifacts / name
                path.write_bytes(content)
                file_hash = bundle_manifest["files"].get(name) or sha256(content)
                ledger.register_artifact(
                    path,
                    kind="dashboard_context",
                    content_sha256=file_hash,
                    size_bytes=len(content),
                    manifest=manifest_items,
                    write_manifest=False,
                )
            zip_bytes_data = _zip_bytes(files)
            zip_path = artifacts / "evidence.zip"
            zip_path.write_bytes(zip_bytes_data)
            ledger.register_artifact(
                zip_path,
                kind="dashboard_evidence_bundle",
                content_sha256=sha256(zip_bytes_data),
                size_bytes=len(zip_bytes_data),
                manifest=manifest_items,
                write_manifest=False,
            )
            for locale in ("en", "vi"):
                localized = {
                    name: content
                    for name, content in files.items()
                    if name != "bundle_manifest.json"
                }
                localized["export_metadata.json"] = canonical(
                    _export_metadata(locale, context)
                ).encode("utf-8")
                if locale == "vi":
                    localized["README.md"] = (
                        "# Bằng chứng phân tích đã đóng băng\n\n"
                        "context.json xác định dataset, policy, scenario và toàn bộ bộ lọc. analysis.json khớp với kết quả đã xác minh trong evidence.json và ledger.jsonl.\n\n"
                        "cleaned_records.csv và reference_products.csv là đầu vào máy tính đã được receipt xác nhận. records.vi.csv bổ sung ngôn ngữ xuất, đơn vị local currency và version. Số dùng dấu chấm thập phân, không phân nhóm hàng nghìn; ngày theo ISO; chi phí chưa biết để trống.\n\n"
                        "normalization_audit.csv ghi dấu vết xử lý trường trên toàn nguồn canonical, gồm giá trị gốc và giá trị sau xử lý. Raw JSON chỉ giữ trong bộ nhớ của lần fetch HTTP thành công và không nằm trong gói.\n\n"
                        "Chất lượng dữ liệu và workbook giữ phạm vi canonical toàn nguồn. Tỷ lệ dùng sản lượng tham chiếu tĩnh, gồm product không có sự kiện. Kết quả lịch sử và scenario không chứng minh điều kiện sản xuất hiện tại hay khoản tiết kiệm đã thực hiện.\n\n"
                        "export_metadata.json ghi scope, ngôn ngữ, quy ước số và đơn vị. bundle_manifest.json liệt kê SHA-256 của từng file; không sửa file trong gói nếu cần đối soát.\n"
                    ).encode()
                loc_manifest = {
                    "context_id": context_id,
                    "execution_id": receipt["execution_id"],
                    "files": {name: sha256(content) for name, content in sorted(localized.items())},
                }
                localized["bundle_manifest.json"] = canonical(loc_manifest).encode("utf-8")
                loc_zip_bytes = _zip_bytes(localized)
                path = artifacts / f"evidence.{locale}.zip"
                path.write_bytes(loc_zip_bytes)
                ledger.register_artifact(
                    path,
                    kind="dashboard_localized_evidence_bundle",
                    content_sha256=sha256(loc_zip_bytes),
                    size_bytes=len(loc_zip_bytes),
                    manifest=manifest_items,
                    write_manifest=False,
                )
            manifest_text = canonical(manifest_items)
            ledger.manifest_path.write_text(manifest_text, encoding="utf-8")
            descriptor = {
                "schema": 1,
                "context": context,
                "clean_sha256": frozen.clean_sha256,
                "scope_validation": scope_validation,
                "entry_sha256": receipt["entry_sha256"],
                "manifest_sha256": sha256(manifest_text),
            }
            receipt_text = canonical(descriptor)
            (staging / "receipt.json").write_text(receipt_text, encoding="utf-8")
            try:
                staging.rename(directory)
            except FileExistsError:
                return self._restore(context_id, frozen, self._read_descriptor(context_id))
            saved = _ContextReceipt(
                canonical(payload),
                directory,
                receipt_text,
                sealed_receipt_bytes=receipt_text.encode("utf-8"),
                sealed_ledger_bytes=(canonical(receipt) + "\n").encode("utf-8"),
                manifest_sha256=descriptor["manifest_sha256"],
            )
            self._contexts[context_id] = saved
            return saved
        finally:
            # Delete only this call's unpublished directory, never an existing context.
            if (
                staging.exists()
                and not staging.is_symlink()
                and staging.resolve().parent == self.contexts_dir.resolve()
                and staging.name.startswith(".pending-")
            ):
                shutil.rmtree(staging)

    def _response(self, saved: _ContextReceipt) -> dict[str, Any]:
        payload = self._verify(saved)
        payload["status"] = copy.deepcopy(self._status)
        return payload

    async def _get_context(
        self,
        scope: Scope,
        snapshot_id: str | None,
        *,
        permitted_missing_event_filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        frozen = await self._snapshot(snapshot_id)
        analytics.validate_filters(
            frozen.snapshot, scope, permitted_missing_event_filters=permitted_missing_event_filters
        )
        validation = analytics.missing_event_filter_values(frozen.snapshot, scope)
        context_id = self._context_id(frozen.snapshot.provenance["snapshot_id"], scope)
        saved = self._contexts.get(context_id)
        if saved is None:
            saved = await asyncio.to_thread(self._create, frozen, scope, validation)
        return self._response(saved)

    async def get_context(
        self, filters: dict[str, Any] | None = None, snapshot_id: str | None = None
    ) -> dict[str, Any]:
        scope = _scope(filters)
        async with self._lock:
            return await self._get_context(scope, snapshot_id)

    async def refresh(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        scope = _scope(filters)
        async with self._lock:
            await self._fetch()
            return await self._get_context(scope, self._latest)

    async def refresh_for_context(self, context_id: str) -> dict[str, Any]:
        """Refresh an exact verified scope, retaining event values that disappear.

        This gate accepts a saved context identity, never caller-supplied filter
        exceptions. Ordinary Dashboard filtering and refresh stay strict.
        """
        _identifier(context_id, "context_id")
        async with self._lock:
            baseline = await self._context_by_id(context_id)
            scope = _scope(baseline["context"]["filters"])
            permitted = {
                field: getattr(scope, field)
                for field in analytics.EVENT_FILTER_OPTIONS
                if getattr(scope, field)
            }
            await self._fetch()
            return await self._get_context(
                scope, self._latest, permitted_missing_event_filters=permitted
            )

    async def _context_by_id(self, context_id: str) -> dict[str, Any]:
        _identifier(context_id, "context_id")
        saved = self._contexts.get(context_id)
        if saved is None:
            descriptor = self._read_descriptor(context_id)
            frozen = await self._snapshot(descriptor["context"]["snapshot_id"])
            saved = await asyncio.to_thread(self._restore, context_id, frozen, descriptor)
        return self._response(saved)

    async def context_by_id(self, context_id: str) -> dict[str, Any]:
        async with self._lock:
            return await self._context_by_id(context_id)

    async def snapshot_for_context(self, context_id: str) -> Snapshot:
        async with self._lock:
            payload = await self._context_by_id(context_id)
            frozen = self._snapshots[payload["context"]["snapshot_id"]]
            return replace(
                copy.deepcopy(frozen.snapshot),
                provenance=copy.deepcopy(payload["analysis"]["metadata"]),
            )

    async def input_paths_for_context(self, context_id: str) -> list[Path]:
        async with self._lock:
            payload = await self._context_by_id(context_id)
            saved = self._contexts[context_id]
            ledger = EvidenceLedger(saved.directory)
            paths = [
                ledger.resolve_artifact("artifacts/" + name)
                for name in ("cleaned_records.csv", "reference_products.csv")
            ]
            for path in paths:
                if sha256(path.read_bytes()) != payload["evidence"]["input_sha256"][path.name]:
                    raise EvidenceIntegrityError(
                        "A cleaned context input no longer matches the analysis receipt"
                    )
            return paths

    async def records(
        self,
        context_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "defect_date",
        sort_order: str = "desc",
        defect_date: str | None = None,
    ) -> dict[str, Any]:
        if defect_date is not None:
            if not isinstance(defect_date, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", defect_date
            ):
                raise ValueError("defect_date must be an ISO date (YYYY-MM-DD)")
            try:
                datetime.strptime(defect_date, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("defect_date must be a valid ISO date (YYYY-MM-DD)") from exc
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page must be a positive integer")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise ValueError("page_size must be between 1 and 100")
        if sort_by not in SORT_FIELDS or sort_order not in {"asc", "desc"}:
            raise ValueError("Unknown record sort field or direction")
        async with self._lock:
            payload = await self._context_by_id(context_id)
            cached_rows = self._records_cache.get(context_id)
            if cached_rows is None:
                snapshot = self._snapshots[payload["context"]["snapshot_id"]].snapshot
                cached_rows = [
                    event.as_record()
                    for event in analytics.filter_events(
                        snapshot, _scope(payload["context"]["filters"])
                    )
                ]
                cached_rows.sort(key=lambda row: (row["defect_id"], row["source_row"]))
                if len(self._records_cache) >= 64:
                    self._records_cache.pop(next(iter(self._records_cache)))
                self._records_cache[context_id] = cached_rows

            if defect_date is not None:
                base_rows = [row for row in cached_rows if row["defect_date"] == defect_date]
            else:
                base_rows = cached_rows

            missing: list[dict[str, Any]] = []
            present: list[dict[str, Any]] = []
            for row in base_rows:
                val = row[sort_by]
                if val is None or (sort_by == "severity" and val == "Unknown"):
                    missing.append(row)
                else:
                    present.append(row)

            key = (
                (lambda row: _SEVERITY_ORDER.get(row["severity"], -1))
                if sort_by == "severity"
                else (lambda row: row[sort_by])
            )
            present.sort(key=key, reverse=sort_order == "desc")
            rows = present + missing
            total = len(rows)
            page_count = max(1, math.ceil(total / page_size))
            page = min(page, page_count)
            return {
                "context": payload["context"],
                "record_filters": {"defect_date": defect_date},
                "total": total,
                "page": page,
                "page_size": page_size,
                "page_count": page_count,
                "sort_by": sort_by,
                "sort_order": sort_order,
                "rows": rows[(page - 1) * page_size : page * page_size],
                "status": payload["status"],
            }

    async def compare_scenario(self, context_id: str, scenario: str) -> dict[str, Any]:
        validate_scenario(scenario)
        async with self._lock:
            origin = await self._context_by_id(context_id)
            base_scope = _scope(origin["context"]["filters"])
            snapshot_id = origin["context"]["snapshot_id"]
            canonical_payload = await self._get_context(
                replace(base_scope, scenario=""),
                snapshot_id,
                permitted_missing_event_filters=origin["evidence"].get("scope_validation"),
            )
            selected_payload = await self._get_context(
                replace(base_scope, scenario=scenario),
                snapshot_id,
                permitted_missing_event_filters=origin["evidence"].get("scope_validation"),
            )
            comparison = analytics.compare_reports(
                canonical_payload["analysis"], selected_payload["analysis"]
            )
            comparison["source_contexts"] = {
                "canonical": canonical_payload["context"]["context_id"],
                "selected": selected_payload["context"]["context_id"],
            }
            return {
                "context": origin["context"],
                "canonical": canonical_payload,
                "selected": selected_payload,
                "comparison": comparison,
                "status": copy.deepcopy(self._status),
            }

    async def compare_periods(
        self,
        context_id: str,
        baseline_filters: dict[str, Any],
        comparison_filters: dict[str, Any],
        dimension: str = "production_line",
    ) -> dict[str, Any]:
        if not isinstance(baseline_filters, dict) or not isinstance(comparison_filters, dict):
            raise ValueError(
                "Period comparison accepts baseline_filters and comparison_filters objects"
            )
        if dimension not in analytics.COMPARISON_DIMENSIONS:
            raise ValueError("Unknown comparison dimension")
        async with self._lock:
            origin = await self._context_by_id(context_id)
            filters = origin["context"]["filters"]
            baseline_scope = _scope({**filters, **baseline_filters})
            comparison_scope = _scope({**filters, **comparison_filters})
            snapshot_id = origin["context"]["snapshot_id"]
            baseline = await self._get_context(baseline_scope, snapshot_id)
            comparison = await self._get_context(comparison_scope, snapshot_id)
            snapshot = self._snapshots[snapshot_id].snapshot
            result = analytics.compare_periods(
                snapshot, baseline_scope, comparison_scope, dimension
            )
            result["metadata"] = {
                key: origin["context"][key]
                for key in ("dataset_version", "policy_version", "snapshot_id")
            }
            result["source_contexts"] = {
                "baseline": baseline["context"]["context_id"],
                "comparison": comparison["context"]["context_id"],
            }
            return {
                "context": origin["context"],
                "baseline": baseline,
                "comparison": comparison,
                "decomposition": result,
                "status": copy.deepcopy(self._status),
            }

    async def trace_records(
        self,
        context_id: str,
        *,
        defect_id: int | None = None,
        source_row: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Trace limit must be between 1 and 100")
        if defect_id is not None and positive_integer(defect_id) != defect_id:
            raise ValueError("defect_id must be a positive integer")
        if source_row is not None and positive_integer(source_row) != source_row:
            raise ValueError("source_row must be a positive integer")
        if defect_id is not None and source_row is not None:
            raise ValueError("Choose defect_id or source_row for a trace")
        async with self._lock:
            payload = await self._context_by_id(context_id)
            snapshot = self._snapshots[payload["context"]["snapshot_id"]].snapshot
            if not snapshot.raw_records:
                raise SnapshotUnavailable(
                    "Original HTTP rows are unavailable in this server; refresh the source to inspect raw lineage"
                )
            raw = [json.loads(row) for row in snapshot.raw_records]
            if source_row is not None:
                if source_row > len(raw):
                    raise ValueError("source_row exceeds the received source rows")
                defect_id = positive_integer(raw[source_row - 1].get("defect_id"))
                identifiers = {defect_id} if defect_id is not None else set()
            elif defect_id is not None:
                identifiers = {defect_id}
            else:
                identifiers = {
                    event.defect_id
                    for event in analytics.filter_events(
                        snapshot, _scope(payload["context"]["filters"])
                    )[:limit]
                }
            scope = _scope(payload["context"]["filters"])
            interpreted = scenario_snapshot(snapshot, scope.scenario)
            selected_ids = {event.defect_id for event in analytics.filter_events(snapshot, scope)}
            matches = [
                {"source_row": index, "raw": row}
                for index, row in enumerate(raw, start=1)
                if positive_integer(row.get("defect_id")) in identifiers or index == source_row
            ]
            row_numbers = {row["source_row"] for row in matches}
            product_ids = {positive_integer(row["raw"].get("product_id")) for row in matches}
            master_rows = (
                list(csv.DictReader(io.StringIO(snapshot.master_bytes.decode("utf-8-sig"))))
                if snapshot.master_bytes
                else []
            )
            return {
                "context": payload["context"],
                "raw_source_kind": "immutable in-memory rows from successful HTTP fetch",
                "raw_records": matches[:limit],
                "raw_match_count": len(matches),
                "truncated": len(matches) > limit,
                "canonical_records": [
                    event.as_record() for event in snapshot.events if event.defect_id in identifiers
                ][:limit],
                "processed_records": [
                    {**event.as_record(), "in_selected_scope": event.defect_id in selected_ids}
                    for event in interpreted.events
                    if event.defect_id in identifiers
                ][:limit],
                "master_lineage": [
                    {
                        "source_row": index,
                        "raw": row,
                        "processed": next(
                            (
                                asdict(product)
                                for product in snapshot.products
                                if product.product_id == positive_integer(row.get("product_id"))
                            ),
                            None,
                        ),
                        "normalization_audit": [
                            asdict(entry)
                            for entry in snapshot.audit
                            if entry.source == "master" and entry.source_row == index
                        ],
                    }
                    for index, row in enumerate(master_rows, start=2)
                    if positive_integer(row.get("product_id")) in product_ids
                ],
                "normalization_audit": [
                    asdict(entry)
                    for entry in snapshot.audit
                    if entry.source == "defects" and entry.source_row in row_numbers
                ],
                "scenario_audit": (
                    [
                        asdict(entry)
                        for entry in interpreted.audit
                        if entry.source == "defects"
                        and entry.source_row in row_numbers
                        and entry.code == "scenario_date_projection"
                    ]
                    if scope.scenario in {"ambiguous_dm", "year_2024"}
                    else [
                        {
                            "source_row": event.source_row,
                            "record_id": str(event.defect_id),
                            "field": "repair_cost",
                            "raw_value": str(event.repair_cost),
                            "processed_value": None,
                            "action": "scenario",
                            "code": "scenario_excluded_cost_10",
                            "policy_version": payload["context"]["policy_version"],
                            "detail": SCENARIOS[scope.scenario]["assumption"],
                        }
                        for event in snapshot.events
                        if event.defect_id in identifiers
                        and event.repair_cost
                        == SCENARIOS[scope.scenario]["parameters"]["repair_cost_equal"]
                    ]
                    if scope.scenario == "exclude_cost_10"
                    else []
                ),
                "quality_scope": QUALITY_SCOPE,
                "status": payload["status"],
            }

    async def _export(self, context_id: str, name: str) -> bytes:
        async with self._lock:
            await self._context_by_id(context_id)
            path = EvidenceLedger(self._contexts[context_id].directory).resolve_artifact(
                "artifacts/" + name
            )
            content = path.read_bytes()
            # Read and compare the same bytes that the HTTP response will contain.
            manifest = EvidenceLedger(self._contexts[context_id].directory).artifacts()
            expected = next(
                entry["sha256"] for entry in manifest if entry["path"] == "artifacts/" + name
            )
            if sha256(content) != expected:
                raise EvidenceIntegrityError(
                    "The requested export changed during integrity verification"
                )
            return content

    async def export_csv(self, context_id: str, locale: str | None = None) -> bytes:
        if locale is not None and locale not in {"en", "vi"}:
            raise ValueError("Export locale must be en or vi")
        return await self._export(
            context_id, f"records.{locale}.csv" if locale else "cleaned_records.csv"
        )

    async def export_zip(self, context_id: str, locale: str | None = None) -> bytes:
        if locale is not None and locale not in {"en", "vi"}:
            raise ValueError("Export locale must be en or vi")
        return await self._export(
            context_id, f"evidence.{locale}.zip" if locale else "evidence.zip"
        )

    async def export_brief(self, context_id: str, locale: str | None = None) -> bytes:
        """Reformat the saved, receipt-bound analysis of one context as a shareable brief."""
        if locale is not None and locale not in {"en", "vi"}:
            raise ValueError("Export locale must be en or vi")
        payload = await self.context_by_id(context_id)
        return render_brief(payload, locale or "en").encode("utf-8")

    async def export_quality_workbook(self) -> bytes:
        """Download the separately completed case deliverable, outside any context."""
        path = self.root / "DATA_QUALITY.xlsx"
        if path.is_symlink() or path.resolve().parent != self.root:
            raise EvidenceIntegrityError(
                "The completed quality workbook is outside the project root"
            )
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as error:
            raise ContextNotFound(
                "The completed full-case quality workbook is not available"
            ) from error


async def _http(operation, *args, **kwargs):
    try:
        return await operation(*args, **kwargs)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except ContextNotFound as error:
        raise HTTPException(404, str(error)) from error
    except (SnapshotUnavailable, EvidenceIntegrityError) as error:
        raise HTTPException(409, str(error)) from error
    except SourceError as error:
        raise HTTPException(503, str(error)) from error


def register_routes(app: FastAPI, service: AnalysisService) -> None:
    def query(request: Request, allowed: set[str]) -> dict[str, str]:
        items = list(request.query_params.multi_items())
        if any(name not in allowed for name, _ in items):
            raise HTTPException(422, "Unknown query parameter")
        if len({name for name, _ in items}) != len(items):
            raise HTTPException(422, "Query parameters cannot be repeated")
        return dict(items)

    @app.get("/api/dashboard")
    async def dashboard(request: Request) -> dict[str, Any]:
        values = query(request, set(FILTER_KEYS) | {"snapshot_id"})
        snapshot_id = values.pop("snapshot_id", None)
        return await _http(service.get_context, values, snapshot_id)

    @app.post("/api/dashboard/refresh")
    async def refresh(request: Request) -> dict[str, Any]:
        try:
            raw = await request.body()
            body = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError) as error:
            raise HTTPException(422, "Refresh body must be JSON") from error
        if not isinstance(body, dict) or set(body) - {"filters"}:
            raise HTTPException(422, "Refresh body accepts only a filters object")
        return await _http(service.refresh, body.get("filters"))

    @app.get("/api/dashboard/quality-workbook")
    async def quality_workbook(request: Request) -> Response:
        query(request, set())
        content = await _http(service.export_quality_workbook)
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": 'attachment; filename="DATA_QUALITY-full-case.xlsx"',
                "X-Analysis-Scope": "completed-full-case",
                "ETag": '"' + sha256(content) + '"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/dashboard/contexts/{context_id}")
    async def context(context_id: str) -> dict[str, Any]:
        return await _http(service.context_by_id, context_id)

    @app.get("/api/dashboard/contexts/{context_id}/records")
    async def records(
        context_id: str,
        request: Request,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        sort_by: str = "defect_date",
        sort_order: str = "desc",
        defect_date: str | None = None,
    ) -> dict[str, Any]:
        query(request, {"page", "page_size", "sort_by", "sort_order", "defect_date"})
        return await _http(
            service.records,
            context_id,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            defect_date=defect_date,
        )

    @app.get("/api/dashboard/contexts/{context_id}/evidence")
    async def evidence(context_id: str) -> dict[str, Any]:
        return (await _http(service.context_by_id, context_id))["evidence"]

    @app.get("/api/dashboard/contexts/{context_id}/trace")
    async def trace(
        context_id: str,
        request: Request,
        defect_id: int | None = Query(None, ge=1),
        source_row: int | None = Query(None, ge=1),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        query(request, {"defect_id", "source_row", "limit"})
        return await _http(
            service.trace_records,
            context_id,
            defect_id=defect_id,
            source_row=source_row,
            limit=limit,
        )

    @app.get("/api/dashboard/contexts/{context_id}/scenarios")
    async def scenarios(context_id: str, request: Request, scenario: str = "") -> dict[str, Any]:
        query(request, {"scenario"})
        return await _http(service.compare_scenario, context_id, scenario)

    @app.get("/api/dashboard/contexts/{context_id}/scenarios/{scenario}")
    async def scenario_context(context_id: str, scenario: str, request: Request) -> dict[str, Any]:
        query(request, set())
        return await _http(service.compare_scenario, context_id, scenario)

    @app.post("/api/dashboard/contexts/{context_id}/compare-periods")
    async def compare_period_context(context_id: str, request: Request) -> dict[str, Any]:
        query(request, set())
        try:
            body = await request.json()
        except (ValueError, UnicodeError) as error:
            raise HTTPException(422, "Period comparison body must be JSON") from error
        if (
            not isinstance(body, dict)
            or set(body) - {"baseline_filters", "comparison_filters", "dimension"}
            or not {"baseline_filters", "comparison_filters"}.issubset(body)
        ):
            raise HTTPException(
                422,
                "Period comparison requires baseline_filters and comparison_filters; dimension is optional",
            )
        return await _http(
            service.compare_periods,
            context_id,
            body["baseline_filters"],
            body["comparison_filters"],
            body.get("dimension", "production_line"),
        )

    @app.get("/api/dashboard/contexts/{context_id}/export.csv")
    async def export_csv(context_id: str, request: Request, locale: str | None = None) -> Response:
        query(request, {"locale"})
        content = await _http(service.export_csv, context_id, locale)
        return Response(
            content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="records-{context_id[:16]}-{locale or "en"}.csv"',
                "ETag": '"' + sha256(content) + '"',
                "Cache-Control": "no-store",
                "Content-Language": locale or "en",
                "X-Currency-Unit": "local currency",
                "X-Value-Locale": "machine",
            },
        )

    @app.get("/api/dashboard/contexts/{context_id}/brief.md")
    async def export_brief(
        context_id: str, request: Request, locale: str | None = None
    ) -> Response:
        query(request, {"locale"})
        content = await _http(service.export_brief, context_id, locale)
        return Response(
            content,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="quality-brief-{context_id[:16]}-{locale or "en"}.md"',
                "ETag": '"' + sha256(content) + '"',
                "Cache-Control": "no-store",
                "Content-Language": locale or "en",
                "X-Currency-Unit": "local currency",
            },
        )

    @app.get("/api/dashboard/contexts/{context_id}/export.zip")
    async def export_zip(context_id: str, request: Request, locale: str | None = None) -> Response:
        query(request, {"locale"})
        content = await _http(service.export_zip, context_id, locale)
        return Response(
            content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="evidence-{context_id[:16]}-{locale or "en"}.zip"',
                "ETag": '"' + sha256(content) + '"',
                "Cache-Control": "no-store",
                "Content-Language": locale or "en",
                "X-Currency-Unit": "local currency",
                "X-Value-Locale": "machine",
            },
        )
