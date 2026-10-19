"""Source-defined runtime settings and documented Responses capability normalization."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, overload
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """A request cannot be constructed safely from the current configuration."""


_ROOT = Path(__file__).resolve().parents[1]
_CAPABILITIES = json.loads((_ROOT / "config/model_capabilities.json").read_text(encoding="utf-8"))
ACTIVE_MODEL = "gpt-6-luna"
_RUN_LIMITS = frozenset(
    {
        "max_output_tokens",
        "max_cost_usd_per_run",
        "dev_api_budget_usd",
        "qa_max_retries",
        "max_turns",
        "max_tool_calls",
        "run_timeout_seconds",
        "tool_timeout_seconds",
        "tool_memory_mb",
        "max_upload_mb",
        "max_hosted_tool_calls",
        "knowledge_upload_max_bytes",
        "max_prompt_chars",
    }
)


def require_application_model(model: str) -> None:
    """Enforce the owner's model policy independently of recorded capabilities."""
    if model != ACTIVE_MODEL:
        raise ConfigurationError(
            f"This application only permits OPENAI_MODEL={ACTIVE_MODEL}; "
            f"received {model!r}. No alternate model or fallback will be called."
        )


def model_capabilities(model: str) -> dict[str, Any]:
    """Return a verified model record; never guess capabilities from a name prefix."""
    if model not in _CAPABILITIES["models"]:
        raise ConfigurationError(
            f"Unknown OPENAI_MODEL {model!r}. Add an officially sourced capability record "
            "to config/model_capabilities.json before making an API request."
        )
    return _CAPABILITIES["models"][model]


def _dotenv(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            key, separator, value = line.partition("=")
            if separator and key.strip() in {"OPENAI_API_KEY", "OPENAI_MODEL"}:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                else:
                    value = value.split(" #", 1)[0].strip()
                result[key.strip()] = value
    return result


@dataclass(frozen=True)
class Settings:
    root: Path = field(default_factory=lambda: _ROOT)
    model: str = ACTIVE_MODEL
    api_key: str = field(default="", repr=False)
    openai_base_url: str | None = None
    mode: str = "auto"
    agent_backend: str = "agents_api"
    unbounded_runs: bool = True
    reasoning_effort: str = "max"
    reasoning_summary: str = "auto"
    reasoning_mode: str = "standard"
    verbosity: str = "high"
    service_tier: str = "default"
    speed: str = "standard"
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int = 12000
    background: bool = True
    compaction_enabled: bool = True
    compaction_threshold: int = 200000
    prompt_cache_key: str = "analyst-workspace-v1"
    multi_agent_native: bool = False
    max_cost_usd_per_run: float = 5.0
    dev_api_budget_usd: float = 10.0
    qa_max_retries: int = 2
    max_turns: int = 40
    max_tool_calls: int = 32
    run_timeout_seconds: float = 450.0
    enable_file_search: bool = False
    enable_code_interpreter: bool = False
    enable_web_search: bool = False
    enable_mcp: bool = False
    enable_tool_search: bool = False
    vector_store_id: str = ""
    mcp_servers: tuple[dict[str, Any], ...] = ()
    memory_enabled: bool = True
    memory_top_k: int = 8
    host: str = "127.0.0.1"
    public_origin: str = ""
    port: int = 8000
    state_dir: str = ".runtime"
    tool_timeout_seconds: float = 120.0
    tool_memory_mb: int = 1024
    max_upload_mb: int = 50
    max_hosted_tool_calls: int = 4
    file_search_max_results: int = 2
    knowledge_vector_expiry_days: int = 1
    knowledge_upload_max_bytes: int = 1000000
    max_prompt_chars: int = 100000
    enable_async_tools: bool = False
    async_tool_names: tuple[str, ...] = (
        "local_python_repl",
        "local_sql_engine",
        "dataset_profiler",
        "run_case_study",
    )

    @classmethod
    def from_env(
        cls, root: Path | str | None = None, environ: Mapping[str, str] | None = None
    ) -> Settings:
        """Load only credentials and model; runtime policy lives in source defaults."""
        root = Path(root or _ROOT).resolve()
        values = _dotenv(root / ".env")
        environment = os.environ if environ is None else environ
        parsed: dict[str, Any] = {"root": root}
        for field_name, variable in (("api_key", "OPENAI_API_KEY"), ("model", "OPENAI_MODEL")):
            # Presence takes precedence, so an empty process key disables a dotenv key.
            value = environment.get(variable, values.get(variable, "")).strip()
            if value:
                parsed[field_name] = value
        settings = cls(**parsed)
        settings.validate()
        return settings

    @property
    def live(self) -> bool:
        return self.mode == "live" or (self.mode == "auto" and bool(self.api_key))

    @property
    def agent_mode(self) -> str:
        return "live" if self.live else "mock" if self.mode == "mock" else "unavailable"

    @overload
    def run_limit(
        self,
        name: Literal[
            "max_output_tokens",
            "qa_max_retries",
            "max_turns",
            "max_tool_calls",
            "tool_memory_mb",
            "max_upload_mb",
            "max_hosted_tool_calls",
            "knowledge_upload_max_bytes",
            "max_prompt_chars",
        ],
    ) -> int | None: ...

    @overload
    def run_limit(
        self,
        name: Literal[
            "max_cost_usd_per_run",
            "dev_api_budget_usd",
            "run_timeout_seconds",
            "tool_timeout_seconds",
        ],
    ) -> float | None: ...

    @overload
    def run_limit(self, name: str) -> int | float | None: ...

    def run_limit(self, name: str) -> int | float | None:
        """Return an active application limit, or None for the owner's unbounded profile.

        Finite values remain available for explicit bounded fixtures and historical
        accounting. No infinity is stored, serialized, or passed to the provider.
        Model capacity, provider stops and owner cancellation remain authoritative.
        """
        if name not in _RUN_LIMITS:
            raise ConfigurationError(f"Unknown application run limit: {name}")
        return None if self.unbounded_runs else getattr(self, name)

    def validate(self) -> None:
        require_application_model(self.model)
        model_capabilities(self.model)
        if self.openai_base_url not in {None, "http://provider.internal/v1"}:
            raise ConfigurationError("openai_base_url must use the private provider bridge")
        if self.mode not in {"auto", "mock", "live"}:
            raise ConfigurationError("ANALYST_MODE must be auto, mock or live")
        if self.agent_backend != "agents_api":
            raise ConfigurationError("agent_backend must be agents_api")
        if not isinstance(self.unbounded_runs, bool):
            raise ConfigurationError("unbounded_runs must be a boolean")
        if self.mode == "live" and not self.api_key:
            raise ConfigurationError(
                "ANALYST_MODE=live requires OPENAI_API_KEY; use auto or mock offline"
            )
        if self.multi_agent_native:
            raise ConfigurationError(
                "MULTI_AGENT_NATIVE must remain false; specialists use the Agents SDK"
            )
        for key in ("max_cost_usd_per_run", "dev_api_budget_usd"):
            value = getattr(self, key)
            if not math.isfinite(value) or value < 0:
                raise ConfigurationError(f"{key} must be a finite nonnegative number")
        if (
            self.qa_max_retries < 0
            or self.max_turns < 1
            or self.memory_top_k < 1
            or self.max_tool_calls < 1
        ):
            raise ConfigurationError("Retry, turn and memory limits are invalid")
        if not math.isfinite(self.run_timeout_seconds) or self.run_timeout_seconds <= 0:
            raise ConfigurationError("AGENT_RUN_TIMEOUT_SECONDS must be finite and positive")
        if self.max_output_tokens < 1 or self.compaction_threshold < 1:
            raise ConfigurationError("Output token and compaction thresholds must be positive")
        if not math.isfinite(self.tool_timeout_seconds) or self.tool_timeout_seconds <= 0:
            raise ConfigurationError("LOCAL_TOOL_TIMEOUT_SECONDS must be finite and positive")
        if self.tool_memory_mb < 1 or self.max_upload_mb < 1:
            raise ConfigurationError("Tool memory and upload limits must be positive")
        if self.max_hosted_tool_calls < 1:
            raise ConfigurationError("MAX_HOSTED_TOOL_CALLS must be positive")
        if not 1 <= self.file_search_max_results <= 50:
            raise ConfigurationError("FILE_SEARCH_MAX_RESULTS must be between 1 and 50")
        if not 1 <= self.knowledge_vector_expiry_days <= 30:
            raise ConfigurationError("KNOWLEDGE_VECTOR_EXPIRY_DAYS must be between 1 and 30")
        if self.knowledge_upload_max_bytes < 1 or self.max_prompt_chars < 1:
            raise ConfigurationError("Knowledge upload and prompt limits must be positive")
        if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in self.async_tool_names):
            raise ConfigurationError("ASYNC_TOOL_NAMES must contain valid direct function names")
        if not 1 <= self.port <= 65535:
            raise ConfigurationError("APP_PORT must be between 1 and 65535")
        if not self.state_dir.strip():
            raise ConfigurationError("ANALYST_STATE_DIR cannot be empty")
        if self.public_origin:
            try:
                origin = urlsplit(self.public_origin)
                _ = origin.port
            except ValueError as error:
                raise ConfigurationError("public_origin must be an HTTPS origin") from error
            if (
                origin.scheme != "https"
                or not origin.hostname
                or origin.username is not None
                or origin.password is not None
                or origin.path
                or origin.query
                or origin.fragment
            ):
                raise ConfigurationError("public_origin must be an HTTPS origin")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ConfigurationError("OPENAI_TEMPERATURE must be between 0 and 2")
        if self.top_p is not None and not 0 <= self.top_p <= 1:
            raise ConfigurationError("OPENAI_TOP_P must be between 0 and 1")

    def allows_origin(self, value: str | None) -> bool:
        """Check browser origins; the deployment still owns visitor authentication."""
        if not value:
            return True
        try:
            candidate = urlsplit(value)
            if (
                candidate.scheme not in {"http", "https"}
                or not candidate.hostname
                or candidate.username is not None
                or candidate.password is not None
                or candidate.path
                or candidate.query
                or candidate.fragment
            ):
                return False
            port = candidate.port
            if port is None:
                port = 443 if candidate.scheme == "https" else 80
            if self.public_origin:
                trusted = urlsplit(self.public_origin)
                return (
                    candidate.scheme == trusted.scheme
                    and candidate.hostname == trusted.hostname
                    and port == (443 if trusted.port is None else trusted.port)
                )
            return candidate.hostname in {"localhost", "127.0.0.1", "::1", self.host}
        except ValueError:
            return False

    def hosted_tools(self) -> list[dict[str, Any]]:
        require_application_model(self.model)
        supported = model_capabilities(self.model)["tools"]
        result: list[dict[str, Any]] = []
        if self.enable_file_search and self.vector_store_id and "file_search" in supported:
            file_search: dict[str, Any] = {
                "type": "file_search",
                "vector_store_ids": [self.vector_store_id],
            }
            if not self.unbounded_runs:
                file_search["max_num_results"] = self.file_search_max_results
            result.append(file_search)
        if self.enable_web_search and "web_search" in supported:
            result.append({"type": "web_search"})
        if self.enable_code_interpreter and "code_interpreter" in supported:
            result.append(
                {"type": "code_interpreter", "container": {"type": "auto", "memory_limit": "1g"}}
            )
        if self.enable_mcp and "mcp" in supported:
            for server in self.mcp_servers:
                if not server.get("server_label") or not server.get("server_url"):
                    raise ConfigurationError("Each MCP server needs server_label and server_url")
                result.append({**server, "type": "mcp"})
        if self.enable_tool_search and "tool_search" in supported:
            result.append({"type": "tool_search"})
        return result

    def public_config(self) -> dict[str, Any]:
        """Safe configuration metadata for the UI; never exposes credentials."""
        return {
            "model": self.model,
            "allowed_models": [ACTIVE_MODEL],
            "model_policy": "luna_only_no_fallback",
            "mode": self.agent_mode,
            "agent_available": self.agent_mode != "unavailable",
            "unavailable_reason": "missing_api_key" if self.agent_mode == "unavailable" else None,
            "runtime": "OpenAI Agents API",
            "agent_backend": self.agent_backend,
            "unbounded_runs": self.unbounded_runs,
            "conversation_strategy": "managed_conversation",
            "max_tool_calls": self.run_limit("max_tool_calls"),
            "run_timeout_seconds": self.run_limit("run_timeout_seconds"),
            "max_output_tokens": self.run_limit("max_output_tokens"),
            "max_turns": self.run_limit("max_turns"),
            "sourced_at": _CAPABILITIES["sourced_at"],
            "sources": _CAPABILITIES["sources"],
            "model_sources": model_capabilities(self.model)["sources"],
            "max_cost_usd_per_run": self.run_limit("max_cost_usd_per_run"),
            "dev_api_budget_usd": self.run_limit("dev_api_budget_usd"),
            "qa_max_retries": self.run_limit("qa_max_retries"),
            "max_hosted_tool_calls": self.run_limit("max_hosted_tool_calls"),
            "reasoning_effort": normalize_model_request(self).get("reasoning", {}).get("effort"),
            "hosted_tools": [tool["type"] for tool in self.hosted_tools()],
            "compaction_enabled": self.compaction_enabled
            and model_capabilities(self.model)["compaction"],
        }


def normalize_model_request(
    settings: Settings,
    input: Any = None,
    instructions: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build only verified request fields; unsupported options are omitted locally.

    Only the owner-authorized Luna model can be dispatched. Invalid effort/tier
    values use Luna's documented default; there is no alternate-model fallback.
    Arbitrary overrides are rejected so SDK defaults cannot leak unsupported fields.
    """
    settings.validate()
    caps = model_capabilities(settings.model)
    effort = (
        settings.reasoning_effort
        if settings.reasoning_effort in caps["reasoning_efforts"]
        else caps["default_effort"]
    )
    request: dict[str, Any] = {
        "model": settings.model,
        "store": True,
    }
    output_limit = settings.run_limit("max_output_tokens")
    if output_limit is not None:
        request["max_output_tokens"] = min(output_limit, caps["max_output_tokens"])
    if input is not None:
        request["input"] = input
    if instructions:
        request["instructions"] = instructions
    if effort is not None:
        reasoning = {"effort": effort}
        if settings.reasoning_summary not in {"", "none", "off"} and caps["reasoning_summary"]:
            # Explicit summary styles differ per model. `auto` is the documented
            # portable choice and selects the most detailed supported summarizer.
            reasoning["summary"] = "auto"
        if settings.reasoning_mode in caps["reasoning_modes"]:
            reasoning["mode"] = settings.reasoning_mode
        request["reasoning"] = reasoning
    if caps["verbosity"] and settings.verbosity in {"low", "medium", "high"}:
        request["text"] = {"verbosity": settings.verbosity}
    if not caps["sampling_requires_none"] or effort == "none":
        for name in ("temperature", "top_p"):
            value = getattr(settings, name)
            if value is not None:
                request[name] = value
    tier = settings.speed if settings.speed in {"fast", "ultrafast"} else settings.service_tier
    if tier in caps["service_tiers"]:
        request["service_tier"] = tier
    else:
        request["service_tier"] = "default"
    if settings.background and caps["background"]:
        request["background"] = True
    if settings.compaction_enabled and caps["compaction"]:
        request["context_management"] = [
            {
                "type": "compaction",
                "compact_threshold": min(
                    settings.compaction_threshold,
                    caps["context_window"]
                    - request.get("max_output_tokens", caps["max_output_tokens"]),
                ),
            }
        ]
    if caps["prompt_cache_options"]:
        request["prompt_cache_options"] = {"ttl": "30m"}
    if settings.prompt_cache_key:
        request["prompt_cache_key"] = settings.prompt_cache_key
    if tools is not None:
        filtered = []
        for tool in tools:
            tool_type = tool.get("type")
            if tool_type in {"function", "custom", "namespace"} or tool_type in caps["tools"]:
                item = dict(tool)
                if item.get("async") and not caps["async_functions"]:
                    item.pop("async")
                filtered.append(item)
        request["tools"] = filtered
    allowed = {
        "previous_response_id",
        "include",
        "stream",
        "metadata",
        "tool_choice",
        "parallel_tool_calls",
        "max_tool_calls",
    }
    unknown = set(overrides) - allowed
    if unknown:
        raise ConfigurationError(f"Unverified Responses overrides: {', '.join(sorted(unknown))}")
    for key, value in overrides.items():
        if key == "max_tool_calls" and settings.unbounded_runs:
            continue
        if value is not None and value != "":
            request[key] = value
    if "include" in request and effort != "none":
        request["include"] = [
            item for item in request["include"] if item != "message.output_text.logprobs"
        ]
    return request


def hosted_tool_prices() -> dict[str, float]:
    """Published OpenAI fees; storage and remote MCP provider billing are separate."""
    return dict(_CAPABILITIES["hosted_tool_prices_usd"])


def estimate_hosted_tool_cost(tool_type: str, calls: int = 1) -> float:
    """Known tool invocation fees, excluding tokens and external resources.

    Code Interpreter's unit is a 1 GB container session of 20 minutes, not an
    arbitrary duration tool call. A runtime must reserve/account sessions itself.
    MCP external charges cannot be inferred from an MCP tool call.
    """
    if calls < 0:
        raise ValueError("Tool call count must be nonnegative")
    key = {
        "web_search": "web_search_per_call",
        "file_search": "file_search_per_call",
        "code_interpreter": "code_interpreter_container_1g_per_20min",
    }.get(tool_type)
    if key is None:
        raise ConfigurationError(f"No verified invocation price for tool type {tool_type!r}")
    return hosted_tool_prices()[key] * calls


def normalize_response_kwargs(
    settings: Settings, *, tools: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return normalize_model_request(settings, tools=tools)


def estimate_token_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    service_tier: str = "default",
) -> float:
    """USD token estimate, including cache writes and long context. Hosted fees separate."""
    caps = model_capabilities(model)
    counts = (input_tokens, output_tokens, cached_tokens, cache_write_tokens)
    if any(count < 0 for count in counts) or cached_tokens + cache_write_tokens > input_tokens:
        raise ValueError("Inconsistent token usage")
    prices = caps["pricing_usd_per_million"]
    threshold = caps["long_context_threshold"]
    long_context = bool(threshold and input_tokens > threshold)
    input_multiplier = caps["long_context_multipliers"]["input"] if long_context else 1
    output_multiplier = caps["long_context_multipliers"]["output"] if long_context else 1
    ordinary = input_tokens - cached_tokens - cache_write_tokens
    input_cost = (
        ordinary * prices["input"]
        + cached_tokens * prices["cached_input"]
        + cache_write_tokens * prices["cache_write"]
    ) * input_multiplier
    output_cost = output_tokens * prices["output"] * output_multiplier
    if service_tier not in caps["tier_multipliers"]:
        raise ConfigurationError(f"Unpriced service tier: {service_tier}")
    return (input_cost + output_cost) / 1_000_000 * caps["tier_multipliers"][service_tier]
