"""Product Agent invariants with real deterministic tools and no paid requests."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import fields

import pytest
from analyst.app import Workspace, create_app
from analyst.case_study.analytics import Scope
from analyst.case_study.ingestion import SourceError
from analyst.config import Settings
from analyst.investigation import response_language, scope_grants, user_changes
from analyst.product_tools import ToolEnvelope
from analyst.runtime import AnalystReport, AnalystRuntime, ReportDraft, audit_report
from analyst.store import SessionStore
from analyst.tools import AnalystTools
from fastapi.testclient import TestClient
from test_dashboard import Loader, service


def filters(**changes):
    return {**{field.name: None for field in fields(Scope)}, **changes}


@pytest.mark.asyncio
async def test_bounded_tools_share_snapshot_compare_and_do_not_expose_execution(tmp_path):
    subject = service(tmp_path)
    bound = await subject.get_context()
    tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    runtime = AnalystRuntime(
        Settings(mode="live", api_key="offline-test", memory_enabled=False),
        tools,
        emit,
        analysis_context=bound,
        analysis_service=subject,
    )
    try:
        names = {tool.name for tool in runtime.sdk_tools()}
        assert not names & {
            "local_python_repl",
            "local_sql_engine",
            "dataset_profiler",
            "run_case_study",
            "delegate_data_engineer",
        }
        result = json.loads(
            await runtime._call("analysis_query", {"section": "kpis", "filters": filters()})
        )
        ToolEnvelope.model_validate(result)
        assert result["scope"] == bound["context"]["filters"]
        assert result["dataset_version"] == bound["context"]["dataset_version"]
        assert result["policy_version"] == "canonical-v2"
        assert result["result"]["analysis"]["kpis"] == bound["analysis"]["kpis"]
        compared = json.loads(
            await runtime._call(
                "analysis_compare",
                {
                    "baseline_filters": filters(start_month="2024-01", end_month="2024-01"),
                    "comparison_filters": filters(start_month="2024-06", end_month="2024-06"),
                    "dimension": "production_line",
                },
            )
        )
        assert compared["status"] == "completed", compared
        assert (
            compared["result"]["baseline"]["context"]["snapshot_id"]
            == bound["context"]["snapshot_id"]
        )
        scenario = json.loads(
            await runtime._call("scenario_compare", {"scenario": "exclude_cost_10"})
        )
        assert (
            scenario["result"]["canonical"]["analysis"]["kpis"]["event_count"]
            == scenario["result"]["selected"]["analysis"]["kpis"]["event_count"]
        )
        assert (
            scenario["result"]["selected"]["analysis"]["kpis"]["cost_coverage_pct"]
            < bound["analysis"]["kpis"]["cost_coverage_pct"]
        )
        assert runtime.analysis_context == bound
        table = json.loads(
            await runtime._call(
                "report_table",
                {
                    "receipt_id": compared["receipt_id"],
                    "path": "$.decomposition.rows",
                    "columns": [
                        "name",
                        "event_count_change",
                        "critical_count_change",
                        "known_repair_cost_change",
                    ],
                    "offset": 0,
                    "limit": 10,
                },
            )
        )
        # Even view utilities retain the same common envelope and source scope.
        ToolEnvelope.model_validate(table)
        assert table["status"] == "completed", table
        assert table["result_reference"] == table["receipt_id"]
        invalid = json.loads(
            await runtime._call(
                "analysis_records",
                {"page": 1, "page_size": 101, "sort_by": "defect_date", "sort_order": "asc"},
            )
        )
        assert invalid["status"] == "failed" and invalid["result"]["error_type"] == "validation"
        with pytest.raises(ValueError, match="disabled"):
            await runtime._call("local_sql_engine", {"query": "SELECT 1"})
        assert tools.ledger.verify_integrity() == []
    finally:
        await tools.close()


@pytest.mark.asyncio
async def test_investigation_is_evidence_checked_durable_and_idempotent(tmp_path):
    subject = service(tmp_path)
    bound = await subject.get_context()
    state = SessionStore(tmp_path / "sessions")
    sid = state.create_session()["id"]
    tools = AnalystTools(tmp_path / "run", [], session_id=sid, memory_enabled=False)

    async def emit(*_):
        pass

    runtime = AnalystRuntime(
        Settings(mode="live", api_key="offline-test", memory_enabled=False),
        tools,
        emit,
        analysis_context=bound,
        analysis_service=subject,
        state_store=state,
    )
    try:
        query = json.loads(
            await runtime._call("analysis_query", {"section": "kpis", "filters": filters()})
        )
        identity = next(
            item["id"]
            for item in query["numeric_evidence"]
            if item["path"] == "$.analysis.kpis.event_count"
        )
        arguments = {
            "question": "Investigate logged events",
            "summary": "The retained events need investigation.",
            "findings": ["Logged events: [value:" + identity + "]"],
            "hypotheses": [
                {"id": None, "text": "Recording coverage may differ.", "status": "open"}
            ],
            "next_steps": ["Collect inspection exposure."],
            "priorities": [
                {
                    "evidence": "Logged events: [value:" + identity + "]",
                    "action": "Validate exposure and recurring failures.",
                    "owner": "Quality Engineer (proposed role)",
                    "metric": "Logged events and cost coverage; collect inspected units for future rates.",
                }
            ],
        }
        first = await runtime._call("investigation_update", arguments, provider_call_id="same-call")
        assert first == await runtime._call(
            "investigation_update", arguments, provider_call_id="same-call"
        )
        saved = state.investigation(sid)
        assert saved["version"] == 1 and saved["findings"][0]["status"] == "unverified"
        assert saved["content_status"] == "unverified" and saved["semantic_review"] is None
        assert saved["hypotheses"][0]["status"] == "open"
        artifact = json.loads(first)["result"]["artifact"]
        assert (
            json.loads(tools.ledger.resolve_artifact(artifact["path"]).read_text(encoding="utf-8"))[
                "version"
            ]
            == 1
        )
        rejected = json.loads(
            await runtime._call(
                "investigation_update", {**arguments, "findings": ["999999 events"]}
            )
        )
        assert rejected["status"] == "failed" and state.investigation(sid)["version"] == 1
        # An arbitrary qualitative statement remains explicitly unverified.
        qualitative = json.loads(
            await runtime._call(
                "investigation_update",
                {
                    **arguments,
                    "findings": [
                        "Automated Testing is the most effective method and proves the root cause."
                    ],
                },
            )
        )
        assert qualitative["result"]["investigation"]["findings"][0]["status"] == "unverified"
        assert qualitative["result"]["investigation"]["findings"][0]["evidence"] == []
        latest = state.investigation(sid)
        edited = user_changes(
            latest,
            {
                "expected_version": latest["version"],
                "findings": [{**latest["findings"][0], "text": "User hypothesis to check."}],
            },
        )
        state.save_investigation(sid, latest["version"], edited)
        assert runtime.product_tools.pending_state() is None
        assert await runtime.product_tools.review_investigation({}) is None
        assert state.investigation(sid)["findings"][0]["status"] == "user_edited"
        other_sid = state.create_session()["id"]
        assert state.investigation(other_sid)["findings"] == []
        state.close()
        state = SessionStore(tmp_path / "sessions")
        assert state.investigation(sid)["findings"][0]["text"] == "User hypothesis to check."
    finally:
        await tools.close()
        state.close()


@pytest.mark.asyncio
async def test_ui_scope_commands_require_direct_authority_support_undo_and_reject_stale_run(
    tmp_path,
):
    subject = service(tmp_path)
    bound = await subject.get_context()
    settings = Settings(
        mode="mock",
        state_dir=str(tmp_path / "workspace"),
        memory_enabled=False,
        enable_file_search=False,
    )
    workspace = Workspace(settings, analysis_service=subject)
    sid = workspace.store.create_session()["id"]
    cid = bound["context"]["context_id"]
    rid, _ = workspace.store.begin_run(sid, cid)
    await workspace.set_ui_scope(sid, cid)
    workspace.ui_bindings[rid] = cid
    workspace.ui_authorities[rid] = scope_grants("Only Critical, then undo.", bound["options"])
    try:
        command = (
            await workspace.execute_ui_command(
                sid,
                rid,
                cid,
                {
                    "action": "apply_scope",
                    "panel": "dashboard",
                    "filters": filters(severity="Critical"),
                    "command_id": None,
                },
            )
        )["command"]
        assert command["context_id"] != cid
        restored = await workspace.undo_ui_command(
            sid, command["command_id"], command["context_id"]
        )
        assert restored["command"]["context_id"] == cid
        assert (await workspace.undo_ui_command(sid, command["command_id"], cid))["command"][
            "command_id"
        ] == restored["command"]["command_id"]
        workspace.ui_bindings[rid] = cid
        with pytest.raises(ValueError, match="authority"):
            await workspace.execute_ui_command(
                sid,
                rid,
                cid,
                {
                    "action": "apply_scope",
                    "panel": "dashboard",
                    "filters": filters(production_line="Line-2"),
                    "command_id": None,
                },
            )
        alternate = await subject.get_context({"production_line": "Line-2"})
        await workspace.set_ui_scope(sid, alternate["context"]["context_id"])
        with pytest.raises(ValueError, match="stale"):
            await workspace.execute_ui_command(
                sid,
                rid,
                cid,
                {
                    "action": "apply_scope",
                    "panel": "dashboard",
                    "filters": filters(severity="Critical"),
                    "command_id": None,
                },
            )
    finally:
        await workspace.close()


def test_normal_no_key_is_unavailable_with_no_fixture_answer(tmp_path):
    settings = Settings(
        mode="auto", api_key="", state_dir=str(tmp_path / "state"), enable_file_search=False
    )
    with TestClient(create_app(settings, analysis_service=service(tmp_path))) as client:
        config = client.get("/api/config").json()
        assert config["mode"] == "unavailable" and config["ai_available"] is False
        sid = client.post("/api/sessions").json()["id"]
        reply = client.post(f"/api/sessions/{sid}/prompt", json={"text": "Prepare a brief"})
        assert reply.status_code == 503 and reply.json()["detail"]["reason"] == "missing_api_key"
        assert client.get(f"/api/sessions/{sid}").json()["runs"] == []
        assert client.get("/api/dashboard").status_code == 200


def test_unbound_case_prompt_reports_http_source_error_before_model_dispatch(tmp_path):
    settings = Settings(
        mode="live",
        api_key="offline-test",
        state_dir=str(tmp_path / "state"),
        enable_file_search=False,
    )
    with TestClient(
        create_app(
            settings, analysis_service=service(tmp_path, Loader(SourceError("HTTP source is down")))
        )
    ) as client:
        sid = client.post("/api/sessions").json()["id"]
        reply = client.post(f"/api/sessions/{sid}/prompt", json={"text": "Investigate the case"})
        assert reply.status_code == 503 and reply.json()["detail"]["code"] == "source_unavailable"
        assert "HTTP source is down" in reply.json()["detail"]["message"]
        assert client.get(f"/api/sessions/{sid}").json()["runs"] == []


def test_cancel_stops_real_task_and_replay_retains_its_terminal_state(tmp_path):
    entered = threading.Event()

    class WaitingRuntime:
        def __init__(self, settings, toolkit, emit, **kwargs):
            pass

        async def run(self, prompt):
            entered.set()
            await asyncio.Event().wait()

    settings = Settings(
        mode="mock",
        state_dir=str(tmp_path / "state"),
        memory_enabled=False,
        enable_file_search=False,
    )
    with TestClient(
        create_app(settings, runtime_factory=WaitingRuntime, analysis_service=service(tmp_path))
    ) as client:
        sid = client.post("/api/sessions").json()["id"]
        assert (
            client.post(
                f"/api/sessions/{sid}/prompt", json={"text": "Wait for cancellation"}
            ).status_code
            == 200
        )
        assert entered.wait(timeout=3)
        run_id = client.get(f"/api/sessions/{sid}").json()["runs"][-1]["id"]
        assert client.post(f"/api/sessions/{sid}/cancel", json={"run_id": run_id}).json()[
            "cancel_requested"
        ]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            saved = client.get(f"/api/sessions/{sid}").json()
            if saved["runs"][-1]["state"] == "cancelled":
                break
            time.sleep(0.01)
        assert saved["runs"][-1]["state"] == "cancelled"
        assert any(
            event["type"] == "status" and event["data"]["state"] == "cancelled"
            for event in saved["events"]
        )
        assert (
            client.post(f"/api/sessions/{sid}/cancel", json={}).json()["cancel_requested"] is False
        )


@pytest.mark.asyncio
async def test_locale_changes_display_and_audit_without_changing_evidence(tmp_path):
    subject = service(tmp_path)
    payload = await subject.get_context()
    tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)

    async def emit(*_):
        pass

    runtime = AnalystRuntime(
        Settings(mode="mock"), tools, emit, analysis_context=payload, language="vi"
    )
    try:
        entry = tools.ledger.record("known", "result", {"amount": 1090.25})
        receipt = runtime.model_receipt(entry)
        identity = receipt["numeric_evidence"][0]["id"]
        report = runtime.resolve_report(ReportDraft(report="Chi phí: [value:" + identity + ":2dp]"))
        assert "1.090,25" in report.report and report.number_format == "vi-VN"
        assert audit_report(report, tools.ledger)["verified"]
        wrong = AnalystReport(
            report="Chi phí: 1.090,26 [claim:" + identity + "]",
            claims=report.claims,
            number_format="vi-VN",
        )
        assert not audit_report(wrong, tools.ledger)["verified"]
        assert tools.ledger.verify_claims([claim.model_dump() for claim in report.claims])[
            "verified"
        ]
        assert (
            scope_grants(
                "Exclude repair cost 10, compare with canonical and apply the scenario",
                payload["options"],
            )["filters"]["scenario"]
            == "exclude_cost_10"
        )
        assert response_language("Điều tra chi phí", "en") == "vi"
        assert response_language("Prepare a handoff brief", "vi") == "en"
    finally:
        await tools.close()
