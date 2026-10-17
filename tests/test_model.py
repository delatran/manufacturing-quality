"""Shared managed model dispatch remains serialized across application roles."""

import asyncio

import pytest
from agents import ModelResponse, ModelSettings, Usage
from agents.models.interface import ModelTracing
from analyst.config import Settings
from analyst.model import AgentModel, BudgetExceeded, ProviderStopped


async def discard(*_):
    pass


async def request(model):
    return await model.get_response(
        "Rules", "Question", ModelSettings(), [], None, [], ModelTracing.DISABLED
    )


def test_shared_model_lock_serializes_roles_until_dispatch_finishes():
    async def scenario():
        entered, release, peer_attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order, active = [], []
        budget = {"cost_usd": 0.0}

        class RoleModel(AgentModel):
            async def _get_response(self, *_args, **_kwargs):
                assert not active
                active.append(self)
                order.append(self)
                if self is lead:
                    entered.set()
                    await release.wait()
                active.remove(self)
                return ModelResponse(output=[], usage=Usage(requests=1), response_id=None)

        lead, qa = [
            RoleModel(Settings(), discard, client=object(), budget=budget) for _ in range(2)
        ]

        async def peer():
            peer_attempted.set()
            return await request(qa)

        first = asyncio.create_task(request(lead))
        await entered.wait()
        second = asyncio.create_task(peer())
        await peer_attempted.wait()
        assert order == [lead] and not second.done()
        release.set()
        results = await asyncio.gather(first, second)
        assert order == [lead, qa] and not active
        assert all(result.usage.requests == 1 for result in results)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure,expected_peer_error,flag",
    [
        (ProviderStopped("Provider stopped"), ProviderStopped, "provider_stopped"),
        (ConnectionError("Unknown dispatch outcome"), BudgetExceeded, "blocked"),
        (asyncio.CancelledError(), BudgetExceeded, "blocked"),
    ],
)
def test_failed_role_blocks_waiting_and_later_peer_dispatch(failure, expected_peer_error, flag):
    async def scenario():
        entered, release, peer_attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        dispatched = []
        budget = {"cost_usd": 0.0}

        class RoleModel(AgentModel):
            async def _get_response(self, *_args, **_kwargs):
                dispatched.append(self)
                entered.set()
                await release.wait()
                raise failure

        lead, qa = [
            RoleModel(Settings(), discard, client=object(), budget=budget) for _ in range(2)
        ]

        async def peer():
            peer_attempted.set()
            return await request(qa)

        first = asyncio.create_task(request(lead))
        await entered.wait()
        second = asyncio.create_task(peer())
        await peer_attempted.wait()
        assert dispatched == [lead] and not second.done()
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], type(failure))
        assert isinstance(results[1], expected_peer_error)
        assert budget[flag] is True and dispatched == [lead]
        with pytest.raises(expected_peer_error, match="no further"):
            await request(qa)
        assert dispatched == [lead]

    asyncio.run(scenario())
