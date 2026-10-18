"""Runtime chart selection binds original evidence through QA and delivery."""

import json
from types import SimpleNamespace

import pytest
from analyst.config import Settings
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.runtime import AnalystReport, AnalystRuntime
from analyst.tools import AnalystTools
from test_runtime import FrozenAnalysisService, canonical_context


def runtime_at(tmp_path, *, live=False, payload=None, events=None, **options):
    async def emit(kind, data):
        if events is not None:
            events.append((kind, data))

    run_dir = tmp_path / "run"
    ledger = EvidenceLedger(run_dir)
    return AnalystRuntime(
        Settings(mode="live" if live else "mock", api_key="offline-fixture", memory_enabled=False),
        SimpleNamespace(ledger=ledger, run_dir=run_dir, input_paths=[], schemas=[]),
        emit,
        analysis_context=payload,
        analysis_service=FrozenAnalysisService(payload) if payload is not None else None,
        **options,
    )


def source(runtime, tmp_path):
    context = canonical_context(tmp_path)["context"]
    return runtime.toolkit.ledger.record(
        "analysis_query",
        "# sealed synthetic source computation",
        {
            "ok": True,
            "context": context,
            "analysis": {
                "rows": [
                    {"category": "Structural", "event_count": 0},
                    {"category": "Functional", "event_count": -2.5},
                    {"category": "Structural", "event_count": None},
                    {"category": "Other", "event_count": 8},
                ]
            },
        },
        analysis_context=context,
        execution_kind="bounded_analysis",
    )


def arguments(entry, **changes):
    return {
        "receipt_id": entry["execution_id"],
        "path": "$.analysis.rows",
        "x": "category",
        "y": "event_count",
        "kind": "bar",
        "offset": 0,
        "limit": 100,
        **changes,
    }


async def issue(runtime, entry, **changes):
    await runtime._call("report_chart", arguments(entry, **changes))
    receipt = runtime.toolkit.ledger.records()[-1]
    assert receipt["tool"] == "report_chart"
    return receipt


def selected_report(receipt):
    return AnalystReport(
        report="The selected source chart is shown below.",
        claims=[],
        deliverable_paths=[receipt["result"]["artifact"]["path"]],
    )


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    def disallow_provider(*args, **kwargs):
        raise AssertionError("Chart runtime tests must not construct a provider client")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", disallow_provider)


def test_qa_receives_only_explicit_server_reference_as_inert_presentation_context(tmp_path):
    presentation = [
        {
            "kind": "line",
            "selection": {
                "path": "$.monthly",
                "x": "month",
                "y": "event_count",
                "offset": 0,
                "limit": 2,
            },
            "axis_labels": {"x": "Month", "y": "Logged defect events"},
            "unit_note": "Logged records, not defective units.",
            "language": "en",
            "analysis_contexts": [{"filters": {"severity": "Critical"}, "scenario": ""}],
        }
    ]
    runtime = runtime_at(tmp_path, referenced_presentation=presentation)
    presentation[0]["kind"] = "bar"
    before = runtime.toolkit.ledger.records()
    packet, errors = runtime.qa_evidence(
        AnalystReport(report="Explanation of the referenced axes.", claims=[])
    )
    assert not errors
    historical = packet["referenced_presentation"]
    assert historical["charts"][0]["kind"] == "line"
    assert "current-run receipts" in historical["evidence_status"]
    assert packet["numeric_support"] == packet["chart_support"] == []
    assert packet["delivery"]["artifacts"] == []
    assert runtime.available_claims() == {}
    assert runtime.toolkit.ledger.records() == before

    normal = runtime_at(
        tmp_path / "normal",
        initial_items=[
            {"role": "user", "content": canonical({"historical_presentation": presentation})}
        ],
    )
    ordinary_packet, errors = normal.qa_evidence(
        AnalystReport(report="An ordinary answer.", claims=[])
    )
    assert not errors
    assert "referenced_presentation" not in ordinary_packet


@pytest.mark.parametrize("live", [False, True])
def test_report_chart_tool_is_available_without_arbitrary_plotting_code(tmp_path, live):
    runtime = runtime_at(tmp_path, live=live)
    tool = next(tool for tool in runtime.sdk_tools() if tool.name == "report_chart")
    schema = tool.params_json_schema
    assert set(schema["properties"]) == {"receipt_id", "path", "x", "y", "kind", "offset", "limit"}
    assert schema["properties"]["y"] == {"type": "string"}
    assert schema["properties"]["kind"]["enum"] == ["bar", "line"]
    assert schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_live_chart_receipt_reuses_source_claims_and_call_id_without_duplicate_measurements(
    tmp_path,
):
    runtime = runtime_at(tmp_path, live=True)
    entry = source(runtime, tmp_path)
    original = runtime.model_receipt(entry)
    issued = await runtime._call("report_chart", arguments(entry), provider_call_id="chart-call")
    receipt = runtime.toolkit.ledger.records()[-1]
    view = json.loads(issued)
    assert view["result"]["path"] == receipt["result"]["artifact"]["path"]
    assert view["result"]["source_receipt_id"] == entry["execution_id"]
    original_ids = {item["path"]: item["id"] for item in original["numeric_evidence"]}
    assert view["numeric_evidence"]
    assert all(item["id"] == original_ids[item["path"]] for item in view["numeric_evidence"])
    assert all(
        claim.execution_id == entry["execution_id"] for claim in runtime.available_claims().values()
    )
    assert runtime.model_receipt(receipt)["numeric_evidence"] == []
    before = len(runtime.toolkit.ledger.records())
    assert (
        await runtime._call("report_chart", arguments(entry), provider_call_id="chart-call")
        == issued
    )
    assert len(runtime.toolkit.ledger.records()) == before
    assert runtime.tool_calls == 1
    assert "def create_chart_view" in receipt["code"]
    assert "def table_selection" in receipt["code"]


@pytest.mark.asyncio
async def test_selected_page_preserves_original_paths_and_null_gaps(tmp_path):
    runtime = runtime_at(tmp_path, live=True)
    entry = source(runtime, tmp_path)
    receipt = await issue(runtime, entry, offset=1, limit=2, kind="line")
    chart = receipt["result"]["chart"]
    assert chart["rows_total"] == 4 and chart["rows_returned"] == 2
    assert [point["label"] for point in chart["points"]] == ["Functional", "Structural"]
    assert [point["value"] for point in chart["points"]] == [-2.5, None]
    assert [point["value_path"] for point in chart["points"]] == [
        "$.analysis.rows[1].event_count",
        "$.analysis.rows[2].event_count",
    ]
    assert len(receipt["result"]["source_claims"]) == 1
    assert receipt["result"]["source_claims"][0]["path"] == "$.analysis.rows[1].event_count"


@pytest.mark.asyncio
async def test_chart_only_answer_keeps_original_canonical_scope_and_qa_cells_without_ok_flag(
    tmp_path,
):
    payload = canonical_context(tmp_path)
    runtime = runtime_at(tmp_path, payload=payload)
    await runtime._call("dashboard_context", {})
    entry = runtime.toolkit.ledger.records()[0]
    assert "ok" not in entry["result"]
    receipt = await issue(runtime, entry, path="$.monthly", x="month", kind="line")
    report = selected_report(receipt)
    delivery, errors = runtime.begin_delivery_proposal(report)
    assert errors == []
    assert delivery["source_execution_ids"] == [entry["execution_id"]]
    assert [item["execution_id"] for item in delivery["source_manifest"]["sources"]] == [
        entry["execution_id"]
    ]
    assert [item["context_id"] for item in delivery["source_manifest"]["contexts"]] == [
        payload["context"]["context_id"]
    ]
    evidence, errors = runtime.qa_evidence(report, delivery=delivery)
    assert errors == []
    assert evidence["numeric_support"] == []
    assert len(evidence["chart_support"]) == 1
    support = evidence["chart_support"][0]
    assert support["chart"] == receipt["result"]["chart"]
    assert support["chart"]["analysis_contexts"] == [payload["context"]]
    assert support["source_claims"] == receipt["result"]["source_claims"]
    assert all(item["tool"] != "report_chart" for item in evidence["qualitative_support"])
    assert report.claims == []


@pytest.mark.asyncio
@pytest.mark.parametrize("disguise_kind", [False, True])
async def test_failed_chart_cannot_deliver_even_if_renderer_registered_an_image(
    tmp_path, monkeypatch, disguise_kind
):
    runtime = runtime_at(tmp_path)
    entry = source(runtime, tmp_path)
    from analyst import runtime as runtime_module

    create = runtime_module.create_chart_view

    def interrupted_after_render(*args, **kwargs):
        create(*args, **kwargs)
        raise ValueError("Synthetic failure after rendering")

    monkeypatch.setattr(runtime_module, "create_chart_view", interrupted_after_render)
    await runtime._call("report_chart", arguments(entry))
    failed = runtime.toolkit.ledger.records()[-1]
    assert failed["result"]["ok"] is False
    artifacts = runtime.toolkit.ledger.artifacts()
    assert len(artifacts) == 1
    if disguise_kind:
        artifacts[0]["kind"] = "image"
        runtime.toolkit.ledger.manifest_path.write_text(canonical(artifacts), encoding="utf-8")
    report = AnalystReport(
        report="Chart unavailable.", claims=[], deliverable_paths=[artifacts[0]["path"]]
    )
    delivery, errors = runtime.begin_delivery_proposal(report)
    assert errors
    assert delivery["artifacts"] == delivery["source_charts"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "bytes",
        "registry_hash",
        "registry_kind",
        "frozen_binding",
        "chart_binding",
        "source_value",
        "source_code",
        "renderer_code",
        "claim_identity",
    ],
)
async def test_chart_delivery_rejects_file_registry_binding_and_source_tampering(
    tmp_path, monkeypatch, mutation
):
    runtime = runtime_at(tmp_path)
    entry = source(runtime, tmp_path)
    receipt = await issue(runtime, entry)
    report = selected_report(receipt)
    first, errors = runtime.begin_delivery_proposal(report)
    assert errors == []
    artifact = first["artifacts"][0]
    path = artifact["path"]
    ledger = runtime.toolkit.ledger
    if mutation in {"bytes", "registry_hash"}:
        content = b"Synthetic replaced chart bytes"
        (ledger.run_dir / path).write_bytes(content)
        if mutation == "registry_hash":
            items = ledger.artifacts()
            items[0].update(sha256=sha256(content), size_bytes=len(content))
            ledger.manifest_path.write_text(canonical(items), encoding="utf-8")
    elif mutation == "registry_kind":
        items = ledger.artifacts()
        items[0]["kind"] = "image"
        ledger.manifest_path.write_text(canonical(items), encoding="utf-8")
    elif mutation == "frozen_binding":
        runtime.delivery_chart_bindings[path]["chart"]["points"][0]["value"] = 999
    elif mutation in {"chart_binding", "source_value", "source_code"}:
        records = ledger.records()
        if mutation == "chart_binding":
            records[-1]["result"]["chart"]["points"][0]["value"] = 999
        elif mutation == "source_value":
            records[0]["result"]["analysis"]["rows"][0]["event_count"] = 999
        else:
            records[0]["code"] += "\n# changed"
        ledger.path.write_text(
            "\n".join(canonical(item) for item in records) + "\n", encoding="utf-8"
        )
    elif mutation == "renderer_code":
        original = runtime.report_chart_code()
        monkeypatch.setattr(runtime, "report_chart_code", lambda: original + "\n# changed renderer")
    elif mutation == "claim_identity":
        identity = receipt["result"]["source_claims"][0]["id"]
        runtime.claim_catalog[identity] = runtime.claim_catalog[identity].model_copy(
            update={"value": 999}
        )
    delivery, errors = runtime.delivery_packet(report)
    assert errors
    assert delivery["artifacts"] == delivery["source_charts"] == []


@pytest.mark.asyncio
async def test_qa_rechecks_chart_registry_and_bytes_even_when_given_an_earlier_packet(tmp_path):
    runtime = runtime_at(tmp_path)
    entry = source(runtime, tmp_path)
    receipt = await issue(runtime, entry)
    report = selected_report(receipt)
    delivery, errors = runtime.begin_delivery_proposal(report)
    assert errors == []
    (runtime.toolkit.run_dir / delivery["artifacts"][0]["path"]).write_bytes(b"changed")
    evidence, errors = runtime.qa_evidence(report, delivery=delivery)
    assert errors
    assert evidence["chart_support"] == []


@pytest.mark.asyncio
async def test_run_started_keeps_exact_public_question_separate_from_runtime_updates(tmp_path):
    settings = Settings(mode="mock", memory_enabled=False)
    toolkit = AnalystTools(tmp_path, [], memory_enabled=False)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    runtime = AnalystRuntime(settings, toolkit, emit)
    question = "What can you analyze?\nKeep the filters unchanged."
    try:
        await runtime.run(question)
        metadata = next(data for kind, data in events if kind == "run_started")
        assert metadata["question"] == question
    finally:
        await toolkit.close()
