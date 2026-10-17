"""Source capability metadata never becomes retrieved analytical evidence."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from analyst.config import Settings
from analyst.evidence import EvidenceLedger
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    ReportDraft,
    comparative_aggregates,
    verified_source_links,
)
from fastapi.testclient import TestClient
from test_operations_integration import analysis_service, completed_runs

ROOT = Path(__file__).resolve().parents[1]


async def emit(*_):
    pass


def runtime_at(tmp_path, paths=(), sources=None, context=None):
    return AnalystRuntime(
        Settings(root=ROOT, mode="mock"),
        SimpleNamespace(ledger=EvidenceLedger(tmp_path), run_dir=tmp_path, input_paths=list(paths)),
        emit,
        input_sources=sources,
        analysis_context=context,
    )


def test_catalog_requires_exact_trusted_origin_and_never_uses_basename(tmp_path):
    master = tmp_path / "uploads" / "master_data.csv"
    legacy = runtime_at(tmp_path / "legacy", [master])
    assert legacy.source_catalog()["local_files"][0]["origin"] == "unknown"
    metadata = [{"path": str(master.resolve()), "origin": "upload", "name": "master_data.csv"}]
    runtime = runtime_at(tmp_path / "trusted", [master], metadata)
    metadata[0]["origin"] = "bundled_reference"
    assert runtime.source_catalog()["local_files"][0]["origin"] == "upload"
    wrong_path = [{"path": str(ROOT / "data" / "master_data.csv"), "origin": "bundled_reference"}]
    assert (
        runtime_at(tmp_path / "different", [master], wrong_path).source_catalog()["local_files"][0][
            "origin"
        ]
        == "unknown"
    )
    duplicate = [
        {"path": str(master.resolve()), "origin": origin}
        for origin in ("upload", "bundled_reference")
    ]
    assert (
        runtime_at(tmp_path / "conflict", [master], duplicate).source_catalog()["local_files"][0][
            "origin"
        ]
        == "unknown"
    )


def test_retriever_attempts_come_only_from_actual_receipts_without_claims_or_source_authority(
    tmp_path,
):
    runtime = runtime_at(tmp_path)
    before = runtime.source_catalog()
    retriever = before["retrievers"][0]
    assert retriever["tool"] == "run_case_study" and retriever["enabled"] is True
    assert retriever["last_attempt"] == {"status": "not_yet_attempted"}
    assert runtime.toolkit.ledger.records() == [] and runtime.available_claims() == {}
    assert verified_source_links(runtime.toolkit.ledger) == []
    failed = runtime.toolkit.ledger.record(
        "run_case_study",
        "# failed retrieval",
        {
            "ok": False,
            "error": "Parsing failed after response",
            "qa": {"status": "unverified"},
            "provenance": {"source_url": "https://source.invalid/2024.json", "record_count": 0},
        },
    )
    attempt = runtime.source_catalog()["retrievers"][0]["last_attempt"]
    assert attempt == {
        "status": "failed",
        "execution_id": failed["execution_id"],
        "error": "Parsing failed after response",
        "analysis_verification_status": "unverified",
        "observed_provenance": {
            "source_url": "https://source.invalid/2024.json",
            "record_count": 0,
        },
    }
    assert verified_source_links(runtime.toolkit.ledger) == [] and runtime.available_claims() == {}
    returned = runtime.toolkit.ledger.record(
        "run_case_study",
        "# returned but unverified",
        {
            "qa": {"status": "unverified"},
            "provenance": {"source_url": "https://source.invalid/2024.json"},
        },
    )
    attempt = runtime.source_catalog()["retrievers"][0]["last_attempt"]
    assert (
        attempt["status"] == "returned_result"
        and attempt["analysis_verification_status"] == "unverified"
    )
    assert attempt["execution_id"] == returned["execution_id"]
    support, errors = runtime.qa_evidence(
        AnalystReport(report="Source retrieval is pending verification.", claims=[])
    )
    assert not errors and support["source_catalog"] == runtime.source_catalog()
    assert support["numeric_support"] == [] and support["verified_links"] == []


def test_dashboard_catalog_preserves_bound_scope_and_freshness_without_enabling_standalone_substitution(
    tmp_path,
):
    context = {
        "context": {"context_id": "frozen-context", "filters": {"production_line": "Line-4"}},
        "status": {"stale": True},
    }
    runtime = runtime_at(tmp_path, context=context)
    catalog = runtime.source_catalog()
    retrievers = {item["tool"]: item for item in catalog["retrievers"]}
    assert set(retrievers) == {"dashboard_context", "dashboard_date_sensitivity"}
    assert retrievers["dashboard_context"]["scope"] == {"production_line": "Line-4"}
    assert retrievers["dashboard_context"]["source_status"] == {"stale": True}
    assert all(item["context_id"] == "frozen-context" for item in retrievers.values())


@pytest.mark.parametrize("mode", ["bundled", "upload", "dashboard_upload"])
def test_app_supplies_actual_origins_and_restores_upload_origin_after_reopen(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls: cls(state_dir=str(tmp_path / "import-state"), mode="mock")),
    )
    from analyst.app import create_app

    captured = []

    class CaptureRuntime:
        def __init__(self, settings, toolkit, emit, **options):
            self.runtime = AnalystRuntime(settings, toolkit, emit, **options)

        async def run(self, prompt):
            captured.append(copy.deepcopy(self.runtime.source_catalog()))
            return {
                "status": "completed",
                "history": [],
                "usage": [],
                "cost_usd": 0,
                "elapsed_seconds": 0,
            }

    config = Settings(
        root=ROOT,
        state_dir=str(tmp_path / "state"),
        mode="mock",
        memory_enabled=False,
        enable_file_search=False,
    )
    application = create_app(
        config, runtime_factory=CaptureRuntime, analysis_service=analysis_service(tmp_path)
    )
    with TestClient(application) as client:
        sid = client.post("/api/sessions").json()["id"]
        if mode != "bundled":
            uploaded = client.post(
                f"/api/sessions/{sid}/files",
                files={"files": ("master_data.csv", b"group,value\na,17\n", "text/csv")},
            )
            assert uploaded.status_code == 200
        body = {"text": "Inspect source availability."}
        if mode == "dashboard_upload":
            body["analysis_context_id"] = client.get("/api/dashboard").json()["context"][
                "context_id"
            ]
        assert client.post(f"/api/sessions/{sid}/prompt", json=body).status_code == 200
        completed_runs(client, sid)
    files = captured[0]["local_files"]
    if mode == "bundled":
        assert len(files) == 1 and files[0]["origin"] == "bundled_reference"
        assert files[0]["path"] == str((ROOT / "data" / "master_data.csv").resolve())
    else:
        uploads = [item for item in files if item["origin"] == "upload"]
        assert len(uploads) == 1 and uploads[0]["name"] == "master_data.csv"
        assert uploads[0]["path"] != str((ROOT / "data" / "master_data.csv").resolve())
        exports = [item for item in files if item["origin"] == "dashboard_export"]
        assert bool(exports) is (mode == "dashboard_upload")
        assert all(item["context_id"] == body.get("analysis_context_id") for item in exports)
        assert not any(item["origin"] == "bundled_reference" for item in files)
    # A new Workspace reconstructs origins from durable upload metadata.
    reopened = create_app(
        config, runtime_factory=CaptureRuntime, analysis_service=analysis_service(tmp_path)
    )
    with TestClient(reopened) as client:
        assert client.post(f"/api/sessions/{sid}/prompt", json=body).status_code == 200
        completed_runs(client, sid, count=2)
    assert captured[1]["local_files"] == captured[0]["local_files"]


def test_comparative_evidence_preserves_actual_rows_nulls_ties_counts_and_scope(tmp_path):
    runtime = runtime_at(tmp_path)
    scope = {"production_line": "Line-4"}
    cells = [
        {"defect_location": "Component", "severity": "Critical", "count": 2, "row_pct": 50.0},
        {"defect_location": "Surface", "severity": "Critical", "count": 3, "row_pct": 50.0},
        {"defect_location": "Internal", "severity": "Critical", "count": 0, "row_pct": None},
    ]
    analysis = {
        "scope": scope,
        "location_severity": {"cells": cells},
        "costs": {"by_type": [{"defect_type": "Functional", "known_cost": None, "known_count": 0}]},
    }
    entry = runtime.toolkit.ledger.record(
        "dashboard_context",
        "# canonical comparator receipt",
        analysis,
        execution_kind="canonical_receipt_import",
        analysis_context={
            "context_id": "synthetic-catalog-line-4",
            "snapshot_id": "synthetic-catalog-snapshot",
            "dataset_version": "synthetic-catalog-dataset",
            "policy_version": "synthetic-catalog-policy",
            "scenario": "",
            "filters": scope,
        },
    )
    view = runtime.model_receipt(entry)
    claim = next(
        item
        for item in view["numeric_evidence"]
        if item["path"] == "$.location_severity.cells[0].row_pct"
    )
    report = runtime.resolve_report(
        ReportDraft(
            report=f"Component shares the highest Critical proportion at [value:{claim['id']}]%."
        )
    )
    support, errors = runtime.qa_evidence(report)
    assert not errors
    groups = next(
        item for item in support["qualitative_support"] if item["tool"] == "dashboard_context"
    )["comparative_aggregates"]
    locations = next(group for group in groups if group["path"] == "$.location_severity.cells")
    assert locations == {
        "execution_id": entry["execution_id"],
        "path": "$.location_severity.cells",
        "scope": scope,
        "rows": cells,
    }
    assert any(group["rows"][0].get("known_cost", "missing") is None for group in groups)
    assert support["numeric_support"][0]["path"] == claim["path"]
    for tool, kind, result in [
        ("local_python_repl", "executed", analysis),
        ("dashboard_context", "executed", analysis),
        ("dashboard_context", "canonical_receipt_import", {"ok": False, **analysis}),
        ("run_case_study", "executed", {"qa": {"status": "unverified"}, "analysis": analysis}),
    ]:
        candidate = {
            "tool": tool,
            "execution_kind": kind,
            "execution_id": "fixture",
            "result": result,
        }
        assert comparative_aggregates(candidate) == []
    helper = {
        "tool": "run_case_study",
        "execution_id": "helper",
        "result": {"qa": {"status": "verified"}, "analysis": analysis},
    }
    assert comparative_aggregates(helper)[0]["path"] == "$.analysis.location_severity.cells"
