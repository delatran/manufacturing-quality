"""Independent measurement unit counterexamples over disposable, explicitly synthetic sources.

No provider client may be constructed. The SDK guardrail case uses an offline
scripted Model and verifies its retained QA input, not live model quality.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import pytest_asyncio
from agents import Model, ModelResponse, Usage
from analyst.app import create_app
from analyst.case_study.analytics import METRIC_METADATA
from analyst.case_study.preparation import build_snapshot
from analyst.dashboard import AnalysisService
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.investigation import export_saved_investigation
from analyst.product_tools import investigation_content_hash
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    QAReview,
    ReportDraft,
    _message,
    audit_report,
)
from analyst.store import SessionStore
from analyst.tools import AnalystTools
from fastapi.testclient import TestClient
from test_agent_review_independent import ROOT, event, filters, no_events, service, settings
from test_evidence_integrity import binding, claim_for, payload


@pytest_asyncio.fixture
async def actual6(tmp_path, monkeypatch, request):
    def deny_provider(*_args, **_kwargs):
        raise AssertionError("Independent v6 tests cannot construct a provider client")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", deny_provider)
    rows = [event(index, product_id=6) for index in range(1, 6)]
    rows += [
        event(6, product_id=63),
        event(7, product_id=63, defect_date="2024-06-15", repair_cost=20),
        event(8, product_id=63, defect_date="2024-06-16", repair_cost=20),
        event(9, product_id=63, defect_date="2024-06-17", repair_cost=20),
        event(10, product_id=81, defect_date="2024-06-15", repair_cost=None),
    ]
    master = (
        b"product_id,product_name,category,production_line,monthly_output\n"
        b"6,Decreasing product,Category 1,Line-3,100\n"
        b"63,Increasing product,Category 2,Line-3,100\n"
        b"81,Unknown cost,Category 2,Line-3,100\n"
        b"83,Zero event,Category 3,Line-3,100\n"
    )

    def loader():
        return build_snapshot(
            rows,
            {
                "source_url": "https://synthetic.invalid/independent-v6",
                "source_sha256": sha256(canonical(rows)),
                "fetched_at": "2026-10-16T00:00:00Z",
            },
            master,
        )

    analysis = AnalysisService(ROOT, tmp_path / "analysis", snapshot_loader=loader)
    bound = await analysis.get_context()
    store = SessionStore(tmp_path / "store")
    sid = store.create_session()["id"]
    tools = AnalystTools(tmp_path / "run", [], session_id=sid, memory_enabled=False)
    language = (
        request.node.callspec.params.get("language", "en")
        if hasattr(request.node, "callspec")
        else "en"
    )
    runtime = AnalystRuntime(
        settings(tmp_path, mode="live", api_key="offline-invalid-key"),
        tools,
        no_events,
        analysis_context=bound,
        analysis_service=analysis,
        state_store=store,
        language=language,
    )
    comparison = json.loads(
        await runtime._call(
            "analysis_compare",
            {
                "baseline_filters": filters(start_month="2024-01", end_month="2024-01"),
                "comparison_filters": filters(start_month="2024-06", end_month="2024-06"),
                "dimension": "product_id",
                "plan": None,
            },
        )
    )
    assert comparison["status"] == "completed"
    evidence = {item["path"]: item for item in comparison["numeric_evidence"]}
    try:
        yield SimpleNamespace(
            runtime=runtime,
            tools=tools,
            store=store,
            sid=sid,
            analysis=analysis,
            bound=bound,
            comparison=comparison,
            evidence=evidence,
        )
    finally:
        await tools.close()
        store.close()


def marker(subject, path):
    return "[value:" + subject.evidence[path]["id"] + "]"


def save_arguments(text):
    return {
        "question": "Describe this comparison without causal attribution.",
        "summary": "Provisional source-backed work awaiting semantic review.",
        "findings": [text],
        "hypotheses": [
            {"id": None, "text": "Actual production exposure may differ.", "status": "open"}
        ],
        "next_steps": ["Collect actual production exposure."],
        "priorities": [],
        "filters": filters(),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language,variant,valid",
    [
        ("en", "proper", True),
        ("vi", "proper", True),
        ("en", "observed_count", False),
        ("vi", "observed_cost", False),
        ("en", "arbitrary_scale", False),
        ("en", "wrong_metric", False),
        ("en", "nearest_wrong_metric", False),
        ("en", "nearby_count", False),
        ("en", "other_clause", False),
        ("en", "no_reference", False),
    ],
)
async def test_per_thousand_scale_is_bound_to_its_cited_reference_intensity(
    actual6, language, variant, valid
):
    intensity = marker(actual6, "$.decomposition.reference_intensity_change")
    count = marker(actual6, "$.baseline.analysis.kpis.event_count")
    proper = (
        "Chỉ số sự kiện trên sản lượng tham chiếu thay đổi "
        + intensity
        + " sự kiện trên 1.000 đơn vị giữa hai tháng, nhưng mẫu số là sản lượng tham chiếu tĩnh chứ không phải sản lượng thực tế đã xác minh."
        if language == "vi"
        else "Reference intensity changed "
        + intensity
        + " events per 1,000 units; the output is a static reference."
    )
    text = {
        "proper": proper,
        "observed_count": "Observed 1000 events, with reference intensity " + intensity + ".",
        "observed_cost": "Chi phí quan sát là 1.000 local currency; cường độ tham chiếu "
        + intensity
        + ".",
        "arbitrary_scale": "Reference intensity "
        + intensity
        + " events per 10,000 reference units.",
        "wrong_metric": "Reference event count " + count + " events per 1,000 units.",
        "nearest_wrong_metric": "Reference intensity "
        + intensity
        + " and event count "
        + count
        + " events per 1,000 units.",
        "nearby_count": proper + " A separate 1000 events were observed.",
        "other_clause": "Reference intensity "
        + intensity
        + ". Another metric uses events per 1,000 reference units.",
        "no_reference": "Intensity " + intensity + " events per 1,000 units.",
    }[variant]
    report = actual6.runtime.resolve_report(ReportDraft(report=text))
    audit = audit_report(report, actual6.tools.ledger)
    assert audit["verified"] is valid, audit
    assert ("1.000" if language == "vi" else "1,000") in report.report or not valid
    assert actual6.tools.ledger.verify_integrity() == []


@pytest.mark.parametrize("change", ["scale", "unit", "untrusted_tool", "tampered_seal"])
def test_declared_unit_metadata_cannot_be_replaced_or_forged(tmp_path, change):
    ledger = EvidenceLedger(tmp_path)
    source = payload(binding("synthetic-january"), 2, 10.0, 80.0)
    source["analysis"]["metric_metadata"] = deepcopy(METRIC_METADATA)
    source["analysis"]["kpis"]["reference_intensity_per_1000"] = 1.5
    if change == "scale":
        source["analysis"]["metric_metadata"]["reference_intensity_per_1000"]["scale"] = 100
    if change == "unit":
        source["analysis"]["metric_metadata"]["reference_intensity_per_1000"]["unit"] = (
            "actual production units"
        )
    entry = ledger.record(
        "local_python_repl" if change == "untrusted_tool" else "analysis_query",
        "# Explicit synthetic unit-declaration counterexample",
        {"ok": True, **source},
        execution_kind="bounded_analysis",
        analysis_context=binding("synthetic-base"),
    )
    claim = claim_for(entry, "$.analysis.kpis.reference_intensity_per_1000", "intensity")
    if change == "tampered_seal":
        edited = deepcopy(entry)
        edited["result"]["analysis"]["metric_metadata"]["reference_intensity_per_1000"][
            "definition"
        ] = "Forged declaration"
        ledger.path.write_text(canonical(edited) + "\n", encoding="utf-8")
    checked = audit_report(
        AnalystReport(
            report="Reference intensity is 1.5 [claim:intensity] events per 1,000 reference units.",
            claims=[claim],
        ),
        ledger,
    )
    assert not checked["verified"], checked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metric", ["reference_intensity_per_1000", "cost_coverage_pct", "critical_share_pct"]
)
@pytest.mark.parametrize("unit", ["logged events", "local currency"])
async def test_exposed_metadata_scale_cannot_supply_an_observed_measurement(actual6, metric, unit):
    response = json.loads(
        await actual6.runtime._call(
            "analysis_query", {"section": "kpis", "filters": filters(), "plan": None}
        )
    )
    scale = next(
        item
        for item in response["numeric_evidence"]
        if item["path"] == "$.metric_metadata." + metric + ".scale"
    )
    assert scale["value"] == (1000 if metric == "reference_intensity_per_1000" else 100)
    assert response["result"]["analysis"]["kpis"]["event_count"] == 10
    report = actual6.runtime.resolve_report(
        ReportDraft(report="Observed [value:" + scale["id"] + "] " + unit + ".")
    )
    checked = audit_report(report, actual6.tools.ledger)
    assert not checked["verified"], checked


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
@pytest.mark.parametrize(
    "metric", ["reference_intensity_per_1000", "cost_coverage_pct", "critical_share_pct"]
)
async def test_explicit_source_unit_definition_remains_citable(actual6, language, metric):
    response = json.loads(
        await actual6.runtime._call(
            "analysis_query", {"section": "kpis", "filters": filters(), "plan": None}
        )
    )
    scale = next(
        item
        for item in response["numeric_evidence"]
        if item["path"] == "$.metric_metadata." + metric + ".scale"
    )
    prefix = "Unit scale: " if language == "en" else "Hệ số đơn vị: "
    report = actual6.runtime.resolve_report(
        ReportDraft(report=prefix + "[value:" + scale["id"] + "].")
    )
    checked = audit_report(report, actual6.tools.ledger)
    assert checked["verified"], checked
    assert report.claims[0].path == scale["path"]


@pytest.mark.parametrize("observed_unit", ["logged events", "local currency"])
def test_declared_count_scale_one_is_not_an_observed_one(tmp_path, observed_unit):
    ledger = EvidenceLedger(tmp_path)
    source = payload(binding("synthetic-count-scale"), 1, 10.0, 80.0)
    source["analysis"]["metric_metadata"] = deepcopy(METRIC_METADATA)
    source["analysis"]["metric_metadata"]["event_count"] = {
        "scale": 1,
        "unit": "logged defect events",
    }
    entry = ledger.record(
        "analysis_query",
        "# Explicit synthetic count-scale declaration; not real case-study observations",
        {"ok": True, **source},
        execution_kind="bounded_analysis",
        analysis_context=binding("synthetic-count-scale"),
    )
    scale = claim_for(entry, "$.analysis.metric_metadata.event_count.scale", "scale")
    count = claim_for(entry, "$.analysis.kpis.event_count", "count")
    wrong = audit_report(
        AnalystReport(report="Observed 1 [claim:scale] " + observed_unit + ".", claims=[scale]),
        ledger,
    )
    assert not wrong["verified"], wrong
    definition = audit_report(
        AnalystReport(report="Unit scale: 1 [claim:scale].", claims=[scale]), ledger
    )
    assert definition["verified"], definition
    measurement = audit_report(
        AnalystReport(report="Observed 1 [claim:count] logged event.", claims=[count]), ledger
    )
    assert measurement["verified"], measurement


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phrase,valid",
    [
        ("Product ID 6 decreased by CHANGE events.", False),
        ("Product ID 6 fell CHANGE events.", False),
        ("Product ID 6 giảm CHANGE sự kiện.", False),
        ("Product ID 6 changed by CHANGE events.", True),
        ("Product ID 6 thay đổi CHANGE sự kiện.", True),
        ("Product ID 6 chênh lệch CHANGE sự kiện.", True),
        ("Product ID 6 fell from BEFORE events to AFTER events.", True),
    ],
)
async def test_signed_negative_changes_are_clear_before_persistence(actual6, phrase, valid):
    rows = actual6.comparison["result"]["decomposition"]["rows"]
    index = next(index for index, row in enumerate(rows) if row["name"] == "6")
    path = f"$.decomposition.rows[{index}]"
    assert actual6.evidence[path + ".event_count_change"]["value"] == -5
    text = phrase.replace("CHANGE", marker(actual6, path + ".event_count_change"))
    text = text.replace("BEFORE", marker(actual6, path + ".baseline.event_count"))
    text = text.replace("AFTER", marker(actual6, path + ".comparison.event_count"))
    before = actual6.store.investigation(actual6.sid)
    response = json.loads(await actual6.runtime._call("investigation_update", save_arguments(text)))
    assert (response["status"] == "completed") is valid, response
    after = actual6.store.investigation(actual6.sid)
    if valid:
        assert after["version"] == before["version"] + 1
        assert after["content_status"] == "unverified"
        assert after["findings"][0]["status"] == "unverified"
    else:
        assert after == before
        assert response["result"]["error_fields"] == ["findings[0]"]
        assert not actual6.tools.ledger.artifacts()


@pytest.mark.asyncio
async def test_positive_product_increase_remains_executable(actual6):
    rows = actual6.comparison["result"]["decomposition"]["rows"]
    index = next(index for index, row in enumerate(rows) if row["name"] == "63")
    path = f"$.decomposition.rows[{index}].event_count_change"
    assert actual6.evidence[path]["value"] == 2
    response = json.loads(
        await actual6.runtime._call(
            "investigation_update",
            save_arguments("Product ID 63 increased by " + marker(actual6, path) + " events."),
        )
    )
    assert response["status"] == "completed", response
    assert actual6.store.investigation(actual6.sid)["findings"][0]["status"] == "unverified"


@pytest.mark.asyncio
async def test_actual_failed_reference_unit_phrase_saves_with_source_derived_notes(actual6):
    text = (
        "Chỉ số sự kiện trên sản lượng tham chiếu tăng "
        + marker(actual6, "$.decomposition.reference_intensity_change")
        + " sự kiện trên 1.000 đơn vị giữa hai tháng, nhưng mẫu số là sản lượng tham chiếu tĩnh chứ không phải sản lượng thực tế đã xác minh. Chi phí sửa chữa hợp lệ đã ghi nhận thay đổi "
        + marker(actual6, "$.decomposition.known_repair_cost_change")
        + "; độ phủ chi phí là "
        + marker(actual6, "$.baseline.analysis.kpis.cost_coverage_pct")
        + " trong tháng 1 và "
        + marker(actual6, "$.comparison.analysis.kpis.cost_coverage_pct")
        + " trong tháng 6, nên tổng chi phí chỉ bao gồm các khoản hợp lệ đã biết."
    )
    # The original live wording said increase. This fixture has a negative
    # intensity difference, so neutral wording preserves the synthetic meaning.
    text = text.replace("tham chiếu tăng ", "tham chiếu thay đổi ")
    response = json.loads(await actual6.runtime._call("investigation_update", save_arguments(text)))
    assert response["status"] == "completed", response
    state = actual6.store.investigation(actual6.sid)
    pending = actual6.runtime.product_tools.pending_state()
    notes = state["unit_notes"]
    assert set(notes) == {"en", "vi"} and all(notes.values())
    assert "source does not specify the currency" in notes["en"] and "(%)" in notes["en"]
    assert "local currency" not in notes["en"]
    assert "per thousand reference output units" in notes["en"]
    assert "not verified actual production" in notes["en"]
    assert "tham chiếu" in notes["vi"] and "nguồn không xác định loại tiền" in notes["vi"]
    assert "nội tệ" not in notes["vi"]
    assert pending["state"] == state
    assert pending["state_sha256"] == sha256(canonical(state))
    registered = response["result"]["artifact"]
    assert (
        json.loads(
            actual6.tools.ledger.resolve_artifact(registered["path"]).read_text(encoding="utf-8")
        )
        == state
    )
    exported, _ = export_saved_investigation(actual6.store, actual6.sid, state["version"], "vi")
    downloaded = json.loads(exported.read_text(encoding="utf-8"))
    assert downloaded["investigation"]["unit_notes"] == notes
    assert downloaded["export"]["state_sha256"] == sha256(canonical(state))
    assert state["content_status"] == "unverified"


@pytest.mark.parametrize("empty", [None, {}, {"en": "", "vi": ""}])
def test_legacy_read_and_registered_export_keep_exact_old_bytes(tmp_path, empty):
    store = SessionStore(tmp_path / "state")
    sid = store.create_session()["id"]
    try:
        saved = store.save_investigation(
            sid,
            0,
            {
                "summary": "Existing owner work.",
                "instructions_version": "investigation-v5",
                "tools_version": "bounded-analysis-v5",
            },
        )
        payload_value = {
            key: value
            for key, value in saved.items()
            if key not in {"session_id", "version", "updated_at", "unit_notes"}
        }
        if empty is not None:
            payload_value["unit_notes"] = empty
        encoded = json.dumps(payload_value, ensure_ascii=False)
        with store.db:
            store.db.execute(
                "UPDATE investigations SET payload=? WHERE session_id=?", (encoded, sid)
            )
        legacy = {
            "session_id": sid,
            "version": saved["version"],
            "updated_at": saved["updated_at"],
            **payload_value,
        }
        old_view = SimpleNamespace(root=store.root, investigation=lambda _: legacy)
        target, artifact = export_saved_investigation(old_view, sid, 1, "en")
        original = target.read_bytes()
        loaded = store.investigation(sid)
        assert loaded == legacy
        assert loaded["instructions_version"] == "investigation-v5"
        assert loaded["tools_version"] == "bounded-analysis-v5"
        assert (
            store.db.execute(
                "SELECT payload FROM investigations WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == encoded
        )
        again, readback = export_saved_investigation(store, sid, 1, "en")
        assert again.read_bytes() == original
        assert readback == artifact
        absent = {key: value for key, value in legacy.items() if key != "unit_notes"}
        assert investigation_content_hash(loaded) == investigation_content_hash(absent)
        nonempty = {**absent, "unit_notes": {"en": "Source count unit.", "vi": "Đơn vị số đếm."}}
        assert investigation_content_hash(nonempty) != investigation_content_hash(absent)
    finally:
        store.close()


def test_owner_http_edits_cannot_forge_notes_and_do_not_promote_content(tmp_path):
    app = create_app(settings(tmp_path), analysis_service=service(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions").json()["id"]
        store = app.state.workspace.store
        notes = {"en": "Counts describe logged events.", "vi": "Số đếm là sự kiện ghi nhận."}
        saved = store.save_investigation(
            sid,
            0,
            {
                "summary": "Existing reviewed work.",
                "unit_notes": notes,
                "content_status": "reviewed",
            },
        )
        rejected = client.patch(
            f"/api/sessions/{sid}/investigation",
            json={"expected_version": saved["version"], "unit_notes": {"en": "Forged unit"}},
        )
        assert rejected.status_code == 422
        assert store.investigation(sid) == saved
        edited = client.patch(
            f"/api/sessions/{sid}/investigation",
            json={
                "expected_version": saved["version"],
                "summary": "Owner changed this interpretation.",
            },
        )
        assert edited.status_code == 200, edited.text
        current = edited.json()
        assert current["unit_notes"] == notes
        assert current["content_status"] == "unverified" and current["semantic_review"] is None
        assert current["version"] == saved["version"] + 1


def sealed_review(subject):
    pending = subject.runtime.product_tools.pending_state()
    path = subject.tools.run_dir / "internal" / "independent-v6-qa.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        canonical(
            {
                "investigation_draft": {
                    key: pending[key] for key in ("version", "state_sha256", "state")
                }
            }
        ),
        encoding="utf-8",
    )
    return path, subject.tools.ledger.record(
        "report_semantic_review",
        "# Synthetic successful exact-state review, no model call",
        {
            "phase": "reviewed",
            "qa_verdict": {"verified": True},
            "runner_input_path": "internal/independent-v6-qa.json",
            "runner_input_sha256": sha256(path.read_bytes()),
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "owner_note_change", "retained_input_tamper"])
async def test_nonempty_notes_are_inside_semantic_state_and_compare_and_swap(actual6, change):
    text = (
        "Reference intensity changed "
        + marker(actual6, "$.decomposition.reference_intensity_change")
        + "."
    )
    result = json.loads(await actual6.runtime._call("investigation_update", save_arguments(text)))
    assert result["status"] == "completed", result
    before = actual6.store.investigation(actual6.sid)
    path, receipt = sealed_review(actual6)
    if change == "owner_note_change":
        owner = actual6.store.save_investigation(
            actual6.sid,
            before["version"],
            {"unit_notes": {"en": "Changed source units.", "vi": "Đơn vị đã đổi."}},
        )
        assert actual6.runtime.product_tools.pending_state() is None
        assert await actual6.runtime.product_tools.review_investigation(receipt) is None
        assert actual6.store.investigation(actual6.sid) == owner
    elif change == "retained_input_tamper":
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["investigation_draft"]["state"]["unit_notes"]["en"] = "Forged source units."
        path.write_text(canonical(tampered), encoding="utf-8")
        with pytest.raises(ValueError, match="retained seal"):
            await actual6.runtime.product_tools.review_investigation(receipt)
        assert actual6.store.investigation(actual6.sid) == before
    else:
        reviewed = await actual6.runtime.product_tools.review_investigation(receipt)
        state = actual6.store.investigation(actual6.sid)
        assert state["unit_notes"] == before["unit_notes"]
        assert state["semantic_review"]["state_sha256"] == sha256(canonical(before))
        assert state["content_status"] == "reviewed"
        path = actual6.tools.ledger.resolve_artifact(reviewed["reviewed_artifact"]["path"])
        assert json.loads(path.read_text(encoding="utf-8"))["unit_notes"] == state["unit_notes"]


class ScriptedFinal(Model):
    def __init__(self, runtime, role, report, observed):
        self.runtime, self.role, self.report, self.observed = runtime, role, report, observed
        self.usage = []

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
        if self.role == "QA Verifier":
            if isinstance(input, str):
                raw = input
            else:
                assert len(input) == 1 and input[0]["role"] == "user"
                raw = input[0]["content"]
            assert isinstance(raw, str)
            self.observed.append(raw)
            output = QAReview(verified=True, errors=[])
        else:
            output = ReportDraft(report=self.report)
        return ModelResponse(
            output=[_message(output.model_dump_json())],
            usage=Usage(requests=0),
            response_id="offline-v6-" + self.role.replace(" ", "_"),
        )

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
async def test_actual_sdk_qa_projection_keeps_exact_state_without_duplicate_populations(
    actual6, language
):
    text = (
        ("Cường độ tham chiếu thay đổi " if language == "vi" else "Reference intensity changed ")
        + marker(actual6, "$.decomposition.reference_intensity_change")
        + "."
    )
    saved = json.loads(await actual6.runtime._call("investigation_update", save_arguments(text)))
    assert saved["status"] == "completed", saved
    before = actual6.store.investigation(actual6.sid)
    observed = []

    def model(role):
        value = ScriptedFinal(actual6.runtime, role, text, observed)
        actual6.runtime.models.append(value)
        return value

    actual6.runtime.model = model
    result = await actual6.runtime.run(
        "Review the selected evidence and keep the investigation local."
    )
    assert result["status"] == "completed", result.get("qa")
    assert len(observed) == 1
    raw = observed[0]
    qa = json.loads(raw)
    assert raw == canonical(qa)
    assert ("Cường độ" if language == "vi" else "Reference intensity") in raw
    assert qa["investigation_draft"]["state"] == before
    assert qa["investigation_draft"]["state_sha256"] == sha256(canonical(before))
    assert qa["investigation_draft"]["state"]["unit_notes"] == before["unit_notes"]
    arrays = [
        group["path"]
        for support in qa["qualitative_support"]
        if support["tool"] == "investigation_update"
        for group in support["comparative_aggregates"]
    ]
    assert not any(path.startswith("$.investigation.") for path in arrays)
    assert any(
        support["tool"] == "analysis_compare" and support["comparative_aggregates"]
        for support in qa["qualitative_support"]
    )
    assert any(
        support["tool"] == "investigation_update" and support["aggregate_availability"]
        for support in qa["qualitative_support"]
    )
    # The saved investigation is reviewed directly, without a model-selected download.
    assert qa["verified_artifacts"] == []
    requests = [
        entry
        for entry in actual6.tools.ledger.records()
        if entry["tool"] == "report_semantic_review" and entry["result"].get("phase") == "requested"
    ]
    retained = actual6.tools.run_dir / requests[0]["result"]["runner_input_path"]
    assert retained.read_text(encoding="utf-8") == raw
    assert sha256(retained.read_bytes()) == requests[0]["result"]["runner_input_sha256"]
    reviewed_state = actual6.store.investigation(actual6.sid)
    assert reviewed_state["semantic_review"]["state_sha256"] == sha256(canonical(before))
    assert reviewed_state["content_status"] == "reviewed"
    reviewed_artifact = next(
        artifact
        for artifact in actual6.tools.ledger.artifacts()
        if artifact["path"].endswith(f"investigation-v{reviewed_state['version']}.json")
    )
    reviewed_path = actual6.tools.ledger.resolve_artifact(reviewed_artifact["path"])
    assert json.loads(reviewed_path.read_text(encoding="utf-8")) == reviewed_state
    assert actual6.tools.ledger.verify_integrity() == []


@pytest.mark.asyncio
async def test_qa_comparators_preserve_counterexamples_ties_nulls_axes_and_original_paths(tmp_path):
    tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)
    runtime = AnalystRuntime(settings(tmp_path), tools, no_events)
    scope = binding("synthetic-analysis")["filters"]
    rows = [
        {
            "name": "Group " + chr(65 + index),
            "production_line": "Line-A",
            "category": "Category-A",
            "event_count": index + 1,
            "known_repair_cost": None if index == 2 else float(index),
            "cost_available": index != 2,
            "cost_status": "unavailable" if index == 2 else "complete",
        }
        for index in range(18)
    ]
    rows[16]["event_count"] = rows[17]["event_count"] = 99
    cells = [
        {
            "location": "Surface",
            "severity": "Critical",
            "count": 4,
            "row_pct": 80.0,
            "column_pct": 20.0,
        },
        {
            "location": "Component",
            "severity": "Minor",
            "count": 4,
            "row_pct": 20.0,
            "column_pct": 80.0,
        },
    ]
    entry = tools.ledger.record(
        "analysis_query",
        "# Synthetic whole-population ranking counterexample, not production data",
        {
            "ok": True,
            "context": binding("synthetic-analysis"),
            "analysis": {
                "scope": scope,
                "lines": rows,
                "location_severity": {"cells": cells},
                "kpis": {"known_repair_cost": None, "cost_available": False},
                "metadata": {"limitations": ["Static reference, unavailable actual exposure."]},
            },
        },
        execution_kind="bounded_analysis",
        analysis_context=binding("synthetic-analysis"),
    )
    runtime.model_receipt(entry)
    report = AnalystReport(
        report="Describe the relative populations without a causal claim.", claims=[]
    )
    support, errors = runtime.qa_evidence(
        report, delivery={"artifacts": [], "source_tables": [], "source_execution_ids": []}
    )
    assert not errors
    item = next(item for item in support["qualitative_support"] if item["tool"] == "analysis_query")
    groups = {item["path"]: item for item in item["comparative_aggregates"]}
    ranked = groups["$.analysis.lines"]
    assert ranked["population_size"] == 18
    assert ranked["rows"][0]["event_count"] == 1
    assert ranked["rows"][2]["known_repair_cost"] is None
    assert ranked["rows"][2]["cost_available"] is False
    maximum = next(
        item["maximum"] for item in ranked["extrema"] if item["field"] == "$.event_count"
    )
    assert maximum == {
        "value": 99,
        "matching_rows": 2,
        "example_paths": ["$.analysis.lines[16].event_count", "$.analysis.lines[17].event_count"],
    }
    assert maximum["value"] > ranked["rows"][0]["event_count"]
    cross = groups["$.analysis.location_severity.cells"]
    assert [
        {key: value for key, value in row.items() if key != "source_path"} for row in cross["rows"]
    ] == cells
    assert cross["rows"][0]["source_path"] == "$.analysis.location_severity.cells[0]"
    availability = next(
        item["values"]
        for item in item["aggregate_availability"]
        if item["path"] == "$.analysis.kpis"
    )
    assert availability["known_repair_cost"] is None and availability["cost_available"] is False
    assert {
        "field": "limitations",
        "value": ["Static reference, unavailable actual exposure."],
        "sources": [
            {
                "path": "$.analysis.metadata.limitations",
                "context_ids": ["synthetic-analysis"],
            }
        ],
    } in item["source_projection"]["declared_metadata"]
    assert tools.ledger.records()[0] == entry
    await tools.close()
