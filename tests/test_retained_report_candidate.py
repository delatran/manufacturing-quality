"""Offline candidate controls. No provider, server, original DB or network access."""

import ast
import asyncio
import copy
import json
import os
import shutil
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
from analyst.config import Settings
from analyst.evidence import EvidenceLedger
from analyst.product_tools import tool_code
from analyst.runtime import AnalystRuntime
from analyst.store import SessionStore

# app.py's default ASGI object is deliberately not constructed by this offline suite.
_app_path = Path(__file__).parents[1] / "analyst/app.py"
_app_ast = ast.parse(_app_path.read_text(encoding="utf-8"), filename=str(_app_path))
_app_initializers = [
    node
    for node in _app_ast.body
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "app"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "create_app"
        and not node.value.args
        and not node.value.keywords
    )
]
assert len(_app_initializers) == 1, "Exclude exactly the default app=create_app() assignment"
_app_ast.body.remove(_app_initializers[0])
_app_module = ModuleType("analyst._retained_report_candidate")
_app_module.__package__ = "analyst"
_app_module.__file__ = str(_app_path)
sys.modules[_app_module.__name__] = _app_module
exec(compile(_app_ast, str(_app_path), "exec"), _app_module.__dict__)
Workspace = _app_module.Workspace

CANONICAL = "a" * 64
SCENARIO = "b" * 64


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        raise AssertionError("Provider, network and environment reads are forbidden")

    import socket
    import sqlite3

    import agents
    import analyst.config
    import analyst.model
    import openai

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(openai, "OpenAI", blocked)
    monkeypatch.setattr(openai, "AsyncOpenAI", blocked)
    monkeypatch.setattr(analyst.model, "AsyncOpenAI", blocked)
    monkeypatch.setattr(agents.Runner, "run", blocked)
    monkeypatch.setattr(analyst.config, "_dotenv", blocked)
    monkeypatch.setattr(Settings, "from_env", blocked)
    monkeypatch.setattr(Workspace, "__init__", blocked)
    monkeypatch.setattr(_app_module, "create_app", blocked)
    connect = sqlite3.connect

    def temp_connect(database, *args, **kwargs):
        path = Path(database)
        if kwargs.get("uri") and isinstance(database, str) and database.startswith("file:"):
            parsed = urlsplit(database)
            assert parsed.netloc in ("", "localhost"), "Offline SQLite requires a local file"
            path = Path(url2pathname(parsed.path))
        assert path.resolve().is_relative_to(tmp_path.resolve()), (
            "Offline SQLite access must stay inside the test directory"
        )
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", temp_connect)


def test_offline_sqlite_guard_accepts_encoded_read_only_temp_uri(tmp_path):
    import sqlite3

    database = tmp_path / "read only # caf\u00e9.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE sentinel (value INTEGER)")
        connection.execute("INSERT INTO sentinel VALUES (17)")
        connection.commit()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone() == (17,)


@pytest.mark.parametrize("form", ["path", "uri", "encoded_parent_uri"])
def test_offline_sqlite_guard_rejects_outside_database(tmp_path, form):
    import sqlite3

    database = tmp_path.parent / "outside.sqlite3"
    target = database if form == "path" else database.as_uri() + "?mode=ro"
    if form == "encoded_parent_uri":
        target = tmp_path.as_uri() + "/%2e%2e/outside.sqlite3?mode=ro"
    with pytest.raises(AssertionError, match="must stay inside the test directory"):
        sqlite3.connect(target, uri=form != "path")


@pytest.fixture
def workspace(tmp_path):
    subject = Workspace.__new__(Workspace)
    subject.settings = Settings(
        root=tmp_path,
        state_dir=str(tmp_path / "state"),
        mode="mock",
        api_key="",
        memory_enabled=False,
    )
    subject.store = SessionStore(tmp_path / "state")
    subject.ui_lock = asyncio.Lock()
    subject.ui_bindings = {}
    subject.ui_authorities = {}
    subject.analysis = SimpleNamespace(context_by_id=async_context)
    subject.emitted = []

    async def emit(sid, rid, kind, data):
        subject.emitted.append((sid, rid, kind, copy.deepcopy(data)))

    subject.emit = emit
    yield subject
    subject.store.close()


async def async_context(cid):
    assert cid in {CANONICAL, SCENARIO}
    return {
        "context": {
            "context_id": cid,
            "filters": {"scenario": "exclude_cost_10" if cid == SCENARIO else ""},
        }
    }


def complete(coroutine):
    """Drive immediate fake-service coroutines without creating any sockets/event loop."""
    try:
        yielded = coroutine.send(None)
    except StopIteration as stopped:
        return stopped.value
    finally:
        coroutine.close()
    raise AssertionError(f"Offline fake unexpectedly suspended: {yielded!r}")


def report_fixture(subject, sid=None, text="first report", context=SCENARIO):
    sid = sid or subject.store.create_session()["id"]
    rid, directory = subject.store.begin_run(sid, context)
    ledger = EvidenceLedger(directory)
    path = directory / "artifacts/business_report.html"
    path.parent.mkdir()
    path.write_text(text)
    context_metadata = {
        "context_id": context,
        "filters": {"scenario": "exclude_cost_10"},
        "dataset_version": "synthetic",
        "policy_version": "test",
        "scenario": "exclude_cost_10",
        "snapshot_id": "synthetic-snapshot",
    }
    registered = ledger.register_artifact(path, kind="business_report", context=context_metadata)
    manifest = {"contexts": [context_metadata], "sources": [], "references": []}
    scope_path = directory / "artifacts/verification_scope.json"
    scope_path.write_text(
        json.dumps({"delivery": {"source_manifest": manifest, "artifacts": [registered]}})
    )
    ledger.register_artifact(scope_path)
    saved = {
        **registered,
        "run_id": rid,
        "url": "https://untrusted.invalid/ignored",
        "source_manifest": manifest,
    }
    current = subject.store.investigation(sid)
    subject.store.save_investigation(
        sid, current["version"], {"artifacts": [*current["artifacts"], saved]}
    )
    subject.store.finish_run(rid, "completed")
    return sid, rid, directory, saved


def alter_saved(subject, sid, change):
    current = subject.store.investigation(sid)
    artifacts = current["artifacts"]
    change(artifacts[-1])
    subject.store.save_investigation(sid, current["version"], {"artifacts": artifacts})


def undo_fixture(subject, sid):
    subject._bind_ui_scope(sid, complete(async_context(CANONICAL)))
    rid, _ = subject.store.begin_run(sid, SCENARIO)
    original = {
        "command_id": "c" * 32,
        "action": "apply_scope",
        "panel": "evidence",
        "context_id": SCENARIO,
        "previous_context_id": CANONICAL,
        "expected_context_id": CANONICAL,
        "run_context_id": CANONICAL,
        "filters": {"scenario": "exclude_cost_10"},
        "previous_panel": "dashboard",
        "reversible": True,
    }
    subject.store.commit_ui_command(
        sid, rid, original, expected_version=subject.store.investigation(sid)["version"]
    )
    current = subject.store.investigation(sid)
    subject.store.save_investigation(
        sid,
        current["version"],
        {
            "summary": "Keep authored summary",
            "question": "Keep question",
            "findings": [
                {
                    "text": "Historical finding",
                    "context_id": SCENARIO,
                    "status": "supported",
                    "evidence": ["prior-id"],
                }
            ],
            "hypotheses": [{"text": "Keep hypothesis"}],
            "next_steps": ["Keep step"],
            "priorities": ["Keep priority"],
            "content_status": "reviewed",
        },
    )
    subject.ui_bindings[rid] = SCENARIO
    subject.ui_authorities[rid] = {"view": True, "undo": True}
    args = {"action": "undo", "command_id": original["command_id"]}
    return rid, args


def runtime_fixture(subject, sid, tmp_path):
    runtime = AnalystRuntime.__new__(AnalystRuntime)
    runtime.toolkit = SimpleNamespace(session_id=sid, ledger=EvidenceLedger(tmp_path / "current"))
    runtime._analysis_context_json = None
    runtime.context_entry = None
    runtime.claim_catalog = {}
    runtime.case_entry = None
    return runtime


def sealed_ui(runtime, result, tool="ui_command", execution_kind="bounded_analysis", code=None):
    return runtime.toolkit.ledger.record(
        tool, tool_code() if code is None else code, result, execution_kind=execution_kind
    )


def test_verified_metadata_is_historical_read_only_and_ignores_saved_url(workspace):
    sid, rid, _, saved = report_fixture(workspace)
    before = workspace.store.investigation(sid)
    actual = workspace._retained_reports(sid)
    assert workspace.store.investigation(sid) == before
    item = actual["items"][0]
    assert item["status"] == "available"
    assert item["historical_context_ids"] == [SCENARIO]
    assert item["url"] == f"/api/sessions/{sid}/runs/{rid}/artifacts/{saved['path']}"
    assert "untrusted" not in json.dumps(actual)
    assert "sources" not in item and "references" not in item and "scope" not in item


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "tamper",
        "size",
        "hash",
        "context",
        "manifest",
        "traversal",
        "unknown_run",
        "registry",
    ],
)
def test_report_negative_controls(workspace, failure):
    sid, _, directory, _ = report_fixture(workspace)
    path = directory / "artifacts/business_report.html"
    if failure == "missing":
        path.unlink()
    elif failure == "tamper":
        path.write_bytes(b"X" * path.stat().st_size)
    elif failure == "registry":
        (directory / "artifacts.json").write_text("[]")
    else:

        def change(item):
            if failure == "size":
                item["size_bytes"] += 1
            elif failure == "hash":
                item["sha256"] = "f" * 64
            elif failure == "context":
                item["context_id"] = CANONICAL
            elif failure == "manifest":
                item["source_manifest"]["contexts"][0]["context_id"] = CANONICAL
            elif failure == "traversal":
                item["path"] = "artifacts/../business_report.html"
            elif failure == "unknown_run":
                item["run_id"] = "f" * 32

        alter_saved(workspace, sid, change)
    actual = workspace._retained_reports(sid)["items"][0]
    assert actual["status"] == "unavailable"
    assert "url" not in actual


def test_cross_session_report_is_unavailable(workspace):
    _, _, _, saved = report_fixture(workspace)
    other = workspace.store.create_session()["id"]
    workspace.store.save_investigation(other, 0, {"artifacts": [saved]})
    assert workspace._retained_reports(other)["items"][0]["status"] == "unavailable"


def test_duplicate_filenames_keep_owning_run_and_hash(workspace):
    sid, first, _, _ = report_fixture(workspace)
    _, second, _, _ = report_fixture(workspace, sid, "second report")
    items = workspace._retained_reports(sid)["items"]
    assert [item["run_id"] for item in items] == [second, first]
    assert len({item["url"] for item in items}) == 2
    assert len({item["sha256"] for item in items}) == 2


def test_output_bounds_and_empty_metadata(workspace):
    sid = workspace.store.create_session()["id"]
    assert workspace._retained_reports(sid)["items"] == []
    for i in range(8):
        report_fixture(workspace, sid, str(i))
    result = workspace._retained_reports(sid)
    assert len(result["items"]) == 5 and result["omitted_count"] == 3
    assert len(json.dumps(result).encode()) < 16_000


@pytest.mark.parametrize("unexpected", [False, True])
def test_failed_enrichment_preserves_committed_undo_and_authored_state(
    workspace, monkeypatch, unexpected
):
    sid, _, directory, _ = report_fixture(workspace)
    rid, args = undo_fixture(workspace, sid)
    before = workspace.store.investigation(sid)
    if unexpected:

        def fail(_sid):
            raise RuntimeError("Unexpected enrichment failure")

        monkeypatch.setattr(workspace, "_retained_reports", fail)
    else:
        (directory / "artifacts/business_report.html").unlink()
    result = complete(workspace.execute_ui_command(sid, rid, SCENARIO, args))
    after = workspace.store.investigation(sid)
    assert result["command"]["action"] == "undo"
    assert result["command"]["context_id"] == CANONICAL
    assert after["version"] == before["version"] + 1
    for field in [
        "summary",
        "question",
        "hypotheses",
        "next_steps",
        "priorities",
        "content_status",
        "artifacts",
    ]:
        assert after[field] == before[field]
    assert after["findings"] == [{**before["findings"][0], "status": "stale"}]
    assert result["retained_reports"].get("status") == ("unavailable" if unexpected else "checked")
    if not unexpected:
        assert result["retained_reports"]["items"][0]["status"] == "unavailable"
    else:
        assert result["retained_reports"]["error"] == "retained_report_lookup_failed"
        assert result["retained_reports"]["error_type"] == "RuntimeError"
    repeated = complete(workspace.execute_ui_command(sid, rid, SCENARIO, args))
    assert repeated["command"]["command_id"] == result["command"]["command_id"]
    assert workspace.store.investigation(sid) == after
    count = workspace.store.db.execute("SELECT count(*) FROM ui_commands").fetchone()[0]
    assert count == 2


def test_stale_command_and_cas_still_fail_before_enrichment(workspace, monkeypatch):
    sid, _, _, _ = report_fixture(workspace)
    rid, args = undo_fixture(workspace, sid)
    current = workspace.store.investigation(sid)
    workspace.store.save_investigation(
        sid, current["version"], {"ui": {**current["ui"], "context_id": CANONICAL}}
    )
    with pytest.raises(ValueError, match="version changed"):
        workspace.store.save_investigation(sid, current["version"], {"summary": "overwrite"})

    def forbidden(_sid):
        raise AssertionError("A stale command must not reach enrichment")

    monkeypatch.setattr(workspace, "_retained_reports", forbidden)
    with pytest.raises(ValueError, match="stale"):
        complete(workspace.execute_ui_command(sid, rid, SCENARIO, args))
    assert not workspace.store.ui_command(sid, args["command_id"])["undone"]


def test_runtime_accepts_exact_sealed_url_without_delivery_or_claim_import(workspace, tmp_path):
    sid, _, _, _ = report_fixture(workspace)
    runtime = runtime_fixture(workspace, sid, tmp_path)
    retained = workspace._retained_reports(sid)
    sealed_ui(
        runtime, {"ok": True, "command": {"command_id": "c" * 32}, "retained_reports": retained}
    )
    delivery = {"artifacts": []}
    before = copy.deepcopy(delivery)
    previous_claims = copy.deepcopy(runtime.available_claims())
    previous_artifact_registry = runtime.toolkit.ledger.artifacts()
    previous_ledger = runtime.toolkit.ledger.path.read_bytes()
    assert runtime.verified_links(delivery) == [retained["items"][0]["url"]]
    assert delivery == before
    assert runtime.available_claims() == previous_claims == {}
    assert runtime.toolkit.ledger.artifacts() == previous_artifact_registry == []
    assert runtime.toolkit.ledger.path.read_bytes() == previous_ledger


@pytest.mark.parametrize("identity", ["absent", None, "", "standalone", 1])
def test_report_only_runtime_preserves_current_links_without_session_identity(
    workspace, tmp_path, identity
):
    sid, _, _, _ = report_fixture(workspace)
    runtime = runtime_fixture(workspace, sid, tmp_path)
    sealed_ui(
        runtime,
        {
            "ok": True,
            "command": {"command_id": "c" * 32},
            "retained_reports": workspace._retained_reports(sid),
        },
    )
    if identity == "absent":
        del runtime.toolkit.session_id
    else:
        runtime.toolkit.session_id = identity
    source_link = f"/api/dashboard/contexts/{CANONICAL}"
    runtime._analysis_context_json = json.dumps({"links": {"dashboard": source_link}})
    runtime.context_entry = {"execution_id": "existing-context"}
    delivery = {"artifacts": [{"path": "artifacts/current-authorized.txt"}]}
    before = copy.deepcopy(delivery)
    records = runtime.toolkit.ledger.path.read_bytes()
    assert runtime.verified_links(delivery) == sorted(
        [source_link, "artifacts/current-authorized.txt"]
    )
    assert delivery == before and runtime.toolkit.ledger.path.read_bytes() == records


@pytest.mark.parametrize(
    "failure",
    [
        "tool",
        "kind",
        "failed",
        "unavailable",
        "url",
        "session",
        "proof",
        "seal",
        "result_seal",
        "code_seal",
        "count",
        "path",
        "backend_namespace",
        "context",
        "context_duplicate",
        "context_overflow",
        "proof_type",
        "command_id",
        "hash",
        "size_type",
    ],
)
def test_runtime_rejects_untrusted_or_unavailable_links(workspace, tmp_path, failure):
    sid, _, _, _ = report_fixture(workspace)
    runtime = runtime_fixture(workspace, sid, tmp_path)
    result = {
        "ok": True,
        "command": {"command_id": "c" * 32},
        "retained_reports": workspace._retained_reports(sid),
    }
    item = result["retained_reports"]["items"][0]
    if failure == "failed":
        result["ok"] = False
    elif failure == "unavailable":
        item["status"] = "unavailable"
    elif failure == "url":
        item["url"] = "https://attacker.invalid/report"
    elif failure == "session":
        item["session_id"] = "f" * 32
    elif failure == "proof":
        item["verification"]["same_session"] = False
    elif failure == "count":
        result["retained_reports"]["items"] *= 6
    elif failure == "path":
        item["path"] = "artifacts/other.html"
    elif failure == "context":
        item["historical_context_ids"] = ["not-a-context"]
    elif failure == "context_duplicate":
        item["historical_context_ids"] *= 2
    elif failure == "context_overflow":
        item["historical_context_ids"] = [f"{value:064x}" for value in range(17)]
    elif failure == "proof_type":
        item["verification"]["same_session"] = 1
    elif failure == "command_id":
        result["command"]["command_id"] = "forged command"
    elif failure == "hash":
        item["sha256"] = "not-a-sha"
    elif failure == "size_type":
        item["size_bytes"] = True
    sealed_ui(
        runtime,
        result,
        tool="investigation_update" if failure == "tool" else "ui_command",
        execution_kind="other" if failure == "kind" else "bounded_analysis",
        code="user supplied code" if failure == "backend_namespace" else None,
    )
    if failure in {"seal", "result_seal", "code_seal"}:
        entry = runtime.toolkit.ledger.records()[0]
        key = {"seal": "entry_sha256", "result_seal": "result_sha256", "code_seal": "code_sha256"}[
            failure
        ]
        entry[key] = "f" * 64
        if failure != "seal":
            from analyst.evidence import canonical, sha256

            entry["entry_sha256"] = sha256(
                canonical({key: value for key, value in entry.items() if key != "entry_sha256"})
            )
        runtime.toolkit.ledger.path.write_text(json.dumps(entry) + "\n")
    assert runtime.verified_links({"artifacts": []}) == []


def test_only_exact_default_app_assignment_was_excluded():
    assert len(_app_initializers) == 1
    assert callable(_app_module.create_app) and callable(Workspace.execute_ui_command)
    assert not hasattr(_app_module, "app")
    for name, module in sys.modules.copy().items():
        if name == "analyst" or name.startswith("analyst."):
            path = getattr(module, "__file__", None)
            if path:
                assert Path(path).resolve().is_relative_to(_app_path.parents[1].resolve())


def test_runtime_rechecks_the_same_receipt_snapshot_seals(workspace, tmp_path, monkeypatch):
    sid, _, _, _ = report_fixture(workspace)
    runtime = runtime_fixture(workspace, sid, tmp_path)
    sealed_ui(
        runtime,
        {
            "ok": True,
            "command": {"command_id": "c" * 32},
            "retained_reports": workspace._retained_reports(sid),
        },
    )

    def changed_after_integrity_read():
        # Emulate bytes changing between the whole-ledger integrity read and
        # the later receipt read. Validate the actual consumed seals as well.
        entry = runtime.toolkit.ledger.records()[0]
        item = entry["result"]["retained_reports"]["items"][0]
        item["run_id"] = "f" * 32
        item["url"] = f"/api/sessions/{sid}/runs/{'f' * 32}/artifacts/{item['path']}"
        runtime.toolkit.ledger.path.write_text(json.dumps(entry) + "\n")
        return []

    monkeypatch.setattr(runtime.toolkit.ledger, "verify_integrity", changed_after_integrity_read)
    assert runtime.verified_links({"artifacts": []}) == []


@pytest.mark.parametrize(
    "failure",
    [
        "physical_size",
        "context_id",
        "context_scope",
        "context_dataset",
        "context_policy",
        "context_scenario",
        "manifest_missing",
        "manifest_overflow",
        "duplicate_registry",
        "malformed_registry",
        "manifest_context_overflow",
    ],
)
def test_original_manifest_and_physical_proof_controls(workspace, failure):
    sid, _, directory, saved = report_fixture(workspace)
    ledger = EvidenceLedger(directory)
    if failure == "malformed_registry":
        ledger.manifest_path.write_text(json.dumps([None]))
    elif failure == "duplicate_registry":
        registered = ledger.artifacts()
        ledger.manifest_path.write_text(json.dumps([*registered, registered[0]]))
    elif failure == "manifest_missing":
        (directory / "artifacts/verification_scope.json").unlink()
    elif failure == "manifest_overflow":
        scope_path = directory / "artifacts/verification_scope.json"
        scope_path.write_text(json.dumps({"padding": "x" * 1_000_001}))
        ledger.register_artifact(scope_path)
    else:
        # Re-register self-consistent saved/registry bytes. The independent physical
        # size check or original delivery/context relationship must still reject it.
        registered = ledger.artifacts()
        report = registered[0]
        manifest = copy.deepcopy(saved["source_manifest"])
        if failure == "physical_size":
            report["size_bytes"] += 1
        elif failure == "context_id":
            report["context_id"] = CANONICAL
        elif failure.startswith("context_"):
            key = {
                "context_scope": "scope",
                "context_dataset": "dataset_version",
                "context_policy": "policy_version",
                "context_scenario": "scenario",
            }[failure]
            report[key] = {"scenario": ""} if key == "scope" else "changed"
        else:
            manifest["contexts"] = [
                {**manifest["contexts"][0], "context_id": f"{i:064x}"} for i in range(17)
            ]
        ledger.manifest_path.write_text(json.dumps(registered))
        scope_path = directory / "artifacts/verification_scope.json"
        scope_path.write_text(
            json.dumps({"delivery": {"artifacts": [report], "source_manifest": manifest}})
        )
        ledger.register_artifact(scope_path)
        alter_saved(workspace, sid, lambda item: item.update(report, source_manifest=manifest))
    assert workspace._retained_reports(sid)["items"][0]["status"] == "unavailable"


def test_malformed_report_does_not_hide_independent_valid_report(workspace):
    sid, bad_run, directory, _ = report_fixture(workspace)
    _, good_run, _, _ = report_fixture(workspace, sid, "available report")
    (directory / "artifacts.json").write_text(json.dumps([None]))
    items = workspace._retained_reports(sid)["items"]
    assert [(item["run_id"], item["status"]) for item in items] == [
        (good_run, "available"),
        (bad_run, "unavailable"),
    ]


def test_symlink_escape_is_unavailable(workspace, tmp_path):
    sid, _, directory, _ = report_fixture(workspace)
    path = directory / "artifacts/business_report.html"
    outside = tmp_path / "outside-report.html"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"OS disallows synthetic symlinks: {error}")
    assert workspace._retained_reports(sid)["items"][0]["status"] == "unavailable"


def test_resolved_escape_is_rejected_before_resolver_reads(workspace, tmp_path, monkeypatch):
    sid, _, directory, _ = report_fixture(workspace)
    selected = directory / "artifacts/business_report.html"
    outside = tmp_path / "outside-report.html"
    outside.write_bytes(selected.read_bytes())
    resolve = Path.resolve

    def resolved_target(path, *args, **kwargs):
        # Simulate only the filesystem resolution result. The producer's
        # anchored containment check must reject it before resolver reads.
        if path == selected:
            return outside
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolved_target)
    assert workspace._retained_reports(sid)["items"][0]["status"] == "unavailable"


def test_comparison_without_single_registered_context_stays_historical(workspace):
    sid, _, directory, saved = report_fixture(workspace)
    ledger = EvidenceLedger(directory)
    registered = ledger.artifacts()
    report = registered[0]
    for key in ["context_id", "scope", "dataset_version", "policy_version", "scenario"]:
        report.pop(key)
    manifest = copy.deepcopy(saved["source_manifest"])
    manifest["contexts"].append(
        {
            **manifest["contexts"][0],
            "context_id": CANONICAL,
            "filters": {"scenario": ""},
            "scenario": "",
        }
    )
    ledger.manifest_path.write_text(json.dumps(registered))
    scope_path = directory / "artifacts/verification_scope.json"
    scope_path.write_text(
        json.dumps({"delivery": {"artifacts": [report], "source_manifest": manifest}})
    )
    ledger.register_artifact(scope_path)

    def update(item):
        for key in ["context_id", "scope", "dataset_version", "policy_version", "scenario"]:
            item.pop(key)
        item.update(source_manifest=manifest)

    alter_saved(workspace, sid, update)
    item = workspace._retained_reports(sid)["items"][0]
    assert item["status"] == "available"
    assert item["historical_context_ids"] == [CANONICAL, SCENARIO]


def test_open_command_reads_only_server_retained_reports(workspace):
    sid, historical_run, _, saved = report_fixture(workspace)
    rid, _ = workspace.store.begin_run(sid, SCENARIO)
    current = workspace.store.investigation(sid)
    workspace.store.save_investigation(
        sid,
        current["version"],
        {"ui": {**current["ui"], "context_id": SCENARIO}, "summary": "Keep summary"},
    )
    workspace.ui_bindings[rid] = SCENARIO
    workspace.ui_authorities[rid] = {"view": True}
    result = complete(
        workspace.execute_ui_command(
            sid,
            rid,
            SCENARIO,
            {
                "action": "open",
                "panel": "evidence",
                "filters": {"scenario": None},
                "command_id": None,
                "run_id": "f" * 32,
                "path": "https://attacker.invalid/ignored",
            },
        )
    )
    assert result["command"]["action"] == "open"
    assert result["command"]["context_id"] == SCENARIO
    assert result["retained_reports"]["items"][0]["run_id"] == historical_run
    assert workspace.store.investigation(sid)["summary"] == "Keep summary"
    assert workspace.store.investigation(sid)["artifacts"] == [saved]
    assert "attacker.invalid" not in json.dumps(result)


@pytest.mark.parametrize("missing", ["run", "runs", "session", "sessions"])
def test_missing_run_roots_do_not_recreate_directories_after_committed_undo(
    workspace, tmp_path, missing
):
    sid, historical_run, directory, _ = report_fixture(workspace)
    rid, args = undo_fixture(workspace, sid)
    before = workspace.store.investigation(sid)
    removed = {
        "run": directory,
        "runs": directory.parent,
        "session": directory.parent.parent,
        "sessions": directory.parent.parent.parent,
    }[missing]
    # Every recursive deletion stays inside this test's synthetic Temp state.
    assert removed.resolve(strict=True).is_relative_to(tmp_path.resolve(strict=True))
    shutil.rmtree(removed)
    assert not removed.exists() and not directory.exists()
    assert historical_run in {run["id"] for run in workspace.store.session(sid)["runs"]}
    creation_events = []
    watching = True

    def observe_creation(event, arguments):
        if not watching:
            return
        if event == "os.mkdir":
            creation_events.append({"event": event, "path": os.fsdecode(arguments[0])})
        elif event == "open" and len(arguments) > 2 and isinstance(arguments[2], int):
            flags = arguments[2]
            if flags & (os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR):
                creation_events.append({"event": event, "path": str(arguments[0]), "flags": flags})

    sys.addaudithook(observe_creation)
    try:
        result = complete(workspace.execute_ui_command(sid, rid, SCENARIO, args))
    finally:
        watching = False
    after = workspace.store.investigation(sid)
    observation = {
        "missing": missing,
        "command_action": result["command"]["action"],
        "command_context": result["command"]["context_id"],
        "retained_reports": result["retained_reports"],
        "removed_path_remains_absent": not removed.exists(),
        "owning_run_remains_absent": not directory.exists(),
        "creation_events": creation_events,
    }
    (tmp_path / "missing-root-observation.json").write_text(json.dumps(observation, indent=2))
    assert result["command"]["action"] == "undo"
    assert result["command"]["context_id"] == CANONICAL
    assert after["version"] == before["version"] + 1
    for field in ("summary", "question", "hypotheses", "next_steps", "priorities", "artifacts"):
        assert after[field] == before[field]
    assert result["retained_reports"]["status"] == "checked"
    assert result["retained_reports"]["items"][0]["status"] == "unavailable"
    assert not creation_events, observation
    assert not removed.exists() and not directory.exists(), observation


def test_production_model_receipt_keeps_bounded_historical_metadata_without_numeric_claims(
    workspace, tmp_path
):
    sid = workspace.store.create_session()["id"]
    for number in range(5):
        _, _, directory, saved = report_fixture(workspace, sid, f"report {number}")
        manifest = copy.deepcopy(saved["source_manifest"])
        manifest["contexts"].extend(
            {**manifest["contexts"][0], "context_id": f"{value:064x}"} for value in range(15)
        )
        manifest["references"] = [{"id": "prior_numeric_claim", "value": 987654}]
        ledger = EvidenceLedger(directory)
        scope_path = directory / "artifacts/verification_scope.json"
        original = json.loads(scope_path.read_text())
        original["delivery"]["source_manifest"] = manifest
        scope_path.write_text(json.dumps(original))
        ledger.register_artifact(scope_path)
        alter_saved(
            workspace, sid, lambda item, manifest=manifest: item.update(source_manifest=manifest)
        )
    rid, args = undo_fixture(workspace, sid)
    successful = complete(workspace.execute_ui_command(sid, rid, SCENARIO, args))
    runtime = runtime_fixture(workspace, sid, tmp_path)
    runtime.simulation = False
    runtime.exposed_receipts = set()
    runtime.claim_identities = {}
    runtime.claim_namespace = "synthetic"
    entry = sealed_ui(runtime, {"ok": True, **successful})
    before_claims = copy.deepcopy(runtime.available_claims())
    visible = runtime.model_receipt(entry)
    from analyst.agent_views import VIEW_BYTES
    from analyst.evidence import canonical

    assert visible["status"] == "completed"
    assert visible["result"]["retained_reports"] == successful["retained_reports"]
    assert len(visible["result"]["retained_reports"]["items"]) == 5
    assert all(
        len(item["historical_context_ids"]) == 16
        for item in visible["result"]["retained_reports"]["items"]
    )
    urls = [item["url"] for item in successful["retained_reports"]["items"]]
    assert runtime.verified_links({"artifacts": []}) == sorted(urls)
    assert all(url in canonical(visible) for url in urls)
    assert visible["numeric_evidence"] == []
    assert runtime.available_claims() == before_claims == {}
    assert runtime.claim_identities == {}
    assert "prior_numeric_claim" not in canonical(visible)
    assert len(canonical(visible).encode("utf-8")) <= VIEW_BYTES == 20_000
    assert runtime.toolkit.ledger.artifacts() == []
    (tmp_path / "production-model-receipt.json").write_text(canonical(visible))
    (tmp_path / "production-model-receipt-proof.json").write_text(
        canonical(
            {
                "encoded_bytes": len(canonical(visible).encode("utf-8")),
                "bound_bytes": VIEW_BYTES,
                "report_count": len(urls),
                "contexts_per_report": 16,
                "numeric_evidence": visible["numeric_evidence"],
                "claim_catalog": {},
                "urls": urls,
            }
        )
    )


def qa_runtime_fixture(subject, sid, tmp_path):
    ledger = EvidenceLedger(tmp_path / "qa-current")
    toolkit = SimpleNamespace(session_id=sid, ledger=ledger, run_dir=ledger.run_dir, input_paths=[])

    async def emit(_kind, _payload):
        pass

    # Runtime initialization only establishes in-process state and ProductTools.
    # Provider/SDK runner entrypoints remain blocked by the offline fixture.
    runtime = AnalystRuntime(subject.settings, toolkit, emit)
    runtime.simulation = False
    return runtime


def qa_for_ui(runtime):
    from analyst.runtime import AnalystReport

    report = AnalystReport(report="The requested view was restored.", claims=[])
    delivery = {
        "artifacts": [],
        "source_tables": [],
        "source_manifest": {"contexts": [], "sources": [], "references": []},
    }
    return runtime.qa_evidence(report, delivery=delivery)


@pytest.mark.parametrize("available", [True, False])
def test_production_qa_operational_proof_preserves_exact_undo_and_historical_availability(
    workspace, tmp_path, available
):
    from analyst.evidence import canonical

    sid, _, directory, _ = report_fixture(workspace)
    rid, arguments = undo_fixture(workspace, sid)
    if not available:
        (directory / "artifacts/business_report.html").unlink()
    result = {"ok": True, **complete(workspace.execute_ui_command(sid, rid, SCENARIO, arguments))}
    runtime = qa_runtime_fixture(workspace, sid, tmp_path)
    entry = sealed_ui(runtime, result)
    visible = runtime.model_receipt(entry)
    before_claims = copy.deepcopy(runtime.available_claims())
    before_delivery = runtime.toolkit.ledger.artifacts()
    qa, errors = qa_for_ui(runtime)
    identity = {
        key: entry[key]
        for key in (
            "execution_id",
            "entry_sha256",
            "result_sha256",
            "code_sha256",
            "execution_kind",
        )
    }
    expected = {"tool": "ui_command", "result": result, **identity}
    (tmp_path / "production-ui-qa-operational-proof.json").write_text(
        canonical(
            {
                "available": available,
                "entry_identity": identity,
                "original_result": result,
                "model_receipt": visible,
                "qa_evidence": qa,
                "errors": errors,
                "model_envelope_bytes": len(canonical(visible).encode("utf-8")),
            }
        )
    )
    assert errors == []
    assert qa["operational_receipts"] == [expected]
    command = qa["operational_receipts"][0]["result"]["command"]
    assert command == result["command"]
    assert command["action"] == "undo" and command["context_id"] == CANONICAL
    assert command["expected_context_id"] == SCENARIO
    assert command["filters"] == {"scenario": ""}
    retained = qa["operational_receipts"][0]["result"]["retained_reports"]
    assert retained == result["retained_reports"]
    assert retained["items"][0]["status"] == ("available" if available else "unavailable")
    if available:
        assert qa["verified_links"] == [retained["items"][0]["url"]]
    else:
        assert "url" not in retained["items"][0] and qa["verified_links"] == []
        assert result["ok"] is True
    assert visible["numeric_evidence"] == [] and qa["numeric_support"] == []
    assert runtime.available_claims() == before_claims == {}
    assert runtime.claim_identities == {}
    assert "prior-id" not in canonical(qa)
    assert runtime.toolkit.ledger.artifacts() == before_delivery == []


@pytest.mark.parametrize(
    "failure",
    [
        "failed",
        "truthy",
        "unbounded",
        "foreign_tool",
        "authored",
        "code_seal",
        "result_seal",
        "entry_seal",
        "damaged_ledger",
    ],
)
def test_qa_operational_proof_rejects_failed_unbounded_foreign_or_unsealed_receipts(
    workspace, tmp_path, failure
):
    from analyst.evidence import canonical, sha256

    sid, _, _, _ = report_fixture(workspace)
    rid, arguments = undo_fixture(workspace, sid)
    result = {"ok": True, **complete(workspace.execute_ui_command(sid, rid, SCENARIO, arguments))}
    runtime = qa_runtime_fixture(workspace, sid, tmp_path)
    if failure == "failed":
        result["ok"] = False
    elif failure == "truthy":
        result["ok"] = 1
    entry = sealed_ui(
        runtime,
        result,
        tool="investigation_update" if failure == "foreign_tool" else "ui_command",
        execution_kind="arbitrary_execution" if failure == "unbounded" else "bounded_analysis",
        code="Untrusted authored tool text" if failure == "authored" else None,
    )
    runtime.model_receipt(entry)
    if failure in {"code_seal", "result_seal", "entry_seal"}:
        field = {
            "code_seal": "code_sha256",
            "result_seal": "result_sha256",
            "entry_seal": "entry_sha256",
        }[failure]
        entry[field] = "f" * 64
        if failure != "entry_seal":
            entry["entry_sha256"] = sha256(
                canonical({key: value for key, value in entry.items() if key != "entry_sha256"})
            )
        runtime.toolkit.ledger.path.write_text(canonical(entry) + "\n", encoding="utf-8")
    elif failure == "damaged_ledger":
        damaged = runtime.toolkit.ledger.record("unrelated_tool", "code", {"ok": False})
        damaged["entry_sha256"] = "f" * 64
        runtime.toolkit.ledger.path.write_text(
            canonical(entry) + "\n" + canonical(damaged) + "\n", encoding="utf-8"
        )
    qa, _errors = qa_for_ui(runtime)
    assert qa["operational_receipts"] == []
    assert qa["numeric_support"] == [] and runtime.available_claims() == {}


def test_qa_operational_receipts_preserve_existing_operations_entry_shapes(workspace, tmp_path):
    sid = workspace.store.create_session()["id"]
    runtime = qa_runtime_fixture(workspace, sid, tmp_path)
    existing = [
        {"tool": "operations_list_cases", "result": {"ok": True, "cases": []}},
        {"tool": "operations_update_draft", "result": {"ok": False, "error": "Synthetic failure"}},
    ]
    for item in existing:
        runtime.toolkit.ledger.record(item["tool"], "existing operation code", item["result"])
    qa, errors = qa_for_ui(runtime)
    assert errors == [] and qa["operational_receipts"] == existing
    assert all(set(item) == {"tool", "result"} for item in qa["operational_receipts"])
