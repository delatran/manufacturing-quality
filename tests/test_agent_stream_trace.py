"""Public provider traces survive disconnects without replaying generation."""

import asyncio
from types import SimpleNamespace

from analyst.app import Workspace
from analyst.config import Settings


def test_workspace_persists_authoritative_summary_identity_and_replay(tmp_path):
    async def scenario():
        workspace = Workspace(
            Settings(root=tmp_path, mode="mock", state_dir=str(tmp_path / "state")),
            analysis_service=SimpleNamespace(),
        )
        try:
            sid = workspace.store.create_session()["id"]
            rid, _ = workspace.store.begin_run(sid)
            event = await workspace.emit(
                sid,
                rid,
                "reasoning_summary",
                {
                    "text": "Public complete summary",
                    "done": True,
                    "item_id": "rs_trace",
                    "summary_index": 1,
                    "output_index": 0,
                    "response_id": "resp_trace",
                    "source": "managed_session_items",
                    "reconciled": True,
                },
            )
            assert event["type"] == "reasoning"
            assert event["data"]["text"] == "Public complete summary"
            assert event["data"]["final"] is True
            assert event["data"]["summary_index"] == 1
            assert event["data"]["source"] == "managed_session_items"
            assert workspace.store.events(sid, event["id"] - 1)[-1] == event
        finally:
            await workspace.close()

    asyncio.run(scenario())
