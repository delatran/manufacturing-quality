"""Independent evidence integrity counterexamples over sealed synthetic sources.

These tests never dispatch a model request. Product integration cases are added
beside the focused metadata and unit boundaries, without application writes.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from analyst.case_study.analytics import DEFINITIONS, METRIC_METADATA, Scope
from analyst.case_study.preparation import build_snapshot
from analyst.dashboard import AnalysisService
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.product_tools import INSTRUCTIONS_VERSION, TOOLS_VERSION
from analyst.report_output import evidence_manifest, with_unit_note
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    NumericClaim,
    ReportDraft,
    audit_exported_reports,
    audit_report,
    render_business_report,
)
from analyst.tools import AnalystTools
from test_agent_review_independent import ROOT, event, filters, no_events, settings


def binding(identity: str, month: str = "2024-01", scenario: str = "") -> dict:
    return {
        "context_id": identity,
        "snapshot_id": "synthetic-review-snapshot",
        "dataset_version": "synthetic-review-dataset",
        "policy_version": "canonical-v2",
        "source_sha256": "synthetic-source",
        "master_sha256": "synthetic-master",
        "code_sha256": "synthetic-engine",
        "kpi_version": "synthetic-kpis",
        "scenario": scenario,
        "filters": asdict(Scope(start_month=month, end_month=month, scenario=scenario)),
    }


def payload(context: dict, count: int, cost: float, coverage: float) -> dict:
    return {
        "context": context,
        "analysis": {
            "scope": context["filters"],
            "kpis": {
                "event_count": count,
                "known_repair_cost": cost,
                "cost_coverage_pct": coverage,
            },
            "definitions": DEFINITIONS,
            "metric_metadata": METRIC_METADATA,
            "metadata": {"currency": "local currency (unspecified)"},
        },
    }


def claim_for(entry: dict, path: str, identity: str) -> NumericClaim:
    value = next(value["value"] for value in entry["numeric_values"] if value["path"] == path)
    return NumericClaim(
        id=identity,
        execution_id=entry["execution_id"],
        path=path,
        value=value,
        code_sha256=entry["code_sha256"],
    )


@pytest.fixture
def portable(tmp_path: Path):
    ledger = EvidenceLedger(tmp_path)
    base = binding("synthetic-base")
    before = payload(binding("synthetic-january"), 2, 10.0, 80.0)
    after = payload(binding("synthetic-june", "2024-06"), 3, 20.0, 50.0)
    entry = ledger.record(
        "analysis_compare",
        "# Independent synthetic comparison, not case-study observations",
        {"ok": True, "baseline": before, "comparison": after},
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    claims = {
        name: claim_for(entry, "$.baseline.analysis.kpis." + field, name)
        for name, field in (
            ("count", "event_count"),
            ("cost", "known_repair_cost"),
            ("coverage", "cost_coverage_pct"),
        )
    }
    return ledger, base, entry, claims


@pytest.mark.parametrize(
    "sentence,valid",
    [
        ("Coverage 80 pp [claim:coverage].", False),
        ("Coverage 80 percentage points [claim:coverage].", False),
        ("Observed cost 10 events [claim:cost].", False),
        ("Coverage 80% [claim:coverage].", True),
        ("Observed cost 10 local currency [claim:cost].", True),
        ("Count 2 events [claim:count]; observed cost 10 local currency [claim:cost].", True),
    ],
)
def test_explicit_unit_kind_not_changed_by_abbreviation_or_count_words(portable, sentence, valid):
    ledger, _, _, claims = portable
    audit = audit_report(AnalystReport(report=sentence, claims=list(claims.values())), ledger)
    assert audit["verified"] is valid, audit


@pytest.mark.parametrize(
    "tool,missing",
    [
        (tool, side)
        for tool in ("analysis_compare", "scenario_compare")
        for side in ("first", "second", "both")
    ],
)
def test_comparative_manifest_never_relabels_missing_source_context(portable, tool, missing):
    ledger, base, original, _ = portable
    sides = ("baseline", "comparison") if tool == "analysis_compare" else ("canonical", "selected")
    before = deepcopy(original["result"]["baseline"])
    after = deepcopy(original["result"]["comparison"])
    if missing in {"first", "both"}:
        before.pop("context")
    if missing in {"second", "both"}:
        after.pop("context")
    entry = ledger.record(
        tool,
        "# Independent incomplete-source negative fixture",
        {"ok": True, sides[0]: before, sides[1]: after},
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    claim = claim_for(entry, f"$.{sides[0]}.analysis.kpis.event_count", "before")
    with pytest.raises(ValueError):
        evidence_manifest(
            ledger,
            [claim.model_dump()],
            language="en",
            instructions_version=INSTRUCTIONS_VERSION,
            tools_version=TOOLS_VERSION,
            bound_context=base,
        )


@pytest.mark.parametrize("suffix,valid", [("pp", True), ("percentage points", True), ("%", False)])
def test_percentage_point_delta_retains_its_own_unit(portable, suffix, valid):
    ledger, base, original, _ = portable
    entry = ledger.record(
        "scenario_compare",
        "# Independent synthetic percentage-level difference",
        {
            "ok": True,
            "canonical": original["result"]["baseline"],
            "selected": original["result"]["comparison"],
            "comparison": {
                "metric_deltas": {
                    "cost_coverage_pct": {"canonical": 80, "selected": 50, "delta": -30}
                }
            },
        },
        execution_kind="bounded_analysis",
        analysis_context=base,
    )
    claim = claim_for(entry, "$.comparison.metric_deltas.cost_coverage_pct.delta", "change")
    result = audit_report(
        AnalystReport(report=f"Coverage changed -30 {suffix} [claim:change].", claims=[claim]),
        ledger,
    )
    assert result["verified"] is valid, result


@pytest_asyncio.fixture
async def actual(tmp_path, monkeypatch, request):
    def deny_provider(*_args, **_kwargs):
        raise AssertionError("Independent v5 probes must never construct an API client")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", deny_provider)
    rows = [
        event(1, product_id=81),
        event(2, product_id=81, defect_date="2024-06-15", repair_cost=100),
        event(3, product_id=82, defect_date="2024-06-15", severity="Minor", repair_cost=None),
    ]
    master = (
        b"product_id,product_name,category,production_line,monthly_output\n"
        b"81,Target,Category 1,Line-3,100\n"
        b"82,Peer,Category 1,Line-3,200\n"
        b"83,Zero event,Category 1,Line-3,50\n"
    )

    def loader():
        return build_snapshot(
            rows,
            {
                "source_url": "https://synthetic.invalid/v5-review",
                "source_sha256": sha256(canonical(rows)),
                "fetched_at": "2026-10-16T00:00:00Z",
            },
            master,
        )

    analysis = AnalysisService(ROOT, tmp_path / "analysis", snapshot_loader=loader)
    bound = await analysis.get_context({"production_line": "Line-3"})
    tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)
    runtime = AnalystRuntime(
        settings(tmp_path),
        tools,
        no_events,
        analysis_context=bound,
        analysis_service=analysis,
        language=(
            request.node.callspec.params.get("language", "en")
            if hasattr(request.node, "callspec")
            else "en"
        ),
    )
    try:
        yield SimpleNamespace(runtime=runtime, tools=tools, analysis=analysis, bound=bound)
    finally:
        await tools.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,language,valid",
    [
        ("product_label", "en", True),
        ("product_measurement", "en", False),
        ("wrong_product", "en", False),
        ("criterion", "en", True),
        ("criterion", "vi", True),
        ("criterion_measurement", "en", False),
        ("criterion_count", "en", False),
        ("uncited_criterion", "en", False),
    ],
)
async def test_actual_product_tool_roles_do_not_become_measurements(actual, kind, language, valid):
    runtime = actual.runtime
    response = json.loads(
        await runtime._call("scenario_compare", {"scenario": "exclude_cost_10", "plan": None})
    )
    assert response["status"] == "completed"
    evidence = {value["path"]: value for value in response["numeric_evidence"]}
    identity = evidence["$.comparison.priority_signals.canonical.highest_known_cost_product_id"]
    criterion = evidence["$.comparison.scenario_definition.parameters.repair_cost_equal"]
    assert identity["value"] == 81 and criterion["value"] == 10
    scalar = "[value:" + identity["id"] + "]"
    threshold = "[value:" + criterion["id"] + "]"
    text = {
        "product_label": "Product ID " + scalar + " leads observed cost.",
        "product_measurement": "Product ID " + scalar + " has " + scalar + " events.",
        "wrong_product": "Product ID 82 leads observed cost [claim:" + identity["id"] + "].",
        "criterion": (
            "Giả định kịch bản: chi phí sửa chữa bằng "
            + threshold
            + " không được dùng để tính chi phí."
            if language == "vi"
            else "Scenario assumption: repair costs equal to "
            + threshold
            + " are financially unavailable."
        ),
        "criterion_measurement": "Observed repair cost is " + threshold + ".",
        "criterion_count": "The scenario has " + threshold + " events.",
        "uncited_criterion": "Scenario assumption: repair costs equal to 10 are financially unavailable.",
    }[kind]
    report = runtime.resolve_report(ReportDraft(report=text))
    checked = audit_report(report, actual.tools.ledger)
    assert checked["verified"] is valid, checked
    assert actual.tools.ledger.verify_integrity() == []


@pytest.mark.parametrize(
    "variant,valid",
    [
        ("registered", True),
        ("registered_bare", True),
        ("measurement", False),
        ("different_version", False),
        ("extra_extension", False),
        ("changed_bytes", False),
    ],
)
def test_registered_version_is_metadata_without_proving_a_nearby_count(portable, variant, valid):
    ledger, _, _, _ = portable
    target = ledger.run_dir / "artifacts" / "review-notes-v2.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    ledger.register_artifact(target)
    text = "Saved artifacts/review-notes-v2.json."
    if variant == "registered_bare":
        text = "Saved artifacts/review-notes-v2.json"
    elif variant == "measurement":
        text += " There were 2 events."
    elif variant == "different_version":
        text = "Saved artifacts/review-notes-v20.json."
    elif variant == "extra_extension":
        text = "Saved artifacts/review-notes-v2.json.bak"
    elif variant == "changed_bytes":
        target.write_text('{"changed":true}', encoding="utf-8")
    checked = audit_report(AnalystReport(report=text, claims=[]), ledger)
    assert checked["verified"] is valid, checked


class EvidenceDOM(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: set[str] = set()
        self.hrefs: list[str] = []
        self.capture = False
        self.manifest_json = ""

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "a" and values.get("href"):
            self.hrefs.append(values["href"])
        if tag == "script" and values.get("id") == "report-evidence":
            assert values.get("type") == "application/json"
            self.capture = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.manifest_json += data


@pytest.mark.parametrize("language", ["en", "vi"])
def test_portable_html_has_resolvable_refs_and_separate_period_contexts(portable, language):
    ledger, base, entry, claims = portable
    after = claim_for(entry, "$.comparison.analysis.kpis.event_count", "after")
    report = AnalystReport(
        report="Before 2 [claim:count]; after 3 [claim:after]. Cost 10 [claim:cost]. Coverage 80 [claim:coverage].",
        claims=[*claims.values(), after],
    )
    body = with_unit_note(
        report.report, [value.model_dump() for value in report.claims], ledger, language
    )
    assert body.startswith(report.report)
    html = render_business_report(
        body, ledger, language=language, claims=report.claims, bound_context=base
    )
    dom = EvidenceDOM()
    dom.feed(html)
    manifest = json.loads(dom.manifest_json)
    refs = {value["id"]: value for value in manifest["references"]}
    assert refs["count"]["context_ids"] == ["synthetic-january"]
    assert refs["after"]["context_ids"] == ["synthetic-june"]
    assert all(
        identity != "synthetic-base" for value in refs.values() for identity in value["context_ids"]
    )
    assert all(href[1:] in dom.ids for href in dom.hrefs if href.startswith("#"))
    assert len([href for href in dom.hrefs if href.startswith("#evidence-")]) == 4
    assert manifest["instructions_version"] == INSTRUCTIONS_VERSION
    assert manifest["tools_version"] == TOOLS_VERSION
    assert {value["unit"]["unit"] for value in refs.values()} == {
        "logged defect events",
        "local currency",
        "%",
    }
    assert all(
        value["dataset_version"] == "synthetic-review-dataset" for value in manifest["contexts"]
    )
    assert all(value["code_sha256"] == entry["code_sha256"] for value in refs.values())
    path = ledger.run_dir / "artifacts" / "business_report.html"
    path.parent.mkdir(exist_ok=True)
    path.write_text(html.replace("synthetic-june", "synthetic-january"), encoding="utf-8")
    artifact = ledger.register_artifact(path, kind="business_report")
    assert not audit_exported_reports(
        report, ledger, language=language, artifacts=[artifact], bound_context=base
    )["verified"]


@pytest.mark.asyncio
async def test_every_runtime_advertised_section_has_honest_scope_and_real_result(actual):
    tool = next(tool for tool in actual.runtime.sdk_tools() if tool.name == "analysis_query")
    sections = tool.params_json_schema["properties"]["section"]["enum"]
    requested = filters(
        product_id="81", start_month="2024-06", end_month="2024-06", scenario="exclude_cost_10"
    )
    for section in sections:
        response = json.loads(
            await actual.runtime._call(
                "analysis_query", {"section": section, "filters": requested, "plan": None}
            )
        )
        assert response["status"] == "completed", (section, response)
        semantics = response["result"]["scope_semantics"]
        if section in {"quality", "cleaning_impact", "quality_scope"}:
            assert semantics["kind"] == "canonical_full_snapshot"
            assert semantics["filters_applied"] is False and semantics["scenario_applied"] is False
            assert response["scope"]["product_id"] == response["scope"]["scenario"] == ""
            assert semantics["requested_filters"]["product_id"] == "81"
        else:
            assert response["scope"]["product_id"] == "81"
            assert response["scope"]["start_month"] == response["scope"]["end_month"] == "2024-06"
            assert response["scope"]["scenario"] == "exclude_cost_10"
        assert response["dataset_version"] == actual.bound["context"]["dataset_version"]
        assert response["policy_version"] == "canonical-v2"
        if section == "inspection_methods":
            methods = response["result"]["analysis"][section]
            assert (
                methods["event_count"]
                == 1
                == sum(value["event_count"] for value in methods["rows"])
            )
            assert methods["unavailable_metrics"]
    assert actual.tools.ledger.verify_integrity() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "vi"])
async def test_reference_intensity_legend_preserves_static_denominator(actual, language):
    runtime = actual.runtime
    response = json.loads(
        await runtime._call(
            "analysis_query", {"section": "kpis", "filters": filters(), "plan": None}
        )
    )
    value = next(
        value
        for value in response["numeric_evidence"]
        if value["path"] == "$.analysis.kpis.reference_intensity_per_1000"
    )
    report = runtime.resolve_report(
        ReportDraft(report="Reference intensity [value:" + value["id"] + ":4dp].")
    )
    noted = with_unit_note(
        report.report,
        [value.model_dump() for value in report.claims],
        actual.tools.ledger,
        language,
    )
    assert noted.startswith(report.report)
    assert (
        "static reference output is not verified actual production"
        if language == "en"
        else "chưa được xác minh là sản lượng thực tế"
    ) in noted
    wrong = runtime.resolve_report(
        ReportDraft(report="Reference intensity [value:" + value["id"] + ":4dp]% .")
    )
    assert not audit_report(wrong, actual.tools.ledger)["verified"]
