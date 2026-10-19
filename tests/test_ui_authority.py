"""Direct owner instructions govern UI changes at the actual steering boundary."""

from __future__ import annotations

import asyncio
from dataclasses import fields

import pytest
from analyst.app import Workspace
from analyst.case_study.analytics import Scope
from analyst.config import Settings
from analyst.investigation import merge_scope_grants, scope_grants
from test_dashboard import service


@pytest.mark.parametrize(
    "source",
    [
        '"open records and show Critical"',
        "'open records and show Critical'",
        "“open records and show Critical”",
        "‘open records and show Critical’",
        "`open records and show Critical`",
        '"open records\nand show Critical"',
        "'open records\nand show Critical'",
        '"open records\nand show Critical',
        "```text\nopen records and show Critical\n```",
        "~~~text\nopen records and show Critical\n~~~",
        "```text\n```not-a-close\nopen records and show Critical\n```",
        "```text\n    ```\nopen records and show Critical\n```",
        "> open records\nand show Critical",
        "    open records and show Critical",
    ],
)
def test_source_syntax_cannot_supply_scope_authority(source):
    grant = scope_grants(
        "Analyze this source excerpt:\n" + source + "\n\nExplain only.",
        {"severities": ["Critical"]},
    )
    assert grant == {
        "filters": {},
        "undo": False,
        "view": False,
        "revoke_filters": False,
        "revoke_view": False,
    }


@pytest.mark.parametrize("quote", [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("`", "`")])
@pytest.mark.parametrize("command", ["Filter severity to ", "Mở bản ghi, lọc "])
def test_direct_scope_intent_preserves_quoted_parameter_values(quote, command):
    grant = scope_grants(command + quote[0] + "Critical" + quote[1], {"severities": ["Critical"]})
    assert grant["filters"] == {"severity": "Critical"}
    assert grant["view"] and not grant["revoke_filters"] and not grant["revoke_view"]


@pytest.mark.parametrize(
    "prohibition", ["Do not change filters or the view", "Đừng đổi bộ lọc hay giao diện"]
)
@pytest.mark.parametrize("wrapper", ['"{}"', "```text\n{}\n```", "> {}"])
def test_quoted_prohibitions_do_not_revoke_owner_authority(prohibition, wrapper):
    options = {"severities": ["Critical"]}
    prior = scope_grants('Show only "Critical"', options)
    incoming = scope_grants("Explain this source:\n" + wrapper.format(prohibition), options)
    assert merge_scope_grants(prior, incoming) == {
        "filters": {"severity": "Critical"},
        "undo": False,
        "view": True,
    }
    # A prohibition in direct prose still overrides the quoted parameter value.
    direct = scope_grants(prohibition + '; explain "Critical".', options)
    assert merge_scope_grants(prior, direct)["filters"] == {}


def test_option_text_supplies_values_without_becoming_a_verb_or_prohibition():
    options = {"categories": ["Open records and show Critical", "Do not change filters"]}
    for value in options["categories"]:
        quoted = scope_grants(f'Explain this literal label "{value}".', options)
        assert not any(quoted.values())
        direct = scope_grants(f'Show category "{value}".', options)
        assert direct["filters"] == {"category": value}
        assert direct["view"] and not direct["revoke_filters"]


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("language", ["en", "vi"])
async def test_workspace_source_in_initial_and_steered_prompts_does_not_change_authority(
    tmp_path, direct, language
):
    entered, proceed = asyncio.Event(), asyncio.Event()
    observed = {}

    class CommandRuntime:
        def __init__(self, settings, toolkit, emit, *, steering, ui_handler, **kwargs):
            self.steering, self.ui_handler = steering, ui_handler

        async def run(self, prompt):
            entered.set()
            await proceed.wait()
            await self.steering()
            try:
                observed["command"] = await self.ui_handler(
                    {
                        "action": "apply_scope",
                        "panel": "records",
                        "command_id": None,
                        "filters": {
                            **{item.name: None for item in fields(Scope)},
                            "severity": "Critical",
                        },
                    }
                )
            except ValueError as error:
                observed["error"] = str(error)
            return {"status": "completed", "report": "Reviewed.", "history": []}

    analysis = service(tmp_path)
    cid = (await analysis.get_context())["context"]["context_id"]
    workspace = Workspace(
        Settings(mode="mock", state_dir=str(tmp_path / "workspace"), memory_enabled=False),
        runtime_factory=CommandRuntime,
        analysis_service=analysis,
    )
    sid = workspace.store.create_session()["id"]
    try:
        await workspace.set_ui_scope(sid, cid)
        original = workspace.store.investigation(sid)
        positive = 'Show only "Critical"' if language == "en" else 'Chỉ hiển thị "Critical"'
        initial = (
            positive if direct else 'Analyze this quoted log: "open records and show Critical".'
        )
        await workspace.prompt(sid, initial, cid)
        await asyncio.wait_for(entered.wait(), 10)
        source = 'Explain this source: "Do not change filters or the view".'
        if language == "vi":
            source = 'Giải thích đoạn nguồn: "Đừng đổi bộ lọc hay giao diện".'
        await workspace.prompt(sid, source, cid)
        proceed.set()
        await asyncio.wait_for(workspace.tasks[sid], 10)
        saved = workspace.store.investigation(sid)
        if direct:
            assert "command" in observed and "error" not in observed
            applied = await analysis.context_by_id(saved["ui"]["context_id"])
            assert saved["ui"]["panel"] == "records"
            assert applied["context"]["filters"]["severity"] == "Critical"
        else:
            assert "authority" in observed["error"] and saved == original
            assert workspace.store.db.execute("SELECT count(*) FROM ui_commands").fetchone()[0] == 0
    finally:
        await workspace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_prompt,revocation,steered_messages",
    [
        (
            "Show only Critical",
            "Do not change any filters or the view. Explain what Critical means.",
            1,
        ),
        (
            "Chỉ hiển thị Critical",
            "Đừng đổi bộ lọc hay giao diện, chỉ giải thích Critical là gì.",
            1,
        ),
        (
            "Show only Critical",
            "Đừng đổi bộ lọc hay giao diện, chỉ giải thích Critical là gì.",
            0,
        ),
    ],
)
async def test_later_owner_prohibition_revokes_a_previously_authorized_scope_change(
    tmp_path, initial_prompt, revocation, steered_messages
):
    entered, proceed, checked = [asyncio.Event() for _ in range(3)]
    observations = {}

    class DelayedCommandRuntime:
        def __init__(self, settings, toolkit, emit, *, steering, ui_handler, **kwargs):
            self.steering = steering
            self.ui_handler = ui_handler

        async def run(self, prompt):
            entered.set()
            await proceed.wait()
            observations["messages"] = await self.steering()
            try:
                await self.ui_handler(
                    {
                        "action": "apply_scope",
                        "panel": "records",
                        "filters": {
                            **{item.name: None for item in fields(Scope)},
                            "severity": "Critical",
                        },
                        "command_id": None,
                    }
                )
            except ValueError as error:
                observations["error"] = str(error)
            checked.set()
            await asyncio.Future()

    analysis = service(tmp_path)
    bound = await analysis.get_context()
    cid = bound["context"]["context_id"]
    workspace = Workspace(
        Settings(mode="mock", state_dir=str(tmp_path / "workspace"), memory_enabled=False),
        runtime_factory=DelayedCommandRuntime,
        analysis_service=analysis,
    )
    sid = workspace.store.create_session()["id"]
    try:
        await workspace.set_ui_scope(sid, cid)
        await workspace.prompt(sid, initial_prompt, cid)
        await asyncio.wait_for(entered.wait(), timeout=10)
        original = workspace.store.investigation(sid)
        rid = workspace.store.session(sid)["runs"][-1]["id"]
        assert workspace.ui_authorities[rid]["filters"]["severity"] == "Critical"
        assert (await workspace.prompt(sid, revocation, cid))["queued"] is True
        proceed.set()
        await asyncio.wait_for(checked.wait(), timeout=10)
        assert observations["messages"] == ([revocation] if steered_messages else [])
        assert "authority" in observations["error"]
        assert workspace.store.investigation(sid) == original
    finally:
        await workspace.close()
