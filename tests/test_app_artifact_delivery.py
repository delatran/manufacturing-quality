"""Final artifact selection survives app events and disposable session replay."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest
from analyst.app import Workspace
from analyst.config import Settings
from analyst.store import SessionStore


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    def disallow_client(*args, **kwargs):
        raise AssertionError("Artifact delivery tests must not create an API client")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", disallow_client)
    settings = Settings(
        root=tmp_path,
        state_dir=str(tmp_path / "state"),
        mode="mock",
        memory_enabled=False,
        enable_file_search=False,
    )
    workspace = Workspace(settings, analysis_service=SimpleNamespace())
    yield workspace
    asyncio.run(workspace.close())


def artifact(digest="a" * 64, **changes):
    return {
        "path": "artifacts/summary.csv",
        "name": "summary.csv",
        "sha256": digest,
        "size_bytes": 24,
        "mime_type": "text/csv",
        "kind": "table",
        **changes,
    }


def packet(artifacts):
    return {
        "version": 1,
        "artifacts": artifacts,
        "source_execution_ids": ["execution_synthetic"],
        "scope": {"production_line": "Synthetic line", "filters": {"category": None}},
    }


def report(**changes):
    return {
        "report": "Synthetic final report.",
        "claims": [],
        "qa": {"verified": True, "errors": []},
        "simulation": True,
        **changes,
    }


def test_selected_artifact_metadata_survives_stream_and_durable_replay(workspace):
    sid = workspace.store.create_session()["id"]
    rid, _ = workspace.store.begin_run(sid, language="vi")
    queue = asyncio.Queue()
    workspace.listeners[sid] = {queue}
    original = report(delivery=packet([artifact(url="https://untrusted.invalid/file")]))
    before = deepcopy(original)

    event = asyncio.run(workspace.emit(sid, rid, "report", original))

    expected = deepcopy(original["delivery"])
    expected["artifacts"][0]["url"] = (
        f"/api/sessions/{sid}/runs/{rid}/artifacts/artifacts/summary.csv"
    )
    assert original == before
    assert event["type"] == "message"
    assert event["run_id"] == rid
    assert event["data"]["delivery"] == expected
    assert event["data"]["language"] == "vi"
    assert queue.get_nowait() == event
    assert workspace.store.session(sid)["events"] == [event]

    restored = SessionStore(workspace.state_dir)
    try:
        assert restored.session(sid)["events"] == [event]
    finally:
        restored.close()


def test_explicit_empty_selection_survives_failed_qa_without_inventory_fallback(workspace):
    sid = workspace.store.create_session()["id"]
    rid, _ = workspace.store.begin_run(sid)
    candidate = asyncio.run(
        workspace.emit(
            sid,
            rid,
            "artifact",
            {
                **artifact(),
                "delivery_candidate": True,
            },
        )
    )
    payload = report(
        delivery=packet([]),
        artifacts=[artifact()],
        qa={"verified": False, "errors": ["Synthetic unresolved claim"]},
    )
    payload["delivery"]["scope"] = None
    before = deepcopy(payload)

    final = asyncio.run(workspace.emit(sid, rid, "report", payload))

    assert payload == before
    assert final["data"]["delivery"] == payload["delivery"]
    assert final["data"]["delivery"]["artifacts"] == []
    events = workspace.store.session(sid)["events"]
    assert [event["type"] for event in events] == ["artifact", "qa", "message"]
    assert events[0] == candidate
    assert events[1]["data"]["status"] == "unverified"
    assert events[-1]["data"]["qa"] == payload["qa"]


@pytest.mark.parametrize(
    "delivery", [None, False, [], "invalid", {}, {"version": 1, "artifacts": "invalid"}]
)
def test_malformed_present_packet_remains_distinct_from_legacy(workspace, delivery):
    sid = workspace.store.create_session()["id"]
    rid, _ = workspace.store.begin_run(sid)
    event = asyncio.run(workspace.emit(sid, rid, "report", report(delivery=delivery)))
    assert "delivery" in event["data"]
    assert event["data"]["delivery"] == delivery
    assert workspace.store.session(sid)["events"][-1] == event


def test_legacy_report_does_not_gain_an_authoritative_selection(workspace):
    sid = workspace.store.create_session()["id"]
    rid, _ = workspace.store.begin_run(sid)
    model_history = [{"role": "assistant", "content": "Synthetic earlier response."}]
    workspace.store.save_history(sid, model_history)
    event = asyncio.run(workspace.emit(sid, rid, "report", report(artifacts=[artifact()])))
    assert "delivery" not in event["data"]
    assert workspace.store.history(sid) == model_history


@pytest.mark.parametrize(
    "path_kind", ["parent", "other_run", "absolute_outside", "missing", "not_string"]
)
def test_unsafe_selected_path_never_receives_or_retains_a_served_url(workspace, path_kind):
    sid = workspace.store.create_session()["id"]
    rid, run_root = workspace.store.begin_run(sid)
    paths = {
        "parent": "../outside.csv",
        "other_run": "../another_run/artifacts/summary.csv",
        "absolute_outside": str(run_root.parent / "outside.csv"),
        "missing": "",
        "not_string": ["artifacts/summary.csv"],
    }
    selected = artifact(path=paths[path_kind], url="https://untrusted.invalid/file")
    payload = report(delivery=packet([selected]))
    before = deepcopy(payload)
    event = asyncio.run(workspace.emit(sid, rid, "report", payload))
    delivered = event["data"]["delivery"]["artifacts"][0]
    assert delivered["path"] == selected["path"]
    assert delivered["sha256"] == selected["sha256"]
    assert "url" not in delivered
    assert payload == before


def test_selection_is_bound_to_owning_run_even_when_names_repeat(workspace):
    sid = workspace.store.create_session()["id"]
    finals = []
    for digest in ("a" * 64, "b" * 64):
        rid, _ = workspace.store.begin_run(sid)
        final = asyncio.run(
            workspace.emit(sid, rid, "report", report(delivery=packet([artifact(digest)])))
        )
        selected = final["data"]["delivery"]["artifacts"][0]
        assert selected["sha256"] == digest
        assert selected["url"] == f"/api/sessions/{sid}/runs/{rid}/artifacts/artifacts/summary.csv"
        finals.append(final)
    assert finals[0]["run_id"] != finals[1]["run_id"]
    assert workspace.store.session(sid)["events"] == finals


def test_new_run_and_candidate_markers_survive_stream_and_session_replay(workspace):
    sid = workspace.store.create_session()["id"]
    rid, _ = workspace.store.begin_run(sid)
    queue = asyncio.Queue()
    workspace.listeners[sid] = {queue}
    started = asyncio.run(
        workspace.emit(
            sid,
            rid,
            "run_started",
            {
                "artifact_delivery_version": 1,
                "simulation": True,
            },
        )
    )
    candidate = asyncio.run(
        workspace.emit(
            sid,
            rid,
            "artifact",
            {
                **artifact(),
                "delivery_candidate": True,
            },
        )
    )
    assert started["type"] == "run_started"
    assert started["data"]["artifact_delivery_version"] == 1
    assert candidate["data"]["delivery_candidate"] is True
    assert [queue.get_nowait(), queue.get_nowait()] == [started, candidate]
    assert workspace.store.session(sid)["events"] == [started, candidate]


@pytest.mark.parametrize(
    "filename",
    ["báo cáo #1.csv", "cost 100%.csv", "literal %23.csv", "inspection notes.csv", "kiểm tra.csv"],
)
def test_artifact_urls_preserve_exact_filename_after_one_url_decode(workspace, filename):
    sid = workspace.store.create_session()["id"]
    rid, _ = workspace.store.begin_run(sid)
    path = f"artifacts/{filename}"
    selected = artifact(path=path, name=filename)
    original = report(
        report=f"[Download]({path}) and [Named link]({filename})",
        artifacts=[selected],
        delivery=packet([selected]),
    )
    before = deepcopy(original)

    candidate = asyncio.run(
        workspace.emit(
            sid,
            rid,
            "artifact",
            {
                **selected,
                "delivery_candidate": True,
            },
        )
    )
    final = asyncio.run(workspace.emit(sid, rid, "report", original))

    url = final["data"]["delivery"]["artifacts"][0]["url"]
    parts = urlsplit(url)
    prefix = f"/api/sessions/{sid}/runs/{rid}/artifacts/"
    assert parts.scheme == parts.netloc == parts.query == parts.fragment == ""
    assert parts.path.startswith(prefix)
    assert unquote(parts.path[len(prefix) :]) == path
    assert parts.path.isascii()
    assert " " not in parts.path
    assert candidate["data"]["url"] == url
    assert final["data"]["text"] == f"[Download]({url}) and [Named link]({url})"
    assert final["data"]["delivery"]["artifacts"][0]["path"] == path
    assert final["data"]["delivery"]["artifacts"][0]["sha256"] == selected["sha256"]
    assert original == before
    if "%23" in filename:
        assert "%2523" in parts.path
