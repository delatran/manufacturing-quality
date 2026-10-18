"""Report checks use one fresh evidence snapshot without weakening numeric QA."""

import copy
import json
from datetime import date, timedelta

import pytest
from analyst.evidence import EvidenceLedger
from analyst.runtime import AnalystReport, audit_report


def claim_for(entry, value):
    return {
        "id": "total",
        "execution_id": entry["execution_id"],
        "path": "$.total",
        "value": value,
        "code_sha256": entry["code_sha256"],
    }


def test_claim_cannot_use_a_different_snapshot_than_its_integrity_check(tmp_path, monkeypatch):
    ledger = EvidenceLedger(tmp_path)
    entry = ledger.record("python", "result={'total':12}", {"total": 12})
    changed = copy.deepcopy(entry)
    changed["result"]["total"] = 13
    changed["numeric_values"][0]["value"] = 13
    reads = iter(([entry], [changed]))
    monkeypatch.setattr(ledger, "records", lambda: next(reads))

    result = ledger.verify_claims([claim_for(entry, 13)])

    assert not result["verified"]
    assert "claim[0]: value differs from evidence" in result["errors"]


def test_report_cannot_admit_labels_from_a_later_unverified_snapshot(tmp_path, monkeypatch):
    ledger = EvidenceLedger(tmp_path)
    entry = ledger.record(
        "analysis_query",
        "# canonical category",
        {"ok": True, "analysis": {"rows": [{"category": "Category 4"}]}},
        execution_kind="bounded_analysis",
    )
    changed = copy.deepcopy(entry)
    changed["result"]["analysis"]["rows"][0]["category"] = "Category 9"
    calls = 0

    def changing_records():
        nonlocal calls
        calls += 1
        return [entry] if calls == 1 else [changed]

    monkeypatch.setattr(ledger, "records", changing_records)

    result = audit_report(AnalystReport(report="Category 9", claims=[]), ledger)

    assert not result["verified"]
    assert any("lacks matching cited evidence" in error for error in result["errors"])


def test_each_new_report_audit_rechecks_changed_disk_bytes(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    entry = ledger.record("python", "result={'total':12}", {"total": 12})
    report = AnalystReport(report="Total: 12 [claim:total]", claims=[claim_for(entry, 12)])
    assert audit_report(report, ledger)["verified"]

    changed = json.loads(ledger.path.read_text(encoding="utf-8"))
    changed["result"]["total"] = 13
    ledger.path.write_text(json.dumps(changed) + "\n", encoding="utf-8")

    result = audit_report(report, ledger)
    assert not result["verified"]
    assert any("hash mismatch" in error for error in result["errors"])


@pytest.mark.parametrize(
    ("text", "verified"),
    [
        ("Recorded on 2024-01-01.\nRecorded on 2025-01-01.", True),
        ("Recorded on 2024-01-01 with 999 events.", False),
        ("x2024-01-01", False),
        ("2024-01-01x", False),
        ("2024-01-01-9", False),
        ("Recorded on 2030-01-01.", False),
    ],
)
def test_large_label_catalog_preserves_exact_boundaries(tmp_path, text, verified):
    ledger = EvidenceLedger(tmp_path)
    ledger.record(
        "local_sql_engine",
        "SELECT date FROM events",
        {
            "rows": [
                {"date": (date(2024, 1, 1) + timedelta(days=day)).isoformat()} for day in range(700)
            ]
        },
    )

    result = audit_report(AnalystReport(report=text, claims=[]), ledger)

    assert result["verified"] is verified
