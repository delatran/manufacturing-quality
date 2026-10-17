"""Saved report provenance must preserve the actual report scope across reloads."""

from __future__ import annotations

import asyncio

import pytest
from analyst.app import Workspace
from analyst.config import Settings
from analyst.store import SessionStore
from test_dashboard import service


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["derived", "comparison", "legacy_data"])
async def test_completed_run_persists_report_sources_without_inventing_a_base_scope(
    tmp_path, monkeypatch, target_kind
):
    def disallow_provider(*args, **kwargs):
        raise AssertionError("Report provenance tests must not construct a provider client")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", disallow_provider)
    analysis = service(tmp_path)
    base = await analysis.get_context({"production_line": "Line-1"})
    derived = await analysis.get_context({"production_line": "Line-1", "severity": "Critical"})
    january = await analysis.get_context(
        {"production_line": "Line-1", "start_month": "2024-01", "end_month": "2024-01"}
    )
    june = await analysis.get_context(
        {"production_line": "Line-1", "start_month": "2024-06", "end_month": "2024-06"}
    )
    report_contexts = [derived] if target_kind == "derived" else [january, june]
    manifest = {
        "version": 1,
        "language": "en",
        "locale": "en-US",
        "instructions_version": "synthetic-instructions",
        "tools_version": "synthetic-tools",
        "contexts": [item["context"] for item in report_contexts],
        "sources": [],
        "references": [],
    }

    class ReportRuntime:
        def __init__(self, settings, toolkit, emit, **kwargs):
            self.toolkit = toolkit

        async def run(self, prompt):
            path = self.toolkit.run_dir / "artifacts" / "report.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<p>Explicit synthetic report.</p>", encoding="utf-8")
            artifact = self.toolkit.ledger.register_artifact(
                path,
                kind="table" if target_kind == "legacy_data" else "business_report",
                context=derived["context"] if target_kind == "derived" else None,
            )
            return {
                "status": "completed",
                "report": "Explicit synthetic report.",
                "qa": {"verified": True, "errors": []},
                "artifacts": [artifact],
                "delivery": {"version": 1, "artifacts": [artifact], "source_manifest": manifest},
            }

    workspace = Workspace(
        Settings(mode="mock", state_dir=str(tmp_path / "workspace"), memory_enabled=False),
        runtime_factory=ReportRuntime,
        analysis_service=analysis,
    )
    sid = workspace.store.create_session()["id"]
    base_id = base["context"]["context_id"]
    try:
        await workspace.prompt(sid, "Prepare this report from the selected data.", base_id)
        await asyncio.wait_for(workspace.tasks[sid], timeout=15)
        saved = workspace.store.investigation(sid)["artifacts"][0]
        assert saved["run_id"] == workspace.store.session(sid)["runs"][-1]["id"]
        assert saved["url"].startswith(f"/api/sessions/{sid}/runs/")
        if target_kind == "derived":
            assert saved["context_id"] == derived["context"]["context_id"] != base_id
            assert saved["scope"] == derived["context"]["filters"]
        elif target_kind == "comparison":
            assert saved["context_id"] is None
        else:
            assert saved["context_id"] == base_id
        if target_kind != "legacy_data":
            assert saved["source_manifest"] == manifest
    finally:
        await workspace.close()
    restored = SessionStore(workspace.state_dir)
    try:
        assert restored.investigation(sid)["artifacts"][0] == saved
    finally:
        restored.close()
