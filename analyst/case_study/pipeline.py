"""Run the complete finite manufacturing analysis with explicit evidence."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from .analytics import (
    Scope,
    analysis_report,
    filter_events,
    filter_products,
    kpis,
    validate_filters,
)
from .config import MASTER_PATH, ROOT, SOURCE_URL
from .ingestion import fetch_events
from .models import Snapshot
from .preparation import build_snapshot
from .sensitivity import EXPECTED_MASTER_SHA256, EXPECTED_SOURCE_SHA256, evaluate_date_sensitivity
from .workbook import generate_workbook


def code_identity() -> dict[str, str]:
    """Identify every executable case module and its declarative knowledge."""
    paths = sorted(Path(__file__).parent.glob("*.py")) + [
        ROOT / "knowledge/kpi_registry.yaml",
        ROOT / "knowledge/independent_oracle.json",
    ]
    return {
        str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
            p.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for p in paths
    }


def _numeric_leaves(value: Any, path: str = "$") -> list[tuple[str, int | float]]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError(f"Nonfinite numeric result at {path}")
        return [(path, value)]
    if isinstance(value, dict):
        return [
            item
            for key, child in value.items()
            for item in _numeric_leaves(child, path + "." + key)
        ]
    if isinstance(value, (list, tuple)):
        return [
            item
            for index, child in enumerate(value)
            for item in _numeric_leaves(child, f"{path}[{index}]")
        ]
    return []


def _qa(snapshot: Snapshot, analysis: dict[str, Any]) -> dict[str, Any]:
    """Check pinned independent answers without regenerating the oracle."""
    oracle = json.loads((ROOT / "knowledge/independent_oracle.json").read_text(encoding="utf-8"))
    checks = []
    for case in oracle["cases"]:
        scope = Scope(**case["filters"])
        try:
            validate_filters(snapshot, scope)
        except ValueError:
            if case["expected_status"] == 200:
                raise
            checks.append({"id": case["id"], "passed": True, "kind": "invalid_scope_rejected"})
            continue
        if case["expected_status"] != 200:
            raise ValueError(f"Expected invalid scope accepted: {case['id']}")
        rows = filter_events(snapshot, scope)
        actual = kpis(rows, filter_products(snapshot, scope), len(scope.months()))
        for field, expected in case["expected"].items():
            if field == "defect_ids_sha256":
                observed = hashlib.sha256(
                    ",".join(
                        str(x.defect_id) for x in sorted(rows, key=lambda x: x.defect_id)
                    ).encode("ascii")
                ).hexdigest()
                passed = observed == expected
            elif field == "known_repair_cost":
                passed = Decimal(str(actual[field])) == Decimal(expected)
            elif isinstance(expected, float):
                passed = math.isclose(actual[field], expected, rel_tol=1e-12, abs_tol=1e-12)
            else:
                passed = actual[field] == expected
            if not passed:
                raise ValueError(f"Independent oracle failed: {case['id']}.{field}")
        checks.append({"id": case["id"], "passed": True, "kind": "independent_numeric_oracle"})
    return {
        "status": "verified",
        "scope": "Historical case source hashes and independent numeric oracle; not model quality or plant truth",
        "checks": checks,
    }


def _render_report(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Every analytical numeric token gets its own tool-result claim path."""
    claims = {}

    def n(path: str, value: int | float, fmt: str = ",.2f") -> str:
        if isinstance(value, bool):
            raise ValueError("Boolean verification flags are not numeric measurements")
        claim_id = path
        claims[claim_id] = {
            "id": claim_id,
            "path": "$." + path,
            "value": value,
            "code_sha256": result["code_sha256"],
        }
        return f"{format(value, fmt)} [claim:{claim_id}]"

    a = result["analysis"]
    k = a["kpis"]
    t = a["trend"]
    lines = [
        "# Manufacturing quality analysis",
        "",
        "Historical review for January through June of year twenty twenty-four. The findings identify where logged defect burden and observed repair costs warrant investigation, with data limitations carried into each decision.",
        "",
        "## Scope and data quality",
        "",
        f"The period contains {n('analysis.kpis.event_count', k['event_count'], ',d')} logged defect events, including {n('analysis.kpis.critical_count', k['critical_count'], ',d')} Critical events. Observed repair cost is {n('analysis.kpis.known_repair_cost', k['known_repair_cost'])} in unspecified local currency, with {n('analysis.kpis.cost_coverage_pct', k['cost_coverage_pct'])}% cost coverage.",
        "Counts refer to logged events, not distinct defective units. Unknown costs remain unknown. Actual production and inspection exposure, unit identifiers, reporting completeness and causal attribution are unavailable.",
        "",
        "| Disposition | Count |",
        "|---|---:|",
    ]
    disposition_labels = {
        "raw_rows": "Source rows received",
        "duplicate_copies_removed": "Duplicate copies removed",
        "quarantined_rows": "Conflicting or invalid rows set aside",
        "retained_clean_events": "Clean events retained",
        "out_of_scope_events": "Events outside the study period",
        "analysis_events": "Events included in the analysis",
        "raw_equals_removed_plus_quarantined_plus_clean": "All source rows accounted for",
    }
    for key, value in a["quality"]["reconciliation"].items():
        label = disposition_labels[key]
        if isinstance(value, bool):
            lines.append(f"| {label} | {'Verified' if value else 'Failed'} |")
        else:
            lines.append(
                f"| {label} | {n('analysis.quality.reconciliation.' + key, value, ',d')} |"
            )
    lines += [
        "",
        "The quality workbook records the issues found, handling decisions, assumptions and their effect on results. Issue counts overlap and must not be added to infer removals.",
        "",
        "## Monthly trend",
        "",
        f"Logged events changed from {n('analysis.trend.first_count', t['first_count'], ',d')} in January to {n('analysis.trend.last_count', t['last_count'], ',d')} in June, an increase of {n('analysis.trend.change_pct', t['change_pct'])}%. This is a negative workload signal that warrants investigation; it does not establish causal deterioration without actual output and inspection coverage.",
        "",
        "| Month | Events | Critical | Known repair cost |",
        "|---|---:|---:|---:|",
    ]
    for i, m in enumerate(a["monthly"]):
        prefix = f"analysis.monthly[{i}]"
        lines.append(
            f"| {datetime.strptime(m['month'], '%Y-%m').strftime('%B')} | {n(prefix + '.event_count', m['event_count'], ',d')} | {n(prefix + '.critical_count', m['critical_count'], ',d')} | {n(prefix + '.known_repair_cost', m['known_repair_cost'])} |"
        )
    lines += [
        "",
        "## Location and severity",
        "",
        "| Location | Events | Critical | Critical share |",
        "|---|---:|---:|---:|",
    ]
    for i, r in enumerate(a["location_severity"]["location_totals"]):
        p = f"analysis.location_severity.location_totals[{i}]"
        lines.append(
            f"| {r['location']} | {n(p + '.event_count', r['event_count'], ',d')} | {n(p + '.critical_count', r['critical_count'], ',d')} | {n(p + '.critical_share_pct', r['critical_share_pct'])}% |"
        )
    test = a["location_severity"]["test"]
    p = "analysis.location_severity.test"
    lines += [
        "",
        f"Surface has the most events; Component has the most Critical events and the highest Critical share, so focus failure review there. Exploratory association p-value is {n(p + '.p_value', test['p_value'], '.7g')} and Cramer's V is {n(p + '.cramers_v', test['cramers_v'], '.4f')}. Expected-cell checks pass for this source; the association is small and does not prove a causal mechanism.",
        "",
        "## Operational patterns",
        "",
    ]
    top_count = max(enumerate(a["lines"]), key=lambda x: x[1]["event_count"])
    i, line = top_count
    leader = a["lines"][0]
    lines.append(
        f"- Workload and exposure rankings differ: {line['production_line']} has {n(f'analysis.lines[{i}].event_count', line['event_count'], ',d')} events; {leader['production_line']} leads reference-output intensity at {n('analysis.lines[0].reference_intensity_per_1000', leader['reference_intensity_per_1000'], '.4f')} logged events per thousand reference units. Verify actual exposure and product mix before assigning line causality."
    )
    functional = a["costs"]["by_type"][0]
    lines.append(
        f"- {functional['name']} accounts for {n('analysis.costs.by_type[0].known_repair_cost', functional['known_repair_cost'])} observed repair cost and {n('analysis.costs.by_type[0].known_cost_share_pct', functional['known_cost_share_pct'])}% of known cost. Maintenance and Finance should review repair mechanisms together with missing-cost capture."
    )
    lines.append(
        f"- Known repair burden is distributed: reaching the Pareto threshold requires {n('analysis.products.cost_pareto_product_count_80pct', a['products']['cost_pareto_product_count_80pct'], ',d')} master products. Do not assume a generic eighty/twenty rule. Calendar coverage also leaves {n('analysis.quality.temporal_coverage.days_without_events', a['quality']['temporal_coverage']['days_without_events'], ',d')} dates without logged events, so confirm reporting coverage before daily alerts."
    )
    lines += [
        "",
        "## Sensitivity of conclusions",
        "",
        "Only slash dates with an ambiguous month/day order are changed. The alternative interpretation and withholding scenarios change the in-scope evidence population, so lower totals are not savings or manufacturing improvement. These scenarios are not confidence intervals.",
        "",
        "| Scenario | Events | Critical | Known cost | January-June count change | Historical candidates |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    scenario_labels = {
        "primary_md": "Adopted month/day convention",
        "ambiguous_only_dm_stress": "Ambiguous dates read as day/month",
        "withhold_ambiguous_raw_rows": "Ambiguous source dates withheld",
    }
    for name, s in result["sensitivity"]["scenarios"].items():
        p = f"sensitivity.scenarios.{name}"
        s["historical_candidate_count"] = len(s["historical_screen"]["candidates"])
        lines.append(
            f"| {scenario_labels[name]} | {n(p + '.kpis.event_count', s['kpis']['event_count'], ',d')} | {n(p + '.kpis.critical_count', s['kpis']['critical_count'], ',d')} | {n(p + '.kpis.known_repair_cost', s['kpis']['known_repair_cost'])} | {n(p + '.trend.change_pct', s['trend']['change_pct'])}% | {n(p + '.historical_candidate_count', s['historical_candidate_count'], ',d')} |"
        )
    lines += [
        "",
        "Component Critical burden, Line-3 reference intensity and Functional known-cost priorities remain stable across these declared scenarios. Exact product ordering and historical candidate dates change. Confirm date convention with the source owner; candidate totals do not measure detector accuracy.",
        "",
        "## Priorities, owners and success measures",
        "",
    ]
    # Recommendations describe action; their numeric evidence is written explicitly above.
    for recommendation in a["recommendations"]:
        lines += [
            f"### {recommendation['title']}",
            "",
            f"Action: {recommendation['action']}",
            f"Owner: {recommendation['owner']}.",
            f"Success measure: {recommendation['success_measure']}",
            "",
        ]
    lines += [
        "## Evidence needed to verify improvement",
        "",
        "Use the local investigation records to document proposed owners, acknowledgements, actions and follow-up reviews. These records do not confirm that another person received an assignment or that a corrective action improved plant performance.",
        "",
        "To verify improvement, collect actual produced and inspected units, operating calendars and operator feedback over comparable periods, and complete the missing cost records. Establish a dependable feed of operating records and clear reporting expectations, validate alert usefulness, and connect shared assignments and reviews to the factory operating workflow. Keep human review before operational decisions.",
        "",
        "The current findings support targeted investigation. They do not establish current plant conditions, confirmed causes or measured savings.",
        "",
        "![Monthly logged events and Critical events](monthly_quality.png)",
        "",
    ]
    rendered = "\n".join(lines)
    for digit, word in (("1", "One"), ("2", "Two"), ("3", "Three"), ("4", "Four")):
        rendered = rendered.replace("Line-" + digit, "Line " + word)
    return rendered, list(claims.values())


def execute_case_study(
    output_dir: str | Path,
    master_path: str | Path | None = None,
    template_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fetch HTTP data, verify frozen evidence, execute all tasks and write artifacts.

    Only output_dir is written. Original input files and template are untouched.
    A source-hash drift fails clearly rather than fabricating agreement with old answers.
    """
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    master = Path(master_path or MASTER_PATH).resolve()
    template = Path(template_path or ROOT / "DATA_QUALITY_TEMPLATE.xlsx").resolve()
    for source in (master, template):
        if output == source.parent or output == source:
            raise ValueError("Use a dedicated run artifact directory, separate from source inputs")
    raw, provenance = fetch_events(SOURCE_URL)
    master_bytes = master.read_bytes()
    if (
        provenance["source_sha256"] != EXPECTED_SOURCE_SHA256
        or hashlib.sha256(master_bytes).hexdigest() != EXPECTED_MASTER_SHA256
    ):
        raise ValueError(
            "Historical case source hash changed; independent re-audit required before using pinned answers"
        )
    snapshot = build_snapshot(raw, provenance, master_bytes)
    analysis = analysis_report(snapshot, Scope())
    sensitivity = evaluate_date_sensitivity(raw, provenance, master_bytes)
    hashes = code_identity()
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    result: dict[str, Any] = {
        "analysis": analysis,
        "sensitivity": sensitivity,
        "qa": _qa(snapshot, analysis),
        "code_sha256": digest,
        "code_files": hashes,
        "provenance": snapshot.provenance,
    }
    report, claims = _render_report(result)
    result["numeric_claims"] = claims
    generated = generate_workbook(snapshot, output, template)
    result["workbook_delivery"] = generated["workbook_delivery"]
    with (output / "cleaned_records.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        rows = [e.as_record() for e in filter_events(snapshot, Scope())]
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        "'" + v
                        if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@"))
                        else v
                    )
                    for key, v in row.items()
                }
            )
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 4.5), layout="constrained")
    months = analysis["monthly"]
    ax.plot(
        [m["month"] for m in months],
        [m["event_count"] for m in months],
        marker="o",
        label="Logged events",
    )
    ax.plot(
        [m["month"] for m in months],
        [m["critical_count"] for m in months],
        marker="s",
        label="Critical events",
    )
    ax.set(title="Historical logged defect burden", xlabel="Month", ylabel="Logged events")
    ax.set_ylim(bottom=0)
    ax.legend()
    ax.grid(axis="y", alpha=0.2)
    fig.savefig(output / "monthly_quality.png", dpi=160)
    plt.close(fig)
    (output / "report.md").write_text(report, encoding="utf-8")
    (output / "numeric_claims.json").write_text(
        json.dumps(claims, indent=2, allow_nan=False), encoding="utf-8"
    )
    if (
        hashlib.sha256(Path(generated["workbook"]).read_bytes()).hexdigest()
        != result["workbook_delivery"]["output"]["sha256"]
    ):
        raise AssertionError("Completed workbook changed after read-back")
    (output / "evidence.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    files = sorted(p for p in output.iterdir() if p.is_file() and p.name != "evidence.zip")
    with ZipFile(output / "evidence.zip", "w", ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, file.name)
    media = {
        "md": "text/markdown",
        "json": "application/json",
        "csv": "text/csv",
        "png": "image/png",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "zip": "application/zip",
    }
    result["artifacts"] = [
        {
            "name": p.name,
            "path": str(p),
            "media_type": media.get(p.suffix[1:], "application/octet-stream"),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(output.iterdir())
        if p.is_file()
    ]
    result["report"] = report
    return result


async def run_case_study(
    output_dir: str | Path,
    master_path: str | Path | None = None,
    template_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute the bounded CPU/network work without blocking the agent event loop."""
    return await asyncio.to_thread(execute_case_study, output_dir, master_path, template_path)
