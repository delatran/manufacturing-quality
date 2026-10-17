"""Current saved work exports remain local, sealed, versioned and available without AI."""

import json

import pytest
from analyst.app import create_app
from analyst.config import Settings
from analyst.evidence import EvidenceLedger, sha256
from fastapi.testclient import TestClient
from test_dashboard import service


@pytest.fixture
def workspace(tmp_path):
    settings = Settings(
        mode="auto", api_key="", state_dir=str(tmp_path / "state"), memory_enabled=False
    )
    app = create_app(settings, analysis_service=service(tmp_path))
    with TestClient(app) as client:
        sid = client.post("/api/sessions").json()["id"]
        yield client, sid, tmp_path / "state"


def export_url(sid, version, language="en"):
    return f"/api/sessions/{sid}/investigation/export.json?version={version}&language={language}"


def test_current_saved_version_exports_exact_text_and_scope(workspace):
    client, sid, _ = workspace
    scope = client.get("/api/dashboard?production_line=Line-1&scenario=exclude_cost_10").json()
    scoped = client.post(
        f"/api/sessions/{sid}/scope", json={"context_id": scope["context"]["context_id"]}
    ).json()
    saved = client.patch(
        f"/api/sessions/{sid}/investigation",
        json={
            "expected_version": scoped["version"],
            "question": "Review current repairs",
            "summary": "A user-authored draft needing review.",
            "priorities": [
                {
                    "evidence": "Review the retained records.",
                    "action": "Validate costs.",
                    "owner": "Quality Engineer (proposed)",
                    "metric": "Cost coverage",
                }
            ],
        },
    ).json()
    response = client.get(export_url(sid, saved["version"]))
    assert response.status_code == 200
    assert response.json()["investigation"] == saved
    assert saved["scope"] == scope["context"]["filters"]
    assert saved["dataset_version"] == scope["context"]["dataset_version"]
    assert saved["policy_version"] == "canonical-v2"
    assert response.json()["investigation"]["ui"]["context_id"] == scope["context"]["context_id"]
    assert response.headers["x-investigation-version"] == str(saved["version"])
    assert response.headers["etag"] == '"' + sha256(response.content) + '"'
    assert "attachment" in response.headers["content-disposition"]


def test_stale_export_version_returns_conflict_after_an_edit(workspace):
    client, sid, _ = workspace
    client.patch(
        f"/api/sessions/{sid}/investigation", json={"expected_version": 0, "summary": "Revised."}
    )
    response = client.get(export_url(sid, 0))
    assert response.status_code == 409 and "version changed" in response.json()["detail"]


def test_export_does_not_require_ai_or_create_a_model_run(workspace):
    client, sid, _ = workspace
    assert client.get("/api/config").json()["ai_available"] is False
    assert client.get(export_url(sid, 0)).status_code == 200
    assert client.get(f"/api/sessions/{sid}").json()["runs"] == []


def test_empty_investigation_exports_an_honest_unverified_packet(workspace):
    client, sid, _ = workspace
    payload = client.get(export_url(sid, 0)).json()
    assert payload["investigation"]["findings"] == []
    assert payload["investigation"]["content_status"] == "unverified"
    assert payload["export"]["schema"] == "investigation-export-v1"
    assert payload["export"]["value_locale"] == "JSON number notation"


def test_repeat_download_reuses_the_registered_bytes_and_ledger(workspace):
    client, sid, root = workspace
    first = client.get(export_url(sid, 0))
    second = client.get(export_url(sid, 0))
    assert first.content == second.content and first.headers["etag"] == second.headers["etag"]
    ledger = EvidenceLedger(root / "investigation_exports" / sid / "version-0-en")
    assert len(ledger.records()) == len(ledger.artifacts()) == 1
    assert ledger.verify_integrity() == []
    artifact = ledger.artifacts()[0]
    assert artifact["sha256"] == sha256(first.content)


def test_export_keeps_sessions_separate_and_locale_explicit(workspace):
    client, sid, _ = workspace
    other = client.post("/api/sessions").json()["id"]
    client.patch(
        f"/api/sessions/{sid}/investigation",
        json={"expected_version": 0, "summary": "Private draft text."},
    )
    en = client.get(export_url(sid, 1, "en")).json()
    vi = client.get(export_url(sid, 1, "vi")).json()
    assert en["investigation"] == vi["investigation"]
    assert en["export"]["report_number_format"] == "en-US"
    assert vi["export"]["report_number_format"] == "vi-VN"
    assert client.get(export_url(other, 0)).json()["investigation"]["summary"] == ""
    assert client.get(export_url(sid, 1, "fr")).status_code == 422


def test_tampered_registered_export_is_not_silently_resealed(workspace):
    client, sid, root = workspace
    assert client.get(export_url(sid, 0)).status_code == 200
    target = (
        root
        / "investigation_exports"
        / sid
        / "version-0-en"
        / "artifacts"
        / "investigation-v0-en.json"
    )
    target.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    response = client.get(export_url(sid, 0))
    assert response.status_code == 409 and "registration" in response.json()["detail"]
