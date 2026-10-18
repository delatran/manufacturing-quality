"""Source-bound unit scales, signed changes and exact compact QA, without API calls."""

from __future__ import annotations

import copy
import json
import re

import pytest
from agents import Model, ModelResponse, Usage
from analyst.agent_views import bounded_result, qa_comparisons
from analyst.case_study.analytics import DEFINITIONS, METRIC_METADATA
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.investigation import user_changes
from analyst.product_tools import investigation_content_hash
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    NumericClaim,
    ReportDraft,
    _message,
    aggregate_availability,
    audit_report,
)
from openai.types.responses import ResponseFunctionToolCall
from test_claim_feedback import evidence_subject as evidence_subject
from test_claim_feedback import filters


@pytest.fixture
def unit_subject(tmp_path):
    ledger = EvidenceLedger(tmp_path / "unit-ledger")
    analysis = {
        "metric_metadata": copy.deepcopy(METRIC_METADATA),
        "definitions": DEFINITIONS,
        "kpis": {"event_count": 147, "known_repair_cost": 65488.29},
    }
    entry = ledger.record(
        "analysis_compare",
        "# original source values, not model arithmetic",
        {
            "ok": True,
            "baseline": {"analysis": copy.deepcopy(analysis)},
            "comparison": {"analysis": copy.deepcopy(analysis)},
            "decomposition": {
                "reference_intensity_change": 1.049868766404199,
                "event_count_change": -5,
            },
        },
        execution_kind="bounded_analysis",
    )
    return ledger, entry


def resolved(entry, text, language="en"):
    paths = {
        "intensity": "$.decomposition.reference_intensity_change",
        "change": "$.decomposition.event_count_change",
        "count": "$.baseline.analysis.kpis.event_count",
        "cost": "$.baseline.analysis.kpis.known_repair_cost",
    }
    values = {item["path"]: item["value"] for item in entry["numeric_values"]}
    claims = []

    def replace(match):
        identity = match[1]
        value = values[paths[identity]]
        claim = NumericClaim(
            id=identity,
            execution_id=entry["execution_id"],
            path=paths[identity],
            value=value,
            code_sha256=entry["code_sha256"],
        )
        if claim not in claims:
            claims.append(claim)
        number = f"{value:,.4f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
        if language == "vi":
            number = number.translate(str.maketrans({",": ".", ".": ","}))
        return number + f" [claim:{identity}]"

    return AnalystReport(
        report=re.sub(r"\[value:([^\]]+)\]", replace, text),
        claims=claims,
        number_format="vi-VN" if language == "vi" else "en-US",
    )


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("en", "Reference intensity changed [value:intensity] events per 1,000 units."),
        ("en", "Intensity changed [value:intensity] per 1000 reference output units."),
        ("en", "Intensity changed [value:intensity] events per 1,000 reference units."),
        ("vi", "Cường độ tham chiếu thay đổi [value:intensity] sự kiện trên 1.000 đơn vị."),
        ("vi", "Cường độ thay đổi [value:intensity] trên mỗi 1.000 đơn vị tham chiếu."),
        (
            "vi",
            "Chỉ số sự kiện trên sản lượng tham chiếu tăng [value:intensity] sự kiện trên 1.000 đơn vị giữa hai tháng, nhưng mẫu số là sản lượng tham chiếu tĩnh.",
        ),
        (
            "en",
            "Observed cost [value:cost] local currency; reference intensity [value:intensity] per 1,000 reference units; count [value:count] events.",
        ),
        (
            "vi",
            "Chi phí [value:cost] local currency; cường độ [value:intensity] trên 1.000 đơn vị tham chiếu; số đếm [value:count] sự kiện.",
        ),
    ],
)
def test_actual_unit_scale_is_a_same_clause_denominator(unit_subject, language, text):
    ledger, entry = unit_subject
    report = resolved(entry, text, language)
    before = report.report
    assert audit_report(report, ledger)["verified"]
    assert report.report == before


@pytest.mark.parametrize(
    "text",
    [
        "Reference intensity [value:intensity]; observed 1,000 events.",
        "Reference intensity [value:intensity]; observed cost 1,000 local currency.",
        "Reference intensity [value:intensity] per 10,000 reference units.",
        "Reference intensity [value:intensity] per 100 reference units.",
        "Reference intensity [value:intensity] per 1,000 actual production units.",
        "Count [value:count] per 1,000 reference units.",
        "Cost [value:cost] per 1,000 reference units.",
        "Reference intensity [value:intensity]; count [value:count] per 1,000 units.",
        "Count [value:count] per 1,000 reference units and intensity [value:intensity].",
        "Reference intensity [value:intensity]. Per 1,000 reference units.",
        "Reference intensity [value:intensity] and 1,000 events per 1,000 reference units.",
        "Per 1,000 reference units.",
    ],
)
def test_unit_scale_never_establishes_an_observed_measurement_or_other_metric(unit_subject, text):
    ledger, entry = unit_subject
    assert not audit_report(resolved(entry, text), ledger)["verified"]


@pytest.mark.parametrize("damage", ["scale", "unit", "missing", "wrong_side", "tampered"])
def test_unit_scale_requires_the_exact_source_declaration(unit_subject, damage):
    ledger, old = unit_subject
    result = copy.deepcopy(old["result"])
    descriptor = result["baseline"]["analysis"]["metric_metadata"]["reference_intensity_per_1000"]
    if damage in {"scale", "wrong_side"}:
        descriptor["scale"] = 10000
    elif damage == "unit":
        descriptor["unit"] = "actual produced units"
    elif damage == "missing":
        del result["baseline"]["analysis"]["metric_metadata"]
    entry = ledger.record(
        "analysis_compare",
        "# declared source metadata fixture",
        result,
        execution_kind="bounded_analysis",
    )
    report = resolved(entry, "Reference intensity [value:intensity] per 1,000 reference units.")
    if damage == "tampered":
        path = ledger.run_dir / "ledger.jsonl"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"scale":1000', '"scale":10000'), encoding="utf-8")
    assert not audit_report(report, ledger)["verified"]


@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ("The count decreased by [value:change] events.", False),
        ("The count declined [value:change] events.", False),
        ("Số sự kiện giảm [value:change].", False),
        ("Số sự kiện giảm là [value:change].", False),
        ("The count change is [value:change] events.", True),
        ("Chênh lệch số sự kiện là [value:change].", True),
        ("Số sự kiện thay đổi [value:change].", True),
        ("The count decreased; the signed change is [value:change] events.", True),
    ],
)
def test_negative_change_requires_unambiguous_direction_without_rewriting(
    unit_subject, text, valid
):
    ledger, entry = unit_subject
    report = resolved(entry, text)
    before = report.report
    audit = audit_report(report, ledger)
    assert audit["verified"] is valid, audit
    assert report.report == before
    if not valid:
        assert any("signed negative change" in error for error in audit["errors"])


def saved_arguments(runtime):
    claims = runtime.claim_catalog.values()
    change = next(
        claim for claim in claims if claim.path.endswith(".event_count_change") and claim.value < 0
    )
    intensity = next(
        claim for claim in claims if claim.path == "$.decomposition.reference_intensity_change"
    )
    cost = next(
        claim for claim in claims if claim.path == "$.decomposition.known_repair_cost_change"
    )
    coverage = next(
        claim for claim in claims if claim.path == "$.baseline.analysis.kpis.cost_coverage_pct"
    )
    return {
        "question": "Investigate the recorded comparison",
        "summary": "A descriptive comparison; causes remain unverified.",
        "findings": [
            f"Số sự kiện giảm [value:{change.id}].",
            f"Chỉ số sự kiện trên sản lượng tham chiếu tăng [value:{intensity.id}] sự kiện trên 1.000 đơn vị giữa hai tháng, nhưng mẫu số là sản lượng tham chiếu tĩnh.",
            f"Chi phí thay đổi [value:{cost.id}] local currency; độ phủ [value:{coverage.id}]%.",
        ],
        "hypotheses": [{"id": None, "text": "The reporting mix may differ.", "status": "open"}],
        "priorities": [
            {
                "evidence": "The comparison is descriptive.",
                "action": "Collect exposure data.",
                "owner": "Proposed process owner",
                "metric": "Verified exposure",
            }
        ],
        "next_steps": ["Validate actual production and recording completeness."],
        "filters": filters(),
    }


def vietnamese_subject(subject):
    previous, tools, store, sid, ids, june, events = subject
    runtime = AnalystRuntime(
        previous.settings,
        tools,
        previous.emit,
        analysis_context=previous.analysis_context,
        analysis_service=previous.analysis_service,
        state_store=store,
        language="vi",
    )
    # Reuse the exact receipts already issued in this disposable test run.
    runtime.claim_catalog = dict(previous.claim_catalog)
    runtime.claim_identities = dict(previous.claim_identities)
    runtime.exposed_receipts = set(previous.exposed_receipts)
    runtime.context_entry = previous.context_entry
    return runtime, tools, store, sid, ids, june, events


@pytest.mark.asyncio
async def test_prewrite_reports_all_bad_fields_without_rejecting_true_unit_scale(evidence_subject):
    runtime, tools, store, sid, _, _, _ = vietnamese_subject(evidence_subject)
    arguments = saved_arguments(runtime)
    change = next(
        c
        for c in runtime.claim_catalog.values()
        if c.path.endswith(".event_count_change") and c.value < 0
    )
    arguments["findings"][0] = f"Số sự kiện giảm -5 [claim:{change.id}]."
    arguments["next_steps"] = ["Inspect 999 unsupported events."]
    before = store.investigation(sid)
    rejected = json.loads(await runtime._call("investigation_update", arguments))
    result = rejected["result"]
    assert rejected["status"] == "failed", rejected
    assert set(result["error_fields"]) == {"findings[0]", "next_steps[0]"}
    assert "1.000" not in result["error"]
    assert "signed negative change" in result["error"]
    assert store.investigation(sid) == before and not tools.ledger.artifacts()


@pytest.mark.asyncio
async def test_saved_units_exact_state_and_all_analytical_comparisons_survive_compact_qa(
    evidence_subject,
):
    runtime, tools, store, sid, ids, _, _ = vietnamese_subject(evidence_subject)
    arguments = saved_arguments(runtime)
    arguments["findings"][0] = arguments["findings"][0].replace("giảm", "thay đổi")
    saved = json.loads(await runtime._call("investigation_update", arguments))
    assert saved["status"] == "completed", saved
    state = store.investigation(sid)
    assert "trên 1.000 đơn vị" in state["findings"][1]["text"]
    assert set(state["unit_notes"]) == {"en", "vi"}
    assert "source does not specify the currency" in state["unit_notes"]["en"]
    assert "nguồn không xác định loại tiền" in state["unit_notes"]["vi"]
    assert "phần trăm" in state["unit_notes"]["vi"]
    assert "per thousand" in state["unit_notes"]["en"]
    assert "**" not in state["unit_notes"]["en"]
    pending = runtime.product_tools.pending_state()
    assert pending["state"] == state and pending["state_sha256"] == sha256(canonical(state))
    artifact = tools.ledger.resolve_artifact(pending["artifact"]["path"])
    assert json.loads(artifact.read_text(encoding="utf-8")) == state
    report = runtime.resolve_report(
        ReportDraft(report=f"Đã ghi nhận [value:{ids['june8']}] sự kiện.")
    )
    support, errors = runtime.qa_evidence(report, delivery={"source_tables": [], "artifacts": []})
    assert not errors
    entries = {entry["execution_id"]: entry for entry in tools.ledger.records()}
    for item in support["qualitative_support"]:
        entry = entries[item["equivalent_receipt_ids"][0]]
        paths = [
            claim["path"]
            for claim in support["numeric_support"]
            if claim["claim_id"] in runtime.claim_catalog
            and runtime.claim_catalog[claim["claim_id"]].execution_id == entry["execution_id"]
        ]
        original = qa_comparisons(entry, paths)
        if item["tool"] == "investigation_update":
            assert {group["path"] for group in original} >= {
                "$.investigation.findings",
                "$.investigation.hypotheses",
                "$.investigation.priorities",
            }
            assert not item["comparative_aggregates"]
            assert item["aggregate_availability"] == aggregate_availability(
                bounded_result(entry)[0]
            )
            assert any(group["values"].get("effect") for group in item["aggregate_availability"])
        else:
            assert item["comparative_aggregates"] == original
    payload = {
        **support,
        "report": report.report,
        "investigation_draft": {key: pending[key] for key in ("version", "state_sha256", "state")},
    }
    compact = canonical(payload)
    assert json.loads(compact) == json.loads(json.dumps(payload))
    assert "Đã ghi nhận" in compact and "\\u0110" not in compact
    assert len(compact.encode()) < len(json.dumps(payload).encode())
    assert store.investigation(sid) == state and runtime.product_tools.pending_state() == pending
    changed = copy.deepcopy(state)
    changed["unit_notes"]["en"] += " Altered."
    assert investigation_content_hash(changed) != investigation_content_hash(state)
    with pytest.raises(ValueError, match="supported content fields"):
        user_changes(
            state, {"expected_version": state["version"], "unit_notes": {"en": "Invented"}}
        )


def test_empty_unit_notes_preserve_old_content_hash(tmp_path):
    from analyst.store import SessionStore

    store = SessionStore(tmp_path / "old-state")
    try:
        sid = store.create_session()["id"]
        state = store.investigation(sid)
        assert state["unit_notes"] == {}
        old = {key: value for key, value in state.items() if key != "unit_notes"}
        assert investigation_content_hash(state) == investigation_content_hash(old)
        with store.db:
            store.db.execute(
                "INSERT INTO investigations VALUES (?,?,?,?)",
                (
                    sid,
                    7,
                    "old-time",
                    json.dumps(
                        {
                            key: value
                            for key, value in old.items()
                            if key not in {"session_id", "version", "updated_at"}
                        }
                    ),
                ),
            )
        loaded = store.investigation(sid)
        assert loaded["version"] == 7 and "unit_notes" not in loaded
        assert investigation_content_hash(loaded) == investigation_content_hash(old)
        assert "unit_notes" not in json.loads(
            store.db.execute(
                "SELECT payload FROM investigations WHERE session_id=?", (sid,)
            ).fetchone()[0]
        )
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metric", ["reference_intensity_per_1000", "critical_share_pct", "cost_coverage_pct"]
)
@pytest.mark.parametrize("language", ["en", "vi"])
async def test_actual_exposed_unit_scale_marker_cannot_prove_an_observed_count(
    evidence_subject, metric, language
):
    runtime, tools, _, _, _, _, _ = (
        vietnamese_subject(evidence_subject) if language == "vi" else evidence_subject
    )
    view = json.loads(
        await runtime._call("analysis_query", {"section": "kpis", "filters": filters()})
    )
    scale = next(
        claim
        for claim in view["numeric_evidence"]
        if claim["path"] == f"$.metric_metadata.{metric}.scale"
    )
    false_count = runtime.resolve_report(
        ReportDraft(report=f"Observed [value:{scale['id']}] logged events.")
    )
    assert not audit_report(false_count, tools.ledger)["verified"]
    explicit = runtime.resolve_report(
        ReportDraft(
            report=("Hệ số đơn vị: " if language == "vi" else "Unit scale: ")
            + f"[value:{scale['id']}]."
        )
    )
    assert audit_report(explicit, tools.ledger)["verified"]
    evidence, errors = runtime.qa_evidence(
        false_count, delivery={"source_tables": [], "artifacts": []}
    )
    assert not errors
    assert (
        next(claim for claim in evidence["numeric_support"] if claim["claim_id"] == scale["id"])[
            "role"
        ]
        == "unit_scale"
    )
    intensity = next(
        claim
        for claim in view["numeric_evidence"]
        if claim["path"] == "$.analysis.kpis.reference_intensity_per_1000"
    )
    true_unit = runtime.resolve_report(
        ReportDraft(
            report=f"Reference intensity [value:{intensity['id']}] events per 1,000 reference units."
        )
    )
    assert audit_report(true_unit, tools.ledger)["verified"]


@pytest.mark.asyncio
async def test_sdk_qa_uses_compact_unicode_and_saved_work_repair_requires_tool(
    evidence_subject, monkeypatch
):
    runtime, tools, store, sid, ids, _, _ = vietnamese_subject(evidence_subject)
    arguments = saved_arguments(runtime)
    arguments["findings"][0] = arguments["findings"][0].replace("giảm", "thay đổi")
    assert (
        json.loads(await runtime._call("investigation_update", arguments))["status"] == "completed"
    )
    qa_inputs, repair_inputs = [], []

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
                text = input if isinstance(input, str) else input[-1]["content"]
                payload = json.loads(text)
                qa_inputs.append(payload)
                assert text == canonical(payload) and "\\u0110" not in text
                state = payload["investigation_draft"]["state"]
                assert state["unit_notes"]["vi"] and state == store.investigation(sid)
                assert payload["investigation_draft"]["state_sha256"] == sha256(canonical(state))
                verified = self.step > 1
                output = [
                    _message(
                        json.dumps(
                            {
                                "verified": verified,
                                "errors": []
                                if verified
                                else ["Saved finding wording requires revision."],
                                "suggestions": [],
                            }
                        )
                    )
                ]
            elif self.step == 2:
                repair_inputs.extend(input)
                replacement = {
                    **arguments,
                    "summary": "Saved work was revised; causes remain unverified.",
                }
                output = [
                    ResponseFunctionToolCall(
                        id="fc_saved_repair",
                        call_id="call_saved_repair",
                        name="investigation_update",
                        arguments=json.dumps(replacement),
                        type="function_call",
                        status="completed",
                    )
                ]
            else:
                output = [
                    _message(
                        json.dumps(
                            {
                                "report": f"Đã ghi nhận [value:{ids['june8']}] sự kiện. Hồ sơ đã lưu vẫn phân biệt phát hiện mô tả và giả thuyết mở.",
                                "chart_paths": [],
                            }
                        )
                    )
                ]
            self.history = (
                [{"role": "user", "content": input}] if isinstance(input, str) else list(input)
            ) + [item.model_dump() for item in output]
            return ModelResponse(
                output=output,
                usage=Usage(requests=0),
                response_id=f"offline_{self.role}_{self.step}",
            )

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError
            yield

    models = {role: ScriptedModel(role) for role in ("Lead Analyst", "QA Verifier")}
    monkeypatch.setattr(runtime, "model", lambda role: models[role])
    result = await runtime.run("Điều tra so sánh và lưu hồ sơ với đơn vị đúng.")
    assert result["status"] == "completed", result["qa"]
    assert len(qa_inputs) == 2
    assert any(
        "call investigation_update" in str(item.get("content", "")) for item in repair_inputs
    )
    assert store.investigation(sid)["summary"].startswith("Saved work was revised")
    reviews = [
        entry
        for entry in tools.ledger.records()
        if entry["tool"] == "report_semantic_review" and entry["result"]["phase"] == "requested"
    ]
    assert len(reviews) == 2
    for entry, parsed in zip(reviews, qa_inputs, strict=True):
        retained = tools.run_dir / entry["result"]["runner_input_path"]
        assert retained.read_bytes() == canonical(parsed).encode()
