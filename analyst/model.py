"""Shared model lifecycle for the managed Agents API and SDK tool interoperability."""

from __future__ import annotations

import asyncio
import inspect
from abc import abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from agents import (
    AgentOutputSchemaBase,
    Handoff,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
)
from openai import AsyncOpenAI

from .config import Settings

Emit = Callable[[str, dict], Awaitable[None]]


def dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True, by_alias=True)
    if isinstance(value, list):
        return [dump(item) for item in value]
    if isinstance(value, dict):
        result = {key: dump(item) for key, item in value.items()}
        # SDK RunItem.to_input_item currently dumps Python field names. Translate
        # the documented reserved-word alias before sending replayed call items.
        item_type = result.get("type")
        # JSON Schema permits a union type list, such as ["string", "null"].
        # It is data inside a function schema, not a Responses item discriminator.
        if (
            isinstance(item_type, str)
            and item_type in {"function_call", "custom_tool_call", "function", "custom"}
            and "async_" in result
        ):
            result["async"] = result.pop("async_")
        if result.get("type") == "compaction":
            result.pop("created_by", None)
        return result
    return value


def compact_items(items: list[dict]) -> list[dict]:
    """Only server-produced opaque items compress context; never summarize."""
    indices = [i for i, item in enumerate(items) if item.get("type") == "compaction"]
    return items[indices[-1] :] if indices else items


class BudgetExceeded(RuntimeError):
    pass


class ProviderStopped(RuntimeError):
    pass


class ResponseProtocolError(RuntimeError):
    pass


class ResponseIncomplete(RuntimeError):
    pass


class ResponseRefused(RuntimeError):
    pass


class AgentModel(Model):
    def __init__(
        self,
        settings: Settings,
        emit: Emit,
        *,
        client: Any = None,
        steering: Callable[[], Any] | None = None,
        budget: dict[str, Any] | None = None,
        tool_dispatch: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.emit = emit
        self.client = client or AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.openai_base_url or "https://api.openai.com/v1",
            max_retries=0,
        )
        self.steering = steering
        self.tool_dispatch = tool_dispatch
        self.budget: dict[str, Any] = budget if budget is not None else {"cost_usd": 0.0}
        self.budget.setdefault("model_lock", asyncio.Lock())
        self.budget.setdefault("unresolved_generation_requests", 0)
        self.history: list[dict] = []
        self.usage: list[dict] = []

    async def _input(self, value: Any) -> list[dict[str, Any]]:
        items = [{"role": "user", "content": value}] if isinstance(value, str) else dump(value)
        # SDK supplies canonical previous output, including compaction items.
        items = compact_items(items)
        if self.steering:
            queued = self.steering()
            queued = await queued if inspect.isawaitable(queued) else queued
            for message in queued or []:
                items.append({"role": "user", "content": message})
                await self.emit(
                    "steering_applied", {"message": message, "mode": "queued_model_boundary"}
                )
        return items

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs: Any,
    ) -> ModelResponse:
        # SDK may invoke specialist agents concurrently. Serialize provider
        # dispatch through accounting so shared budget reservations cannot race.
        async with self.budget["model_lock"]:
            if self.budget.get("provider_stopped"):
                raise ProviderStopped(
                    "Provider stopped this conversation; no further model dispatch"
                )
            if self.budget.get("blocked"):
                raise BudgetExceeded(
                    "A prior request failed or has uncertain accounting; no further dispatch"
                )
            try:
                return await self._get_response(
                    system_instructions,
                    input,
                    model_settings,
                    tools,
                    output_schema,
                    handoffs,
                    tracing,
                    **kwargs,
                )
            except ProviderStopped:
                self.budget["provider_stopped"] = True
                raise
            except BaseException:
                self.budget["blocked"] = True
                raise

    @abstractmethod
    async def _get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        """Dispatch through the managed provider implementation."""
        raise NotImplementedError

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator:
        # App events are streamed directly from get_response; Runner.run owns
        # actual tool execution and output guardrails.
        raise NotImplementedError(
            "Use Runner.run; provider events stream through the application emitter"
        )
        yield
