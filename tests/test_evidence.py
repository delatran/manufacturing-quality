import json

import pytest
from analyst.evidence import EvidenceLedger


def test_numeric_object_keys_hash_the_normalized_json_result(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    record = ledger.record("python", "result = {2: 2, 10: 10}", {2: 2, 10: 10})
    assert record["result"] == {"2": 2, "10": 10}
    assert ledger.verify_integrity() == []
    assert ledger.verify_claims(
        [
            {
                "execution_id": record["execution_id"],
                "path": "$.10",
                "value": 10,
                "code_sha256": record["code_sha256"],
            }
        ]
    )["verified"]


def test_claims_require_exact_execution_and_hash(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    record = ledger.record("python", "result={'total':12}", {"total": 12})
    claim = {
        "execution_id": record["execution_id"],
        "path": "$.total",
        "value": 12,
        "code_sha256": record["code_sha256"],
    }
    assert ledger.verify_claims([claim])["verified"]
    assert not ledger.verify_claims([{**claim, "value": 13}])["verified"]
    assert not ledger.verify_claims([{**claim, "path": "$.invented"}])["verified"]
    assert not ledger.verify_claims([{**claim, "code_sha256": "fake"}])["verified"]
    assert not ledger.verify_claims([{**claim, "value": 999, "tolerance": float("nan")}])[
        "verified"
    ]
    assert not ledger.verify_claims(
        [{key: value for key, value in claim.items() if key != "code_sha256"}]
    )["verified"]
    failed = ledger.record(
        "python", "broken", {"ok": False, "value": {"total": 42}, "error": "failure"}
    )
    assert not ledger.verify_claims(
        [
            {
                "execution_id": failed["execution_id"],
                "path": "$.value.total",
                "value": 42,
                "code_sha256": failed["code_sha256"],
            }
        ]
    )["verified"]


def test_tampering_invalidates_claims_and_artifacts(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    ledger.record("sql", "SELECT 1", {"count": 1})
    entry = json.loads(ledger.path.read_text())
    entry["result"]["count"] = 2
    ledger.path.write_text(json.dumps(entry) + "\n")
    assert ledger.verify_integrity()
    artifact = tmp_path / "artifacts" / "report.txt"
    artifact.parent.mkdir()
    artifact.write_text("report")
    registered = ledger.register_artifact(artifact)
    assert ledger.resolve_artifact(registered["path"]) == artifact
    artifact.write_text("tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        ledger.resolve_artifact(registered["path"])
    with pytest.raises(FileNotFoundError):
        ledger.resolve_artifact("../secret")


def test_manifest_verification_rechecks_every_artifact(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    artifacts = [artifact_root / f"report-{index}.txt" for index in range(3)]
    for artifact in artifacts:
        artifact.write_text("verified", encoding="utf-8")
        ledger.register_artifact(artifact)
    ledger.verify_artifacts()
    artifacts[-1].write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        ledger.verify_artifacts()
    artifacts[-1].write_text("verified", encoding="utf-8")
    artifacts[0].unlink()
    with pytest.raises(FileNotFoundError):
        ledger.verify_artifacts()


def test_manifest_verification_rejects_escaped_and_duplicate_paths(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    artifact = tmp_path / "artifacts" / "report.txt"
    artifact.parent.mkdir()
    artifact.write_text("verified", encoding="utf-8")
    entry = ledger.register_artifact(artifact)
    with pytest.raises(ValueError, match="duplicate path"):
        ledger.verify_artifacts(manifest=[entry, entry])
    outside = tmp_path / "outside.txt"
    outside.write_text("verified", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        ledger.verify_artifacts(manifest=[{**entry, "path": "artifacts/../outside.txt"}])
    ledger.verify_artifacts(manifest=[entry])
