"""Shared dashboard contexts use synthetic prepared HTTP snapshots, never network."""

from __future__ import annotations

import asyncio
import copy
import csv
import io
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import pytest
from analyst.analysis_sql import query_snapshot
from analyst.case_study.analytics import Scope, analysis_report
from analyst.case_study.ingestion import SourceError
from analyst.case_study.preparation import build_snapshot
from analyst.dashboard import (
    AnalysisService,
    ContextNotFound,
    EvidenceIntegrityError,
    SnapshotUnavailable,
    register_routes,
)
from analyst.evidence import EvidenceLedger, canonical, sha256
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
MASTER = (
    b"product_id,product_name,category,production_line,monthly_output\n"
    b"1,Widget A,Category 1,Line-1,100\n"
    b"2,Widget B,Category 1,Line-1,200\n"
    b"3,Widget C,Category 2,Line-2,50\n"
)


def event(identifier=1, **changes):
    return {
        "defect_id": identifier,
        "product_id": 1,
        "defect_type": "Functional",
        "defect_date": "2024-01-15",
        "defect_location": "Component",
        "severity": "Critical",
        "inspection_method": "Visual Inspection",
        "repair_cost": 10,
        **changes,
    }


def make_snapshot(rows=None, *, fetched_at="2026-01-01T00:00:00+00:00"):
    rows = (
        rows
        if rows is not None
        else [
            event(),
            event(),
            event(2, severity="Minor", repair_cost=None),
            event(3, product_id=2, defect_date="2024-02-03", repair_cost=20),
            event(4, product_id=99, defect_date="2024-03-04", severity="Moderate", repair_cost=-1),
            event(5, product_id=2, defect_date="2024-06-05", repair_cost=30),
            event(6, product_id=3, defect_date="2024-07-06", severity="Moderate", repair_cost=40),
            event(7, product_id=1),
            event(7, product_id=2),
            event(8, defect_date="6/3/2024", repair_cost=0),
        ]
    )
    return build_snapshot(
        rows,
        {
            "source_url": "https://synthetic.invalid/events",
            "source_sha256": sha256(canonical(rows)),
            "fetched_at": fetched_at,
        },
        MASTER,
    )


class Loader:
    def __init__(self, *items):
        self.items = list(items) or [make_snapshot()]
        self.calls = 0

    def __call__(self):
        item = self.items[min(self.calls, len(self.items) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def service(tmp_path, loader=None):
    return AnalysisService(ROOT, tmp_path / "state", snapshot_loader=loader or Loader())


def rows_csv(content: bytes):
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))


def make_client(subject):
    app = FastAPI()
    register_routes(app, subject)
    return TestClient(app)


@pytest.mark.asyncio
async def test_one_canonical_receipt_owns_all_dashboard_and_agent_values(tmp_path):
    loader = Loader()
    subject = service(tmp_path, loader)
    assert loader.calls == 0
    payload = await subject.get_context()
    context, report, receipt = payload["context"], payload["analysis"], payload["evidence"]
    assert loader.calls == 1
    assert len(context["context_id"]) == len(context["snapshot_id"]) == 64
    assert report["kpis"]["event_count"] == 6
    assert report["kpis"]["critical_count"] == 4
    assert Decimal(str(report["kpis"]["known_repair_cost"])) == Decimal("60")
    assert report["kpis"]["reference_output"] == 2100
    assert report["kpis"]["cost_unknown_count"] == 2
    assert report == receipt["result"]
    assert context["code_sha256"] == receipt["code_sha256"] == sha256(receipt["code"])
    assert receipt["result_sha256"] == sha256(canonical(report))
    assert receipt["context"] == context
    numeric = {item["path"]: item["value"] for item in receipt["numeric_values"]}
    assert numeric["$.kpis.event_count"] == 6
    assert numeric["$.kpis.critical_count"] == 4
    assert numeric["$.kpis.known_repair_cost"] == 60
    assert {
        "analyst/dashboard.py",
        "analyst/evidence.py",
        "analyst/case_study/preparation.py",
        "analyst/case_study/ingestion.py",
        "analyst/case_study/analytics.py",
        "analyst/case_study/models.py",
        "analyst/case_study/config.py",
        "knowledge/kpi_registry.yaml",
    } <= receipt["code_manifest"].keys()
    snapshot = await subject.snapshot_for_context(context["context_id"])
    assert report == analysis_report(snapshot, Scope(**context["filters"]))
    ledger = EvidenceLedger(subject.contexts_dir / context["context_id"])
    assert ledger.verify_claims(
        [
            {
                "execution_id": receipt["execution_id"],
                "path": "$.kpis.event_count",
                "value": 6,
                "code_sha256": receipt["code_sha256"],
            }
        ]
    )["verified"]
    assert payload["quality_scope"]["filters_applied"] is False
    assert report["quality"]["raw_event_count"] == 10


@pytest.mark.asyncio
async def test_canonical_scope_cache_and_defensive_copies(tmp_path):
    loader = Loader()
    subject = service(tmp_path, loader)
    first = await subject.get_context({"severity": "Critical", "production_line": "Line-1"})
    again = await subject.get_context(
        {
            "production_line": "Line-1",
            "severity": "Critical",
            **{
                key: value
                for key, value in first["context"]["filters"].items()
                if key not in {"severity", "production_line"}
            },
        }
    )
    assert first == again
    assert loader.calls == 1
    context_id = first["context"]["context_id"]
    expected_hash = first["evidence"]["result_sha256"]
    first["analysis"]["kpis"]["event_count"] = 999
    first["context"]["filters"]["severity"] = "Minor"
    first["evidence"]["result"]["kpis"]["event_count"] = 999
    snapshot = await subject.snapshot_for_context(context_id)
    snapshot.quality["raw_event_count"] = 999
    restored = await subject.context_by_id(context_id)
    assert restored["analysis"]["kpis"]["event_count"] == 4
    assert restored["analysis"]["quality"]["raw_event_count"] == 10
    assert restored["evidence"]["result_sha256"] == expected_hash
    assert len(EvidenceLedger(subject.contexts_dir / context_id).entries) == 1


@pytest.mark.asyncio
async def test_agent_inputs_and_exports_use_same_scope_and_reference_cohort(tmp_path):
    subject = service(tmp_path)
    payload = await subject.get_context(
        {"production_line": "Line-1", "severity": "Critical", "start_month": "2024-02"}
    )
    context_id = payload["context"]["context_id"]
    inputs = await subject.input_paths_for_context(context_id)
    assert [path.name for path in inputs] == ["cleaned_records.csv", "reference_products.csv"]
    events, products = [rows_csv(path.read_bytes()) for path in inputs]
    assert {int(row["defect_id"]) for row in events} == {3, 5, 8}
    assert {int(row["product_id"]) for row in products} == {1, 2}
    kpis = payload["analysis"]["kpis"]
    assert len(events) == kpis["event_count"]
    assert sum(
        Decimal(row["repair_cost"]) for row in events if row["repair_cost"] != ""
    ) == Decimal(str(kpis["known_repair_cost"]))
    assert sum(int(row["monthly_output"]) for row in products) * 5 == kpis["reference_output"]
    intensity = payload["analysis"]["products"]["intensity_all"]
    assert [row["product_id"] for row in intensity] == [1, 2]
    assert [row["linked_event_count"] for row in intensity] == [1, 2]
    assert [row["reference_output"] for row in intensity] == [500, 1000]
    assert all(row["reference_intensity_per_1000"] == 2 for row in intensity)
    assert await subject.export_csv(context_id) == inputs[0].read_bytes()
    before = await subject.export_zip(context_id)
    await subject.get_context({"severity": "Minor"})
    assert await subject.export_zip(context_id) == before
    with ZipFile(io.BytesIO(before)) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert {
            "context.json",
            "analysis.json",
            "evidence.json",
            "ledger.jsonl",
            "cleaned_records.csv",
            "reference_products.csv",
            "definitions.json",
            "kpi_registry.yaml",
            "analysis_code.txt",
            "bundle_manifest.json",
        } <= names
        assert not any("defect_records.json" in name or "raw_records" in name for name in names)
        assert json.loads(archive.read("analysis.json")) == payload["analysis"]
        assert json.loads(archive.read("evidence.json")) == payload["evidence"]
        assert json.loads(archive.read("context.json"))["context"] == payload["context"]
        assert json.loads(archive.read("ledger.jsonl")) == payload["evidence"]
        manifest = json.loads(archive.read("bundle_manifest.json"))
        for name, digest in manifest["files"].items():
            assert sha256(archive.read(name)) == digest
        assert archive.read("cleaned_records.csv") == inputs[0].read_bytes()
        assert (
            json.loads(archive.read("definitions.json"))["quality_scope"]["filters_applied"]
            is False
        )


@pytest.mark.asyncio
async def test_computational_inputs_preserve_labels_types_and_safe_exports(tmp_path):
    labels = ["NA", "=ABC", "'=ABC", "001"]
    master = (
        b"product_id,product_name,category,production_line,monthly_output\n"
        b"1,NA,Category 1,Line-1,100\n"
        b"2,=ABC,Category 1,Line-1,\n"
        b"3,'=ABC,Category 1,Line-1,200\n"
        b"4,001,Category 1,Line-1,50\n"
    )
    source_rows = [
        event(index, product_id=index, repair_cost=cost)
        for index, cost in enumerate([10, None, 0, 20], start=1)
    ]
    snapshot = build_snapshot(
        source_rows,
        {
            "source_url": "https://synthetic.invalid/events",
            "source_sha256": sha256(canonical(source_rows)),
            "fetched_at": "2026-01-01T00:00:00+00:00",
        },
        master,
    )
    subject = service(tmp_path, Loader(snapshot))
    payload = await subject.get_context()
    context_id = payload["context"]["context_id"]
    paths = await subject.input_paths_for_context(context_id)
    hashes = payload["evidence"]["input_sha256"]
    assert all(path.parent.name == "inputs" for path in paths)
    assert [row["product_name"] for row in rows_csv(paths[0].read_bytes())] == labels
    actual = query_snapshot(
        "SELECT e.product_name, p.product_name AS master_name, e.repair_cost, "
        "p.monthly_output, e.matched_product FROM events e "
        "JOIN products p USING (product_id) ORDER BY e.defect_id",
        paths,
        hashes,
    )["rows"]
    assert [row["product_name"] for row in actual] == labels
    assert [row["master_name"] for row in actual] == labels
    assert [row["repair_cost"] for row in actual] == [10, None, 0, 20]
    assert [row["monthly_output"] for row in actual] == [100, None, 200, 50]
    assert all(row["matched_product"] is True for row in actual)
    assert query_snapshot(
        "SELECT count(*) AS n FROM events WHERE product_name IN ('NA', '=ABC', '001')",
        paths,
        hashes,
    )["rows"] == [{"n": 3}]
    assert query_snapshot(
        "SELECT sum(repair_cost) AS cost, count(*) FILTER (WHERE repair_cost IS NULL) AS unknown "
        "FROM events",
        paths,
        hashes,
    )["rows"] == [{"cost": payload["analysis"]["kpis"]["known_repair_cost"], "unknown": 1}]
    for locale in (None, "en", "vi"):
        exported = rows_csv(await subject.export_csv(context_id, locale))
        assert [row["product_name"] for row in exported] == ["NA", "'=ABC", "'=ABC", "001"]
        with ZipFile(io.BytesIO(await subject.export_zip(context_id, locale))) as archive:
            assert not any(name.startswith("inputs/") for name in archive.namelist())
            assert rows_csv(archive.read("cleaned_records.csv")) == rows_csv(
                await subject.export_csv(context_id)
            )
            retained = json.loads(archive.read("canonical_inputs.json"))
            assert retained["input_sha256"] == hashes
            assert retained["tables"]["events"]["rows"] == [
                event.as_record() for event in snapshot.events
            ]
            for name, table_name in (
                ("cleaned_records.csv", "events"),
                ("reference_products.csv", "products"),
            ):
                table = retained["tables"][table_name]
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, fieldnames=table["columns"], lineterminator="\n")
                writer.writeheader()
                writer.writerows(table["rows"])
                assert sha256(stream.getvalue().encode("utf-8-sig")) == hashes[name]
                assert sha256(archive.read(name)) != hashes[name]
            manifest = json.loads(archive.read("bundle_manifest.json"))
            assert all(
                sha256(archive.read(name)) == digest for name, digest in manifest["files"].items()
            )


@pytest.mark.asyncio
async def test_empty_and_unknown_cost_contexts_preserve_semantics(tmp_path):
    subject = service(tmp_path)
    empty = await subject.get_context({"production_line": "Line-2"})
    assert empty["analysis"]["kpis"]["event_count"] == 0
    assert empty["analysis"]["kpis"]["cost_status"] == "empty"
    assert empty["analysis"]["kpis"]["critical_share_pct"] is None
    assert empty["analysis"]["kpis"]["reference_output"] == 300
    empty_product = empty["analysis"]["products"]["intensity_top"][0]
    assert empty_product["product_id"] == 3
    assert empty_product["linked_event_count"] == 0
    assert empty_product["reference_intensity_per_1000"] == 0
    assert empty_product["intensity_available"] is True
    assert rows_csv(await subject.export_csv(empty["context"]["context_id"])) == []
    empty_paths = await subject.input_paths_for_context(empty["context"]["context_id"])
    assert query_snapshot(
        "SELECT count(*) AS n, sum(repair_cost) AS cost FROM events",
        empty_paths,
        empty["evidence"]["input_sha256"],
    )["rows"] == [{"n": 0, "cost": None}]
    unknown = await subject.get_context({"product_id": "99"})
    assert unknown["analysis"]["kpis"]["event_count"] == 1
    assert unknown["analysis"]["kpis"]["cost_status"] == "unavailable"
    assert unknown["analysis"]["kpis"]["cost_unknown_count"] == 1
    assert unknown["analysis"]["kpis"]["reference_output"] is None
    assert unknown["analysis"]["kpis"]["reference_intensity_per_1000"] is None
    assert unknown["analysis"]["products"]["intensity_all"] == []
    assert unknown["analysis"]["quality"] == empty["analysis"]["quality"]
    paths = await subject.input_paths_for_context(unknown["context"]["context_id"])
    assert rows_csv(paths[1].read_bytes()) == []


@pytest.mark.asyncio
async def test_refresh_retains_old_receipt_and_failed_fetch_only_changes_status(tmp_path):
    loader = Loader(
        make_snapshot(),
        make_snapshot([event(20, repair_cost=200)]),
        SourceError("Synthetic source outage"),
    )
    subject = service(tmp_path, loader)
    first = await subject.get_context()
    original = copy.deepcopy(first)
    second = await subject.refresh()
    assert second["context"]["snapshot_id"] != first["context"]["snapshot_id"]
    assert second["analysis"]["kpis"]["event_count"] == 1
    frozen = await subject.context_by_id(first["context"]["context_id"])
    assert frozen["analysis"] == original["analysis"]
    assert frozen["evidence"] == original["evidence"]
    assert frozen["context"] == original["context"]
    comparison = await subject.get_context(
        {"severity": "Minor"}, snapshot_id=first["context"]["snapshot_id"]
    )
    assert comparison["analysis"]["kpis"]["event_count"] == 1
    stale = await subject.refresh()
    assert stale["status"]["stale"] is True
    assert "Synthetic source outage" in stale["status"]["last_error"]
    for key in ("context", "analysis", "evidence"):
        assert stale[key] == second[key]
    old_stale = await subject.context_by_id(first["context"]["context_id"])
    assert old_stale["evidence"] == original["evidence"]
    assert old_stale["status"]["stale"] is True
    assert old_stale["analysis"]["metadata"]["stale"] is False
    assert loader.calls == 3


@pytest.mark.asyncio
async def test_unchanged_refresh_and_restart_reuse_original_receipt(tmp_path):
    subject = service(
        tmp_path, Loader(make_snapshot(), make_snapshot(fetched_at="2026-01-02T00:00:00+00:00"))
    )
    original = await subject.get_context({"severity": "Critical"})
    refreshed = await subject.refresh({"severity": "Critical"})
    for key in ("context", "analysis", "evidence"):
        assert refreshed[key] == original[key]
    after_restart = service(tmp_path, Loader(make_snapshot(fetched_at="2026-01-03T00:00:00+00:00")))
    restored = await after_restart.context_by_id(original["context"]["context_id"])
    for key in ("context", "analysis", "evidence"):
        assert restored[key] == original[key]
    assert await after_restart.export_zip(
        original["context"]["context_id"]
    ) == await subject.export_zip(original["context"]["context_id"])


@pytest.mark.asyncio
async def test_restart_never_substitutes_new_source_or_code(tmp_path, monkeypatch):
    original = await service(tmp_path).get_context()
    changed_source = service(tmp_path, Loader(make_snapshot([event(200)])))
    with pytest.raises(SnapshotUnavailable, match="not switched"):
        await changed_source.context_by_id(original["context"]["context_id"])
    original_bundle = AnalysisService._code_bundle

    def altered_bundle(self):
        code, manifest, registry = original_bundle(self)
        return code + "\n# Synthetic version change\n", manifest, registry

    monkeypatch.setattr(AnalysisService, "_code_bundle", altered_bundle)
    changed_code = service(tmp_path)
    newer = await changed_code.get_context()
    assert newer["context"]["code_sha256"] != original["context"]["code_sha256"]
    assert newer["context"]["snapshot_id"] != original["context"]["snapshot_id"]
    with pytest.raises(SnapshotUnavailable):
        await changed_code.context_by_id(original["context"]["context_id"])


@pytest.mark.asyncio
async def test_concurrent_context_requests_write_one_receipt(tmp_path):
    loader = Loader()
    subject = service(tmp_path, loader)
    values = await asyncio.gather(
        *(subject.get_context({"severity": "Critical"}) for _ in range(6))
    )
    assert len({value["evidence"]["execution_id"] for value in values}) == 1
    assert loader.calls == 1
    assert (
        len(EvidenceLedger(subject.contexts_dir / values[0]["context"]["context_id"]).entries) == 1
    )


@pytest.mark.parametrize(
    "filters",
    [
        {"made_up": "x"},
        {"severity": "Extreme"},
        {"production_line": "Line-unknown"},
        {"category": "Missing"},
        {"product_id": "01"},
        {"product_id": 1},
        {"start_month": "2023-12"},
        {"end_month": "2024-07"},
        {"start_month": "2024-06", "end_month": "2024-01"},
        {"severity": None},
    ],
)
@pytest.mark.asyncio
async def test_invalid_scopes_are_rejected_without_context_artifacts(tmp_path, filters):
    subject = service(tmp_path)
    with pytest.raises(ValueError):
        await subject.get_context(filters)
    assert not subject.contexts_dir.exists()


@pytest.mark.parametrize("identifier", ["../context", "A" * 64, "1" * 63, "", None])
@pytest.mark.asyncio
async def test_strict_context_identifiers_do_not_touch_files_or_fetch(tmp_path, identifier):
    loader = Loader()
    subject = service(tmp_path, loader)
    with pytest.raises(ValueError):
        await subject.context_by_id(identifier)
    assert loader.calls == 0
    assert not subject.contexts_dir.exists()


@pytest.mark.asyncio
async def test_missing_context_and_snapshot_have_distinct_errors(tmp_path):
    subject = service(tmp_path)
    with pytest.raises(ContextNotFound):
        await subject.context_by_id("a" * 64)
    with pytest.raises(SnapshotUnavailable):
        await subject.get_context(snapshot_id="b" * 64)


@pytest.mark.parametrize(
    "relative",
    [
        "ledger.jsonl",
        "artifacts.json",
        "receipt.json",
        "artifacts/analysis.json",
        "artifacts/evidence.json",
        "artifacts/cleaned_records.csv",
        "artifacts/reference_products.csv",
        "artifacts/inputs/cleaned_records.csv",
        "artifacts/inputs/reference_products.csv",
        "artifacts/canonical_inputs.json",
        "artifacts/evidence.zip",
    ],
)
@pytest.mark.asyncio
async def test_any_tampered_receipt_or_registered_artifact_is_refused(tmp_path, relative):
    subject = service(tmp_path)
    payload = await subject.get_context()
    context_id = payload["context"]["context_id"]
    target = subject.contexts_dir / context_id / relative
    target.write_bytes(target.read_bytes() + b"\ntampered")
    with pytest.raises(EvidenceIntegrityError):
        await subject.context_by_id(context_id)
    with pytest.raises(EvidenceIntegrityError):
        await subject.export_csv(context_id)
    with pytest.raises(EvidenceIntegrityError):
        await subject.input_paths_for_context(context_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["code", "numeric_values", "entry_identity"])
async def test_restart_rejects_rehashed_ledger_with_invalid_receipt_bindings(tmp_path, tamper):
    subject = service(tmp_path)
    payload = await subject.get_context()
    context_id = payload["context"]["context_id"]
    directory = subject.contexts_dir / context_id
    ledger_path = directory / "ledger.jsonl"
    entry = json.loads(ledger_path.read_text(encoding="utf-8"))
    if tamper == "code":
        entry["code"] = "print('different calculation')"
        entry["code_sha256"] = sha256(entry["code"])
    elif tamper == "numeric_values":
        entry["numeric_values"] = []
    else:
        entry["execution_id"] = "unrecognized-execution"
    entry["entry_sha256"] = sha256(
        canonical({key: value for key, value in entry.items() if key != "entry_sha256"})
    )
    ledger_path.write_text(canonical(entry) + "\n", encoding="utf-8")
    if tamper != "entry_identity":
        descriptor_path = directory / "receipt.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["entry_sha256"] = entry["entry_sha256"]
        descriptor_path.write_text(canonical(descriptor), encoding="utf-8")
    assert EvidenceLedger(directory).verify_integrity() == []
    with pytest.raises(EvidenceIntegrityError):
        await service(tmp_path).context_by_id(context_id)


@pytest.mark.asyncio
async def test_records_sort_pagination_unknowns_and_scope_match_receipt(tmp_path):
    subject = service(tmp_path)
    payload = await subject.get_context()
    context_id = payload["context"]["context_id"]
    for order in ("asc", "desc"):
        result = await subject.records(
            context_id, page_size=100, sort_by="repair_cost", sort_order=order
        )
        assert result["total"] == payload["analysis"]["kpis"]["event_count"] == 6
        assert [row["defect_id"] for row in result["rows"][-2:]] == [2, 4]
        costs = [row["repair_cost"] for row in result["rows"][:-2]]
        assert costs == sorted(costs, reverse=order == "desc")
    first = await subject.records(context_id, page_size=2, sort_by="defect_id", sort_order="asc")
    last = await subject.records(
        context_id, page=99, page_size=2, sort_by="defect_id", sort_order="asc"
    )
    assert first["page_count"] == last["page"] == 3
    assert [row["defect_id"] for row in first["rows"]] == [1, 2]
    assert [row["defect_id"] for row in last["rows"]] == [5, 8]
    for changes in (
        {"page": 0},
        {"page_size": 101},
        {"sort_by": "__dict__"},
        {"sort_order": "down"},
    ):
        with pytest.raises(ValueError):
            await subject.records(context_id, **changes)


def test_routes_filter_context_evidence_exports_and_errors(tmp_path):
    subject = service(tmp_path)
    with make_client(subject) as client:
        response = client.get("/api/dashboard", params={"severity": "Critical"})
        assert response.status_code == 200, response.text
        payload = response.json()
        context_id = payload["context"]["context_id"]
        base = "/api/dashboard/contexts/" + context_id
        assert client.get(base).json()["evidence"] == payload["evidence"]
        assert client.get(payload["links"]["evidence"]).json() == payload["evidence"]
        records = client.get(
            payload["links"]["records"],
            params={"page_size": 2, "sort_by": "defect_id", "sort_order": "asc"},
        ).json()
        assert records["total"] == 4 and len(records["rows"]) == 2
        exported = client.get(payload["links"]["export_csv"])
        assert exported.status_code == 200 and len(rows_csv(exported.content)) == 4
        assert "attachment" in exported.headers["content-disposition"]
        zipped = client.get(payload["links"]["export_zip"])
        assert zipped.status_code == 200 and zipped.headers["content-type"] == "application/zip"
        assert (
            client.post(
                "/api/dashboard/refresh", json={"filters": {"severity": "Critical"}}
            ).json()["context"]
            == payload["context"]
        )
        for url in (
            "/api/dashboard?surprise=x",
            "/api/dashboard?severity=Minor&severity=Critical",
            "/api/dashboard?severity=Extreme",
            base + "/records?sort_by=bad",
            base + "/records?page_size=0",
            "/api/dashboard/contexts/invalid",
            "/api/dashboard?snapshot_id=../bad",
        ):
            assert client.get(url).status_code == 422, url
        assert client.get("/api/dashboard/contexts/" + "a" * 64).status_code == 404
        assert client.get("/api/dashboard", params={"snapshot_id": "b" * 64}).status_code == 409
        assert client.post("/api/dashboard/refresh", json={"unexpected": True}).status_code == 422
        assert client.post("/api/dashboard/refresh", content="not json").status_code == 422
        (subject.contexts_dir / context_id / "artifacts" / "evidence.zip").write_bytes(b"tampered")
        assert client.get(payload["links"]["export_zip"]).status_code == 409


def test_source_failure_has_clear_503_without_local_raw_fallback(tmp_path):
    subject = service(tmp_path, Loader(SourceError("Synthetic source outage")))
    with make_client(subject) as client:
        response = client.get("/api/dashboard")
        assert response.status_code == 503
        assert "HTTP source" in response.json()["detail"]
    assert not subject.contexts_dir.exists()


@pytest.mark.parametrize("value", [None, [], "not a receipt", 17])
def test_valid_json_with_wrong_ledger_shape_is_integrity_error(tmp_path, value):
    subject = service(tmp_path)
    with make_client(subject) as client:
        payload = client.get("/api/dashboard").json()
        context_id = payload["context"]["context_id"]
        (subject.contexts_dir / context_id / "ledger.jsonl").write_text(
            json.dumps(value) + "\n", encoding="utf-8"
        )
        assert client.get("/api/dashboard/contexts/" + context_id).status_code == 409
    restarted = service(tmp_path)
    with make_client(restarted) as client:
        assert client.get("/api/dashboard/contexts/" + context_id).status_code == 409


def test_product_intensity_ranks_entire_cohort_with_stable_ties():
    master = (
        "product_id,product_name,category,production_line,monthly_output\n"
        + "".join(
            f"{identifier},Widget {identifier},Category 1,Line-1,{1 if identifier == 12 else 1000}\n"
            for identifier in range(1, 13)
        )
    ).encode()
    rows = [
        event(identifier * 10 + offset, product_id=identifier)
        for identifier in range(1, 12)
        for offset in range(3)
    ]
    rows.append(event(120, product_id=12, repair_cost=None))
    snapshot = build_snapshot(
        rows,
        {
            "source_sha256": sha256(canonical(rows)),
            "source_url": "https://synthetic.invalid/events",
            "fetched_at": "2026-01-01T00:00:00Z",
        },
        master,
    )
    report = analysis_report(snapshot, Scope(end_month="2024-01"))
    products = report["products"]
    assert [row["product_id"] for row in products["top"]] == list(range(1, 11))
    assert [row["product_id"] for row in products["intensity_all"]] == [12, *range(1, 12)]
    assert products["intensity_top"] == products["intensity_all"][:10]
    leader = products["intensity_top"][0]
    assert leader["linked_event_count"] == leader["event_count"] == 1
    assert leader["reference_output"] == 1
    assert leader["reference_intensity_per_1000"] == 1000
    assert leader["cost_status"] == "unavailable"
    assert leader["cost_coverage_pct"] == 0
    assert "cumulative_share_pct" not in leader
    assert "cumulative_known_cost_share_pct" not in leader
    assert [row["rank"] for row in products["intensity_all"]] == list(range(1, 13))
    assert report["kpis"]["reference_output"] == 11001
    assert report["kpis"]["reference_intensity_per_1000"] == pytest.approx(34 / 11001 * 1000)


@pytest.mark.parametrize("output", [None, 0])
def test_missing_or_zero_product_output_stays_unavailable_after_valid_zero_event_products(output):
    snapshot = make_snapshot([event(repair_cost=None)])
    snapshot = replace(
        snapshot,
        products=tuple(
            replace(product, monthly_output=output) if product.product_id == 1 else product
            for product in snapshot.products
        ),
    )
    report = analysis_report(snapshot, Scope(end_month="2024-01"))
    intensity = report["products"]["intensity_all"]
    assert [row["product_id"] for row in intensity] == [2, 3, 1]
    assert [row["reference_intensity_per_1000"] for row in intensity] == [0, 0, None]
    unavailable = intensity[-1]
    assert unavailable["reference_output"] == output
    assert unavailable["linked_event_count"] == 1
    assert unavailable["intensity_available"] is False
    assert unavailable["cost_unknown_count"] == 1
    assert unavailable["cost_status"] == "unavailable"


def test_sparse_location_association_preserves_unknowns_and_warns_about_expected_cells():
    snapshot = make_snapshot(
        [
            event(defect_location="Surface"),
            event(2, defect_location="Component", severity="Minor"),
            event(3, defect_location=None, severity=None),
        ]
    )
    report = analysis_report(snapshot, Scope())
    location = report["location_severity"]
    assert sum(row["event_count"] for row in location["location_totals"]) == 3
    assert sum(cell["count"] for cell in location["cells"]) == 3
    assert location["test"]["excluded_unknown_count"] == 1
    assert location["test"]["available"] is True
    assert location["test"]["minimum_expected"] == 0.5
    assert location["test"]["low_expected_cell_pct"] == 100
    assert location["test"]["reliable"] is False
    assert report["kpis"]["critical_share_pct"] == pytest.approx(100 / 3)


def test_quality_workbook_download_is_actual_completed_full_case_not_scoped(tmp_path):
    subject = service(tmp_path)
    completed = (ROOT / "DATA_QUALITY.xlsx").read_bytes()
    with make_client(subject) as client:
        full = client.get("/api/dashboard").json()
        narrowed = client.get(
            "/api/dashboard", params={"severity": "Minor", "end_month": "2024-01"}
        ).json()
        assert (
            full["analysis"]["kpis"]["event_count"] != narrowed["analysis"]["kpis"]["event_count"]
        )
        assert full["links"]["quality_workbook"] == narrowed["links"]["quality_workbook"]
        scope = narrowed["quality_workbook_scope"]
        assert scope["kind"] == "completed_full_case"
        assert scope["filters_applied"] is False
        assert "does not follow dashboard filters" in scope["description"]
        assert "currently selected context" in scope["description"]
        response = client.get(narrowed["links"]["quality_workbook"])
        assert response.status_code == 200
        assert response.content == completed
        assert (
            response.headers["content-type"]
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert 'filename="DATA_QUALITY-full-case.xlsx"' in response.headers["content-disposition"]
        assert response.headers["x-analysis-scope"] == "completed-full-case"
        assert response.headers["etag"] == '"' + sha256(completed) + '"'
        with ZipFile(io.BytesIO(response.content)) as workbook:
            assert workbook.testzip() is None
            assert "xl/workbook.xml" in workbook.namelist()
        assert (
            client.get(
                narrowed["links"]["quality_workbook"], params={"severity": "Minor"}
            ).status_code
            == 422
        )
        with ZipFile(io.BytesIO(client.get(narrowed["links"]["export_zip"]).content)) as bundle:
            descriptor = json.loads(bundle.read("context.json"))
            assert descriptor["quality_workbook_scope"] == scope
            assert "DATA_QUALITY.xlsx" not in bundle.namelist()


def test_missing_completed_workbook_does_not_serve_template_or_fetch_source(tmp_path):
    loader = Loader(SourceError("The workbook download must not fetch source data"))
    subject = service(tmp_path, loader)
    subject.root = tmp_path
    (tmp_path / "DATA_QUALITY_TEMPLATE.xlsx").write_bytes(b"Template is not a completed workbook")
    with make_client(subject) as client:
        response = client.get("/api/dashboard/quality-workbook")
        assert response.status_code == 404
        assert "completed full-case" in response.json()["detail"]
    assert loader.calls == 0


def test_exact_day_records_preserve_context_and_scope_before_pagination(tmp_path):
    rows = [event(i, defect_date="2024-06-03") for i in range(1, 23)]
    rows += [event(23, defect_date="2024-06-04"), event(24, product_id=3, defect_date="2024-06-03")]
    subject = service(tmp_path, Loader(make_snapshot(rows)))
    with make_client(subject) as client:
        payload = client.get("/api/dashboard", params={"production_line": "Line-1"}).json()
        url = payload["links"]["records"]
        first = client.get(url, params={"defect_date": "2024-06-03"}).json()
        last = client.get(url, params={"defect_date": "2024-06-03", "page": 2}).json()
        assert first["context"] == last["context"] == payload["context"]
        assert first["record_filters"] == {"defect_date": "2024-06-03"}
        assert first["total"] == last["total"] == 22
        assert first["page_count"] == last["page_count"] == 2
        assert len(first["rows"]) == 20 and len(last["rows"]) == 2
        inspected = first["rows"] + last["rows"]
        assert {row["defect_date"] for row in inspected} == {"2024-06-03"}
        assert len({row["defect_id"] for row in inspected}) == 22
        assert client.get(url).json()["total"] == 23
        assert len(rows_csv(client.get(payload["links"]["export_csv"]).content)) == 23
        outside = client.get(url, params={"defect_date": "2024-05-03"}).json()
        assert outside["total"] == 0 and outside["rows"] == []
        for invalid in ("2024-6-3", "2024-02-30", "June 3", "2024-06-03T00:00:00", ""):
            assert client.get(url, params={"defect_date": invalid}).status_code == 422
