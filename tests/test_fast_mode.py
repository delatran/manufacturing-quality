"""Fast-mode configuration reaches native requests without changing run policy."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from agents import ModelSettings
from agents.models.interface import ModelTracing
from analyst.app import create_app
from analyst.config import (
    ConfigurationError,
    Settings,
    estimate_token_cost,
    normalize_model_request,
)
from analyst.managed_agents import ManagedAgentsModel
from fastapi.testclient import TestClient
from openai import AsyncOpenAI
from test_managed_agents import native_completed_provider


async def discard(*_):
    pass


@pytest.mark.parametrize("source", ["dotenv", "mapping", "process"])
@pytest.mark.parametrize(
    "flag,tier", [("true", "fast"), (" TRUE ", "fast"), ("false", "default"), ("", "default")]
)
def test_fast_flag_selects_native_tier_and_preserves_execution_policy(
    tmp_path, monkeypatch, source, flag, tier
):
    monkeypatch.delenv("OPENAI_FAST_MODE", raising=False)
    if source == "dotenv":
        (tmp_path / ".env").write_text(f'OPENAI_FAST_MODE="{flag}"\n', encoding="utf-8")
    elif source == "process":
        monkeypatch.setenv("OPENAI_FAST_MODE", flag)
    settings = Settings.from_env(
        tmp_path, {"OPENAI_FAST_MODE": flag} if source == "mapping" else None
    )
    request = normalize_model_request(settings)
    assert request["service_tier"] == settings.public_config()["service_tier"] == tier
    assert settings.model == "gpt-6-luna" and settings.agent_backend == "agents_api"
    assert settings.unbounded_runs and not settings.multi_agent_native
    assert request["reasoning"]["effort"] == "max"
    assert "max_output_tokens" not in request and "max_tool_calls" not in request
    assert settings.run_limit("max_cost_usd_per_run") is None


@pytest.mark.parametrize("source", ["mapping", "process"])
@pytest.mark.parametrize(
    "flag,tier", [(None, "fast"), ("", "default"), ("false", "default"), ("true", "fast")]
)
def test_process_presence_overrides_dotenv_fast_flag(tmp_path, monkeypatch, source, flag, tier):
    (tmp_path / ".env").write_text("OPENAI_FAST_MODE=true\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_FAST_MODE", raising=False)
    environment = {} if flag is None else {"OPENAI_FAST_MODE": flag}
    if source == "process":
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
    settings = Settings.from_env(tmp_path, environment if source == "mapping" else None)
    assert normalize_model_request(settings)["service_tier"] == tier


def test_absent_flag_keeps_standard_and_legacy_speed_environment_is_ignored(tmp_path):
    settings = Settings.from_env(tmp_path, {"OPENAI_SPEED": "fast", "OPENAI_SERVICE_TIER": "fast"})
    assert settings.speed == "standard"
    assert normalize_model_request(settings)["service_tier"] == "default"


@pytest.mark.parametrize("flag", ["yes", "1", "fast"])
def test_invalid_fast_flag_fails_before_dispatch(tmp_path, flag):
    with pytest.raises(ConfigurationError, match="OPENAI_FAST_MODE must be true or false"):
        Settings.from_env(tmp_path, {"OPENAI_FAST_MODE": flag})


@pytest.mark.parametrize(
    "line", ["export OPENAI_FAST_MODE=' TrUe '", "OPENAI_FAST_MODE=true # Fast mode"]
)
def test_dotenv_fast_flag_supports_existing_export_quote_and_comment_syntax(tmp_path, line):
    (tmp_path / ".env").write_text("\ufeff" + line + "\n", encoding="utf-8")
    assert normalize_model_request(Settings.from_env(tmp_path, {}))["service_tier"] == "fast"


@pytest.mark.parametrize("flag,tier", [("true", "fast"), ("false", "default")])
def test_public_api_reports_requested_tier_without_exposing_credentials(tmp_path, flag, tier):
    settings = replace(
        Settings.from_env(tmp_path, {"OPENAI_FAST_MODE": flag}),
        root=Path(__file__).resolve().parents[1],
        state_dir=str(tmp_path / "state"),
        memory_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/config")
        assert response.status_code == 200
        assert response.json()["service_tier"] == tier
        assert "api_key" not in response.json()


@pytest.mark.parametrize("flag,tier", [("true", "fast"), ("false", "default")])
def test_flag_serializes_inside_actual_sdk_managed_session_request(tmp_path, flag, tier):
    async def scenario():
        captured = []

        async def handle(request):
            assert str(request.url) == "https://offline.example/v1/agents/sessions"
            captured.append(json.loads(request.content))
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content="")

        async with AsyncOpenAI(
            api_key="offline-test-only",
            base_url="https://offline.example/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
            max_retries=0,
        ) as client:
            settings = Settings.from_env(tmp_path, {"OPENAI_FAST_MODE": flag})
            model = ManagedAgentsModel(settings, discard, client=client)
            stream = await client.beta.agents.sessions.create(
                agent=model._agent_configuration("Inspect the frozen context", [], None, []),
                environment={"type": "none"},
                input=model._messages([{"role": "user", "content": "Inspect the frozen context"}]),
                stream=True,
            )
            await stream.close()
        assert len(captured) == 1
        body = captured[0]
        assert body["agent"]["service_tier"] == tier
        assert body["agent"]["model"] == "gpt-6-luna"
        assert body["agent"]["reasoning"]["effort"] == "max"
        assert body["agent"]["multi_agent"] == {"enabled": False}
        assert body["environment"] == {"type": "none"}
        assert "spend_control" not in body and "service_tier" not in body
        assert "max_output_tokens" not in body["agent"]

    asyncio.run(scenario())


@pytest.mark.parametrize("reported_tier,multiplier", [("fast", 2), ("priority", 2), ("default", 1)])
def test_managed_usage_prices_reported_tier_including_fast_downgrade(
    tmp_path, reported_tier, multiplier
):
    settings = Settings.from_env(tmp_path, {"OPENAI_FAST_MODE": "true"})
    provider = native_completed_provider("sess_fast_mode", ["Verified result"])
    for snapshot in provider.snapshots:
        snapshot.agent.service_tier = reported_tier
    model = ManagedAgentsModel(
        settings,
        discard,
        client=SimpleNamespace(beta=SimpleNamespace(agents=SimpleNamespace(sessions=provider))),
    )

    async def scenario():
        await model.get_response(
            "Rules", "Question", ModelSettings(), [], None, [], ModelTracing.DISABLED
        )
        await model.aclose()

    asyncio.run(scenario())
    assert provider.creates[0]["agent"]["service_tier"] == "fast"
    assert "spend_control" not in provider.creates[0]
    usage = model.usage[0]
    standard_cost = estimate_token_cost("gpt-6-luna", 12, 4)
    assert usage["estimated_token_cost_usd"] == pytest.approx(standard_cost * multiplier)
    assert model.budget["cost_usd"] == usage["estimated_token_cost_usd"]
    assert model.budget["managed_reserved_cost_usd"] == 0
    assert model.budget["unresolved_generation_requests"] == 0
    assert usage["actual_cost_usd"] is None and not usage["billing_settled"]
