import json
from dataclasses import replace

import pytest
from analyst.config import (
    ConfigurationError,
    Settings,
    estimate_hosted_tool_cost,
    estimate_token_cost,
    model_capabilities,
    normalize_model_request,
)


def test_empty_environment_values_use_defaults(tmp_path):
    (tmp_path / ".env").write_text('OPENAI_MODEL=gpt-6-luna\nOPENAI_API_KEY=""\nQA_MAX_RETRIES=4\n')
    settings = Settings.from_env(
        tmp_path, {"OPENAI_MODEL": "  ", "OPENAI_TEMPERATURE": "", "QA_MAX_RETRIES": ""}
    )
    assert settings.model == "gpt-6-luna"
    assert settings.qa_max_retries == 2
    assert not settings.live
    assert "temperature" not in normalize_model_request(settings)


def test_invalid_effort_uses_luna_default_and_removes_sampling():
    settings = Settings(reasoning_effort="unverified", temperature=0.4, top_p=0.9)
    request = normalize_model_request(settings)
    assert request["reasoning"]["effort"] == "medium"
    assert "temperature" not in request and "top_p" not in request
    assert request["context_management"] == [{"type": "compaction", "compact_threshold": 200000}]
    assert request["prompt_cache_options"] == {"ttl": "30m"}
    assert "multi_agent" not in request


def test_recorded_non_luna_model_cannot_be_dispatched():
    with pytest.raises(ConfigurationError, match="only permits OPENAI_MODEL=gpt-6-luna"):
        normalize_model_request(Settings(model="gpt-4.1"))


def test_luna_sampling_only_with_none():
    settings = Settings(model="gpt-6-luna", reasoning_effort="none", temperature=0.7, top_p=0.9)
    assert normalize_model_request(settings)["temperature"] == 0.7
    assert "temperature" not in normalize_model_request(replace(settings, reasoning_effort="high"))


def test_luna_filters_unsupported_request_options():
    request = normalize_model_request(
        Settings(reasoning_effort="none", speed="ultrafast", temperature=0.3, top_p=0.9),
        tools=[
            {"type": "function", "name": "local_python_repl", "async": True},
            {"type": "web_search_preview"},
            {"type": "web_search"},
        ],
    )
    caps = model_capabilities("gpt-6-luna")
    assert request["service_tier"] in caps["service_tiers"]
    assert all(tool["type"] != "web_search_preview" for tool in request["tools"])
    if caps["reasoning_efforts"]:
        assert request["reasoning"]["effort"] in caps["reasoning_efforts"]
    if not caps["async_functions"]:
        assert "async" not in request["tools"][0]


def test_unknown_model_fails_before_network():
    with pytest.raises(ConfigurationError, match="only permits OPENAI_MODEL=gpt-6-luna"):
        normalize_model_request(Settings(model="future-unverified-model"))


def test_unverified_request_overrides_fail_before_network():
    with pytest.raises(ConfigurationError, match="Unverified Responses overrides"):
        normalize_model_request(Settings(), temperature=0.3)


def test_native_multiagent_disabled():
    with pytest.raises(ConfigurationError, match="must remain false"):
        normalize_model_request(Settings(multi_agent_native=True))


def test_optional_hosted_tools_and_dependencies():
    settings = Settings(
        enable_file_search=True,
        enable_web_search=True,
        enable_code_interpreter=True,
        enable_mcp=True,
        vector_store_id="vs_test",
        mcp_servers=({"server_label": "docs", "server_url": "https://developers.openai.com/mcp"},),
    )
    tools = settings.hosted_tools()
    assert {tool["type"] for tool in tools} == {
        "file_search",
        "web_search",
        "code_interpreter",
        "mcp",
    }
    assert tools[0]["vector_store_ids"] == ["vs_test"]
    assert "max_num_results" not in tools[0]
    assert replace(settings, unbounded_runs=False).hosted_tools()[0]["max_num_results"] == 2
    assert Settings(vector_store_id="").hosted_tools() == []


def test_cost_accounts_for_cache_write_and_cache_read_and_tier():
    # 700 ordinary + 200 cached + 100 cache-write input tokens, 100 output.
    cost = estimate_token_cost("gpt-6-astra", 1000, 100, 200, 100)
    assert cost == pytest.approx((700 * 10 + 200 * 1 + 100 * 12.5 + 100 * 50) / 1_000_000)
    assert estimate_token_cost("gpt-6-astra", 1000, 100, 200, 100, "ultrafast") == pytest.approx(
        cost * 6
    )
    assert estimate_token_cost("gpt-6-astra", 300000, 1000) == pytest.approx(6 + 0.075)
    with pytest.raises(ValueError, match="Inconsistent token usage"):
        estimate_token_cost("gpt-6-astra", 1000, 100, 1001)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_cost_usd_per_run": float("nan")},
        {"dev_api_budget_usd": -1},
        {"qa_max_retries": -1},
        {"top_p": 1.1},
        {"mode": "invalid"},
        {"mode": "live"},
        {"port": 65536},
        {"run_timeout_seconds": float("inf")},
        {"tool_timeout_seconds": float("nan")},
        {"tool_memory_mb": 0},
        {"max_upload_mb": -1},
        {"knowledge_vector_expiry_days": 31},
        {"knowledge_upload_max_bytes": 0},
        {"max_prompt_chars": 0},
        {"file_search_max_results": 51},
    ],
)
def test_invalid_programmatic_configuration_rejected(overrides):
    with pytest.raises(ConfigurationError):
        replace(Settings(), **overrides).validate()


def test_public_config_has_sources_without_secrets():
    settings = Settings(
        api_key="sk-private",
        mcp_servers=(
            {
                "server_label": "private",
                "server_url": "https://private.example",
                "authorization": "secret",
            },
        ),
    )
    public = settings.public_config()
    serialized = json.dumps(public)
    assert public["sourced_at"] == "2026-10-15"
    assert public["sources"] and public["model_sources"]
    assert (
        "sk-private" not in serialized
        and "whsec-private" not in serialized
        and "authorization" not in serialized
    )


def test_known_hosted_fees_and_unknown_external_fees():
    assert estimate_hosted_tool_cost("web_search", 4) == 0.04
    assert estimate_hosted_tool_cost("file_search", 4) == 0.01
    assert estimate_hosted_tool_cost("code_interpreter") == 0.03
    with pytest.raises(ConfigurationError, match="No verified invocation price"):
        estimate_hosted_tool_cost("mcp")
    request = normalize_model_request(Settings(unbounded_runs=False), max_tool_calls=4)
    assert request["max_tool_calls"] == 4


def test_unbounded_profile_omits_application_limits_and_serializes_null():
    settings = Settings()
    assert settings.agent_backend == "agents_api"
    assert settings.unbounded_runs
    request = normalize_model_request(settings, max_tool_calls=4)
    assert request["reasoning"]["effort"] == "max"
    assert "max_output_tokens" not in request
    assert "max_tool_calls" not in request
    public = settings.public_config()
    for name in (
        "max_output_tokens",
        "max_cost_usd_per_run",
        "dev_api_budget_usd",
        "max_turns",
        "max_tool_calls",
        "run_timeout_seconds",
        "max_hosted_tool_calls",
    ):
        assert settings.run_limit(name) is None
        assert public[name] is None
    assert public["runtime"] == "OpenAI Agents API"
    assert public["conversation_strategy"] == "managed_conversation"
    json.dumps(public, allow_nan=False)


def test_explicit_bounded_profile_retains_finite_policy_and_model_capacity():
    settings = Settings(unbounded_runs=False, max_output_tokens=999999)
    request = normalize_model_request(settings, max_tool_calls=7)
    assert request["max_output_tokens"] == model_capabilities(settings.model)["max_output_tokens"]
    assert request["max_tool_calls"] == 7
    assert settings.run_limit("max_cost_usd_per_run") == 5
    assert settings.public_config()["conversation_strategy"] == "managed_conversation"
    with pytest.raises(ConfigurationError, match="Unknown application run limit"):
        settings.run_limit("misnamed_limit")


@pytest.mark.parametrize(
    "overrides",
    [{"agent_backend": "unknown"}, {"agent_backend": "responses"}, {"unbounded_runs": "true"}],
)
def test_invalid_execution_profile_fails_locally(overrides):
    with pytest.raises(ConfigurationError):
        replace(Settings(), **overrides).validate()


def test_gpt41_fast_pricing_uses_its_published_rate():
    assert estimate_token_cost("gpt-4.1", 1000000, 0, service_tier="fast") == 3.5
    # Historical billing remains readable; the model is not dispatchable.
    with pytest.raises(ConfigurationError, match="only permits"):
        normalize_model_request(Settings(model="gpt-4.1", service_tier="flex"))


def test_async_tool_names_and_capability_filtering():
    defaults = Settings()
    assert not defaults.enable_async_tools
    assert defaults.async_tool_names == (
        "local_python_repl",
        "local_sql_engine",
        "dataset_profiler",
        "run_case_study",
    )
    configured = replace(defaults, async_tool_names=("local_python_repl", "dataset_profiler"))
    assert not configured.enable_async_tools
    assert configured.async_tool_names == ("local_python_repl", "dataset_profiler")
    tool = {"type": "function", "name": "local_python_repl", "async": True}
    assert normalize_model_request(defaults, tools=[tool])["tools"][0]["async"]
    with pytest.raises(ConfigurationError, match="only permits"):
        normalize_model_request(Settings(model="gpt-4.1"), tools=[tool])
    with pytest.raises(ConfigurationError, match="direct function names"):
        replace(defaults, async_tool_names=("bad function name",)).validate()


@pytest.mark.parametrize("source", ["process", "dotenv"])
def test_runtime_policy_ignores_legacy_environment_variables(tmp_path, monkeypatch, source):
    legacy = {
        "ANALYST_MODE": "live",
        "OPENAI_REASONING_EFFORT": "high",
        "OPENAI_REASONING_SUMMARY": "off",
        "OPENAI_REASONING_MODE": "extended",
        "OPENAI_VERBOSITY": "high",
        "OPENAI_SERVICE_TIER": "fast",
        "OPENAI_SPEED": "ultrafast",
        "OPENAI_TEMPERATURE": "0.9",
        "OPENAI_TOP_P": "0.9",
        "OPENAI_MAX_OUTPUT_TOKENS": "999999",
        "OPENAI_BACKGROUND": "false",
        "COMPACTION_ENABLED": "false",
        "COMPACTION_THRESHOLD": "1",
        "OPENAI_PROMPT_CACHE_KEY": "unexpected",
        "MULTI_AGENT_NATIVE": "true",
        "MAX_COST_USD_PER_RUN": "999999",
        "DEV_API_BUDGET_USD": "nan",
        "QA_MAX_RETRIES": "999999",
        "AGENT_MAX_TURNS": "999999",
        "AGENT_MAX_TOOL_CALLS": "999999",
        "AGENT_RUN_TIMEOUT_SECONDS": "inf",
        "ENABLE_FILE_SEARCH": "true",
        "ENABLE_CODE_INTERPRETER": "true",
        "ENABLE_WEB_SEARCH": "true",
        "ENABLE_MCP": "true",
        "ENABLE_TOOL_SEARCH": "true",
        "OPENAI_VECTOR_STORE_ID": "vs_unexpected",
        "MCP_SERVERS_JSON": "malformed JSON",
        "MEMORY_ENABLED": "perhaps",
        "MEMORY_TOP_K": "999999",
        "OPENAI_WEBHOOK_SECRET": "ignored-secret",
        "APP_HOST": "0.0.0.0",
        "APP_PORT": "65536",
        "ANALYST_STATE_DIR": "unexpected",
        "LOCAL_TOOL_TIMEOUT_SECONDS": "inf",
        "LOCAL_TOOL_MEMORY_MB": "0",
        "MAX_UPLOAD_MB": "999999",
        "MAX_HOSTED_TOOL_CALLS": "999999",
        "FILE_SEARCH_MAX_RESULTS": "999999",
        "KNOWLEDGE_VECTOR_EXPIRY_DAYS": "999999",
        "KNOWLEDGE_UPLOAD_MAX_BYTES": "999999",
        "MAX_PROMPT_CHARS": "999999",
        "ENABLE_ASYNC_TOOLS": "true",
        "ASYNC_TOOL_NAMES": "invalid tool name",
    }
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    if source == "dotenv":
        (tmp_path / ".env").write_text(
            "\n".join(f"{key}={value}" for key, value in legacy.items()), encoding="utf-8"
        )
    else:
        for key, value in legacy.items():
            monkeypatch.setenv(key, value)
    settings = Settings.from_env(tmp_path)
    assert settings == Settings(root=tmp_path.resolve())
    assert settings.max_output_tokens == 12000
    assert settings.reasoning_effort == "max"
    assert settings.run_timeout_seconds == 450
    assert (settings.max_cost_usd_per_run, settings.dev_api_budget_usd) == (5, 10)
    assert settings.agent_mode == "unavailable"
    assert not settings.multi_agent_native
    assert settings.hosted_tools() == []
    assert (settings.host, settings.port, settings.state_dir) == ("127.0.0.1", 8000, ".runtime")


@pytest.mark.parametrize("source", ["mapping", "process"])
def test_allowed_environment_overrides_dotenv_with_whitespace(tmp_path, monkeypatch, source):
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=dotenv-test-key\nOPENAI_MODEL=unsupported-model\n", encoding="utf-8"
    )
    values = {"OPENAI_API_KEY": " process-test-key ", "OPENAI_MODEL": " gpt-6-luna "}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    settings = Settings.from_env(tmp_path, values if source == "mapping" else None)
    assert settings.api_key == "process-test-key"
    assert settings.model == "gpt-6-luna"
    assert settings.live
    assert "process-test-key" not in repr(settings)
    assert "process-test-key" not in json.dumps(settings.public_config())


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_environment_key_disables_dotenv_credentials(tmp_path, blank):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=dotenv-test-key\n", encoding="utf-8")
    settings = Settings.from_env(tmp_path, {"OPENAI_API_KEY": blank})
    assert settings.api_key == ""
    assert settings.agent_mode == "unavailable"


def test_dotenv_allowed_values_are_loaded_when_environment_is_absent(tmp_path):
    (tmp_path / ".env").write_text(
        '\ufeff# Credentials\nexport OPENAI_API_KEY=" dotenv-test-key "\n'
        "OPENAI_MODEL=gpt-6-luna # selected model\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(tmp_path, {})
    assert settings.api_key == "dotenv-test-key"
    assert settings.model == "gpt-6-luna"
    assert settings.live


@pytest.mark.parametrize("model", ["gpt-4.1", "future-unverified-model"])
def test_environment_model_policy_rejects_alternate_models(tmp_path, model):
    with pytest.raises(ConfigurationError, match="only permits OPENAI_MODEL=gpt-6-luna"):
        Settings.from_env(tmp_path, {"OPENAI_MODEL": model})
