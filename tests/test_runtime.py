from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest
from agents import (
    Runner,
)
from analyst.config import Settings
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.model import (
    ProviderStopped,
    compact_items,
    dump,
)
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    NumericClaim,
    _message,
    audit_exported_reports,
    audit_report,
    metadata_label,
)
from analyst.tools import AnalystTools
from openai.types.responses import ResponseFunctionToolCall


def canonical_context(tmp_path, *, filters=None, event_count=12, snapshot_id="frozen-snapshot"):
    scope = {
        "start_month": "2024-01",
        "end_month": "2024-02",
        "production_line": "Line-3",
        "category": "",
        "product_id": "",
        "severity": "",
        "defect_type": "",
        "defect_location": "",
        "inspection_method": "",
        "scenario": "",
    }
    scope.update(filters or {})
    source, master = sha256("synthetic-source"), sha256("synthetic-master")
    first = event_count // 3
    last = event_count - first
    analysis = {
        "scope": scope,
        "metadata": {
            "source_sha256": source,
            "master_sha256": master,
            "stale": False,
            "dataset_version": "synthetic-dataset-v1",
            "policy_version": "synthetic-policy-v1",
            "scenario": scope["scenario"],
        },
        "definitions": {"event_count": "Logged events in the applied scope; not defective units."},
        "kpis": {
            "event_count": event_count,
            "critical_count": 5,
            "critical_share_pct": 5 / event_count * 100,
            "known_repair_cost": 125.5,
            "cost_coverage_pct": 100.0,
            "cost_unknown_count": 0,
            "reference_intensity_per_1000": 3.0,
        },
        "trend": {
            "first_month": "2024-01",
            "last_month": "2024-02",
            "first_count": first,
            "last_count": last,
            "change_pct": (last - first) / first * 100,
        },
        "monthly": [
            {
                "month": "2024-01",
                "label": "Jan",
                "event_count": first,
                "critical_count": 2,
                "known_repair_cost": 42.0,
            },
            {
                "month": "2024-02",
                "label": "Feb",
                "event_count": last,
                "critical_count": 3,
                "known_repair_cost": 83.5,
            },
        ],
        "location_severity": {
            "location_totals": [
                {
                    "location": "Surface",
                    "event_count": event_count,
                    "critical_count": 5,
                    "critical_share_pct": 5 / event_count * 100,
                }
            ]
        },
    }
    receipt = EvidenceLedger(tmp_path / "service-ledger").record(
        "canonical_analysis",
        "# Synthetic canonical service implementation",
        analysis,
        sources=[{"source_sha256": source, "master_sha256": master}],
    )
    context = {
        "context_id": sha256(canonical({"scope": scope, "snapshot": snapshot_id}))[:24],
        "snapshot_id": snapshot_id,
        "filters": scope,
        "source_sha256": source,
        "master_sha256": master,
        "code_sha256": receipt["code_sha256"],
        "kpi_version": "canonical-test",
        "fetched_at": "2026-10-16T00:00:00Z",
        "dataset_version": "synthetic-dataset-v1",
        "policy_version": "synthetic-policy-v1",
        "scenario": scope["scenario"],
    }
    return {
        "context": context,
        "analysis": analysis,
        "evidence": receipt,
        "options": {},
        "links": {},
    }


class FrozenAnalysisService:
    def __init__(self, payload, comparison=None):
        self.payload = deepcopy(payload)
        self.comparison = deepcopy(comparison)
        self.lookups, self.comparisons = [], []

    async def context_by_id(self, identity):
        self.lookups.append(identity)
        if identity != self.payload["context"]["context_id"]:
            raise LookupError("Frozen context is unavailable")
        return deepcopy(self.payload)

    async def get_context(self, filters=None, snapshot_id=None):
        self.comparisons.append({"filters": deepcopy(filters), "snapshot_id": snapshot_id})
        if self.comparison is None:
            raise LookupError("Comparison is unavailable in this frozen snapshot")
        return deepcopy(self.comparison)


def test_context_metadata_is_readable_without_weakening_numeric_audit():
    assert metadata_label("2024-06") == "June twenty twenty-four"
    assert metadata_label("Jan 2024") == "January twenty twenty-four"
    assert metadata_label("Line-3") == "Line three"
    assert metadata_label("1234") == "one thousand two hundred thirty-four"


def test_numeric_audit_rejects_changed_embedded_and_missing_numbers(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    receipt = ledger.record("python", "result={'n':12}", {"n": 12})
    claim = NumericClaim(
        id="n",
        execution_id=receipt["execution_id"],
        path="$.n",
        value=12,
        code_sha256=receipt["code_sha256"],
    )
    for text in (
        "Revenue USD999, size 12 [claim:n]",
        "Total999 and 12 [claim:n]",
        "13 [claim:n]",
        "12",
    ):
        assert not audit_report(AnalystReport(report=text, claims=[claim]), ledger)["verified"]
    assert audit_report(AnalystReport(report="12 [claim:n]", claims=[claim]), ledger)["verified"]


def test_compaction_opaque_replay():
    opaque = {"type": "compaction", "id": "cmp", "encrypted_content": "OPAQUE"}
    assert compact_items(
        [{"role": "user", "content": "old"}, opaque, {"role": "user", "content": "new"}]
    ) == [opaque, {"role": "user", "content": "new"}]


def test_exported_narrative_cannot_hide_an_unsupported_number(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    receipt = ledger.record("python", "result={'values':[12]}", {"values": [12]})
    claim = NumericClaim(
        id="values[0]",
        execution_id=receipt["execution_id"],
        path="$.values[0]",
        value=12,
        code_sha256=receipt["code_sha256"],
    )
    report = AnalystReport(
        report="Observed 12 [claim:values[0]]",
        claims=[claim],
        deliverable_paths=["artifacts/hidden_report.md"],
    )
    assert audit_report(report, ledger)["verified"]
    directory = tmp_path / "artifacts"
    directory.mkdir()
    path = directory / "hidden_report.md"
    path.write_text("Revenue USD999, observed 12 [claim:values[0]]", encoding="utf-8")
    ledger.register_artifact(path)
    audited = audit_exported_reports(report, ledger)
    assert not audited["verified"] and "999" in str(audited["errors"])


def test_steering_reaches_engineer_and_lead_once_but_not_qa(tmp_path):
    async def scenario():
        queued = ["Prioritize revenue"]

        async def steering():
            messages = queued[:]
            queued.clear()
            return messages

        async def emit(kind, data):
            pass

        runtime = AnalystRuntime(Settings(mode="mock"), None, emit, steering=steering)
        engineer = runtime.model_steering("Data Engineer")
        lead = runtime.model_steering("Lead Analyst")
        assert runtime.model_steering("QA Verifier") is None
        assert await engineer() == ["Prioritize revenue"]
        assert await lead() == ["Prioritize revenue"]
        assert await engineer() == await lead() == []

    asyncio.run(scenario())


def test_sdk_fixture_verifies_chat_and_retains_internal_tool_artifacts_without_exports(tmp_path):
    async def scenario():
        source = tmp_path / "sales.csv"
        source.write_text("group,value\na,2\na,4\nb,6\n")
        tools = AnalystTools(tmp_path / "run", [source])
        events = []

        async def emit(kind, data):
            events.append((kind, data))

        runtime = AnalystRuntime(Settings(mode="mock"), tools, emit)
        try:
            result = await runtime.run("Phân tích CSV và đề xuất bước tiếp theo")
            assert result["status"] == "completed", result["qa"]
            assert result["simulation"] and result["input_tokens"] == result["cost_usd"] == 0
            assert result["qa"]["verified"]
            records = tools.ledger.records()
            assert {r["tool"] for r in records} >= {
                "dataset_profiler",
                "local_python_repl",
                "local_sql_engine",
            }
            assert "run_case_study" not in {r["tool"] for r in records}
            python = next(r for r in records if r["tool"] == "local_python_repl")
            assert python["result"]["value"]["row_count"] == 3
            assert tools.ledger.verify_claims(result["claims"])["verified"]
            assert result["artifacts"] == result["delivery"]["artifacts"] == []
            assert "distribution.png" in {item["name"] for item in tools.ledger.artifacts()}
            for name in (
                "business_report.html",
                "verified_report.md",
                "verified_claims.json",
                "verified_evidence.zip",
                "verification_scope.json",
                "execution_ledger.jsonl",
            ):
                assert not (tools.run_dir / "artifacts" / name).exists()
            assert list((tools.run_dir / "internal/qa-inputs").glob("*.json"))
            started = {d["tool_id"] for kind, d in events if kind == "tool_started"}
            completed = {d["tool_id"] for kind, d in events if kind == "tool_completed"}
            assert started == completed and len(started) >= 4
        finally:
            await tools.close()

    asyncio.run(scenario())


def test_qa_retries_then_reports_unverified_without_success(tmp_path):
    async def scenario():
        source = tmp_path / "sample.csv"
        source.write_text("x\n1\n")
        tools = AnalystTools(tmp_path / "run", [source])
        events = []

        async def emit(kind, data):
            events.append(kind)

        runtime = AnalystRuntime(Settings(mode="mock", qa_max_retries=1), tools, emit)
        runtime.fixture_report = lambda: AnalystReport(report="Unsupported 999", claims=[])
        try:
            result = await runtime.run("Profile the supplied CSV")
            assert result["status"] == "unverified"
            assert not result["qa"]["verified"]
            assert events.count("qa_retry") == 2
            assert (
                len(
                    [
                        entry
                        for entry in tools.ledger.records()
                        if entry["tool"] == "local_python_repl"
                    ]
                )
                == 2
            )
        finally:
            await tools.close()

    asyncio.run(scenario())


def test_live_sdk_qa_retry_retains_tool_outputs_and_omits_app_display_note(tmp_path, monkeypatch):
    from agents import Model, ModelResponse, Usage
    from analyst.localization import VI_NUMBER_NOTE

    async def scenario():
        tools = AnalystTools(tmp_path / "run", [])

        async def emit(*_):
            pass

        payload = canonical_context(tmp_path)
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test", qa_max_retries=1),
            tools,
            emit,
            language="vi",
            analysis_context=payload,
            analysis_service=FrozenAnalysisService(payload),
        )
        qa_inputs, repair_inputs, recorded_inputs = [], [], []
        original_run = Runner.run

        async def inspect_qa_input(agent, input, *args, **kwargs):
            if agent.name == "QA Verifier":
                requests = [
                    entry
                    for entry in tools.ledger.records()
                    if entry["tool"] == "report_semantic_review"
                    and entry["result"]["phase"] == "requested"
                ]
                request = requests[-1]["result"]
                retained = tools.run_dir / request["runner_input_path"]
                assert isinstance(input, str)
                assert retained.read_bytes() == input.encode("utf-8")
                assert request["runner_input_sha256"] == sha256(input)
                assert request["runner_input_bytes"] == len(input.encode("utf-8"))
                assert request["attempt"] == len(recorded_inputs) + 1
                recorded_inputs.append((request, input))
            return await original_run(agent, input, *args, **kwargs)

        monkeypatch.setattr(Runner, "run", inspect_qa_input)

        class ScriptedModel(Model):
            def __init__(self, role):
                self.role, self.step, self.history, self.usage = role, 0, [], []

            async def get_response(
                self,
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                **kwargs,
            ):
                self.step += 1
                if self.role == "QA Verifier":
                    value = json.loads(input if isinstance(input, str) else input[-1]["content"])
                    qa_inputs.append(value)
                    output = [
                        _message(
                            json.dumps(
                                {
                                    "verified": value["audit"]["verified"],
                                    "errors": value["audit"]["errors"],
                                    "suggestions": ["A shorter title may read better."],
                                }
                            )
                        )
                    ]
                elif self.role == "Lead Analyst" and self.step == 1:
                    from dataclasses import fields

                    from analyst.case_study.analytics import Scope

                    output = [
                        ResponseFunctionToolCall(
                            id="fc_evidence",
                            call_id="call_evidence",
                            name="analysis_query",
                            arguments=json.dumps(
                                {
                                    "section": "kpis",
                                    "filters": {field.name: None for field in fields(Scope)},
                                }
                            ),
                            type="function_call",
                            status="completed",
                        )
                    ]
                else:
                    if self.step == 3:
                        repair_inputs.extend(dump(input))
                    identity = next(
                        claim.id
                        for claim in runtime.claim_catalog.values()
                        if claim.path == "$.analysis.kpis.event_count"
                    )
                    value = 13 if self.step == 2 else 12
                    output = [
                        _message(
                            json.dumps(
                                {"report": f"Đã ghi nhận {value} sự kiện [claim:{identity}]."}
                            )
                        )
                    ]
                self.history = (
                    [{"role": "user", "content": input}] if isinstance(input, str) else dump(input)
                ) + dump(output)
                return ModelResponse(
                    output=output,
                    usage=Usage(requests=0),
                    response_id=f"offline_{self.role}_{self.step}",
                )

            async def stream_response(self, *args, **kwargs):
                raise NotImplementedError
                yield

        runtime.model = lambda role: ScriptedModel(role)
        try:
            result = await runtime.run("Tóm tắt số sự kiện quan sát.")
            assert result["status"] == "completed", result["qa"]
            assert len(qa_inputs) == 2 and all(
                VI_NUMBER_NOTE not in item["report"] for item in qa_inputs
            )
            assert result["report"].count(VI_NUMBER_NOTE) == 1
            assert result["qa"]["suggestions"] == ["A shorter title may read better."]
            assert any(
                item.get("type") == "function_call_output"
                and "event_count" in item.get("output", "")
                for item in repair_inputs
            )
            assert any(
                "COMPLETE REPLACEMENT REPORT" in str(item.get("content", ""))
                and runtime.prompt in str(item.get("content", ""))
                for item in repair_inputs
            )
            assert any("13" in str(item.get("content", "")) for item in repair_inputs)
            assert (
                len(
                    [entry for entry in tools.ledger.records() if entry["tool"] == "analysis_query"]
                )
                == 1
            )
            reviews = [
                entry["result"]
                for entry in tools.ledger.records()
                if entry["tool"] == "report_semantic_review"
            ]
            assert [review["phase"] for review in reviews] == [
                "requested",
                "reviewed",
                "requested",
                "reviewed",
            ]
            assert len(recorded_inputs) == 2
            assert len({request["runner_input_path"] for request, _ in recorded_inputs}) == 2
            assert all("report_semantic_review" not in input for _, input in recorded_inputs)
            assert all(
                "internal/qa-inputs" not in artifact["path"] for artifact in result["artifacts"]
            )
            assert all(
                request["runner_input_path"] not in result["report"]
                for request, _ in recorded_inputs
            )
        finally:
            await tools.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("severity", "Critical"),
        ("defect_type", "Functional"),
        ("defect_location", "Component"),
        ("inspection_method", "Visual Inspection"),
    ],
)
def test_date_sensitivity_preserves_filter_when_scenario_removes_its_vocabulary(
    tmp_path, monkeypatch, field, value
):
    from analyst.case_study import config, ingestion
    from analyst.case_study.analytics import Scope, analysis_report
    from analyst.case_study.preparation import build_snapshot

    master = b"product_id,product_name,category,production_line,monthly_output\n1,Widget A,Category 1,Line-1,100\n"
    master_path = tmp_path / "master.csv"
    master_path.write_bytes(master)
    rows = [
        {
            "defect_id": 1,
            "product_id": 1,
            "defect_type": "Functional",
            "defect_date": "6/3/2024",
            "defect_location": "Component",
            "severity": "Critical",
            "inspection_method": "Visual Inspection",
            "repair_cost": 10,
        }
    ]
    provenance = {
        "source_sha256": sha256(canonical(rows)),
        "source_url": "https://synthetic.invalid/events",
        "fetched_at": "2026-10-16T00:00:00Z",
    }
    snapshot = build_snapshot(rows, provenance, master)
    analysis = analysis_report(snapshot, Scope(**{field: value}))
    receipt = EvidenceLedger(tmp_path / "service").record(
        "canonical_analysis", "# exact test implementation", analysis
    )
    context = {
        "context_id": "bound-test",
        "snapshot_id": "snapshot-test",
        "filters": analysis["scope"],
        "source_sha256": provenance["source_sha256"],
        "master_sha256": sha256(master),
        "code_sha256": receipt["code_sha256"],
        "kpi_version": "test",
        "fetched_at": provenance["fetched_at"],
    }
    payload = {"context": context, "analysis": analysis, "evidence": receipt}
    monkeypatch.setattr(config, "MASTER_PATH", master_path)
    monkeypatch.setattr(ingestion, "fetch_events", lambda: (deepcopy(rows), dict(provenance)))
    runtime = AnalystRuntime(Settings(mode="mock"), None, None, analysis_context=payload)
    result = asyncio.run(runtime.date_sensitivity())
    assert [scenario["kpis"]["event_count"] for scenario in result["scenarios"]] == [1, 1, 0]
    assert all(scenario["scope"] == context["filters"] for scenario in result["scenarios"])
    assert all(scenario["kpis"]["reference_output"] == 600 for scenario in result["scenarios"])
    monkeypatch.setattr(
        ingestion,
        "fetch_events",
        lambda: (deepcopy(rows), {**provenance, "source_sha256": "changed"}),
    )
    with pytest.raises(ValueError, match="changed since"):
        asyncio.run(runtime.date_sensitivity())


def test_nested_sdk_provider_stop_propagates_without_retry(tmp_path):
    async def scenario():
        tools = AnalystTools(tmp_path / "run", [])
        events = []

        async def emit(kind, data):
            events.append(kind)

        runtime = AnalystRuntime(Settings(mode="mock"), tools, emit)
        original_model = runtime.model

        def model(role):
            value = original_model(role)
            if role == "Data Engineer":

                async def stopped(*args, **kwargs):
                    raise ProviderStopped("Provider stopped the affected conversation")

                value.get_response = stopped
            return value

        runtime.model = model
        try:
            result = await runtime.run("Profile the supplied dataset")
            assert result["status"] == "provider_stopped", result
            assert "qa_retry" not in events
        finally:
            await tools.close()

    asyncio.run(scenario())


def test_context_sdk_run_reuses_exact_filtered_canonical_receipt(tmp_path):
    async def scenario():
        payload = canonical_context(tmp_path)
        original = deepcopy(payload)
        service = FrozenAnalysisService(payload)
        tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)
        events = []

        async def emit(kind, data):
            events.append((kind, data))

        runtime = AnalystRuntime(
            Settings(mode="mock", memory_enabled=False),
            tools,
            emit,
            analysis_context=payload,
            analysis_service=service,
            initial_items=[
                {"role": "user", "content": "Prior unfiltered analysis had 1071 events."}
            ],
        )
        # UI filter edits and caller mutation cannot retarget an in-flight run.
        payload["context"]["filters"]["severity"] = "Critical"
        payload["analysis"]["kpis"]["event_count"] = 999
        exposed = runtime.analysis_context
        exposed["context"]["filters"]["production_line"] = "Line-4"
        try:
            result = await runtime.run(
                "Explain this manufacturing case study in the applied Dashboard scope"
            )
            assert result["status"] == "completed", result["qa"]
            assert result["qa"]["verified"] and result["simulation"]
            assert result["analysis_context"] == original["context"]
            assert "1071" not in result["report"] and "999" not in result["report"]
            entries = tools.ledger.records()
            imported = next(entry for entry in entries if entry["tool"] == "dashboard_context")
            assert imported["execution_id"] == original["evidence"]["execution_id"]
            assert imported["code_sha256"] == original["evidence"]["code_sha256"]
            assert imported["result_sha256"] == original["evidence"]["result_sha256"]
            assert (
                imported["canonical_receipt"]["entry_sha256"]
                == original["evidence"]["entry_sha256"]
            )
            assert imported["execution_kind"] == "canonical_receipt_import"
            assert tools.ledger.verify_integrity() == []
            assert tools.ledger.verify_claims(result["claims"])["verified"]
            assert {claim["execution_id"] for claim in result["claims"]} == {
                original["evidence"]["execution_id"]
            }
            assert "run_case_study" not in {entry["tool"] for entry in entries}
            assert service.lookups == [original["context"]["context_id"]]
            assert service.comparisons == []
            qa_files = list((tools.run_dir / "internal/qa-inputs").glob("*.json"))
            assert len(qa_files) == 1
            reviewed = json.loads(qa_files[0].read_text(encoding="utf-8"))
            assert reviewed["analysis_context"] == original["context"]
            assert (
                result["canonical_receipt"]["execution_id"] == original["evidence"]["execution_id"]
            )
            assert result["artifacts"] == result["delivery"]["artifacts"] == []
        finally:
            await tools.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["result", "scope", "code", "receipt", "unavailable"])
def test_invalid_or_unavailable_context_never_falls_back_to_full_case(tmp_path, corruption):
    async def scenario():
        payload = canonical_context(tmp_path)
        service = None
        if corruption == "result":
            payload["analysis"]["kpis"]["event_count"] = 999
        elif corruption == "scope":
            payload["context"]["filters"] = {
                **payload["context"]["filters"],
                "severity": "Critical",
            }
        elif corruption == "code":
            payload["evidence"]["code"] += "\n# changed"
        elif corruption == "receipt":
            payload["evidence"]["numeric_values"][0]["value"] = 999
        else:
            service = FrozenAnalysisService(
                canonical_context(tmp_path, filters={"severity": "Major"})
            )
        tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)

        async def emit(kind, data):
            pass

        runtime = AnalystRuntime(
            Settings(mode="mock", memory_enabled=False, qa_max_retries=0),
            tools,
            emit,
            analysis_context=payload,
            analysis_service=service,
        )
        try:
            result = await runtime.run("Solve the full manufacturing case study")
            assert result["status"] == "unverified", result
            assert not result["qa"]["verified"] and result["unverified"]
            assert "No alternative snapshot" in result["report"]
            assert "run_case_study" not in {item["tool"] for item in tools.ledger.records()}
            assert (
                next(
                    item for item in tools.ledger.records() if item["tool"] == "dashboard_context"
                )["result"]["ok"]
                is False
            )
        finally:
            await tools.close()

    asyncio.run(scenario())


def test_explicit_scope_comparison_uses_same_snapshot_without_changing_binding(tmp_path):
    async def scenario():
        payload = canonical_context(tmp_path)
        alternative = canonical_context(tmp_path, filters={"severity": "Critical"}, event_count=20)
        service = FrozenAnalysisService(payload, comparison=alternative)
        tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)

        async def emit(kind, data):
            pass

        runtime = AnalystRuntime(
            Settings(mode="mock", memory_enabled=False),
            tools,
            emit,
            analysis_context=payload,
            analysis_service=service,
        )
        try:
            names = {tool.name for tool in runtime.sdk_tools()}
            assert {"dashboard_context", "dashboard_compare_scope"} <= names
            assert "run_case_study" not in names
            await runtime._call("dashboard_context", {})
            receipt = json.loads(
                await runtime._call(
                    "dashboard_compare_scope", {"filters": {"severity": "Critical"}}
                )
            )
            assert receipt["execution_id"] == alternative["evidence"]["execution_id"]
            assert receipt["analysis_context"]["snapshot_id"] == payload["context"]["snapshot_id"]
            assert runtime.analysis_context["context"] == payload["context"]
            assert service.comparisons == [
                {
                    "filters": alternative["context"]["filters"],
                    "snapshot_id": payload["context"]["snapshot_id"],
                }
            ]
            claim = NumericClaim(
                id="comparison_count",
                execution_id=receipt["execution_id"],
                path="$.kpis.event_count",
                value=20,
                code_sha256=receipt["code_sha256"],
            )
            assert audit_report(
                AnalystReport(
                    report="Comparison scope: 20 [claim:comparison_count]", claims=[claim]
                ),
                tools.ledger,
            )["verified"]
            assert runtime.context_entry["result"]["kpis"]["event_count"] == 12
        finally:
            await tools.close()

    asyncio.run(scenario())


def test_comparison_cannot_replace_frozen_snapshot(tmp_path):
    async def scenario():
        payload = canonical_context(tmp_path)
        replacement = canonical_context(
            tmp_path, filters={"severity": "Critical"}, snapshot_id="new-snapshot"
        )
        tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)

        async def emit(kind, data):
            pass

        runtime = AnalystRuntime(
            Settings(mode="mock", memory_enabled=False),
            tools,
            emit,
            analysis_context=payload,
            analysis_service=FrozenAnalysisService(payload, comparison=replacement),
        )
        try:
            receipt = json.loads(
                await runtime._call(
                    "dashboard_compare_scope", {"filters": {"severity": "Critical"}}
                )
            )
            assert receipt["result"]["ok"] is False
            assert "different frozen snapshot" in receipt["result"]["error"]
            assert runtime.comparison_entries == []
            assert runtime.analysis_context["context"] == payload["context"]
        finally:
            await tools.close()

    asyncio.run(scenario())


def test_bound_refresh_failure_status_is_carried_without_mutating_canonical_analysis(tmp_path):
    async def scenario():
        payload = canonical_context(tmp_path)
        payload["status"] = {
            "stale": True,
            "last_error": "HTTP 503 fixture failure",
            "last_attempt_at": "2026-10-16T01:00:00Z",
        }
        bound_status = deepcopy(payload["status"])
        service = FrozenAnalysisService(payload)
        # Later observer status is separate from the immutable canonical result.
        service.payload["status"] = {
            "stale": False,
            "last_error": None,
            "last_attempt_at": "2026-10-16T02:00:00Z",
        }
        tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)

        async def emit(kind, data):
            pass

        runtime = AnalystRuntime(
            Settings(mode="mock", memory_enabled=False),
            tools,
            emit,
            analysis_context=payload,
            analysis_service=service,
        )
        payload["status"]["stale"] = False
        try:
            result = await runtime.run("Explain the applied Dashboard context")
            assert result["status"] == "completed", result["qa"]
            assert "latest source refresh failed" in result["report"]
            assert "retained frozen snapshot" in result["report"]
            assert result["source_status"] == bound_status
            assert runtime.context_entry["source_status"] == bound_status
            assert runtime.context_entry["result"]["metadata"]["stale"] is False
            qa_files = list((tools.run_dir / "internal/qa-inputs").glob("*.json"))
            assert len(qa_files) == 1
            reviewed = json.loads(qa_files[0].read_text(encoding="utf-8"))
            assert reviewed["source_status"] == bound_status
            assert result["artifacts"] == result["delivery"]["artifacts"] == []
            assert all("sensitivity" not in item["title"].lower() for item in tools.todos)
            assert tools.todos[1]["title"] == "Read canonical scope and verify KPI evidence"
        finally:
            await tools.close()

    asyncio.run(scenario())
