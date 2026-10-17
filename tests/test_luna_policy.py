"""No network is needed to verify the application's hard model boundary."""

import asyncio

import pytest
from analyst.config import ACTIVE_MODEL, ConfigurationError, Settings, normalize_model_request
from analyst.managed_agents import ManagedAgentsModel
from analyst.model import dump
from analyst.runtime import AnalystRuntime


@pytest.mark.parametrize(
    "model", ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-luna", "gpt-4.1", "unknown"]
)
def test_alternate_models_are_rejected_at_all_entry_points(tmp_path, model):
    with pytest.raises(ConfigurationError, match="only permits"):
        Settings.from_env(tmp_path, {"OPENAI_MODEL": model})
    with pytest.raises(ConfigurationError, match="only permits"):
        normalize_model_request(Settings(model=model))
    with pytest.raises(ConfigurationError, match="only permits"):
        ManagedAgentsModel(Settings(model=model), lambda *_: None, client=object())


def test_all_application_roles_share_luna_settings(tmp_path):
    async def emit(*_):
        pass

    settings = Settings(root=tmp_path, mode="live", api_key="fake-key")
    runtime = AnalystRuntime(settings, None, emit)
    for role in ("Lead Analyst", "Data Engineer", "QA Verifier"):
        adapter = runtime.model(role)
        assert adapter.settings is settings
        assert normalize_model_request(adapter.settings)["model"] == ACTIVE_MODEL
        asyncio.run(adapter.client.close())


def test_transport_preserves_nullable_schema_fields_and_compaction():
    schema = {
        "type": "function",
        "name": "dashboard_compare_scope",
        "async_": True,
        "parameters": {
            "type": "object",
            "properties": {"production_line": {"type": ["string", "null"]}},
        },
    }
    result = dump(schema)
    assert result["async"] is True and "async_" not in result
    assert result["parameters"]["properties"]["production_line"]["type"] == ["string", "null"]
    assert dump({"type": "compaction", "encrypted_content": "opaque", "created_by": "sdk"}) == {
        "type": "compaction",
        "encrypted_content": "opaque",
    }
