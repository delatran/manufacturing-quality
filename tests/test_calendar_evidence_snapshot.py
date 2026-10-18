"""Calendar preparation remains confined to one fresh, independently checked audit."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from analyst.claim_feedback import calendar_text_checker, typed_calendar_text
from analyst.evidence import EvidenceLedger
from analyst.runtime import AnalystReport, audit_report


def calendar_entry(year):
    return {
        "tool": "analysis_query",
        "execution_kind": "bounded_analysis",
        "result": {
            "ok": True,
            "context": {
                "context_id": f"context-{year}",
                "snapshot_id": "synthetic-snapshot",
                "dataset_version": "synthetic-dataset",
                "policy_version": "synthetic-policy",
                "scenario": "",
                "filters": {
                    "start_month": f"{year}-01",
                    "end_month": f"{year}-03",
                    "scenario": "",
                },
            },
        },
    }


def record_period(ledger, year):
    entry = calendar_entry(year)
    return ledger.record(
        entry["tool"],
        "# Synthetic frozen period, not observed case-study data",
        entry["result"],
        execution_kind=entry["execution_kind"],
    )


def test_prepared_calendar_is_an_independent_snapshot_and_new_check_is_fresh():
    entries = {"query": calendar_entry(2024)}
    check = calendar_text_checker(entries)
    assert not check("Jan–Mar 2024")[1]

    entries["query"]["result"]["context"]["filters"]["end_month"] = "2024-06"
    assert not check("Jan–Mar 2024")[1]
    assert check("Jan–Jun 2024")[1]
    assert typed_calendar_text("Jan–Mar 2024", entries)[1]
    assert not typed_calendar_text("Jan–Jun 2024", entries)[1]


def test_new_report_rejects_a_yearless_range_made_ambiguous_by_appended_evidence(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    record_period(ledger, 2024)
    report = AnalystReport(report="Tháng 1–3", claims=[])
    assert audit_report(report, ledger)["verified"]

    record_period(ledger, 2025)
    result = audit_report(report, ledger)
    assert not result["verified"]
    assert result["numeric_issues"][0]["available_periods"] == [
        ["2024-01", "2024-03"],
        ["2025-01", "2025-03"],
    ]


def test_calendar_preparation_does_not_hide_hash_failure_after_disk_tampering(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    record_period(ledger, 2024)
    report = AnalystReport(report="Jan–Mar 2024", claims=[])
    assert audit_report(report, ledger)["verified"]

    records = ledger.records()
    records[0]["result"]["context"]["filters"]["end_month"] = "2024-06"
    ledger.path.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
    result = audit_report(AnalystReport(report="Jan–Jun 2024", claims=[]), ledger)
    assert not result["verified"]
    assert any("hash mismatch" in error for error in result["errors"])


@pytest.mark.parametrize("shared_checker", [False, True])
def test_concurrent_calendar_checks_keep_periods_text_and_errors_separate(tmp_path, shared_checker):
    ledgers = {year: EvidenceLedger(tmp_path / str(year)) for year in (2024, 2025)}
    for year, ledger in ledgers.items():
        record_period(ledger, year)
    checkers = {year: calendar_text_checker({"query": calendar_entry(year)}) for year in ledgers}
    cases = [
        (2024, "Jan–Mar 2024", True),
        (2024, "Jan–Mar 2025", False),
        (2025, "Jan–Mar 2025", True),
        (2025, "Event count is Jan–Mar 2025", False),
        (2024, "Tháng 1–3 sự kiện", False),
    ] * 8

    def verify(case):
        year, text, valid = case
        if shared_checker:
            _, issues = checkers[year](text)
            return (not issues) is valid
        report = AnalystReport(report=text, claims=[])
        return audit_report(report, ledgers[year])["verified"] is valid

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(verify, cases))
    assert all(not checker("Tháng 1–3")[1] for checker in checkers.values())
