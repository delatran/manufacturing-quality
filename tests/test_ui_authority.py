"""Direct owner instructions govern UI changes at the actual steering boundary."""

from __future__ import annotations

import asyncio
from dataclasses import fields

import pytest
from analyst.app import Workspace
from analyst.case_study.analytics import Scope
from analyst.config import Settings
from test_dashboard import service


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
