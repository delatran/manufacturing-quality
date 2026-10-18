"""Keep the historical bounded evaluator separate from the unbounded product profile."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import analyst.app
import pytest
from analyst.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_profile_evaluator_under_test", ROOT / "scripts/run_rebuild_eval.py"
)
assert SPEC is not None and SPEC.loader is not None
EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVAL)


@pytest.mark.asyncio
async def test_historical_eval_applies_its_declared_limits_before_workspace_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = {
        "full_suite_usd": 5.0,
        "task_usd": 20.0,
        "per_turn_usd": 0.3,
        "max_output_tokens": 2500,
        "qa_max_retries": 1,
        "max_sdk_turns": 12,
    }
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setattr(EVAL, "STATE", tmp_path)
    monkeypatch.setattr(EVAL, "validate", lambda: ({"resource_limits": limits}, {}))
    monkeypatch.setattr(EVAL, "manifest", lambda: {})
    monkeypatch.setattr(EVAL, "evaluator_manifest", lambda: {})
    monkeypatch.setattr(
        EVAL,
        "task_cost",
        lambda: {
            "conservative_accounted_usd": 16.0,
            "unresolved_reserved_usd": 0.0,
            "unknown_usage_responses": [],
        },
    )
    base = Settings(root=ROOT, api_key="synthetic-never-dispatched")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls, root: base))
    captured: list[Settings] = []

    class StopBeforeWorkspace(Exception):
        pass

    def capture_settings(settings: Settings) -> None:
        captured.append(settings)
        raise StopBeforeWorkspace

    # Stop before creating any workspace, model client, provider run or spend record.
    monkeypatch.setattr(analyst.app, "create_app", capture_settings)
    with pytest.raises(StopBeforeWorkspace):
        await EVAL.run_live(SimpleNamespace(live=True, suite_id="offline-profile-probe"))

    assert len(captured) == 1
    actual = captured[0]
    assert actual.model == "gpt-6-luna"
    assert actual.agent_backend == "agents_api"
    assert actual.unbounded_runs is False
    assert actual.run_limit("dev_api_budget_usd") == 4.0
    assert actual.run_limit("max_cost_usd_per_run") == limits["per_turn_usd"]
    assert actual.run_limit("max_output_tokens") == limits["max_output_tokens"]
    assert actual.run_limit("qa_max_retries") == limits["qa_max_retries"]
    assert actual.run_limit("max_turns") == limits["max_sdk_turns"]
    assert base.unbounded_runs is True
    assert base.reasoning_effort == "max"
    assert base.run_limit("max_cost_usd_per_run") is None
    assert not (tmp_path / "task_spend.jsonl").exists()
