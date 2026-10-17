import asyncio
import inspect
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from analyst.config import Settings
from analyst.knowledge import knowledge_fingerprint, prepare_knowledge
from analyst.memory import AnalystMemory
from openai import AsyncOpenAI


@pytest.fixture
def setup(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "kpi_registry.yaml").write_text(
        json.dumps(
            {
                "source": {
                    "file": "case.pdf",
                    "sha256": "a" * 64,
                    "note": "No numerical answer key.",
                },
                "adopted_assumptions": {"cost": "Missing cost stays unknown; do not impute."},
                "kpis": {"count": {"formula": "count(events)", "unit": "events"}},
                "pdf_requirements": [{"id": "events", "fields": ["defect_id", "repair_cost"]}],
            }
        )
    )
    (knowledge / "case_brief.md").write_text("# Case\nBusiness requirements only.")
    (knowledge / "independent_oracle.json").write_text('{"answer": 666}')
    return Settings(root=tmp_path, enable_file_search=True), AnalystMemory(
        tmp_path / ".runtime/memory"
    )


def test_offline_seed_no_network_and_stable_cache_identity(setup, monkeypatch):
    settings, memory = setup
    monkeypatch.setattr(
        "analyst.knowledge.AsyncOpenAI", lambda **_: pytest.fail("Network client in offline mode")
    )
    events = []
    effective, status = asyncio.run(
        prepare_knowledge(settings, memory, lambda kind, data: events.append((kind, data)))
    )
    assert status["status"] == "pending_no_api_key"
    assert not status["remote_verified"] and not effective.vector_store_id
    assert status["memory_notes"] == 4
    assert "Missing cost stays unknown" in memory.markdown_path.read_text()
    assert "666" not in memory.markdown_path.read_text()
    assert events[0][0] == "memory"
    again, _ = asyncio.run(prepare_knowledge(effective, memory))
    assert again.prompt_cache_key == effective.prompt_cache_key
    old = knowledge_fingerprint(settings.root)
    (settings.root / "knowledge/case_brief.md").write_text("Changed rule")
    assert knowledge_fingerprint(settings.root) != old


class FakeClient:
    def __init__(self, status="completed", fail=False):
        self.store = SimpleNamespace(
            id="vs_owned",
            status="completed",
            usage_bytes=12345,
            metadata={},
            file_counts=SimpleNamespace(completed=2, failed=0, in_progress=0),
        )
        self.uploads = []
        self.uploaded_ids = []
        self.files = SimpleNamespace(create=AsyncMock(side_effect=self.upload), delete=AsyncMock())
        self.vector_stores = SimpleNamespace(
            create=AsyncMock(side_effect=self.create_store),
            retrieve=AsyncMock(return_value=self.store),
            delete=AsyncMock(),
            files=SimpleNamespace(
                create_and_poll=AsyncMock(
                    side_effect=RuntimeError("secret-dont-emit") if fail else None,
                    return_value=SimpleNamespace(status=status),
                )
            ),
        )

    async def upload(self, **kwargs):
        self.uploads.append(kwargs)
        file_id = f"file_{len(self.uploads)}"
        self.uploaded_ids.append(file_id)
        return SimpleNamespace(id=file_id)

    async def create_store(self, **kwargs):
        self.store.metadata = kwargs["metadata"]
        return self.store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def test_live_uploads_only_curated_documents_then_reuses_remote_readback(setup, monkeypatch):
    settings, memory = setup
    settings = replace(settings, api_key="fake", mode="live")
    client = FakeClient()
    monkeypatch.setattr("analyst.knowledge.AsyncOpenAI", lambda **_: client)
    effective, status = asyncio.run(prepare_knowledge(settings, memory))
    assert status["status"] == "synchronized" and status["remote_verified"]
    assert effective.vector_store_id == "vs_owned"
    assert {item["file"][0] for item in client.uploads} == {"kpi_registry.md", "case_brief.md"}
    assert all(item["purpose"] == "assistants" for item in client.uploads)
    assert all(
        item["expires_after"] == {"anchor": "created_at", "seconds": 86400}
        for item in client.uploads
    )
    assert all(b"666" not in item["file"][1] for item in client.uploads)
    client.vector_stores.create.assert_awaited_once()
    assert client.vector_stores.create.call_args.kwargs["expires_after"] == {
        "anchor": "last_active_at",
        "days": 1,
    }
    assert 0 < status["storage_reservation_usd"] < 0.01
    again, second = asyncio.run(prepare_knowledge(settings, memory))
    assert again.vector_store_id == "vs_owned" and second["remote_verified"]
    assert len(client.uploads) == 2
    assert client.vector_stores.retrieve.await_count == 2
    assert (settings.root / ".runtime/knowledge_vector_state.json").is_file()


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "in_progress"])
def test_failed_ingestion_is_not_synchronized_and_cleans_created_resources(
    setup, monkeypatch, terminal
):
    settings, memory = setup
    client = FakeClient(status=terminal)
    monkeypatch.setattr("analyst.knowledge.AsyncOpenAI", lambda **_: client)
    effective, status = asyncio.run(prepare_knowledge(replace(settings, api_key="fake"), memory))
    assert status["status"] == "pending_remote_error"
    assert not status["remote_verified"] and not effective.vector_store_id
    client.vector_stores.delete.assert_awaited_once_with("vs_owned")
    client.files.delete.assert_awaited_once_with("file_1")
    assert not (settings.root / ".runtime/knowledge_vector_state.json").exists()


def test_configured_store_mock_and_zero_budget_never_provision(setup, monkeypatch):
    settings, memory = setup
    monkeypatch.setattr(
        "analyst.knowledge.AsyncOpenAI", lambda **_: pytest.fail("Unexpected provisioning")
    )
    effective, status = asyncio.run(
        prepare_knowledge(replace(settings, api_key="fake", vector_store_id="vs_external"), memory)
    )
    assert effective.vector_store_id == "vs_external"
    assert status["status"] == "configured_external_store"
    _, status = asyncio.run(
        prepare_knowledge(replace(settings, api_key="fake", mode="mock"), memory)
    )
    assert status["status"] == "pending_mock_mode"
    _, status = asyncio.run(
        prepare_knowledge(
            replace(settings, api_key="fake", max_cost_usd_per_run=0, unbounded_runs=False), memory
        )
    )
    assert status["status"] == "pending_budget"


def test_actual_indexed_storage_budget_gate_cleans_owned_resources(setup, monkeypatch):
    settings, memory = setup
    client = FakeClient()
    client.store.usage_bytes = 100_000_000_000
    monkeypatch.setattr("analyst.knowledge.AsyncOpenAI", lambda **_: client)
    effective, status = asyncio.run(
        prepare_knowledge(replace(settings, api_key="fake", unbounded_runs=False), memory)
    )
    assert status["status"] == "pending_remote_error"
    assert status["reason"] == "knowledge_storage_budget_exceeded"
    assert not effective.vector_store_id
    client.vector_stores.delete.assert_awaited_once_with("vs_owned")
    assert client.files.delete.await_count == 2


def test_unbounded_knowledge_ignores_legacy_caps_and_omits_polling_deadline(setup, monkeypatch):
    settings, memory = setup
    settings = replace(
        settings,
        api_key="fake",
        max_cost_usd_per_run=0,
        dev_api_budget_usd=0,
        knowledge_upload_max_bytes=1,
        tool_timeout_seconds=0.001,
    )
    client = FakeClient()
    client.store.usage_bytes = 100_000_000_000
    constructed = []

    def connect(**kwargs):
        constructed.append(kwargs)
        return client

    monkeypatch.setattr("analyst.knowledge.AsyncOpenAI", connect)
    effective, status = asyncio.run(prepare_knowledge(settings, memory))
    assert effective.vector_store_id == "vs_owned"
    assert status["status"] == "synchronized"
    assert status["storage_reservation_usd"] == 10
    assert constructed[0]["timeout"] is None
    assert all(
        "max_wait_seconds" not in call.kwargs
        for call in client.vector_stores.files.create_and_poll.await_args_list
    )
    client.vector_stores.delete.assert_not_awaited()
    state = json.loads((settings.root / ".runtime/knowledge_vector_state.json").read_text())
    assert state["storage_reservation_usd"] == 10
    json.dumps(state, allow_nan=False)


def test_stale_remote_fingerprint_rebuilds_instead_of_claiming_cache_hit(setup, monkeypatch):
    settings, memory = setup
    directory = settings.root / ".runtime"
    directory.mkdir(exist_ok=True)
    (directory / "knowledge_vector_state.json").write_text(
        json.dumps(
            {"fingerprint": knowledge_fingerprint(settings.root), "vector_store_id": "vs_stale"}
        )
    )
    client = FakeClient()
    monkeypatch.setattr("analyst.knowledge.AsyncOpenAI", lambda **_: client)
    effective, status = asyncio.run(prepare_knowledge(replace(settings, api_key="fake"), memory))
    assert status["status"] == "synchronized" and effective.vector_store_id == "vs_owned"
    client.vector_stores.create.assert_awaited_once()
    assert len(client.uploads) == 2


def test_installed_sdk_supports_exact_contract():
    client = AsyncOpenAI(api_key="offline")
    assert {"file", "purpose", "expires_after"} <= set(
        inspect.signature(client.files.create).parameters
    )
    assert {"name", "metadata", "expires_after"} <= set(
        inspect.signature(client.vector_stores.create).parameters
    )
    assert {"file_id", "vector_store_id", "attributes", "max_wait_seconds"} <= set(
        inspect.signature(client.vector_stores.files.create_and_poll).parameters
    )
