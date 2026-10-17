"""Bound total/group provenance without provider construction or application servers."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import fields
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from analyst.agent_views import VIEW_BYTES
from analyst.case_study.analytics import DEFINITIONS, Scope
from analyst.case_study.preparation import build_snapshot
from analyst.config import Settings
from analyst.dashboard import AnalysisService
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.report_output import metric_unit
from analyst.runtime import (
    AnalystRuntime,
    ReportDraft,
    audit_exported_reports,
    audit_report,
    render_business_report,
    result_at_path,
)
from analyst.tools import AnalystTools

FINANCIAL = (
    "event_count",
    "known_repair_cost",
    "cost_known_count",
    "cost_unknown_count",
    "cost_coverage_pct",
    "cost_available",
    "cost_status",
)


def filters(**changes):
    return {**{field.name: None for field in fields(Scope)}, **changes}


class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.visible = []

    def handle_starttag(self, tag, _attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden -= 1

    def handle_data(self, text):
        if not self.hidden:
            self.visible.append(text)


@pytest_asyncio.fixture
async def subject(tmp_path, monkeypatch, request):
    def deny_provider(*_args, **_kwargs):
        raise AssertionError("Cost-grain checks cannot construct a provider")

    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr("analyst.model.AsyncOpenAI", deny_provider)
    monkeypatch.setattr("analyst.runtime.Runner.run", deny_provider)
    rows = [
        {
            "defect_id": index,
            "product_id": product_id,
            "defect_type": "Functional",
            "defect_date": "2024-01-15",
            "defect_location": location,
            "severity": severity,
            "inspection_method": "Visual Inspection",
            "repair_cost": cost,
        }
        for index, (product_id, location, severity, cost) in enumerate(
            [
                (1, "Surface", "Critical", 50),
                (1, "Surface", "Minor", None),
                (1, "Component", "Critical", 90),
                (1, "Component", "Critical", 10),
                (1, "Internal", "Minor", 40),
                (3, "Surface", "Critical", 1000),
            ],
            1,
        )
    ]
    master = (
        b"product_id,product_name,category,production_line,monthly_output\n"
        b"1,Synthetic observed,Category A,Line-1,100\n"
        b"2,Synthetic empty,Category A,Line-1,100\n"
        b"3,Synthetic other line,Category A,Line-2,100\n"
    )

    def loader():
        return build_snapshot(
            rows,
            {
                "source_url": "https://synthetic.invalid/cost-grain",
                "source_sha256": sha256(canonical(rows)),
                "fetched_at": "2026-10-16T00:00:00Z",
            },
            master,
        )

    root = Path(__file__).resolve().parents[1]
    analysis = AnalysisService(root, tmp_path / "analysis", snapshot_loader=loader)
    bound = await analysis.get_context(filters={"production_line": "Line-1"})
    tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)

    async def emit(_kind, _data):
        pass

    runtime = AnalystRuntime(
        Settings(mode="live", api_key="offline-invalid-key", memory_enabled=False),
        tools,
        emit,
        analysis_context=bound,
        analysis_service=analysis,
        language=getattr(request, "param", "en"),
    )
    try:
        yield SimpleNamespace(runtime=runtime, tools=tools, bound=bound)
    finally:
        await tools.close()


async def costs(subject, **changes):
    raw = await subject.runtime._call(
        "analysis_query", {"section": "costs", "filters": filters(**changes), "plan": None}
    )
    assert len(raw.encode("utf-8")) <= VIEW_BYTES
    view = json.loads(raw)
    assert view["status"] == "completed", view
    entry = next(
        item
        for item in subject.tools.ledger.records()
        if item["execution_id"] == view["receipt_id"]
    )
    for item in view["numeric_evidence"]:
        assert result_at_path(entry["result"], item["path"]) == item["value"]
        assert subject.tools.ledger.verify_claims(
            [subject.runtime.claim_catalog[item["id"]].model_dump()]
        )["verified"]
    return view, entry


def marker(view, path):
    return (
        "[value:"
        + next(item["id"] for item in view["numeric_evidence"] if item["path"] == path)
        + "]"
    )


@pytest.mark.parametrize("changes", [{}, {"severity": "Critical"}, {"product_id": "2"}])
@pytest.mark.asyncio
async def test_cost_totals_are_exact_target_payload_not_base_or_group(subject, changes):
    target = await subject.runtime.read_dashboard_context(filters(**changes))
    original = deepcopy(target["analysis"])
    view, entry = await costs(subject, **changes)
    expected = {key: original["kpis"][key] for key in FINANCIAL}
    assert entry["result"]["analysis"] == {"kpis": expected, "costs": original["costs"]}
    assert view["result"]["analysis"]["kpis"] == expected
    assert view["result"]["analysis"]["costs"] == original["costs"]
    assert entry["analysis_context"] == target["context"]
    assert view["scope"] == target["context"]["filters"]
    assert view["result"]["scope_semantics"]["scope_total_path"] == "$.analysis.kpis"
    assert view["result"]["scope_semantics"]["grouped_detail_path"] == "$.analysis.costs"
    assert target["analysis"] == original
    if changes.get("severity"):
        assert expected["known_repair_cost"] == 150
        assert subject.bound["analysis"]["kpis"]["known_repair_cost"] == 190
        assert target["context"]["context_id"] != subject.bound["context"]["context_id"]
    if not changes:
        assert expected["known_repair_cost"] == 190
        assert expected["event_count"] == 5
        assert expected["cost_coverage_pct"] == 80
        assert original["costs"]["by_location"][0]["known_repair_cost"] == 100
        assert view["citation_rows"][0]["path"] == "$.analysis.kpis"


@pytest.mark.asyncio
async def test_each_qa_quantity_keeps_its_group_labels_and_original_parent(subject):
    view, entry = await costs(subject)
    row = next(
        index
        for index, item in enumerate(entry["result"]["analysis"]["costs"]["by_location"])
        if item["name"] == "Surface"
    )
    row_path = f"$.analysis.costs.by_location[{row}]"
    report = subject.runtime.resolve_report(
        ReportDraft(
            report="Surface known cost is "
            + marker(view, row_path + ".known_repair_cost")
            + " local currency; selected-scope known cost is "
            + marker(view, "$.analysis.kpis.known_repair_cost")
            + " local currency."
        )
    )
    assert audit_report(report, subject.tools.ledger)["verified"]
    support = subject.runtime.qa_evidence(report)[0]["numeric_support"]
    group, total = support
    assert group["labels"] == {"name": "Surface"}
    assert group["source_parent_path"] == row_path
    assert group["execution_id"] == entry["execution_id"]
    assert group["scope"]["defect_location"] == ""
    assert total["labels"] == {}
    assert total["source_parent_path"] == "$.analysis.kpis"
    assert total["value"] != group["value"]
    # Numeric binding alone does not decide whether prose uses the right population.
    wrong = report.model_copy(update={"report": report.report.replace("Surface", "The whole line")})
    wrong_support = subject.runtime.qa_evidence(wrong)[0]["numeric_support"]
    assert wrong_support[0]["labels"] == {"name": "Surface"}
    contexts = {
        item["id"]: item for item in subject.runtime.qa_evidence(wrong)[0]["ancestor_contexts"]
    }
    assert any(
        contexts[identity]["path"] == row_path and contexts[identity]["labels"]["name"] == "Surface"
        for identity in wrong_support[0]["ancestor_context_ids"]
    )


@pytest.mark.asyncio
async def test_critical_group_remains_valid_under_a_critical_target(subject):
    view, entry = await costs(subject, severity="Critical")
    path = "$.analysis.costs.by_severity[0].known_repair_cost"
    report = subject.runtime.resolve_report(
        ReportDraft(report="Critical known cost is " + marker(view, path) + " local currency.")
    )
    item = subject.runtime.qa_evidence(report)[0]["numeric_support"][0]
    assert item["labels"] == {"name": "Critical"}
    assert item["scope"]["severity"] == "Critical"
    assert item["source_parent_path"] == "$.analysis.costs.by_severity[0]"
    assert item["value"] == entry["result"]["analysis"]["kpis"]["known_repair_cost"]


@pytest.mark.parametrize("subject,language", [("en", "en"), ("vi", "vi")], indirect=["subject"])
@pytest.mark.asyncio
async def test_portable_appendix_shows_exact_group_label_and_rejects_tamper(subject, language):
    view, entry = await costs(subject)
    path = "$.analysis.costs.by_location[0].known_repair_cost"
    label = entry["result"]["analysis"]["costs"]["by_location"][0]["name"]
    report = subject.runtime.resolve_report(
        ReportDraft(report="Observed repair cost: " + marker(view, path) + " local currency.")
    )
    html = render_business_report(
        report.report,
        subject.tools.ledger,
        language=language,
        claims=report.claims,
        bound_context=subject.bound["context"],
    )
    parser = VisibleHTML()
    parser.feed(html)
    assert "name: " + label in " ".join(parser.visible)
    changed = html.replace("name: " + label, "name: Different group", 1)
    assert changed != html
    target = subject.tools.run_dir / "artifacts" / "business_report.html"
    target.parent.mkdir(exist_ok=True)
    target.write_text(changed, encoding="utf-8")
    artifact = subject.tools.ledger.register_artifact(target, kind="business_report")
    audit = audit_exported_reports(
        report,
        subject.tools.ledger,
        language=language,
        artifacts=[artifact],
        bound_context=subject.bound["context"],
    )
    assert not audit["verified"] and "differs" in audit["errors"][0]


@pytest.mark.asyncio
async def test_source_label_is_escaped_without_changing_the_source_or_claim(subject):
    _view, entry = await costs(subject)
    result = deepcopy(entry["result"])
    label = "<script>alert('label')</script> & named group"
    result["analysis"]["costs"]["by_location"][0]["name"] = label
    isolated = subject.tools.ledger.record(
        "analysis_query",
        "# synthetic escaped-label source",
        result,
        analysis_context=entry["analysis_context"],
        execution_kind="bounded_analysis",
    )
    view = subject.runtime.model_receipt(isolated)
    report = subject.runtime.resolve_report(
        ReportDraft(
            report="Known cost "
            + marker(view, "$.analysis.costs.by_location[0].known_repair_cost")
            + " local currency."
        )
    )
    html = render_business_report(report.report, subject.tools.ledger, claims=report.claims)
    assert "&lt;script&gt;alert(&#x27;label&#x27;)&lt;/script&gt; &amp; named group" in html
    assert "<script>alert('label')</script>" not in html
    parser = VisibleHTML()
    parser.feed(html)
    assert label in " ".join(parser.visible)
    assert subject.runtime.qa_evidence(report)[0]["numeric_support"][0]["labels"] == {"name": label}


@pytest.mark.asyncio
async def test_unmarked_group_counts_still_fail_evidence_audit(subject):
    view, _entry = await costs(subject)
    report = subject.runtime.resolve_report(
        ReportDraft(
            report="Known cost "
            + marker(view, "$.analysis.kpis.known_repair_cost")
            + " local currency; 999 logged events."
        )
    )
    assert not audit_report(report, subject.tools.ledger)["verified"]


@pytest.mark.parametrize(
    "metric", ["linked_event_count", "unmatched_event_count", "eligible_event_count"]
)
@pytest.mark.asyncio
async def test_count_unit_uses_its_actual_declared_population_in_qa(subject, metric):
    query = json.loads(
        await subject.runtime._call(
            "analysis_query", {"section": "kpis", "filters": filters(), "plan": None}
        )
    )
    path = "$.analysis.kpis." + metric
    report = subject.runtime.resolve_report(
        ReportDraft(report="Observed logged events: " + marker(query, path) + ".")
    )
    item = subject.runtime.qa_evidence(report)[0]["numeric_support"][0]
    assert item["unit"]["definition"] == DEFINITIONS[metric]
    assert item["unit"]["definition"] != DEFINITIONS["event_count"]
    assert item["unit"]["unit"] == "logged defect events"
    assert item["value"] == subject.bound["analysis"]["kpis"][metric]


@pytest.mark.parametrize("metric", ["critical_count", "cost_known_count", "cost_unknown_count"])
def test_missing_metric_specific_definition_keeps_existing_legacy_fallback(tmp_path, metric):
    assert metric not in DEFINITIONS
    ledger = EvidenceLedger(tmp_path)
    entry = ledger.record(
        "analysis_query",
        "# legacy count-definition source",
        {"ok": True, "analysis": {"kpis": {metric: 2}}, "definitions": DEFINITIONS},
        execution_kind="bounded_analysis",
    )
    descriptor = metric_unit(entry, "$.analysis.kpis." + metric)
    assert descriptor is not None
    assert descriptor["definition"] == DEFINITIONS["event_count"]
    assert descriptor["unit"] == "logged defect events"


@pytest.mark.parametrize("unusable", [None, "", "   ", ["not a text definition"]])
def test_nontext_or_empty_definition_cannot_replace_the_existing_source_fallback(
    tmp_path, unusable
):
    ledger = EvidenceLedger(tmp_path)
    entry = ledger.record(
        "analysis_query",
        "# incomplete legacy count-definition source",
        {
            "ok": True,
            "analysis": {"kpis": {"linked_event_count": 2}},
            "definitions": {
                "event_count": DEFINITIONS["event_count"],
                "linked_event_count": unusable,
            },
        },
        execution_kind="bounded_analysis",
    )
    descriptor = metric_unit(entry, "$.analysis.kpis.linked_event_count")
    assert descriptor is not None
    assert descriptor["definition"] == DEFINITIONS["event_count"]
