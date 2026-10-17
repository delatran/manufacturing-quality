"""Behavioral checks for versioned policy, scoped scenarios and row evidence."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
from dataclasses import asdict
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from analyst.case_study.analytics import (
    Scope,
    analysis_report,
    compare_periods,
    scenario_comparison,
)
from analyst.case_study.ingestion import SourceError, fetch_events
from analyst.case_study.preparation import build_snapshot
from analyst.dashboard import AnalysisService, register_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
MASTER = b"product_id,product_name,category,production_line,monthly_output\n1,A,Category 1,Line-1,100\n2,B,Category 2,Line-2,200\n"


def event(defect_id: int = 1, **overrides):
    return {
        "defect_id": defect_id,
        "product_id": 1,
        "defect_type": "Functional",
        "defect_date": "1/2/2024",
        "defect_location": "Component",
        "severity": "Critical",
        "inspection_method": "Automated Testing",
        "repair_cost": 10,
        **overrides,
    }


def snapshot(rows, master=MASTER):
    payload = json.dumps(rows, sort_keys=True).encode()
    provenance = {
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "source_url": "https://synthetic.invalid/events",
        "fetched_at": "2026-10-16T00:00:00Z",
    }
    return build_snapshot(rows, provenance, master)


def test_source_content_schema_and_exact_response_identity_stay_in_memory(tmp_path, monkeypatch):
    payload = json.dumps([event(), event(2, repair_cost=None)]).encode()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=payload,
            headers={"Content-Type": "text/plain; charset=utf-8", "ETag": '"actual-source"'},
        )
    )
    monkeypatch.chdir(tmp_path)
    rows, provenance = fetch_events("https://synthetic.invalid/events", transport=transport)
    assert provenance["source_sha256"] == hashlib.sha256(payload).hexdigest()
    assert provenance["source_content_type"] == "text/plain; charset=utf-8"
    assert provenance["source_etag"] == '"actual-source"'
    assert provenance["source_schema"]["observed_field_types"]["repair_cost"] == ["null", "number"]
    prepared = build_snapshot(rows, provenance, MASTER)
    assert list(tmp_path.iterdir()) == []
    assert prepared.provenance["policy_version"] == "canonical-v2"
    assert prepared.provenance["dataset_version"].startswith("dataset-")
    assert prepared.provenance["source_schema"]["row_count"] == 2
    rows[0]["repair_cost"] = 999
    with pytest.raises(SourceError, match="changed after fetching"):
        build_snapshot(rows, provenance, MASTER)


def test_raw_values_and_normalization_are_traceable_without_mutating_inputs():
    rows = [event(severity=" critical ", defect_type=" Functional ")]
    original = copy.deepcopy(rows)
    prepared = snapshot(rows)
    assert rows == original
    assert isinstance(prepared.raw_records, tuple)
    assert json.loads(prepared.raw_records[0]) == original[0]
    rows[0]["severity"] = "Minor"
    assert json.loads(prepared.raw_records[0])["severity"] == " critical "
    changes = [asdict(entry) for entry in prepared.audit if entry.code == "label_normalized"]
    assert {entry["field"] for entry in changes} == {"severity", "defect_type"}
    assert all(
        entry["policy_version"] == "canonical-v2" and entry["source_rows"] == (1,)
        for entry in changes
    )
    severity = next(entry for entry in changes if entry["field"] == "severity")
    assert severity["raw_value"] == " critical " and severity["processed_value"] == "Critical"
    date_change = next(entry for entry in prepared.audit if entry.field == "defect_date")
    assert date_change.raw_value == "1/2/2024" and date_change.processed_value == "2024-01-02"


def test_financial_scenario_preserves_complementary_event_and_canonical_cost():
    prepared = snapshot([event(), event(repair_cost=None)])
    before = copy.deepcopy(prepared)
    compared = scenario_comparison(prepared, Scope(), "exclude_cost_10")
    canonical, selected = compared["canonical"], compared["selected"]
    assert canonical["kpis"]["event_count"] == selected["kpis"]["event_count"] == 1
    assert canonical["kpis"]["known_repair_cost"] == 10
    assert selected["kpis"]["known_repair_cost"] == 0
    assert selected["kpis"]["cost_known_count"] == 0
    assert selected["cost_distribution"]["unknown_by_status"] == {"scenario_excluded_cost_10": 1}
    assert compared["comparison"]["metric_deltas"]["known_repair_cost"]["delta"] == -10
    assert prepared == before
    merge = next(entry for entry in prepared.audit if entry.code == "complementary_duplicate")
    assert merge.processed_value == "10.0" and merge.source_rows == (1, 2)


def test_date_scenario_rechecks_conflicts_before_resolving_duplicates():
    # The two source spellings mean the same date canonically. Under D/M the
    # slash value moves to February while the ISO value stays in January.
    prepared = snapshot([event(), event(defect_date="2024-01-02")])
    compared = scenario_comparison(prepared, Scope(), "ambiguous_dm")
    assert compared["canonical"]["kpis"]["event_count"] == 1
    assert compared["selected"]["kpis"]["event_count"] == 0
    assert compared["selected"]["quality"]["clean_event_count"] == 1
    assert compared["selected"]["quality_scope"]["scenario_applied"] is False
    filtered = analysis_report(prepared, Scope(severity="Critical", scenario="ambiguous_dm"))
    assert filtered["kpis"]["event_count"] == 0 and filtered["scope"]["severity"] == "Critical"
    assert prepared.events[0].defect_date.isoformat() == "2024-01-02"


def test_year_scenario_retains_actual_source_year_and_same_dataset_version():
    prepared = snapshot([event(defect_date="2026-06-20")])
    compared = scenario_comparison(prepared, Scope(), "year_2024")
    assert compared["canonical"]["kpis"]["event_count"] == 0
    assert compared["selected"]["kpis"]["event_count"] == 1
    assert (
        compared["canonical"]["metadata"]["dataset_version"]
        == compared["selected"]["metadata"]["dataset_version"]
    )
    assert prepared.events[0].defect_date.year == 2026
    assert json.loads(prepared.raw_records[0])["defect_date"] == "2026-06-20"


def test_event_filter_and_zero_event_products_do_not_shrink_denominator():
    prepared = snapshot([event(), event(2, product_id=2, severity="Minor")])
    report = analysis_report(
        prepared, Scope(start_month="2024-01", end_month="2024-01", severity="Critical")
    )
    values = report["kpis"]
    assert values["event_count"] == values["eligible_event_count"] == 1
    assert values["cohort_product_count"] == 2 and values["cohort_products_without_events"] == 1
    assert values["reference_output"] == 300 and values["output_coverage_pct"] == 100
    assert values["reference_intensity_per_1000"] == pytest.approx(1000 / 300)
    incomplete = MASTER.replace(b"Line-2,200", b"Line-2,")
    values = analysis_report(snapshot([event()], incomplete), Scope())["kpis"]
    assert values["output_known_product_count"] == values["output_unknown_product_count"] == 1
    assert values["output_coverage_pct"] == 50 and values["known_monthly_output_subtotal"] == 100
    assert values["reference_output"] is None and values["reference_intensity_per_1000"] is None
    assert values["reference_intensity_status"] == "incomplete_output"


def test_histogram_boundaries_quantiles_and_unknown_coverage_are_independent():
    costs = [10, 100, 250, 500, 750, 1000, None, -2]
    distribution = analysis_report(
        snapshot([event(i, repair_cost=cost) for i, cost in enumerate(costs, 1)]), Scope()
    )["cost_distribution"]
    assert [row["count"] for row in distribution["histogram"]] == [1, 1, 1, 1, 1, 1]
    assert sum(row["known_cost"] for row in distribution["histogram"]) == 2610
    assert distribution["quantiles"]["median"] == 375
    assert distribution["quantiles"]["min"] == 10 and distribution["quantiles"]["max"] == 1000
    assert distribution["cost_coverage_pct"] == 75
    assert distribution["unknown_by_status"] == {"missing": 1, "negative": 1}
    empty = analysis_report(snapshot([event(repair_cost=None)]), Scope())["cost_distribution"]
    assert all(value is None for value in empty["quantiles"].values())
    assert all(row["share_of_known_cost_events_pct"] is None for row in empty["histogram"])


def test_decomposition_reconciles_offsetting_and_negative_changes():
    prepared = snapshot(
        [
            event(1),
            event(2),
            event(3, product_id=2),
            event(4, defect_date="6/20/2024"),
            event(5, product_id=2, defect_date="6/20/2024"),
            event(6, product_id=2, defect_date="6/20/2024"),
        ]
    )
    january = Scope(start_month="2024-01", end_month="2024-01")
    june = Scope(start_month="2024-06", end_month="2024-06")
    comparison = compare_periods(prepared, january, june, "production_line")
    assert comparison["event_count_change"] == 0
    assert {row["event_count_change"] for row in comparison["rows"]} == {-1, 1}
    assert all(row["contribution_pct_of_event_change"] is None for row in comparison["rows"])
    assert all(comparison["reconciliation"].values())
    decline = compare_periods(
        prepared, january, Scope(start_month="2024-02", end_month="2024-02"), "product_id"
    )
    assert decline["event_count_change"] == -3
    assert sum(row["contribution_pct_of_event_change"] for row in decline["rows"]) == pytest.approx(
        100
    )


def test_scenario_api_trace_exports_and_frozen_context_are_consistent(tmp_path):
    prepared = snapshot(
        [event(), event(2, severity=" critical "), event(3, product_id=1), event(3, product_id=2)]
    )
    service = AnalysisService(ROOT, tmp_path, snapshot_loader=lambda: prepared)
    app = FastAPI()
    register_routes(app, service)
    with TestClient(app) as client:
        canonical = client.get("/api/dashboard").json()
        context_id = canonical["context"]["context_id"]
        comparison = client.get(
            f"/api/dashboard/contexts/{context_id}/scenarios/exclude_cost_10"
        ).json()
        selected = comparison["selected"]
        assert selected["context"]["context_id"] != context_id
        assert selected["context"]["snapshot_id"] == canonical["context"]["snapshot_id"]
        assert selected["context"]["scenario"] == "exclude_cost_10"
        assert selected["context"]["policy_version"] == "canonical-v2"
        trace = client.get(selected["links"]["trace"], params={"defect_id": 1}).json()
        assert trace["raw_records"][0]["raw"]["repair_cost"] == 10
        assert trace["canonical_records"][0]["repair_cost"] == 10
        assert trace["processed_records"][0]["repair_cost"] is None
        assert trace["scenario_audit"][0]["processed_value"] is None
        assert trace["master_lineage"][0]["raw"]["monthly_output"] == "100"
        assert trace["master_lineage"][0]["processed"]["monthly_output"] == 100
        trace["raw_records"][0]["raw"]["repair_cost"] = 888
        assert (
            client.get(selected["links"]["trace"], params={"defect_id": 1}).json()["raw_records"][
                0
            ]["raw"]["repair_cost"]
            == 10
        )
        quarantined = client.get(canonical["links"]["trace"], params={"defect_id": 3}).json()
        assert quarantined["raw_match_count"] == 2 and quarantined["canonical_records"] == []
        assert (
            len(
                [
                    row
                    for row in quarantined["normalization_audit"]
                    if row["code"] == "conflicting_id"
                ]
            )
            == 2
        )
        download = client.get(selected["links"]["export_csv"], params={"locale": "vi"})
        assert download.headers["content-language"] == "vi"
        exported = list(csv.DictReader(io.StringIO(download.content.decode("utf-8-sig"))))
        assert all(
            row["export_locale"] == "vi" and row["currency_unit"] == "local currency"
            for row in exported
        )
        assert all(
            row["scenario"] == "exclude_cost_10" and row["repair_cost"] == "" for row in exported
        )
        zipped = client.get(selected["links"]["export_zip"], params={"locale": "vi"})
        with ZipFile(io.BytesIO(zipped.content)) as archive:
            metadata = json.loads(archive.read("export_metadata.json"))
            assert (
                metadata["export_locale"] == "vi"
                and metadata["scope"] == selected["context"]["filters"]
            )
            manifest = json.loads(archive.read("bundle_manifest.json"))
            assert all(
                hashlib.sha256(archive.read(name)).hexdigest() == digest
                for name, digest in manifest["files"].items()
            )
            assert "normalization_audit.csv" in archive.namelist()
            assert not any(
                "raw_records" in name or "defect_records.json" in name
                for name in archive.namelist()
            )
        assert (
            client.get(f"/api/dashboard/contexts/{context_id}").json()["analysis"]
            == canonical["analysis"]
        )
        decomposed = client.post(
            canonical["links"]["compare_periods"],
            json={
                "baseline_filters": {"start_month": "2024-01", "end_month": "2024-01"},
                "comparison_filters": {"start_month": "2024-06", "end_month": "2024-06"},
                "dimension": "production_line",
            },
        )
        assert decomposed.status_code == 200
        assert decomposed.json()["decomposition"]["event_count_change"] == -2
        assert all(decomposed.json()["decomposition"]["reconciliation"].values())
        assert client.get("/api/dashboard", params={"scenario": "untrusted"}).status_code == 422
        assert (
            client.get(selected["links"]["export_csv"], params={"locale": "xx"}).status_code == 422
        )


@pytest.mark.parametrize("scenario", [None, "canonical", "exclude_all", True])
def test_invalid_scenario_identifiers_fail_validation(scenario):
    with pytest.raises(ValueError):
        analysis_report(snapshot([event()]), Scope(scenario=scenario))
