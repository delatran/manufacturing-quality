"""Public runtime credentials and origins remain server-defined boundaries."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analyst.app import create_app
from analyst.config import ConfigurationError, Settings
from analyst.execution import PythonSession, analysis_environment
from analyst.managed_agents import ManagedAgentsModel
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.mark.parametrize(
    "origin",
    [
        "https://tranthiennhan.com.evil.example",
        "https://other.example",
        "http://tranthiennhan.com",
        "https://tranthiennhan.com:444",
        "https://tranthiennhan.com:0",
        "https://user@tranthiennhan.com",
        "https://tranthiennhan.com/path",
        "https://tranthiennhan.com?query=1",
        "https://tranthiennhan.com#fragment",
        "https://[broken",
        "null",
    ],
)
def test_public_origin_rejects_nonmatching_authorities(origin):
    settings = Settings(public_origin="https://tranthiennhan.com")
    assert not settings.allows_origin(origin)


@pytest.mark.parametrize("origin", ["https://tranthiennhan.com", "https://tranthiennhan.com:443"])
def test_public_origin_accepts_only_canonical_effective_port(origin):
    assert Settings(public_origin="https://tranthiennhan.com").allows_origin(origin)


@pytest.mark.parametrize(
    "origin",
    [
        "http://tranthiennhan.com",
        "https://user@tranthiennhan.com",
        "https://tranthiennhan.com/",
        "https://tranthiennhan.com:bad",
    ],
)
def test_invalid_public_origin_configuration_fails(origin):
    with pytest.raises(ConfigurationError, match="HTTPS origin"):
        Settings(public_origin=origin).validate()


def test_origin_policy_applies_to_writes_and_websockets(tmp_path):
    settings = replace(
        Settings(),
        mode="mock",
        state_dir=str(tmp_path / "state"),
        public_origin="https://tranthiennhan.com",
        memory_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        accepted = {"Origin": "https://tranthiennhan.com"}
        rejected = {"Origin": "https://other.example"}
        sid = client.post("/api/sessions", headers=accepted).json()["id"]
        assert client.post("/api/sessions", headers=rejected).status_code == 403
        with client.websocket_connect(f"/api/sessions/{sid}/ws", headers=accepted) as socket:
            socket.close()
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/api/sessions/{sid}/ws", headers=rejected),
        ):
            pass


def test_worker_environment_excludes_secrets_and_owner_paths(tmp_path, monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "CLOUDFLARE_API_TOKEN",
        "USERPROFILE",
        "LOCALAPPDATA",
    ):
        monkeypatch.setenv(name, "synthetic-private-value")
    environment = analysis_environment(tmp_path)
    assert "synthetic-private-value" not in environment.values()
    assert environment["HOME"] == str(tmp_path)
    assert environment["TEMP"] == environment["TMPDIR"] == str(tmp_path / ".tmp")


def test_real_python_worker_does_not_inherit_provider_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-owner-key")
    monkeypatch.setenv("HTTPS_PROXY", "synthetic-proxy-credential")

    async def scenario():
        worker = PythonSession(tmp_path / "run", [])
        try:
            result = await worker.execute(
                "result = {key: pd.io.common.os.environ.get(key) "
                "for key in ['OPENAI_API_KEY', 'HTTPS_PROXY']}"
            )
            assert result["ok"]
            assert result["value"] == {"OPENAI_API_KEY": None, "HTTPS_PROXY": None}
        finally:
            await worker.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "base_url,expected",
    [
        (None, "https://api.openai.com/v1"),
        ("http://provider.internal/v1", "http://provider.internal/v1"),
    ],
)
def test_managed_client_uses_explicit_server_provider(base_url, expected, monkeypatch):
    recorded = {}
    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.example/v1")

    def client(**kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(with_options=lambda **options: SimpleNamespace())

    async def emit(*_):
        pass

    monkeypatch.setattr("analyst.model.AsyncOpenAI", client)
    ManagedAgentsModel(Settings(api_key="synthetic-proxy-key", openai_base_url=base_url), emit)
    assert recorded["base_url"] == expected
    assert recorded["api_key"] == "synthetic-proxy-key"


def test_public_provider_configuration_cannot_be_redirected_by_environment(tmp_path):
    settings = Settings.from_env(
        tmp_path,
        {
            "OPENAI_API_KEY": "synthetic-key",
            "OPENAI_BASE_URL": "https://untrusted.example/v1",
            "PUBLIC_ORIGIN": "https://untrusted.example",
        },
    )
    assert settings.openai_base_url is None
    assert settings.public_origin == ""
    with pytest.raises(ConfigurationError, match="private provider bridge"):
        replace(settings, openai_base_url="https://untrusted.example/v1").validate()
