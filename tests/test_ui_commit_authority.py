"""Owner steering remains authoritative while UI contexts are prepared."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import fields
from types import SimpleNamespace

import pytest
from analyst.app import Workspace
from analyst.case_study.analytics import Scope
from analyst.config import Settings
from analyst.investigation import merge_scope_grants, scope_grants
from analyst.store import SessionStore
from test_dashboard import service


def arguments(action="apply_scope", **filters):
    return {
        "action": action,
        "panel": "records",
        "filters": {**{field.name: None for field in fields(Scope)}, **filters},
        "command_id": None,
    }


@asynccontextmanager
async def active_workspace(tmp_path):
    analysis = service(tmp_path)
    bound = await analysis.get_context()
    cid = bound["context"]["context_id"]
    workspace = Workspace(
        Settings(mode="mock", state_dir=str(tmp_path / "workspace"), memory_enabled=False),
        analysis_service=analysis,
    )
    sid = workspace.store.create_session()["id"]
    await workspace.set_ui_scope(sid, cid)
    rid, _ = workspace.store.begin_run(sid, cid)
    workspace.tasks[sid] = asyncio.create_task(asyncio.Event().wait())
    workspace.active_run_ids[sid] = rid
    workspace.active_contexts[sid] = cid
    workspace.active_languages[sid] = "en"
    workspace.active_action_intents[sid] = "none"
    workspace.active_runtimes[sid] = SimpleNamespace(accepting_steering=True)
    workspace.ui_bindings[rid] = cid
    workspace.ui_authorities[rid] = scope_grants("Show only Critical, then undo", bound["options"])
    try:
        yield workspace, sid, rid, cid
    finally:
        await workspace.close()


def pause_first_context(monkeypatch, analysis, method):
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = getattr(analysis, method)
    first = True

    async def delayed(*args, **kwargs):
        nonlocal first
        result = await original(*args, **kwargs)
        if first:
            first = False
            entered.set()
            await proceed.wait()
        return result

    monkeypatch.setattr(analysis, method, delayed)
    return entered, proceed


def command_count(workspace):
    return workspace.store.db.execute("SELECT COUNT(*) FROM ui_commands").fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,revocation",
    [
        ("apply_scope", "Do not change any filters or the view. Explain what Critical means."),
        ("apply_scope", "Đừng đổi bộ lọc hay giao diện, chỉ giải thích Critical là gì."),
        ("apply_scope", "Keep filters unchanged; open the evidence panel."),
        ("open_panel", "Do not open any panel. Explain the current view."),
    ],
)
async def test_owner_revocation_during_preparation_prevents_any_ui_commit(
    tmp_path, monkeypatch, action, revocation
):
    async with active_workspace(tmp_path) as (workspace, sid, rid, cid):
        entered, proceed = pause_first_context(
            monkeypatch,
            workspace.analysis,
            "get_context" if action == "apply_scope" else "context_by_id",
        )
        requested = arguments(
            action, **({"severity": "Critical"} if action == "apply_scope" else {})
        )
        pending = asyncio.create_task(workspace.execute_ui_command(sid, rid, cid, requested))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            original = workspace.store.investigation(sid)
            assert (await workspace.prompt(sid, revocation, cid))["queued"]
            proceed.set()
            with pytest.raises(ValueError, match="authority"):
                await asyncio.wait_for(pending, 10)
            assert workspace.store.investigation(sid) == original
            assert command_count(workspace) == 0
            assert workspace.ui_bindings[rid] == cid
        finally:
            proceed.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_new_prompt_scope_during_preparation_is_not_overwritten(tmp_path, monkeypatch):
    async with active_workspace(tmp_path) as (workspace, sid, rid, cid):
        alternate = await workspace.analysis.get_context({"production_line": "Line-2"})
        other_cid = alternate["context"]["context_id"]
        entered, proceed = pause_first_context(monkeypatch, workspace.analysis, "get_context")
        pending = asyncio.create_task(
            workspace.execute_ui_command(sid, rid, cid, arguments(severity="Critical"))
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            assert (await workspace.prompt(sid, "Explain these records", other_cid))["queued"]
            latest = workspace.store.investigation(sid)
            assert latest["ui"]["context_id"] == other_cid
            proceed.set()
            with pytest.raises(ValueError, match="stale command"):
                await asyncio.wait_for(pending, 10)
            assert workspace.store.investigation(sid) == latest
            assert command_count(workspace) == 0
            assert workspace.ui_bindings[rid] == cid
        finally:
            proceed.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor,steering", [("agent", "revoke"), ("agent", "scope"), ("user", "scope")]
)
async def test_undo_preparation_cannot_overwrite_later_owner_instruction(
    tmp_path, monkeypatch, steering, actor
):
    async with active_workspace(tmp_path) as (workspace, sid, rid, cid):
        original = (
            await workspace.execute_ui_command(sid, rid, cid, arguments(severity="Critical"))
        )["command"]
        selected_cid = original["context_id"]
        alternate = await workspace.analysis.get_context({"production_line": "Line-2"})
        entered, proceed = pause_first_context(monkeypatch, workspace.analysis, "context_by_id")
        pending = asyncio.create_task(
            workspace.execute_ui_command(
                sid, rid, cid, {**arguments("undo"), "command_id": original["command_id"]}
            )
            if actor == "agent"
            else workspace.undo_ui_command(sid, original["command_id"], selected_cid)
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            new_cid = selected_cid if steering == "revoke" else alternate["context"]["context_id"]
            prompt = "Do not undo." if steering == "revoke" else "Explain these records"
            assert (await workspace.prompt(sid, prompt, new_cid))["queued"]
            latest = workspace.store.investigation(sid)
            proceed.set()
            with pytest.raises(ValueError, match="authority|stale command"):
                await asyncio.wait_for(pending, 10)
            assert workspace.store.investigation(sid) == latest
            assert command_count(workspace) == 1
            assert not workspace.store.ui_command(sid, original["command_id"])["undone"]
            assert workspace.ui_bindings[rid] == selected_cid
        finally:
            proceed.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize(
    "prohibition",
    [
        "Do not reset the filters.",
        "Don't undo.",
        "Never restore the view.",
        "Do not revert the view.",
        "Đừng đặt lại bộ lọc.",
        "Không khôi phục giao diện.",
        "Đừng hoàn tác.",
        "Đừng quay về quy tắc chuẩn.",
        "Không quay lại.",
    ],
)
def test_negated_ui_verbs_revoke_prior_grants(prohibition):
    incoming = scope_grants(prohibition, {})
    assert incoming["filters"] == {}
    assert not incoming["undo"] and not incoming["view"]
    assert incoming["revoke_filters"]
    prior = scope_grants("Show Critical, then undo", {"severities": ["Critical"]})
    merged = merge_scope_grants(prior, incoming)
    assert merged["filters"] == {} and not merged["undo"]
    if incoming["revoke_view"]:
        assert not merged["view"]


@pytest.mark.parametrize(
    "prompt",
    [
        "Reset the filters.",
        "Restore the view.",
        "Undo.",
        "Revert the view.",
        "Đặt bộ lọc Critical.",
        "Khôi phục giao diện.",
        "Hoàn tác.",
        "Quay về quy tắc chuẩn.",
        "Quay lại.",
    ],
)
def test_positive_ui_verbs_keep_direct_authority(prompt):
    grant = scope_grants(prompt, {"severities": ["Critical"]})
    assert grant["view"] and not grant["revoke_filters"] and not grant["revoke_view"]


@pytest.mark.parametrize("failure", ["state", "undo_marker"])
def test_ui_commit_rolls_back_command_state_and_undo_marker_together(
    tmp_path, monkeypatch, failure
):
    store = SessionStore(tmp_path)
    try:
        sid = store.create_session()["id"]
        store.save_investigation(
            sid, 0, {"ui": {"context_id": "before", "panel": "dashboard", "last_command_id": None}}
        )
        command = {
            "command_id": "original",
            "context_id": "after",
            "expected_context_id": "before",
            "panel": "records",
        }
        undo_id = None
        if failure == "state":
            original_save = store._save_investigation

            def fail_after_state(*args, **kwargs):
                original_save(*args, **kwargs)
                raise sqlite3.IntegrityError("Synthetic state persistence failure")

            monkeypatch.setattr(store, "_save_investigation", fail_after_state)
        else:
            store.commit_ui_command(sid, "run", command, expected_version=1)
            undo_id = command["command_id"]
            command = {
                "command_id": "inverse",
                "context_id": "before",
                "expected_context_id": "after",
                "panel": "dashboard",
            }
            with store.db:
                store.db.execute(
                    "CREATE TRIGGER fail_undo BEFORE UPDATE OF undone ON ui_commands "
                    "BEGIN SELECT RAISE(ABORT, 'Synthetic undo persistence failure'); END"
                )
        original_state = store.investigation(sid)
        original_commands = store.db.execute("SELECT * FROM ui_commands").fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="Synthetic"):
            store.commit_ui_command(
                sid,
                "run",
                command,
                expected_version=original_state["version"],
                undo_command_id=undo_id,
            )
        assert store.investigation(sid) == original_state
        assert store.db.execute("SELECT * FROM ui_commands").fetchall() == original_commands
        assert not store.db.in_transaction
    finally:
        store.close()
