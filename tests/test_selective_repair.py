"""Same-run selective repairs over disposable sources, with provider creation denied."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import pytest_asyncio
from analyst.case_study.analytics import Scope
from analyst.case_study.preparation import build_snapshot
from analyst.config import Settings
from analyst.dashboard import AnalysisService
from analyst.evidence import canonical, sha256
from analyst.product_tools import product_schemas
from analyst.runtime import AnalystRuntime, ReportDraft
from analyst.store import SessionStore
from analyst.tools import AnalystTools

AUTHORED = ("question", "summary", "findings", "hypotheses", "next_steps", "priorities")


def filters(**changes):
    return {**{field.name: None for field in fields(Scope)}, **changes}


def partial(**changes):
    return {**dict.fromkeys(AUTHORED), "filters": filters(), **changes}


@pytest_asyncio.fixture
async def subject(tmp_path, monkeypatch):
    def deny_provider(*_args, **_kwargs):
        raise AssertionError("Selective repair verification cannot construct a provider")

    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr("analyst.model.AsyncOpenAI", deny_provider)
    rows = [
        {
            "defect_id": index,
            "product_id": 1,
            "defect_type": "Functional",
            "defect_date": "2024-01-15" if index < 3 else "2024-06-15",
            "defect_location": "Component",
            "severity": "Critical" if index != 2 else "Minor",
            "inspection_method": "Visual Inspection",
            "repair_cost": None if index == 2 else 20,
        }
        for index in range(1, 5)
    ]
    master = (
        b"product_id,product_name,category,production_line,monthly_output\n"
        b"1,Synthetic product,Category A,Line-1,100\n"
    )

    def loader():
        return build_snapshot(
            rows,
            {
                "source_url": "https://synthetic.invalid/selective-repair",
                "source_sha256": sha256(canonical(rows)),
                "fetched_at": "2026-10-16T00:00:00Z",
            },
            master,
        )

    root = Path(__file__).resolve().parents[1]
    analysis = AnalysisService(root, tmp_path / "analysis", snapshot_loader=loader)
    bound = await analysis.get_context()
    store = SessionStore(tmp_path / "store")
    sid = store.create_session()["id"]
    tools = AnalystTools(tmp_path / "run", [], session_id=sid, memory_enabled=False)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    runtime = AnalystRuntime(
        Settings(mode="live", api_key="offline-invalid-key", memory_enabled=False),
        tools,
        emit,
        analysis_context=bound,
        analysis_service=analysis,
        state_store=store,
    )
    query = json.loads(
        await runtime._call("analysis_query", {"section": "kpis", "filters": filters()})
    )
    assert query["status"] == "completed", query
    evidence = {item["path"]: item["id"] for item in query["numeric_evidence"]}

    def marker(metric):
        return "[value:" + evidence["$.analysis.kpis." + metric] + "]"

    initial = {
        "question": "Describe the logged events and retain competing explanations.",
        "summary": "The selected scope has " + marker("event_count") + " logged events.",
        "findings": [
            "Observed " + marker("event_count") + " logged events.",
            "Known repair cost is " + marker("known_repair_cost") + " local currency.",
            "Valid-cost coverage is " + marker("cost_coverage_pct") + " %.",
            "Reference intensity is "
            + marker("reference_intensity_per_1000")
            + " logged events per 1,000 reference-output units.",
        ],
        "hypotheses": [
            {
                "id": None,
                "text": "The product mix may differ; exposure evidence is needed.",
                "status": "open",
            },
            {
                "id": None,
                "text": "Reporting completeness may differ and needs verification.",
                "status": "open",
            },
        ],
        "next_steps": ["Obtain actual exposure and independently check recording completeness."],
        "priorities": [],
        "filters": filters(),
    }
    try:
        yield SimpleNamespace(
            runtime=runtime,
            tools=tools,
            store=store,
            sid=sid,
            bound=bound,
            analysis=analysis,
            initial=initial,
            events=events,
        )
    finally:
        await tools.close()
        store.close()


async def save(subject, arguments):
    return json.loads(await subject.runtime._call("investigation_update", arguments))


async def seeded(subject):
    result = await save(subject, deepcopy(subject.initial))
    assert result["status"] == "completed", result
    return deepcopy(subject.store.investigation(subject.sid))


def changed_hypotheses(subject):
    values = deepcopy(subject.initial["hypotheses"])
    values[0]["text"] = (
        "The product mix is an open explanation; its contribution has not been identified."
    )
    return values


@pytest.mark.asyncio
async def test_one_hypothesis_repair_keeps_every_other_field_and_original_claim(subject):
    before = await seeded(subject)
    result = await save(subject, partial(hypotheses=changed_hypotheses(subject)))
    assert result["status"] == "completed", result
    after = subject.store.investigation(subject.sid)
    for name in ("question", "summary", "findings", "next_steps", "priorities", "unit_notes"):
        assert after[name] == before[name]
    assert after["hypotheses"][1] == before["hypotheses"][1]
    assert after["hypotheses"][0]["text"] != before["hypotheses"][0]["text"]
    assert after["version"] == before["version"] + 1
    assert after["content_status"] == "unverified"
    assert after["semantic_review"] is None
    pending = subject.runtime.product_tools.pending_state()
    assert pending["proposal_sha256"] == sha256(canonical(pending["proposal"]))
    assert pending["run_id"] == subject.tools.run_dir.name
    assert all(pending["proposal"][name] is not None for name in AUTHORED)
    assert pending["proposal"]["findings"] == subject.initial["findings"]
    assert (
        json.loads(
            subject.tools.ledger.resolve_artifact(pending["artifact"]["path"]).read_text(
                encoding="utf8"
            )
        )
        == after
    )
    report = subject.runtime.resolve_report(ReportDraft(report=subject.initial["summary"]))
    evidence, errors = subject.runtime.qa_evidence(report)
    assert not errors
    assert pending["state_sha256"] == sha256(canonical(after))
    assert evidence["numeric_support"]
    assert any(item["unit"]["kind"] == "cost" for item in evidence["numeric_support"])


@pytest.mark.asyncio
async def test_repeated_partial_repair_uses_the_resolved_preceding_proposal(subject):
    await seeded(subject)
    first = await save(subject, partial(hypotheses=changed_hypotheses(subject)))
    assert first["status"] == "completed", first
    middle = deepcopy(subject.store.investigation(subject.sid))
    second = await save(
        subject, partial(next_steps=["Confirm exposure before deciding on an intervention."])
    )
    assert second["status"] == "completed", second
    after = subject.store.investigation(subject.sid)
    assert after["hypotheses"] == middle["hypotheses"]
    assert after["findings"] == middle["findings"]
    assert after["unit_notes"] == middle["unit_notes"]


@pytest.mark.asyncio
async def test_explicit_empty_array_clears_only_that_collection(subject):
    before = await seeded(subject)
    result = await save(subject, partial(next_steps=[]))
    assert result["status"] == "completed", result
    after = subject.store.investigation(subject.sid)
    assert after["next_steps"] == []
    for name in ("findings", "hypotheses", "summary", "unit_notes"):
        assert after[name] == before[name]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", AUTHORED)
async def test_first_save_cannot_inherit_any_authored_field(subject, field):
    arguments = deepcopy(subject.initial)
    arguments[field] = None
    before = deepcopy(subject.store.investigation(subject.sid))
    result = await save(subject, arguments)
    assert result["status"] == "failed", result
    assert "successful save" in result["result"]["error"]
    assert subject.store.investigation(subject.sid) == before
    assert subject.runtime.product_tools.pending_state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["all_null", "unchanged_array", "unchanged_text"])
async def test_partial_noop_does_not_bump_version(subject, kind):
    before = await seeded(subject)
    arguments = partial()
    if kind == "unchanged_array":
        arguments["hypotheses"] = deepcopy(subject.initial["hypotheses"])
    if kind == "unchanged_text":
        arguments["summary"] = subject.initial["summary"]
    result = await save(subject, arguments)
    assert result["status"] == "failed", result
    assert "No investigation fields changed" in result["result"]["error"]
    assert subject.store.investigation(subject.sid) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "owner_content",
        "version",
        "proposal",
        "run",
        "run_directory",
        "scope",
        "pending_state_context",
    ],
)
async def test_partial_repair_fails_closed_for_stale_or_tampered_binding(subject, damage):
    before = await seeded(subject)
    arguments = partial(hypotheses=changed_hypotheses(subject))
    pending = subject.runtime.product_tools.pending_investigation
    if damage == "owner_content":
        subject.store.save_investigation(
            subject.sid, before["version"], {"summary": "Owner content must remain."}
        )
    elif damage == "version":
        subject.store.save_investigation(subject.sid, before["version"], {"ui": before["ui"]})
    elif damage == "proposal":
        pending["proposal"]["summary"] = "Tampered inherited proposal."
    elif damage == "run":
        pending["run_id"] = "a-different-run"
    elif damage == "run_directory":
        pending["run_dir"] = str(
            subject.tools.run_dir.parent / "another" / subject.tools.run_dir.name
        )
    else:
        arguments["filters"] = filters(severity="Critical")
        if damage == "pending_state_context":
            alternate = await subject.analysis.get_context({"severity": "Critical"})
            pending["state"]["context_id"] = alternate["context"]["context_id"]
            pending["state"]["scope"] = alternate["context"]["filters"]
    current = deepcopy(subject.store.investigation(subject.sid))
    result = await save(subject, arguments)
    assert result["status"] == "failed", result
    assert subject.store.investigation(subject.sid) == current


@pytest.mark.asyncio
async def test_fresh_runtime_cannot_inherit_another_runs_pending_proposal(subject):
    before = await seeded(subject)
    old = subject.runtime.product_tools.pending_investigation
    subject.runtime.product_tools.pending_investigation = None
    result = await save(subject, partial(hypotheses=changed_hypotheses(subject)))
    assert result["status"] == "failed", result
    assert subject.store.investigation(subject.sid) == before
    subject.runtime.product_tools.pending_investigation = old


@pytest.mark.asyncio
async def test_unbound_new_number_still_fails_whole_numeric_audit_atomically(subject):
    before = await seeded(subject)
    result = await save(subject, partial(summary="Observed 999999 logged events."))
    assert result["status"] == "failed", result
    assert "summary" in result["result"]["error_fields"]
    assert subject.store.investigation(subject.sid) == before


@pytest.mark.asyncio
async def test_pending_proposal_is_detached_from_callers_mutable_arrays(subject):
    arguments = deepcopy(subject.initial)
    result = await save(subject, arguments)
    assert result["status"] == "completed", result
    arguments["findings"][0] = "Caller changed its old argument."
    assert (
        subject.runtime.product_tools.pending_state()["proposal"]["findings"]
        == subject.initial["findings"]
    )
    result = await save(subject, partial(hypotheses=changed_hypotheses(subject)))
    assert result["status"] == "completed", result


@pytest.mark.parametrize("field", ["findings", "hypotheses", "next_steps", "priorities"])
def test_nullable_outer_arrays_keep_strict_nonnullable_items(field):
    schema = next(item for item in product_schemas() if item["name"] == "investigation_update")[
        "parameters"
    ]
    assert set(schema["required"]) == {*AUTHORED, "filters"}
    assert schema["additionalProperties"] is False
    assert "null" in schema["properties"][field]["type"]
    arguments = partial(**{field: [None]})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(arguments, schema)
