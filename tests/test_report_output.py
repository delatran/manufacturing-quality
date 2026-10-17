"""Portable evidence, units and readability are checked without a model call."""

from __future__ import annotations

import copy
import json
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from analyst.case_study.analytics import DEFINITIONS, METRIC_METADATA
from analyst.config import Settings
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.product_tools import INSTRUCTIONS_VERSION, SECTIONS, TOOLS_VERSION
from analyst.report_output import evidence_manifest, metric_unit, with_unit_note
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    NumericClaim,
    ReportDraft,
    audit_exported_reports,
    audit_report,
    business_scope,
    render_business_report,
)
from test_claim_feedback import evidence_subject as evidence_subject
from test_claim_feedback import filters


def context(identity, start="2024-01", end="2024-06", **filters):
    return {
        "context_id": identity,
        "snapshot_id": "snapshot-A",
        "dataset_version": "dataset-A",
        "policy_version": "canonical-v2",
        "source_sha256": "source-A",
        "master_sha256": "master-A",
        "code_sha256": "calculation-A",
        "kpi_version": "kpi-A",
        "scenario": filters.get("scenario", ""),
        "filters": {
            "start_month": start,
            "end_month": end,
            "production_line": "Line-3",
            "severity": "",
            "scenario": "",
            **filters,
        },
    }


def payload(binding, events, cost, coverage):
    return {
        "context": binding,
        "analysis": {
            "scope": binding["filters"],
            "kpis": {
                "event_count": events,
                "known_repair_cost": cost,
                "cost_coverage_pct": coverage,
            },
            "metadata": {"currency": "local currency (unspecified)"},
            "metric_metadata": METRIC_METADATA,
            "definitions": DEFINITIONS,
        },
    }


@pytest.fixture
def subject(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    base = context("base")
    first = payload(context("january", end="2024-01"), 147, 65488.29, 96.5986394557823)
    last = payload(context("june", start="2024-06"), 252, 123930.1, 97.22222222222221)
    entry = ledger.record(
        "analysis_compare",
        "# central comparison fixture",
        {
            "ok": True,
            "baseline": first,
            "comparison": last,
            "decomposition": {
                "event_count_change": 105,
                "rows": [
                    {"name": "36", "event_count_change": 4},
                    {"name": "7", "event_count_change": 4},
                ],
                "dimension": "product_id",
            },
        },
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    paths = {
        "first": "$.baseline.analysis.kpis.event_count",
        "last": "$.comparison.analysis.kpis.event_count",
        "cost": "$.baseline.analysis.kpis.known_repair_cost",
        "coverage": "$.baseline.analysis.kpis.cost_coverage_pct",
        "equalA": "$.decomposition.rows[0].event_count_change",
        "equalB": "$.decomposition.rows[1].event_count_change",
    }
    values = {item["path"]: item["value"] for item in entry["numeric_values"]}
    claims = {
        name: NumericClaim(
            id=name,
            execution_id=entry["execution_id"],
            path=path,
            value=values[path],
            code_sha256=entry["code_sha256"],
        )
        for name, path in paths.items()
    }
    return ledger, base, entry, claims


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.words = []
        self.manifest = None
        self.in_manifest = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        self.in_manifest = (
            tag == "script" and attrs.get("id") == "report-evidence" or self.in_manifest
        )

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_manifest = False

    def handle_data(self, text):
        if self.in_manifest:
            self.manifest = json.loads(text)
        else:
            self.words.append(text)


@pytest.mark.parametrize("language", ["en", "vi"])
def test_printable_comparison_keeps_both_source_scopes_versions_units_and_refs(subject, language):
    ledger, base, entry, claims = subject
    selected = [claims[name] for name in ["first", "last", "cost", "coverage"]]
    report = AnalystReport(
        report="147 [claim:first]; 252 [claim:last]; cost 65,488.29 [claim:cost]; coverage 96.5986 [claim:coverage].",
        claims=selected,
    )
    assert business_scope(ledger, report.claims) is None
    html = render_business_report(
        report.report, ledger, language=language, claims=report.claims, bound_context=base
    )
    document = Document(html)
    assert ("aside", {"class": "report-metadata"}) in document.tags
    assert document.manifest["locale"] == ("vi-VN" if language == "vi" else "en-US")
    assert document.manifest["instructions_version"] == INSTRUCTIONS_VERSION
    assert document.manifest["tools_version"] == TOOLS_VERSION
    contexts = {item["context_id"]: item for item in document.manifest["contexts"]}
    assert contexts["january"]["filters"]["end_month"] == "2024-01"
    assert contexts["june"]["filters"]["start_month"] == "2024-06"
    assert contexts["base"]["roles"] == ["bound"]
    assert all(
        item["dataset_version"] == "dataset-A" and item["policy_version"] == "canonical-v2"
        for item in contexts.values()
    )
    refs = document.manifest["references"]
    assert refs[0]["context_ids"] == ["january"] and refs[1]["context_ids"] == ["june"]
    assert {item["unit"]["unit"] for item in refs} == {
        "logged defect events",
        "local currency",
        "%",
    }
    targets = {attrs["id"] for _, attrs in document.tags if "id" in attrs}
    anchors = [attrs["href"] for _, attrs in document.tags if "href" in attrs]
    assert len(anchors) == 4 and all(target[1:] in targets for target in anchors)
    assert refs[0]["execution_id"] == entry["execution_id"]
    assert "[claim:" not in html


@pytest.mark.parametrize("language", ["en", "vi"])
def test_chat_unit_legend_preserves_model_prose_and_is_idempotent(subject, language):
    ledger, _, _, claims = subject
    prose = "Original prose: [claim:coverage] [claim:cost]"
    bound = [claims["coverage"].model_dump(), claims["cost"].model_dump()]
    shown = with_unit_note(prose, bound, ledger, language)
    assert shown.startswith(prose + "\n\n")
    assert "%" in shown
    assert "local currency" not in shown and "nội tệ" not in shown
    assert (
        "source does not specify the currency"
        if language == "en"
        else "nguồn không xác định loại tiền"
    ) in shown
    assert ("No currency conversion" if language == "en" else "Không quy đổi tiền tệ") in shown
    assert shown == with_unit_note(shown, bound, ledger, language)


@pytest.mark.parametrize(
    "sentence",
    [
        "Count 147% [claim:first].",
        "Cost USD 65,488.29 [claim:cost].",
        "Cost 65,488.29 VND [claim:cost].",
        "Cost 65,488.29 EUR [claim:cost].",
        "Coverage 96.5986 events [claim:coverage].",
        "Coverage 96.5986 percentage points [claim:coverage].",
        "Coverage 96.5986 pp [claim:coverage].",
        "Observed cost is 65,488.29 events [claim:cost].",
    ],
)
def test_explicit_wrong_unit_is_blocked(subject, sentence):
    ledger, _, _, claims = subject
    report = AnalystReport(report=sentence, claims=list(claims.values()))
    audit = audit_report(report, ledger)
    assert not audit["verified"]
    assert any("unit" in error or "currency" in error for error in audit["errors"])


@pytest.mark.parametrize(
    "separator,valid",
    [
        (" ", False),
        (" and ", True),
        (" và ", True),
        (" / ", True),
        ("; product_id 7 changed ", True),
    ],
)
def test_equal_values_need_an_explicit_relationship(subject, separator, valid):
    ledger, _, _, claims = subject
    report = AnalystReport(
        report="Product decomposition: Product ID 36 and Product ID 7 changed 4 [claim:equalA]"
        + separator
        + "4 [claim:equalB].",
        claims=[claims["equalA"], claims["equalB"]],
    )
    audit = audit_report(report, ledger)
    assert audit["verified"] is valid, audit
    if not valid:
        assert any("Adjacent numeric values" in error for error in audit["errors"])


def test_scenario_levels_and_deltas_have_distinct_units(subject):
    ledger, base, _, _ = subject
    before = payload(base, 10, 100, 80)
    after = payload(context("scenario", scenario="exclude_cost_10"), 10, 90, 70)
    entry = ledger.record(
        "scenario_compare",
        "# declared scenario",
        {
            "ok": True,
            "canonical": before,
            "selected": after,
            "comparison": {
                "metric_deltas": {
                    "cost_coverage_pct": {"canonical": 80, "selected": 70, "delta": -10}
                }
            },
        },
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    level = NumericClaim(
        id="level",
        execution_id=entry["execution_id"],
        path="$.comparison.metric_deltas.cost_coverage_pct.canonical",
        value=80,
        code_sha256=entry["code_sha256"],
    )
    delta = NumericClaim(
        id="delta",
        execution_id=entry["execution_id"],
        path="$.comparison.metric_deltas.cost_coverage_pct.delta",
        value=-10,
        code_sha256=entry["code_sha256"],
    )
    assert metric_unit(entry, level.path)["unit"] == "%"
    assert metric_unit(entry, delta.path)["unit"] == "percentage points"
    good = AnalystReport(
        report="Coverage 80% [claim:level]; change -10 percentage points [claim:delta].",
        claims=[level, delta],
    )
    assert audit_report(good, ledger)["verified"]
    wrong = good.model_copy(update={"report": "Change -10% [claim:delta]."})
    assert not audit_report(wrong, ledger)["verified"]
    manifest = evidence_manifest(
        ledger,
        [delta.model_dump()],
        language="en",
        instructions_version=INSTRUCTIONS_VERSION,
        tools_version=TOOLS_VERSION,
    )
    assert {value["scenario"] for value in manifest["contexts"]} == {"", "exclude_cost_10"}
    assert len(manifest["references"][0]["context_ids"]) == 2


def test_declared_reference_intensity_and_cleaning_grains_remain_distinct(subject):
    ledger, base, _, _ = subject
    entry = ledger.record(
        "analysis_query",
        "# source-declared units and grains",
        {
            "ok": True,
            "context": base,
            "definitions": DEFINITIONS,
            "metric_metadata": METRIC_METADATA,
            "analysis": {
                "kpis": {"reference_intensity_per_1000": 12},
                "cleaning_impact": {
                    "raw": {
                        "unit": "received source rows before duplicate resolution",
                        "row_count": 20,
                        "cost_unknown_count": 3,
                    },
                    "retained": {
                        "unit": "retained unique nonconflicting defect events",
                        "event_count": 15,
                    },
                    "row_count_difference": -5,
                    "raw_numeric_cost_including_negatives": 123,
                },
            },
        },
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    assert (
        metric_unit(entry, "$.analysis.kpis.reference_intensity_per_1000")["unit"]
        == "events per thousand reference output units"
    )
    assert (
        metric_unit(entry, "$.analysis.cleaning_impact.raw.row_count")["unit"]
        == "received source rows"
    )
    assert (
        metric_unit(entry, "$.analysis.cleaning_impact.raw.cost_unknown_count")["unit"]
        == "received source rows"
    )
    assert (
        metric_unit(entry, "$.analysis.cleaning_impact.retained.event_count")["unit"]
        == "logged defect events"
    )
    assert (
        metric_unit(entry, "$.analysis.cleaning_impact.row_count_difference")["unit"]
        == "retained-event count minus raw-row count"
    )
    assert (
        metric_unit(entry, "$.analysis.cleaning_impact.raw_numeric_cost_including_negatives")[
            "kind"
        ]
        == "cost_diagnostic"
    )


@pytest.mark.parametrize(
    "sentence,valid",
    [
        ("Product ID 81 leads the cost priority [claim:product].", True),
        ("Product ID 81 had 81 events [claim:product].", False),
        ("Product ID 7 leads the cost priority [claim:product].", False),
        ("The count is 81 [claim:product].", False),
    ],
)
def test_priority_scalar_identifier_does_not_supply_a_measurement(subject, sentence, valid):
    ledger, base, _, _ = subject
    entry = ledger.record(
        "scenario_compare",
        "# central declared priority signals",
        {
            "ok": True,
            "comparison": {
                "priority_signals": {"canonical": {"highest_known_cost_product_id": 81}}
            },
        },
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    claim = NumericClaim(
        id="product",
        execution_id=entry["execution_id"],
        path="$.comparison.priority_signals.canonical.highest_known_cost_product_id",
        value=81,
        code_sha256=entry["code_sha256"],
    )
    audit = audit_report(AnalystReport(report=sentence, claims=[claim]), ledger)
    assert audit["verified"] is valid, audit


@pytest.mark.parametrize(
    "sentence,valid",
    [
        (
            "Scenario assumption: repair costs equal to 10 [claim:criterion] are financially unavailable.",
            True,
        ),
        (
            "Giả định kịch bản: chi phí sửa chữa bằng 10 [claim:criterion] không được dùng để tính chi phí.",
            True,
        ),
        ("Observed repair costs equal to 10 [claim:criterion].", False),
        ("The scenario has 10 events [claim:criterion].", False),
        ("Observed cost is 10 [claim:criterion].", False),
        (
            "Scenario assumption: repair costs equal to 10 USD [claim:criterion] are withheld.",
            False,
        ),
    ],
)
def test_typed_policy_criterion_cannot_supply_observed_measurements(subject, sentence, valid):
    ledger, base, _, _ = subject
    entry = ledger.record(
        "scenario_compare",
        "# central typed scenario criterion",
        {
            "ok": True,
            "comparison": {
                "scenario": "exclude_cost_10",
                "scenario_definition": {
                    "parameters": {
                        "repair_cost_equal": 10,
                        "repair_cost_unit": "local currency",
                        "effect": "withhold_financial_availability",
                    }
                },
            },
        },
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    claim = NumericClaim(
        id="criterion",
        execution_id=entry["execution_id"],
        path="$.comparison.scenario_definition.parameters.repair_cost_equal",
        value=10,
        code_sha256=entry["code_sha256"],
    )
    audit = audit_report(AnalystReport(report=sentence, claims=[claim]), ledger)
    assert audit["verified"] is valid, audit


def test_scenario_identifier_requires_an_actual_successful_source(subject):
    ledger, base, _, _ = subject
    report = AnalystReport(
        report="Scenario exclude_cost_10 changes financial availability.", claims=[]
    )
    assert not audit_report(report, ledger)["verified"]
    ledger.record(
        "scenario_compare",
        "# actual scenario",
        {"ok": True, "comparison": {"scenario": "exclude_cost_10"}},
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    assert audit_report(report, ledger)["verified"]
    assert not audit_report(
        report.model_copy(
            update={"report": "Scenario exclude_cost_100 changes financial availability."}
        ),
        ledger,
    )["verified"]


@pytest.mark.asyncio
async def test_every_advertised_section_completes_with_its_actual_scope(evidence_subject):
    runtime, _, _, _, _, _, _ = evidence_subject
    requested = filters(
        start_month="2024-06", end_month="2024-06", product_id="63", scenario="exclude_cost_10"
    )
    for section in SECTIONS:
        result = json.loads(
            await runtime._call(
                "analysis_query", {"section": section, "filters": requested, "plan": None}
            )
        )
        assert result["status"] == "completed", (section, result)
        semantics = result["result"]["scope_semantics"]
        if section in {"quality", "cleaning_impact", "quality_scope"}:
            assert result["scope"]["start_month"] == "2024-01"
            assert result["scope"]["end_month"] == "2024-06"
            assert result["scope"]["product_id"] == "" and result["scope"]["scenario"] == ""
            assert semantics["kind"] == "canonical_full_snapshot"
            assert semantics["filters_applied"] is False and semantics["scenario_applied"] is False
            assert semantics["requested_filters"]["product_id"] == "63"
            assert semantics["requested_filters"]["scenario"] == "exclude_cost_10"
        else:
            assert (
                result["scope"]["product_id"] == "63"
                and result["scope"]["scenario"] == "exclude_cost_10"
            )
        if section == "inspection_methods":
            methods = result["result"]["analysis"]["inspection_methods"]
            assert methods["event_count"] == sum(row["event_count"] for row in methods["rows"])
            assert methods["unavailable_metrics"]


@pytest.mark.asyncio
async def test_actual_typed_criterion_and_priority_scalar_reach_model_view(evidence_subject):
    runtime, tools, _, _, _, _, _ = evidence_subject
    view = json.loads(
        await runtime._call("scenario_compare", {"scenario": "exclude_cost_10", "plan": None})
    )
    evidence = {value["path"]: value for value in view["numeric_evidence"]}
    criterion = evidence["$.comparison.scenario_definition.parameters.repair_cost_equal"]
    assert criterion["value"] == 10
    rows = canonical(view["citation_rows"])
    assert "policy_parameter" in rows and "[value:" + criterion["id"] + "]" in rows
    report = runtime.resolve_report(
        ReportDraft(
            report=f"Scenario assumption: repair costs equal to [value:{criterion['id']}] are financially unavailable."
        )
    )
    assert audit_report(report, tools.ledger)["verified"]
    identifier = evidence["$.comparison.priority_signals.canonical.highest_known_cost_product_id"]
    report = runtime.resolve_report(
        ReportDraft(
            report=f"Product ID {identifier['value']} leads the cost priority [claim:{identifier['id']}]."
        )
    )
    assert audit_report(report, tools.ledger)["verified"]
    report = runtime.resolve_report(
        ReportDraft(
            report=f"Product ID {identifier['value']} has [value:{identifier['id']}] events."
        )
    )
    assert not audit_report(report, tools.ledger)["verified"]


@pytest.mark.asyncio
async def test_derived_investigation_artifact_matches_saved_scope_before_and_after_review(
    evidence_subject,
):
    runtime, tools, store, sid, _, _, _ = evidence_subject
    target = filters(production_line="Line-1", severity="Critical")
    queried = json.loads(
        await runtime._call("analysis_query", {"section": "kpis", "filters": target, "plan": None})
    )
    number = next(
        value
        for value in queried["numeric_evidence"]
        if value["path"] == "$.analysis.kpis.event_count"
    )
    saved = json.loads(
        await runtime._call(
            "investigation_update",
            {
                "question": "Review this Critical scope",
                "summary": "Scope findings are ready for review.",
                "findings": [f"Recorded events [value:{number['id']}]."],
                "hypotheses": [],
                "priorities": [],
                "next_steps": ["Review the source records."],
                "filters": target,
            },
        )
    )
    assert saved["status"] == "completed", saved
    artifact = saved["result"]["artifact"]
    state = store.investigation(sid)
    assert artifact["context_id"] == state["context_id"]
    assert artifact["scope"] == state["scope"] and artifact["scope"]["severity"] == "Critical"
    assert artifact["export_locale"] == "en"
    pending = runtime.product_tools.pending_state()
    review_path = tools.ledger.run_dir / "internal" / "synthetic-review.json"
    review_path.parent.mkdir(exist_ok=True)
    review_path.write_text(
        canonical({"investigation_draft": {"state_sha256": pending["state_sha256"]}}),
        encoding="utf-8",
    )
    receipt = tools.ledger.record(
        "report_semantic_review",
        "# synthetic exact-content receipt; no model call",
        {
            "phase": "reviewed",
            "qa_verdict": {"verified": True},
            "runner_input_path": "internal/synthetic-review.json",
            "runner_input_sha256": sha256(review_path.read_bytes()),
        },
    )
    reviewed = await runtime.product_tools.review_investigation(receipt)
    reviewed_artifact = reviewed["reviewed_artifact"]
    current = store.investigation(sid)
    assert reviewed_artifact["context_id"] == current["context_id"] == artifact["context_id"]
    assert reviewed_artifact["scope"] == current["scope"] == artifact["scope"]
    assert (
        json.loads(
            tools.ledger.resolve_artifact(reviewed_artifact["path"]).read_text(encoding="utf-8")
        )["context_id"]
        == current["context_id"]
    )


@pytest.mark.parametrize("change", ["wrong_scope", "missing_unit", "missing_reference"])
def test_registered_but_changed_metadata_cannot_pass_exact_report_readback(subject, change):
    ledger, base, _, claims = subject
    report = AnalystReport(
        report="Coverage 96.5986% [claim:coverage].", claims=[claims["coverage"]]
    )
    original = render_business_report(
        report.report, ledger, claims=report.claims, bound_context=base
    )
    changed = (
        original.replace("2024-01", "2024-02")
        if change == "wrong_scope"
        else original.replace("%;", "unknown;")
        if change == "missing_unit"
        else original.replace('id="evidence-1"', 'id="missing-1"')
    )
    if change == "missing_unit":
        changed = original.replace("<dd>%</dd>", "<dd>unknown</dd>")
    assert changed != original
    target = ledger.run_dir / "artifacts" / "business_report.html"
    target.parent.mkdir(exist_ok=True)
    target.write_text(changed, encoding="utf-8")
    artifact = ledger.register_artifact(target, kind="business_report")
    audit = audit_exported_reports(report, ledger, artifacts=[artifact], bound_context=base)
    assert not audit["verified"] and "differs" in audit["errors"][0]


@pytest.mark.parametrize("tool", ["analysis_compare", "scenario_compare"])
@pytest.mark.parametrize("missing", [("baseline",), ("comparison",), ("baseline", "comparison")])
def test_comparison_missing_context_cannot_borrow_other_side_or_bound_context(
    subject, missing, tool
):
    ledger, base, entry, _ = subject
    malformed = copy.deepcopy(entry["result"])
    for side in missing:
        malformed[side].pop("context")
    claim_path = "$.baseline.analysis.kpis.event_count"
    if tool == "scenario_compare":
        malformed = {
            "ok": True,
            "canonical": malformed["baseline"],
            "selected": malformed["comparison"],
        }
        claim_path = "$.canonical.analysis.kpis.event_count"
    source = ledger.record(
        tool,
        "# malformed comparison fixture",
        malformed,
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    claim = NumericClaim(
        id="baseline",
        execution_id=source["execution_id"],
        path=claim_path,
        value=147,
        code_sha256=source["code_sha256"],
    )
    with pytest.raises(ValueError, match="explicit valid .* context"):
        render_business_report(
            "Baseline count 147 [claim:baseline].", ledger, claims=[claim], bound_context=base
        )


@pytest.mark.parametrize("language", ["en", "vi"])
def test_mixed_same_line_measurements_keep_each_local_unit(subject, language):
    ledger, _, _, claims = subject
    text = (
        "Cost 65,488.29 local currency [claim:cost] and 147 events [claim:first]; coverage 96.5986% [claim:coverage]."
        if language == "en"
        else "Chi phí 65.488,29 local currency [claim:cost] và 147 sự kiện [claim:first]; độ phủ 96,5986% [claim:coverage]."
    )
    report = AnalystReport(
        report=text,
        claims=[claims[name] for name in ["cost", "first", "coverage"]],
        number_format="en-US" if language == "en" else "vi-VN",
    )
    assert audit_report(report, ledger)["verified"]


def test_unsuccessful_source_never_enters_portable_manifest(subject):
    ledger, base, entry, _ = subject
    result = copy.deepcopy(entry["result"])
    result["ok"] = False
    source = ledger.record(
        "analysis_compare",
        "# failed comparison",
        result,
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    claim = NumericClaim(
        id="failed",
        execution_id=source["execution_id"],
        path="$.baseline.analysis.kpis.event_count",
        value=147,
        code_sha256=source["code_sha256"],
    )
    with pytest.raises(ValueError, match="missing or unsuccessful"):
        render_business_report("147 [claim:failed]", ledger, claims=[claim])


def test_registered_paths_are_identifiers_but_nearby_measurements_still_need_proof(subject):
    ledger, _, _, _ = subject
    target = ledger.run_dir / "artifacts" / "investigation-v2.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    ledger.register_artifact(target)
    assert audit_report(
        AnalystReport(report="Saved artifacts/investigation-v2.json", claims=[]), ledger
    )["verified"]
    assert audit_report(AnalystReport(report="Saved investigation-v2.json", claims=[]), ledger)[
        "verified"
    ]
    assert not audit_report(
        AnalystReport(report="Saved artifacts/investigation-v2.json with 999 events", claims=[]),
        ledger,
    )["verified"]
    assert not audit_report(
        AnalystReport(report="Saved artifacts/investigation-v20.json", claims=[]), ledger
    )["verified"]
    assert not audit_report(
        AnalystReport(report="Saved artifacts/investigation-v2.json.bak", claims=[]), ledger
    )["verified"]
    assert not audit_report(
        AnalystReport(report="Saved investigation-v2.json with 2 events", claims=[]), ledger
    )["verified"]


def test_chat_source_manifest_retains_actual_target_context_without_export(subject):
    ledger, base, _, _ = subject
    target = context("critical", severity="Critical")
    result = payload(target, 7, 40, 100)
    entry = ledger.record(
        "analysis_query",
        "# explicit Critical scope",
        {"ok": True, **result},
        execution_kind="bounded_analysis",
        analysis_context=target,
    )
    claim = NumericClaim(
        id="critical",
        execution_id=entry["execution_id"],
        path="$.analysis.kpis.event_count",
        value=7,
        code_sha256=entry["code_sha256"],
    )
    report = AnalystReport(report="Critical count 7 [claim:critical].", claims=[claim])

    async def emit(*args):
        pass

    tools = SimpleNamespace(run_dir=ledger.run_dir, ledger=ledger)
    runtime = AnalystRuntime(
        Settings(memory_enabled=False), tools, emit, analysis_context={"context": base}
    )
    packet, errors = runtime.begin_delivery_proposal(report)
    assert not errors and packet["artifacts"] == []
    assert packet["scope"]["severity"] == "Critical"
    assert packet["source_manifest"]["sources"][-1]["contexts"] == {"scope": "critical"}
    assert packet["source_manifest"]["locale"] == "en-US"
    assert canonical(packet["source_manifest"])
    assert not ledger.artifacts()
