"""Retained managed items keep full pagination and normalization semantics."""

import asyncio

import pytest
from analyst import managed_agents
from analyst.model import dump
from openai.types.beta.agent_session_assistant_message import AgentSessionAssistantMessage
from test_managed_agents import Page, Sessions, model_for


def sdk_message(identity, *, turn_id="old_turn"):
    return AgentSessionAssistantMessage(
        id=identity,
        turn_id=turn_id,
        type="message",
        role="assistant",
        status="completed",
        phase="final_answer",
        content=[{"type": "output_text", "text": "Previously verified message"}],
    )


@pytest.mark.parametrize("representation", ["sdk", "dict"])
def test_seen_only_pages_are_read_without_serializing_discarded_items(monkeypatch, representation):
    async def run():
        seen = [sdk_message(f"seen_{index}") for index in range(205)]
        retained = [
            sdk_message("unseen_historical"),
            {
                "id": "opaque_compaction",
                "type": "compaction",
                "encrypted_content": "opaque",
                "created_by": "provider",
            },
            {"id": "unseen_function", "type": "function_call", "async_": True, "arguments": "{}"},
            sdk_message("current_message", turn_id="current_turn"),
            {"type": "future_item", "content": "An item without an ID is still retained"},
        ]
        if representation == "dict":
            seen = [dump(item) for item in seen]
        expected = [dump(item) for item in retained]
        pages = [seen[:100], seen[100:200], [*seen[200:], *retained]]
        visited, requests, serialized = [], [], []

        class FullHistory:
            async def __aiter__(self):
                for page_index, page in enumerate(pages):
                    visited.append(page_index)
                    for item in page:
                        yield item

        class FullSessions(Sessions):
            async def list_items(self, session_id, **kwargs):
                requests.append((session_id, kwargs))
                return FullHistory()

        def track_dump(item):
            serialized.append(item)
            return dump(item)

        model, _ = model_for(FullSessions([], [], []))
        model.session_id = "sess_real"
        model._seen_items = {f"seen_{index}" for index in range(205)}
        monkeypatch.setattr(managed_agents, "dump", track_dump)

        assert await model._items() == expected
        assert visited == [0, 1, 2]
        assert requests == [("sess_real", {"order": "asc", "limit": 100})]
        assert serialized == retained
        assert model._seen_items == {f"seen_{index}" for index in range(205)}

    asyncio.run(run())


def test_items_without_public_id_still_use_the_existing_normalized_id_filter():
    class NormalizedItem:
        def model_dump(self, **kwargs):
            return {"id": "seen_id", "type": "message"}

    async def run():
        sessions = Sessions([], [[NormalizedItem()]], [])
        model, _ = model_for(sessions)
        model._seen_items = {"seen_id"}
        assert await model._items() == []

    asyncio.run(run())


def test_page_failure_after_seen_only_history_does_not_return_partial_items():
    failure = RuntimeError("Later history page failed")

    class FailedPage(Page):
        async def __aiter__(self):
            yield sdk_message("seen_id")
            yield sdk_message("new_id")
            raise failure

    class FailedSessions(Sessions):
        async def list_items(self, session_id, **kwargs):
            return FailedPage([])

    async def run():
        model, _ = model_for(FailedSessions([], [], []))
        model._seen_items = {"seen_id"}
        with pytest.raises(RuntimeError) as caught:
            await model._items()
        assert caught.value is failure
        assert model._seen_items == {"seen_id"}

    asyncio.run(run())
