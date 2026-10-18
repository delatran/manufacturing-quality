"""Semantic QA receives frozen raw-field inventory without another model call."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from analyst.config import Settings
from analyst.evidence import EvidenceLedger, canonical, sha256
from analyst.runtime import AnalystReport, AnalystRuntime, source_schema_evidence
from test_runtime import canonical_context


def runtime_and_source(tmp_path, *, schema=None):
    payload = canonical_context(tmp_path)
    payload["analysis"]["metadata"]["source_schema"] = (
        {
            "container": "array",
            "row_count": 15,
            "required_fields": ["defect_id", "product_id"],
            "observed_field_types": {
                "defect_id": ["str"],
                "product_id": ["str"],
                "severity": ["str", "NoneType"],
                "optional_machine_id": ["str"],
            },
        }
        if schema is None
        else schema
    )
    # Required master fields are not a complete raw master-field inventory.
    payload["analysis"]["metadata"]["master_schema"] = {
        "required_fields": ["product_id"],
        "row_count": 10,
    }
    original = payload["evidence"]
    payload["evidence"] = EvidenceLedger(tmp_path / "new-service-ledger").record(
        "canonical_analysis", original["code"], payload["analysis"], original["sources"]
    )
    run_dir = tmp_path / "run"
    runtime = AnalystRuntime(
        Settings(mode="mock", memory_enabled=False),
        SimpleNamespace(
            ledger=EvidenceLedger(run_dir), run_dir=run_dir, input_paths=[], schemas=[]
        ),
        None,
        analysis_context=payload,
    )
    entry = runtime.import_dashboard_receipt(payload, "dashboard_context")
    return runtime, entry


def reseal(ledger, entry):
    entry["result_sha256"] = sha256(canonical(entry["result"]))
    entry["entry_sha256"] = sha256(
        canonical({key: value for key, value in entry.items() if key != "entry_sha256"})
    )
    ledger.path.write_text(canonical(entry) + "\n", encoding="utf-8")


def test_qa_receives_complete_frozen_http_inventory_without_querying_or_inventing_fields(tmp_path):
    runtime, entry = runtime_and_source(tmp_path)
    before = runtime.toolkit.ledger.path.read_bytes()
    packet, errors = runtime.qa_evidence(AnalystReport(report="Source field inventory.", claims=[]))
    assert not errors
    assert runtime.toolkit.ledger.path.read_bytes() == before
    assert runtime.tool_calls == 0
    assert len(packet["source_schemas"]) == 1
    inventory = packet["source_schemas"][0]
    assert inventory["coverage"] == "complete_observed_top_level_fields"
    assert inventory["row_count"] == 15
    assert inventory["context"] == entry["analysis_context"]
    assert inventory["source_sha256"] == entry["result"]["metadata"]["source_sha256"]
    assert inventory["path"] == "$.metadata.source_schema"
    for key in ("execution_id", "entry_sha256", "code_sha256", "result_sha256"):
        assert inventory[key] == entry[key]
    assert {item["name"]: item["types"] for item in inventory["columns"]} == entry["result"][
        "metadata"
    ]["source_schema"]["observed_field_types"]
    assert "optional_machine_id" in canonical(inventory)
    assert "product master's complete schema" in inventory["scope_note"]
    assert "ok" not in entry["result"]  # Canonical service results have no ok wrapper.


@pytest.mark.parametrize(
    "schema",
    [
        {},
        "untrusted schema text",
        {"required_fields": ["defect_id", "product_id"], "row_count": 15},
        {"container": "array", "row_count": True, "observed_field_types": {"x": ["str"]}},
        {"container": "array", "row_count": 0, "observed_field_types": {"x": ["str"]}},
        {"container": "array", "row_count": 15, "observed_field_types": {"x": []}},
        {"container": "array", "row_count": 15, "observed_field_types": {"x": [123]}},
    ],
)
def test_required_normalized_or_incomplete_schema_is_not_promoted_to_full_raw_inventory(
    tmp_path, schema
):
    runtime, entry = runtime_and_source(tmp_path, schema=schema)
    assert source_schema_evidence([entry], runtime.toolkit.ledger) == []


@pytest.mark.parametrize(
    "change",
    ["failed", "wrong_tool", "unbounded", "no_upstream", "foreign_source", "wrong_upstream"],
)
def test_even_resealed_wrong_source_or_failed_entries_cannot_supply_frozen_inventory(
    tmp_path, change
):
    runtime, original = runtime_and_source(tmp_path)
    entry = deepcopy(original)
    if change == "failed":
        entry["result"]["ok"] = False
    elif change == "wrong_tool":
        entry["tool"] = "analysis_query"
    elif change == "unbounded":
        entry["execution_kind"] = "local_execution"
    elif change == "no_upstream":
        del entry["canonical_receipt"]
    elif change == "foreign_source":
        entry["analysis_context"]["source_sha256"] = "different-source"
    else:
        entry["canonical_receipt"]["execution_id"] = "different-upstream"
    reseal(runtime.toolkit.ledger, entry)
    assert not runtime.toolkit.ledger.verify_integrity()
    assert source_schema_evidence([entry], runtime.toolkit.ledger) == []


@pytest.mark.parametrize("change", ["code", "result", "entry", "chain"])
def test_tampered_ledger_cannot_supply_raw_field_inventory(tmp_path, change):
    runtime, original = runtime_and_source(tmp_path)
    entry = deepcopy(original)
    if change == "code":
        entry["code"] += "\n# drift"
    elif change == "result":
        entry["result"]["metadata"]["source_schema"]["observed_field_types"]["fake"] = ["str"]
    elif change == "entry":
        entry["analysis_context"]["source_sha256"] = "different-source"
    else:
        entry["previous_sha256"] = "foreign-ledger"
    runtime.toolkit.ledger.path.write_text(canonical(entry) + "\n", encoding="utf-8")
    assert runtime.toolkit.ledger.verify_integrity()
    assert source_schema_evidence([entry], runtime.toolkit.ledger) == []


def test_foreign_or_unretained_receipt_cannot_supply_raw_field_inventory(tmp_path):
    runtime, entry = runtime_and_source(tmp_path)
    assert source_schema_evidence([entry]) == []
    assert source_schema_evidence([entry], EvidenceLedger(tmp_path / "other-run")) == []
    changed = deepcopy(entry)
    changed["source_status"] = {"stale": True}
    assert source_schema_evidence([changed], runtime.toolkit.ledger) == []


def test_existing_dataset_profiler_evidence_keeps_its_limited_column_contract(tmp_path):
    entry = EvidenceLedger(tmp_path).record(
        "dataset_profiler", "# profile", {"datasets": {"uploaded.csv": {"columns": {"x": "str"}}}}
    )
    assert source_schema_evidence([entry]) == [
        {
            "execution_id": entry["execution_id"],
            "name": "uploaded.csv",
            "columns": [{"name": "x", "dtype": "str"}],
        }
    ]
