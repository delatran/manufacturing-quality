"""Independent claim grammar regressions over sealed evidence.

Every source below is explicitly synthetic. No provider client may be created.
The scripted SDK check establishes the retained QA projection, not live model
calibration or quality. Actual post-v5 failures remain Failed independently.
"""

from __future__ import annotations

import json
from html import unescape
from types import SimpleNamespace

import pytest
import pytest_asyncio
from analyst.case_study.preparation import build_snapshot
from analyst.dashboard import AnalysisService
from analyst.evidence import canonical, sha256
from analyst.product_tools import INSTRUCTIONS_VERSION, TOOLS_VERSION
from analyst.report_output import (
    evidence_manifest,
    metadata_html,
    metric_unit,
    visible_scope_header,
)
from analyst.runtime import AnalystRuntime, ReportDraft, audit_report
from analyst.store import SessionStore
from analyst.tools import AnalystTools
from test_agent_review_independent import ROOT, event, filters, no_events, settings
from test_evidence_integrity import binding, claim_for, payload
from test_measurement_units import ScriptedFinal


@pytest_asyncio.fixture
async def actual7(tmp_path, monkeypatch, request):
    def deny_provider(*_args, **_kwargs):
        raise AssertionError("Independent v7 probes must never create a provider client")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", deny_provider)
    rows = [
        event(1, product_id=81, defect_location="Component", severity="Critical", repair_cost=100),
        event(2, product_id=63, defect_location="Component", severity="Minor", repair_cost=20),
        event(
            3,
            product_id=6,
            defect_date="2024-06-01",
            defect_location="Surface",
            severity="Moderate",
            repair_cost=10,
        ),
        event(
            4,
            product_id=6,
            defect_date="2024-06-02",
            defect_location="Surface",
            severity="Critical",
            repair_cost=35,
        ),
        event(
            5,
            product_id=63,
            defect_date="2024-06-03",
            defect_location="Component",
            severity="Critical",
            repair_cost=20,
        ),
        event(
            6,
            product_id=81,
            defect_date="2024-06-04",
            defect_location="Internal",
            severity="Critical",
            repair_cost=100,
        ),
    ]
    if getattr(request, "param", None) == "observed_cost10":
        # The observed subtotal and policy threshold deliberately coincide.
        # Their semantic roles must remain different even with equal digits.
        rows = [event(1, product_id=81, repair_cost=10)]
    master = (
        b"product_id,product_name,category,production_line,monthly_output\n"
        b"6,Cost criterion product,Category 1,Line-3,100\n"
        b"63,Peer product,Category 2,Line-3,100\n"
        b"81,Leading product,Category 2,Line-3,100\n"
    )

    def loader():
        return build_snapshot(
            rows,
            {
                "source_url": "https://synthetic.invalid/independent-v7",
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
    outputs = {}
    for section in ("kpis", "location_severity", "calendar_coverage"):
        outputs[section] = json.loads(
            await runtime._call(
                "analysis_query", {"section": section, "filters": filters(), "plan": None}
            )
        )
        assert outputs[section]["status"] == "completed"
    outputs["scenario"] = json.loads(
        await runtime._call("scenario_compare", {"scenario": "exclude_cost_10", "plan": None})
    )
    assert outputs["scenario"]["status"] == "completed"
    evidence = {
        key: {item["path"]: item for item in value["numeric_evidence"]}
        for key, value in outputs.items()
    }
    try:
        yield SimpleNamespace(
            runtime=runtime,
            tools=tools,
            store=store,
            sid=sid,
            analysis=analysis,
            bound=bound,
            outputs=outputs,
            evidence=evidence,
        )
    finally:
        await tools.close()
        store.close()


def marker(subject, section, path):
    return "[value:" + subject.evidence[section][path]["id"] + "]"


def save_arguments(text):
    return {
        "question": "Describe the selected source without causal claims.",
        "summary": text,
        "findings": [],
        "hypotheses": [],
        "next_steps": [],
        "priorities": [],
        "filters": filters(),
    }


async def save(subject, text):
    return json.loads(await subject.runtime._call("investigation_update", save_arguments(text)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language,text,valid",
    [
        ("en", "Compared January-June 2024 in the selected source.", True),
        ("en", "Compared January 2024 through June 2024 in the selected source.", True),
        ("en", "Compared Jan-Jun 2024 in the selected source.", True),
        ("vi", "Phạm vi tháng 01–06/2024 trong nguồn đang chọn.", True),
        ("vi", "Phạm vi tháng 1 đến tháng 6 năm 2024 trong nguồn đang chọn.", True),
        ("en", "Compared January-May 2024 in the selected source.", False),
        ("en", "Compared January-June 2025 in the selected source.", False),
        ("en", "Compared June-January 2024 in the selected source.", False),
        ("vi", "Phạm vi tháng 01–07/2024 trong nguồn đang chọn.", False),
        ("en", "Observed 2024 defect events during January-June 2024.", False),
        ("en", "Observed repair cost 2024 local currency during January-June 2024.", False),
        ("en", "Observed 2024 defect events.", False),
        ("en", "The recorded event count is 01–06/2024 events.", False),
        ("en", "The repair cost equals January-June 2024 local currency.", False),
        ("en", "Repair cost: January-June 2024.", True),
    ],
)
async def test_natural_calendar_is_exact_scope_label_and_not_measurement(
    actual7, language, text, valid
):
    before = actual7.store.investigation(actual7.sid)
    response = await save(actual7, text)
    assert (response["status"] == "completed") is valid, response
    after = actual7.store.investigation(actual7.sid)
    if valid:
        assert after["summary"] == text
        assert after["content_status"] == "unverified"
    else:
        assert after == before
        assert response["result"]["error_fields"] == ["summary"]


@pytest.mark.asyncio
async def test_real_first_proposal_shape_fails_only_uncited_policy_value_then_saves(actual7):
    count = marker(actual7, "scenario", "$.selected.analysis.kpis.event_count")
    coverage = marker(actual7, "scenario", "$.selected.analysis.kpis.cost_coverage_pct")
    leader = marker(
        actual7, "scenario", "$.comparison.priority_signals.selected.highest_known_cost_product_id"
    )
    criterion = marker(
        actual7, "scenario", "$.comparison.scenario_definition.parameters.repair_cost_equal"
    )
    args = save_arguments(
        "Compared canonical policy with the exclude_cost_10 financial-availability assumption for January-June 2024. The observed rankings are descriptive."
    )
    args["hypotheses"] = [
        {
            "id": None,
            "text": "Withholding repair costs equal to 10 may change the headline signals in this January-June 2024 scope.",
            "status": "unresolved",
        }
    ]
    args["priorities"] = [
        {
            "evidence": f"The scenario preserves {count} defect events and reports {coverage} cost coverage; the top known-cost Product ID remains {leader}.",
            "action": "Validate source entries before changing policy.",
            "owner": "Quality data steward, proposed.",
            "metric": "Observed events and known-cost coverage in each sensitivity view.",
        }
    ]
    before = actual7.store.investigation(actual7.sid)
    response = json.loads(await actual7.runtime._call("investigation_update", args))
    assert response["status"] == "failed", response
    assert response["result"]["error_fields"] == ["hypotheses[0].text"], response
    assert actual7.store.investigation(actual7.sid) == before
    args["hypotheses"][0]["text"] = (
        f"Withholding repair costs equal to {criterion} may change the headline signals in this January-June 2024 scope."
    )
    response = json.loads(await actual7.runtime._call("investigation_update", args))
    assert response["status"] == "completed", response
    assert actual7.store.investigation(actual7.sid)["content_status"] == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize("actual7", [None, "observed_cost10"], indirect=True)
@pytest.mark.parametrize(
    "variant,valid",
    [
        ("observed_policy_cost", False),
        ("recorded_policy_cost", False),
        ("policy_events", False),
        ("measured_cost", True),
        ("criterion", True),
        ("mixed_valid", True),
        ("mixed_observed_policy", False),
    ],
)
async def test_policy_parameter_purpose_is_independent_of_equal_observed_value(
    actual7, variant, valid
):
    parameter = marker(
        actual7, "scenario", "$.comparison.scenario_definition.parameters.repair_cost_equal"
    )
    cost = marker(actual7, "kpis", "$.analysis.kpis.known_repair_cost")
    text = {
        "observed_policy_cost": f"Under the scenario, observed repair cost equals {parameter} local currency.",
        "recorded_policy_cost": f"Recorded repair cost is {parameter} local currency under the scenario.",
        "policy_events": f"Scenario criterion: repair costs equal {parameter} logged events.",
        "measured_cost": f"Observed repair cost equals {cost} local currency.",
        "criterion": f"Withholding repair costs equal to {parameter} may alter this financial-availability view.",
        "mixed_valid": f"The scenario withholds repair costs equal to {parameter}; canonical observed repair cost equals {cost} local currency.",
        "mixed_observed_policy": f"Canonical observed repair cost is {cost} local currency; under the scenario, observed repair cost equals {parameter} local currency.",
    }[variant]
    before = actual7.store.investigation(actual7.sid)
    response = await save(actual7, text)
    assert (response["status"] == "completed") is valid, response
    if not valid:
        assert actual7.store.investigation(actual7.sid) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant,valid",
    [
        ("remains", True),
        ("is", True),
        ("vi", True),
        ("vi_remains", True),
        ("plural", True),
        ("wrong_measurement_id", False),
        ("scalar_count", False),
        ("scalar_cost", False),
        ("array_count", False),
        ("array_cost", False),
    ],
)
async def test_typed_identity_grammar_never_supplies_measurements(actual7, variant, valid):
    leader = marker(
        actual7, "scenario", "$.comparison.priority_signals.selected.highest_known_cost_product_id"
    )
    first = marker(actual7, "scenario", "$.comparison.rankings.products_by_known_cost.canonical[0]")
    second = marker(
        actual7, "scenario", "$.comparison.rankings.products_by_known_cost.canonical[1]"
    )
    count = marker(actual7, "kpis", "$.analysis.kpis.event_count")
    text = {
        "remains": f"The highest-cost Product ID remains {leader}.",
        "is": f"The highest-cost Product ID is {leader}.",
        "vi": f"Mã sản phẩm là {leader} trong xếp hạng chi phí.",
        "vi_remains": f"Mã sản phẩm vẫn là {leader} trong xếp hạng chi phí.",
        "plural": f"Product IDs {first} and {second} are the leading known-cost products.",
        "wrong_measurement_id": f"Product ID remains {count} in the highest-cost position.",
        "scalar_count": f"Observed {leader} logged defect events.",
        "scalar_cost": f"Observed repair cost {leader} local currency.",
        "array_count": f"Observed {first} logged defect events.",
        "array_cost": f"Observed repair cost {first} local currency.",
    }[variant]
    before = actual7.store.investigation(actual7.sid)
    response = await save(actual7, text)
    assert (response["status"] == "completed") is valid, response
    if not valid:
        assert actual7.store.investigation(actual7.sid) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("add_event_unit", [False, True])
async def test_repeated_ranking_id_list_binds_both_lists_and_keeps_measurement_boundary(
    actual7, add_event_unit
):
    prefix = "$.comparison.rankings.products_by_known_cost."
    first = marker(actual7, "scenario", prefix + "canonical[1]")
    second = marker(actual7, "scenario", prefix + "canonical[2]")
    after_first = marker(actual7, "scenario", prefix + "selected[1]")
    after_second = marker(actual7, "scenario", prefix + "selected[2]")
    source = actual7.outputs["scenario"]["result"]["comparison"]["rankings"][
        "products_by_known_cost"
    ]
    assert source["canonical"][1:3] == [6, 63]
    assert source["selected"][1:3] == [63, 6]
    text = (
        f"Product IDs {first} and {second} switch relative order in the displayed ranking "
        f"to {after_first} and {after_second}" + (" logged events." if add_event_unit else ".")
    )
    response = await save(actual7, text)
    assert (response["status"] == "completed") is (not add_event_unit), response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,currency",
    [
        ("ranking_id", "USD"),
        ("ranking_id", "dollars"),
        ("ranking_id", "VND"),
        ("unit_scale", "USD"),
        ("calendar_days", "USD"),
        ("product_cohort", "VND"),
    ],
)
async def test_nonmoney_source_values_cannot_be_relabelled_with_currency(actual7, role, currency):
    values = {
        "ranking_id": marker(
            actual7, "scenario", "$.comparison.rankings.products_by_known_cost.canonical[0]"
        ),
        "unit_scale": marker(
            actual7, "kpis", "$.metric_metadata.reference_intensity_per_1000.scale"
        ),
        "calendar_days": marker(
            actual7, "calendar_coverage", "$.analysis.calendar_coverage.calendar_days"
        ),
        "product_cohort": marker(actual7, "kpis", "$.analysis.kpis.cohort_product_count"),
    }
    response = await save(actual7, "Observed " + values[role] + " " + currency + ".")
    assert response["status"] == "failed", response


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["identifier", "unit_scale", "calendar", "products"])
async def test_correct_money_claim_does_not_invalidate_another_typed_clause(actual7, role):
    leader = marker(
        actual7, "scenario", "$.comparison.priority_signals.selected.highest_known_cost_product_id"
    )
    scale = marker(actual7, "kpis", "$.metric_metadata.reference_intensity_per_1000.scale")
    days = marker(actual7, "calendar_coverage", "$.analysis.calendar_coverage.calendar_days")
    cohort = marker(actual7, "kpis", "$.analysis.kpis.cohort_product_count")
    amount = marker(actual7, "kpis", "$.analysis.kpis.known_repair_cost")
    clauses = {
        "identifier": f"Product ID remains {leader}.",
        "unit_scale": f"Unit scale: {scale}.",
        "calendar": f"Calendar scope contains {days} calendar days.",
        "products": f"The selected product cohort contains {cohort} products.",
    }
    response = await save(
        actual7, clauses[role] + f" Observed repair cost is {amount} local currency."
    )
    assert response["status"] == "completed", response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant,valid",
    [
        ("actual_output", True),
        ("cited_hyphen", True),
        ("vi_cited", True),
        ("missing_reference", False),
        ("wrong_metric", False),
        ("arbitrary_scale", False),
        ("observed_count", False),
        ("observed_cost", False),
        ("unbound_scale", False),
    ],
)
async def test_actual_r06_unit_grammar_preserves_source_bound_scale(actual7, variant, valid):
    intensity = marker(actual7, "kpis", "$.analysis.kpis.reference_intensity_per_1000")
    scale = marker(actual7, "kpis", "$.metric_metadata.reference_intensity_per_1000.scale")
    count = marker(actual7, "kpis", "$.analysis.kpis.event_count")
    text = {
        "actual_output": f"Reference-output intensity: {intensity} defect events per 1,000 output units; the denominator is static reference output.",
        "cited_hyphen": f"Reference-output intensity: {intensity} defect events per {scale} reference-output units; it is not verified actual production.",
        "vi_cited": f"Cường độ tham chiếu: {intensity} sự kiện lỗi trên {scale} đơn vị sản lượng tham chiếu tĩnh.",
        "missing_reference": "Reference-output intensity: defect events per 1,000 output units.",
        "wrong_metric": f"Reference-output intensity: {count} defect events per 1,000 output units.",
        "arbitrary_scale": f"Reference-output intensity: {intensity} defect events per 10,000 output units.",
        "observed_count": f"Observed {scale} logged defect events; reference intensity is {intensity}.",
        "observed_cost": f"Observed repair cost {scale} local currency; reference intensity is {intensity}.",
        "unbound_scale": f"Reference-output intensity: {intensity} defect events per 1,000 [claim:unknown] reference-output units.",
    }[variant]
    response = await save(actual7, text)
    assert (response["status"] == "completed") is valid, response


@pytest.mark.asyncio
async def test_explicit_scale_citation_from_other_scope_does_not_bind_this_intensity(actual7):
    other = json.loads(
        await actual7.runtime._call(
            "analysis_query",
            {
                "section": "kpis",
                "filters": filters(start_month="2024-01", end_month="2024-01"),
                "plan": None,
            },
        )
    )
    external = next(
        x
        for x in other["numeric_evidence"]
        if x["path"] == "$.metric_metadata.reference_intensity_per_1000.scale"
    )
    intensity = marker(actual7, "kpis", "$.analysis.kpis.reference_intensity_per_1000")
    response = await save(
        actual7,
        f"Reference-output intensity: {intensity} defect events per [value:{external['id']}] reference-output units.",
    )
    assert response["status"] == "failed", response


def manifest_for(subject, section, paths):
    entries = {e["execution_id"]: e for e in subject.tools.ledger.records()}
    claims = []
    for index, path in enumerate(paths):
        assert path in subject.evidence[section]
        claims.append(
            claim_for(
                entries[subject.outputs[section]["receipt_id"]],
                path,
                "independent-unit-" + str(index),
            ).model_dump()
        )
    return evidence_manifest(
        subject.tools.ledger,
        claims,
        language="en",
        instructions_version=INSTRUCTIONS_VERSION,
        tools_version=TOOLS_VERSION,
        bound_context=subject.bound["context"],
    )


@pytest.mark.asyncio
async def test_cell_axes_calendar_and_product_units_are_typed_at_exact_paths(actual7):
    section = actual7.outputs["location_severity"]["result"]["analysis"]["location_severity"]
    index = next(
        i
        for i, row in enumerate(section["cells"])
        if row["location"] == "Component" and row["severity"] == "Critical"
    )
    assert section["cells"][index]["count"] == 2
    assert section["cells"][index]["row_pct"] == pytest.approx(200 / 3)
    # The advertised engine supplies within-location row percentages. The
    # hypothetical within-severity denominator would instead give fifty.
    assert section["cells"][index]["row_pct"] != pytest.approx(50)
    paths = [
        f"$.analysis.location_severity.cells[{index}]." + leaf for leaf in ("count", "row_pct")
    ]
    refs = manifest_for(actual7, "location_severity", paths)["references"]
    assert refs[0]["unit"]["unit"] == "logged defect events"
    assert refs[1]["unit"]["unit"] == "%"
    assert "location" in refs[1]["unit"]["definition"].lower()
    for leaf in ("calendar_days", "days_without_events", "max_observed_day_of_month"):
        ref = manifest_for(actual7, "calendar_coverage", ["$.analysis.calendar_coverage." + leaf])[
            "references"
        ][0]
        assert "day" in ref["unit"]["unit"].lower()
        assert "event" not in ref["unit"]["unit"].lower()
    ref = manifest_for(actual7, "kpis", ["$.analysis.kpis.cohort_product_count"])["references"][0]
    assert "product" in ref["unit"]["unit"].lower()
    assert "event" not in ref["unit"]["unit"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section,path",
    [
        ("calendar_coverage", "$.analysis.calendar_coverage.calendar_days"),
        ("kpis", "$.analysis.kpis.cohort_product_count"),
    ],
)
async def test_calendar_and_master_product_counts_cannot_be_labelled_as_event_counts(
    actual7, section, path
):
    response = await save(
        actual7, "Observed " + marker(actual7, section, path) + " logged defect events."
    )
    assert response["status"] == "failed", response


@pytest.mark.asyncio
async def test_unrecognized_same_leaf_names_do_not_gain_trusted_units(actual7):
    entry = actual7.tools.ledger.record(
        "analysis_query",
        "# Synthetic undeclared section negative",
        {
            "ok": True,
            "analysis": {"invented": {"count": 1000, "row_pct": 100}},
            "context": actual7.bound["context"],
        },
        execution_kind="bounded_analysis",
        analysis_context=actual7.bound["context"],
    )
    assert metric_unit(entry, "$.analysis.invented.count") is None
    assert metric_unit(entry, "$.analysis.invented.row_pct") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
async def test_actual_sdk_qa_header_is_exact_same_visible_renderer_projection(actual7, language):
    text = (
        ("Ghi nhận " if language == "vi" else "Observed ")
        + marker(actual7, "kpis", "$.analysis.kpis.event_count")
        + (
            " sự kiện lỗi được ghi nhận trong phạm vi đã chọn."
            if language == "vi"
            else " logged defect events in the selected scope."
        )
    )
    observed = []

    def model(role):
        if role == "QA Verifier" and observed:
            raise AssertionError("This fixture expects one successful QA review, without retries")
        value = ScriptedFinal(actual7.runtime, role, text, observed)
        actual7.runtime.models.append(value)
        return value

    actual7.runtime.model = model
    result = await actual7.runtime.run(
        "Tóm tắt bằng chứng đã chọn, không thay đổi giao diện."
        if language == "vi"
        else "Summarize the selected evidence without changing the view."
    )
    assert result["status"] == "completed", result.get("qa")
    assert len(observed) == 1
    qa = json.loads(observed[0])
    assert qa["language"] == language and qa["audit"]["verified"]
    manifest = result["delivery"]["source_manifest"]
    header = qa["visible_scope_header"]
    assert header == visible_scope_header(manifest)
    assert header["source_contexts_sha256"] == sha256(canonical(manifest["contexts"]))
    assert header["shared_period"] == {"start_month": "2024-01", "end_month": "2024-06"}
    html = unescape(metadata_html(manifest))
    assert all(row["label"] in html and row["text"] in html for row in header["contexts"])
    assert actual7.tools.ledger.verify_integrity() == []


@pytest.mark.asyncio
async def test_mixed_period_header_never_supplies_one_false_shared_period(actual7):
    comparison = json.loads(
        await actual7.runtime._call(
            "analysis_compare",
            {
                "baseline_filters": filters(start_month="2024-01", end_month="2024-01"),
                "comparison_filters": filters(start_month="2024-06", end_month="2024-06"),
                "dimension": "product_id",
                "plan": None,
            },
        )
    )
    entries = {entry["execution_id"]: entry for entry in actual7.tools.ledger.records()}
    proofs = []
    for side in ("baseline", "comparison"):
        path = f"$.{side}.analysis.kpis.event_count"
        assert any(item["path"] == path for item in comparison["numeric_evidence"])
        proofs.append(claim_for(entries[comparison["receipt_id"]], path, side).model_dump())
    manifest = evidence_manifest(
        actual7.tools.ledger,
        proofs,
        language="en",
        instructions_version=INSTRUCTIONS_VERSION,
        tools_version=TOOLS_VERSION,
        bound_context=actual7.bound["context"],
    )
    header = visible_scope_header(manifest)
    assert header["shared_period"] is None
    assert len(header["contexts"]) == 3
    assert any("2024-01 to 2024-01" in row["text"] for row in header["contexts"])
    assert any("2024-06 to 2024-06" in row["text"] for row in header["contexts"])


@pytest.mark.asyncio
async def test_missing_comparison_context_cannot_be_used_for_a_qualified_header(actual7):
    before = payload(binding("synthetic-before"), 1, 10.0, 100.0)
    after = payload(binding("synthetic-after", "2024-06"), 2, 20.0, 100.0)
    after.pop("context")
    entry = actual7.tools.ledger.record(
        "analysis_compare",
        "# Synthetic missing side negative",
        {"ok": True, "baseline": before, "comparison": after},
        execution_kind="bounded_analysis",
        analysis_context=actual7.bound["context"],
    )
    claim = claim_for(entry, "$.baseline.analysis.kpis.event_count", "before")
    with pytest.raises(ValueError):
        evidence_manifest(
            actual7.tools.ledger,
            [claim.model_dump()],
            language="en",
            instructions_version=INSTRUCTIONS_VERSION,
            tools_version=TOOLS_VERSION,
            bound_context=actual7.bound["context"],
        )


@pytest.mark.asyncio
async def test_source_tampering_cannot_relabel_a_calendar_period(actual7):
    ledger = actual7.tools.ledger
    entries = ledger.records()
    selected = next(
        entry for entry in entries if entry["execution_id"] == actual7.outputs["kpis"]["receipt_id"]
    )
    selected["result"]["context"]["filters"]["end_month"] = "2024-05"
    ledger.path.write_text(
        "\n".join(canonical(entry) for entry in entries) + "\n", encoding="utf-8"
    )
    report = actual7.runtime.resolve_report(
        ReportDraft(
            report="Observed "
            + marker(actual7, "kpis", "$.analysis.kpis.event_count")
            + " events in January-May 2024."
        )
    )
    assert not audit_report(report, ledger)["verified"]
    assert ledger.verify_integrity()
